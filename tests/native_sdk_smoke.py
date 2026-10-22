"""Explicit optional native SDK fixture pilot, never collected by pytest.

Run with the standards extra installed: python tests/native_sdk_smoke.py --output /tmp/sdk-pilot
All website/provider responses are local MockTransport fixtures. This checks SDK
software compatibility, not scientific validity or a real network namespace.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
from importlib.metadata import version
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from worldd.app import create_app
from worldd.gateway import Gateway
from worldd.security import create_agent_app

MODEL = "gpt-4o-mini-2024-07-18"


def socket_listener():
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


class Fixture:
    def __init__(self, output):
        self.output, self.calls, self.fetches = output, [], []
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.sync(self.start())

    def sync(self, awaitable):
        return asyncio.run_coroutine_threadsafe(awaitable, self.loop).result(timeout=60)

    async def start(self):
        async def dns(host, port):
            assert host == "fixture.example"
            return ["93.184.216.34"]

        async def website(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            self.fetches.append(request.url.path)
            return httpx.Response(200, text="Native SDK fixture page.")

        async def provider(request):
            body = json.loads(request.content)
            assert request.headers["authorization"] == "Bearer fixture-provider-key"
            assert "X-World-Run-Token" not in request.headers
            self.calls.append(body)
            tools = [tool["function"]["name"] for tool in body.get("tools", [])]
            tool_results = [message for message in body["messages"] if message["role"] == "tool"]
            name = (
                next(name for name in tools if name.endswith("fetch"))
                if not tool_results
                else "finish"
            )
            if name == "finish":
                name = next(name for name in tools if "finish" in name.lower())
            arguments = (
                {"url": "https://fixture.example/sdk", "wait": True}
                if not tool_results
                else {"message": "Native SDK fixture complete."}
            )
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_fixture_" + str(len(self.calls)),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            }
            assert body.get("stream") is not True, (
                "This fixture registers non-streaming SDK requests"
            )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": MODEL,
                    "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125},
                },
            )

        self.network = httpx.AsyncClient(transport=httpx.MockTransport(website))
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        gateway = Gateway(
            self.output / "world", {"domain_interval_s": 0}, self.network, resolver=dns
        )
        self.app = create_app(self.output / "world", gateway=gateway, lane_client=self.upstream)
        self.world = self.app.state.world
        await self.world.recover()
        lane = FastAPI()
        lane.state.require_run_token = True
        lane.include_router(self.app.state.lane_router)
        self.sockets, self.servers, self.tasks, self.urls = [], [], [], []
        for app in (self.app, create_agent_app(self.world), lane):
            listener = socket_listener()
            server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
            self.sockets.append(listener)
            self.servers.append(server)
            self.tasks.append(asyncio.create_task(server.serve(sockets=[listener])))
            self.urls.append("http://127.0.0.1:" + str(listener.getsockname()[1]))
        for _ in range(200):
            if all(server.started for server in self.servers):
                return
            await asyncio.sleep(0.01)
        raise RuntimeError("World fixture listeners did not start")

    async def close(self):
        for server in self.servers:
            server.should_exit = True
        await asyncio.gather(*self.tasks)
        await self.world.aclose()
        await self.network.aclose()
        await self.upstream.aclose()

    def stop(self):
        try:
            self.sync(self.close())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
            self.loop.close()


async def check_openenv(fixture):
    from faces.openenv.native import LandingPartyAction, LandingPartyClient, create_native_app

    app = create_native_app(
        world_url=fixture.urls[0],
        agent_url=fixture.urls[1],
        config={"goal": "SDK fixture", "budget": {"usd": 1, "wall_s": 120}},
    )
    listener = socket_listener()
    server = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        async with LandingPartyClient(
            "http://127.0.0.1:" + str(listener.getsockname()[1])
        ) as client:
            first = await client.reset(seed=7)
            assert first.reward is None and not first.done
            state = await client.state()
            assert state.episode_id == first.observation.run_id
            ticket = await client.step(
                LandingPartyAction(
                    tool="fetch", params={"url": "https://fixture.example/openenv", "wait": False}
                )
            )
            assert ticket.observation.reply["kind"] == "ticket" and ticket.reward is None
            second = await client.reset(seed=7)
            assert second.observation.run_id != first.observation.run_id
            assert "run_token" not in first.observation.reply
            return {
                "typed_websocket_reset_step_state": True,
                "ticket_valid": True,
                "reset_creates_new_run": True,
                "live_reward": None,
                "sealed_source_run": first.observation.run_id,
            }
    finally:
        server.should_exit = True
        await task


async def check_inspect(fixture, run_id):
    from inspect_ai import eval_async

    from faces.inspect.native import inspect_task

    logs = await eval_async(
        inspect_task([{"run_id": run_id, "data_dir": fixture.output / "world"}]),
        model="mockllm/model",
        log_dir=str(fixture.output / "inspect"),
    )
    assert len(logs) == 1 and logs[0].status == "success", logs[0].error
    score = next(iter(logs[0].samples[0].scores.values()))
    assert score.value == {} and score.metadata["reward"] is None
    return {
        "native_eval_completed": True,
        "passive_model_calls": 0,
        "live_reward": None,
        "source_chain_verified": score.metadata["source"]["sealed"],
    }


async def check_harbor(fixture, run_id):
    from harbor.models.task.task import Task
    from harbor.models.trial.config import VerifierConfig
    from harbor.models.trial.paths import TrialPaths
    from harbor.verifier.factory import VerifierFactory

    from faces.harbor.package import build_task

    package = build_task(fixture.output / "harbor-task", instruction="SDK fixture", run_id=run_id)
    task = Task(package)
    verifier = VerifierFactory.create_verifier_from_config(
        VerifierConfig(
            import_path="faces.harbor.native:WorldNoScoreVerifier",
            kwargs={"run_id": run_id, "world_url": fixture.urls[0]},
        ),
        task=task,
        trial_paths=TrialPaths(fixture.output / "harbor-trial"),
        environment=None,
    )
    result = await verifier.verify()
    assert result.rewards is None
    return {
        "native_task_parsed": True,
        "native_verifier_factory": True,
        "rewards": None,
        "docker_namespace_exercised": False,
    }


async def check_openhands(fixture):
    async with httpx.AsyncClient(base_url=fixture.urls[0], trust_env=False) as client:
        response = await client.post(
            "/v1/run",
            json={
                "goal": "Fetch the fixture page and finish.",
                "budget": {"usd": 2, "wall_s": 120},
                "harness_id": "openhands-sdk-native-fixture@" + version("openhands-sdk"),
                "model_snapshot_id": MODEL,
                "capture_model_io": True,
            },
        )
        response.raise_for_status()
        run = response.json()
        (fixture.output / "briefing.txt").write_text(run["preamble"])
        (fixture.output / "goal.txt").write_text(
            "Use world fetch on https://fixture.example/sdk then finish."
        )
        environment = {
            key: os.environ[key]
            for key in (
                "HOME",
                "XDG_DATA_HOME",
                "XDG_CACHE_HOME",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
            )
        }
        environment.update(
            PATH=str(Path(sys.executable).parent) + ":/usr/bin:/bin",
            PYTHONPATH=str(Path(__file__).resolve().parents[1]),
            WORLD_RUN_TOKEN=run["run_token"],
            WORLD_RUN_ID=run["run_id"],
            LITELLM_LOCAL_MODEL_COST_MAP="true",
            DO_NOT_TRACK="1",
            OPENHANDS_SUPPRESS_BANNER="1",
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "adapters.openhands.native",
            "--model",
            MODEL,
            "--workspace",
            str(fixture.output / "work"),
            "--briefing",
            str(fixture.output / "briefing.txt"),
            "--goal",
            str(fixture.output / "goal.txt"),
            "--transcript",
            str(fixture.output / "openhands.jsonl"),
            "--lane-url",
            fixture.urls[2] + "/openai/v1",
            "--world-url",
            fixture.urls[1],
            "--max-steps",
            "5",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        (fixture.output / "openhands.stderr").write_bytes(stderr)
        assert process.returncode == 0, stderr.decode(errors="replace")[-3000:]
        result = json.loads(stdout.decode().splitlines()[-1])
        assert result["status"].lower().endswith("finished"), result
        transcript = (fixture.output / "openhands.jsonl").read_text()
        assert run["run_token"] not in transcript and "fixture-provider-key" not in transcript
        response = await client.post(
            "/v1/beam-up", json={"run_id": run["run_id"], "reason": "harness_stopped"}
        )
        response.raise_for_status()
        status = (await client.get("/v1/status", params={"run_id": run["run_id"]})).json()
        assert len(fixture.calls) == 2 and "/sdk" in fixture.fetches
        assert status["state"] == "sealed" and not status["tainted"]
        sealed_report = (await client.get("/v1/report", params={"run_id": run["run_id"]})).json()
        assert (
            sealed_report["integrity"]["record"]["valid"]
            and sealed_report["integrity"]["ledger"]["valid"]
        )
        tools = [tool["function"]["name"] for tool in fixture.calls[0]["tools"]]
        assert not any(
            "terminal" in name.lower() or "browser" in name.lower()
            for name in tools
            if "world" not in name.lower()
        )
        return {
            "native_conversation_completed": True,
            "model_calls": len(fixture.calls),
            "world_mcp_fetch": True,
            "frontend_token_stripped_upstream": True,
            "sealed": True,
            "record_valid": True,
            "ledger_valid": True,
            "transcript_credentials_absent": True,
            "spent": status["spent"],
            "tools": tools,
        }


async def run(output):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (output / "work").mkdir()
    (output / "home").mkdir(mode=0o700)
    os.environ["HOME"] = str(output / "home")
    os.environ["XDG_DATA_HOME"] = str(output / "home" / "data")
    os.environ["XDG_CACHE_HOME"] = str(output / "home" / "cache")
    os.environ["WORLD_OPENAI_API_KEY"] = "fixture-provider-key"
    os.environ["WORLD_OPENAI_UPSTREAM"] = "https://fixture.provider"
    os.environ.pop("WORLD_CONTROL_TOKEN", None)
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    external_attempts = []

    async def deny_external(reader, writer):
        first = await reader.readline()
        external_attempts.append(first.decode(errors="replace").split(" ")[:2])
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    fallback = await asyncio.start_server(deny_external, "127.0.0.1", 0)
    proxy = "http://127.0.0.1:" + str(fallback.sockets[0].getsockname()[1])
    os.environ.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, NO_PROXY="127.0.0.1,localhost")
    fixture = Fixture(output)
    report = {
        "fixture_only": True,
        "paid_external_calls": 0,
        "scientific_gate_evidence": False,
        "sdk_versions": {
            name: version(name)
            for name in ("openenv-core", "inspect-ai", "harbor", "openhands-sdk")
        },
    }
    try:
        report["openenv"] = await check_openenv(fixture)
        report["inspect"] = await check_inspect(fixture, report["openenv"]["sealed_source_run"])
        report["harbor"] = await check_harbor(fixture, report["openenv"]["sealed_source_run"])
        report["openhands"] = await check_openhands(fixture)
        report["fallback_external_attempts"] = external_attempts
        assert not external_attempts
        report["status"] = "passed"
    finally:
        fixture.stop()
        fallback.close()
        await fallback.wait_closed()
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    print(json.dumps(asyncio.run(run(options.output)), indent=2))
