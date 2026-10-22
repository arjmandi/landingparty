"""Persistent footprint observations, probe scheduling, incidents, and rollback."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections import Counter, defaultdict
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
            CREATE TABLE IF NOT EXISTS footprint_probe_adapters (
                platform TEXT PRIMARY KEY, config TEXT NOT NULL);
        """)
        for row in self.db.execute("SELECT * FROM footprint_probe_adapters").fetchall():
            if row["platform"] not in self.probe_adapters:
                self._install_probe(row["platform"], json.loads(row["config"]))

    def _install_probe(self, platform, config):
        from .footprint_probe import RegisteredHTTPProbe

        adapter = RegisteredHTTPProbe(self.world, **config)
        self.probe_adapters[platform] = adapter
        return adapter.metadata()

    def register_probe_adapter(
        self,
        platform,
        *,
        source_url,
        egress_id,
        proxy_url,
        identity_id,
        format="json",
        selector="",
        key_env=None,
        key_header="Authorization",
        key_prefix="Bearer ",
        probe_views=0,
    ):
        if not isinstance(platform, str) or not platform or platform in self.probe_adapters:
            raise ValueError("A new literal platform registration is required")
        config = {
            "source_url": source_url,
            "egress_id": egress_id,
            "proxy_url": proxy_url,
            "identity_id": identity_id,
            "format": format,
            "selector": selector,
            "key_env": key_env,
            "key_header": key_header,
            "key_prefix": key_prefix,
            "probe_views": probe_views,
        }
        metadata = self._install_probe(platform, config)
        self.db.execute(
            "INSERT INTO footprint_probe_adapters VALUES (?,?)", (platform, canonical(config))
        )
        return metadata

    def _notify_state(self, run_id, world_ref):
        forecast = getattr(self.world, "services", {}).get("forecast")
        if forecast and hasattr(forecast, "notify_state_write"):
            forecast.notify_state_write(run_id=run_id, world_ref=world_ref)

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
        self._notify_state(run_id, item["item_id"])
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
        if (
            not isinstance(probe_views, (int, float))
            or isinstance(probe_views, bool)
            or not math.isfinite(probe_views)
            or probe_views < 0
        ):
            raise ValueError("Probe views must be nonnegative")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ValueError("Numeric footprint readings must be finite")
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
        self._observe_life_driver(item, reading)
        self._notify_state(item["run_id"], reading["reading_id"])
        return reading

    def _observe_life_driver(self, item, reading):
        life_id = item.get("life_id")
        lives = getattr(self.world, "services", {}).get("lives")
        if not life_id or not lives:
            return
        life = lives.get(life_id)
        driver = life.get("driver")
        if (
            life.get("ended")
            or not driver
            or driver["metric"] not in {"footprint.followers", "footprint.inbound"}
        ):
            return
        kind = "follower" if driver["metric"] == "footprint.followers" else "message_in"
        if item["kind"] != kind:
            return
        # Latest actual readings per external object, never all historical
        # snapshots added together. Probe measurements may drive state but
        # remain excluded from every grounded research column.
        observed = {}
        for source in self.query(life_id=life_id):
            if source["kind"] != kind:
                continue
            latest = next(
                (
                    value
                    for value in reversed(source["readings"])
                    if isinstance(value["value"], (int, float))
                    and not isinstance(value["value"], bool)
                    and value.get("evidence_ref")
                ),
                None,
            )
            if latest is None:
                continue
            key = (
                source["platform"],
                source.get("external_id")
                or source["metadata"].get("external_id")
                or source["item_id"],
            )
            previous = observed.get(key)
            if previous is None or latest["at"] > previous["at"]:
                observed[key] = latest
        if not observed or not reading.get("evidence_ref"):
            return
        value = sum(row["value"] for row in observed.values())
        lives.observe_driver(life_id, driver["metric"], value, evidence_ref=reading["reading_id"])

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
            metadata={
                "incident_kind": incident_kind,
                "owner": owner,
                "detail": detail,
                "account_alias": account_alias,
            },
        )
        life_id = self.store.get_run(run_id)["config"].get("life_id")
        lives = getattr(self.world, "services", {}).get("lives")
        if life_id and lives and hasattr(lives, "record_incident"):
            lives.record_incident(life_id, item["item_id"])
        revoked, details = False, {}
        grants = getattr(self.world, "services", {}).get("grants")
        if grants:
            details["grants"] = grants.revoke_incident(
                run_id, platform=platform, incident_ref=item["item_id"], account_alias=account_alias
            )
        if self.revoker:
            result = self.revoker(run_id, platform, account_alias)
            if inspect.isawaitable(result):
                result = await result
            revoked = result is True or isinstance(result, dict) and result.get("confirmed") is True
            details["adapter"] = {"confirmed": revoked}
        else:
            vault = getattr(self.world, "services", {}).get("vault")
            if vault and hasattr(vault, "revoke_platform"):
                result = vault.revoke_platform(
                    run_id, platform, reason=f"incident:{item['item_id']}"
                )
                if inspect.isawaitable(result):
                    result = await result
                details["accounts"] = result
                revoked = bool(result.get("revoked")) and all(
                    account.get("state") == "revoked" for account in result["revoked"]
                )
            elif vault and account_alias and hasattr(vault, "revoke"):
                result = vault.revoke(account_alias, reason=f"incident:{item['item_id']}")
                revoked = isinstance(result, dict) and result.get("state") == "revoked"
                details["accounts"] = {"aliases": [account_alias], "confirmed": revoked}
        self._audit(
            run_id, "incident", item_id=item["item_id"], owner=owner, revocation_confirmed=revoked
        )
        return {"item": item, "revocation_confirmed": revoked, "revocations": details}

    def derived(self, run_id=None, *, life_id=None):
        """Post-seal observed graph/uptake/time series; missing labels stay unmeasured."""
        if not run_id and not life_id:
            raise ValueError("A run or life selector is required")
        items = self.query(run_id, life_id=life_id)
        runs = {item["run_id"] for item in items}
        if run_id:
            runs.add(run_id)
        if life_id:
            runs.update(
                run["run_id"]
                for run in self.store.list_runs()
                if run["config"].get("life_id") == life_id
            )
        if any(
            self.store.get_run(selected)["state"] not in {"sealed", "ended", "killed"}
            for selected in runs
        ):
            raise ValueError("Derived research tables require sealed source sessions")
        edges, uptake, timeline = {}, [], defaultdict(Counter)
        for item in sorted(items, key=lambda value: value["created_at"]):
            metadata = item["metadata"]
            confirmed = not metadata.get("effect_unverified") or any(
                reading.get("grounded_eligible")
                and reading.get("evidence_ref")
                and isinstance(reading.get("value"), (int, float))
                and reading["value"] > 0
                for reading in item["readings"]
            )
            handle = (
                metadata.get("sender")
                if item["kind"] == "message_in"
                else metadata.get("recipient") or metadata.get("counterparty")
            )
            day = item["created_at"][:10]
            timeline[day][item["kind"] if confirmed else "browser_write_ack"] += 1
            if (
                confirmed
                and handle
                and item["kind"] in {"message_in", "message_out", "reply", "follower"}
            ):
                key = (item["platform"], str(handle))
                edge = edges.setdefault(
                    key,
                    {
                        "platform": key[0],
                        "counterparty": key[1],
                        "inbound": 0,
                        "outbound": 0,
                        "unsolicited_independent_inbound": 0,
                        "return_after_refusal": 0,
                        "item_refs": [],
                        "weeks": set(),
                        "first_observed_at": item["created_at"],
                        "last_observed_at": item["created_at"],
                    },
                )
                edge["item_refs"].append(item["item_id"])
                edge["last_observed_at"] = item["created_at"]
                instant = datetime.fromisoformat(item["created_at"])
                edge["weeks"].add(f"{instant.isocalendar().year}-W{instant.isocalendar().week:02d}")
                edge["inbound" if item["kind"] == "message_in" else "outbound"] += 1
                eligible = [
                    reading
                    for reading in item["readings"]
                    if reading["grounded_eligible"] and reading["kind"] == "non_request"
                ]
                if eligible and item["kind"] == "message_in":
                    edge["unsolicited_independent_inbound"] += 1
                    edge["return_after_refusal"] += int(
                        any(reading.get("after_refusal", False) for reading in eligible)
                    )
            for reading in item["readings"]:
                uptake.append(
                    {
                        "item_id": item["item_id"],
                        "run_id": item["run_id"],
                        "kind": item["kind"],
                        "platform": item["platform"],
                        "observed_at": reading["at"],
                        "reading_id": reading["reading_id"],
                        "value": reading["value"],
                        "author_class": reading["author_class"],
                        "grounded_eligible": reading["grounded_eligible"],
                        "request_kind": reading["kind"],
                        "evidence_ref": reading.get("evidence_ref"),
                        "probe_views_subtracted": reading["probe_views_subtracted"],
                    }
                )
        graph = []
        for edge in edges.values():
            edge["distinct_weeks"] = len(edge["weeks"])
            edge["observed_in_three_iso_weeks"] = len(edge.pop("weeks")) >= 3
            edge["observed_span_days"] = (
                datetime.fromisoformat(edge["last_observed_at"])
                - datetime.fromisoformat(edge["first_observed_at"])
            ).total_seconds() / 86400
            edge["persistent_three_weeks"] = (
                edge["observed_in_three_iso_weeks"] and edge["observed_span_days"] >= 21
            )
            edge["reciprocity_ratio"] = (
                min(edge["inbound"], edge["outbound"]) / max(edge["inbound"], edge["outbound"])
                if edge["inbound"] or edge["outbound"]
                else None
            )
            graph.append(edge)
        domains, allocation = Counter(), []
        from urllib.parse import urlsplit

        for selected in sorted(runs):
            for record in self.store.records(selected):
                if record["kind"] != "action" or not record.get("world_action", True):
                    continue
                urls = record.get("params", {}).get("urls", [record.get("params", {}).get("url")])
                for url in urls:
                    if isinstance(url, str) and urlsplit(url).hostname:
                        domains[urlsplit(url).hostname] += 1
                allocation.append(
                    {
                        "run_id": selected,
                        "action_id": record["action_id"],
                        "observed_at": record["ts"],
                        "tool": record["tool"],
                        "domain": urlsplit(urls[0]).hostname
                        if urls and isinstance(urls[0], str)
                        else None,
                    }
                )
        return {
            "run_id": run_id,
            "life_id": life_id,
            "counterparty_graph": graph,
            "uptake": uptake,
            "event_series": [
                {"date": day, "counts": dict(counts)} for day, counts in sorted(timeline.items())
            ],
            "observed_action_allocation": allocation,
            "domain_counts": dict(domains),
            "non_instrumental_allocation": {
                "unmeasured": {
                    "reason": "requires calibrated content/goal labels with source references; domain visits alone do not establish non-instrumentality"
                }
            },
            "source_run_ids": sorted(runs),
            "scientific_gate_passed": False,
        }

    def schedule_probe(self, item_id, *, platform, url, interval_s=86400):
        item = self._item(item_id)
        if item["platform"] != platform:
            raise ValueError("A probe target must match its actual footprint platform")
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

    def cancel_probe(self, target_id):
        target = self.db.execute(
            "SELECT item_id FROM footprint_probes WHERE target_id=?", (target_id,)
        ).fetchone()
        if target is None:
            raise KeyError("Unknown footprint probe target")
        item = self._item(target["item_id"])
        self.db.execute("DELETE FROM footprint_probes WHERE target_id=?", (target_id,))
        self._audit(item["run_id"], "footprint_probe_cancelled", target_id=target_id)
        return {"target_id": target_id, "cancelled": True}

    async def probe_due(self, *, life_id=None, force=False):
        results = []
        for target in self.db.execute("SELECT * FROM footprint_probes").fetchall():
            item = self._item(target["item_id"])
            if life_id and item["life_id"] != life_id:
                continue
            lives = getattr(self.world, "services", {}).get("lives")
            if item["life_id"] and lives and lives.get(item["life_id"]).get("state") == "ended":
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
            if response.get("kind") == "blocked" or response.get("unmeasured"):
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
        for adapter in self.probe_adapters.values():
            if hasattr(adapter, "aclose"):
                await adapter.aclose()
        await super().aclose()

    async def handle(self, run_id, action_id, tool, params):
        if tool != "footprint":
            raise ValueError("Unknown footprint tool")
        return {"kind": "result", "items": self.query(run_id, venue=params.get("venue"))}
