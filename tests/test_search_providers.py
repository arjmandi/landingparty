"""Alternate retriever, exact paid-read binding, and zero-live replay behavior."""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from worldd.app import create_app
from worldd.gateway import Gateway
from worldd.models import RunConfig
from worldd.provider_boundary import submit_search
from worldd.search import provider_request


@pytest.fixture
async def search_world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_EXA_API_KEY", "fixture-exa-secret")
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com/item",
                        "title": "Found item",
                        "publishedDate": "2026-10-01T00:00:00Z",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:

        async def public_dns(host, port):
            return ["93.184.216.34"]

        gateway = Gateway(tmp_path, client=client, resolver=public_dns)
        world = create_app(tmp_path, gateway=gateway, search_client=client).state.world
        yield world, calls
        await world.aclose()


async def test_exa_paid_read_is_pinned_normalized_and_replay_needs_no_key(
    search_world, monkeypatch
):
    world, calls = search_world
    started = world.create_run(RunConfig(search_provider="exa"))
    run_id = started["run_id"]
    result = await world.act(
        run_id, "search", {"query": "test event", "k": 3, "site": "example.com", "freshness": "pw"}
    )
    assert result["provider_id"] == "exa@search-v1/neural"
    assert result["items"][0]["url"] == "https://example.com/item"
    assert result["items"][0]["rank"] == 1
    assert result["items"][0]["published"] == "2026-10-01T00:00:00Z"
    assert len(calls) == 1
    wire = calls[0]
    assert wire.method == "POST" and str(wire.url) == "https://93.184.216.34/search"
    assert wire.headers["host"] == "api.exa.ai"
    assert wire.headers["x-api-key"] == "fixture-exa-secret"
    body = json.loads(wire.content)
    action = next(row for row in world.store.records(run_id) if row["kind"] == "action")
    assert body == {
        "query": "test event",
        "numResults": 3,
        "type": "neural",
        "includeDomains": ["example.com"],
        "startPublishedDate": (
            datetime.fromisoformat(action["ts"]) - timedelta(days=7)
        ).isoformat(),
    }
    assert world.store.spend(run_id) == {"search": 0.005}
    assert world.store.reserved(run_id) == 0
    assert (
        world.store.get_run(run_id)["config"]["tuple"]["search_provider_id"]
        == "exa@search-v1/neural"
    )
    assert "fixture-exa-secret" not in json.dumps(world.store.records(run_id))
    await world.end_run(run_id)
    monkeypatch.delenv("WORLD_EXA_API_KEY")
    replay = world.create_run(RunConfig(mode="replay", archive_in=run_id))["run_id"]
    again = await world.act(
        replay, "search", {"query": "test event", "k": 3, "site": "example.com", "freshness": "pw"}
    )
    assert again["items"] == result["items"]
    assert again["provider_id"] == f"local@{run_id}"
    assert world.store.spend(replay) == {} and len(calls) == 1


@pytest.mark.parametrize("mutation", ["contents", "query", "count", "endpoint", "credentials"])
async def test_exa_boundary_rejects_unrecorded_addons_before_egress(search_world, mutation):
    world, calls = search_world
    run_id = world.create_run(RunConfig(search_provider="exa"))["run_id"]
    action = world.store.append_record(
        run_id, "action", tool="search", action_id="a_exact", params={"query": "event", "k": 2}
    )
    held = world.store.reserve(run_id, "0.005")
    request = provider_request(
        "exa", "fixture-exa-secret", action["params"], submitted_at=action["ts"]
    )
    body = json.loads(request.content)
    url = str(request.url)
    if mutation == "contents":
        body["contents"] = {"text": True}
    elif mutation == "query":
        body["query"] = "changed event"
    elif mutation == "count":
        body["numResults"] = 20
    elif mutation == "endpoint":
        url = "https://api.exa.ai/contents"
    else:
        url = "https://user:password@api.exa.ai/search"
    modified = httpx.Request("POST", url, headers=request.headers, json=body)
    result = await submit_search(world, run_id, "a_exact", modified, reservation_id=held)
    assert result["rule_id"] in {"search_destination", "search_action_hash"}
    assert not calls and world.store.spend(run_id) == {}
    world.store.release(held)


async def test_unconfigured_exa_does_not_reserve_or_fall_back(search_world, monkeypatch):
    world, calls = search_world
    monkeypatch.delenv("WORLD_EXA_API_KEY")
    monkeypatch.setenv("WORLD_BRAVE_API_KEY", "unrelated-provider-key")
    world.instance_settings.delete_api_key("exa")
    run_id = world.create_run(RunConfig(search_provider="exa"))["run_id"]
    result = await world.act(run_id, "search", {"query": "event"})
    assert result["rule_id"] == "search_credentials"
    assert not calls and world.store.reserved(run_id) == 0
