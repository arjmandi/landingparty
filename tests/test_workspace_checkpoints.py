"""Historical curves require actual committed spending and captured workspace bytes."""

import os
import time
from types import SimpleNamespace

import pytest

from worldd.checkpoints import CAPTURE_POLICY, WorkspaceCheckpointService
from worldd.storage import Store
from worldd.workspaces import snapshot_manifest, snapshot_workspace


@pytest.fixture
def subject(tmp_path):
    store = Store(tmp_path)
    run_id = store.create_run({"budget": {"usd": 1}})["run_id"]
    work = store.run_dir(run_id) / "work"
    work.mkdir()
    world = SimpleNamespace(store=store)
    service = WorkspaceCheckpointService(world)
    store.ledger_observer = service.observe_debit
    yield store, run_id, work, service
    store.close()


def receipts(store, run_id):
    return [row for row in store.records(run_id) if row["kind"] == "workspace_checkpoint"]


def test_actual_workspace_changes_are_preserved_at_each_committed_threshold(subject):
    store, run_id, work, service = subject
    store.append_record(run_id, "action", action_id="internal", world_action=False)
    for index, amount in enumerate((0.1, 0.15, 0.25, 0.5), 1):
        store.append_record(run_id, "action", action_id=f"a_{index}")
        (work / "answer.txt").write_text(f"actual version {index}")
        assert store.debit(run_id, "harness", amount)
    rows = receipts(store, run_id)
    assert [row["fraction"] for row in rows] == [0.1, 0.25, 0.5, 1.0]
    assert [row["actual_ledger_nano"] for row in rows] == [
        100000000,
        250000000,
        500000000,
        1000000000,
    ]
    assert [row["world_actions"] for row in rows] == [1, 2, 3, 4]
    for index, row in enumerate(rows, 1):
        assert row["status"] == "measured"
        assert (
            store.run_dir(run_id) / row["path"] / "answer.txt"
        ).read_text() == f"actual version {index}"
        assert service.verify_checkpoint(run_id, row)["root_hash"] == row["root_hash"]
    assert store.total_spent(run_id) == 1
    assert not store.get_run(run_id).get("tainted")
    assert store.verify_record(run_id)["valid"] and store.reconcile(run_id)["valid"]


def test_jump_uses_one_snapshot_and_identical_actual_coordinates(subject, monkeypatch):
    store, run_id, work, service = subject
    (work / "answer").write_text("jumped actual state")
    calls = []
    original = service._capture

    def capture(source, target):
        calls.append(target)
        return original(source, target)

    monkeypatch.setattr(service, "_capture", capture)
    store.append_record(run_id, "action", action_id="a_one")
    assert store.debit(run_id, "harness", 0.6)
    rows = receipts(store, run_id)
    assert len(calls) == 1 and len(rows) == 3
    assert {row["usd"] for row in rows} == {0.6}
    assert {row["world_actions"] for row in rows} == {1}
    assert len({row["path"] for row in rows}) == len({row["root_hash"] for row in rows}) == 1
    (work / "answer").write_text("future state")
    assert store.debit(run_id, "harness", 0.4)
    assert len(calls) == 2 and len(receipts(store, run_id)) == 4
    assert (calls[0] / "answer").read_text() == "jumped actual state"


def test_recovery_marks_missed_thresholds_without_copying_current_work(tmp_path):
    store = Store(tmp_path)
    run_id = store.create_run({"budget": {"usd": 1}})["run_id"]
    work = store.run_dir(run_id) / "work"
    work.mkdir()
    (work / "answer").write_text("old state never observed")
    store.debit(run_id, "harness", 0.3)  # crash after billing, before a hook was installed
    (work / "answer").write_text("current state cannot substitute for history")
    store.close()
    reopened = Store(tmp_path)
    try:
        service = WorkspaceCheckpointService(SimpleNamespace(store=reopened))
        rows = receipts(reopened, run_id)
        assert len(rows) == 2 and all(row["status"] == "unmeasured" for row in rows)
        assert all(
            row["path"] is None and row["reason"] == "missed_ledger_observation" for row in rows
        )
        assert not (reopened.run_dir(run_id) / "work-checkpoints").exists()
        service.recover()
        assert receipts(reopened, run_id) == rows
        reopened.ledger_observer = service.observe_debit
        reopened.debit(run_id, "harness", 0.2)
        measured = receipts(reopened, run_id)[-1]
        assert measured["fraction"] == 0.5 and measured["status"] == "measured"
        assert (
            (reopened.run_dir(run_id) / measured["path"] / "answer")
            .read_text()
            .startswith("current")
        )
    finally:
        reopened.close()


@pytest.mark.parametrize("state", ["running", "frozen", "stopping"])
def test_committed_settlements_capture_before_seal_even_during_drain(subject, state):
    store, run_id, work, _ = subject
    (work / "answer").write_text("state at actual settlement")
    reservation = store.reserve(run_id, 1)
    store.update_run(run_id, state=state)
    assert store.settle(reservation, "harness", 1)
    rows = receipts(store, run_id)
    assert len(rows) == 4 and all(row["status"] == "measured" for row in rows)


def test_no_checkpoint_writes_after_seal_or_for_preflight(subject):
    store, run_id, work, service = subject
    (work / "answer").write_text("actual")
    reservation = store.reserve(run_id, 0.5)
    store.update_run(run_id, state="preflight")
    assert store.settle(reservation, "harness", 0.5)
    assert receipts(store, run_id) == []
    store.append_record(run_id, "seal")
    before = store.records(run_id)
    store.update_run(run_id, state="stopping")
    service.recover()
    assert store.records(run_id) == before


@pytest.mark.parametrize(
    "flags",
    [
        {"private_archive": True},
        {"world_accounting": True},
        {"metering_lane": "kernel"},
        {"measurement_of": "r_parent"},
        {"parent_run_id": "r_parent"},
    ],
)
def test_private_world_and_measurement_charges_never_create_agent_checkpoints(tmp_path, flags):
    store = Store(tmp_path)
    try:
        run_id = store.create_run({"budget": {"usd": 1}, **flags})["run_id"]
        (store.run_dir(run_id) / "work").mkdir()
        service = WorkspaceCheckpointService(SimpleNamespace(store=store))
        store.ledger_observer = service.observe_debit
        assert store.debit(run_id, "perception", 1)
        assert receipts(store, run_id) == []
    finally:
        store.close()


def test_absent_and_symlink_workspaces_do_not_follow_private_sources(tmp_path):
    store = Store(tmp_path)
    try:
        run_id = store.create_run({"budget": {"usd": 1}})["run_id"]
        service = WorkspaceCheckpointService(SimpleNamespace(store=store))
        store.ledger_observer = service.observe_debit
        assert store.debit(run_id, "harness", 0.3)
        private = tmp_path / "private"
        private.mkdir()
        (private / "secret").write_text("never captured")
        (store.run_dir(run_id) / "work").symlink_to(private, target_is_directory=True)
        assert store.debit(run_id, "harness", 0.7)
        assert receipts(store, run_id) == []
    finally:
        store.close()


def test_tampered_bytes_forged_receipt_and_invalid_source_chain_fail_closed(subject):
    store, run_id, work, service = subject
    (work / "answer").write_text("actual")
    store.debit(run_id, "harness", 0.1)
    row = receipts(store, run_id)[0]
    with pytest.raises(ValueError, match="actual measured"):
        service.verify_checkpoint(run_id, {**row, "usd": 0.09})
    (store.run_dir(run_id) / row["path"] / "answer").write_text("tampered")
    with pytest.raises(ValueError, match="content manifest"):
        service.verify_checkpoint(run_id, row)
    (store.run_dir(run_id) / "record.jsonl").write_text("")
    with pytest.raises(ValueError, match="source chain"):
        service.verify_checkpoint(run_id, row)


def test_fifo_and_escaping_links_are_explicit_unmeasured_without_external_reads(subject):
    store, run_id, work, service = subject
    os.mkfifo(work / "pipe")
    (work / "private").symlink_to("../../private")
    store.debit(run_id, "harness", 0.1)
    row = receipts(store, run_id)[0]
    assert row["status"] == "unmeasured" and row["reason"] == "capture_incomplete"
    assert {item["reason"] for item in row["omissions"]} == {
        "special_file",
        "unsafe_snapshot_links",
    }
    assert row["entries"] == {"private": {"type": "symlink", "target": "../../private"}}
    with pytest.raises(ValueError, match="actual measured"):
        service.verify_checkpoint(run_id, row)
    assert not store.get_run(run_id).get("tainted")


@pytest.mark.parametrize("exhaust", ["entries", "bytes"])
def test_large_worktree_is_bounded_and_is_not_presented_as_measured(subject, exhaust):
    store, run_id, work, _ = subject
    if exhaust == "entries":
        for index in range(CAPTURE_POLICY["max_entries"] + 1):
            (work / f"file{index}").touch()
    else:
        with (work / "large").open("wb") as output:
            output.truncate(CAPTURE_POLICY["max_bytes"] + 1)
    store.debit(run_id, "harness", 0.1)
    row = receipts(store, run_id)[0]
    assert row["status"] == "unmeasured"
    assert any(
        item["reason"] == f"capture_{'entry' if exhaust == 'entries' else 'byte'}_limit"
        for item in row["omissions"]
    )
    assert not store.get_run(run_id).get("tainted")


def test_worker_timeout_is_actual_bounded_failure_and_threshold_is_not_retried(
    subject, monkeypatch, tmp_path
):
    from worldd import checkpoints

    store, run_id, work, _ = subject
    (work / "answer").write_text("actual")
    sleeper = tmp_path / "test-python"
    sleeper.write_text("#!/bin/sh\nexec /bin/sleep 3\n")
    sleeper.chmod(0o700)
    monkeypatch.setattr(checkpoints.sys, "executable", str(sleeper))
    started = time.monotonic()
    assert store.debit(run_id, "harness", 0.1)
    assert time.monotonic() - started < 1.5
    row = receipts(store, run_id)[0]
    assert row["status"] == "unmeasured" and row["reason"] == "capture_process_timeout"
    assert row["path"] is None
    assert store.debit(run_id, "harness", 0.01)
    assert receipts(store, run_id) == [row]


def test_observer_only_accepts_the_real_committed_debit(subject):
    store, run_id, work, service = subject
    (work / "answer").write_text("actual")
    store.ledger_observer = None
    store.debit(run_id, "harness", 0.1)
    debit = next(row for row in store.records(run_id) if row["kind"] == "ledger_debit")
    with pytest.raises(ValueError, match="actual committed"):
        service.observe_debit(run_id, {**debit, "amount_nano": 999})
    assert receipts(store, run_id) == []


def test_bounded_fd_snapshot_keeps_default_final_snapshot_behavior(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "actual").write_bytes(b"12345")
    limits = {"max_entries": 3, "max_bytes": 4, "max_seconds": 1}
    partial = tmp_path / "partial"
    assert snapshot_workspace(source, partial, limits=limits) == [
        {"path": ".", "reason": "capture_byte_limit"}
    ]
    assert snapshot_manifest(partial)["entries"] == {}
    final = tmp_path / "final"
    assert snapshot_workspace(source, final) == []
    assert (final / "actual").read_bytes() == b"12345"


def test_crash_between_group_receipts_rolls_back_all_fractions_and_orphans_are_not_history(
    subject, monkeypatch
):
    store, run_id, work, service = subject
    (work / "answer").write_text("first state")
    original = store._append_record

    def fail_second_fraction(run_id, kind, **fields):
        if kind == "workspace_checkpoint" and fields["fraction"] == 0.25:
            raise OSError("simulated crash before receipt group committed")
        return original(run_id, kind, **fields)

    monkeypatch.setattr(store, "_append_record", fail_second_fraction)
    assert store.debit(run_id, "harness", 0.6)
    assert receipts(store, run_id) == []
    assert any(row["kind"] == "checkpoint_capture_error" for row in store.records(run_id))
    assert list((store.run_dir(run_id) / "work-checkpoints").iterdir())
    monkeypatch.setattr(store, "_append_record", original)
    (work / "answer").write_text("later state")
    restored = WorkspaceCheckpointService(SimpleNamespace(store=store))
    rows = receipts(store, run_id)
    assert len(rows) == 3 and all(row["status"] == "unmeasured" for row in rows)
    assert all(row["path"] is None for row in rows)
    store.ledger_observer = restored.observe_debit
    assert store.debit(run_id, "harness", 0.4)
    final = receipts(store, run_id)[-1]
    assert final["status"] == "measured"
    assert (store.run_dir(run_id) / final["path"] / "answer").read_text() == "later state"
    assert service.verify_checkpoint(run_id, final)["root_hash"] == final["root_hash"]


def test_worker_environment_never_receives_world_provider_or_proxy_secrets(subject, monkeypatch):
    from worldd import checkpoints

    store, run_id, work, _ = subject
    (work / "answer").write_text("actual")
    monkeypatch.setenv("WORLD_RESEARCHER_TOKEN", "fixture-private-control")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-provider-key")
    monkeypatch.setenv("HTTPS_PROXY", "https://fixture:private@proxy.example")
    original = checkpoints.subprocess.run
    observed = []

    def run(argv, **kwargs):
        observed.append(set(kwargs["env"]))
        return original(argv, **kwargs)

    monkeypatch.setattr(checkpoints.subprocess, "run", run)
    assert store.debit(run_id, "harness", 0.1)
    assert receipts(store, run_id)[0]["status"] == "measured"
    assert observed and observed[0] <= {"PATH", "LANG", "LC_ALL", "TZ", "PYTHONDONTWRITEBYTECODE"}


def test_replaying_a_real_debit_outside_its_actual_hook_cannot_capture_present_work(subject):
    store, run_id, work, service = subject
    store.ledger_observer = None
    (work / "answer").write_text("past state")
    store.debit(run_id, "harness", 0.1)
    debit = next(row for row in store.records(run_id) if row["kind"] == "ledger_debit")
    (work / "answer").write_text("future state")
    service.observe_debit(run_id, debit)
    row = receipts(store, run_id)[0]
    assert row["status"] == "unmeasured" and row["path"] is None
    assert row["reason"] == "missed_ledger_observation"
    assert not (store.run_dir(run_id) / "work-checkpoints").exists()


def test_unavailable_private_snapshot_storage_is_explicit_unmeasured(subject, tmp_path):
    store, run_id, work, _ = subject
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (store.run_dir(run_id) / "work-checkpoints").symlink_to(outside, target_is_directory=True)
    (work / "answer").write_text("actual")
    assert store.debit(run_id, "harness", 0.1)
    row = receipts(store, run_id)[0]
    assert row["status"] == "unmeasured" and row["reason"] == "capture_storage_unavailable"
    assert row["path"] is None and list(outside.iterdir()) == []


def test_source_mutation_during_copy_is_an_explicit_omission(tmp_path, monkeypatch):
    from worldd import workspaces

    work, target = tmp_path / "work", tmp_path / "snapshot"
    work.mkdir()
    source = work / "changing"
    source.write_text("old state")
    original = workspaces.os.read
    changed = False

    def racing_read(descriptor, size):
        nonlocal changed
        block = original(descriptor, size)
        if block and not changed:
            changed = True
            source.write_text("new state, concurrently written")
        return block

    monkeypatch.setattr(workspaces.os, "read", racing_read)
    assert snapshot_workspace(work, target) == [
        {"path": "changing", "reason": "file_changed_during_capture"}
    ]
    assert (target / "changing").read_text() == "old state"
