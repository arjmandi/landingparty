#!/usr/bin/env python3
"""B1: one model request per step, with the world's native parallel calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

from worldd.mcp import manifest_tools


async def run_reference(
    config: dict[str, Any],
    model: str,
    *,
    world_url: str = "http://127.0.0.1:7777",
    agent_url: str = "http://127.0.0.1:7778",
    lane_url: str = "http://127.0.0.1:8081/openai/v1",
    max_steps: int = 200,
    max_tokens: int = 4096,
    world_client: httpx.AsyncClient | None = None,
    lane_client: httpx.AsyncClient | None = None,
    agent_client: httpx.AsyncClient | None = None,
    run_info: dict[str, Any] | None = None,
    initial_messages: list[dict[str, Any]] | None = None,
    context_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if max_steps < 1 or max_tokens < 1:
        raise ValueError("max_steps and max_tokens must be positive")
    if initial_messages and not context_refs:
        raise ValueError("Resumed messages require source capture references")
    if initial_messages and not run_info:
        raise ValueError("Transcript continuity requires a pre-created life session")
    if initial_messages and any(
        message.get("role") not in {"system", "user", "assistant", "tool"}
        for message in initial_messages
    ):
        raise ValueError("Captured context must contain provider chat messages")
    injected_world = world_client is not None
    own_world, own_lane = world_client is None, lane_client is None
    world_client = world_client or httpx.AsyncClient(
        base_url=world_url,
        timeout=65,
        trust_env=False,
        headers={"X-World-Control-Token": os.environ["WORLD_CONTROL_TOKEN"]}
        if os.environ.get("WORLD_CONTROL_TOKEN")
        else {},
    )
    own_agent = agent_client is None and not injected_world
    agent_client = agent_client or (
        world_client
        if injected_world
        else httpx.AsyncClient(base_url=agent_url, timeout=65, trust_env=False)
    )
    lane_client = lane_client or httpx.AsyncClient(
        base_url=lane_url.rstrip("/") + "/", timeout=180, trust_env=False
    )
    run_id = None
    end_reason = "harness_stopped"
    should_seal = True
    messages: list[dict[str, Any]] = []
    steps = 0
    try:
        config = dict(
            config,
            harness_id=(
                f"reference-loop@0.1/native-parallel/max-steps={max_steps}/max-tokens={max_tokens}"
            ),
        )
        if run_info is None:
            started = await world_client.post("/v1/run", json=config)
            started.raise_for_status()
            info = started.json()
        else:
            info = dict(run_info)
            if (
                not isinstance(info.get("run_id"), str)
                or not info["run_id"].startswith("r_")
                or not info.get("preamble")
                or not info.get("run_token")
            ):
                raise ValueError(
                    "A pre-created session requires its real id, preamble, and capability"
                )
        run_id = info["run_id"]
        run_token = info.get("run_token")
        agent_headers = {"Authorization": "Bearer " + run_token} if run_token else {}
        lane_headers = {"X-World-Run-Id": run_id}
        if run_token:
            lane_headers["X-World-Run-Token"] = run_token
        manifest = info.get("manifest")
        if manifest is None:
            response = await agent_client.get(
                "/v1/manifest", params={"run_id": run_id}, headers=agent_headers
            )
            response.raise_for_status()
            manifest = response.json()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item["description"],
                    "parameters": item["inputSchema"],
                },
            }
            for item in manifest_tools(manifest)
        ]
        messages = [{"role": "system", "content": info["preamble"]}]
        if initial_messages:
            # The new session's world-authored briefing replaces the old system
            # briefing; user/assistant/tool content is copied without synthesis.
            messages.extend(
                json.loads(
                    json.dumps(
                        [message for message in initial_messages if message["role"] != "system"]
                    )
                )
            )
        goal = config.get("goal")
        if goal and not initial_messages:
            goal_text = goal.get("text", "") if isinstance(goal, dict) else str(goal)
            messages.append({"role": "user", "content": goal_text})

        async def act(call: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            function = call["function"]
            try:
                arguments = json.loads(function.get("arguments", "{}"))
                if not isinstance(arguments, dict):
                    raise TypeError("tool arguments must be an object")
            except (ValueError, TypeError):
                reply: Any = {
                    "kind": "blocked",
                    "type": "policy_deny",
                    "rule_id": "invalid_json",
                    "detail": "Tool arguments must be valid JSON.",
                }
            else:
                response = await agent_client.post(
                    "/v1/act",
                    json={"run_id": run_id, "tool": function["name"], "params": arguments},
                    headers=agent_headers,
                )
                response.raise_for_status()
                reply = response.json()
            return (
                {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(reply)},
                function["name"] == "sleep" and reply.get("kind") == "silence",
            )

        def result(output=""):
            resumed = [message for message in initial_messages or [] if message["role"] != "system"]
            return {
                "run_id": run_id,
                "steps": steps,
                "reason": end_reason,
                "output": output,
                "messages": messages,
                "context_refs": list(context_refs or []),
                "compactions": 0,
                "resumed_context_messages": len(resumed),
                "resumed_context_bytes": len(
                    json.dumps(resumed, ensure_ascii=False, separators=(",", ":")).encode()
                )
                if resumed
                else 0,
                "continuity_semantics": "current briefing plus captured non-system messages"
                if initial_messages
                else "fresh context",
            }

        for step in range(max_steps):
            if run_info is not None:
                state = await agent_client.get(
                    "/v1/status", params={"run_id": run_id}, headers=agent_headers
                )
                state.raise_for_status()
                status = state.json()
                if status["state"] in {"sealed", "ended", "killed", "stopping"}:
                    end_reason = (status.get("end") or {}).get("reason", status["state"])
                    should_seal = False
                    return result()
            response = await lane_client.post(
                "chat/completions",
                headers=lane_headers,
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "max_tokens": max_tokens,
                },
            )
            response.raise_for_status()
            assistant = response.json()["choices"][0]["message"]
            steps = step + 1
            messages.append(assistant)
            calls = assistant.get("tool_calls") or []
            if not calls:
                end_reason = "agent_finished"
                return result(assistant.get("content") or "")
            # Ticket replies are returned unchanged; the model chooses whether to read its inbox.
            replies = await asyncio.gather(*(act(call) for call in calls))
            messages.extend(message for message, _ in replies)
            if any(sleep for _, sleep in replies):
                end_reason, should_seal = "sleep", False
                return result()
        return result()
    finally:
        try:
            if run_id is not None and should_seal:
                sealed = await world_client.post(
                    "/v1/beam-up", json={"run_id": run_id, "reason": end_reason}
                )
                sealed.raise_for_status()
        finally:
            if own_world:
                await world_client.aclose()
            if own_lane:
                await lane_client.aclose()
            if own_agent:
                await agent_client.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--world-url", default=os.environ.get("WORLD_BASE_URL", "http://127.0.0.1:7777")
    )
    parser.add_argument(
        "--lane-url", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8081/openai/v1")
    )
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument(
        "--agent-url", default=os.environ.get("WORLD_AGENT_URL", "http://127.0.0.1:7778")
    )
    parser.add_argument("--max-tokens", type=int, default=4096)
    args = parser.parse_args(argv)
    result = asyncio.run(
        run_reference(
            json.loads(args.config.read_text()),
            args.model,
            world_url=args.world_url,
            agent_url=args.agent_url,
            lane_url=args.lane_url,
            max_steps=args.max_steps,
            max_tokens=args.max_tokens,
        )
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
