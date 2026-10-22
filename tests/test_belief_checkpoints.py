from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from analysis.belief_checkpoints import belief_checkpoints
from worldd.kernel import BeliefService
from worldd.storage import Store


@pytest.fixture
def environment(tmp_path, monkeypatch):
    clock = datetime(2026, 10, 22, 12, tzinfo=UTC)
    monkeypatch.setattr("worldd.storage.utc_now", lambda: clock.isoformat())
    monkeypatch.setattr("worldd.kernel.now", lambda: clock.isoformat())
    store = Store(tmp_path)
    world = SimpleNamespace(store=store, services={}, push=lambda *args: None)
    service = BeliefService(world, clock=lambda: clock)
    world.services["beliefs"] = service
    yield world, service
    service.database.close()
    store.close()


async def add(service, run_id, subject):
    reply = await service.handle(
        run_id,
        "a_write_" + subject,
        "believe",
        {
            "record": {
                "kind": "fact",
                "claim": subject + " is open",
                "rank": 4,
                "triple": {"subject": subject, "predicate": "state", "object": "open"},
                "evidence": [{"ref": "a_source", "class": "observed"}],
            }
        },
    )
    assert reply["kind"] == "result", reply
    return reply["data"]


async def test_actual_cost_prefixes_do_not_leak_later_beliefs_or_evidence_with_tied_timestamps(
    environment,
):
    world, service = environment
    store = world.store
    run_id = store.create_run({"budget": {"usd": 10}})["run_id"]
    store.append_record(run_id, "outcome", action_id="a_source")
    harbour, unrelated = await add(service, run_id, "harbour"), await add(service, run_id, "gate")
    store.debit(run_id, "model", 1)
    future = await add(service, run_id, "future")
    store.debit(run_id, "model", 1.5)
    await service.observe(
        run_id, {"observation_ref": "o_conflict", "belief_id": harbour["id"], "supports": False}
    )
    store.append_record(
        run_id,
        "action",
        action_id="a_read",
        tool="beliefs",
        params={"query": "why", "id": harbour["id"]},
    )
    read = await service.handle(run_id, "a_read", "beliefs", {"query": "why", "id": harbour["id"]})
    store.append_record(run_id, "outcome", action_id="a_read", reply=read)
    store.debit(run_id, "model", 1)
    revised = await service.handle(
        run_id,
        "a_revise",
        "revise",
        {
            "id": harbour["id"],
            "delta": {
                "rank": 2,
                "evidence": [*harbour["evidence"], {"ref": "o_conflict", "class": "observed"}],
            },
        },
    )
    assert revised["kind"] == "result", revised
    store.debit(run_id, "model", 1.5)
    await service.observe(
        run_id, {"observation_ref": "o_after_half", "belief_id": unrelated["id"], "supports": False}
    )
    store.append_record(run_id, "seal")
    store.update_run(run_id, state="sealed")
    report = belief_checkpoints(world, run_id)
    first, quarter, half, full = report["checkpoints"]
    assert {row["id"] for row in first["beliefs"]} == {harbour["id"], unrelated["id"]}
    assert future["id"] in {row["id"] for row in quarter["beliefs"]}
    early = next(row for row in quarter["beliefs"] if row["id"] == harbour["id"])
    late = next(row for row in half["beliefs"] if row["id"] == harbour["id"])
    assert early["rank"] == 4 and early["n_contra"] == 0
    assert late["rank"] == 2 and late["n_contra"] == 1
    assert next(row for row in half["beliefs"] if row["id"] == unrelated["id"])["n_contra"] == 0
    assert (
        next(row for row in service.fold(run_id) if row["id"] == unrelated["id"])["n_contra"] == 1
    )
    assert half["metrics"]["revision_latency"]["median_latency_usd"] == 1
    assert half["metrics"]["revision_latency"]["median_latency_actions"] == 1
    assert half["revision_rate"]["counts"] == {"new_nonself_evidence_linked": 1}
    assert half["preservation"]["n"] == 0 and half["preservation"]["events_checked"] == 1
    assert all(row["status"] == "unmeasured" for row in half["rank_falsification_curve"]["bins"])
    assert full["status"] == "unmeasured" and report["status"] == "partial"
    assert first["anchor_ts"] == half["anchor_ts"]  # Exact sequence, not timestamp-only folding.


async def test_import_boundaries_and_after_seal_evidence_do_not_leak_into_budget_checkpoints(
    environment,
):
    world, service = environment
    store = world.store
    source = store.create_run({"budget": {"usd": 1}})["run_id"]
    store.append_record(source, "outcome", action_id="a_source")
    original = await add(service, source, "harbour")
    await service.observe(
        source, {"observation_ref": "o_imported", "belief_id": original["id"], "supports": False}
    )
    target = store.create_run({"budget": {"usd": 1}})["run_id"]
    service.import_continuity(target, service.export_continuity(source))
    store.debit(target, "model", 1)
    store.append_record(target, "seal")
    store.update_run(target, state="sealed")
    before = store.records(target)
    await service.observe(
        target, {"observation_ref": "o_after_seal", "belief_id": original["id"], "supports": False}
    )
    report = belief_checkpoints(world, target, fractions=(1,))
    assert report["status"] == "measured"
    assert report["checkpoints"][0]["beliefs"][0]["n_contra"] == 1
    assert service.fold(target)[0]["n_contra"] == 2
    assert store.records(target) == before


async def test_revision_checkpoints_include_later_retractions_and_require_prior_companion_confirmation(
    environment,
):
    world, service = environment
    store = world.store
    run_id = store.create_run({"budget": {"usd": 1}, "reflection_policy": "companion"})["run_id"]
    store.append_record(run_id, "outcome", action_id="a_source")
    revised = await add(service, run_id, "revised")
    unconfirmed = await add(service, run_id, "unconfirmed")
    retained = await add(service, run_id, "retained")
    await service.handle(
        run_id, "a_unconfirmed_edit", "revise", {"id": revised["id"], "delta": {"rank": 3}}
    )
    for record in (revised, retained):
        accepted = await service.handle(
            run_id,
            "a_accept_" + record["id"],
            "revise",
            {"id": record["id"], "delta": {"status": "held"}},
        )
        assert accepted["kind"] == "result", accepted
    for record in (revised, unconfirmed):
        await service.observe(
            run_id,
            {
                "observation_ref": "o_" + record["id"],
                "belief_id": record["id"],
                "supports": False,
            },
        )
    store.debit(run_id, "model", 0.5)
    await service.handle(run_id, "a_retract", "retract", {"id": revised["id"]})
    store.debit(run_id, "model", 0.5)
    store.append_record(run_id, "seal")
    store.update_run(run_id, state="sealed")
    checkpoint = belief_checkpoints(world, run_id, fractions=(1,))["checkpoints"][0]
    assert [row["id"] for row in checkpoint["beliefs"]] == [retained["id"]]
    assert checkpoint["revision_eligibility"]["n"] == 1
    assert checkpoint["revision_rate"]["n"] == 1  # Pre-confirmation rank edit is excluded.
    assert checkpoint["triple_revision_rate"]["n"] == 1
    assert checkpoint["metrics"]["revision_latency"]["median_latency_usd"] == 0.5
