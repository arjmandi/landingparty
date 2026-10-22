import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from worldd.storage import GENESIS_HASH, Store, content_hash


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "private")
    yield instance
    instance.close()


def start(store, cap="1"):
    return store.create_run({"budget": {"usd": cap, "wall_s": 30}})["run_id"]


def test_exact_atomic_cap_under_parallel_debits(store):
    run_id = start(store, "0.1")
    with ThreadPoolExecutor(max_workers=12) as pool:
        charged = list(pool.map(lambda _: store.debit(run_id, "search", "0.003"), range(70)))
    assert sum(charged) == 33
    assert store.total_spent(run_id) == 0.099
    assert store.spend(run_id) == {"search": 0.099}
    assert store.debit(run_id, "model", Decimal("0.001"))
    assert store.total_spent(run_id) == 0.1
    assert not store.debit(run_id, "search", "0.000000001")
    assert store.reconcile(run_id)["valid"]
    assert store.verify_record(run_id)["valid"]


@pytest.mark.parametrize("invalid", [-1, "NaN", "Infinity", "-Infinity", True, "bad"])
def test_invalid_money_never_writes(store, invalid):
    run_id = start(store)
    before = store.records(run_id)
    with pytest.raises((ValueError, TypeError)):
        store.debit(run_id, "model", invalid)
    assert store.total_spent(run_id) == 0
    assert store.records(run_id) == before


def test_subnanodollar_rounds_up(store):
    run_id = start(store, "0.000000001")
    assert store.debit(run_id, "model", "0.00000000001")
    assert store.total_spent(run_id) == 0.000000001
    assert not store.debit(run_id, "model", "0.00000000001")


def test_cap_never_rounds_up_above_requested_budget(store):
    run_id = start(store, "0.0000000019")
    assert store.debit(run_id, "model", "0.000000001")
    assert not store.debit(run_id, "model", "0.000000001")


def test_decimal_budget_and_huge_amount_validation(store):
    run_id = start(store, Decimal("0.01"))
    assert store.debit(run_id, "model", Decimal("0.01"))
    with pytest.raises(ValueError):
        store.debit(run_id, "model", "1e1000000")


def test_reservations_prevent_concurrent_unmetered_spend(store):
    run_id = start(store)
    hold = store.reserve(run_id, "0.8")
    assert hold is not None
    assert store.reserved(run_id) == 0.8
    assert store.reserve(run_id, "0.3") is None
    assert not store.debit(run_id, "search", "0.3")
    assert store.settle(hold, "model", "0.3")
    assert store.reserved(run_id) == 0
    assert store.total_spent(run_id) == 0.3
    assert not store.settle(hold, "model", "0.3")
    assert store.debit(run_id, "search", "0.7")
    assert store.reconcile(run_id)["valid"]


def test_ledger_observer_sees_committed_charge_and_can_append_its_record(store):
    run_id = start(store)
    observed = []

    def capture(source, event):
        assert source == run_id and not store.db.in_transaction
        assert store._mirror_offsets is None
        assert store._ledger_observation == (source, event["seq"], event["hash"])
        assert store.records(source)[-1] == event
        observed.append((event, store.total_spent(source)))
        store.append_record(source, "workspace_checkpoint", debit_seq=event["seq"])

    store.ledger_observer = capture
    assert store.debit(run_id, "model", ".2")
    hold = store.reserve(run_id, ".5")
    assert len(observed) == 1
    assert not store.settle(hold, "model", ".6")
    assert len(observed) == 1
    store.update_run(run_id, state="stopping")
    assert store.settle(hold, "model", ".3")
    assert not store.settle(hold, "model", ".3")
    assert [total for _, total in observed] == [0.2, 0.5]
    assert observed[1][0]["reservation_id"] == hold
    assert store.records(run_id)[-1]["kind"] == "workspace_checkpoint"
    assert store._ledger_observation is None
    assert store.verify_record(run_id)["valid"] and store.reconcile(run_id)["valid"]


def test_reentrant_ledger_observation_restores_outer_trusted_context(store):
    run_id = start(store)
    contexts = []

    def observe(source, event):
        expected = (source, event["seq"], event["hash"])
        assert store._ledger_observation == expected
        contexts.append(expected)
        if event["category"] == "outer":
            assert store.debit(source, "inner", ".1")
            assert store._ledger_observation == expected

    store.ledger_observer = observe
    assert store.debit(run_id, "outer", ".1")
    assert len(contexts) == 2 and contexts[0] != contexts[1]
    assert store._ledger_observation is None
    assert store.total_spent(run_id) == 0.2 and store.verify_record(run_id)["valid"]


def test_ledger_rollback_never_calls_observer(store, monkeypatch):
    run_id = start(store)
    observed = []
    store.ledger_observer = lambda *args: observed.append(args)
    charge = store._charge

    def fail(*args, **kwargs):
        charge(*args, **kwargs)
        raise OSError("charge commit interrupted")

    monkeypatch.setattr(store, "_charge", fail)
    with pytest.raises(OSError):
        store.debit(run_id, "model", ".2")
    assert observed == [] and store.total_spent(run_id) == 0
    hold = store.reserve(run_id, ".5")
    with pytest.raises(OSError):
        store.settle(hold, "model", ".2")
    assert observed == [] and store.reserved(run_id) == 0.5
    assert store.verify_record(run_id)["valid"]


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_ledger_observer_failure_does_not_change_actual_billing_result(store, error):
    run_id = start(store)

    def fail(*args):
        raise error("capture interrupted after charge")

    store.ledger_observer = fail
    assert store.debit(run_id, "model", ".2")
    assert store.total_spent(run_id) == 0.2
    event = store.records(run_id)[-1]
    assert event["kind"] == "checkpoint_capture_error"
    assert event["error"] == error.__name__
    assert event["debit_ref"]["hash"] == store.records(run_id)[-2]["hash"]
    assert store.verify_record(run_id)["valid"] and store.reconcile(run_id)["valid"]


def test_ledger_observer_never_adds_failure_after_callback_seals_source(store):
    run_id = start(store)

    def seal_then_fail(source, event):
        store.append_record(source, "seal")
        store.update_run(source, state="sealed")
        raise ValueError("observer cleanup after seal")

    store.ledger_observer = seal_then_fail
    assert store.debit(run_id, "model", ".2")
    assert store.records(run_id)[-1]["kind"] == "seal"
    assert store.verify_record(run_id)["valid"] and store.reconcile(run_id)["valid"]


def test_terminal_source_cannot_settle_a_hold_or_notify_an_observer(store):
    run_id = start(store)
    hold = store.reserve(run_id, ".5")
    observed = []
    store.ledger_observer = lambda *args: observed.append(args)
    store.append_record(run_id, "seal")
    store.update_run(run_id, state="sealed")
    before = store.records(run_id)
    assert not store.settle(hold, "model", ".2")
    assert store.reserved(run_id) == 0.5 and store.total_spent(run_id) == 0
    assert observed == [] and store.records(run_id) == before


def test_parallel_ledger_observer_completes_under_lock_before_next_charge(store):
    run_id = start(store)
    observed = []

    def capture(source, event):
        assert not store.db.in_transaction
        total_nano = store._row(source)["spent_nano"]
        observed.append((event["seq"], total_nano))
        store.append_record(source, "workspace_checkpoint", debit_seq=event["seq"])

    store.ledger_observer = capture
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.debit(run_id, "model", ".05"), range(30)))
    assert sum(results) == len(observed) == 20
    assert [nano for _, nano in observed] == [50_000_000 * index for index in range(1, 21)]
    events = store.records(run_id)
    for seq, _ in observed:
        assert events[seq]["kind"] == "workspace_checkpoint" and events[seq]["debit_seq"] == seq
    assert store.verify_record(run_id)["valid"] and store.reconcile(run_id)["valid"]


def test_oversettlement_preserves_hold_until_explicit_release(store):
    run_id = start(store)
    hold = store.reserve(run_id, "0.4")
    assert not store.settle(hold, "model", "0.5")
    assert store.reserved(run_id) == 0.4
    assert store.total_spent(run_id) == 0
    store.release(hold)
    store.release(hold)
    assert store.reserved(run_id) == 0
    assert store.debit(run_id, "search", "1")


def test_same_database_connections_serialize_ledger_and_log(store):
    run_id = start(store, "0.1")
    second = Store(store.data_dir)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda instance: [instance.debit(run_id, "search", "0.01") for _ in range(20)],
                    [store, second],
                )
            )
        assert sum(sum(result) for result in results) == 10
        assert store.total_spent(run_id) == 0.1
        assert store.verify_record(run_id)["valid"]
    finally:
        second.close()


def test_hash_chain_and_mirror_detect_tampering(store):
    run_id = start(store)
    event = store.append_record(run_id, "action", action_id="a_first", params={"ü": "value"})
    records = store.records(run_id)
    assert records[0]["prev_hash"] == GENESIS_HASH
    assert event["seq"] == 2
    assert event["prev_hash"] == records[0]["hash"]
    assert event["hash"] == content_hash(
        {key: value for key, value in event.items() if key != "hash"}
    )
    assert store.verify_record(run_id)["valid"]
    path = store.run_dir(run_id) / "record.jsonl"
    altered = path.read_text().replace("a_first", "a_other")
    path.write_text(altered)
    verification = store.verify_record(run_id)
    assert not verification["valid"]
    assert "mirror" in verification["error"].lower()


def test_corrupted_authoritative_chain_detected(store):
    run_id = start(store)
    store.append_record(run_id, "action", action_id="a_before")
    row = store.db.execute(
        "SELECT payload FROM records WHERE run_id=? AND seq=2", (run_id,)
    ).fetchone()
    payload = json.loads(row[0])
    payload["action_id"] = "a_after"
    store.db.execute(
        "UPDATE records SET payload=? WHERE run_id=? AND seq=2", (json.dumps(payload), run_id)
    )
    assert not store.verify_record(run_id)["valid"]


def test_inbox_pagination_survives_restart(store):
    run_id = start(store)
    for number in range(4):
        store.push_event(run_id, {"type": "job_done", "number": number})
    first = store.inbox(run_id, limit=2)
    assert [event["number"] for event in first["events"]] == [0, 1]
    assert first["cursor"] == 2
    assert store.inbox_pending(run_id, first["cursor"]) == 2
    second = Store(store.data_dir)
    try:
        following = second.inbox(run_id, since=first["cursor"])
        assert [event["number"] for event in following["events"]] == [2, 3]
        assert following["cursor"] == 4
        assert second.inbox(run_id, since=4) == {"events": [], "cursor": 4}
        assert len({event["event_id"] for event in first["events"] + following["events"]}) == 4
        assert second.verify_record(run_id)["valid"]
    finally:
        second.close()


def test_inbox_source_delivery_is_once_across_connections_and_restart(store):
    run_id = start(store)
    second = Store(store.data_dir)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            delivered = list(
                pool.map(
                    lambda instance: instance.push_event_once(
                        run_id,
                        {"type": "message_received", "body": "actual inbound"},
                        source_key="late:source:event",
                    ),
                    [store, second],
                )
            )
        assert delivered[0] == delivered[1]
        assert store.inbox(run_id)["events"] == delivered[:1]
        store.append_record(run_id, "seal")
        store.update_run(run_id, state="sealed")
        before = store.records(run_id)
    finally:
        second.close()
    restarted = Store(store.data_dir)
    try:
        assert (
            restarted.push_event_once(
                run_id,
                {"type": "message_received", "body": "retry after lost ack"},
                source_key="late:source:event",
            )
            == delivered[0]
        )
        assert restarted.records(run_id) == before
        assert restarted.verify_record(run_id)["valid"]
    finally:
        restarted.close()


def test_inbox_once_rolls_back_dedupe_with_failed_record_append(store, monkeypatch):
    run_id = start(store)
    append = store._append_record

    def fail(*args, **kwargs):
        append(*args, **kwargs)
        raise OSError("simulated failed commit")

    monkeypatch.setattr(store, "_append_record", fail)
    with pytest.raises(OSError):
        store.push_event_once(run_id, {"type": "job_done"}, source_key="job:one")
    assert store.inbox(run_id)["events"] == []
    assert store.db.execute("SELECT COUNT(*) FROM inbox_sources").fetchone()[0] == 0
    assert store.verify_record(run_id)["valid"]
    monkeypatch.setattr(store, "_append_record", append)
    event = store.push_event_once(run_id, {"type": "job_done"}, source_key="job:one")
    assert event["cursor"] == 1
    assert store.verify_record(run_id)["valid"]


def test_run_identity_is_immutable_and_local_metadata_retained(store):
    config = {"run_id": "r_known", "budget": {"usd": 2}, "packs": ["deliver"]}
    run = store.create_run(config)
    config["packs"].append("watch")
    assert store.get_run(run["run_id"])["config"]["packs"] == ["deliver"]
    updated = store.update_run(run["run_id"], state="sealed", tainted=False, end={"reason": "done"})
    assert updated["state"] == "sealed"
    assert updated["end"] == {"reason": "done"}
    assert not store.debit(run["run_id"], "search", "0.1")
    assert store.reserve(run["run_id"], "0.1") is None
    with pytest.raises(ValueError):
        store.update_run(run["run_id"], config={})
    with pytest.raises(sqlite3.IntegrityError):
        store.create_run({"run_id": "r_known", "budget": {"usd": 2}})
    assert len(store.list_runs()) == 1


def test_paths_and_record_system_fields_cannot_be_supplied(store):
    for bad in ["../../escape", "r_../bad", "", "/tmp/run", "r_a/b"]:
        with pytest.raises(ValueError):
            store.run_dir(bad)
    run_id = start(store)
    with pytest.raises(ValueError):
        store.append_record(run_id, "action", seq=100)
    assert store.verify_record(run_id)["valid"]


def test_ledger_tampering_is_not_a_passing_reconciliation(store):
    run_id = start(store)
    assert store.debit(run_id, "search", "0.01")
    store.db.execute("UPDATE ledger SET spent_nano=1 WHERE run_id=?", (run_id,))
    assert not store.reconcile(run_id)["valid"]


def test_filesystem_permissions(store):
    run_id = start(store)
    assert store.data_dir.stat().st_mode & 0o777 == 0o700
    assert store.run_dir(run_id).stat().st_mode & 0o777 == 0o700
    assert (store.data_dir / "world.sqlite3").stat().st_mode & 0o777 == 0o600
    assert (store.run_dir(run_id) / "record.jsonl").stat().st_mode & 0o777 == 0o600
