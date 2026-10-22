"""Declared site tools stay untrusted and use the browser's existing authority."""

import json

import pytest
from capability_helpers import CapabilityWorld
from test_browser import Backend, open_session

from worldd.approvals import action_hash
from worldd.browser import BrowserService
from worldd.site_tools import SiteToolError, SiteTools


class RPCPage:
    def __init__(self, *, sse=False):
        self.calls, self.sse = [], sse

    async def evaluate(self, script, arguments=None):
        if arguments is None:
            return [
                {
                    "tool_id": "endpoint:https://fixture.test/mcp",
                    "path": "site_mcp",
                    "name": "Fixture MCP",
                    "url": "https://fixture.test/mcp",
                    "input_schema": {},
                }
            ]
        body = arguments["body"]
        self.calls.append(arguments)
        method = body["method"]
        if method == "notifications/initialized":
            return {"status": 202, "text": ""}
        result = (
            {"protocolVersion": "2025-03-26"}
            if method == "initialize"
            else (
                {"tools": [{"name": "publish", "inputSchema": {"type": "object"}}]}
                if method == "tools/list"
                else {"content": [{"type": "text", "text": body["params"]["arguments"]["text"]}]}
            )
        )
        packet = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
        return {
            "status": 200,
            "text": "data: " + packet + "\n\n" if self.sse else packet,
            "session_id": "private-mcp-session",
        }


@pytest.mark.parametrize("sse", [False, True])
async def test_declared_site_mcp_initializes_lists_and_calls_with_exact_authorization(sse):
    page = RPCPage(sse=sse)
    tools = SiteTools({"page": page})
    authorized = []

    async def authorize(endpoint, body):
        authorized.append((endpoint, action_hash(body)))

    descriptor = await tools.describe("__list_tools__", "https://fixture.test/mcp")
    listed = await tools.invoke(descriptor, {}, authorize_rpc=authorize)
    assert listed["tools"][0]["name"] == "publish"
    surfaced = await tools.discover()
    assert any(
        item.get("discovery") == "tools/list" and item["name"] == "publish" for item in surfaced
    )
    descriptor = await tools.describe("publish", "https://fixture.test/mcp")
    result = await tools.invoke(descriptor, {"text": "Fixture post"}, authorize_rpc=authorize)
    assert result["content"][0]["text"] == "Fixture post"
    assert [item["body"]["method"] for item in page.calls] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/list",
        "tools/call",
    ]
    assert len(authorized) == len(page.calls)
    assert page.calls[-1]["headers"]["Mcp-Session-Id"] == "private-mcp-session"
    assert "private-mcp-session" not in json.dumps(result)
    with pytest.raises(SiteToolError, match="not declared"):
        await tools.describe("publish", "https://attacker.test/mcp")
    with pytest.raises(SiteToolError, match="not declared"):
        await tools.invoke({**descriptor, "name": "delete_all"}, {}, authorize_rpc=authorize)


class ToolBackend(Backend):
    async def describe_site_tool(self, handle, name, endpoint):
        return {
            "name": name,
            "tool_id": "mcp:" + name,
            "path": "site_mcp",
            "url": endpoint,
            "input_schema": {"type": "object"},
        }

    async def site_call(self, handle, descriptor, arguments, *, authorize_rpc):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        await authorize_rpc(descriptor["url"], body)
        if hasattr(self, "network_check"):
            await self.network_check(body)
        return {"content": [{"text": "fixture-secret-value"}], "access_token": "hidden-token"}


async def test_browser_records_site_path_and_never_trusts_read_hint_or_leaks_result(tmp_path):
    world = CapabilityWorld(tmp_path)
    run_id = world.run()
    backend = ToolBackend()
    browser = BrowserService(world, backend=backend, clock=world.clock)
    world.services["browser"] = browser
    try:
        session_id = await open_session(world, browser, run_id)
        await browser.handle(
            run_id, "a_page", "goto", {"session_id": session_id, "url": "https://fixture.test/doc"}
        )

        async def network_check(body):
            session = browser.sessions[session_id]
            allowed = await browser.authorize_network_request(
                run_id,
                "POST",
                "https://fixture.test/mcp",
                {},
                json.dumps(body).encode(),
                token=session["token"],
            )
            denied = await browser.authorize_network_request(
                run_id,
                "POST",
                "https://fixture.test/mcp",
                {},
                json.dumps({**body, "id": 2}).encode(),
                token=session["token"],
            )
            assert allowed["allow"] and allowed["jsonrpc_response_id"] == 1
            assert allowed["protocol_header_names"] == ["mcp-session-id"]
            assert not denied["allow"] and denied.get("jsonrpc_response_id") is None

        backend.network_check = network_check
        params = {
            "session_id": session_id,
            "site_tool": "publish",
            "site_endpoint": "https://fixture.test/mcp",
            "arguments": {"text": "hi"},
        }
        assert (await browser.handle(run_id, "a_read", "site_call", params))[
            "rule_id"
        ] == "site_mcp_scope"
        result = await browser.handle(
            run_id, "a_list", "site_call", {**params, "site_tool": "__list_tools__"}
        )
        assert result["kind"] == "result"
        assert "fixture-secret-value" not in json.dumps(result)
        assert "hidden-token" not in json.dumps(result)
        observations = [
            item for item in world.store.records(run_id) if item["kind"] == "browser_observation"
        ]
        assert observations[-1]["path"] == "site_mcp" and observations[-1]["modality"] == "tree"
        assert any(
            item["kind"] == "browser_action" and item["path"] == "site_mcp"
            for item in world.store.records(run_id)
        )
    finally:
        await browser.aclose()
        world.close()


async def test_expired_storage_state_relogs_in_without_replaying_pending_write(tmp_path):
    class LoginBackend(Backend):
        async def goto(self, handle, url):
            handle["url"] = url
            if not handle.get("logged_in"):
                self.challenge = "login"

        async def login(self, handle, login):
            handle.update(url=login["url"], logged_in=True)
            self.challenge = None
            self.actions.append({"login": True})

    world = CapabilityWorld(tmp_path)
    run_id = world.run()
    world.provision(
        run_id,
        credential={
            "storage_state": {"cookies": [], "origins": []},
            "login": {"url": "https://fixture.test/login", "password": "private-password"},
        },
    )
    backend = LoginBackend()
    browser = BrowserService(world, backend=backend, clock=world.clock)
    world.services["browser"] = browser
    try:
        session_id = (await browser.handle(run_id, "a_open", "open_session", {"alias": "fixture"}))[
            "session_id"
        ]
        result = await browser.handle(
            run_id, "a_doc", "goto", {"session_id": session_id, "url": "https://fixture.test/doc"}
        )
        assert result["url"] == "https://fixture.test/doc"
        assert backend.actions == [{"login": True}]
        assert "private-password" not in json.dumps(world.store.records(run_id))
    finally:
        await browser.aclose()
        world.close()
