import json
from pathlib import Path

import httpx

from adapters.baselines.runner import run_baseline
from worldd.models import RunConfig


def fixture_client(actions, seals):
    async def endpoint(request):
        if request.url.path == "/v1/run":
            return httpx.Response(
                201, json={"run_id": "r_baseline", "preamble": "Fixture world briefing"}
            )
        body = json.loads(request.content)
        if request.url.path == "/v1/beam-up":
            seals.append(body)
            return httpx.Response(200, json={"state": "sealed"})
        tool, params = body["tool"], body["params"]
        actions.append((tool, params))
        if tool == "fetch":
            return httpx.Response(
                200,
                json={
                    "kind": "result",
                    "handle": "/world/fetched/fixture.html",
                    "final_url": params["url"],
                },
            )
        if tool == "extract":
            return httpx.Response(
                200, json={"kind": "result", "data": [{"url": "https://example.org/linked"}]}
            )
        return httpx.Response(200, json={"kind": "state"})

    return httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="http://world")


async def test_b0_has_no_tools_twenty_revisions_and_always_seals():
    actions, seals, payloads = [], [], []

    async def provider(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        assert payload["tools"] == []
        assert request.headers["x-world-run-id"] == "r_baseline"
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "draft"}}]}
        )

    async with (
        fixture_client(actions, seals) as world,
        httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="http://lane/") as lane,
    ):
        result = await run_baseline(
            "B0", {"goal": "write"}, world_client=world, lane_client=lane, model="pinned"
        )
    assert len(payloads) == 21
    assert result["world_actions"] == 0 and not actions
    assert seals[0]["reason"] == "agent_finished"


async def test_random_shadow_is_reproducible_and_only_uses_registered_surface():
    outputs = []
    for _ in range(2):
        actions, seals = [], []
        async with fixture_client(actions, seals) as world:
            await run_baseline(
                "B3",
                {"seed": 12},
                world_client=world,
                seed_urls=["https://example.org/a"],
                queries=["public query"],
                max_steps=20,
            )
        outputs.append(actions)
        assert len(actions) == 20 and len(seals) == 1
    assert outputs[0] == outputs[1]
    assert {tool for tool, _params in outputs[0]} <= {
        "status",
        "inbox",
        "fetch",
        "search",
        "extract",
    }


async def test_scripted_crawler_follows_extracted_links_within_step_limit():
    actions, seals = [], []
    async with fixture_client(actions, seals) as world:
        result = await run_baseline(
            "B3b", {}, world_client=world, seed_urls=["https://example.org/start"], max_steps=4
        )
    assert result["world_actions"] == 4
    assert actions[2] == ("fetch", {"url": "https://example.org/linked"})
    assert len(seals) == 1


def test_all_fifteen_goal_configs_validate_and_remain_unfrozen_assets():
    entries = json.loads(Path("goals/catalog.json").read_text())
    assert {entry["goal_id"] for entry in entries} == set(range(1, 16))
    for entry in entries:
        config = json.loads((Path(entry["directory"]) / "run.json").read_text())
        RunConfig.model_validate(config)
        assert entry["status"] == "configurable_development_asset"
