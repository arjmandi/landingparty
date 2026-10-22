"""Passive, bounded historical workspaces at actual committed spending thresholds.

Only the ledger hook may capture a current workspace. Recovery records missed
observations, never a reconstruction from files that happen to exist now.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from .storage import NANODOLLARS, canonical_json, utc_now
from .workspaces import POLICY_ID as SNAPSHOT_POLICY_ID
from .workspaces import snapshot_manifest, snapshot_workspace, verify_import

POLICY_ID = "workspace-spend-checkpoints@1"
PERCENTAGES = (10, 25, 50, 100)
CAPTURE_POLICY = {
    "max_entries": 256,
    "max_bytes": 1_048_576,
    "max_seconds": 0.15,
    "child_timeout_seconds": 0.5,
    "semantics": "bounded observed workspace after first committed debit crossing; no interpolation",
}
_PATH = re.compile(r"work-checkpoints/c_[1-9][0-9]*_[a-f0-9]{16}\Z")
_OPEN_STATES = frozenset({"running", "frozen", "stopping"})


def _subject(config):
    return not (
        config.get("world_accounting")
        or config.get("private_archive")
        or config.get("measurement_of")
        or config.get("parent_run_id")
        or config.get("metering_lane", "harness") != "harness"
    )


def _crossings(records, cap):
    """Actual first crossing coordinates, using exact ledger nanodollars."""
    total = actions = 0
    crossed = {}
    for event in records:
        if event["kind"] == "action" and event.get("world_action", True):
            actions += 1
        if event["kind"] != "ledger_debit":
            continue
        amount = event.get("amount_nano")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ValueError("Invalid committed debit amount")
        total += amount
        for percent in PERCENTAGES:
            if percent not in crossed and total * 100 >= cap * percent:
                crossed[percent] = {
                    "fraction": percent / 100,
                    "usd": total / NANODOLLARS,
                    "actual_ledger_nano": total,
                    "world_actions": actions,
                    "trigger_record_seq": event["seq"],
                    "trigger_record_hash": event["hash"],
                }
    return crossed


def _capture_child(source, target):
    """Trusted fixed worker: never imports/executes code from the workspace."""
    started = utc_now()
    omissions = snapshot_workspace(
        source,
        target,
        limits={key: CAPTURE_POLICY[key] for key in ("max_entries", "max_bytes", "max_seconds")},
    )
    manifest = snapshot_manifest(target)
    try:
        verify_import(target, manifest)
    except ValueError:
        omissions.append({"path": ".", "reason": "unsafe_snapshot_links"})
    return {
        **manifest,
        "omissions": omissions,
        "status": "unmeasured" if omissions else "measured",
        "capture_started_at": started,
        "capture_finished_at": utc_now(),
        **({"reason": "capture_incomplete"} if omissions else {}),
    }


class WorkspaceCheckpointService:
    def __init__(self, world):
        self.world, self.store = world, world.store
        self._observed = {}
        self.recovery_errors = {}
        self.recover()

    def _capture(self, source, target):
        # A bounded traversal also has a separate process deadline: a slow file,
        # fsync, or malicious tree cannot turn the synchronous ledger hook into
        # an unbounded copy. No secrets or agent-controlled Python path are sent.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "TZ"}
        }
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            result = subprocess.run(
                [sys.executable, "-m", "worldd.checkpoints", "--capture", str(source), str(target)],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                check=True,
                timeout=CAPTURE_POLICY["child_timeout_seconds"],
            )
            captured = json.loads(result.stdout)
            if captured.get("policy_id") != SNAPSHOT_POLICY_ID:
                raise ValueError("Invalid capture policy")
            return captured
        except subprocess.TimeoutExpired:
            reason = "capture_process_timeout"
        except (OSError, ValueError, subprocess.SubprocessError):
            reason = "capture_failed"
        # A timed-out worker's orphan/staging directory is never treated as a
        # historical receipt, even if a rename happened before it was killed.
        return {
            "status": "unmeasured",
            "path": None,
            "policy_id": SNAPSHOT_POLICY_ID,
            "entries": {},
            "root_hash": None,
            "omissions": [{"path": ".", "reason": reason}],
            "reason": reason,
        }

    def recover(self):
        """Anchor missed pre-crash crossings without reading present work bytes."""
        for run in self.store.list_runs():
            try:
                self._observe(run["run_id"], None)
            except (OSError, ValueError, KeyError) as error:
                # Corruption cannot produce a historical proof or stop recovery
                # of other runs. The verifier still rejects the damaged source.
                self.recovery_errors[run["run_id"]] = type(error).__name__

    def observe_debit(self, run_id, debit_record):
        """Called synchronously after an actual committed debit under Store lock."""
        return self._observe(run_id, debit_record)

    def _observe(self, run_id, debit_record):
        with self.store._lock:
            run = self.store.get_run(run_id)
            if run["state"] not in _OPEN_STATES or not _subject(run["config"]):
                return []
            row = self.store._row(run_id)
            cap = row["cap_nano"]
            if cap <= 0:
                return []
            root = self.store.run_dir(run_id)
            source = root / "work"
            try:
                if not stat.S_ISDIR(source.lstat().st_mode):
                    return []
            except FileNotFoundError:
                return []
            observed = self._observed.get(run_id, set())
            if all(p in observed or row["spent_nano"] * 100 < cap * p for p in PERCENTAGES):
                return []
            records = self.store.records(run_id)
            if any(event["kind"] == "seal" for event in records):
                return []
            if (
                not self.store.verify_record(run_id)["valid"]
                or not self.store.reconcile(run_id)["valid"]
            ):
                raise ValueError("Workspace checkpoints require a valid source chain and ledger")
            if debit_record is not None and not any(
                event == debit_record and event["kind"] == "ledger_debit" for event in records
            ):
                raise ValueError("Checkpoint trigger is not an actual committed debit")
            crossed = _crossings(records, cap)
            existing = [event for event in records if event["kind"] == "workspace_checkpoint"]
            observed = set()
            for event in existing:
                percent = self._validate_coordinates(event, crossed)
                if percent in observed:
                    raise ValueError("Duplicate spending checkpoint receipt")
                observed.add(percent)
            pending = defaultdict(list)
            for percent, coordinates in crossed.items():
                if percent not in observed:
                    pending[coordinates["trigger_record_seq"]].append(coordinates)
            last_debit = next(
                (event for event in reversed(records) if event["kind"] == "ledger_debit"), None
            )
            written = []
            for coordinates in pending.values():
                trigger = coordinates[0]
                capture = {
                    "status": "unmeasured",
                    "path": None,
                    "policy_id": SNAPSHOT_POLICY_ID,
                    "entries": {},
                    "root_hash": None,
                    "omissions": [],
                    "reason": "missed_ledger_observation",
                }
                if (
                    debit_record is not None
                    and debit_record == last_debit
                    and trigger["trigger_record_hash"] == debit_record["hash"]
                    and getattr(self.store, "_ledger_observation", None)
                    == (run_id, debit_record["seq"], debit_record["hash"])
                ):
                    try:
                        parent = root / "work-checkpoints"
                        parent.mkdir(mode=0o700, exist_ok=True)
                        if parent.is_symlink():
                            raise ValueError("Private checkpoint directory cannot be a symlink")
                        relative = (
                            f"work-checkpoints/c_{debit_record['seq']}_{debit_record['hash'][7:23]}"
                        )
                        capture = {"path": relative, **self._capture(source, root / relative)}
                    except (OSError, ValueError):
                        capture.update(
                            reason="capture_storage_unavailable",
                            omissions=[{"path": ".", "reason": "capture_storage_unavailable"}],
                        )
                # One atomic group for a jumped debit: all fractions share the
                # same observed state and exact dollar/action coordinates.
                with self.store._transaction():
                    for point in coordinates:
                        written.append(
                            self.store._append_record(
                                run_id,
                                "workspace_checkpoint",
                                **point,
                                **capture,
                                checkpoint_policy_id=POLICY_ID,
                                capture_policy=CAPTURE_POLICY,
                            )
                        )
                        observed.add(round(point["fraction"] * 100))
            self._observed[run_id] = observed
            return written

    @staticmethod
    def _validate_coordinates(event, crossed):
        fraction = event.get("fraction")
        if isinstance(fraction, bool) or fraction not in (0.1, 0.25, 0.5, 1.0):
            raise ValueError("Invalid spending checkpoint fraction")
        percent = round(fraction * 100)
        expected = crossed.get(percent)
        if (
            not expected
            or any(event.get(key) != value for key, value in expected.items())
            or event.get("checkpoint_policy_id") != POLICY_ID
            or event.get("capture_policy") != CAPTURE_POLICY
            or event.get("policy_id") != SNAPSHOT_POLICY_ID
            or event["seq"] <= expected["trigger_record_seq"]
        ):
            raise ValueError("Checkpoint does not match its actual first ledger crossing")
        return percent

    def verify_checkpoint(self, run_id, event):
        """Verify an actual receipt and exact private bytes before offline grading."""
        with self.store._lock:
            records = self.store.records(run_id)
            if (
                not isinstance(event, dict)
                or event.get("kind") != "workspace_checkpoint"
                or event.get("run_id") != run_id
                or event not in records
                or event.get("status") != "measured"
                or event.get("omissions") != []
                or not _subject(self.store.get_run(run_id)["config"])
            ):
                raise ValueError("An actual measured checkpoint receipt is required")
            if (
                not self.store.verify_record(run_id)["valid"]
                or not self.store.reconcile(run_id)["valid"]
            ):
                raise ValueError("Checkpoint source chain or ledger failed verification")
            cap = self.store._row(run_id)["cap_nano"]
            if cap <= 0:
                raise ValueError("A positive actual run cap is required")
            crossed = _crossings(records, cap)
            self._validate_coordinates(event, crossed)
            if sum(
                other["kind"] == "workspace_checkpoint"
                and other.get("fraction") == event["fraction"]
                for other in records
            ) != 1 or any(
                other["kind"] == "seal" and other["seq"] < event["seq"] for other in records
            ):
                raise ValueError("Checkpoint receipt is duplicated or follows the source seal")
            if not isinstance(event.get("path"), str) or not _PATH.fullmatch(event["path"]):
                raise ValueError("Checkpoint path must be a scoped private snapshot")
            expected_path = f"work-checkpoints/c_{event['trigger_record_seq']}_{event['trigger_record_hash'][7:23]}"
            if event["path"] != expected_path:
                raise ValueError("Checkpoint path differs from its actual debit identity")
            return verify_import(self.store.run_dir(run_id) / event["path"], event)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--capture":
        raise SystemExit("Only the fixed trusted checkpoint capture worker is supported")
    print(canonical_json(_capture_child(Path(sys.argv[2]), Path(sys.argv[3]))))
