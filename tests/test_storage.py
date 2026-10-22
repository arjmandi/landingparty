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
