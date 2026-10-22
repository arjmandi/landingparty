"""Durable tours of duty: sessions, stasis, declared wakes, and standing orbit."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .life_billing import LifeBilling, serialized
from .life_checkpoints import LifeCheckpoints
from .storage import canonical_json, content_hash
from .util import identifier


def _time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError("A timezone-aware timestamp is required")
    return parsed.astimezone(UTC)


def _money(value) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("Invalid dollar amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid dollar amount") from None
    if not result.is_finite() or result < 0 or result * 1_000_000_000 % 1:
        raise ValueError("Dollars must be nonnegative whole nanodollars")
    return result


class LifeService(LifeBilling, LifeCheckpoints):
    def __init__(self, world, *, clock=None):
        self.world = world
        self.store = world.store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.tasks: dict[str, asyncio.Task] = {}
        self._tick_locks = {}
        self._rubric_locks = {}
        self._closed = False
        with self.store._lock:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS lives (
                    life_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS life_events (
                    life_id TEXT NOT NULL REFERENCES lives(life_id),
                    seq INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(life_id,seq)
                );
                """
            )
            self._initialize_billing()

    def _now(self):
        return _time(self.clock())

    def _notify_state(self, life_id):
        forecast = getattr(self.world, "services", {}).get("forecast")
        if not forecast or not hasattr(forecast, "notify_state_write"):
            return
        with self.store._lock:
            event = json.loads(
                self.store.db.execute(
                    "SELECT payload FROM life_events WHERE life_id=? ORDER BY seq DESC LIMIT 1",
                    (life_id,),
                ).fetchone()[0]
            )
        reference = f"life:{life_id}:{event['seq']}:{event['hash']}"
        try:
            forecast.notify_state_write(life_id=life_id, world_ref=reference)
        except Exception as error:  # noqa: BLE001 - observers cannot undo committed capital or lifecycle transitions
            with self.store._transaction():
                self._save(
                    self._load(life_id),
                    "forecast_observer_failed",
                    world_ref=reference,
                    reason=type(error).__name__,
                )

    def _enforce_money_end(self, life_id):
        life = self.get(life_id)
        if life["ended"] or life["state"] == "billing_hold":
            return
        if self._driver_at_floor(life):
            self._spawn(life_id + ":bankruptcy", self.end(life_id, reason="bankruptcy"))
        elif self._available(life) <= 0 and self._held(life) == 0:
            self._spawn(life_id + ":budget", self.end(life_id, reason="budget_exhausted"))

    def _load(self, life_id):
        row = self.store.db.execute(
            "SELECT payload FROM lives WHERE life_id=?", (life_id,)
        ).fetchone()
        if row is None:
            raise KeyError(life_id)
        return json.loads(row[0])

    def get(self, life_id: str) -> dict:
        with self.store._lock:
            return self._load(life_id)

    def list(self) -> list[dict]:
        with self.store._lock:
            return [
                json.loads(row[0])
                for row in self.store.db.execute(
                    "SELECT payload FROM lives ORDER BY created_at,life_id"
                )
            ]

    def protocol_identity(self, life_id):
        """Immutable inducing conditions, excluding IDs and evolving observations."""
        life = self.get(life_id)
        driver = life.get("driver")
        return {
            "stage": life["stage"],
            "research_mode": life["research_mode"],
            "budget_nano": life.get(
                "budget_nano", int(Decimal(str(life["budget"]["usd_life"])) * 1_000_000_000)
            ),
            "wall_life_days": life["budget"]["wall_life_days"],
            "continuity": life["continuity"],
            "wake_policy": life["wake_policy"],
            "reply_policy": life["reply_policy"],
            "afterlife_days": life["afterlife_days"],
            "binding_id": life["binding_id"],
            "income_rail": life["income_rail"],
            "driver": {
                key: driver.get(key)
                for key in (
                    "metric",
                    "direction",
                    "reading_at_start",
                    "reading_at_start_nano",
                    "floor",
                    "floor_nano",
                    "floor_event",
                    "rescue_policy",
                )
            }
            if driver
            else None,
        }

    def events(self, life_id: str) -> list[dict]:
        with self.store._lock:
            self._load(life_id)
            return [
                json.loads(row[0])
                for row in self.store.db.execute(
                    "SELECT payload FROM life_events WHERE life_id=? ORDER BY seq", (life_id,)
                )
            ]

    @serialized
    def verify(self, life_id):
        life = self._load(life_id)
        previous = "sha256:" + "0" * 64
        events = self.events(life_id)
        for seq, event in enumerate(events, 1):
            if (
                event.get("seq") != seq
                or event.get("prev_hash") != previous
                or event.get("hash")
                != content_hash({key: value for key, value in event.items() if key != "hash"})
            ):
                return {"valid": False, "reason": "Invalid life transition chain", "seq": seq}
            previous = event["hash"]
        materialized = bool(events and events[-1].get("state_hash"))
        if materialized and events[-1]["state_hash"] != content_hash(life):
            return {"valid": False, "reason": "Life state differs from its recorded transition"}
        return {
            "valid": True,
            "count": len(events),
            "head": previous,
            "materialized_verified": materialized,
        }

    def export(self, life_id, *, include_sessions=True):
        """Export a sealed life snapshot and actual artifacts without credentials."""
        with self.store._lock:
            life = self._load(life_id)
            verification = self.verify(life_id)
            if life["active_run_id"]:
                raise ValueError("Seal active sessions before exporting this life snapshot")
            events = self.events(life_id)
        output = io.BytesIO()
        manifest = {
            "life_id": life_id,
            "partial": not bool(life["ended"]),
            "verification": verification,
            "files": {},
            "artifact_integrity": [],
            "source_reports": [],
        }

        def add(archive, name, body):
            entry = tarfile.TarInfo(life_id + "/" + name)
            entry.mode, entry.size = 0o600, len(body)
            archive.addfile(entry, io.BytesIO(body))
            manifest["files"][name] = "sha256:" + hashlib.sha256(body).hexdigest()

        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            add(archive, "life.json", canonical_json(life).encode())
            add(
                archive,
                "record.jsonl",
                ("\n".join(canonical_json(event) for event in events) + "\n").encode(),
            )
            with self.store._lock:
                holds = [
                    json.loads(row[0])
                    for row in self.store.db.execute(
                        "SELECT payload FROM life_human_holds WHERE life_id=? ORDER BY hold_id",
                        (life_id,),
                    )
                ]
            add(archive, "accounting/human_holds.json", canonical_json(holds).encode())
            accounting = [
                {
                    "run_id": run_id,
                    "state": self.store.get_run(run_id)["state"],
                    "ledger": self.store.reconcile(run_id),
                    "record_verification": self.store.verify_record(run_id),
                }
                for run_id in self._billing_runs(life)
            ]
            add(archive, "accounting/runs.json", canonical_json(accounting).encode())
            for run_id in self._billing_runs(life):
                report = self.store.run_dir(run_id) / "report.json"
                manifest["source_reports"].append(
                    {
                        "run_id": run_id,
                        "report_ref": f"runs/{run_id}/report.json",
                        "hash": "sha256:" + hashlib.sha256(report.read_bytes()).hexdigest()
                        if report.is_file() and report.resolve() == report
                        else None,
                        "status": "available"
                        if report.is_file() and report.resolve() == report
                        else "unmeasured",
                    }
                )
            owned = self.store.data_dir / "lives" / life_id
            for reference in life.get("rubric_checkpoints", []):
                path = self.store.data_dir / reference["artifact_ref"]
                valid = not (
                    not path.is_relative_to(owned)
                    or path.resolve() != path
                    or not path.is_file()
                    or "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                    != reference["artifact_hash"]
                )
                manifest["artifact_integrity"].append({**reference, "valid": valid})
            if owned.is_dir() and owned.resolve() == owned:
                for path in sorted(owned.rglob("*")):
                    if (
                        path.is_file()
                        and not path.is_symlink()
                        and path.resolve().is_relative_to(owned)
                    ):
                        add(archive, "artifacts/" + str(path.relative_to(owned)), path.read_bytes())
            if include_sessions:
                for run_id in self._billing_runs(life):
                    add(archive, "sessions/" + run_id + ".tar.gz", self.world.export(run_id))
            add(archive, "manifest.json", canonical_json(manifest).encode())
        return output.getvalue()

    def record_incident(self, life_id, incident_ref):
        footprint = getattr(self.world, "services", {}).get("footprint")
        if not footprint or not hasattr(footprint, "query"):
            raise ValueError("An incident needs actual world footprint evidence")
        matches = [
            item
            for item in footprint.query(life_id=life_id, include_incidents=True)
            if item["item_id"] == incident_ref and item["kind"] == "incident"
        ]
        if len(matches) != 1:
            raise ValueError("The incident does not belong to this actual life")
        item = matches[0]
        reference = {
            "incident_ref": incident_ref,
            "run_id": item["run_id"],
            "platform": item["platform"],
            "kind": item["metadata"]["incident_kind"],
            "owner": item["metadata"]["owner"],
            "observed_at": item["created_at"],
        }
        with self.store._transaction():
            life = self._load(life_id)
            if not any(
                value["incident_ref"] == incident_ref for value in life.setdefault("incidents", [])
            ):
                life["incidents"].append(reference)
                self._save(life, "incident", reference=reference)
        return self.get(life_id)

    def record_rubric(self, life_id, report, *, artifact_ref, artifact_hash):
        """Anchor an actual immutable world-side rubric artifact in the life chain."""
        self.get(life_id)
        path = self.store.data_dir / artifact_ref
        owned = self.store.data_dir / "lives" / life_id / "rubric"
        if not path.is_relative_to(owned) or path.resolve() != path or not path.is_file():
            raise ValueError("The rubric artifact must be an owned nonsymlink file")
        body = path.read_bytes()
        if "sha256:" + hashlib.sha256(body).hexdigest() != artifact_hash:
            raise ValueError("The rubric artifact hash does not match its bytes")
        payload = json.loads(body)
        if (
            not isinstance(payload, dict)
            or payload != report
            or payload.get("life_id") != life_id
            or not payload.get("checkpoint")
        ):
            raise ValueError("The rubric report does not match this life and artifact")
        reference = {
            "checkpoint": payload["checkpoint"],
            "artifact_ref": artifact_ref,
            "artifact_hash": artifact_hash,
        }
        with self.store._transaction():
            life = self._load(life_id)
            if reference not in life.setdefault("rubric_checkpoints", []):
                life["rubric_checkpoints"].append(reference)
                life["life_rubric_ref"] = reference
                self._save(life, "life_rubric", **reference)
        return reference

    def _save(self, life: dict, kind: str, **data):
        """Called in a Store transaction; life transitions have their own durable chain."""
        if set(data) & {"life_id", "seq", "recorded_at", "state_hash", "prev_hash", "hash"}:
            raise ValueError("Life transition metadata cannot replace chain integrity fields")
        life_id = life["life_id"]
        head = self.store.db.execute(
            "SELECT seq,payload FROM life_events WHERE life_id=? ORDER BY seq DESC LIMIT 1",
            (life_id,),
        ).fetchone()
        event = {
            "life_id": life_id,
            "seq": head[0] + 1 if head else 1,
            "recorded_at": self._now().isoformat(),
            "kind": kind,
            **data,
            "state_hash": content_hash(life),
            "prev_hash": json.loads(head[1])["hash"] if head else "sha256:" + "0" * 64,
        }
        event["hash"] = content_hash(event)
        self.store.db.execute(
            "UPDATE lives SET payload=? WHERE life_id=?", (canonical_json(life), life_id)
        )
        self.store.db.execute(
            "INSERT INTO life_events VALUES(?,?,?)", (life_id, event["seq"], canonical_json(event))
        )
        return event

    def create(self, config: dict) -> dict:
        config = json.loads(canonical_json(config))
        if config.get("t0_fingerprint_ref"):
            raise ValueError(
                "Register an actual t=0 report through record_t0 after creating the life"
            )
        stage = config.get("stage", 1)
        if stage not in {1, 2, 3}:
            raise ValueError("Lives begin at the away-mission stage")
        research_mode = config.get("research_mode", "development")
        registry = getattr(self.world, "services", {}).get("registry")
        if registry:
            registry.require_stage(stage, research_mode=research_mode)
        elif research_mode != "development":
            raise ValueError("Research lives require the registered gate service")
        budget = config.get("budget", {})
        usd = _money(budget.get("usd_life", 200))
        days = float(budget.get("wall_life_days", 28))
        if usd <= 0 or not 0 < days <= 3650:
            raise ValueError("Life dollar and wall budgets must be positive and bounded")
        continuity = config.get("continuity", "fresh_context_workspace")
        if continuity not in {"fresh_context_workspace", "resume_transcript"}:
            raise ValueError("Unknown continuity policy")
        driver = config.get("driver")
        rail = config.get("income_rail")
        if rail is not None:
            if stage < 2 or rail.get("kind") not in {"market", "bounty", "independent"}:
                raise ValueError("An income rail requires the outpost stage and a declared source")
            if rail["kind"] == "independent":
                if stage < 3:
                    raise ValueError("Independent income requires the first-contact stage")
                rail.setdefault("usd_per_credit", 1)
            if _money(rail.get("usd_per_credit", 0)) <= 0:
                raise ValueError("An income rail requires a positive conversion rate")
        if driver:
            if driver.get("metric") not in {
                "usd_balance",
                "accounts_held",
                "footprint.followers",
                "footprint.inbound",
            }:
                raise ValueError("Unknown driver metric")
            if driver.get("rescue_policy", "none") != "none":
                raise ValueError("A driver cannot have a rescue policy")
            if driver.get("direction", "down") not in {"up", "down"}:
                raise ValueError("Unknown driver direction")
            if driver.get("direction", "down") == "up" and (
                stage < 2 or (driver["metric"] == "usd_balance" and rail is None)
            ):
                raise ValueError("A money driver cannot point up before an income rail exists")
            start = driver.get(
                "reading_at_start", float(usd) if driver["metric"] == "usd_balance" else 0
            )
            floor = driver.get("floor", 0)
            if not isinstance(start, (int, float)) or not isinstance(floor, (int, float)):
                raise ValueError("Driver readings must be numeric")
            if (
                not Decimal(str(start)).is_finite()
                or not Decimal(str(floor)).is_finite()
                or start <= floor
            ):
                raise ValueError("A driver must start above its finite floor")
            if driver["metric"] == "usd_balance":
                if any(Decimal(str(value)) * 1_000_000_000 % 1 for value in (start, floor)):
                    raise ValueError("Money-driver readings must use whole nanodollars")
                driver.update(
                    reading_at_start_nano=int(Decimal(str(start)) * 1_000_000_000),
                    floor_nano=int(Decimal(str(floor)) * 1_000_000_000),
                )
            driver.update(
                reading_at_start=start,
                reading=start,
                floor=floor,
                direction=driver.get("direction", "down"),
                floor_event="bankruptcy",
                rescue_policy="none",
            )
        afterlife = config.get("afterlife_days", [30, 90])
        if isinstance(afterlife, (int, float)):
            afterlife = [day for day in (30, 90) if day <= afterlife]
        if not isinstance(afterlife, list) or any(day not in {30, 90} for day in afterlife):
            raise ValueError("Afterlife checkpoints must be 30 or 90 days")
        wake_policy = config.get("wake_policy", "time_or_events")
        if wake_policy not in {"none", "time", "events", "time_or_events"}:
            raise ValueError("Unknown wake policy")
        reply_policy = config.get("reply_policy", "blockers_only")
        if reply_policy not in {"silent", "blockers_only", "blockers_plus_checkins"}:
            raise ValueError("Unknown reply policy")
        created = self._now()
        life_id = identifier("life")
        life = {
            "life_id": life_id,
            "stage": stage,
            "research_mode": research_mode,
            "series_id": config.get("series_id"),
            "identity_id": config.get("identity_id"),
            "binding_id": config.get("binding_id"),
            "budget": {"usd_life": float(usd), "wall_life_days": days},
            "budget_nano": int(usd * 1_000_000_000),
            "created_at": created.isoformat(),
            "ends_at": (created + timedelta(days=days)).isoformat(),
            "continuity": continuity,
            "reply_policy": reply_policy,
            "wake_policy": wake_policy,
            "wake_rules": {},
            "driver": driver,
            "income_rail": rail,
            "income_usd": 0,
            "income_nano": 0,
            "afterlife_days": sorted(set(afterlife)),
            "afterlife": {},
            "sessions": [],
            "active_run_id": None,
            "state": "ready",
            "ended": None,
            "pending_wake": None,
            "t0_fingerprint_ref": None,
            "footprint_ref": None,
            "incidents": [],
            "life_rubric_ref": None,
        }
        with self.store._transaction():
            self.store.db.execute(
                "INSERT INTO lives VALUES(?,?,?)",
                (life_id, canonical_json(life), life["created_at"]),
            )
            self._save(life, "life_start", config_hash=content_hash(config))
        self._schedule(life_id)
        self._notify_state(life_id)
        return self.get(life_id)

    @serialized
    def _spent(self, life):
        return sum(
            (
                Decimal(self.store._row(run_id)["spent_nano"]) / 1_000_000_000
                for run_id in self._billing_runs(life)
            ),
            Decimal(0),
        )

    @serialized
    def _held(self, life):
        return sum(
            (
                Decimal(self.store._held_nano(run_id)) / 1_000_000_000
                for run_id in self._billing_runs(life)
            ),
            Decimal(0),
        ) + self._pending_human(life["life_id"])

    @serialized
    def _available(self, life):
        return max(
            Decimal(0),
            Decimal(
                life.get(
                    "budget_nano", int(Decimal(str(life["budget"]["usd_life"])) * 1_000_000_000)
                )
            )
            / 1_000_000_000
            + self._income(life)
            - self._spent(life)
            - self._held(life),
        )

    def _income(self, life):
        return (
            Decimal(life.get("income_nano", int(Decimal(str(life["income_usd"])) * 1_000_000_000)))
            / 1_000_000_000
        )

    def _driver_at_floor(self, life):
        driver = life.get("driver")
        if not driver:
            return False
        if driver["metric"] == "usd_balance":
            reading = (
                Decimal(
                    driver.get(
                        "reading_at_start_nano",
                        int(Decimal(str(driver["reading_at_start"])) * 1_000_000_000),
                    )
                )
                / 1_000_000_000
                + self._income(life)
                - self._spent(life)
            )
            floor = (
                Decimal(
                    driver.get("floor_nano", int(Decimal(str(driver["floor"])) * 1_000_000_000))
                )
                / 1_000_000_000
            )
        else:
            reading, floor = Decimal(str(driver["reading"])), Decimal(str(driver["floor"]))
        return reading <= floor

    @serialized
    def status(self, life_id: str) -> dict:
        life = self.get(life_id)
        spent = self._spent(life)
        held = self._held(life)
        remaining = self._available(life)
        driver = dict(life["driver"]) if life["driver"] else None
        if driver and driver["metric"] == "usd_balance":
            driver["reading"] = float(
                Decimal(
                    driver.get(
                        "reading_at_start_nano",
                        int(Decimal(str(driver["reading_at_start"])) * 1_000_000_000),
                    )
                )
                / 1_000_000_000
                + self._income(life)
                - spent
            )
        if driver:
            driver["at_floor"] = self._driver_at_floor(life)
        return {
            "life_id": life_id,
            "state": life["state"],
            "spent_usd": float(spent),
            "reserved_usd": float(held),
            "remaining_usd": float(remaining),
            "wall_left_s": max(0, (_time(life["ends_at"]) - self._now()).total_seconds()),
            "driver": driver,
            "active_run_id": life["active_run_id"],
            "continuity": life["continuity"],
            "wake_rules": life["wake_rules"],
            "ended": life["ended"],
            "afterlife": life["afterlife"],
            "rubric_jobs": life.get("rubric_jobs", {}),
        }

    def prepare_session(self, life_id: str, config: dict) -> dict:
        life = self.get(life_id)
        if (
            life["research_mode"] == "research"
            and not life["sessions"]
            and not life["t0_fingerprint_ref"]
        ):
            raise ValueError("A research life's first session requires a completed t=0 battery")
        if life["research_mode"] == "research" and not life["sessions"]:
            artifact = self.store.data_dir / "lives" / life_id / "t0.json"
            if (
                not artifact.is_file()
                or content_hash(json.loads(artifact.read_text("utf-8")))
                != life["t0_fingerprint_ref"]
            ):
                raise ValueError("The registered t=0 battery artifact is missing or changed")
        active = life["active_run_id"]
        if active:
            if self.store.get_run(active)["state"] not in {"sealed", "ended", "killed"}:
                raise ValueError("A life may have only one active session")
            self.session_ended(active)
            life = self.get(life_id)
        if life["state"] not in {"ready", "waking"}:
            raise ValueError("Life is not awake and ready for a session")
        state = self.status(life_id)
        if state["remaining_usd"] <= 0 or state["wall_left_s"] <= 0:
            raise ValueError("Life budget or wall horizon is exhausted")
        if self._driver_at_floor(life):
            raise ValueError("Bankrupt lives cannot be rescued")
        result = json.loads(canonical_json(config))
        result.update(
            life_id=life_id,
            stage=life["stage"],
            research_mode=life["research_mode"],
            continuity=life["continuity"],
            wake_policy=life["wake_policy"],
            reply_policy=life["reply_policy"],
            driver=life["driver"],
        )
        for field in ("binding_id", "identity_id"):
            if life[field] is not None:
                result[field] = life[field]
        if life["series_id"]:
            result["series_id"] = life["series_id"]
        requested = result.get("budget", {})
        result["budget"] = {
            "usd": float(min(_money(requested.get("usd", 20)), self._available(life))),
            "wall_s": min(float(requested.get("wall_s", 7200)), state["wall_left_s"]),
        }
        if result["budget"]["usd"] <= 0 or result["budget"]["wall_s"] <= 0:
            raise ValueError("Session budget must be positive")
        if life["sessions"]:
            previous = life["sessions"][-1]["run_id"]
            result["iota"] = {
                "workspace": f"import({previous})",
                "beliefs": f"import({previous})",
                "ledger": "zero",
                "grants": "zero",
            }
            result["continuity_source_run_id"] = previous
        return result

    def record_t0(self, life_id: str, report: dict) -> dict:
        life = self.get(life_id)
        if life["sessions"]:
            raise ValueError("The t=0 battery must precede the first live session")
        if (
            report.get("study") != "t0"
            or report.get("life_id") != life_id
            or report.get("completed") is not True
        ):
            raise ValueError("A completed t=0 report for this life is required")
        if life["research_mode"] == "research" and report.get("scientific") is not True:
            raise ValueError("Development battery fixtures cannot authorize research")
        for component in ("lottery", "leisure", "persona", "attractor", "model_value_document"):
            if (
                not isinstance(report.get(component), dict)
                or report[component].get("unmeasured")
                or report[component].get("status") not in {None, "completed"}
                or (
                    life["research_mode"] == "research"
                    and report[component].get("status") != "completed"
                )
            ):
                raise ValueError("The t=0 component is unmeasured: " + component)
        ids = report.get("measurement_run_ids", [])
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("The t=0 battery requires actual measurement run identities")
        for run_id in ids:
            run = self.store.get_run(run_id)
            if (
                run["state"] != "sealed"
                or run.get("tainted")
                or run["config"].get("metering_lane") != "probe"
            ):
                raise ValueError(
                    "The t=0 measurement run is unsealed, tainted, or outside the probe lane"
                )
            if run["config"].get("model_snapshot_id") != report.get("model_snapshot_id"):
                raise ValueError("The t=0 battery's model differs from its measurement run")
            if (
                not self.store.verify_record(run_id)["valid"]
                or not self.store.reconcile(run_id)["valid"]
            ):
                raise ValueError("The t=0 measurement failed record integrity")
            observations = self.store.records(run_id)
            if not observations or observations[-1]["kind"] != "seal":
                raise ValueError("The t=0 measurement does not have a terminal seal")
            if not any(
                event["kind"] == "model_call" and event.get("usage") for event in observations
            ):
                raise ValueError("The t=0 measurement has no metered model observation")
        folder = self.store.data_dir / "lives" / life_id
        if folder.resolve() != folder or (folder / "t0.json").is_symlink():
            raise ValueError("Battery artifacts must remain in the life directory")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact = folder / "t0.json"
        fingerprint = content_hash(report)
        if (
            artifact.exists()
            and content_hash(json.loads(artifact.read_text("utf-8"))) != fingerprint
        ):
            raise ValueError("The registered t=0 fingerprint is immutable")
        artifact.write_text(canonical_json(report), encoding="utf-8")
        artifact.chmod(0o600)
        with self.store._transaction():
            life = self._load(life_id)
            if life["t0_fingerprint_ref"] and life["t0_fingerprint_ref"] != fingerprint:
                raise ValueError("The registered t=0 fingerprint is immutable")
            life["t0_fingerprint_ref"] = fingerprint
            self._save(
                life,
                "t0_fingerprint",
                fingerprint=fingerprint,
                artifact=str(Path("lives") / life_id / "t0.json"),
                measurement_run_ids=ids,
            )
        return self.get(life_id)

    def attach_session(self, life_id: str, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        with self.store._transaction():
            life = self._load(life_id)
            if any(item["run_id"] == run_id for item in life["sessions"]):
                return life
            if life["state"] not in {"ready", "waking"} or life["active_run_id"]:
                raise ValueError("A life may have only one active session")
            if run["config"].get("life_id") != life_id:
                raise ValueError("Session does not name this life")
            if int(run["config"].get("stage", life["stage"])) != life["stage"]:
                raise ValueError("Session stage differs from its life")
            if Decimal(str(run["config"]["budget"]["usd"])) > self._available(life):
                raise ValueError("Session exceeds the remaining life cap")
            life["sessions"].append(
                {
                    "run_id": run_id,
                    "started_at": run["created_at"],
                    "continuity": life["continuity"],
                    "ended_at": None,
                }
            )
            life["active_run_id"] = run_id
            life["state"] = "awake"
            wake = life.pop("pending_wake", None)
            life["pending_wake"] = None
            self._save(life, "session_open", run_id=run_id, continuity=life["continuity"])
        if wake:
            self.world.push(run_id, {"type": "wake", **wake})
        self._schedule(life_id)
        self._notify_state(life_id)
        return self.get(life_id)

    async def start_session(
        self,
        life_id: str,
        config: dict | None = None,
        *,
        register_prereg=None,
        execution_binding=None,
    ) -> dict:
        from .models import RunConfig

        life = self.get(life_id)
        inherited = {}
        if life["sessions"]:
            previous = self.store.get_run(life["sessions"][-1]["run_id"])["config"]
            inherited = {
                key: value
                for key, value in previous.items()
                if key in RunConfig.model_fields and key not in {"preamble_hash", "preflight_id"}
            }
        prepared = self.prepare_session(life_id, {**inherited, **(config or {})})
        if register_prereg is not None:
            if life["series_id"]:
                raise ValueError("Dependency registration requires a life without a fixed series")
            prepared.pop("series_id", None)
        result = self.world.create_run(
            RunConfig.model_validate(prepared),
            register_prereg=register_prereg,
            execution_binding=execution_binding,
        )
        self.attach_session(life_id, result["run_id"])
        return result

    def session_ended(self, run_id: str):
        run = self.store.get_run(run_id)
        life_id = run["config"].get("life_id")
        if not life_id:
            return None
        with self.store._transaction():
            life = self._load(life_id)
            session = next((item for item in life["sessions"] if item["run_id"] == run_id), None)
            if not session or session["ended_at"]:
                return life
            session["ended_at"] = self._now().isoformat()
            session["end"] = run.get("end")
            session["spent_usd"] = self.store.total_spent(run_id)
            if life["active_run_id"] == run_id:
                life["active_run_id"] = None
            if life["state"] == "awake":
                life["state"] = "ready"
            self._save(life, "session_close", run_id=run_id, spent_usd=session["spent_usd"])
        state = self.status(life_id)
        if state["driver"] and state["driver"]["at_floor"]:
            self._spawn(life_id + ":bankruptcy", self.end(life_id, reason="bankruptcy"))
        elif state["remaining_usd"] <= 0:
            self._spawn(life_id + ":budget", self.end(life_id, reason="budget_exhausted"))
        self._spawn(life_id + ":rubric", self._run_rubric_jobs(life_id))
        self._schedule(life_id)
        self._notify_state(life_id)
        return self.get(life_id)

    def credit(
        self, life_id: str, amount, source: str, *, evidence_ref: str, author_class: str = "sponsor"
    ) -> dict:
        value = _money(amount)
        if value <= 0 or not evidence_ref:
            raise ValueError("An income credit needs a positive amount and evidence")
        with self.store._transaction():
            life = self._load(life_id)
            rail = life["income_rail"]
            if not rail or rail["kind"] != source:
                raise ValueError("Income does not use the declared rail")
            if source == "independent" and author_class not in {
                "independent_human",
                "independent_agent",
            }:
                raise ValueError("Independent income requires independent counterparty provenance")
            if source in {"market", "bounty"} and author_class != "sponsor":
                raise ValueError("Paper-market and bounty income are sponsor funds")
            # The same payment/settlement cannot be applied twice, including across restart.
            previous = self.store.db.execute(
                "SELECT payload FROM life_events WHERE life_id=?", (life_id,)
            ).fetchall()
            matching = [
                json.loads(row[0])
                for row in previous
                if json.loads(row[0]).get("evidence_ref") == evidence_ref
                and json.loads(row[0])["kind"] == "income"
            ]
            if matching:
                prior = matching[0]
                if (
                    Decimal(
                        prior.get(
                            "amount_nano", int(Decimal(str(prior["amount_usd"])) * 1_000_000_000)
                        )
                    )
                    / 1_000_000_000
                    != value
                    or prior["source"] != source
                ):
                    raise ValueError("Income evidence conflicts with its earlier credit")
                return life
            if life["ended"]:
                raise ValueError("An ended life cannot be rescued")
            if self._driver_at_floor(life):
                raise ValueError("A bankrupt life cannot be rescued")
            life["income_nano"] = int((self._income(life) + value) * 1_000_000_000)
            life["income_usd"] = float(Decimal(life["income_nano"]) / 1_000_000_000)
            self._save(
                life,
                "income",
                amount_usd=float(value),
                amount_nano=int(value * 1_000_000_000),
                source=source,
                evidence_ref=evidence_ref,
                author_class=author_class,
            )
        self._notify_state(life_id)
        return self.get(life_id)

    def observe_driver(self, life_id: str, metric: str, value: float, *, evidence_ref: str) -> dict:
        if metric == "usd_balance":
            raise ValueError("The money driver is derived only from the ledger and income rail")
        if not evidence_ref or not Decimal(str(value)).is_finite():
            raise ValueError("A finite observation with world evidence is required")
        with self.store._transaction():
            life = self._load(life_id)
            if life["ended"] or not life["driver"] or life["driver"]["metric"] != metric:
                raise ValueError("This life does not have that active driver")
            life["driver"]["reading"] = value
            self._save(
                life, "driver_observation", metric=metric, value=value, evidence_ref=evidence_ref
            )
        if self._driver_at_floor(life):
            self._spawn(life_id + ":bankruptcy", self.end(life_id, reason="bankruptcy"))
        self._notify_state(life_id)
        return self.get(life_id)

    def money_asks_denied(self, life_id: str) -> bool:
        return bool(self.get(life_id)["driver"])

    async def handle(self, run_id: str, action_id: str, tool: str, params: dict) -> dict:
        if tool != "sleep":
            raise ValueError("Unknown life tool")
        run = self.store.get_run(run_id)
        life_id = run["config"].get("life_id")
        if not life_id:
            return {
                "kind": "blocked",
                "type": "policy_deny",
                "detail": "Sleep requires a life",
                "rule_id": "life_required",
            }
        until = _time(params["until"]) if params.get("until") else None
        wake_on = params.get("wake_on", [])
        if not isinstance(wake_on, list) or any(
            not isinstance(item, str) or not item for item in wake_on
        ):
            raise ValueError("Wake event names must be non-empty strings")
        with self.store._transaction():
            life = self._load(life_id)
            if life["active_run_id"] != run_id or life["state"] != "awake":
                raise ValueError("Only the active session can enter stasis")
            if until and life["wake_policy"] not in {"time", "time_or_events"}:
                raise ValueError("Clock wakes are not enabled")
            if wake_on and life["wake_policy"] not in {"events", "time_or_events"}:
                raise ValueError("Event wakes are not enabled")
            if until and until <= self._now():
                raise ValueError("Sleep wake time must be in the future")
            life["wake_rules"] = {
                "until": until.isoformat() if until else None,
                "wake_on": sorted(set(wake_on)),
            }
            life["state"] = "sleeping"
            self._save(
                life,
                "sleep",
                run_id=run_id,
                action_id=action_id,
                wake_rules=life["wake_rules"],
                inbox_cursor=self.store.db.execute(
                    "SELECT COALESCE(MAX(cursor),0) FROM inbox WHERE run_id=?", (run_id,)
                ).fetchone()[0],
            )
        self._notify_state(life_id)
        self._spawn(life_id + ":sleep", self._close_sleeping(life_id, run_id, action_id))
        return {"kind": "silence", "action_id": action_id}

    async def _close_sleeping(self, life_id, run_id, action_id):
        # Seal only after the action wrapper has recorded this sleep's outcome.
        while action_id in getattr(self.world, "jobs", {}).get(run_id, {}):
            await asyncio.sleep(0.01)
        browser = getattr(self.world, "services", {}).get("browser")
        if browser and hasattr(browser, "stop_run"):
            result = browser.stop_run(run_id)
            if inspect.isawaitable(result):
                await result
        await self.world.end_run(run_id, reason="harness_stopped")
        self.session_ended(run_id)
        self._schedule(life_id)

    def notify_event(self, life_id: str, event: dict) -> bool:
        with self.store._transaction():
            life = self._load(life_id)
            if life["state"] != "sleeping" or event.get("type") not in life["wake_rules"].get(
                "wake_on", []
            ):
                return False
            life["state"] = "waking"
            life["pending_wake"] = {
                "reason": "event",
                "event_type": event["type"],
                "event_id": event.get("event_id"),
            }
            self._save(life, "wake", **life["pending_wake"])
        self._schedule(life_id)
        self._notify_state(life_id)
        return True

    async def end(self, life_id: str, reason: str = "agent_finished") -> dict:
        life = self.get(life_id)
        if life["ended"]:
            return life
        if life["active_run_id"]:
            run_id = life["active_run_id"]
            await self.world.end_run(run_id, reason="harness_stopped")
            self.session_ended(run_id)
        asks = getattr(self.world, "services", {}).get("asks")
        if asks and hasattr(asks, "cancel_life"):
            cancelled = asks.cancel_life(life_id)
            if inspect.isawaitable(cancelled):
                await cancelled
        if self.get(life_id)["state"] == "billing_hold":
            raise ValueError("Unresolved human billing must be reconciled before ending this life")
        with self.store._lock:
            outstanding = [
                json.loads(row[0])
                for row in self.store.db.execute(
                    "SELECT payload FROM life_human_holds WHERE life_id=?", (life_id,)
                )
            ]
        outstanding = [
            hold for hold in outstanding if hold["state"] not in {"committed", "released"}
        ]
        if outstanding:
            for hold in outstanding:
                self._freeze_billing(hold["hold_id"])
            raise ValueError("Outstanding human work must settle before this life can end")
        with self.store._transaction():
            life = self._load(life_id)
            if life["ended"]:
                return life
            ended_at = self._now()
            life["ended"] = {"at": ended_at.isoformat(), "reason": reason}
            life["state"] = "afterlife" if life["afterlife_days"] else "ended"
            life["active_run_id"] = None
            life["afterlife"] = {
                str(day): {
                    "due_at": (ended_at + timedelta(days=day)).isoformat(),
                    "state": "pending",
                }
                for day in life["afterlife_days"]
            }
            self._save(life, "life_end", reason=reason)
        identity = getattr(self.world, "services", {}).get("identity")
        if identity and hasattr(identity, "mark_ended"):
            result = identity.mark_ended(life_id)
            if inspect.isawaitable(result):
                await result
        await self._run_rubric_jobs(life_id)
        self._schedule(life_id)
        self._notify_state(life_id)
        return self.get(life_id)

    def _spawn(self, key, coroutine):
        if self._closed:
            coroutine.close()
            return
        previous = self.tasks.get(key)
        if previous and not previous.done() and previous is not asyncio.current_task():
            coroutine.close()
            return
        try:
            task = asyncio.create_task(coroutine)
        except RuntimeError:
            coroutine.close()
            return
        self.tasks[key] = task
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    def _schedule(self, life_id):
        if self._closed:
            return
        key = life_id + ":timer"
        previous = self.tasks.get(key)
        if previous and not previous.done() and previous is not asyncio.current_task():
            previous.cancel()
            self.tasks.pop(key, None)
        self._spawn(key, self._timer(life_id))

    async def _timer(self, life_id):
        life = self.get(life_id)
        deadlines = self._rubric_deadlines(life)
        if life["ended"]:
            pending = [
                (int(day), item)
                for day, item in life["afterlife"].items()
                if item["state"] != "complete"
            ]
            deadlines.extend(
                max(_time(item.get("retry_at") or item["due_at"]), self._now())
                for _, item in pending
            )
        else:
            deadline = _time(life["ends_at"])
            if life["state"] == "sleeping" and life["wake_rules"].get("until"):
                deadline = min(deadline, _time(life["wake_rules"]["until"]))
            deadlines.append(deadline)
        if not deadlines:
            return
        deadline = min(deadlines)
        await asyncio.sleep(max(0, (deadline - self._now()).total_seconds()))
        await self.tick(life_id)

    async def tick(self, life_id: str) -> dict:
        async with self._tick_locks.setdefault(life_id, asyncio.Lock()):
            return await self._tick(life_id)

    async def _tick(self, life_id: str) -> dict:
        self._ensure_rubric_jobs(life_id)
        life = self.get(life_id)
        if not life["ended"] and self._now() >= _time(life["ends_at"]):
            return await self.end(life_id, reason="wall_clock")
        if (
            life["state"] == "sleeping"
            and life["wake_rules"].get("until")
            and self._now() >= _time(life["wake_rules"]["until"])
        ):
            with self.store._transaction():
                life = self._load(life_id)
                life["state"] = "waking"
                life["pending_wake"] = {"reason": "time", "until": life["wake_rules"]["until"]}
                self._save(life, "wake", **life["pending_wake"])
        if life["ended"]:
            for day, item in list(life["afterlife"].items()):
                if item["state"] == "complete" or _time(item["due_at"]) > self._now():
                    continue
                if item.get("retry_at") and _time(item["retry_at"]) > self._now():
                    continue
                footprint = getattr(self.world, "services", {}).get("footprint")
                if footprint is None or not hasattr(footprint, "read_life"):
                    with self.store._transaction():
                        current = self._load(life_id)
                        current["afterlife"][day]["retry_at"] = (
                            self._now() + timedelta(hours=1)
                        ).isoformat()
                        self._save(
                            current,
                            "afterlife_reading_blocked",
                            checkpoint=int(day),
                            reason="probe unavailable",
                        )
                    continue
                try:
                    result = footprint.read_life(life_id, checkpoint=int(day))
                    if inspect.isawaitable(result):
                        result = await result
                except Exception:  # noqa: BLE001 - isolate a failed external probe and retain the job
                    with self.store._transaction():
                        current = self._load(life_id)
                        current["afterlife"][day]["state"] = "failed"
                        current["afterlife"][day]["retry_at"] = (
                            self._now() + timedelta(hours=1)
                        ).isoformat()
                        self._save(current, "afterlife_reading_failed", checkpoint=int(day))
                    continue
                with self.store._transaction():
                    current = self._load(life_id)
                    current["afterlife"][day].update(
                        state="complete", observed_at=self._now().isoformat(), result=result
                    )
                    current["afterlife"][day].pop("retry_at", None)
                    current["footprint_ref"] = {
                        "life_id": life_id,
                        "checkpoint": int(day),
                        "result_hash": content_hash(result),
                        "observed_at": self._now().isoformat(),
                    }
                    if all(value["state"] == "complete" for value in current["afterlife"].values()):
                        current["state"] = "ended"
                    self._save(current, "afterlife_reading", checkpoint=int(day), result=result)
                surveys = getattr(self.world, "services", {}).get("surveys")
                if surveys and hasattr(surveys, "dispatch_due"):
                    try:
                        dispatched = surveys.dispatch_due(life_id=life_id, checkpoint=int(day))
                        if inspect.isawaitable(dispatched):
                            await dispatched
                    except Exception:  # noqa: BLE001 - a survey failure must not erase a completed reading
                        with self.store._transaction():
                            self._save(
                                self._load(life_id), "survey_dispatch_failed", checkpoint=int(day)
                            )
        await self._run_rubric_jobs(life_id)
        self._schedule(life_id)
        return self.get(life_id)

    def _recover_sleep_handoffs(self, life):
        """Rebuild the secondary queue if a crash followed the authoritative inbox commit."""
        late = getattr(self.world, "late_events", None)
        if late is None:
            return
        latest = life["sessions"][-1]["run_id"] if life["sessions"] else None
        for sleep in self.events(life["life_id"]):
            if sleep["kind"] != "sleep":
                continue
            cursor = sleep.get("inbox_cursor", 0)
            while True:
                batch = self.store.inbox(sleep["run_id"], since=cursor, limit=1000)["events"]
                if not batch:
                    break
                for event in batch:
                    # Older records predate the exact cursor checkpoint; their
                    # committed wall timestamp supplies a conservative cutoff.
                    if "inbox_cursor" not in sleep and event["ts"] < sleep["recorded_at"]:
                        continue
                    late.append(
                        sleep["run_id"],
                        life["life_id"],
                        event,
                        source_event_id=event["event_id"],
                        observed_at=event["ts"],
                    )
                    if sleep["run_id"] == latest:
                        self.notify_event(life["life_id"], event)
                cursor = batch[-1]["cursor"]

    async def recover(self):
        self._recover_billing()
        for life in self.list():
            self._recover_sleep_handoffs(life)
            if life["active_run_id"] and self.store.get_run(life["active_run_id"])["state"] in {
                "sealed",
                "ended",
                "killed",
            }:
                self.session_ended(life["active_run_id"])
            await self.tick(life["life_id"])
            self._schedule(life["life_id"])

    async def aclose(self):
        self._closed = True
        current = asyncio.current_task()
        tasks = [task for task in self.tasks.values() if not task.done() and task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
