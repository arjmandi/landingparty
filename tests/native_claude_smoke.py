"""Explicit native Claude CLI fixture pilot; never collected by standard pytest.

Run: .venv/bin/python tests/native_claude_smoke.py --claude /path/to/claude
Every provider and website response is an injected local fixture. Fresh HOME,
minimal environment, disabled telemetry, and a denying fallback proxy prevent
use of ambient credentials or intended external service access.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from worldd.app import create_app
from worldd.gateway import Gateway
from worldd.models import RunConfig
from worldd.security import create_agent_app

ROOT = Path(__file__).resolve().parents[1]
MODEL = "claude-sonnet-5-5"


def module(path):
    spec = importlib.util.spec_from_file_location("native_smoke_" + path.stem, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def event(kind, payload):
    return "event: " + kind + "\ndata: " + json.dumps({"type": kind, **payload}) + "\n\n"


def message_stream(model, *, tools=None):
    chunks = [
        event(
            "message_start",
            {
                "message": {
                    "id": "msg_fixture",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                }
            },
        )
    ]
    for index, tool in enumerate(tools or []):
        chunks += [
            event(
                "content_block_start",
                {
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": f"toolu_fixture_{index}",
                        "name": tool,
                        "input": {},
                    },
                },
            ),
            event(
                "content_block_delta",
                {
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            {"url": f"https://fixture.example/{index}", "wait": True}
                        ),
                    },
                },
            ),
            event("content_block_stop", {"index": index}),
        ]
    if not tools:
        chunks += [
            event(
                "content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}
            ),
            event(
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": "Native fixture complete."}},
            ),
            event("content_block_stop", {"index": 0}),
        ]
    chunks += [
        event(
            "message_delta",
            {
                "delta": {
                    "stop_reason": "tool_use" if tools else "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": 40},
            },
        ),
        event("message_stop", {}),
    ]
    return "".join(chunks).encode()


def listening_socket():
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


async def run(binary, output, timeout):
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    (output / "home").mkdir(mode=0o700)
    (output / "work").mkdir(mode=0o700)
    version_process = await asyncio.create_subprocess_exec(
        str(binary),
        "--version",
        env={"PATH": "/usr/bin:/bin", "HOME": str(output / "home")},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    version_output, _ = await asyncio.wait_for(version_process.communicate(), 10)
    cli_version = version_output.decode().strip()
    external_attempts, provider_requests, resource_requests = [], [], []
    active, peak = 0, 0
    overlap = asyncio.Event()

    async def deny_proxy(reader, writer):
        first = await reader.readline()
        external_attempts.append(first.decode(errors="replace").split(" ")[:2])
        writer.write(
            b"HTTP/1.1 403 Fixture external egress denied\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    fallback = await asyncio.start_server(deny_proxy, "127.0.0.1", 0)
    fallback_url = "http://127.0.0.1:" + str(fallback.sockets[0].getsockname()[1])

    async def dns(host, port):
        assert host == "fixture.example"
        return ["93.184.216.34"]

    async def website(request):
        nonlocal active, peak
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        resource_requests.append(request.url.path)
        active += 1
        peak = max(peak, active)
        if active == 2:
            overlap.set()
        try:
            await asyncio.wait_for(overlap.wait(), timeout=0.5)
        except TimeoutError:
            pass
        finally:
            active -= 1
        return httpx.Response(
            200, text="Fixture page " + request.url.path, headers={"Content-Type": "text/plain"}
        )

    frontend_token = None
    fixture_key = "fixture-only-provider-key-not-a-real-credential"

    def provider(request):
        assert request.url.host == "fixture.provider"
        assert request.headers.get("x-api-key") == fixture_key
        assert frontend_token not in json.dumps(dict(request.headers))
        assert frontend_token not in request.content.decode()
        body = json.loads(request.content)
        names = [tool["name"] for tool in body.get("tools", [])]
        assert "WebSearch" not in names and "WebFetch" not in names
        provider_requests.append(
            {
                "model": body.get("model"),
                "stream": body.get("stream"),
                "tool_names": names,
                "tool_results": sum(
                    block.get("type") == "tool_result"
                    for message in body.get("messages", [])
                    if isinstance(message.get("content"), list)
                    for block in message["content"]
                ),
            }
        )
        fetch = next((name for name in names if name.endswith("__fetch")), None)
        if len(provider_requests) == 1 and fetch:
            return httpx.Response(
                200,
                content=message_stream(body["model"], tools=[fetch, fetch]),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            content=message_stream(body["model"]),
            headers={"Content-Type": "text/event-stream"},
        )

    old_env = {
        name: os.environ.get(name)
        for name in ("WORLD_ANTHROPIC_API_KEY", "WORLD_ANTHROPIC_UPSTREAM", "WORLD_CONTROL_TOKEN")
    }
    os.environ["WORLD_ANTHROPIC_API_KEY"] = fixture_key
    os.environ["WORLD_ANTHROPIC_UPSTREAM"] = "https://fixture.provider"
    os.environ.pop("WORLD_CONTROL_TOKEN", None)
    process, servers, tasks, sockets = None, [], [], []
    world = None
    report = {
        "status": "failed",
        "cli_version": cli_version,
        "paid_external_calls": 0,
        "fixture_only": True,
        "scientific_gate_evidence": False,
    }
    try:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(website)) as network,
            httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream,
        ):
            gateway = Gateway(output / "world", {"domain_interval_s": 0}, network, resolver=dns)
            app = create_app(output / "world", gateway=gateway, lane_client=upstream)
            world = app.state.world
            await world.recover()
            created = world.create_run(
                RunConfig(
                    goal="Fetch both fixture pages and finish.",
                    budget={"usd": 20, "wall_s": 180},
                    harness_id="claude-code-native-fixture@" + cli_version.split()[0],
                    model_snapshot_id=MODEL,
                    capture_model_io=True,
                )
            )
            run_id, frontend_token = created["run_id"], created["run_token"]
            lane = FastAPI()
            lane.state.require_run_token = True
            lane.include_router(app.state.lane_router)
            listeners = [create_agent_app(world), lane]
            for listener in listeners:
                bound = listening_socket()
                sockets.append(bound)
                server = uvicorn.Server(uvicorn.Config(listener, log_level="error", lifespan="off"))
                servers.append(server)
                tasks.append(asyncio.create_task(server.serve(sockets=[bound])))
            for _ in range(200):
                if all(server.started for server in servers):
                    break
                await asyncio.sleep(0.01)
            if not all(server.started for server in servers):
                raise RuntimeError("Fixture HTTP listeners did not start")
            binding = output / "binding"
            configure = module(ROOT / "adapters/claude-code/configure.py")
            configure.write_binding(
                binding,
                configure.binding(
                    run_id,
                    world_url=f"http://127.0.0.1:{sockets[0].getsockname()[1]}",
                    lane_url=f"http://127.0.0.1:{sockets[1].getsockname()[1]}/anthropic",
                    python=sys.executable,
                ),
            )
            (output / "briefing.txt").write_text(created["preamble"])
            (output / "goal.txt").write_text(
                "Fetch https://fixture.example/0 and https://fixture.example/1 using world tools, then finish."
            )
            environment = {
                "PATH": str(binary.parent)
                + ":"
                + str(Path(sys.executable).parent)
                + ":/usr/bin:/bin",
                "HOME": str(output / "home"),
                "TMPDIR": str(output / "home"),
                "LANG": "C.UTF-8",
                "PYTHONPATH": str(ROOT),
                "WORLD_RUN_TOKEN": frontend_token,
                "ANTHROPIC_MODEL": MODEL,
                "ANTHROPIC_SMALL_FAST_MODEL": MODEL,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
                "ENABLE_TOOL_SEARCH": "false",
                "HTTP_PROXY": fallback_url,
                "HTTPS_PROXY": fallback_url,
                "ALL_PROXY": fallback_url,
                "NO_PROXY": "127.0.0.1,localhost",
            }
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(ROOT / "adapters/claude-code/run.py"),
                str(binding),
                "--briefing",
                str(output / "briefing.txt"),
                "--goal",
                str(output / "goal.txt"),
                "--transcript",
                str(output / "transcript.jsonl"),
                env=environment,
                cwd=output / "work",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = await process.communicate()
                report["timeout"] = True
            transcript = (
                (output / "transcript.jsonl").read_text()
                if (output / "transcript.jsonl").exists()
                else ""
            )
            report.update(
                cli_returncode=process.returncode,
                provider_requests=provider_requests,
                resource_requests=resource_requests,
                maximum_parallel_fetches=peak,
                external_attempts=external_attempts,
                stderr=stderr.decode(errors="replace")
                .replace(frontend_token, "[run capability]")
                .replace(fixture_key, "[fixture credential]")[-3000:],
                stdout=stdout.decode(errors="replace")[-500:],
            )
            await world.end_run(run_id, reason="harness_stopped")
            records = world.store.records(run_id)
            report.update(
                run_id=run_id,
                record_sealed=world.store.get_run(run_id)["state"] == "sealed",
                record_valid=world.store.verify_record(run_id)["valid"],
                ledger_valid=world.store.reconcile(run_id)["valid"],
                spend=world.store.spend(run_id),
                model_calls=sum(record["kind"] == "model_call" for record in records),
                final_output_present="Native fixture complete." in transcript,
                frontend_token_stripped=bool(provider_requests),
            )
            success = (
                process.returncode == 0
                and len(provider_requests) >= 2
                and provider_requests[-1]["tool_results"] == 2
                and len(resource_requests) == 2
                and peak == 2
                and report["final_output_present"]
                and report["record_sealed"]
                and report["record_valid"]
                and report["ledger_valid"]
                and report["spend"]["model"] > 0
                and not external_attempts
            )
            report["status"] = "passed" if success else "failed"
    finally:
        if process and process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        for server in servers:
            server.should_exit = True
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if world:
            await world.aclose()
        fallback.close()
        await fallback.wait_closed()
        for name, value in old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", default=shutil.which("claude"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if not args.claude:
        parser.error("An installed native Claude CLI path is required")
    output = args.output or Path(tempfile.mkdtemp(prefix="landingparty-native-claude-")) / "pilot"
    report = asyncio.run(run(Path(args.claude).absolute(), output.absolute(), args.timeout))
    print(json.dumps({**report, "artifact_directory": str(output)}, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
