"""Registered studies execute real adapters through the actual ASGI application."""

import asyncio

import httpx
import pytest

from worldd.app import create_app
from worldd.gateway import Gateway
from worldd.models import RunConfig
from worldd.studies import StudyService


@pytest.fixture
async def study_app(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_OPENAI_API_KEY", "fixture-only")
    provider_calls = []

    def provider(request):
        provider_calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "A completed artifact."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    def network(request):
        return httpx.Response(
            200,
            text="User-agent: *\nAllow: /\n"
            if request.url.path == "/robots.txt"
            else "<p>Evidence</p>",
        )

    async def resolve(host, port):
        return ["93.184.216.34"]

    remote = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(network))
    gateway = Gateway(
        tmp_path, {"domain_interval_s": 0, "domain_burst": 100}, client=upstream, resolver=resolve
    )
    prices = {
        "models": {
            "fixture-model": {
                "provider": "openai",
                "input": "2",
                "output": "4",
                "context_window": 100000,
                "max_output_tokens": 1000,
            }
        }
    }
    app = create_app(tmp_path, gateway=gateway, lane_client=remote, prices=prices)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://world")
    yield app, client, provider_calls
    await client.aclose()
    await app.state.world.aclose()
    await remote.aclose()
    await upstream.aclose()


def prereg():
    return {
        "experiment_id": "adapter-check",
        "primary_metric": {"name": "completion", "threshold": 1},
        "seed_count": 2,
        "seed_count_rationale": "Engineering fixture, no scientific inference",
        "stopping_rule": "All cells retain every started row",
        "likely_null": "No task difference",
        "exploratory_metrics": [],
    }


async def registered_spec(app, *, baseline="B1", seeds=None, max_steps=1):
    world = app.state.world
    config = {
        "goal": "Write a concise artifact",
        "packs": ["deliver"],
        "budget": {"usd": 1, "wall_s": 30},
        "model_snapshot_id": "fixture-model",
        "harness_id": f"reference-loop@0.1/native-parallel/max-steps={max_steps}/max-tokens=20",
    }
    if baseline != "B1":
        config["harness_id"] = f"{baseline}@0.1/steps={max_steps}/revisions=0"
    created = world.create_run(RunConfig.model_validate(config))
    await world.end_run(created["run_id"])
    return {
        "prereg": prereg(),
        "cells": [
            {
                "cell_id": "registered",
                "baseline": baseline,
                "seeds": seeds or [3, 7],
                "config": config,
                "model": "fixture-model",
                "max_steps": max_steps,
                "max_tokens": 20,
                "revision_turns": 0,
                "reference_run_id": created["run_id"],
                "seed_urls": ["https://example.org/evidence"],
            }
        ],
    }


async def test_default_shakedown_is_durable_plan_without_runs_or_paid_calls(study_app):
    app, client, calls = study_app
    response = await client.post("/v1/studies/plan", json={"kind": "shakedown"})
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["state"] == "planned" and value["run_count"] == 12
    assert value["total_cap_usd"] == 300
    assert value["prerequisites"] and not value["scientific"]
    assert app.state.world.store.list_runs() == [] and calls == []
    failure = await client.post(
        f"/v1/studies/{value['study_id']}/execute", json={"execute": True, "budget_usd": 300}
    )
    assert failure.status_code == 400 and "prerequisites" in failure.text


async def test_shakedown_cannot_be_relabelled_as_a_smaller_pilot(study_app):
    app, client, calls = study_app
    spec = await registered_spec(app)
    response = await client.post("/v1/studies/plan", json={"kind": "shakedown", "spec": spec})
    assert response.status_code == 400 and "twelve" in response.text
    assert calls == []


async def test_registered_reference_adapter_calls_real_lane_once_per_seed(study_app):
    app, client, calls = study_app
    spec = await registered_spec(app)
    response = await client.post("/v1/studies/plan", json={"spec": spec})
    assert response.status_code == 201, response.text
    planned = response.json()
    assert planned["prerequisites"] == []
    endpoint = f"/v1/studies/{planned['study_id']}/execute"
    denied = await client.post(endpoint, json={"execute": False, "budget_usd": 2})
    assert denied.status_code == 400
    denied = await client.post(endpoint, json={"execute": True, "budget_usd": 1})
    assert denied.status_code == 400 and calls == []
    result = await client.post(
        endpoint, json={"execute": True, "budget_usd": 2, "options": {"wait": True}}
    )
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["state"] == "completed", value
    assert len(calls) == 2 and len(value["run_ids"]) == 2
    assert [row["seed"] for row in value["result"]["attrition"]] == [3, 7]
    assert not value["scientific"]
    for run_id in value["run_ids"]:
        store = app.state.world.store
        assert store.get_run(run_id)["state"] == "sealed"
        assert store.spend(run_id)["model"] > 0
        assert store.verify_record(run_id)["valid"]
        assert store.reconcile(run_id)["valid"]
    replay = await client.post(endpoint, json={"execute": True, "budget_usd": 2})
    assert replay.status_code == 400 and len(calls) == 2


async def test_real_scripted_baseline_has_one_episode_and_world_actions(study_app):
    app, client, calls = study_app
    spec = await registered_spec(app, baseline="B3b", seeds=[9])
    planned = (await client.post("/v1/studies/plan", json={"spec": spec})).json()
    result = await client.post(
        f"/v1/studies/{planned['study_id']}/execute",
        json={"execute": True, "budget_usd": 1, "options": {"wait": True}},
    )
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["state"] == "completed", value
    assert len(value["run_ids"]) == 1 and calls == []
    assert value["result"]["attrition"][0]["result"]["world_actions"] == 1
    assert app.state.world.store.verify_record(value["run_ids"][0])["valid"]


async def test_failed_started_rows_and_interleaved_seeds_are_retained(study_app):
    app, _, calls = study_app
    spec = await registered_spec(app)
    seen = []

    async def harness(created, cell):
        seen.append(cell["config"]["seed"])
        raise ValueError("Expected fixture failure")

    service = StudyService(app.state.world, harnesses={"B1": harness})
    try:
        plan = service.plan(spec)
        value = await service.execute(
            plan["study_id"], execute=True, budget_usd=2, options={"wait": True}
        )
        assert seen == [3, 7]
        assert value["state"] == "failed" and len(value["run_ids"]) == 2
        assert all(row["status"] == "failed" for row in value["result"]["attrition"])
        assert calls == []
        assert all(
            app.state.world.store.get_run(run_id)["state"] == "sealed"
            for run_id in value["run_ids"]
        )
    finally:
        await service.aclose()


async def test_cancelled_worker_seals_started_row_and_preserves_attrition(study_app):
    app, _, _ = study_app
    spec = await registered_spec(app)
    started = asyncio.Event()

    async def harness(created, cell):
        started.set()
        await asyncio.Event().wait()

    service = StudyService(app.state.world, harnesses={"B1": harness})
    try:
        plan = service.plan(spec)
        await service.execute(plan["study_id"], execute=True, budget_usd=2)
        await asyncio.wait_for(started.wait(), 1)
        await service.aclose()
        current = service.get(plan["study_id"])
        assert current["state"] == "failed" and current["result"]["reason"] == "cancelled"
        assert len(current["run_ids"]) == 1
        assert app.state.world.store.get_run(current["run_ids"][0])["state"] == "sealed"
    finally:
        await service.aclose()
