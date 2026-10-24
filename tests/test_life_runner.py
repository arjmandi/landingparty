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


def native_life(app, callback, *, usd=2):
    world = app.state.world
    service = world.services["studies"]
    service.harnesses["native"] = callback
    life = world.services["lives"].create(
        {
            "stage": 2,
            "budget": {"usd_life": usd, "wall_life_days": None},
            "driver": {"metric": "usd_balance"},
            "income_rail": {"kind": "bounty", "usd_per_credit": 1},
            "on_exhaustion": "strand",
            "on_harness_exit": "rest",
        }
    )
    runner = service.plan_life(
        life["life_id"],
        {
            "baseline": "native",
            "model": "fixture-model",
            "config": {
                "harness_id": "claude-code@9.9.9/api_key",
                "packs": ["schedule", "deliver"],
                "budget": {"usd": 10000, "wall_s": 30},
            },
        },
    )
    return life["life_id"], service, runner


async def test_a_native_exit_rests_the_life_and_a_real_event_wakes_it(running_app):
    app, _ = running_app
    world = app.state.world
    lives = world.services["lives"]
    calls = []

    def callback(created, cell):
        calls.append((created["run_id"], cell["config"]["harness_id"]))
        return {}

    life_id, service, runner = native_life(app, callback)
    await service.execute_life(runner["runner_id"], execute=True, budget_usd=2)
    await until(lambda: calls and lives.status(life_id)["life_state"] == "resting")
    assert calls[0][1] == "claude-code@9.9.9/api_key"
    assert world.store.reconcile(calls[0][0])["cap_usd"] == 2
    world.push(calls[0][0], {"type": "message_received", "body": "A later fixture message"})
    await until(lambda: len(calls) == 2 and lives.status(life_id)["life_state"] == "resting")
    assert lives.get(life_id)["ended"] is None
    await lives.end(life_id, reason="beam_up")
    await until(lambda: service.get_life_runner(runner["runner_id"])["state"] == "completed")
    assert lives.get(life_id)["ended"]["reason"] == "beam_up"


async def test_a_stranded_life_waits_for_income_instead_of_failing(running_app):
    app, _ = running_app
    world = app.state.world
    lives = world.services["lives"]
    calls = []

    def callback(created, cell):
        calls.append(created["run_id"])
        cap = world.store.reconcile(created["run_id"])["cap_usd"]
        assert world.store.debit(created["run_id"], "model", cap)
        return {}

    life_id, service, runner = native_life(app, callback, usd=1)
    await service.execute_life(runner["runner_id"], execute=True, budget_usd=1)
    await until(lambda: lives.get(life_id)["state"] == "stranded")
    await asyncio.sleep(0.3)
    assert len(calls) == 1
    assert service.get_life_runner(runner["runner_id"])["waiting_reason"] == "stranded"
    lives.credit(life_id, 1, "bounty", evidence_ref="bounty:fixture-award")
    await until(lambda: len(calls) == 2 and lives.get(life_id)["state"] == "stranded")
    assert world.store.reconcile(calls[1])["cap_usd"] == 1
    assert lives.get(life_id)["ended"] is None
    await lives.end(life_id, reason="beam_up")
    await until(lambda: service.get_life_runner(runner["runner_id"])["state"] == "completed")


def model_error(world, run_id, status, error_class, **extra):
    world.store.append_record(
        run_id,
        "model_error",
        status=status,
        error=None,
        error_class=error_class,
        model="fixture-model",
        message="The provider refused",
        **extra,
    )


async def test_a_model_the_provider_refuses_holds_the_life_instead_of_resting(running_app):
    app, _ = running_app
    world = app.state.world
    lives = world.services["lives"]
    calls = []

    def callback(created, cell):
        calls.append(created["run_id"])
        model_error(world, created["run_id"], 404, "model_unavailable")
        return {}

    life_id, service, runner = native_life(app, callback)
    await service.execute_life(runner["runner_id"], execute=True, budget_usd=2)
    await until(lambda: lives.status(life_id)["life_state"] == "held")
    status = lives.status(life_id)
    assert status["hold_reason"]["kind"] == "model_unavailable"
    assert status["hold_reason"]["status"] == 404 and status["wake_rules"] == {}
    await asyncio.sleep(0.3)
    assert len(calls) == 1
    assert service.get_life_runner(runner["runner_id"])["waiting_reason"] == "model_hold"
    # Events do not wake a held life; only the operator's resolution does.
    world.push(calls[0], {"type": "message_received", "body": "Ignored while held"})
    await asyncio.sleep(0.2)
    assert lives.get(life_id)["state"] == "model_hold"
    lives.resume_held(life_id, reason="model_switched")
    await until(lambda: len(calls) == 2)
    await lives.end(life_id, reason="beam_up")


async def test_an_unestimable_session_holds_the_life_too(running_app):
    app, _ = running_app
    world = app.state.world
    lives = world.services["lives"]

    def callback(created, cell):
        world.store.append_record(
            created["run_id"],
            "estimate_unavailable",
            reason="No pinned price",
            metering="transcript",
        )
        return {}

    life_id, service, runner = native_life(app, callback)
    await service.execute_life(runner["runner_id"], execute=True, budget_usd=2)
    await until(lambda: lives.status(life_id)["life_state"] == "held")
    assert lives.status(life_id)["hold_reason"]["kind"] == "estimate_unavailable"
    await lives.end(life_id, reason="beam_up")


async def test_a_plan_limit_rests_until_its_reset_and_an_outage_backs_off(running_app):
    app, _ = running_app
    world = app.state.world
    lives = world.services["lives"]
    reset = "2099-01-01T00:00:00+00:00"
    errors = iter(
        [
            (429, "plan_limit", {"resets_at": reset}),
            (529, "provider_error", {}),
            (503, "provider_error", {}),
        ]
    )
    calls = []

    def callback(created, cell):
        calls.append(created["run_id"])
        status, kind, extra = next(errors)
        model_error(world, created["run_id"], status, kind, **extra)
        return {}

    life_id, service, runner = native_life(app, callback)
    await service.execute_life(runner["runner_id"], execute=True, budget_usd=2)
    await until(lambda: lives.status(life_id)["life_state"] == "resting")
    rules = lives.get(life_id)["wake_rules"]
    assert rules["cause"] == "plan_limit" and rules["until"] == reset
    for backoff in (900, 1800):
        before = len(calls)
        world.push(calls[-1], {"type": "message_received", "body": "Wake for the next session"})
        await until(
            lambda before=before: (
                len(calls) == before + 1 and lives.status(life_id)["life_state"] == "resting"
            )
        )
        life = lives.get(life_id)
        assert life["wake_rules"]["cause"] == "provider_error"
        assert life["provider_backoff_s"] == backoff
    await lives.end(life_id, reason="beam_up")


async def test_a_confirmed_objective_claim_ends_the_life_even_across_rest(running_app):
    app, _ = running_app
    world = app.state.world
    lives, asks = world.services["lives"], world.services["asks"]
    life_id = lives.create(
        {
            "budget": {"usd_life": 2, "wall_life_days": None},
            "objective": "Write one useful note.",
            "on_harness_exit": "rest",
            "reply_policy": "silent",
        }
    )["life_id"]
    created = await lives.start_session(life_id, {"budget": {"usd": 1, "wall_s": 30}})
    run_id = created["run_id"]
    assert "claim_objective" in world.run_config(run_id)["preamble"]
    assert world.run_config(run_id)["goal"] == "Write one useful note."
    rejected = await lives.handle(run_id, "a_claim", "claim_objective", {"text": "Done early."})
    asks.answer(rejected["ticket_id"], "reject", reason="not_yet")
    claim = await lives.handle(
        run_id, "a_claim_2", "claim_objective", {"text": "Done.", "evidence_refs": ["a_1"]}
    )
    assert asks.get(claim["ticket_id"])["kind"] == "objective_claim"
    await world.end_run(run_id, reason="harness_stopped")
    lives.rest(life_id, run_id)
    assert lives.status(life_id)["life_state"] == "resting"
    assert asks.get(claim["ticket_id"])["state"] == "open"
    asks.answer(claim["ticket_id"], "approve")
    await until(lambda: lives.get(life_id)["ended"] is not None)
    assert lives.get(life_id)["ended"]["reason"] == "objective_achieved"


async def test_claiming_needs_a_life_with_an_objective(running_app):
    app, _ = running_app
    world = app.state.world
    lives = world.services["lives"]
    life_id = lives.create({"budget": {"usd_life": 2, "wall_life_days": None}})["life_id"]
    created = await lives.start_session(life_id, {"budget": {"usd": 1, "wall_s": 30}})
    reply = await lives.handle(created["run_id"], "a_claim", "claim_objective", {"text": "Done."})
    assert reply["rule_id"] == "objective_required"
