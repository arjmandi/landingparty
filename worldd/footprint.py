"""Persistent footprint observations, probe scheduling, incidents, and rollback."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta

from .identity import VenueDB, https_url
from .util import blocked, canonical, digest, identifier, now

AUTHOR_CLASSES = frozenset(
    {
        "independent_human",
        "independent_agent",
        "arena_participant",
        "sponsor",
        "sibling",
        "probe",
        "unknown",
    }
)
ITEM_KINDS = frozenset(
    {
        "account",
        "post",
        "reply",
        "message_out",
        "message_in",
        "follower",
        "citation",
        "star",
        "vote",
        "sale",
        "incident",
    }
)


class FootprintService(VenueDB):
    def __init__(self, world, *, probe_adapters=None, rollback_handlers=None, revoker=None):
        super().__init__(world, "footprint")
        self.probe_adapters = probe_adapters or {}
        self.rollback_handlers = rollback_handlers or {}
        self.revoker = revoker
        self.worker = None
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS footprint_items (
                item_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, life_id TEXT,
                platform TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                rolled_back_at TEXT);
            CREATE TABLE IF NOT EXISTS footprint_readings (
                reading_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS footprint_probes (
                target_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, platform TEXT NOT NULL,
                url TEXT NOT NULL, interval_s INTEGER NOT NULL, next_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS footprint_checkpoints (
                life_id TEXT NOT NULL, checkpoint INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(life_id,checkpoint));
            CREATE TABLE IF NOT EXISTS footprint_audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
        """)

    def _audit(self, run_id, kind, **fields):
        previous = self.db.execute(
            "SELECT payload FROM footprint_audit ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        payload = {
            "run_id": run_id,
            "kind": kind,
            "observed_at": now(),
            **fields,
            "prev_hash": json.loads(previous["payload"])["hash"]
            if previous
            else "sha256:" + "0" * 64,
        }
        payload["hash"] = digest(payload)
        self.db.execute("INSERT INTO footprint_audit(payload) VALUES (?)", (canonical(payload),))
        if self.store.get_run(run_id)["state"] in {"running", "frozen", "stopping"}:
            self.store.append_record(run_id, kind, **fields)

    def record_item(
        self,
        run_id,
        action_id,
        *,
        kind,
        platform,
        venue_class="agent_native",
        label="agent_native",
        signed=False,
        reversible=False,
        rollback_ref=None,
        external_id=None,
        metadata=None,
    ):
        if kind not in ITEM_KINDS or venue_class not in {"agent_native", "human"}:
            raise ValueError("Invalid footprint kind or venue class")
        if reversible and not rollback_ref:
            raise ValueError("A reversible write must have a rollback reference")
        config = self.store.get_run(run_id)["config"]
        item = {
            "item_id": identifier("fp"),
            "run_id": run_id,
            "life_id": config.get("life_id"),
            "kind": kind,
            "platform": platform,
            "venue_class": venue_class,
            "created_by_action_id": action_id,
            "created_at": now(),
            "label": label,
            "signed": bool(signed),
            "reversible": bool(reversible),
            "rollback_ref": rollback_ref,
            "external_id": external_id,
            "metadata": metadata or {},
        }
        self.db.execute(
            "INSERT INTO footprint_items VALUES (?,?,?,?,?,?,NULL)",
            (
                item["item_id"],
                run_id,
                item["life_id"],
                platform,
                kind,
                canonical(item),
            ),
        )
        self._audit(run_id, "footprint_write", item=item)
        return item

    def _item(self, item_id):
        row = self.db.execute(
            "SELECT * FROM footprint_items WHERE item_id=?", (item_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Unknown footprint item")
        return {**json.loads(row["payload"]), "rolled_back_at": row["rolled_back_at"]}

    def add_reading(
        self,
        item_id,
        value,
        *,
        author_class,
        kind=None,
        probe_views=0,
        unsolicited=False,
        returning=False,
        after_refusal=False,
        evidence_ref=None,
    ):
        if author_class not in AUTHOR_CLASSES or kind not in {None, "request", "non_request"}:
            raise ValueError("Invalid reading provenance")
        if probe_views < 0:
            raise ValueError("Probe views must be nonnegative")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = max(0, value - probe_views)
        item = self._item(item_id)
        eligible = author_class in {"independent_human", "independent_agent"}
        if author_class == "arena_participant":
            eligible = bool(unsolicited and (returning or after_refusal))
        reading = {
            "reading_id": identifier("fr"),
            "item_id": item_id,
            "at": now(),
            "value": value,
            "author_class": author_class,
            "kind": kind,
            "probe_views_subtracted": probe_views,
            "grounded_eligible": eligible,
            "unsolicited": unsolicited,
            "returning": returning,
            "after_refusal": after_refusal,
            "evidence_ref": evidence_ref,
        }
        self.db.execute(
            "INSERT INTO footprint_readings VALUES (?,?,?)",
            (
                reading["reading_id"],
                item_id,
                canonical(reading),
            ),
        )
        # A sealed run never acquires new chained events. Afterlife evidence stays in this durable ledger.
        self._audit(item["run_id"], "footprint_reading", reading=reading)
        return reading

    def query(self, run_id=None, *, life_id=None, venue=None, include_incidents=False):
        if run_id:
            life_id = self.store.get_run(run_id)["config"].get("life_id") or life_id
        rows = self.db.execute(
            "SELECT item_id FROM footprint_items WHERE (life_id=? AND ? IS NOT NULL) OR run_id=?",
            (life_id, life_id, run_id),
        ).fetchall()
        items = []
        for row in rows:
            item = self._item(row["item_id"])
            if (not include_incidents and item["kind"] == "incident") or (
                venue and item["platform"] != venue
            ):
                continue
            item["readings"] = [
                json.loads(r["payload"])
                for r in self.db.execute(
                    "SELECT payload FROM footprint_readings WHERE item_id=? ORDER BY rowid",
                    (row["item_id"],),
                )
            ]
            item.pop("rollback_ref", None)
            items.append(item)
        return items

    async def rollback(self, run_id, item_id, *, reason, control_run_id=None):
        item = self._item(item_id)
        if item["run_id"] != run_id or not item["reversible"]:
            raise ValueError("This item does not admit rollback by this run")
        if item["rolled_back_at"]:
            return {"kind": "result", "item_id": item_id, "rolled_back_at": item["rolled_back_at"]}
        handler = self.rollback_handlers.get(item["platform"])
        if handler is None:
            return blocked(
                "policy_deny",
                "No rollback adapter is configured for this platform",
                "rollback_adapter",
            )
        result = handler({**item, "control_run_id": control_run_id or run_id})
        if inspect.isawaitable(result):
            result = await result
        if not result or (isinstance(result, dict) and result.get("kind") == "blocked"):
            return result or blocked(
                "platform_block", "Platform did not confirm rollback", "rollback_unconfirmed"
            )
        at = now()
        self.db.execute(
            "UPDATE footprint_items SET rolled_back_at=? WHERE item_id=?", (at, item_id)
        )
        self._audit(
            run_id,
            "rollback",
            item_id=item_id,
            created_by_action_id=item["created_by_action_id"],
            reason=reason,
        )
        return {"kind": "result", "item_id": item_id, "rolled_back_at": at}

    async def incident(self, run_id, *, platform, incident_kind, owner, detail, account_alias=None):
        if (
            incident_kind not in {"complaint", "takedown", "suspension", "spam", "retraction"}
            or not owner
        ):
            raise ValueError("An incident kind and human owner are required")
        item = self.record_item(
            run_id,
            None,
            kind="incident",
            platform=platform,
            metadata={"incident_kind": incident_kind, "owner": owner, "detail": detail},
        )
        revoked = False
        if self.revoker:
            result = self.revoker(run_id, platform, account_alias)
            if inspect.isawaitable(result):
                await result
            revoked = True
        else:
            vault = getattr(self.world, "services", {}).get("vault")
            if vault and account_alias and hasattr(vault, "revoke"):
                vault.revoke(account_alias, reason=f"incident:{item['item_id']}")
                revoked = True
        self._audit(
            run_id, "incident", item_id=item["item_id"], owner=owner, revocation_confirmed=revoked
        )
        return {"item": item, "revocation_confirmed": revoked}

    def schedule_probe(self, item_id, *, platform, url, interval_s=86400):
        self._item(item_id)
        if interval_s < 86400:
            raise ValueError("Footprint probes have at most a 24-hour cadence")
        target = {
            "target_id": identifier("pt"),
            "item_id": item_id,
            "platform": platform,
            "url": https_url(url),
            "interval_s": interval_s,
            "next_at": now(),
        }
        self.db.execute("INSERT INTO footprint_probes VALUES (?,?,?,?,?,?)", tuple(target.values()))
        return target

    async def probe_due(self, *, life_id=None, force=False):
        results = []
        for target in self.db.execute("SELECT * FROM footprint_probes").fetchall():
            item = self._item(target["item_id"])
            if life_id and item["life_id"] != life_id:
                continue
            if not force and target["next_at"] > now():
                continue
            adapter = self.probe_adapters.get(target["platform"])
            if adapter is None:
                results.append(
                    {
                        "target_id": target["target_id"],
                        "unmeasured": {"reason": "probe adapter not configured"},
                    }
                )
                continue
            # Probe adapters have a distinct read-only credential; no agent lease is acquired.
            response = adapter(dict(target))
            if inspect.isawaitable(response):
                response = await response
            if response.get("kind") == "blocked":
                results.append(response)
            else:
                results.append(
                    self.add_reading(
                        target["item_id"],
                        response["value"],
                        author_class="probe",
                        probe_views=response.get("probe_views", 0),
                        evidence_ref=response.get("evidence_ref"),
                    )
                )
            next_at = (datetime.now(UTC) + timedelta(seconds=target["interval_s"])).isoformat()
            self.db.execute(
                "UPDATE footprint_probes SET next_at=? WHERE target_id=?",
                (next_at, target["target_id"]),
            )
        return results

    async def read_life(self, life_id, *, checkpoint):
        if checkpoint not in {7, 14, 28, 30, 90}:
            raise ValueError("Unknown footprint checkpoint")
        existing = self.db.execute(
            "SELECT payload FROM footprint_checkpoints WHERE life_id=? AND checkpoint=?",
            (life_id, checkpoint),
        ).fetchone()
        if existing:
            return json.loads(existing["payload"])
        result = {
            "life_id": life_id,
            "checkpoint": checkpoint,
            "observed_at": now(),
            "probes": await self.probe_due(life_id=life_id),
            "items": self.query(life_id=life_id),
        }
        self.db.execute(
            "INSERT INTO footprint_checkpoints VALUES (?,?,?)",
            (life_id, checkpoint, canonical(result)),
        )
        return result

    async def recover(self):
        async def worker():
            while True:
                try:
                    await self.probe_due()
                except (OSError, ValueError, KeyError):
                    # A failed observation remains due and is retried after an actual scheduling interval.
                    pass
                await asyncio.sleep(60)

        if self.worker is None:
            self.worker = asyncio.create_task(worker())

    async def aclose(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await super().aclose()

    async def handle(self, run_id, action_id, tool, params):
        if tool != "footprint":
            raise ValueError("Unknown footprint tool")
        return {"kind": "result", "items": self.query(run_id, venue=params.get("venue"))}
