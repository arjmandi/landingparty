"""Registered studies execute real adapters through the actual ASGI application."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from worldd.app import create_app
from worldd.gateway import Gateway
from worldd.models import RunConfig
from worldd.storage import content_hash
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
    registered = app.state.world.services["registry"].execution_log(value["study_id"])
    assert [row["cell"] for row in registered] == ["registered", "registered"]
    assert [row["seed"] for row in registered] == [3, 7]
    assert [row["run_id"] for row in registered] == value["run_ids"]
    assert not value["scientific"]
    assert app.state.world.services["studies"]._clients == []
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


async def test_five_episode_dag_imports_actual_prior_workspace_and_replays_first_archive(study_app):
    app, _, calls = study_app
    world = app.state.world
    seen = []

    async def harness(created, cell):
        run_id, episode = created["run_id"], cell["episode"]
        work = world.store.run_dir(run_id) / "work"
        carried = cell["condition_id"] == "carried"
        if carried and episode > 1:
            assert (work / "prior.txt").read_text() == str(episode - 1)
            assert world.services["beliefs"].fold(run_id)
        else:
            assert not (work / "prior.txt").exists()
        (work / "prior.txt").write_text(str(episode))
        fetched = await world.act(run_id, "fetch", {"url": "https://example.org/evidence"})
        assert fetched["kind"] == "result", fetched
        if episode == 1:
            belief = await world.act(
                run_id,
                "believe",
                {
                    "record": {
                        "kind": "fact",
                        "claim": "Actual fixture observation",
                        "rank": 0,
                        "evidence": [{"ref": Path(fetched["handle"]).stem, "class": "observed"}],
                        "triple": {"subject": "fixture", "predicate": "has", "object": "evidence"},
                    }
                },
            )
            assert belief["kind"] == "result", belief
        seen.append((cell["cell_id"], run_id, world.run_config(run_id)))
        return {"run_id": run_id, "actual_fetch": fetched["handle"]}

    service = StudyService(world, harnesses={"B1": harness})
    cells = []
    for episode in range(1, 6):
        for condition in ("carried", "stripped"):
            cell = {
                "cell_id": f"{condition}.{episode}",
                "condition_id": condition,
                "episode": episode,
                "baseline": "B1",
                "seeds": [7],
                "model": "fixture-model",
                "max_steps": 1,
                "max_tokens": 20,
                "config": {"stage": 1, "packs": ["belief"], "budget": {"usd": 1, "wall_s": 30}},
            }
            if episode > 1:
                cell["depends_on"] = {"archive": {"source_cell": f"{condition}.1", "episode": 1}}
                if condition == "carried":
                    reference = {"source_cell": f"carried.{episode - 1}", "episode": episode - 1}
                    cell["depends_on"].update(
                        workspace=reference, beliefs={**reference, "mount": "continuity"}
                    )
            cells.append(cell)
    try:
        planned = service.plan({"prereg": prereg(), "cells": cells})
        assert planned["prerequisites"] == []
        result = await service.execute(
            planned["study_id"], execute=True, budget_usd=10, options={"wait": True}
        )
        assert result["state"] == "completed", result
        assert len(seen) == 10 and calls == []
        log = world.services["registry"].execution_log(planned["study_id"])
        assert [row["cell"] for row in log] == [cell["cell_id"] for cell in cells]
        assert all(
            row["dependency_protocol_hash"] == planned["dependency_protocol"]["hash"] for row in log
        )
        source = {name: run_id for name, run_id, _ in seen}
        for name, run_id, config in seen:
            assert world.store.verify_record(run_id)["valid"]
            assert config["execution_binding"]["study_id"] == planned["study_id"]
            if not name.endswith(".1"):
                assert config["archive_in"] == source[name.split(".")[0] + ".1"]
                assert config["mode"] == "replay"
        assert result["scientific"] is False
    finally:
        await service.aclose()


async def test_dependency_failure_never_creates_a_recipient_and_keeps_started_casualty(study_app):
    app, _, _ = study_app

    async def failing(created, cell):
        raise ValueError("actual donor failed")

    service = StudyService(app.state.world, harnesses={"B1": failing})
    base = {
        "baseline": "B1",
        "seeds": [3],
        "model": "fixture-model",
        "config": {"budget": {"usd": 1, "wall_s": 30}},
    }
    try:
        plan = service.plan(
            {
                "prereg": prereg(),
                "cells": [
                    {**base, "cell_id": "donor"},
                    {
                        **base,
                        "cell_id": "recipient",
                        "depends_on": {"workspace": {"source_cell": "donor"}},
                    },
                ],
            }
        )
        result = await service.execute(
            plan["study_id"], execute=True, budget_usd=2, options={"wait": True}
        )
        assert result["state"] == "failed" and len(result["run_ids"]) == 1
        assert result["result"]["attrition"][0]["status"] == "failed"
        assert "dependency resolution failed" in result["result"]["stopped"]
    finally:
        await service.aclose()


async def test_seed_dependency_topology_waits_for_actual_later_donor_seed(study_app):
    app, _, _ = study_app
    world = app.state.world
    order = []

    async def harness(created, cell):
        seed = cell["config"]["seed"]
        path = world.store.run_dir(created["run_id"]) / "work" / "source-seed.txt"
        if cell["cell_id"] == "donor":
            path.write_text(str(seed))
        else:
            assert path.read_text() == "7"
        order.append((cell["cell_id"], seed))
        return {"run_id": created["run_id"]}

    service = StudyService(world, harnesses={"B1": harness})
    base = {"baseline": "B1", "model": "fixture-model", "config": {}}
    try:
        planned = service.plan(
            {
                "prereg": prereg(),
                "cells": [
                    {**base, "cell_id": "donor", "seeds": [3, 7]},
                    {
                        **base,
                        "cell_id": "recipient",
                        "seeds": [3],
                        "depends_on": {"workspace": {"source_cell": "donor", "seed": 7}},
                    },
                ],
            }
        )
        result = await service.execute(
            planned["study_id"], execute=True, budget_usd=60, options={"wait": True}
        )
        assert result["state"] == "completed", result
        assert order == [("donor", 3), ("donor", 7), ("recipient", 3)]
    finally:
        await service.aclose()


async def test_dependency_protocol_rejects_future_cells_and_unregistered_research_graph(study_app):
    app, _, _ = study_app
    service = app.state.world.services["studies"]
    base = {"baseline": "B1", "seeds": [3], "model": "fixture-model", "config": {}}
    with pytest.raises(ValueError, match="earlier declared"):
        service.plan(
            {
                "prereg": prereg(),
                "cells": [
                    {
                        **base,
                        "cell_id": "recipient",
                        "depends_on": {"workspace": {"source_cell": "donor"}},
                    },
                    {**base, "cell_id": "donor"},
                ],
            }
        )
    base["config"] = {"research_mode": "research"}
    with pytest.raises(ValueError, match="pre-registration protocol hash"):
        service.plan(
            {
                "prereg": prereg(),
                "cells": [
                    {**base, "cell_id": "donor"},
                    {
                        **base,
                        "cell_id": "recipient",
                        "depends_on": {"workspace": {"source_cell": "donor"}},
                    },
                ],
            }
        )


async def test_committed_start_without_coordinator_ack_is_recovered_as_actual_attrition(
    study_app, monkeypatch
):
    app, _, _ = study_app
    world = app.state.world
    service = world.services["studies"]
    plan = service.plan(await registered_spec(app, seeds=[3]))
    original = service._save

    class SimulatedCrash(BaseException):
        pass

    def crash_after_creation(value):
        if value["study_id"] == plan["study_id"] and value["run_ids"]:
            raise SimulatedCrash()
        return original(value)

    monkeypatch.setattr(service, "_save", crash_after_creation)
    with pytest.raises(SimulatedCrash):
        await service.execute(plan["study_id"], execute=True, budget_usd=1, options={"wait": True})
    monkeypatch.setattr(service, "_save", original)
    service.tasks.clear()
    assert service.get(plan["study_id"])["run_ids"] == []
    starts = [
        run
        for run in world.store.list_runs()
        if run["config"].get("execution_binding", {}).get("study_id") == plan["study_id"]
    ]
    assert len(starts) == 1
    await world.recover()
    recovered = service.get(plan["study_id"])
    assert recovered["state"] == "failed" and recovered["run_ids"] == [starts[0]["run_id"]]
    assert recovered["result"]["attrition"][0]["state"] == "sealed"
    assert (
        world.services["registry"].execution_log(plan["study_id"])[0]["run_id"]
        == starts[0]["run_id"]
    )


async def test_failed_creation_return_still_seals_the_actual_committed_start(
    study_app, monkeypatch
):
    app, _, _ = study_app
    world = app.state.world
    service = world.services["studies"]
    plan = service.plan(await registered_spec(app, seeds=[3]))
    create = world.create_run

    def interrupted_return(*args, **kwargs):
        create(*args, **kwargs)
        raise ValueError("creation response lost after actual commit")

    monkeypatch.setattr(world, "create_run", interrupted_return)
    result = await service.execute(
        plan["study_id"], execute=True, budget_usd=1, options={"wait": True}
    )
    assert result["state"] == "failed" and len(result["run_ids"]) == 1
    assert world.store.get_run(result["run_ids"][0])["state"] == "sealed"
    assert result["result"]["attrition"][0]["run_id"] == result["run_ids"][0]


async def test_six_life_recipients_resolve_real_donor_then_continue_after_seven_day_stasis(
    study_app,
):
    app, _, calls = study_app
    world = app.state.world
    service = world.services["studies"]
    lives = world.services["lives"]
    clock = [datetime.now(UTC)]
    lives.clock = lambda: clock[0]

    async def donor_harness(created, cell):
        (world.store.run_dir(created["run_id"]) / "work" / "donor.txt").write_text("actual donor")
        return {"run_id": created["run_id"]}

    service.harnesses["B1"] = donor_harness
    donor_plan = service.plan(await registered_spec(app, seeds=[7]))
    donor_study = await service.execute(
        donor_plan["study_id"], execute=True, budget_usd=1, options={"wait": True}
    )
    donor = donor_study["run_ids"][0]
    seen = []

    async def recipient_harness(created, cell):
        run_id = created["run_id"]
        config = world.run_config(run_id)
        life = lives.get(config["life_id"])
        work = world.store.run_dir(run_id) / "work"
        assert (work / "donor.txt").read_text() == "actual donor"
        if len(life["sessions"]) == 1:
            assert config["iota"]["workspace"] == f"import({donor})"
            (work / "recipient.txt").write_text(life["life_id"])
            await world.act(run_id, "sleep", {"until": (clock[0] + timedelta(days=7)).isoformat()})
        else:
            assert (work / "recipient.txt").read_text() == life["life_id"]
            assert config["iota"]["workspace"] == f"import({life['sessions'][0]['run_id']})"
        seen.append(run_id)
        return {"run_id": run_id}

    service.harnesses["B1"] = recipient_harness
    references = {
        "workspace": {"study_id": donor_plan["study_id"], "source_cell": "registered", "seed": 7}
    }
    from worldd.study_dependencies import validate_references

    protocol = {
        "version": "life-dependencies@1",
        "depends_on": validate_references(references, external=True),
    }
    registered = {**prereg(), "dependency_protocol_hash": content_hash(protocol)}
    runners = []
    try:
        for _ in range(6):
            life = lives.create({"stage": 1, "budget": {"usd_life": 2, "wall_life_days": 28}})
            runner = service.plan_life(
                life["life_id"],
                {
                    "baseline": "B1",
                    "model": "fixture-model",
                    "max_steps": 1,
                    "max_tokens": 20,
                    "prereg": registered,
                    "depends_on": references,
                    "config": {
                        "packs": ["schedule"],
                        "seed": 7,
                        "budget": {"usd": 1, "wall_s": 30},
                    },
                },
            )
            runners.append(runner)
            await service.execute_life(runner["runner_id"], execute=True, budget_usd=2)
        async with asyncio.timeout(5):
            while not all(
                service.get_life_runner(r["runner_id"])["sessions"]
                and service.get_life_runner(r["runner_id"])["sessions"][-1]["status"] == "sleeping"
                for r in runners
            ):
                await asyncio.sleep(0.01)
        assert len(seen) == 6 and calls == []
        clock[0] += timedelta(days=7)
        for runner in runners:
            await lives.tick(runner["life_id"])
        async with asyncio.timeout(5):
            while not all(
                service.get_life_runner(r["runner_id"])["state"] == "completed" for r in runners
            ):
                await asyncio.sleep(0.01)
        assert len(seen) == 12
        for runner in runners:
            actual = service.get_life_runner(runner["runner_id"])
            assert len(actual["sessions"]) == 2
            assert actual["dependency_resolution"]["sources"]["workspace"]["source_run_id"] == donor
            assert all(world.store.verify_record(s["run_id"])["valid"] for s in actual["sessions"])
        assert calls == []
    finally:
        service.harnesses.pop("B1", None)
