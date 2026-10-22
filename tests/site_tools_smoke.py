"""Offline real Chromium exercise of site declarations and fill-only re-login.

The WebMCP native API is explicitly a simulated site implementation. MCP HTTP,
declarative forms, cookies, navigation, DOM, and Chromium are actual executions.
No site or credential request leaves the container (Playwright route fixtures).
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from worldd.asks import AskService
from worldd.browser import BrowserService, PlaywrightBackend
from worldd.storage import Store
from worldd.vault import VaultService

PAGE = """<!doctype html><h1>Fixture workspace</h1><p>private-session</p><p>private-storage-key</p>
<link rel="mcp" href="/mcp" title="Fixture MCP">
<form toolname="search_fixture" tooldescription="Find a fixture" action="/search" method="get">
<input name="term" aria-label="Term"><input type=hidden name=csrf value=private-csrf>
<button type=submit>Find</button></form><p id=result></p>
<script>localStorage.setItem('fixture_session_token','private-storage-key');Object.defineProperty(navigator,'modelContextTesting',{value:{
listTools:async()=>[{name:'draft_fixture',description:'Simulated site native tool',inputSchema:{type:'object'}}],
executeTool:async(name,args)=>{document.querySelector('#result').textContent=JSON.parse(args).text;return 'fixture draft created';}
}});</script>"""


async def main():
    store = Store(Path("/tmp/site-tools-world"))
    world = SimpleNamespace(store=store, services={})
    world.push = store.push_event
    asks, vault = AskService(world), VaultService(world)
    world.services.update(asks=asks, vault=vault)
    run_id = store.create_run(
        {
            "budget": {"usd": 20, "wall_s": 7200},
            "stage": 3,
            "reply_policy": "blockers_only",
            "identity_id": "fixture_identity",
        }
    )["run_id"]
    vault.review_terms(
        "fixture",
        "permits_labelled",
        source_url="https://fixture.test/terms",
        origin_url="https://fixture.test",
    )
    vault.provision(
        alias="fixture",
        platform="fixture",
        identity_id="fixture_identity",
        principal="operator",
        kind="password",
        label="bot_labelled",
        run_id=run_id,
        scope_classes=["read", "settings"],
        origin_url="https://fixture.test",
        scope_rules={scope: ["https://fixture.test/"] for scope in ["read", "settings"]},
        credential={
            "storage_state": {"cookies": [], "origins": []},
            "login": {
                "url": "https://fixture.test/login",
                "username_selector": "#username",
                "username": "fixture_user",
                "password_selector": "#password",
                "password": "private-password",
                "submit_selector": "button",
                "allowed_paths": ["/login", "/session"],
            },
        },
    )
    backend = PlaywrightBackend(state_dir=Path("/tmp/site-tools-browser"))
    browser = BrowserService(world, backend=backend)
    world.services["browser"] = browser
    calls, checks = [], {}
    original_open = backend.open

    async def opened(*args, **kwargs):
        handle = await original_open(*args, **kwargs)

        async def route(request_route):
            request = request_route.request
            from urllib.parse import urlsplit

            parsed = urlsplit(request.url)
            assert parsed.hostname == "fixture.test"
            path = parsed.path
            if path == "/login" or (
                path == "/doc" and "sid=private-session" not in request.headers.get("cookie", "")
            ):
                await request_route.fulfill(
                    content_type="text/html",
                    body='<h1>Sign in</h1><form method="post" action="/session"><input id=username name=username><input id=password type=password name=password><button>Sign in</button></form>',
                )
            elif path == "/session":
                assert request.post_data == "username=fixture_user&password=private-password"
                calls.append("login")
                await request_route.fulfill(
                    content_type="text/html",
                    headers={
                        "Set-Cookie": "sid=private-session; Secure; HttpOnly; SameSite=Strict; Path=/"
                    },
                    body="<h1>Signed in</h1>",
                )
            elif path == "/mcp":
                body = request.post_data_json
                calls.append(body["method"])
                if body["method"] == "notifications/initialized":
                    await request_route.fulfill(status=202, body="")
                    return
                result = (
                    {"protocolVersion": "2025-03-26"}
                    if body["method"] == "initialize"
                    else (
                        {"tools": [{"name": "save_fixture", "inputSchema": {"type": "object"}}]}
                        if body["method"] == "tools/list"
                        else {
                            "content": [
                                {"type": "text", "text": "Fixture saved"},
                                {"type": "text", "text": "private-mcp-session"},
                            ]
                        }
                    )
                )
                packet = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
                await request_route.fulfill(
                    content_type="text/event-stream",
                    headers={"Mcp-Session-Id": "private-mcp-session"},
                    body="data: " + packet + "\n\n",
                )
            elif path == "/search":
                assert "term=useful" in parsed.query
                await request_route.fulfill(content_type="text/html", body="<h1>Fixture found</h1>")
            elif path == "/captcha":
                await request_route.fulfill(
                    status=403,
                    content_type="text/html",
                    headers={"X-World-Response-Block-Type": "captcha"},
                    body="<h1>Verify you are human</h1>",
                )
            elif path == "/mfa":
                await request_route.fulfill(
                    content_type="text/html",
                    body='<h1>Verification code</h1><form action="/mfa-done"><input name=code autocomplete=one-time-code><button>Continue</button></form>',
                )
            elif path == "/mfa-done":
                await request_route.fulfill(
                    content_type="text/html", body="<h1>Verification complete</h1>"
                )
            else:
                await request_route.fulfill(content_type="text/html", body=PAGE)

        await handle["page"].route("**/*", route)
        return handle

    backend.open = opened
    try:
        session = await browser.handle(run_id, "a_open", "open_session", {"alias": "fixture"})
        session_id = session["session_id"]

        async def action(name, tool, **params):
            return await browser.handle(run_id, name, tool, {"session_id": session_id, **params})

        page = await action("a_doc", "goto", url="https://fixture.test/doc", view="both")
        checks["expired_state_relogin_restores_destination"] = page[
            "url"
        ] == "https://fixture.test/doc" and calls == ["login"]
        checks["tree_screenshot_and_three_declared_paths"] = (
            "Fixture workspace" in page["tree"]
            and bool(page["screenshot_handle"])
            and {item["path"] for item in page["site_tools"]}
            == {"webmcp", "webmcp_form", "site_mcp"}
        )
        refused = await action(
            "a_native_read", "site_call", site_tool="draft_fixture", arguments={"text": "Draft"}
        )
        checks["site_declaration_does_not_authorize_write"] = refused["kind"] == "blocked"
        native = await action(
            "a_native",
            "site_call",
            site_tool="draft_fixture",
            scope="settings",
            arguments={"text": "Draft"},
        )
        checks["simulated_native_api_actual_browser_execution"] = (
            native["site_result"]["value"] == "fixture draft created" and "Draft" in native["tree"]
        )
        form = await action(
            "a_form", "site_call", site_tool="search_fixture", arguments={"term": "useful"}
        )
        checks["declarative_form_actual_navigation"] = "Fixture found" in form["tree"]
        await action("a_again", "goto", url="https://fixture.test/doc")
        listed = await action(
            "a_list",
            "site_call",
            site_tool="__list_tools__",
            site_endpoint="https://fixture.test/mcp",
        )
        called = await action(
            "a_call",
            "site_call",
            site_tool="save_fixture",
            site_endpoint="https://fixture.test/mcp",
            scope="settings",
            arguments={},
        )
        checks["actual_http_mcp_initialize_list_call_sse"] = (
            listed["site_result"]["tools"][0]["name"] == "save_fixture"
            and called["site_result"]["content"][0]["text"] == "Fixture saved"
            and "tools/call" in calls
        )
        captcha = await action("a_captcha", "goto", url="https://fixture.test/captcha")
        checks["refuse_policy_preserves_real_captcha_without_solving"] = captcha[
            "rule_id"
        ] == "captcha_policy" and "Verify you are human" in await browser.sessions[session_id][
            "handle"
        ]["page"].inner_text("body")
        takeover = await action("a_mfa", "goto", url="https://fixture.test/mfa")
        asks.answer(takeover["ticket_id"], "approve")
        browser.takeover_start(session_id, takeover["ticket_id"])
        await browser.human_action(
            session_id, "type", {"selector": "input", "text": "private-human-code"}
        )
        await browser.human_action(session_id, "click", {"selector": "button"})
        await browser.takeover_done(session_id)
        checks["actual_mfa_takeover_and_resume"] = (
            "Verification complete" in (await action("a_after_mfa", "snapshot"))["tree"]
        )
        await action("a_close", "close")
        session_id = (
            await browser.handle(run_id, "a_reopen", "open_session", {"alias": "fixture"})
        )["session_id"]
        restored = await action("a_restored", "goto", url="https://fixture.test/doc")
        checks["encrypted_storage_state_reopens_without_password_login"] = (
            "Fixture workspace" in restored["tree"] and calls.count("login") == 1
        )
        records = store.records(run_id)
        checks["each_site_path_and_modality_recorded"] = {
            item["path"] for item in records if item["kind"] == "browser_action"
        } >= {"webmcp", "webmcp_form", "site_mcp"}
        checks["no_secret_in_observations_or_record"] = all(
            value not in json.dumps(records) + json.dumps(page) + json.dumps(called)
            for value in [
                "private-password",
                "private-session",
                "private-mcp-session",
                "private-human-code",
                "private-storage-key",
            ]
        )
        assert all(checks.values()), checks
        print(
            json.dumps(
                {
                    "checks": checks,
                    "passed": len(checks),
                    "network": "Playwright local fixture only",
                }
            )
        )
    finally:
        await browser.aclose()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
