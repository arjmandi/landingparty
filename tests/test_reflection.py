import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from worldd.kernel import BeliefService
from worldd.reflection import ReflectionService
from worldd.storage import Store


class WorldFixture:
    def __init__(self, tmp_path):
        self.store = Store(tmp_path)
        self.services = {}
        self.jobs = {}
        self.events = []

    def push(self, run_id, event):
        self.events.append((run_id, event))
        return self.store.push_event(run_id, event)


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = WorldFixture(tmp_path)
    clock = lambda: datetime(2026, 10, 22, 12, tzinfo=UTC)
    monkeypatch.setattr("worldd.storage.utc_now", lambda: clock().isoformat())
    monkeypatch.setattr("worldd.kernel.now", lambda: clock().isoformat())
    beliefs = BeliefService(world, clock=clock)
    world.services["beliefs"] = beliefs
    yield world, clock
    beliefs.database.close()
    world.store.close()


def run(world, policy="self", **overrides):
    config = {"budget": {"usd": 1}, "reflection_policy": policy, "reflect_share": 0.15, **overrides}
    run_id = world.store.create_run(config)["run_id"]
    world.store.append_record(run_id, "outcome", action_id="a_evidence")
    return run_id


def metered(world, *, operations=None, charge="0.01", wrong_tag=False):
    calls = []

    async def callback(**request):
        calls.append(request)
        amount = Decimal(charge)
        assert amount <= Decimal(request["max_usd"])
        hold = world.store.reserve(request["run_id"], amount)
        assert hold is not None
        assert world.store.settle(hold, "model", amount)
        call_id = f"call_{len(calls)}"
        world.store.append_record(
            request["run_id"],
            "model_call",
            call_id=call_id,
            usd=float(amount),
            usage={"input": 50, "output": 20},
            tags=["model"] if wrong_tag else [request["tag"]],
            reservation_id=hold,
        )
        text = "I should reconsider the new observation."
        if request["purpose"] == "reflection_structuring":
            text = json.dumps({"operations": operations or []})
        return {"text": text, "call_id": call_id}

    return callback, calls


async def test_none_arm_does_not_call_model_or_change_beliefs(world):
    world, clock = world
    run_id = run(world, "none")
    callback, calls = metered(world)
    service = ReflectionService(world, metered_callback=callback, clock=clock)
    try:
        result = await service.trigger(run_id, {"type": "belief_review", "event_id": "e_one"})
        assert result["reason"] == "reflection_none"
        assert calls == [] and world.store.total_spent(run_id) == 0
    finally:
        service.database.close()


async def test_self_two_passes_are_separately_metered_and_tagged(world):
    world, clock = world
    run_id = run(world)
    callback, calls = metered(
        world,
        operations=[
            {
                "tool": "believe",
                "params": {
                    "record": {
                        "kind": "rule",
                        "claim": "Recheck the latest observation.",
                        "rank": 3,
                        "evidence": [{"ref": "a_evidence", "class": "self_inference"}],
                    }
                },
            }
        ],
    )
    service = ReflectionService(world, metered_callback=callback, clock=clock)
    try:
        result = await service.trigger(run_id, {"type": "belief_review", "event_id": "e_one"})
        assert result["kind"] == "result"
        assert len(calls) == 2
        assert {call["tag"] for call in calls} == {"model.reflect"}
        assert [call["purpose"] for call in calls] == [
            "reflection_reasoning",
            "reflection_structuring",
        ]
        assert world.store.total_spent(run_id) == 0.02
        assert len(world.services["beliefs"].fold(run_id)) == 1
        assert (await service.trigger(run_id, {"type": "belief_review", "event_id": "e_one"}))[
            "reason"
        ] == "already_triggered"
    finally:
        service.database.close()


async def test_unregistered_trigger_does_not_call_model(world):
    world, clock = world
    run_id = run(world)
    callback, calls = metered(world)
    service = ReflectionService(world, metered_callback=callback, clock=clock)
    try:
        assert (await service.trigger(run_id, {"type": "page_changed"}))[
            "reason"
        ] == "no_registered_trigger"
        assert calls == []
    finally:
        service.database.close()


async def test_callback_cannot_assert_metering_without_durable_usage_and_debit(world):
    world, clock = world
    run_id = run(world)

    async def unmetered(**request):
        return {"text": "Do something", "call_id": "invented", "metered": True}

    service = ReflectionService(world, metered_callback=unmetered, clock=clock)
    try:
        result = await service.trigger(run_id, {"type": "belief_review", "event_id": "e_one"})
        assert result["kind"] == "blocked"
        assert "metered model-call evidence" in result["detail"]
        assert world.services["beliefs"].fold(run_id) == []
    finally:
        service.database.close()


async def test_wrong_model_lane_tag_rejected_before_using_model_output(world):
    world, clock = world
    run_id = run(world)
    callback, calls = metered(world, wrong_tag=True)
    service = ReflectionService(world, metered_callback=callback, clock=clock)
    try:
        result = await service.trigger(run_id, {"type": "belief_review", "event_id": "e_one"})
        assert result["kind"] == "blocked"
        assert len(calls) == 1
    finally:
        service.database.close()


async def test_reflection_share_stops_next_call_without_mutating_budget(world):
    world, clock = world
    run_id = run(world, reflect_share=0.02)
    callback, calls = metered(world)
    service = ReflectionService(world, metered_callback=callback, clock=clock)
    try:
        assert (await service.trigger(run_id, {"type": "belief_review", "event_id": "e_one"}))[
            "kind"
        ] == "result"
        result = await service.trigger(run_id, {"type": "belief_review", "event_id": "e_two"})
        assert result["kind"] == "blocked"
        assert "share exhausted" in result["detail"]
        assert len(calls) == 2
    finally:
        service.database.close()


async def test_companion_is_proposed_only_and_waits_for_idle(world):
    world, clock = world
    run_id = run(world, "companion")
    callback, calls = metered(
        world,
        operations=[
            {
                "tool": "believe",
                "params": {
                    "record": {
                        "kind": "strategy",
                        "claim": "Try a different source.",
                        "rank": 5,
                        "evidence": [{"ref": "a_evidence", "class": "self_inference"}],
                    }
                },
            }
        ],
    )
    service = ReflectionService(world, metered_callback=callback, clock=clock)
    try:
        world.jobs[run_id] = {"a_work": object()}
        assert (await service.companion_cycle(run_id))["reason"] == "not_idle"
        assert calls == []
        world.jobs[run_id] = {}
        result = await service.companion_cycle(run_id)
        assert result["kind"] == "result"
        proposed = world.services["beliefs"].fold(run_id)[0]
        assert proposed["status"] == "proposed" and proposed["author"] == "reflector"
        assert {call["tag"] for call in calls} == {"model.reflector"}
    finally:
        service.database.close()


async def test_companion_cannot_rerank_or_retract_agent_belief(world):
    world, clock = world
    run_id = run(world, "companion")
    original = (
        await world.services["beliefs"].handle(
            run_id,
            "a_first",
            "believe",
            {
                "record": {
                    "kind": "rule",
                    "claim": "Keep evidence.",
                    "rank": 4,
                    "evidence": [{"ref": "a_evidence", "class": "observed"}],
                }
            },
        )
    )["data"]
    callback, _ = metered(
        world,
        operations=[{"tool": "revise", "params": {"id": original["id"], "delta": {"rank": 0}}}],
    )
    service = ReflectionService(world, metered_callback=callback, clock=clock)
    try:
        result = await service.companion_cycle(run_id)
        assert result["kind"] == "blocked"
        assert world.services["beliefs"].fold(run_id)[0]["rank"] == 4
        assert world.services["beliefs"].fold(run_id)[0]["author"] == "agent"
    finally:
        service.database.close()


async def test_fifty_cycle_ceiling_has_no_fifty_first_provider_call(world):
    world, clock = world
    run_id = run(world)
    callback, calls = metered(world, charge="0.00001")
    service = ReflectionService(world, metered_callback=callback, clock=clock)
    try:
        for index in range(50):
            result = await service.trigger(
                run_id, {"type": "belief_review", "event_id": f"e_{index}"}
            )
            assert result["kind"] == "result"
        denied = await service.trigger(run_id, {"type": "belief_review", "event_id": "e_51"})
        assert denied["rule_id"] == "reflection_ceiling"
        assert len(calls) == 100
    finally:
        service.database.close()


async def test_companion_recovery_worker_runs_once_for_each_new_idle_world_context(world):
    import asyncio

    world, clock = world
    run(world, "companion")
    callback, calls = metered(world)
    service = ReflectionService(world, metered_callback=callback, clock=clock)
    try:
        await service.recover()
        for _ in range(100):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == 2
        assert {request["tag"] for request in calls} == {"model.reflector"}
    finally:
        await service.aclose()


async def test_no_flags_arm_suppresses_excess_self_inference_diagnostics(world):
    world, clock = world
    run_id = run(world, validator_policy_id="structural-no-flags@1")
    service = ReflectionService(world, clock=clock)
    try:
        operations = [
            {
                "tool": "believe",
                "params": {
                    "record": {
                        "kind": "rule",
                        "claim": f"Inference {index}",
                        "rank": 4,
                        "evidence": [{"ref": "a_evidence", "class": "self_inference"}],
                    }
                },
            }
            for index in range(6)
        ]
        await service._apply(run_id, operations)
        assert not [row for row in service.database.journal(run_id) if row["kind"] == "belief_flag"]
        assert all(
            not row["belief"]["flags"]
            for row in world.services["beliefs"].beliefs(run_id)._records()
        )
    finally:
        service.database.close()


async def test_companion_context_biases_whole_open_ticket_entity_and_ignores_closed_assumptions(
    world,
):
    from types import SimpleNamespace

    world, clock = world
    run_id = run(world, "companion")
    beliefs = world.services["beliefs"]
    world.store.append_record(run_id, "ask_opened", ticket_id="t_open", state="open")
    for subject, assumptions in (("A unrelated", []), ("Z harbour", ["t_open"]), ("Z harbour", [])):
        result = await beliefs.handle(
            run_id,
            "a_context",
            "believe",
            {
                "record": {
                    "kind": "fact",
                    "claim": subject + " is open",
                    "rank": 3,
                    "triple": {"subject": subject, "predicate": "state", "object": "open"},
                    "assumptions": assumptions,
                    "evidence": [{"ref": "a_evidence", "class": "observed"}],
                }
            },
        )
        assert result["kind"] == "result", result
    world.services["asks"] = SimpleNamespace(
        list_tickets=lambda source: [
            {"ticket_id": "t_open", "state": "open"},
            {"ticket_id": "t_closed", "state": "approved"},
        ]
    )
    service = ReflectionService(world, clock=clock)
    try:
        context = service._context(run_id, {"type": "idle"}, companion=True)
        assert context["open_ticket_ids"] == ["t_open"]
        assert [row["triple"]["subject"] for row in context["beliefs"][:2]] == [
            "Z harbour",
            "Z harbour",
        ]
    finally:
        service.database.close()
