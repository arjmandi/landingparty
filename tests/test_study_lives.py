"""Whole-life cohort execution through actual ASGI and metered model lanes."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from worldd.app import create_app
from worldd.studies import StudyService


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def life_cohort(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_OPENAI_API_KEY", "fixture-only")
    clock = [datetime.now(UTC)]
    seen = {}

    def provider(request):
        messages = json.loads(request.content)["messages"]
        label = next(
            message["content"]
            for message in messages
            if message.get("role") == "user" and str(message.get("content", "")).startswith("LIFE ")
        )
        requests = seen.setdefault(label, [])
        requests.append(messages)
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "sleep_source",
                        "type": "function",
                        "function": {
                            "name": "sleep",
                            "arguments": json.dumps(
                                {"until": (clock[0] + timedelta(days=7)).isoformat()}
                            ),
                        },
                    }
                ],
            }
        else:
            message = {"role": "assistant", "content": "The actual life is complete."}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    remote = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    app = create_app(
        tmp_path,
        lane_client=remote,
        prices={
            "models": {
                "fixture-model": {
                    "provider": "openai",
                    "input": "2",
                    "output": "4",
                    "context_window": 100000,
                    "max_output_tokens": 1000,
                }
            }
        },
    )
    app.state.world.services["lives"].clock = lambda: clock[0]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://world")
    yield app, client, clock, seen
    await client.aclose()
    await app.state.world.aclose()
    await remote.aclose()


async def planned(client, *, capital=2):
    cells, lives = [], []
    for seed, continuity in enumerate(("fresh_context_workspace", "resume_transcript")):
        response = await client.post(
            "/v1/lives",
            json={
                "stage": 1,
                "continuity": continuity,
                "budget": {"usd_life": capital, "wall_life_days": 28},
            },
        )
        assert response.status_code == 201, response.text
        life = response.json()["life_id"]
        lives.append(life)
        cells.append(
            {
                "cell_id": continuity,
                "baseline": "B1",
                "seeds": [seed],
                "model": "fixture-model",
                "max_steps": 5,
                "max_tokens": 20,
                "config": {
                    "life_id": life,
                    "packs": ["schedule", "deliver"],
                    "goal": "LIFE " + continuity,
                    "budget": {"usd": 1, "wall_s": 30},
                },
            }
        )
    prereg = {
        "experiment_id": "life-cohort-fixture",
        "primary_metric": {"name": "completion", "threshold": 1},
        "seed_count": 2,
        "seed_count_rationale": "Engineering fixture only",
        "stopping_rule": "Keep every started life/session",
        "likely_null": "No continuity difference",
        "exploratory_metrics": [],
    }
    response = await client.post(
        "/v1/studies/plan", json={"spec": {"prereg": prereg, "cells": cells}}
    )
    assert response.status_code == 201, response.text
    return response.json(), lives


async def test_actual_cohort_owns_lifetime_allowance_and_waits_through_stasis(life_cohort):
    app, client, clock, seen = life_cohort
    world = app.state.world
    plan, lives = await planned(client)
    assert plan["execution_kind"] == "lives" and plan["total_cap_usd"] == 4
    endpoint = f"/v1/studies/{plan['study_id']}/execute"
    denied = await client.post(endpoint, json={"execute": True, "budget_usd": 2})
    assert denied.status_code == 400 and seen == {}
    response = await client.post(endpoint, json={"execute": True, "budget_usd": 4})
    assert response.status_code == 200, response.text
    service = world.services["studies"]
    await until(
        lambda: all(
            world.services["lives"].get(life)["state"] == "sleeping"
            and world.services["lives"].get(life)["active_run_id"] is None
            for life in lives
        )
    )
    current = service.get(plan["study_id"])
    assert current["state"] == "running" and len(current["life_runners"]) == 2
    assert len(current["run_ids"]) == 2
    assert all(len(requests) == 1 for requests in seen.values())
    assert all(world.services["lives"].get(life)["income_nano"] == 0 for life in lives)
    clock[0] += timedelta(days=7)
    for life in lives:
        await world.services["lives"].tick(life)
    await until(lambda: service.get(plan["study_id"])["state"] in {"completed", "failed"})
    current = service.get(plan["study_id"])
    assert current["state"] == "completed", current
    assert len(current["result"]["life_rows"]) == 2 and len(current["result"]["session_rows"]) == 4
    assert all(row["unit"] == "life_session" for row in current["result"]["session_rows"])
    assert all(row["ended"] for row in current["result"]["life_rows"])
    rows = world.services["registry"].execution_log(plan["study_id"])
    assert len(rows) == 4 and all(row["unit"] == "life_session" for row in rows)
    for label, requests in seen.items():
        old_sleep = any(
            message.get("role") == "assistant" and message.get("tool_calls")
            for message in requests[1]
        )
        assert old_sleep is label.endswith("resume_transcript")
    assert all(
        world.store.verify_record(run_id)["valid"] and world.store.reconcile(run_id)["valid"]
        for run_id in current["run_ids"]
    )


async def test_life_study_restart_and_explicit_pause_preserve_authority_and_one_budget(life_cohort):
    app, client, clock, seen = life_cohort
    world = app.state.world
    plan, lives = await planned(client)
    service = world.services["studies"]
    await service.execute(plan["study_id"], execute=True, budget_usd=4)
    await until(
        lambda: all(
            world.services["lives"].get(life)["state"] == "sleeping"
            and world.services["lives"].get(life)["active_run_id"] is None
            for life in lives
        )
    )
    await until(
        lambda: (
            len(service.get(plan["study_id"])["life_runners"]) == 2
            and all(
                service.get_life_runner(row["runner_id"])["sessions"][-1]["status"] == "sleeping"
                for row in service.get(plan["study_id"])["life_runners"]
            )
        )
    )
    before = {run: world.store.records(run) for run in service.get(plan["study_id"])["run_ids"]}
    await service.aclose()
    restored = StudyService(world)
    restored.configure(app)
    world.services["studies"] = restored
    await restored.recover()
    await asyncio.sleep(0.05)
    assert restored.get(plan["study_id"])["state"] == "running"
    assert sum(len(requests) for requests in seen.values()) == 2
    response = await client.post(
        f"/v1/studies/{plan['study_id']}/pause", json={"reason": "actual operator pause"}
    )
    assert response.status_code == 200, response.text
    paused = response.json()
    assert paused["state"] == "paused" and len(paused["result"]["life_rows"]) == 2
    clock[0] += timedelta(days=7)
    for life in lives:
        await world.services["lives"].tick(life)
    await asyncio.sleep(0.1)
    assert sum(len(requests) for requests in seen.values()) == 2
    response = await client.post(
        f"/v1/studies/{plan['study_id']}/execute", json={"execute": True, "budget_usd": 4}
    )
    assert response.status_code == 200, response.text
    await until(lambda: restored.get(plan["study_id"])["state"] in {"completed", "failed"})
    final = restored.get(plan["study_id"])
    assert final["state"] == "completed", final
    assert len(final["life_runners"]) == 2 and len(final["run_ids"]) == 4
    assert all(world.store.records(run) == record for run, record in before.items())
    assert all(world.services["lives"].get(life)["income_nano"] == 0 for life in lives)
    assert all(world.services["lives"].get(life)["budget_nano"] == 2_000_000_000 for life in lives)


async def test_lifetime_budget_exhaustion_retains_real_started_rows_and_no_new_credit(life_cohort):
    app, client, _clock, seen = life_cohort
    world = app.state.world
    plan, lives = await planned(client, capital=1)
    service = world.services["studies"]

    async def consume_resource(created, cell):
        run_id = created["run_id"]
        assert world.store.debit(run_id, "fixture_resource", 1)
        reply = await world.act(run_id, "status", {})
        assert reply["type"] == "budget_exhausted"
        await asyncio.sleep(0)
        return {"run_id": run_id, "actual_fixture_resource_cost_usd": 1}

    service.harnesses["B1"] = consume_resource
    try:
        result = await service.execute(
            plan["study_id"], execute=True, budget_usd=2, options={"wait": True}
        )
        assert result["state"] == "completed", result
        assert len(result["run_ids"]) == 2 and len(result["result"]["life_rows"]) == 2
        assert seen == {}
        assert all(world.services["lives"].status(life)["remaining_usd"] == 0 for life in lives)
        assert all(world.services["lives"].get(life)["income_nano"] == 0 for life in lives)
        assert all(world.store.reconcile(run)["valid"] for run in result["run_ids"])
    finally:
        service.harnesses.pop("B1", None)


async def test_active_cohort_cancellation_stays_paused_after_restart_without_new_calls(life_cohort):
    app, client, _clock, seen = life_cohort
    world = app.state.world
    service = world.services["studies"]
    plan, lives = await planned(client)
    entered = set()

    async def active_harness(created, cell):
        entered.add(created["run_id"])
        await asyncio.Event().wait()

    service.harnesses["B1"] = active_harness
    await service.execute(plan["study_id"], execute=True, budget_usd=4)
    await until(lambda: len(entered) == 2)
    paused = await service.pause_study(plan["study_id"])
    assert paused["state"] == "paused" and len(paused["result"]["session_rows"]) == 2
    assert all(row["status"] == "cancelled" for row in paused["result"]["session_rows"])
    assert all(world.store.get_run(run)["state"] == "sealed" for run in entered)
    await service.aclose()
    restored = StudyService(world)
    restored.configure(app)
    world.services["studies"] = restored
    await restored.recover()
    await asyncio.sleep(0.1)
    assert restored.get(plan["study_id"])["state"] == "paused"
    assert set(restored.get(plan["study_id"])["run_ids"]) == entered and seen == {}
    child = restored.get(plan["study_id"])["life_runners"][0]
    with pytest.raises(ValueError, match="owning study"):
        await restored.execute_life(child["runner_id"], execute=True, budget_usd=2)
    assert all(world.services["lives"].get(life)["budget_nano"] == 2_000_000_000 for life in lives)


async def test_interrupted_requested_life_measurement_is_uncertain_without_retry(
    life_cohort, monkeypatch
):
    app, client, _clock, seen = life_cohort
    world = app.state.world
    service = world.services["studies"]
    prototype, _lives = await planned(client)
    selected = {**prototype["cells"][0], "analysis_options": {"measurement_budget_usd": 0.1}}
    plan = service.plan({"prereg": prototype["prereg"], "cells": [selected]})
    assert plan["total_cap_usd"] == 2.1
    entered, calls = asyncio.Event(), []
    pipeline = world.services["pipeline"]
    original = pipeline.run

    async def measurement(source, *, options=None):
        if options and options.get("measurement_budget_usd"):
            calls.append(source)
            entered.set()
            await asyncio.Event().wait()
        return await original(source, options=options)

    async def completed_harness(created, cell):
        return {"run_id": created["run_id"]}

    monkeypatch.setattr(pipeline, "run", measurement)
    service.harnesses["B1"] = completed_harness
    await service.execute(plan["study_id"], execute=True, budget_usd=2.1)
    await asyncio.wait_for(entered.wait(), 3)
    assert service.get(plan["study_id"])["state"] == "running"
    await service.aclose()
    restored = StudyService(world)
    restored.configure(app)
    world.services["studies"] = restored
    await restored.recover()
    await until(lambda: restored.get(plan["study_id"])["state"] in {"completed", "failed"})
    result = restored.get(plan["study_id"])
    assert result["state"] == "failed" and len(calls) == 1 and seen == {}
    assert result["result"]["measurements"][0]["state"] == "uncertain"
    assert result["result"]["life_rows"][0]["ended"]
