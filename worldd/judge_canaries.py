"""Explicitly authorized, durable scheduled checks of a frozen judge pair.

Canary agreement measures stability, never expert calibration or correctness.
Interrupted purchases keep their full authorization reserved and are not retried.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .util import digest, identifier

POLICY_ID = "scheduled-judge-pair-swapped-budget-reservations@1"


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _time(value):
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise ValueError("Judge-canary time must include an explicit timezone")
    return instant.astimezone(UTC)


def _stamp(value):
    return _time(value).isoformat(timespec="microseconds")


def _nano(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= 10000
    ):
        raise ValueError(
            "Judge-canary budgets must be explicit positive finite dollars up to 10000"
        )
    scaled = Decimal(str(value)) * 1_000_000_000
    if scaled != scaled.to_integral_value():
        raise ValueError("Judge-canary budgets require whole nanodollars")
    return int(scaled)


class _TrackedModel:
    def __init__(self, model, world):
        self.model, self.world, self.tasks = model, world, set()

    async def complete(self, *args, **kwargs):
        if getattr(self.world, "closing", False):
            raise RuntimeError("World is shutting down")
        task = asyncio.create_task(self.model.complete(*args, **kwargs))
        self.tasks.add(task)
        return await task

    async def drain(self):
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


class JudgeCanaryService:
    def __init__(self, world, model=None, clock=None, poll_seconds=1):
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or not math.isfinite(poll_seconds)
            or not 0.05 <= poll_seconds <= 3600
        ):
            raise ValueError("Judge-canary polling interval must be finite and bounded")
        self.world, self.store, self.model = world, world.store, model
        self._owns_model = False
        self.clock = clock or (lambda: datetime.now(UTC))
        self.poll_seconds = poll_seconds
        path = self.store.data_dir / "judge-canaries.sqlite3"
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS judge_canary_schedules (
                schedule_id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS judge_canary_checks (
                check_id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
        """)
        path.chmod(0o600)
        self.lock = threading.RLock()
        self._worker, self._closing, self._closed = None, False, False
        self._tasks, self._recovering = set(), set()

    def _protocol(self, source_run_id, descriptor):
        from analysis.instruments import load_artifact
        from analysis.judges import JudgeSpec, ToollessJudge
        from analysis.pipeline import _comparison_artifact

        source = self.store.get_run(source_run_id)
        if (
            not self.store.reconcile(source_run_id)["valid"]
            or self.store.reconcile(source_run_id).get("reserved_usd") != 0
        ):
            raise ValueError("Judge-canary source ledger is not reconciled")
        raw, provenance = load_artifact(self.store, source_run_id, descriptor)
        protocol = json.loads(raw)
        if (
            not isinstance(protocol, dict)
            or not isinstance(protocol.get("protocol_id"), str)
            or not protocol["protocol_id"]
        ):
            raise ValueError("Judge canaries require a frozen named protocol")
        spec = JudgeSpec(**protocol["model_spec"])
        agent_family = protocol.get("agent_family")
        if not isinstance(agent_family, str) or not agent_family.strip():
            raise ValueError("Judge canaries require the source's declared model family")
        # Constructor validates cross-family before a schedule or paid call exists.
        ToollessJudge(None, spec, agent_family=agent_family, budget_usd=1)
        if not isinstance(protocol.get("criterion"), str) or not protocol["criterion"]:
            raise ValueError("Judge canaries require a frozen comparison criterion")
        if protocol.get("expected_winner") not in {None, "left", "right", "tie"}:
            raise ValueError("Expected canary winner must be left, right or tie")
        limit = protocol.get("max_chars", 24000)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000_000:
            raise ValueError("Judge-canary length control requires a bounded character limit")
        tokens = protocol.get("max_tokens", 2048)
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 1 <= tokens <= 32768:
            raise ValueError("Judge-canary max_tokens must lie between one and 32768")
        if not all(
            isinstance(protocol.get(side + "_artifact"), dict)
            and protocol[side + "_artifact"].get("path", "").startswith("work-snapshot/")
            for side in ("left", "right")
        ):
            raise ValueError("Judge-canary pairs must cite actual sealed workspace artifacts")
        left, left_ref = _comparison_artifact(self.store, source_run_id, protocol, "left")
        right, right_ref = _comparison_artifact(self.store, source_run_id, protocol, "right")
        if not left or not right:
            raise ValueError("Judge-canary artifacts must be nonempty")
        calibration = None
        if protocol.get("calibration_ref") is not None:
            _, calibration = load_artifact(self.store, source_run_id, protocol["calibration_ref"])
        return (
            protocol,
            spec,
            left,
            right,
            {
                "instrument": provenance,
                "left": left_ref,
                "right": right_ref,
                "calibration_artifact": calibration,
                "source_record_head": self.store.records(source["run_id"])[-1]["hash"],
            },
        )

    def register(self, source_run_id, config):
        if not isinstance(config, dict):
            raise TypeError("Judge-canary configuration must be an object")
        config = json.loads(_json(config))
        self._protocol(source_run_id, config["instrument"])
        per_check, lifetime = (
            _nano(config["per_check_budget_usd"]),
            _nano(config["lifetime_budget_usd"]),
        )
        interval, maximum = config["interval_s"], config["max_checks"]
        if per_check < 2 or lifetime < per_check:
            raise ValueError("Judge-canary lifetime budget must cover at least one swapped pair")
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or not 1 <= interval <= 366 * 86400
        ):
            raise ValueError("Judge-canary interval must be explicit and bounded")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 10000:
            raise ValueError("Judge-canary max_checks must be a positive bounded integer")
        enabled = config.get("enabled", False)
        if not isinstance(enabled, bool):
            raise TypeError("Judge-canary enabled must be boolean")
        frozen = {
            "instrument": config["instrument"],
            "per_check_budget_usd": config["per_check_budget_usd"],
            "lifetime_budget_usd": config["lifetime_budget_usd"],
            "interval_s": interval,
            "max_checks": maximum,
        }
        schedule_id = (
            "jc_" + digest({"source_run_id": source_run_id, "config": frozen}).split(":", 1)[1][:32]
        )
        schedule = {
            "schedule_id": schedule_id,
            "source_run_id": source_run_id,
            "config": frozen,
            "policy_id": POLICY_ID,
            "created_at": _stamp(self.clock()),
            "enabled": enabled,
            "next_due": _stamp(config.get("first_due") or self.clock()),
            "attempts": 0,
            "spent_nano": 0,
            "held_nano": 0,
            "active_check": None,
            "last_check_at": None,
            "last_attempt_at": None,
            "last_result": None,
        }
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO judge_canary_schedules VALUES(?,?,?)",
                (schedule_id, source_run_id, _json(schedule)),
            )
        return self.get(schedule_id)

    configure = register

    def _read(self, schedule_id):
        row = self.db.execute(
            "SELECT payload FROM judge_canary_schedules WHERE schedule_id=?", (schedule_id,)
        ).fetchone()
        if row is None:
            raise KeyError(schedule_id)
        return json.loads(row[0])

    def _save(self, schedule):
        self.db.execute(
            "UPDATE judge_canary_schedules SET payload=? WHERE schedule_id=?",
            (_json(schedule), schedule["schedule_id"]),
        )

    def get(self, schedule_id):
        with self.lock:
            value = self._read(schedule_id)
        return {
            **value,
            "spent_usd": value["spent_nano"] / 1e9,
            "held_usd": value["held_nano"] / 1e9,
            "remaining_authorized_usd": (
                _nano(value["config"]["lifetime_budget_usd"])
                - value["spent_nano"]
                - value["held_nano"]
            )
            / 1e9,
            "model_configured": self.model is not None,
        }

    def list(self, source_run_id=None):
        with self.lock:
            rows = self.db.execute(
                "SELECT schedule_id FROM judge_canary_schedules"
                + (" WHERE source_run_id=?" if source_run_id else "")
                + " ORDER BY schedule_id",
                (source_run_id,) if source_run_id else (),
            ).fetchall()
        return [self.get(row[0]) for row in rows]

    def set_enabled(self, schedule_id, enabled):
        if not isinstance(enabled, bool):
            raise TypeError("Judge-canary enabled must be boolean")
        with self.lock:
            value = self._read(schedule_id)
            value["enabled"] = enabled
            self._save(value)
        return self.get(schedule_id)

    def _claim(self, schedule_id):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                schedule = self._read(schedule_id)
                if (
                    not schedule["enabled"]
                    or schedule["active_check"]
                    or _time(schedule["next_due"]) > _time(self.clock())
                ):
                    self.db.execute("COMMIT")
                    return None
                cap = _nano(schedule["config"]["per_check_budget_usd"])
                if schedule["attempts"] >= schedule["config"]["max_checks"] or schedule[
                    "spent_nano"
                ] + schedule["held_nano"] + cap > _nano(schedule["config"]["lifetime_budget_usd"]):
                    schedule.update(enabled=False, stop_reason="authorized_limit_reached")
                    self._save(schedule)
                    self.db.execute("COMMIT")
                    return None
                check_id, stamp = identifier("judgecheck"), _stamp(self.clock())
                check = {
                    "check_id": check_id,
                    "schedule_id": schedule_id,
                    "source_run_id": schedule["source_run_id"],
                    "status": "pending",
                    "claimed_at": stamp,
                    "budget_ceiling_nano": cap,
                    "known_child_ids": [row["run_id"] for row in self.store.list_runs()],
                    "previous_winner": (schedule.get("last_result") or {}).get("winner"),
                }
                schedule.update(
                    active_check=check_id,
                    held_nano=schedule["held_nano"] + cap,
                    attempts=schedule["attempts"] + 1,
                    last_attempt_at=stamp,
                    next_due=_stamp(
                        _time(self.clock()) + timedelta(seconds=schedule["config"]["interval_s"])
                    ),
                )
                self._save(schedule)
                self.db.execute(
                    "INSERT INTO judge_canary_checks VALUES(?,?,?,?)",
                    (check_id, schedule_id, "pending", _json(check)),
                )
                self.db.execute("COMMIT")
                return check
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _finish(self, check, report=None, error=None, failure_artifact=None):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                schedule = self._read(check["schedule_id"])
                current = self.db.execute(
                    "SELECT status FROM judge_canary_checks WHERE check_id=?", (check["check_id"],)
                ).fetchone()
                if current[0] != "pending":
                    self.db.execute("COMMIT")
                    return
                check.update(
                    status="measured"
                    if report
                    else "interrupted"
                    if error == "interrupted"
                    else "failed",
                    completed_at=_stamp(self.clock()),
                )
                if report:
                    actual = report["spent_nano"]
                    schedule["held_nano"] -= check["budget_ceiling_nano"]
                    schedule["spent_nano"] += actual
                    schedule.update(
                        last_check_at=report["observed_at"],
                        last_result={
                            key: report[key]
                            for key in (
                                "winner",
                                "position_disagreement",
                                "drift_from_previous",
                                "drift_from_expected",
                                "observed_at",
                                "check_id",
                            )
                        },
                    )
                    check["artifact"] = report["artifact"]
                else:
                    check["error"] = error
                    check["budget_status"] = "full_ceiling_retained_no_automatic_retry"
                    if failure_artifact is not None:
                        check["artifact"] = failure_artifact
                if schedule["active_check"] == check["check_id"]:
                    schedule["active_check"] = None
                self._save(schedule)
                self.db.execute(
                    "UPDATE judge_canary_checks SET status=?,payload=? WHERE check_id=?",
                    (check["status"], _json(check), check["check_id"]),
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _artifact(self, check, payload):
        relative = f"analysis/judge-canaries/{check['check_id']}.failed.json"
        target = self.store.run_dir(check["source_run_id"]) / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        body = _json(payload).encode()
        target.write_bytes(body)
        target.chmod(0o400)
        return {"path": relative, "hash": digest(body)}

    async def _execute(self, check):
        from analysis.instruments import load_artifact
        from analysis.judges import ToollessJudge
        from analysis.values import _receipt

        tracker = _TrackedModel(self.model, self.world)
        try:
            schedule = self.get(check["schedule_id"])
            protocol, spec, left, right, provenance = self._protocol(
                check["source_run_id"], schedule["config"]["instrument"]
            )
            pair_budget = 2 * (check["budget_ceiling_nano"] // 2) / 1e9
            judge = ToollessJudge(
                tracker,
                spec,
                agent_family=protocol["agent_family"],
                budget_usd=pair_budget,
                max_tokens=protocol.get("max_tokens", 2048),
            )
            result = await judge.pairwise(
                check["source_run_id"],
                left,
                right,
                max_chars=protocol.get("max_chars", 24000),
                criterion=protocol["criterion"],
            )
            await tracker.drain()
            seen = set(check["known_child_ids"])
            spent_nano = 0
            for judgment in result["judgments"]:
                _receipt(self.store, check["source_run_id"], spec, judgment, seen=seen)
                child = judgment["measurement"]["measurement_run_id"]
                spent = sum(self.store.spend(child).values())
                if spent > pair_budget / 2 + 1e-9:
                    raise ValueError("Judge canary exceeded its registered per-call ceiling")
                spent_nano += sum(
                    row.get("amount_nano", 0)
                    for row in self.store.records(child)
                    if row["kind"] == "ledger_debit"
                )
                judgment["measurement"]["spent_usd"] = spent
            if spent_nano > check["budget_ceiling_nano"]:
                raise ValueError("Judge canary exceeded its total check ceiling")
            winner, expected = result["winner"], protocol.get("expected_winner")
            report = {
                **result,
                "check_id": check["check_id"],
                "schedule_id": check["schedule_id"],
                "policy_id": POLICY_ID,
                "source_run_id": check["source_run_id"],
                "protocol_id": protocol["protocol_id"],
                "provenance": provenance,
                "claimed_at": check["claimed_at"],
                "observed_at": _stamp(self.clock()),
                "position_disagreement": winner == "disagreement",
                "expected_winner": expected,
                "drift_from_expected": winner != expected if expected is not None else None,
                "drift_from_previous": winner != check["previous_winner"]
                if check["previous_winner"] is not None
                else None,
                "spent_usd": spent_nano / 1e9,
                "spent_nano": spent_nano,
                "column": "J",
                "calibration_inferred": False,
                "scientific_gate_evidence": False,
            }
            relative = f"analysis/judge-canaries/{check['check_id']}.json"
            target = self.store.run_dir(check["source_run_id"]) / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            body = _json(report).encode()
            target.write_bytes(body)
            target.chmod(0o400)
            report["artifact"] = {"path": relative, "hash": digest(body)}
            # Read our frozen receipt before releasing unused authorization.
            load_artifact(self.store, check["source_run_id"], report["artifact"])
            self._finish(check, report=report)
            return report
        except asyncio.CancelledError:
            await tracker.drain()
            self._finish(check, error="interrupted")
            raise
        except Exception as error:  # noqa: BLE001 - any external adapter failure must retain authorization.
            await tracker.drain()
            failure = {
                "check_id": check["check_id"],
                "source_run_id": check["source_run_id"],
                "claimed_at": check["claimed_at"],
                "completed_at": _stamp(self.clock()),
                "status": "unmeasured",
                "reason": type(error).__name__,
                "metering_verification": "not_established",
                "completions": [
                    task.result()
                    for task in tracker.tasks
                    if not task.cancelled() and task.exception() is None
                ],
            }
            artifact = self._artifact(check, failure)
            self._finish(check, error=type(error).__name__, failure_artifact=artifact)
            return {
                "check_id": check["check_id"],
                "status": "unmeasured",
                "reason": type(error).__name__,
                "artifact": artifact,
            }

    async def run_due(self):
        if self.model is None or self._closing or getattr(self.world, "closing", False):
            return []
        self._recover_pending()
        reports = []
        for schedule in self.list():
            if (
                not schedule["enabled"]
                or schedule["active_check"]
                or _time(schedule["next_due"]) > _time(self.clock())
            ):
                continue
            try:
                self._protocol(schedule["source_run_id"], schedule["config"]["instrument"])
            except (KeyError, ValueError, TypeError, OSError) as error:
                with self.lock:
                    value = self._read(schedule["schedule_id"])
                    value.update(
                        enabled=False,
                        last_error=type(error).__name__,
                        last_validation_at=_stamp(self.clock()),
                    )
                    self._save(value)
                continue
            check = self._claim(schedule["schedule_id"])
            if check:
                task = asyncio.create_task(self._execute(check))
                self._tasks.add(task)
                try:
                    reports.append(await task)
                finally:
                    self._tasks.discard(task)
        return reports

    tick_schedules = run_due

    def _recover_pending(self):
        for check_id in list(self._recovering):
            with self.lock:
                row = self.db.execute(
                    "SELECT payload FROM judge_canary_checks WHERE check_id=? AND status='pending'",
                    (check_id,),
                ).fetchone()
            if row is None:
                self._recovering.discard(check_id)
                continue
            check = json.loads(row[0])
            if any(
                run["run_id"] not in check["known_child_ids"]
                and run["config"].get("measurement_of") == check["source_run_id"]
                and run["state"] not in {"sealed", "ended", "killed"}
                for run in self.store.list_runs()
            ):
                continue
            self._finish(check, error="interrupted")
            self._recovering.discard(check_id)

    def reports_for_run(self, run_id):
        from analysis.instruments import load_artifact
        from analysis.values import _receipt

        reports = []
        for schedule in self.list(run_id):
            with self.lock:
                row = self.db.execute(
                    "SELECT payload FROM judge_canary_checks WHERE schedule_id=? ORDER BY rowid DESC LIMIT 1",
                    (schedule["schedule_id"],),
                ).fetchone()
            if row is None:
                reports.append(
                    {
                        "schedule_id": schedule["schedule_id"],
                        "status": "unmeasured",
                        "reason": schedule.get("last_error", "not_run"),
                        "last_attempt_at": None,
                    }
                )
                continue
            try:
                check = json.loads(row[0])
                if check["status"] != "measured":
                    if check.get("artifact"):
                        load_artifact(self.store, run_id, check["artifact"])
                    reports.append(
                        {
                            "schedule_id": schedule["schedule_id"],
                            "check_id": check["check_id"],
                            "status": "unmeasured",
                            "reason": check.get("error", "pending"),
                            "last_attempt_at": check["claimed_at"],
                            "completed_at": check.get("completed_at"),
                            "last_successful_check_at": schedule["last_check_at"],
                            "critique_artifact": check.get("artifact"),
                        }
                    )
                    continue
                body, _ = load_artifact(self.store, run_id, check["artifact"])
                report = json.loads(body)
                _, spec, _, _, _ = self._protocol(run_id, schedule["config"]["instrument"])
                seen = set()
                for judgment in report["judgments"]:
                    _receipt(self.store, run_id, spec, judgment, seen=seen)
                reports.append({**report, "artifact": check["artifact"], "status": "measured"})
            except (KeyError, ValueError, TypeError, OSError, RuntimeError) as error:
                reports.append(
                    {
                        "schedule_id": schedule["schedule_id"],
                        "status": "unmeasured",
                        "reason": type(error).__name__,
                        "last_recorded_check_at": schedule["last_check_at"],
                    }
                )
        return reports

    async def _loop(self):
        while not self._closing:
            try:
                await self.run_due()
            except (OSError, sqlite3.Error):
                pass  # Durable claims and authorization survive transient local failures.
            await asyncio.sleep(self.poll_seconds)

    async def recover(self):
        with self.lock:
            self._recovering.update(
                row[0]
                for row in self.db.execute(
                    "SELECT check_id FROM judge_canary_checks WHERE status='pending'"
                )
            )
        self._recover_pending()
        if self._worker is None:
            self._closing = False
            self._worker = asyncio.create_task(self._loop())

    async def aclose(self):
        if self._closed:
            return
        self._closing = True
        if self._worker:
            self._worker.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(
            *self._tasks, *([self._worker] if self._worker else []), return_exceptions=True
        )
        self.db.close()
        if self._owns_model and self.model is not None:
            await self.model.aclose()
        self._closed = True
