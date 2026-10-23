"""Passive projections for the trusted operator application.

These readers never use agent tools, refresh status files, expire tickets,
initialize belief stores, advance inbox cursors, or purchase model calls.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SECRET_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "set_cookie",
    "password",
    "secret",
    "private_key",
    "credentials",
    "native_auth",
    "auth",
    "env",
    "environment",
    "headers",
    "token",
    "key",
}
_SECRET_TEXT = re.compile(
    r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|Bearer\s+[A-Za-z0-9._~+/=-]{12,})",
    re.IGNORECASE,
)
_MODEL_CATEGORIES = {"model", "model.harness", "model.native", "harness"}
_FINAL = {"sealed", "ended", "killed"}


def _safe(value):
    """Remove authentication material from structured operator-visible content."""
    if isinstance(value, dict):
        return {
            key: _safe(item)
            for key, item in value.items()
            if not (
                str(key).lower().replace("-", "_") in _SECRET_KEYS
                or (
                    isinstance(item, (str, dict, list))
                    and str(key).lower().endswith(("_token", "_secret", "_key"))
                )
            )
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return _url(value)
        return _SECRET_TEXT.sub("[redacted]", value)
    return value


def _url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    query = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(
            part in key.lower()
            for part in ("token", "key", "auth", "secret", "password", "credential")
        )
    ]
    host = parsed.hostname
    if ":" in host:
        host = "[" + host + "]"
    try:
        if parsed.port:
            host += ":" + str(parsed.port)
    except ValueError:
        return None
    return _SECRET_TEXT.sub(
        "[redacted]", urlunsplit((parsed.scheme, host, parsed.path, urlencode(query), ""))
    )


def _time(value):
    try:
        instant = datetime.fromisoformat(value)
        return instant.astimezone(UTC) if instant.tzinfo else None
    except (TypeError, ValueError):
        return None


def _sum(values):
    return float(sum((Decimal(str(value)) for value in values), Decimal(0)))


class RunViews:
    def __init__(self, world):
        self.world, self.store = world, world.store

    def _records(self, run_id, *, since=0, limit=100, latest=False):
        if not isinstance(since, int) or isinstance(since, bool) or since < 0:
            raise ValueError("since must be a nonnegative record sequence")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between one and five hundred")
        self.store.get_run(run_id)
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT payload FROM records WHERE run_id=? AND seq>? ORDER BY seq "
                + ("DESC" if latest else "ASC")
                + " LIMIT ?",
                (run_id, since, limit + 1),
            ).fetchall()
        records = [json.loads(row[0]) for row in rows[:limit]]
        return (list(reversed(records)) if latest else records), len(rows) > limit

    def _costs(self, run):
        config, run_id = run["config"], run["run_id"]
        actual = self.store.spend(run_id)
        subscription = config.get("model_billing_mode") == "subscription"
        model = _sum(value for key, value in actual.items() if key in _MODEL_CATEGORIES)
        # A market stake is money placed in the world, not the price of a tool.
        stakes = _sum(value for key, value in actual.items() if key == "market")
        tools = _sum(
            value
            for key, value in actual.items()
            if key not in _MODEL_CATEGORIES and key != "market"
        )
        recorded = _sum(actual.values())
        billing = {
            "mode": "subscription" if subscription else "api_key",
            "model_cost_measured": not subscription,
            "model_cost_usd": None if subscription else model,
            "cap_scope": "world_tools" if subscription else "all_metered_spend",
            "cap_spent_usd": tools if subscription else recorded,
            "reason": "Native subscription model usage is not priced by this ledger; the dollar cap covers world tools only."
            if subscription
            else None,
        }
        tokens = Counter()
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT payload FROM records WHERE run_id=? AND json_extract(payload,'$.kind')='model_call' ORDER BY seq",
                (run_id,),
            ).fetchall()
        for row in rows:
            usage = json.loads(row[0]).get("usage") or {}
            for name, aliases in {
                "input": ("input", "input_tokens", "prompt_tokens"),
                "output": ("output", "output_tokens", "completion_tokens"),
                "cache_read": ("cache_read", "cache_read_input_tokens"),
                "cache_write": ("cache_write", "cache_creation_input_tokens"),
            }.items():
                value = next((usage[key] for key in aliases if key in usage), None)
                if isinstance(value, (float, int)) and not isinstance(value, bool):
                    tokens[name] += value
        costs = {
            "status": "partial" if subscription else "measured",
            "usd_total": None if subscription else recorded,
            "model_usd": None if subscription else model,
            "world_tools_usd": tools,
            "market_stakes_usd": stakes,
            "recorded_usd": recorded,
            "by_category": {
                key: None if subscription and key in _MODEL_CATEGORIES else value
                for key, value in actual.items()
            },
            "reserved_usd": self.store.reserved(run_id),
            "tokens": dict(tokens) if tokens else None,
        }
        return billing, costs

    def _ticket_rows(self, run_id):
        service = self.world.services.get("asks")
        if service is None:
            return []
        # list_tickets() expires tickets. A product read must not do that.
        with service.database.lock:
            rows = service.database.db.execute(
                "SELECT * FROM asks WHERE run_id=? ORDER BY opened_at,ticket_id", (run_id,)
            ).fetchall()
        return [_safe(service._public(row)) for row in rows]

    def _summary(self, run, *, billing=None, costs=None):
        config = run["config"]
        if billing is None or costs is None:
            billing, costs = self._costs(run)
        goal = config.get("goal")
        if isinstance(goal, dict):
            goal = goal.get("text")
        name = config.get("name")
        if not isinstance(name, str) or not name.strip():
            text = " ".join(goal.split()) if isinstance(goal, str) else ""
            name = (text[:77] + "…" if len(text) > 80 else text) or "No objective"
        return _safe(
            {
                "run_id": run["run_id"],
                "name": name,
                "goal": goal,
                "state": run["state"],
                "model": config.get("model_snapshot_id"),
                "harness_id": config.get("harness_id"),
                "created_at": run["created_at"],
                "billing": billing,
                "spent_usd": costs["usd_total"],
                "budget_usd": config.get("budget", {}).get("usd"),
                "pending_asks": sum(
                    ticket["state"] in {"open", "accepted"}
                    for ticket in self._ticket_rows(run["run_id"])
                ),
                "tainted": run.get("tainted", False),
                "life_id": config.get("life_id"),
            }
        )

    def list_runs(self):
        return {
            "runs": [
                self._summary(run)
                for run in reversed(self.store.list_runs())
                if not any(
                    run["config"].get(key)
                    for key in ("measurement_of", "world_accounting", "private_archive")
                )
            ]
        }

    def get(self, run_id):
        source = self.store.get_run(run_id)
        config = source["config"]
        billing, costs = self._costs(source)
        run = self._summary(source, billing=billing, costs=costs)
        run.update(
            _safe(
                {
                    key: config.get(key)
                    for key in (
                        "tuple",
                        "packs",
                        "mode",
                        "family",
                        "research_mode",
                        "iota",
                        "learning_mechanism",
                        "reflection_policy",
                        "continuity",
                        "vault_mode",
                        "reply_policy",
                        "budget_visible",
                        "operator_messages_allowed",
                        "isolation_profile",
                        "capture_model_io",
                        "seed",
                        "seed_scope",
                    )
                }
            )
        )
        recent, _ = self._records(run_id, latest=True)
        end = source.get("end")
        start = _time(source.get("activated_at", source["created_at"]))
        stop = (
            next(
                (
                    _time(row["ts"])
                    for row in reversed(recent)
                    if row["kind"] in {"beam_up", "seal"}
                ),
                None,
            )
            if source["state"] in _FINAL
            else datetime.now(UTC)
        )
        elapsed = max(0, (stop - start).total_seconds()) if stop and start else None
        if source["state"] == "preflight":
            elapsed = 0
        budget = config.get("budget", {})
        wall = budget.get("wall_s")
        cap = budget.get("usd")
        limits = {
            "budget_usd": cap,
            "cap_scope": billing["cap_scope"],
            "remaining_usd": max(0, cap - billing["cap_spent_usd"] - costs["reserved_usd"])
            if isinstance(cap, (float, int))
            else None,
            "wall_s": wall,
            "max_in_flight": config.get("max_in_flight"),
        }
        status = {
            "kind": "state",
            "run_id": run_id,
            "state": source["state"],
            "tuple": run.get("tuple"),
            "tainted": source.get("tainted", False),
            "end": _safe(end),
            "in_flight": self.world._in_flight(run_id),
            "inbox_pending": self.store.inbox_pending(run_id, self.world.cursors.get(run_id, 0)),
            "pending_asks": run["pending_asks"],
            "elapsed_s": elapsed,
            "wall_left_s": max(0, wall - elapsed)
            if isinstance(wall, (float, int)) and elapsed is not None
            else None,
            "spent": costs["by_category"],
            "cap": cap,
            "reserved": costs["reserved_usd"],
        }
        return {
            "run": run,
            "status": status,
            "billing": billing,
            "costs": costs,
            "limits": limits,
            "timeline": [self._event(row) for row in recent],
            "timeline_cursor": recent[-1]["seq"] if recent else 0,
        }

    @staticmethod
    def _event(row):
        kind = row["kind"]
        event = {
            "seq": row["seq"],
            "at": row["ts"],
            "kind": kind,
            "record_hash": row["hash"],
            "title": kind.replace("_", " "),
        }
        for key in (
            "action_id",
            "call_id",
            "ticket_id",
            "tool",
            "state",
            "category",
            "reason",
            "rule_id",
            "model",
        ):
            if key in row:
                event[key] = row[key]
        if kind == "ledger_debit":
            event["cost_usd"] = row["amount_nano"] / 1e9
        if kind == "action":
            params = row.get("params") or {}
            event["url"] = _url(params.get("url"))
            if row.get("tool") == "search":
                event["query"] = params.get("query")
        elif kind == "outcome":
            reply = row.get("reply") or row.get("resource") or {}
            event["outcome"] = reply.get("kind", row.get("reply_kind"))
            event["blocked_type"] = reply.get("type")
            event["url"] = _url(reply.get("final_url") or reply.get("url"))
        elif kind == "event_pushed":
            event["event_type"] = (row.get("event") or {}).get("type")
        elif kind == "model_call":
            event["usage"] = {
                key: value
                for key, value in (row.get("usage") or {}).items()
                if key
                in {
                    "input",
                    "output",
                    "cache_read",
                    "cache_write",
                    "cache_write_1h",
                    "input_tokens",
                    "output_tokens",
                    "prompt_tokens",
                    "completion_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                }
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            } or None
        if kind == "action":
            event["title"] = "Requested " + str(event.get("tool") or "world action")
            event["detail"] = event.get("url") or event.get("query")
        elif kind == "outcome":
            event["title"] = "World result" + (
                ": " + event["outcome"] if event.get("outcome") else ""
            )
            event["detail"] = event.get("url") or event.get("blocked_type")
        elif kind == "ledger_debit":
            event["title"] = "Charge recorded"
            amount = f"{event['cost_usd']:.9f}".rstrip("0").rstrip(".")
            event["detail"] = str(event.get("category") or "Recorded spending") + " · $" + amount
        elif kind == "model_call":
            event["title"] = "Model call" + (
                " · " + str(event["model"]) if event.get("model") else ""
            )
        elif kind == "event_pushed":
            event["detail"] = (event.get("event_type") or "").replace("_", " ") or None
        return _safe(event)

    def events(self, run_id, since=0, limit=100):
        rows, more = self._records(run_id, since=since, limit=limit)
        return {
            "run_id": run_id,
            "events": [self._event(row) for row in rows],
            "next_seq": rows[-1]["seq"] if rows else since,
            "has_more": more,
        }

    def asks(self, run_id):
        self.store.get_run(run_id)
        return {"run_id": run_id, "tickets": self._ticket_rows(run_id)}

    def footprint(self, run_id):
        self.store.get_run(run_id)
        service = self.world.services.get("footprint")
        items = service.query(run_id=run_id, include_incidents=True) if service else []
        # A life query includes siblings; the run view labels only this run's work.
        return {
            "run_id": run_id,
            "items": [_safe(item) for item in items if item["run_id"] == run_id],
            "available": service is not None,
        }

    def beliefs(self, run_id, query="all", entity=None, id=None):
        from .beliefs import BeliefStore

        self.store.get_run(run_id)
        if query not in {"all", "about", "history", "why"}:
            raise ValueError("unsupported operator belief query")
        if query == "about" and (not isinstance(entity, str) or not entity.strip()):
            raise ValueError("about requires an entity")
        if query in {"history", "why"} and (not isinstance(id, str) or not id):
            raise ValueError("belief id is required")
        service = self.world.services.get("beliefs")
        path = self.store.run_dir(run_id) / "beliefs/events.jsonl"
        if service is None or not path.is_file():
            return {
                "run_id": run_id,
                "beliefs": [],
                "available": False,
                "reason": "No existing belief store for this run.",
            }
        if path.resolve() != path:
            raise ValueError("belief view cannot follow symbolic links")
        reader = object.__new__(BeliefStore)
        reader.run_id, reader.log_path, reader._lock = run_id, path, threading.RLock()
        reader.enable_flags = False
        projection = copy.copy(service)
        projection._stores = {run_id: reader}
        if query == "history":
            records = reader.history(id)
        else:
            records = projection.fold(run_id)
            if query == "about":
                records = [
                    record
                    for record in records
                    if record["status"] != "retracted"
                    and (
                        entity.casefold() in record["claim"].casefold()
                        or entity in (record.get("triple") or {}).values()
                    )
                ]
            if query == "why":
                records = [record for record in records if record["id"] == id]
        return {"run_id": run_id, "beliefs": _safe(records), "available": True, "reason": None}

    def ledger(self, run_id):
        source = self.store.get_run(run_id)
        billing, costs = self._costs(source)
        with self.store._lock:
            entries = self.store.db.execute(
                "SELECT payload FROM records WHERE run_id=? AND json_extract(payload,'$.kind')='ledger_debit' ORDER BY seq",
                (run_id,),
            ).fetchall()
            reservations = self.store.db.execute(
                "SELECT state,amount_nano,actual_nano,created_at FROM reservations WHERE run_id=? ORDER BY created_at,reservation_id",
                (run_id,),
            ).fetchall()
        return {
            "run_id": run_id,
            "billing": billing,
            "costs": costs,
            "entries": [self._event(json.loads(row[0])) for row in entries],
            "reservations": [
                {
                    "state": row["state"],
                    "usd": row["amount_nano"] / 1e9,
                    "actual_usd": row["actual_nano"] / 1e9
                    if row["actual_nano"] is not None
                    else None,
                    "created_at": row["created_at"],
                }
                for row in reservations
            ],
        }
