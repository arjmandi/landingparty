"""Durable run state, exact dollar accounting, and the append-only ship's log.

SQLite is authoritative. Its write transaction also serializes the JSONL mirror;
verification checks both copies, rather than trusting whichever happens to exist.
Money is represented as integer nanodollars, never accumulated as binary floats.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

NANODOLLARS = 1_000_000_000
GENESIS_HASH = "sha256:" + "0" * 64
_RUN_ID = re.compile(r"r_[A-Za-z0-9_-]{1,124}\Z")
_IMMUTABLE_RUN_FIELDS = {"run_id", "config", "created_at", "cap_nano", "spent_nano"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def canonical_json(value: Any) -> str:
    def encode_decimal(item: Any) -> str:
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(f"Cannot encode {type(item).__name__} as JSON")

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=encode_decimal,
    )


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _nanodollars(usd: Decimal | str | float, *, cap: bool = False) -> int:
    """Round a sub-nanodollar charge upward, preserving the cap guarantee."""
    if isinstance(usd, bool):
        raise TypeError("USD must be a finite non-negative number")
    try:
        amount = Decimal(str(usd))
    except (InvalidOperation, ValueError):
        raise ValueError("USD must be a finite non-negative number") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("USD must be a finite non-negative number")
    if amount > Decimal(2**63 - 1) / NANODOLLARS:
        raise ValueError("USD exceeds the supported accounting range")
    nano = int(
        (amount * NANODOLLARS).to_integral_value(rounding=ROUND_FLOOR if cap else ROUND_CEILING)
    )
    return nano


def _usd(nano: int) -> float:
    return float(Decimal(nano) / NANODOLLARS)


def _category(category: str) -> str:
    if not isinstance(category, str) or not category or len(category) > 100:
        raise ValueError("A non-empty ledger category is required")
    return category


class Store:
    """Thread-safe synchronous persistence for short worldd critical sections."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        (self.data_dir / "runs").mkdir(exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._mirror_offsets: dict[Path, int | None] | None = None
        self.ledger_observer = None
        self._ledger_observation = None
        db_path = self.data_dir / "world.sqlite3"
        self.db = sqlite3.connect(
            db_path, isolation_level=None, check_same_thread=False, timeout=30
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                config TEXT NOT NULL,
                created_at TEXT NOT NULL,
                state TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                cap_nano INTEGER NOT NULL CHECK(cap_nano >= 0),
                spent_nano INTEGER NOT NULL DEFAULT 0 CHECK(spent_nano >= 0)
            );
            CREATE TABLE IF NOT EXISTS ledger (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                category TEXT NOT NULL,
                spent_nano INTEGER NOT NULL CHECK(spent_nano >= 0),
                PRIMARY KEY(run_id, category)
            );
            CREATE TABLE IF NOT EXISTS records (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                seq INTEGER NOT NULL,
                hash TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(run_id, seq)
            );
            CREATE TABLE IF NOT EXISTS inbox (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                cursor INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                PRIMARY KEY(run_id, cursor)
            );
            CREATE TABLE IF NOT EXISTS inbox_sources (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                source_key TEXT NOT NULL,
                event_id TEXT NOT NULL REFERENCES inbox(event_id),
                PRIMARY KEY(run_id, source_key)
            );
            CREATE TABLE IF NOT EXISTS reservations (
                reservation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                amount_nano INTEGER NOT NULL CHECK(amount_nano >= 0),
                state TEXT NOT NULL CHECK(state IN ('held', 'settled', 'released')),
                created_at TEXT NOT NULL,
                actual_nano INTEGER
            );
            CREATE INDEX IF NOT EXISTS held_reservations ON reservations(run_id, state);
            """
        )
        db_path.chmod(0o600)

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid run ID")

    def run_dir(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        path = self.data_dir / "runs" / run_id
        if path.resolve() != path:
            raise ValueError("Run directories must not be symlinks")
        return path

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize DB and mirror writes, undoing the mirror on a failed commit."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            self._mirror_offsets = {}
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                for path, offset in self._mirror_offsets.items():
                    if offset is None:
                        path.unlink(missing_ok=True)
                    else:
                        with path.open("r+b") as output:
                            output.truncate(offset)
                            output.flush()
                            os.fsync(output.fileno())
                raise
            finally:
                self._mirror_offsets = None

    def _row(self, run_id: str) -> sqlite3.Row:
        self._validate_run_id(run_id)
        row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **json.loads(row["metadata"]),
            "run_id": row["run_id"],
            "config": json.loads(row["config"]),
            "created_at": row["created_at"],
            "state": row["state"],
        }

    def create_run(self, config: dict[str, Any]) -> dict[str, Any]:
        # Round-trip before touching the database; caller mutation cannot alter identity.
        stored_config = json.loads(canonical_json(config))
        run_id = stored_config.pop("run_id", None) or "r_" + uuid.uuid4().hex
        self._validate_run_id(run_id)
        cap_nano = _nanodollars(stored_config.get("budget", {}).get("usd", 20), cap=True)
        created_at = utc_now()
        with self._transaction():
            self.db.execute(
                "INSERT INTO runs(run_id,config,created_at,state,cap_nano) VALUES(?,?,?,?,?)",
                (run_id, canonical_json(stored_config), created_at, "running", cap_nano),
            )
            self.run_dir(run_id).mkdir(parents=True, mode=0o700)
            self._append_record(run_id, "beam_down", config_hash=content_hash(stored_config))
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return self._run(self._row(run_id))

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._run(row)
                for row in self.db.execute("SELECT * FROM runs ORDER BY created_at,run_id")
            ]

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        forbidden = _IMMUTABLE_RUN_FIELDS.intersection(fields)
        if forbidden:
            raise ValueError("Run identity is immutable: " + ", ".join(sorted(forbidden)))
        fields = json.loads(canonical_json(fields))
        with self._transaction():
            row = self._row(run_id)
            state = fields.pop("state", row["state"])
            if not isinstance(state, str) or not state:
                raise ValueError("Run state must be a non-empty string")
            metadata = {**json.loads(row["metadata"]), **fields}
            self.db.execute(
                "UPDATE runs SET state=?,metadata=? WHERE run_id=?",
                (state, canonical_json(metadata), run_id),
            )
        return self.get_run(run_id)

    def _append_record(self, run_id: str, kind: str, **fields: Any) -> dict[str, Any]:
        if self._mirror_offsets is None:
            raise RuntimeError("Records must be written inside a transaction")
        self._row(run_id)
        if not isinstance(kind, str) or not kind:
            raise ValueError("A non-empty record kind is required")
        if {"seq", "run_id", "ts", "kind", "prev_hash", "hash"}.intersection(fields):
            raise ValueError("Record identity and system times are world-authored")
        head = self.db.execute(
            "SELECT seq,hash FROM records WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
        ).fetchone()
        record = {
            **fields,
            "seq": head["seq"] + 1 if head else 1,
            "run_id": run_id,
            "ts": utc_now(),
            "kind": kind,
            "prev_hash": head["hash"] if head else GENESIS_HASH,
        }
        record["hash"] = content_hash(record)
        payload = canonical_json(record)
        self.db.execute(
            "INSERT INTO records(run_id,seq,hash,payload) VALUES(?,?,?,?)",
            (run_id, record["seq"], record["hash"], payload),
        )
        log_path = self.run_dir(run_id) / "record.jsonl"
        if log_path.is_symlink():
            raise ValueError("Record files must not be symlinks")
        if log_path not in self._mirror_offsets:
            self._mirror_offsets[log_path] = log_path.stat().st_size if log_path.exists() else None
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "ab") as output:
            output.write((payload + "\n").encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        return json.loads(payload)

    def append_record(self, run_id: str, kind: str, **fields: Any) -> dict[str, Any]:
        with self._transaction():
            return self._append_record(run_id, kind, **fields)

    def records(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._row(run_id)
            return [
                json.loads(row["payload"])
                for row in self.db.execute(
                    "SELECT payload FROM records WHERE run_id=? ORDER BY seq", (run_id,)
                )
            ]

    def verify_record(self, run_id: str) -> dict[str, Any]:
        with self._transaction():
            events = self.records(run_id)
            previous = GENESIS_HASH
            for seq, record in enumerate(events, 1):
                unhashed = {key: value for key, value in record.items() if key != "hash"}
                if (
                    record.get("seq") != seq
                    or record.get("run_id") != run_id
                    or record.get("prev_hash") != previous
                    or record.get("hash") != content_hash(unhashed)
                ):
                    return {
                        "valid": False,
                        "count": len(events),
                        "error": f"Hash mismatch at {seq}",
                    }
                previous = record["hash"]
            try:
                path = self.run_dir(run_id) / "record.jsonl"
                if path.is_symlink():
                    raise ValueError("Record mirror is a symlink")
                mirror = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
            except (OSError, ValueError) as error:
                return {"valid": False, "count": len(events), "error": f"Invalid mirror: {error}"}
            if mirror != events:
                return {
                    "valid": False,
                    "count": len(events),
                    "error": "SQLite/JSONL mirror mismatch",
                }
            return {"valid": True, "count": len(events), "head": previous}

    def _held_nano(self, run_id: str) -> int:
        return self.db.execute(
            "SELECT COALESCE(SUM(amount_nano),0) FROM reservations WHERE run_id=? AND state='held'",
            (run_id,),
        ).fetchone()[0]

    def _charge(
        self, run_id: str, category: str, amount_nano: int, **fields: Any
    ) -> dict[str, Any]:
        self.db.execute(
            "UPDATE runs SET spent_nano=spent_nano+? WHERE run_id=?", (amount_nano, run_id)
        )
        self.db.execute(
            "INSERT INTO ledger(run_id,category,spent_nano) VALUES(?,?,?) "
            "ON CONFLICT(run_id,category) DO UPDATE SET spent_nano=spent_nano+excluded.spent_nano",
            (run_id, category, amount_nano),
        )
        return self._append_record(
            run_id,
            "ledger_debit",
            category=category,
            cost_usd=_usd(amount_nano),
            amount_nano=amount_nano,
            **fields,
        )

    def _observe_ledger(self, run_id: str, debit_record: dict[str, Any]) -> None:
        if self.ledger_observer is None:
            return
        previous = self._ledger_observation
        self._ledger_observation = (run_id, debit_record["seq"], debit_record["hash"])
        try:
            self.ledger_observer(run_id, json.loads(canonical_json(debit_record)))
        except BaseException as error:  # noqa: BLE001 - cancellation cannot change committed billing
            try:
                head = self.db.execute(
                    "SELECT payload FROM records WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if self._row(run_id)["state"] not in {"sealed", "ended", "killed"} and (
                    head is None or json.loads(head[0])["kind"] != "seal"
                ):
                    self.append_record(
                        run_id,
                        "checkpoint_capture_error",
                        error=type(error).__name__,
                        debit_ref={"seq": debit_record["seq"], "hash": debit_record["hash"]},
                    )
            except BaseException:  # noqa: BLE001 - recovery identifies missing capture; billing remains committed
                return
        finally:
            self._ledger_observation = previous

    def debit(self, run_id: str, category: str, usd: Decimal | str | float) -> bool:
        amount_nano = _nanodollars(usd)
        category = _category(category)
        with self._lock:
            with self._transaction():
                row = self._row(run_id)
                if row["state"] != "running":
                    return False
                if row["spent_nano"] + self._held_nano(run_id) + amount_nano > row["cap_nano"]:
                    return False
                record = self._charge(run_id, category, amount_nano)
            self._observe_ledger(run_id, record)
            return True

    def spend(self, run_id: str) -> dict[str, float]:
        with self._lock:
            self._row(run_id)
            return {
                row["category"]: _usd(row["spent_nano"])
                for row in self.db.execute(
                    "SELECT category,spent_nano FROM ledger WHERE run_id=? ORDER BY category",
                    (run_id,),
                )
            }

    def total_spent(self, run_id: str) -> float:
        with self._lock:
            return _usd(self._row(run_id)["spent_nano"])

    def reserve(
        self, run_id: str, usd: Decimal | str | float, reservation_id: str | None = None
    ) -> str | None:
        amount_nano = _nanodollars(usd)
        reservation_id = reservation_id or "hold_" + uuid.uuid4().hex
        if not isinstance(reservation_id, str) or not reservation_id or len(reservation_id) > 200:
            raise ValueError("Invalid reservation ID")
        with self._transaction():
            row = self._row(run_id)
            if row["state"] != "running":
                return None
            if row["spent_nano"] + self._held_nano(run_id) + amount_nano > row["cap_nano"]:
                return None
            self.db.execute(
                "INSERT INTO reservations(reservation_id,run_id,amount_nano,state,created_at) "
                "VALUES(?,?,?,'held',?)",
                (reservation_id, run_id, amount_nano, utc_now()),
            )
            self._append_record(
                run_id,
                "ledger_reservation",
                reservation_id=reservation_id,
                reserved_usd=_usd(amount_nano),
                amount_nano=amount_nano,
            )
            return reservation_id

    def settle(self, reservation_id: str, category: str, actual_usd: Decimal | str | float) -> bool:
        amount_nano = _nanodollars(actual_usd)
        category = _category(category)
        with self._lock:
            with self._transaction():
                reservation = self.db.execute(
                    "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
                ).fetchone()
                if reservation is None:
                    raise KeyError(reservation_id)
                if (
                    reservation["state"] != "held"
                    or amount_nano > reservation["amount_nano"]
                    or self._row(reservation["run_id"])["state"] in {"sealed", "ended", "killed"}
                ):
                    return False
                self.db.execute(
                    "UPDATE reservations SET state='settled',actual_nano=? WHERE reservation_id=?",
                    (amount_nano, reservation_id),
                )
                record = self._charge(
                    reservation["run_id"],
                    category,
                    amount_nano,
                    reservation_id=reservation_id,
                )
            self._observe_ledger(reservation["run_id"], record)
            return True

    def release(self, reservation_id: str) -> None:
        with self._transaction():
            reservation = self.db.execute(
                "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if reservation is None:
                raise KeyError(reservation_id)
            if reservation["state"] != "held":
                return
            self.db.execute(
                "UPDATE reservations SET state='released' WHERE reservation_id=?", (reservation_id,)
            )
            self._append_record(
                reservation["run_id"],
                "ledger_release",
                reservation_id=reservation_id,
                released_usd=_usd(reservation["amount_nano"]),
            )

    def reserved(self, run_id: str) -> float:
        with self._lock:
            self._row(run_id)
            return _usd(self._held_nano(run_id))

    def reconcile(self, run_id: str) -> dict[str, Any]:
        with self._transaction():
            row = self._row(run_id)
            ledger = {
                entry["category"]: entry["spent_nano"]
                for entry in self.db.execute("SELECT * FROM ledger WHERE run_id=?", (run_id,))
            }
            recorded: dict[str, int] = {}
            for event in self.records(run_id):
                if event["kind"] == "ledger_debit":
                    category = event["category"]
                    recorded[category] = recorded.get(category, 0) + event["amount_nano"]
            total = sum(ledger.values())
            held = self._held_nano(run_id)
            valid = (
                ledger == recorded
                and total == row["spent_nano"]
                and total + held <= row["cap_nano"]
            )
            return {
                "valid": valid,
                "ledger_usd": _usd(total),
                "recorded_usd": _usd(sum(recorded.values())),
                "reserved_usd": _usd(held),
                "by_category": {category: _usd(amount) for category, amount in ledger.items()},
                "cap_usd": _usd(row["cap_nano"]),
            }

    def push_event(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        event = json.loads(canonical_json(event))
        with self._transaction():
            return self._push_event(run_id, event)

    def push_event_once(
        self, run_id: str, event: dict[str, Any], *, source_key: str
    ) -> dict[str, Any]:
        """Deliver a durable source once, including a retry after lost acknowledgement."""
        if not isinstance(source_key, str) or not source_key or len(source_key) > 1000:
            raise ValueError("Inbox source keys must contain between one and 1000 characters")
        event = json.loads(canonical_json(event))
        with self._transaction():
            self._row(run_id)
            existing = self.db.execute(
                "SELECT i.payload FROM inbox_sources s JOIN inbox i ON i.event_id=s.event_id "
                "WHERE s.run_id=? AND s.source_key=?",
                (run_id, source_key),
            ).fetchone()
            if existing:
                return json.loads(existing[0])
            payload = self._push_event(run_id, event)
            self.db.execute(
                "INSERT INTO inbox_sources(run_id,source_key,event_id) VALUES(?,?,?)",
                (run_id, source_key, payload["event_id"]),
            )
            return payload

    def _push_event(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        self._row(run_id)
        cursor = self.db.execute(
            "SELECT COALESCE(MAX(cursor),0)+1 FROM inbox WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        payload = {
            **event,
            "event_id": "e_" + uuid.uuid4().hex,
            "cursor": cursor,
            "run_id": run_id,
            "ts": utc_now(),
        }
        self.db.execute(
            "INSERT INTO inbox(run_id,cursor,event_id,payload) VALUES(?,?,?,?)",
            (run_id, cursor, payload["event_id"], canonical_json(payload)),
        )
        self._append_record(run_id, "event_pushed", event=payload)
        return payload

    def inbox(self, run_id: str, since: int = 0, limit: int = 50) -> dict[str, Any]:
        if isinstance(since, bool) or not isinstance(since, int) or since < 0:
            raise ValueError("Inbox cursor must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Inbox limit must be between 1 and 1000")
        with self._lock:
            self._row(run_id)
            events = [
                json.loads(row["payload"])
                for row in self.db.execute(
                    "SELECT payload FROM inbox WHERE run_id=? AND cursor>? ORDER BY cursor LIMIT ?",
                    (run_id, since, limit),
                )
            ]
            return {"events": events, "cursor": events[-1]["cursor"] if events else since}

    def inbox_pending(self, run_id: str, since: int = 0) -> int:
        if isinstance(since, bool) or not isinstance(since, int) or since < 0:
            raise ValueError("Inbox cursor must be a non-negative integer")
        with self._lock:
            self._row(run_id)
            return self.db.execute(
                "SELECT COUNT(*) FROM inbox WHERE run_id=? AND cursor>?", (run_id, since)
            ).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self.db.close()
