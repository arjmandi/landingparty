"""Explicit native Claude/Codex runner pilot with local provider fixtures only.

Not collected by pytest; pass installed CLI paths and a fresh output directory.
"""

import argparse
import asyncio
import importlib.util
import json
import os
import socket
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from worldd.app import create_app
from worldd.gateway import Gateway
from worldd.models import RunConfig
from worldd.security import create_agent_app


def listener():
    bound = socket.socket()
    bound.bind(("127.0.0.1", 0))
    bound.listen()
    return bound


def responses(model, tool=None):
    item = {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [
            {"type": "output_text", "text": "Native runner fixture complete.", "annotations": []}
        ],
    }
    result = {
        "id": "resp_fixture",
        "object": "response",
        "model": model,
        "status": "completed",
        "output": [item],
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 25,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 125,
        },
    }
    events = [
        {
            "type": "response.created",
            "response": {**result, "status": "in_progress", "output": [], "usage": None},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "delta": item["content"][0]["text"],
        },
        {
            "type": "response.output_text.done",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "text": item["content"][0]["text"],
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": result},
    ]
    if tool:
        arguments = json.dumps({"url": "https://fixture.example/native", "wait": True})
        item = {
            "id": "fc_fixture",
            "type": "function_call",
            "call_id": "call_fixture",
            "name": tool[1] if isinstance(tool, tuple) else tool,
            "arguments": arguments,
            "status": "completed",
        }
        if isinstance(tool, tuple):
            item["namespace"] = tool[0]
        result["output"] = [item]
        events = [
            {
                "type": "response.created",
                "response": {**result, "status": "in_progress", "output": [], "usage": None},
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "status": "in_progress", "arguments": ""},
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": item["id"],
                "output_index": 0,
                "delta": arguments,
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": item["id"],
                "output_index": 0,
                "arguments": arguments,
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": result},
        ]
    return "".join(
        "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events
    ).encode()


async def main(options):
    output = options.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    fixture_path = Path(__file__).with_name("native_claude_smoke.py")
    spec = importlib.util.spec_from_file_location("fixture_claude_stream", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    provider_calls, external_attempts, servers, tasks, sockets = [], [], [], [], []
    secrets = {
        "openai": "fixture-openai-provider-key",
        "anthropic": "fixture-anthropic-provider-key",
    }
    for provider, secret in secrets.items():
        os.environ["WORLD_" + provider.upper() + "_API_KEY"] = secret
        os.environ["WORLD_" + provider.upper() + "_UPSTREAM"] = "https://fixture.provider"

    async def reject(reader, writer):
        external_attempts.append((await reader.readline()).decode(errors="replace").split(" ")[:2])
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    fallback = await asyncio.start_server(reject, "127.0.0.1", 0)
    fallback_url = "http://127.0.0.1:" + str(fallback.sockets[0].getsockname()[1])

    async def provider(request):
        body = json.loads(request.content)
        assert request.url.host == "fixture.provider"
        assert "X-World-Run-Token" not in request.headers
        previous = sum(call["model"] == body["model"] for call in provider_calls)
        tool_names = [tool.get("name", "") for tool in body.get("tools", [])]
        fetch = next((name for name in tool_names if name.endswith("__fetch")), None)
        if not fetch:
            fetch = next(
                (
                    (tool["name"], function["name"])
                    for tool in body.get("tools", [])
                    if tool.get("type") == "namespace" and tool.get("name") == "mcp__world"
                    for function in tool.get("tools", [])
                    if function.get("name") == "fetch"
                ),
                None,
            )
        assert fetch, "Native CLI did not expose its registered World MCP fetch tool"
        provider_calls.append(
            {"model": body["model"], "path": request.url.path, "stream": body.get("stream")}
        )
        if request.url.path.endswith("messages"):
            assert request.headers["x-api-key"] == secrets["anthropic"]
            assert not any(
                tool["name"] in {"WebSearch", "WebFetch"} for tool in body.get("tools", [])
            )
            raw = fixture.message_stream(body["model"], tools=[fetch] if not previous else None)
        else:
            assert request.headers["authorization"] == "Bearer " + secrets["openai"]
            raw = responses(body["model"], tool=fetch if not previous else None)
        return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})

    async def website(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text="Actual archived native runner fixture resource.")

    async def dns(host, port):
        assert host == "fixture.example"
        return ["93.184.216.34"]

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream,
        httpx.AsyncClient(transport=httpx.MockTransport(website)) as network,
    ):
        app = create_app(
            output / "world",
            gateway=Gateway(output / "world", client=network, resolver=dns),
            lane_client=upstream,
        )
        world = app.state.world
        service = world.services["harnesses"]
        service.environment.update(
            WORLD_CLAUDE_CLI=str(options.claude.resolve()),
            WORLD_CODEX_CLI=str(options.codex.resolve()),
        )
        lane = FastAPI()
        lane.state.require_run_token = True
        lane.include_router(app.state.lane_router)
        report = {
            "fixture_only": True,
            "paid_external_calls": 0,
            "scientific_gate_evidence": False,
            "harnesses": {},
        }
        try:
            for application in (create_agent_app(world), lane):
                bound = listener()
                sockets.append(bound)
                server = uvicorn.Server(
                    uvicorn.Config(application, log_level="error", lifespan="off")
                )
                servers.append(server)
                tasks.append(asyncio.create_task(server.serve(sockets=[bound])))
            for _ in range(200):
                if all(server.started for server in servers):
                    break
                await asyncio.sleep(0.01)
            assert all(server.started for server in servers)
            service.configure_urls(
                agent_url="http://127.0.0.1:" + str(sockets[0].getsockname()[1]),
                lane_url="http://127.0.0.1:" + str(sockets[1].getsockname()[1]),
            )
            for harness_id, model in (
                ("claude-code", "claude-sonnet-4-20250514"),
                ("codex", "gpt-4o-mini-2024-07-18"),
            ):
                created = world.create_run(
                    RunConfig(
                        goal="Return the fixture completion.",
                        stage=1,
                        packs=["deliver", "belief", "schedule"],
                        budget={"usd": 20, "wall_s": 90},
                        harness_id=harness_id + "@native-runner-fixture",
                        model_snapshot_id=model,
                    )
                )
                created["proxy"] = {
                    "server": fallback_url,
                    "ca_path": "/etc/ssl/certs/ca-certificates.crt",
                }
                before = len(provider_calls)
                session = await service.launch(
                    created, harness_id, auth_mode="api_key", model=model
                )
                session_id = session["session_id"]
                try:
                    await asyncio.wait_for(asyncio.shield(service.tasks[session_id]), 35)
                except TimeoutError:
                    await service.stop(session_id)
                final = service.get(session_id)
                if final["state"] != "completed":
                    (output / (harness_id + "-failed-logs.json")).write_text(
                        json.dumps(service.logs(session_id), indent=2)
                    )
                report["harnesses"][harness_id] = {
                    "state": final["state"],
                    "exit_code": final["exit_code"],
                    "provider_requests": len(provider_calls) - before,
                    "model_calls": sum(
                        record["kind"] == "model_call"
                        for record in world.store.records(created["run_id"])
                    ),
                    "record_valid": world.store.verify_record(created["run_id"])["valid"],
                    "ledger_valid": world.store.reconcile(created["run_id"])["valid"],
                    "sealed": world.store.get_run(created["run_id"])["state"] == "sealed",
                    "world_mcp_fetch": any(
                        record.get("kind") == "action" and record.get("tool") == "fetch"
                        for record in world.store.records(created["run_id"])
                    ),
                }
            report["external_attempts"] = len(external_attempts)
            report["external_attempt_details"] = external_attempts
            report["passed"] = (
                all(
                    item["state"] == "completed"
                    and item["provider_requests"] >= 1
                    and item["model_calls"] >= 1
                    and item["record_valid"]
                    and item["ledger_valid"]
                    and item["sealed"]
                    and item["world_mcp_fetch"]
                    for item in report["harnesses"].values()
                )
                and not external_attempts
            )
        finally:
            for server in servers:
                server.should_exit = True
            await asyncio.gather(*tasks, return_exceptions=True)
            await world.aclose()
            fallback.close()
            await fallback.wait_closed()
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude", type=Path, required=True)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
