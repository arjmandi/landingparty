"""B0/B3/B3b/B3c baselines using the same world API and dollar cap."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx


async def run_baseline(
    baseline: str,
    config: dict,
    *,
    world_client: httpx.AsyncClient,
    lane_client: httpx.AsyncClient | None = None,
    model: str | None = None,
    seed_urls: list[str] = (),
    queries: list[str] = (),
    recipients: list[str] = (),
    max_steps: int = 100,
    revision_turns: int = 20,
    max_tokens: int = 4096,
) -> dict:
    if baseline not in {"B0", "B3", "B3b", "B3c", "self-interaction"}:
        raise ValueError("unsupported baseline")
    if not 1 <= max_steps <= 10000 or not 0 <= revision_turns <= 100:
        raise ValueError("invalid registered step/revision limit")
    if baseline in {"B0", "self-interaction"} and (lane_client is None or not model):
        raise ValueError("a metered lane client and pinned model are required")
    if baseline == "B3c" and (not config.get("life_id") or not recipients):
        raise ValueError(
            "scripted outreach requires its own registered life and explicit recipients"
        )
    settings = dict(
        config, harness_id=f"{baseline}@0.1/steps={max_steps}/revisions={revision_turns}"
    )
    if baseline in {"B0", "self-interaction"}:
        settings["tools_disabled"] = True
    started = await world_client.post("/v1/run", json=settings)
    started.raise_for_status()
    info = started.json()
    run_id = info["run_id"]
    run_headers = {"X-World-Run-Id": run_id}
    if info.get("run_token"):
        run_headers["X-World-Run-Token"] = info["run_token"]
    observations, output = [], ""
    reason = "harness_stopped"

    async def act(tool, params):
        response = await world_client.post(
            "/v1/act",
            headers=run_headers,
            json={"run_id": run_id, "tool": tool, "params": params},
        )
        response.raise_for_status()
        reply = response.json()
        observations.append({"tool": tool, "reply": reply})
        return reply

    try:
        if baseline in {"B0", "self-interaction"}:
            goal = settings.get("goal")
            goal = goal.get("text", "") if isinstance(goal, dict) else (goal or "")
            messages = [{"role": "system", "content": info["preamble"]}]
            if goal:
                messages.append({"role": "user", "content": goal})
            for turn in range(revision_turns + 1):
                response = await lane_client.post(
                    "chat/completions",
                    headers=run_headers,
                    json={
                        "model": model,
                        "messages": messages,
                        "tools": [],
                        "max_tokens": max_tokens,
                        "stream": False,
                    },
                )
                response.raise_for_status()
                message = response.json()["choices"][0]["message"]
                if message.get("tool_calls"):
                    raise ValueError("a tools-off baseline returned tool calls")
                output = message.get("content", "")
                messages.append(message)
                if turn < revision_turns:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Continue the conversation with yourself."
                                if baseline == "self-interaction"
                                else "Revise your artifact if useful using only the information already available."
                            ),
                        }
                    )
            reason = "agent_finished"
        elif baseline == "B3":
            rng = random.Random(settings.get("seed", 0))
            handles = []
            for _ in range(max_steps):
                choices = (
                    ["status", "inbox"]
                    + (["fetch"] if seed_urls else [])
                    + (["search"] if queries else [])
                    + (["extract"] if handles else [])
                )
                tool = rng.choice(choices)
                params = (
                    {"url": rng.choice(seed_urls)}
                    if tool == "fetch"
                    else (
                        {"query": rng.choice(queries)}
                        if tool == "search"
                        else {
                            "handle": rng.choice(handles),
                            "view": rng.choice(["text", "links", "metadata"]),
                        }
                        if tool == "extract"
                        else {}
                    )
                )
                reply = await act(tool, params)
                if reply.get("handle"):
                    handles.append(reply["handle"])
                if reply.get("type") == "budget_exhausted":
                    break
        elif baseline == "B3b":
            queue, visited = deque(seed_urls), set()
            while queue and len(observations) < max_steps:
                url = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)
                fetched = await act("fetch", {"url": url})
                if (
                    fetched.get("kind") != "result"
                    or not fetched.get("handle")
                    or len(observations) >= max_steps
                ):
                    continue
                extracted = await act("extract", {"handle": fetched["handle"], "view": "links"})
                for link in extracted.get("links", extracted.get("data", [])):
                    href = link.get("url", link.get("href")) if isinstance(link, dict) else link
                    if isinstance(href, str):
                        target = urljoin(fetched.get("final_url", url), href)
                        if urlsplit(target).scheme in {"http", "https"} and target not in visited:
                            queue.append(target)
        else:
            for recipient in recipients[:max_steps]:
                await act(
                    "send",
                    {
                        "to": recipient,
                        "subject": "Research agent outreach",
                        "body": "This is a scripted baseline outreach message from the registered research identity.",
                    },
                )
        return {
            "run_id": run_id,
            "baseline": baseline,
            "output": output,
            "observations": observations,
            "world_actions": len(observations),
        }
    finally:
        sealed = await world_client.post(
            "/v1/beam-up", headers=run_headers, json={"run_id": run_id, "reason": reason}
        )
        sealed.raise_for_status()


async def _main(args):
    config = json.loads(args.config.read_text())
    async with (
        httpx.AsyncClient(
            base_url=args.world_url,
            timeout=65,
            trust_env=False,
            headers={"X-World-Control-Token": os.environ["WORLD_CONTROL_TOKEN"]}
            if os.environ.get("WORLD_CONTROL_TOKEN")
            else {},
        ) as world,
        httpx.AsyncClient(
            base_url=args.lane_url.rstrip("/") + "/", timeout=180, trust_env=False
        ) as lane,
    ):
        result = await run_baseline(
            args.baseline,
            config,
            world_client=world,
            lane_client=lane,
            model=args.model,
            seed_urls=args.url,
            queries=args.query,
            max_steps=args.max_steps,
            revision_turns=args.revision_turns,
        )
        print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", choices=["B0", "B3", "B3b", "self-interaction"])
    parser.add_argument("config", type=Path)
    parser.add_argument("--world-url", default="http://127.0.0.1:7777")
    parser.add_argument("--lane-url", default="http://127.0.0.1:8081/openai/v1")
    parser.add_argument("--model")
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--revision-turns", type=int, default=20)
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
