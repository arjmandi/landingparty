"""Human work stays on the lifetime ledger after its agent session seals."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from worldd.lives import LifeService
from worldd.storage import Store, canonical_json


@pytest.fixture
async def billing(tmp_path):
    store = Store(tmp_path)
    world = SimpleNamespace(store=store, services={}, jobs={})
    lives = LifeService(world)
    world.services["lives"] = lives
    life_id = lives.create({"budget": {"usd_life": 10}})["life_id"]
    config = lives.prepare_session(
        life_id,
        {
            "budget": {"usd": 10, "wall_s": 60},
            "human_time_priced": "in_budget",
            "human_rate_per_minute": 1,
        },
    )
    source = store.create_run(config)["run_id"]
    lives.attach_session(life_id, source)
    yield store, lives, life_id, source
    await lives.aclose()
    store.close()


def finish_session(store, lives, source):
    store.append_record(source, "seal")
    store.update_run(source, state="sealed")
    lives.session_ended(source)


async def test_active_human_hold_blocks_other_spend_then_debits_same_ledger(billing):
    store, lives, life, source = billing
    hold = lives.reserve_human(life, 6, evidence_ref="ask:one", source_run_id=source)
    assert lives.status(life)["remaining_usd"] == 4
    assert not store.debit(source, "model", 5)
    receipt = lives.settle_human(hold, 3)
    assert receipt["run_id"] == source
    assert store.spend(source) == {"human": 3}
    assert lives.status(life)["remaining_usd"] == 7
    assert lives.settle_human(hold, 3) == receipt
    with pytest.raises(ValueError, match="conflicts"):
        lives.settle_human(hold, 4)


async def test_late_charge_seals_private_accounting_run_without_mutating_source(billing):
    store, lives, life, source = billing
    assert store.debit(source, "model", 2)
    finish_session(store, lives, source)
    before = store.records(source)
    receipt = lives.charge_human(life, 3, evidence_ref="ask:late", source_run_id=source)
    run = store.get_run(receipt["run_id"])
    assert run["config"]["world_accounting"]
    assert run["state"] == "sealed"
    assert store.records(source) == before
    assert store.verify_record(receipt["run_id"])["valid"]
    assert store.reconcile(receipt["run_id"])["valid"]
    assert lives.status(life)["spent_usd"] == 5
    assert lives.charge_human(life, 3, evidence_ref="ask:late", source_run_id=source) == receipt
    assert len(lives.get(life)["sessions"]) == 1


async def test_concurrent_late_reservations_cannot_exceed_lifetime_cap(billing):
    store, lives, life, source = billing
    finish_session(store, lives, source)

    def reserve(index):
        try:
            return lives.reserve_human(life, 6, evidence_ref=f"ask:{index}", source_run_id=source)
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sum(value is not None for value in results) == 1
    assert lives.status(life)["reserved_usd"] == 6
    lives.release_human(next(value for value in results if value))
    assert lives.status(life)["remaining_usd"] == 10


async def test_new_session_cannot_spend_a_pending_late_human_ceiling(billing):
    store, lives, life, source = billing
    finish_session(store, lives, source)
    hold = lives.reserve_human(life, 6, evidence_ref="ask:across-wake", source_run_id=source)
    assert lives.prepare_session(life, {"budget": {"usd": 10}})["budget"]["usd"] == 4
    oversized = store.create_run({"life_id": life, "budget": {"usd": 8}})
    with pytest.raises(ValueError, match="remaining life cap"):
        lives.attach_session(life, oversized["run_id"])
    lives.release_human(hold)


async def test_recovery_finishes_committed_debit_without_charging_twice(billing):
    store, lives, life, source = billing
    finish_session(store, lives, source)
    hold_id = lives.reserve_human(life, 6, evidence_ref="ask:crash", source_run_id=source)
    hold = lives._human_hold(hold_id)
    # Simulate process loss between the ledger's atomic commit and receipt commit.
    hold.update(state="settling", actual_usd=3, settlement_evidence="ask:crash")
    with store._transaction():
        lives._write_human(hold)
    assert store.settle(hold["backing_reservation_id"], "human", 3)
    replacement = LifeService(lives.world)
    try:
        replacement._recover_billing()
        assert replacement._human_hold(hold_id)["state"] == "committed"
        assert replacement.status(life)["spent_usd"] == 3
        assert store.get_run(hold["billing_run_id"])["state"] == "sealed"
        assert (
            len([r for r in store.records(hold["billing_run_id"]) if r["kind"] == "ledger_debit"])
            == 1
        )
    finally:
        await replacement.aclose()


async def test_unsettled_sealed_session_freezes_life_without_fabricating_charge(billing):
    store, lives, life, source = billing
    hold = lives.reserve_human(life, 6, evidence_ref="ask:unresolved", source_run_id=source)
    finish_session(store, lives, source)
    before = store.records(source)
    lives._recover_billing()
    assert lives.get(life)["state"] == "billing_hold"
    assert lives._human_hold(hold)["state"] == "unresolved"
    assert store.records(source) == before
    assert lives.status(life)["spent_usd"] == 0
    with pytest.raises(ValueError, match="not awake"):
        lives.prepare_session(life, {})


async def test_pending_reservation_recovery_reuses_backing_hold(billing):
    store, lives, life, source = billing
    hold_id = lives.reserve_human(life, 6, evidence_ref="ask:prepare", source_run_id=source)
    hold = lives._human_hold(hold_id)
    hold.update(state="pending", backing_reservation_id=None)
    with store._transaction():
        store.db.execute(
            "UPDATE life_human_holds SET payload=? WHERE hold_id=?", (canonical_json(hold), hold_id)
        )
    lives._recover_billing()
    assert lives._human_hold(hold_id)["state"] == "reserved"
    assert store.reserved(source) == 6
    assert json.loads(
        store.db.execute(
            "SELECT payload FROM life_human_holds WHERE hold_id=?", (hold_id,)
        ).fetchone()[0]
    )["backing_reservation_id"]


async def test_untracked_outstanding_human_work_blocks_life_end_and_can_be_released(billing):
    store, lives, life, source = billing
    finish_session(store, lives, source)
    hold = lives.reserve_human(life, 6, evidence_ref="human:pending-work", source_run_id=source)
    with pytest.raises(ValueError, match="Outstanding human work"):
        await lives.end(life)
    assert lives.get(life)["state"] == "billing_hold" and not lives.get(life)["ended"]
    lives.release_human(hold)
    await lives.end(life)
    assert lives.get(life)["ended"]
