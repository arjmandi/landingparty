"""Actual metered B1 episodes sleep, wake, and resume only an authorized life."""

import asyncio
import json

import httpx
import pytest

from worldd.app import create_app
from worldd.studies import StudyService


@pytest.fixture
async def running_app(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_OPENAI_API_KEY", "fixture-only")
    messages = []

    def provider(request):
        messages.append(json.loads(request.content)["messages"])
        if len(messages) == 1:
            response = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "sleep_call",
                        "type": "function",
                        "function": {
                            "name": "sleep",
                            "arguments": json.dumps({"wake_on": ["message_received"]}),
                        },
                    }
                ],
            }
        else:
            response = {"role": "assistant", "content": "The authorized life is complete."}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": response, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    remote = httpx.AsyncClient(transport=httpx.MockTransport(provider))
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
    prices["models"]["fixture-model-2"] = dict(prices["models"]["fixture-model"])
    app = create_app(tmp_path, lane_client=remote, prices=prices)
    yield app, messages
    await app.state.world.aclose()
    await remote.aclose()


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def plan(app, *, continuity="fresh_context_workspace"):
    world = app.state.world
    life = world.services["lives"].create(
        {"budget": {"usd_life": 2, "wall_life_days": 28}, "continuity": continuity}
    )
    service = world.services["studies"]
    runner = service.plan_life(
        life["life_id"],
        {
            "baseline": "B1",
            "model": "fixture-model",
            "max_steps": 5,
            "max_tokens": 20,
            "config": {
                "packs": ["schedule", "deliver"],
                "goal": "Wait for a message, then conclude.",
                "budget": {"usd": 1, "wall_s": 30},
            },
        },
    )
    return life["life_id"], service, runner


@pytest.mark.parametrize("continuity", ["fresh_context_workspace", "resume_transcript"])
async def test_real_reference_life_sleeps_and_wakes_with_exact_registered_continuity(
    running_app, continuity
):
    app, messages = running_app
    world = app.state.world
    life_id, service, runner = plan(app, continuity=continuity)
    assert messages == [] and world.services["lives"].get(life_id)["sessions"] == []
    with pytest.raises(ValueError, match="explicit authorization"):
        await service.execute_life(runner["runner_id"], execute=False, budget_usd=2)
    with pytest.raises(ValueError, match="actual initial capital"):
        await service.execute_life(runner["runner_id"], execute=True, budget_usd=1)
    await service.execute_life(runner["runner_id"], execute=True, budget_usd=2)
    await until(
        lambda: (
            service.get_life_runner(runner["runner_id"])["sessions"]
            and service.get_life_runner(runner["runner_id"])["sessions"][-1]["status"] == "sleeping"
        )
    )
    source = service.get_life_runner(runner["runner_id"])["sessions"][0]["run_id"]
    before = world.store.records(source)
    assert len(messages) == 1
    world.push(source, {"type": "message_received", "body": "An actual late fixture message"})
    await until(
        lambda: service.get_life_runner(runner["runner_id"])["state"] in {"completed", "failed"}
    )
    result = service.get_life_runner(runner["runner_id"])
    assert result["state"] == "completed", result
    assert len(messages) == 2 and len(result["sessions"]) == 2
    assert world.store.records(source) == before
    assert world.services["lives"].verify(life_id)["valid"]
    assert all(
        world.store.get_run(row["run_id"])["state"] == "sealed" for row in result["sessions"]
    )
    old_sleep_in_context = any(
        row.get("role") == "assistant" and row.get("tool_calls") for row in messages[1]
    )
    assert old_sleep_in_context is (continuity == "resume_transcript")
    assert world.services["lives"].status(life_id)["spent_usd"] > 0
    assert all(row["compactions"] == 0 for row in result["sessions"])
    assert (
        result["sessions"][1]["resumed_context_messages"] > 0
        if continuity == "resume_transcript"
        else result["sessions"][1]["resumed_context_messages"] == 0
    )


async def test_restart_only_resumes_previously_authorized_sleeping_runner(running_app):
    app, messages = running_app
    world = app.state.world
    life_id, original, runner = plan(app, continuity="resume_transcript")
    other_id, _, unconfigured = plan(app)
    await original.execute_life(runner["runner_id"], execute=True, budget_usd=2)
    await until(
        lambda: (
            original.get_life_runner(runner["runner_id"])["sessions"]
            and original.get_life_runner(runner["runner_id"])["sessions"][-1]["status"]
            == "sleeping"
        )
    )
    source = original.get_life_runner(runner["runner_id"])["sessions"][0]["run_id"]
    await original.aclose()
    replacement = StudyService(world)
    replacement.configure(app)
    world.services["studies"] = replacement
    await replacement.recover()
    assert replacement.get_life_runner(unconfigured["runner_id"])["state"] == "planned"
    assert world.services["lives"].get(other_id)["sessions"] == []
    world.push(source, {"type": "message_received", "body": "Wake after process replacement"})
    await until(
        lambda: replacement.get_life_runner(runner["runner_id"])["state"] in {"completed", "failed"}
    )
    assert replacement.get_life_runner(runner["runner_id"])["state"] == "completed"
    assert len(messages) == 2 and world.services["lives"].get(life_id)["ended"]


async def test_native_resume_and_supplied_messages_are_rejected_before_launch(running_app):
    app, messages = running_app
    world = app.state.world
    life = world.services["lives"].create({"continuity": "resume_transcript"})
    service = world.services["studies"]
    with pytest.raises(ValueError, match="actual supported reference-loop"):
        service.plan_life(life["life_id"], {"baseline": "B2", "model": "fixture-model"})
    with pytest.raises(ValueError, match="never supplied messages"):
        service.plan_life(
            life["life_id"],
            {
                "baseline": "B1",
                "model": "fixture-model",
                "initial_messages": [{"role": "assistant", "content": "invented"}],
            },
        )
    assert messages == []


async def test_actual_control_api_pauses_authorized_runner_and_requires_explicit_resume(
    running_app,
):
    app, messages = running_app
    world = app.state.world
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://world"
    ) as client:
        life = (await client.post("/v1/lives", json={"budget": {"usd_life": 2}})).json()
        response = await client.post(
            f"/v1/lives/{life['life_id']}/runner",
            json={
                "cell": {
                    "baseline": "B1",
                    "model": "fixture-model",
                    "max_steps": 5,
                    "max_tokens": 20,
                    "config": {"packs": ["schedule"], "budget": {"usd": 1, "wall_s": 30}},
                }
            },
        )
        assert response.status_code == 201, response.text
        runner_id = response.json()["runner_id"]
        assert messages == []
        endpoint = f"/v1/life-runners/{runner_id}"
        assert (
            await client.post(endpoint + "/execute", json={"execute": True, "budget_usd": 2})
        ).status_code == 200
        service = world.services["studies"]
        await until(
            lambda: (
                service.get_life_runner(runner_id)["sessions"]
                and service.get_life_runner(runner_id)["sessions"][-1]["status"] == "sleeping"
            )
        )
        paused = await client.post(endpoint + "/pause", json={"reason": "researcher pause"})
        assert paused.status_code == 200 and paused.json()["state"] == "paused"
        source = service.get_life_runner(runner_id)["sessions"][0]["run_id"]
        world.push(source, {"type": "message_received", "body": "Wake while orchestration paused"})
        await asyncio.sleep(0.15)
        assert len(messages) == 1
        assert (await client.get(endpoint)).json()["state"] == "paused"
        assert (
            await client.post(endpoint + "/execute", json={"execute": True, "budget_usd": 2})
        ).status_code == 200
        await until(lambda: service.get_life_runner(runner_id)["state"] in {"completed", "failed"})
        assert service.get_life_runner(runner_id)["state"] == "completed"
        assert len(messages) == 2


async def test_six_transfer_recipients_mount_actual_life_bundles_context_and_shuffled_control(
    running_app,
):
    app, messages = running_app
    world = app.state.world
    service, lives = world.services["studies"], world.services["lives"]
    prereg = {
        "experiment_id": "Transfer-engineering-fixture",
        "primary_metric": {"name": "fidelity", "threshold": 0.8},
        "seed_count": 5,
        "seed_count_rationale": "Software validation only",
        "stopping_rule": "Retain all started recipients",
        "likely_null": "No transfer",
        "exploratory_metrics": [],
    }

    async def donor(label):
        life = lives.create({"stage": 1, "budget": {"usd_life": 2, "wall_life_days": 28}})
        original = service._harness

        async def actual_donor(created, cell):
            service.harnesses.pop("B1")
            run_id = created["run_id"]
            evidence = await world.act(run_id, "deliver", {"kind": "note", "body": label})
            for index, rank in enumerate((3, 5)):
                result = await world.act(
                    run_id,
                    "believe",
                    {
                        "record": {
                            "kind": "rule",
                            "claim": f"{label} strategy {index}",
                            "rank": rank,
                            "evidence": [{"ref": evidence["action_id"], "class": "observed"}],
                        }
                    },
                )
                assert result["kind"] == "result"
            return await original(created, cell)

        service.harnesses["B1"] = actual_donor
        runner = service.plan_life(
            life["life_id"],
            {
                "baseline": "B1",
                "model": "fixture-model",
                "max_steps": 5,
                "max_tokens": 20,
                "prereg": prereg,
                "config": {
                    "packs": ["schedule", "belief", "deliver"],
                    "seed": 7,
                    "goal": label,
                    "budget": {"usd": 1, "wall_s": 30},
                },
            },
        )
        await service.execute_life(runner["runner_id"], execute=True, budget_usd=2)
        await until(lambda: service.get_life_runner(runner["runner_id"])["sessions"])
        if len(messages) < 2:
            await until(
                lambda: (
                    service.get_life_runner(runner["runner_id"])["sessions"][-1]["status"]
                    == "sleeping"
                )
            )
            source = service.get_life_runner(runner["runner_id"])["sessions"][-1]["run_id"]
            world.push(source, {"type": "message_received", "body": "Actual donor continuation"})
        await until(
            lambda: service.get_life_runner(runner["runner_id"])["state"] in {"completed", "failed"}
        )
        assert service.get_life_runner(runner["runner_id"])["state"] == "completed"
        return runner["runner_id"], lives.get(life["life_id"])["sessions"][-1]["run_id"]

    donor_id, donor_source = await donor("DonorAlphaSignal")
    other_id, _ = await donor("DonorBetaSignal")
    before = world.store.records(donor_source)
    reference = {"runner_id": donor_id, "session": "last", "seed": 7}
    cells = []
    for name in ("same", "cold", "context", "cross-model", "specificity", "shuffled"):
        cell = {
            "cell_id": name,
            "baseline": "B1",
            "seeds": [7],
            "max_steps": 1,
            "max_tokens": 20,
            "model": "fixture-model-2" if name == "cross-model" else "fixture-model",
            "config": {"stage": 1, "packs": ["belief"], "budget": {"usd": 1, "wall_s": 30}},
        }
        if name == "context":
            cell["depends_on"] = {"context": reference}
        elif name != "cold":
            cell["depends_on"] = {
                "beliefs": {
                    **reference,
                    "runner_id": other_id if name == "specificity" else donor_id,
                }
            }
        if name == "shuffled":
            cell["config"]["belief_import_policy"] = "shuffled"
        cells.append(cell)
    planned = service.plan({"prereg": prereg, "cells": cells})
    result = await service.execute(
        planned["study_id"], execute=True, budget_usd=6, options={"wait": True}
    )
    assert result["state"] == "completed", result
    rows = world.services["registry"].execution_log(planned["study_id"])
    assert len(rows) == 6
    runs = {row["cell"]: row["run_id"] for row in rows}
    assert world.services["beliefs"].fold(runs["same"])
    assert not any(
        "DonorAlphaSignal" in row["claim"] for row in world.services["beliefs"].fold(runs["cold"])
    )
    assert any(
        "DonorBetaSignal" in row["claim"]
        for row in world.services["beliefs"].fold(runs["specificity"])
    )
    shuffled = world.services["beliefs"].fold(runs["shuffled"])
    original = world.services["beliefs"].fold(donor_source)
    assert sorted(row["rank"] for row in shuffled) == sorted(row["rank"] for row in original)
    assert all("initial_condition" not in row for row in shuffled)
    assert any(
        row["belief"]["initial_condition"]["agent_endorsed"] is False
        for row in world.services["beliefs"].beliefs(runs["shuffled"])._records()
    )
    assert world.run_config(runs["context"])["context_import_policy"] == "captured"
    assert "DonorAlphaSignal" in json.dumps(messages[5])
    assert world.store.records(donor_source) == before
    assert all(world.store.verify_record(run_id)["valid"] for run_id in runs.values())
    from worldd.study_dependencies import resolve_dependencies

    source_context = service.get_life_runner(donor_id)["sessions"][-1]["context"]
    path = world.store.run_dir(donor_source) / source_context["path"]
    captured = path.read_bytes()
    try:
        path.write_text('[{"role":"user","content":"forged donor context"}]')
        count = len(world.store.list_runs())
        with pytest.raises(ValueError, match="context changed"):
            resolve_dependencies(service, {"context": reference}, 7)
        assert len(world.store.list_runs()) == count
    finally:
        path.write_bytes(captured)
    with pytest.raises(ValueError, match="actual registered dependencies"):
        service.plan(
            {
                "prereg": prereg,
                "cells": [
                    {**cells[1], "initial_messages": [{"role": "user", "content": "forged"}]}
                ],
            }
        )
