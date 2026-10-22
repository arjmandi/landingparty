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
        (data_dir / "late-events.sqlite3").chmod(0o600)

    def append(self, run_id, life_id, event):
        event = {**event, "event_id": identifier("e"), "run_id": run_id, "ts": now()}
        self.db.execute(
            "INSERT INTO events VALUES(?,?,?,?,NULL)",
            (event["event_id"], run_id, life_id, canonical(event)),
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
