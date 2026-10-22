from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from worldd.cli import main as cli_main
from worldd.mcp import MCPBridge, manifest_tools

ROOT = Path(__file__).resolve().parents[1]


def load_adapter(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


claude = load_adapter("claude_configure", "adapters/claude-code/configure.py")
claude_run = load_adapter("claude_run", "adapters/claude-code/run.py")
reference = load_adapter("reference_loop", "adapters/reference-loop/run.py")
assay = load_adapter("assay_adapter", "adapters/assay/adapter.py")


def rpc(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_claude_fresh_context_attrition_uses_stream_events(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps({"type": "system", "subtype": "init"})
        + "\n"
        + json.dumps({"type": "system", "subtype": "compact_boundary"})
        + "\n"
        + json.dumps({"type": "assistant", "message": {"id": "actual-message", "content": []}})
        + "\n"
    )
    assert claude_run.transcript_attrition(path) == {
        "compactions": 1,
        "resumed_context_messages": 0,
        "resumed_context_bytes": 0,
        "transcript_malformed_lines": 0,
    }
    with path.open("a") as stream:
        stream.write("a truncated JSON event\n")
    assert claude_run.transcript_attrition(path)["compactions"] is None


@pytest.mark.asyncio
async def test_mcp_negotiation_and_notifications():
    async with httpx.AsyncClient() as client:
        bridge = MCPBridge(client, "r_one")
        response = await bridge.handle(rpc("initialize", {"protocolVersion": "2024-11-05"}))
        assert response["result"]["protocolVersion"] == "2024-11-05"
        assert response["result"]["capabilities"] == {"tools": {}, "resources": {}}
        assert (
            await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        )
        assert bridge.initialized
        assert (await bridge.handle(rpc("ping")))["result"] == {}


@pytest.mark.asyncio
async def test_mcp_uses_run_manifest_and_preserves_blocked_data():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/manifest":
            assert request.url.params["run_id"] == "r_one"
            return httpx.Response(
                200,
                json={
                    "tools": [
                        {
                            "name": "deliver",
                            "description": "Deliver an artifact",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"body": {"type": "string"}},
                                "required": ["body"],
                            },
                        }
                    ]
                },
            )
        assert json.loads(request.content) == {
            "run_id": "r_one",
            "tool": "deliver",
            "params": {"body": "hello"},
        }
        return httpx.Response(200, json={"kind": "blocked", "type": "policy_deny"})

    async with httpx.AsyncClient(
        base_url="http://world", transport=httpx.MockTransport(handler)
    ) as client:
        bridge = MCPBridge(client, "r_one")
        listed = await bridge.handle(rpc("tools/list"))
        assert listed["result"]["tools"][0]["name"] == "deliver"
        assert listed["result"]["tools"][0]["inputSchema"]["required"] == ["body"]
        response = await bridge.handle(
            rpc("tools/call", {"name": "deliver", "arguments": {"body": "hello"}})
        )
        assert response["result"]["isError"] is False
        assert json.loads(response["result"]["content"][0]["text"])["kind"] == "blocked"
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_mcp_resource_cannot_read_another_runs_inbox():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["run_id"] == "r_one"
        return httpx.Response(200, json={"events": [{"type": "job_done"}], "cursor": 1})

    async with httpx.AsyncClient(
        base_url="http://world", transport=httpx.MockTransport(handler)
    ) as client:
        bridge = MCPBridge(client, "r_one")
        listing = await bridge.handle(rpc("resources/list"))
        uri = listing["result"]["resources"][0]["uri"]
        assert uri == "world://inbox/r_one"
        denied = await bridge.handle(rpc("resources/read", {"uri": "world://inbox/r_other"}))
        assert denied["error"]["code"] == -32602
        result = await bridge.handle(rpc("resources/read", {"uri": uri}))
        assert json.loads(result["result"]["contents"][0]["text"])["cursor"] == 1
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_mcp_parallel_tool_calls_are_not_serialized():
    active = 0
    overlap = asyncio.Event()

    async def handler(request):
        nonlocal active
        active += 1
        if active == 2:
            overlap.set()
        await asyncio.wait_for(overlap.wait(), 1)
        return httpx.Response(200, json={"kind": "silence"})

    async with httpx.AsyncClient(
        base_url="http://world", transport=httpx.MockTransport(handler)
    ) as client:
        bridge = MCPBridge(client, "r_one")
        replies = await asyncio.gather(
            *[
                bridge.handle(rpc("tools/call", {"name": "wait", "arguments": {"seconds": 1}}, i))
                for i in (1, 2)
            ]
        )
        assert {reply["id"] for reply in replies} == {1, 2}
        assert all(not reply["result"]["isError"] for reply in replies)


@pytest.mark.asyncio
async def test_mcp_protocol_errors_and_transport_failures_do_not_leak_headers():
    def handler(request):
        return httpx.Response(500, json={"detail": "secret-fixture-do-not-print"})

    async with httpx.AsyncClient(
        base_url="http://world", transport=httpx.MockTransport(handler)
    ) as client:
        bridge = MCPBridge(client, "r_one")
        assert (await bridge.handle([]))["error"]["code"] == -32600
        assert (await bridge.handle(rpc("missing")))["error"]["code"] == -32601
        assert (await bridge.handle(rpc("tools/call", {"name": "wait", "arguments": []})))["error"][
            "code"
        ] == -32602
        error = await bridge.handle(rpc("tools/call", {"name": "status"}))
        assert error["result"]["isError"] is True
        assert "secret-fixture" not in json.dumps(error)
        assert "HTTP 500" in json.dumps(error)


def test_manifest_normalization_is_strict_about_actual_tools():
    assert manifest_tools({"manifest": {"tools": [{"name": "status"}]}})[0]["name"] == "status"
    with pytest.raises(TypeError):
        manifest_tools({"tools": ["not a tool schema"]})


def test_cli_energize_and_replay_preserve_config(tmp_path, capsys):
    config = tmp_path / "run.json"
    original = {"budget": {"usd": 20, "wall_s": 7200}, "packs": ["deliver"]}
    config.write_text(json.dumps(original))
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        assert request.url.path == "/v1/run"
        return httpx.Response(200, json={"run_id": "r_created"})

    with httpx.Client(base_url="http://world", transport=httpx.MockTransport(handler)) as client:
        assert cli_main(["energize", str(config)], client=client) == 0
        assert json.loads(capsys.readouterr().out)["run_id"] == "r_created"
        assert cli_main(["holodeck", "r_archive", str(config)], client=client) == 0
    assert seen[0] == original
    assert seen[1] == {**original, "mode": "replay", "archive_in": "r_archive"}
    assert json.loads(config.read_text()) == original


@pytest.mark.parametrize(
    "verb,path",
    [
        ("beam-up", "/v1/beam-up"),
        ("red-alert", "/v1/freeze"),
        ("resume", "/v1/resume"),
        ("emergency-beam-out", "/v1/kill"),
    ],
)
def test_cli_lifecycle_verbs(verb, path, capsys):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == path
        body = json.loads(request.content)
        assert body["run_id"] == "r_one"
        if verb == "emergency-beam-out":
            assert body["dry_run"] is False
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(base_url="http://world", transport=httpx.MockTransport(handler)) as client:
        assert cli_main([verb, "r_one"], client=client) == 0
        assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_cli_act_preserves_literal_shell_characters(tmp_path, capsys):
    params = {"text": "$(do-not-execute) `do-not-execute`\nactual newline", "kind": "message"}
    params_file = tmp_path / "params.json"
    params_file.write_text(json.dumps(params))

    def handler(request):
        assert json.loads(request.content) == {"run_id": "r_one", "tool": "ask", "params": params}
        return httpx.Response(200, json={"kind": "silence"})

    with httpx.Client(base_url="http://world", transport=httpx.MockTransport(handler)) as client:
        assert cli_main(["act", "r_one", "ask", "--params", f"@{params_file}"], client=client) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "silence"


def test_cli_export_preserves_existing_files(tmp_path, capsys):
    output = tmp_path / "episode.tar.gz"

    def handler(request):
        assert request.url.path == "/v1/export"
        return httpx.Response(200, content=b"archive fixture")

    with httpx.Client(base_url="http://world", transport=httpx.MockTransport(handler)) as client:
        assert cli_main(["export", "r_one", "--output", str(output)], client=client) == 0
        assert output.read_bytes() == b"archive fixture"
        assert cli_main(["export", "r_one", "--output", str(output)], client=client) == 1
        assert output.read_bytes() == b"archive fixture"
    assert "already exists" in capsys.readouterr().err


def test_cli_stardate_and_http_error(capsys):
    def handler(request):
        if request.url.path == "/v1/status":
            return httpx.Response(200, json={"tuple": {"world_id": "landing-party@0.1"}})
        return httpx.Response(404, json={"detail": "unknown run"})

    with httpx.Client(base_url="http://world", transport=httpx.MockTransport(handler)) as client:
        assert cli_main(["stardate", "r_one"], client=client) == 0
        assert json.loads(capsys.readouterr().out) == {"world_id": "landing-party@0.1"}
        assert cli_main(["report", "r_unknown"], client=client) == 1
        assert "HTTP 404" in capsys.readouterr().err


def test_cli_shakedown_plans_without_claiming_pilot_passed(capsys):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            201, json={"study_id": "study_fixture", "state": "planned", "scientific": False}
        )

    with httpx.Client(base_url="http://world", transport=httpx.MockTransport(handler)) as client:
        assert cli_main(["shakedown"], client=client) == 0
    assert calls == [("POST", "/v1/studies/plan", {"kind": "shakedown", "spec": None})]
    assert json.loads(capsys.readouterr().out)["state"] == "planned"


def test_claude_binding_routes_model_calls_and_never_copies_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-test-fixture")
    files = claude.binding("r_one", python="/python")
    settings = files["world.settings.json"]
    assert settings["permissions"]["deny"] == ["WebSearch", "WebFetch"]
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8081/anthropic"
    assert "ANTHROPIC_CUSTOM_HEADERS" not in settings["env"]
    assert "secret-test-fixture" not in json.dumps(files)
    assert "HTTPS_PROXY" not in settings["env"]
    server = files["world.mcp.json"]["mcpServers"]["world"]
    assert server["command"] == "/python"
    assert server["env"]["WORLD_RUN_ID"] == "r_one"


def test_claude_binding_requires_real_explicit_gateway_pair():
    with pytest.raises(ValueError):
        claude.binding("r_one", gateway_url="http://gateway")
    with pytest.raises(ValueError):
        claude.binding("r_one\r\nAuthorization: attacker")
    files = claude.binding("r_one", gateway_url="http://gateway", ca_path="/world/ca.pem")
    assert files["world.settings.json"]["env"]["SSL_CERT_FILE"] == "/world/ca.pem"


def test_claude_binding_writer_preserves_existing_work(tmp_path):
    files = claude.binding("r_one")
    claude.write_binding(tmp_path, files)
    expected = (tmp_path / "world.settings.json").read_text()
    with pytest.raises(FileExistsError):
        claude.write_binding(tmp_path, claude.binding("r_other"))
    assert (tmp_path / "world.settings.json").read_text() == expected


def test_claude_launcher_treats_briefing_as_literal_argument(tmp_path):
    briefing = "The budget is $20. $(not-a-command) `not-a-command`."
    command = claude_run.command(tmp_path, briefing, "Read two pages.")
    assert command[command.index("-p") + 1] == briefing + "\n\nRead two pages."
    assert "--strict-mcp-config" in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"


def test_claude_auth_is_injected_at_runtime_without_researcher_credentials(tmp_path):
    claude.write_binding(tmp_path, claude.binding("r_one"))
    environment = claude_run.runtime_environment(
        tmp_path,
        {
            "WORLD_RUN_TOKEN": "run-test-capability",
            "WORLD_CONTROL_TOKEN": "control-test-secret",
            "WORLD_ANTHROPIC_API_KEY": "provider-test-secret",
            "WORLD_PAYMENT_API_KEY": "payment-test-secret",
            "WORLD_INBOUND_WEBHOOK_SECRET": "mail-test-secret",
            "AWS_SECRET_ACCESS_KEY": "ambient-cloud-secret",
            "PATH": "/bin",
        },
    )
    assert (
        environment["ANTHROPIC_CUSTOM_HEADERS"]
        == "X-World-Run-Id: r_one\nX-World-Run-Token: run-test-capability"
    )
    assert "WORLD_CONTROL_TOKEN" not in environment
    assert "WORLD_ANTHROPIC_API_KEY" not in environment
    assert "WORLD_PAYMENT_API_KEY" not in environment
    assert "WORLD_INBOUND_WEBHOOK_SECRET" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "run-test-capability" not in (tmp_path / "world.settings.json").read_text()


@pytest.mark.asyncio
async def test_reference_loop_uses_lane_and_runs_native_parallel_tools():
    calls, model_calls = [], []
    overlap = asyncio.Event()
    active = 0

    async def world_handler(request):
        nonlocal active
        body = json.loads(request.content) if request.content else {}
        calls.append((request.url.path, body))
        if request.url.path == "/v1/run":
            assert "max-steps=3" in body["harness_id"]
            return httpx.Response(
                200,
                json={
                    "run_id": "r_one",
                    "preamble": "Exact briefing.",
                    "manifest": {
                        "tools": [
                            {
                                "name": "fetch",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"url": {"type": "string"}},
                                },
                            }
                        ]
                    },
                },
            )
        if request.url.path == "/v1/act":
            active += 1
            if active == 2:
                overlap.set()
            await asyncio.wait_for(overlap.wait(), 1)
            return httpx.Response(200, json={"kind": "result", "url": body["params"]["url"]})
        assert request.url.path == "/v1/beam-up"
        assert body["reason"] == "agent_finished"
        return httpx.Response(200, json={"state": "sealed"})

    def lane_handler(request):
        body = json.loads(request.content)
        model_calls.append(body)
        assert request.url.path == "/openai/v1/chat/completions"
        assert request.headers["X-World-Run-Id"] == "r_one"
        assert body["model"] == "fixture-model"
        assert body["messages"][0] == {"role": "system", "content": "Exact briefing."}
        if len(model_calls) == 1:
            assistant = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": "fetch",
                            "arguments": json.dumps({"url": f"https://example.com/{i}"}),
                        },
                    }
                    for i in (1, 2)
                ],
            }
        else:
            assert [
                message["tool_call_id"] for message in body["messages"] if message["role"] == "tool"
            ] == ["call_1", "call_2"]
            assistant = {"role": "assistant", "content": "Done."}
        return httpx.Response(200, json={"choices": [{"message": assistant}]})

    async with (
        httpx.AsyncClient(
            base_url="http://world", transport=httpx.MockTransport(world_handler)
        ) as wc,
        httpx.AsyncClient(
            base_url="http://lane/openai/v1/", transport=httpx.MockTransport(lane_handler)
        ) as lc,
    ):
        result = await reference.run_reference(
            {"goal": "Read two pages."},
            "fixture-model",
            max_steps=3,
            world_client=wc,
            lane_client=lc,
        )
    assert {key: result[key] for key in ("run_id", "steps", "reason", "output")} == {
        "run_id": "r_one",
        "steps": 2,
        "reason": "agent_finished",
        "output": "Done.",
    }
    assert result["messages"][-1]["content"] == "Done."
    assert active == 2
    assert len(model_calls) == 2
    assert calls[-1][0] == "/v1/beam-up"


@pytest.mark.asyncio
async def test_reference_loop_seals_when_provider_fails():
    sealed = []

    def world_handler(request):
        if request.url.path == "/v1/run":
            return httpx.Response(
                200, json={"run_id": "r_one", "preamble": "briefing", "manifest": {"tools": []}}
            )
        sealed.append(json.loads(request.content))
        return httpx.Response(200, json={"state": "sealed"})

    async with (
        httpx.AsyncClient(
            base_url="http://world", transport=httpx.MockTransport(world_handler)
        ) as wc,
        httpx.AsyncClient(
            base_url="http://lane/openai/v1/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(429, json={"error": "budget exhausted"})
            ),
        ) as lc,
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await reference.run_reference({}, "fixture", world_client=wc, lane_client=lc)
    assert sealed == [{"run_id": "r_one", "reason": "harness_stopped"}]


async def test_reference_existing_life_context_sleeps_without_new_run_or_second_model_call():
    calls, models = [], []
    captured = [
        {"role": "system", "content": "Old session briefing"},
        {"role": "user", "content": "Actual prior goal"},
        {"role": "assistant", "content": "Actual prior answer"},
    ]
    refs = [{"run_id": "r_source", "path": "harness-context.json", "hash": "sha256:" + "1" * 64}]

    def world_handler(request):
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer scoped-test-capability"
        if request.url.path == "/v1/status":
            return httpx.Response(200, json={"state": "running"})
        assert request.url.path == "/v1/act"
        assert json.loads(request.content)["tool"] == "sleep"
        return httpx.Response(200, json={"kind": "silence", "action_id": "a_sleep"})

    def lane_handler(request):
        body = json.loads(request.content)
        models.append(body)
        assert body["messages"] == [
            {"role": "system", "content": "Current exact briefing"},
            *captured[1:],
        ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_sleep",
                                    "type": "function",
                                    "function": {"name": "sleep", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    async with (
        httpx.AsyncClient(
            base_url="http://world", transport=httpx.MockTransport(world_handler)
        ) as wc,
        httpx.AsyncClient(
            base_url="http://lane/", transport=httpx.MockTransport(lane_handler)
        ) as lc,
    ):
        result = await reference.run_reference(
            {"goal": "Actual prior goal"},
            "fixture",
            run_info={
                "run_id": "r_existing",
                "run_token": "scoped-test-capability",
                "preamble": "Current exact briefing",
                "manifest": {"tools": [{"name": "sleep", "inputSchema": {"type": "object"}}]},
            },
            initial_messages=captured,
            context_refs=refs,
            world_client=wc,
            lane_client=lc,
        )
    assert calls == ["/v1/status", "/v1/act"] and len(models) == 1
    assert result["reason"] == "sleep" and result["context_refs"] == refs
    assert result["messages"][-1]["tool_call_id"] == "call_sleep"
    assert captured[0]["content"] == "Old session briefing"


async def test_reference_resume_requires_actual_capture_references():
    with pytest.raises(ValueError, match="source capture references"):
        await reference.run_reference(
            {},
            "fixture",
            initial_messages=[{"role": "assistant", "content": "Cannot invent continuity"}],
        )


@pytest.mark.asyncio
async def test_assay_preserves_tickets_and_sets_wait_without_predictions():
    def handler(request):
        assert json.loads(request.content) == {
            "run_id": "r_one",
            "tool": "fetch",
            "params": {"url": "https://example.com", "wait": True},
        }
        return httpx.Response(
            200, json={"kind": "ticket", "ticket_id": "t_one", "action_id": "a_one"}
        )

    async with httpx.AsyncClient(
        base_url="http://world", transport=httpx.MockTransport(handler)
    ) as client:
        result = await assay.AssayAdapter(client, "r_one").act(
            "fetch", {"url": "https://example.com"}
        )
    assert result["kind"] == "ticket"
    assert assay.assay_budget(20) == 21
    with pytest.raises(ValueError):
        assay.assay_budget(20, margin=0)
    with pytest.raises(ValueError):
        assay.assay_budget("NaN")


@pytest.mark.asyncio
async def test_reference_loop_against_real_control_plane_and_model_lane(tmp_path, monkeypatch):
    """Exercise actual run/tool/charge/seal APIs, replacing only provider transport."""
    from worldd.app import create_app

    monkeypatch.setenv("WORLD_OPENAI_API_KEY", "provider-fixture-secret")
    provider_requests = []

    def provider(request):
        payload = json.loads(request.content)
        provider_requests.append(payload)
        assert request.headers["authorization"] == "Bearer provider-fixture-secret"
        assert request.url.path == "/v1/chat/completions"
        if len(provider_requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_status",
                        "type": "function",
                        "function": {
                            "name": "status",
                            "arguments": "{}",
                        },
                    },
                    {
                        "id": "call_deliver",
                        "type": "function",
                        "function": {
                            "name": "deliver",
                            "arguments": json.dumps(
                                {
                                    "kind": "report",
                                    "body": "Actual core integration artifact.",
                                }
                            ),
                        },
                    },
                ],
            }
        else:
            tool_messages = [item for item in payload["messages"] if item["role"] == "tool"]
            assert [item["tool_call_id"] for item in tool_messages] == [
                "call_status",
                "call_deliver",
            ]
            assert json.loads(tool_messages[0]["content"])["kind"] == "state"
            assert json.loads(tool_messages[1]["content"])["kind"] == "silence"
            message = {"role": "assistant", "content": "Artifact saved."}
        return httpx.Response(
            200,
            json={
                "model": "fixture-integration",
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20},
            },
        )

    prices = {
        "models": {
            "fixture-integration": {
                "provider": "openai",
                "input": "2",
                "output": "10",
                "cache_read": "1",
                "context_window": 1000,
                "max_output_tokens": 100,
            }
        }
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client:
        app = create_app(tmp_path, lane_client=provider_client, prices=prices)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://world"
            ) as wc,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://world/openai/v1/"
            ) as lc,
        ):
            result = await reference.run_reference(
                {
                    "goal": "Save one report.",
                    "packs": ["deliver"],
                    "budget": {"usd": 1, "wall_s": 300},
                },
                "fixture-integration",
                max_steps=3,
                max_tokens=100,
                world_client=wc,
                lane_client=lc,
            )
            assert result["reason"] == "agent_finished"
            assert result["output"] == "Artifact saved."
            run_id = result["run_id"]
            status = (await wc.get("/v1/status", params={"run_id": run_id})).json()
            assert status["state"] == "sealed"
            assert status["spent"]["model"] == 0.0006
            report = (await wc.get("/v1/report", params={"run_id": run_id})).json()
            assert report["integrity"]["record"]["valid"]
            assert report["integrity"]["ledger"]["valid"]
            artifact = next((tmp_path / "runs" / run_id / "work-snapshot" / "out").glob("*.json"))
            assert json.loads(artifact.read_text())["body"] == "Actual core integration artifact."
            record = (tmp_path / "runs" / run_id / "record.jsonl").read_text()
            assert "provider-fixture-secret" not in record
    assert len(provider_requests) == 2


@pytest.mark.asyncio
async def test_mcp_against_actual_registered_manifest_and_run_tools(tmp_path):
    from worldd.app import create_app

    app = create_app(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://world"
        ) as client,
    ):
        started = await client.post("/v1/run", json={"packs": ["deliver"]})
        run_id = started.json()["run_id"]
        bridge = MCPBridge(client, run_id)
        listing = await bridge.handle(rpc("tools/list"))
        tools = {item["name"]: item for item in listing["result"]["tools"]}
        assert "deliver" in tools
        assert "watch" not in tools
        assert "believe" not in tools
        assert set(tools["deliver"]["inputSchema"]["required"]) == {"kind", "body"}
        response = await bridge.handle(
            rpc(
                "tools/call",
                {
                    "name": "ask",
                    "arguments": {"kind": "approval", "text": "Gate check."},
                },
            )
        )
        reply = json.loads(response["result"]["content"][0]["text"])
        assert reply["kind"] == "blocked"
        assert reply["rule_id"] == "stage_gate"
        sealed = await client.post("/v1/beam-up", json={"run_id": run_id})
        assert sealed.status_code == 200


async def test_assay_authenticates_manifest_and_actions_with_run_capability(monkeypatch):
    monkeypatch.setenv("WORLD_RUN_TOKEN", "fixture-run-capability")
    paths = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer fixture-run-capability"
        assert "x-world-control-token" not in request.headers
        paths.append(request.url.path)
        return httpx.Response(
            200, json={"tools": []} if request.url.path == "/v1/manifest" else {"kind": "state"}
        )

    async with httpx.AsyncClient(
        base_url="http://agent", transport=httpx.MockTransport(handler)
    ) as client:
        adapter = assay.AssayAdapter(client, "r_one")
        await adapter.manifest()
        await adapter.act("status", {})
    assert paths == ["/v1/manifest", "/v1/act"]


def test_mcp_read_hints_enable_native_parallel_reads_without_marking_writes_safe():
    tools = manifest_tools(
        {"tools": [{"name": name} for name in ("fetch", "status", "send", "ask")]}
    )
    by_name = {tool["name"]: tool for tool in tools}
    assert by_name["fetch"]["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
    assert by_name["status"]["annotations"]["readOnlyHint"] is True
    assert "annotations" not in by_name["send"] and "annotations" not in by_name["ask"]
