"""Durable observations arriving after an immutable episode has sealed."""

import json
import sqlite3

from .util import canonical, identifier, now


class LateEvents:
    def __init__(self, data_dir):
        self.db = sqlite3.connect(
            data_dir / "late-events.sqlite3", isolation_level=None, check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events "
            "(event_id TEXT PRIMARY KEY,run_id TEXT,life_id TEXT,payload TEXT,delivered_to TEXT)"
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
        if "source_event_id" not in columns:
            self.db.execute("ALTER TABLE events ADD COLUMN source_event_id TEXT")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS late_source_event ON events(source_event_id) "
            "WHERE source_event_id IS NOT NULL"
        )
        (data_dir / "late-events.sqlite3").chmod(0o600)

    def append(self, run_id, life_id, event, *, source_event_id=None, observed_at=None):
        event = {
            **event,
            "event_id": identifier("e"),
            "run_id": run_id,
            "ts": observed_at or now(),
        }
        if source_event_id:
            event["source_event_id"] = source_event_id
        self.db.execute(
            "INSERT OR IGNORE INTO events "
            "(event_id,run_id,life_id,payload,delivered_to,source_event_id) "
            "VALUES(?,?,?,?,NULL,?)",
            (event["event_id"], run_id, life_id, canonical(event), source_event_id),
        )
        if source_event_id:
            return json.loads(
                self.db.execute(
                    "SELECT payload FROM events WHERE source_event_id=?", (source_event_id,)
                ).fetchone()[0]
            )
        return event

    def pending(self, life_id):
        return [
            json.loads(row["payload"])
            for row in self.db.execute(
                "SELECT payload FROM events WHERE life_id=? AND delivered_to IS NULL ORDER BY rowid",
                (life_id,),
            )
        ]

    def delivered(self, event_id, run_id):
        self.db.execute("UPDATE events SET delivered_to=? WHERE event_id=?", (run_id, event_id))

    def close(self):
        self.db.close()
