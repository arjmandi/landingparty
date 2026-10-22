"""Durable asynchronous expectations and world-seeded forecast questions.

Pending expectations belong to the world, not an agent process. They are not
cancelled when a run is sealed; later outcomes use the extension journal.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .beliefs import BeliefValidationError, _canonical, _stamp, _time
from .kernel import ExtensionDB
from .util import blocked, digest, identifier

_RESOLVERS = {"watch", "inbox", "fetch", "footprint_probe", "ticket", "human"}
_COMPARATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "exists"}
_PREFIXES = ("world.", "ticket.", "ask.", "footprint.", "account.", "driver.")
FORECAST_POLICY_ID = "world-observed-replay-final-grace@2"


def _compare(actual, expected, comparator):
    if comparator == "exists":
        return actual is not None
    if comparator in {"eq", "ne"}:
        equal = actual == expected and isinstance(actual, bool) == isinstance(expected, bool)
        return equal if comparator == "eq" else not equal
    if comparator == "contains":
        return expected in actual
    return {
        "gt": lambda: actual > expected,
        "gte": lambda: actual >= expected,
        "lt": lambda: actual < expected,
        "lte": lambda: actual <= expected,
    }[comparator]()


class ForecastService:
    def __init__(
        self, world, *, clock=None, state_reader=None, median_reader=None, poll_seconds=1.0
    ):
        self.world = world
        self.store = world.store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.state_reader = state_reader
        self.median_reader = median_reader
        self.poll_seconds = poll_seconds
        self.database = ExtensionDB(self.store)
        self.database.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS expectations (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, state TEXT NOT NULL,
                outcome TEXT NOT NULL, deadline TEXT NOT NULL, next_check TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS pending_expectations ON expectations(outcome,next_check);
            CREATE TABLE IF NOT EXISTS forecast_questions (
                question_id TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS expectation_bindings (
                run_id TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(run_id,state)
            );
            CREATE TABLE IF NOT EXISTS expectation_observations (
                ref TEXT PRIMARY KEY, run_id TEXT NOT NULL, state TEXT NOT NULL,
                observed_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS expectation_state_write_queue (
                ref TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL
            );
            """
        )
        self._worker = None
        self._closing = False
        self._lock = asyncio.Lock()

    def register_question(self, question_id: str, record: dict) -> dict:
        """Researcher-only immutable question registration, including its live median."""
        if not isinstance(question_id, str) or not question_id:
            raise BeliefValidationError("question id must be nonempty")
        record = copy.deepcopy(record)
        self._validate_state(record.get("state"))
        if record.get("resolver", "fetch") not in _RESOLVERS:
            raise BeliefValidationError("invalid question resolver")
        if record.get("cmp", "eq") not in _COMPARATORS:
            raise BeliefValidationError("invalid question comparator")
        if "binary" in record and not isinstance(record["binary"], bool):
            raise BeliefValidationError("question binary flag must be boolean")
        median = record.get("community_median")
        if median is not None and (
            isinstance(median, bool)
            or not isinstance(median, (int, float))
            or not math.isfinite(median)
            or (record.get("binary", True) and not 0 <= median <= 1)
        ):
            raise BeliefValidationError(
                "community median must be finite and match the question type"
            )
        if record.get("community_median_observed_at") is not None:
            if _time(record["community_median_observed_at"]) > _time(self.clock()):
                raise BeliefValidationError("community median observation cannot be in the future")
            record["community_median_observed_at"] = _stamp(record["community_median_observed_at"])
        if record.get("resolve_at") is not None:
            record["resolve_at"] = _stamp(record["resolve_at"])
        encoded = _canonical(record)
        with self.database.lock:
            prior = self.database.db.execute(
                "SELECT payload FROM forecast_questions WHERE question_id=?", (question_id,)
            ).fetchone()
            if prior and prior[0] != encoded:
                raise BeliefValidationError(
                    "question registration is immutable; use a new question id"
                )
            self.database.db.execute(
                "INSERT OR IGNORE INTO forecast_questions VALUES(?,?)", (question_id, encoded)
            )
        return {"question_id": question_id, **record}

    @staticmethod
    def _validate_state(state):
        if (
            not isinstance(state, str)
            or len(state) > 1000
            or not re.fullmatch(r"[\w-]+(?:\.[\w-]+)+", state)
        ):
            raise BeliefValidationError("state must use the addressable world namespace")
        parts = state.split(".")
        valid = (
            (parts[0] in {"world", "driver"} and len(parts) >= 2)
            or (parts[0] == "ticket" and len(parts) >= 3)
            or (parts[0] == "ask" and len(parts) == 3 and parts[-1] == "outcome")
            or (parts[0] == "footprint" and len(parts) >= 4)
            or (parts[0] == "account" and len(parts) == 3 and parts[-1] == "state")
        )
        if not valid:
            raise BeliefValidationError("state does not match an addressable namespace shape")

    def policy_identity(self, settings=None):
        """Pure registered identity; root hashes this descriptor into the run tuple."""
        return {
            "policy_id": FORECAST_POLICY_ID,
            "poll_seconds": self.poll_seconds,
            "default_interval_s": 3600,
            "grace_days": 1,
            "final_lateness_max_s": 3600,
            "late_final_headline_eligible": False,
            "namespace_id": "addressable-state-shapes@1",
            "questions_hash": digest((settings or {}).get("forecast_questions", {})),
        }

    def bind_state(self, run_id: str, state: str, resolver: str, config: dict) -> dict:
        """Researcher-only binding from an addressable state to a concrete resolver.

        A fetch binding is ``{url, selector: [json keys/array indexes], interval_s}``.
        Tool calls cannot introduce a resolver URL or executable expression.
        """
        self.store.get_run(run_id)
        self._validate_state(state)
        if resolver not in _RESOLVERS:
            raise BeliefValidationError("unknown resolver")
        binding = {"resolver": resolver, **copy.deepcopy(config)}
        if "interval_s" in binding and (
            not isinstance(binding["interval_s"], (float, int))
            or not math.isfinite(binding["interval_s"])
            or binding["interval_s"] < 2
        ):
            raise BeliefValidationError("resolver interval must be at least two seconds")
        if "selector" in binding and (
            not isinstance(binding["selector"], list)
            or any(
                not isinstance(key, (str, int)) or isinstance(key, bool)
                for key in binding["selector"]
            )
        ):
            raise BeliefValidationError("selector must be a JSON path of keys and indexes")
        with self.database.lock:
            self.database.db.execute(
                "INSERT OR REPLACE INTO expectation_bindings VALUES(?,?,?)",
                (run_id, state, _canonical(binding)),
            )
        self.database.audit(run_id, "expectation_resolver_bound", state=state, resolver=resolver)
        return binding

    def _binding(self, run_id, state):
        with self.database.lock:
            row = self.database.db.execute(
                "SELECT payload FROM expectation_bindings WHERE run_id=? AND state=?",
                (run_id, state),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def open(
        self, run_id: str, record: dict, *, action_id: str | None = None, trusted_question=False
    ) -> dict:
        self.store.get_run(run_id)
        supplied = copy.deepcopy(record)
        if supplied.keys() & {
            "id",
            "opened_at",
            "resolved_at",
            "outcome",
            "observation_ref",
            "recorded_at",
        }:
            raise BeliefValidationError("expectation system fields are world-owned")
        fields = {
            "state",
            "cmp",
            "value",
            "p",
            "horizon",
            "resolver",
            "about_action",
            "assumptions",
        }
        if trusted_question:
            fields |= {
                "question_id",
                "community_median",
                "community_median_provenance",
                "interval",
                "not_before",
            }
        if supplied.keys() - fields:
            raise BeliefValidationError("unknown expectation fields")
        self._validate_state(supplied.get("state"))
        if supplied.get("cmp", "eq") not in _COMPARATORS:
            raise BeliefValidationError("invalid expectation comparator")
        resolver = supplied.get("resolver", "fetch")
        if resolver not in _RESOLVERS:
            raise BeliefValidationError("invalid expectation resolver")
        probability = supplied.get("p")
        if probability is not None and (
            isinstance(probability, bool)
            or not isinstance(probability, (float, int))
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            raise BeliefValidationError("p must be a finite probability from zero to one")
        assumptions = supplied.get("assumptions", [])
        if (
            not isinstance(assumptions, list)
            or len(assumptions) > 4
            or any(not isinstance(item, str) or not item for item in assumptions)
            or len(set(assumptions)) != len(assumptions)
        ):
            raise BeliefValidationError("at most four distinct ticket assumptions")
        beliefs = getattr(self.world, "services", {}).get("beliefs")
        if assumptions and beliefs is not None:
            beliefs._check_assumptions(run_id, supplied)
        outstanding = {
            ticket for expectation in self.pending(run_id) for ticket in expectation["assumptions"]
        }
        if beliefs is not None:
            outstanding.update(
                ticket
                for belief in beliefs.fold(run_id)
                if belief["status"] in {"held", "conditional", "review", "proposed"}
                for ticket in belief.get("assumptions", [])
            )
        outstanding = {
            ticket
            for ticket in outstanding
            if (self._local_state(run_id, f"ticket.{ticket}.state") or {}).get("value", "open")
            in {"open", "accepted"}
        }
        if len(outstanding | set(assumptions)) > 64:
            raise BeliefValidationError("at most 64 distinct open expectation assumptions per run")
        opened = _time(self.clock())
        horizon = supplied.get("horizon", 14)
        if isinstance(horizon, str):
            deadline = _time(horizon)
        elif (
            isinstance(horizon, (int, float))
            and not isinstance(horizon, bool)
            and math.isfinite(horizon)
        ):
            deadline = opened + timedelta(days=horizon)
        else:
            raise BeliefValidationError("horizon must be days or an ISO deadline")
        if deadline <= opened or deadline > opened + timedelta(days=60):
            raise BeliefValidationError("horizon must be in the next 60 days")
        if supplied.get("not_before") and _time(supplied["not_before"]) > deadline:
            raise BeliefValidationError(
                "question resolution time postdates the expectation horizon"
            )
        expected = {
            **supplied,
            "id": identifier("ex"),
            "run_id": run_id,
            "action_id": action_id,
            "cmp": supplied.get("cmp", "eq"),
            "resolver": resolver,
            "assumptions": assumptions,
            "opened_at": _stamp(opened),
            "horizon": _stamp(deadline),
            "outcome": "open",
            "grounded": resolver != "human",
        }
        encoded = _canonical(expected)
        with self.database.lock:
            self.database.db.execute(
                "INSERT INTO expectations VALUES(?,?,?,?,?,?,?)",
                (
                    expected["id"],
                    run_id,
                    expected["state"],
                    "open",
                    _stamp(deadline),
                    _stamp(opened),
                    encoded,
                ),
            )
        self.database.audit(run_id, "expectation_open", expectation=expected, action_id=action_id)
        return copy.deepcopy(expected)

    def get(self, expectation_id):
        with self.database.lock:
            row = self.database.db.execute(
                "SELECT payload FROM expectations WHERE id=?", (expectation_id,)
            ).fetchone()
        if not row:
            raise KeyError(expectation_id)
        return json.loads(row[0])

    def pending(self, run_id=None):
        with self.database.lock:
            rows = self.database.db.execute(
                "SELECT payload FROM expectations WHERE outcome='open'"
                + (" AND run_id=?" if run_id else "")
                + " ORDER BY deadline,id",
                (run_id,) if run_id else (),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _scope_runs(self, run_id):
        life_id = self.store.get_run(run_id)["config"].get("life_id")
        if not life_id:
            return [run_id]
        return [
            run["run_id"]
            for run in self.store.list_runs()
            if run["config"].get("life_id") == life_id
        ]

    def _finish(
        self, expectation, outcome, *, observation_ref=None, value=None, reason=None, metadata=None
    ):
        prior = self.get(expectation["id"])
        result = {
            key: value
            for key, value in prior.items()
            if key
            not in {
                "observation_ref",
                "observed_value",
                "reason",
                "resolution_observed_at",
                "late_final",
                "scheduled_grace_at",
                "late_by_seconds",
                "headline_eligible",
            }
        }
        result.update(
            outcome=outcome,
            resolved_at=_stamp(self.clock()),
            recorded_at=_stamp(self.clock()),
            valid_from=expectation["opened_at"],
        )
        result.update(metadata or {})
        if observation_ref:
            result["observation_ref"] = observation_ref
            result["observed_value"] = value
        if reason:
            result["reason"] = reason
        with self.database.lock:
            changed = self.database.db.execute(
                "UPDATE expectations SET outcome=?,payload=? WHERE id=? AND outcome=?",
                (outcome, _canonical(result), expectation["id"], prior["outcome"]),
            ).rowcount
        if not changed:
            return self.get(expectation["id"])
        event = self.database.audit(
            expectation["run_id"],
            "expectation_resolved"
            if prior["outcome"] == "open"
            else "expectation_resolution_replayed",
            expectation=result,
            previous_outcome=prior["outcome"],
        )
        self.world.push(
            expectation["run_id"],
            {
                "type": "expectation_resolved",
                "expectation": result,
                "observation_ref": event["event_id"],
            },
        )
        return result

    def _decision(self, expectation, observations):
        """Fold the first eligible resolution in trusted observed-time order."""
        opened, grace = (
            _time(expectation["opened_at"]),
            _time(expectation["horizon"]) + timedelta(days=1),
        )
        assumptions, latest = {}, None
        for observation in observations:
            instant = _time(observation["observed_at"])
            final = (
                observation.get("final_check_for") == expectation["id"]
                and bool(expectation.get("final_check_started_at"))
                and instant <= grace + timedelta(seconds=expectation.get("final_lateness_max_s", 0))
            )
            if instant > grace and not final:
                continue
            for ticket in expectation["assumptions"]:
                if observation["state"] == f"ask.{ticket}.outcome":
                    assumptions[ticket] = observation["value"]
            if instant < opened:
                continue
            if any(
                value in {"rejected", "expired", "ignored", "cancelled"}
                for value in assumptions.values()
            ):
                return {
                    "outcome": "void",
                    "reason": "assumption_not_met",
                    "metadata": {"resolution_observed_at": observation["observed_at"]},
                }
            if (
                observation["state"] == expectation["state"]
                and observation["resolver"] == expectation["resolver"]
                and (
                    not expectation.get("not_before") or instant >= _time(expectation["not_before"])
                )
            ):
                latest = observation
            if latest is None or not all(
                assumptions.get(ticket) in {"approved", "deposited", "complete", "held"}
                for ticket in expectation["assumptions"]
            ):
                continue
            try:
                held = bool(_compare(latest["value"], expectation.get("value"), expectation["cmp"]))
            except (TypeError, ValueError, KeyError):
                continue
            metadata = {
                "resolution_observed_at": observation["observed_at"],
                "late_final": instant > grace,
                "headline_eligible": instant <= grace,
                "grounded": expectation["resolver"] != "human"
                and latest.get("grounded_eligible", True),
                "author_class": latest.get("author_class"),
                "reading_ref": latest.get("reading_ref"),
            }
            if final:
                metadata.update(
                    scheduled_grace_at=_stamp(grace),
                    late_by_seconds=max(0, (instant - grace).total_seconds()),
                )
            return {
                "outcome": "held" if held else "missed",
                "observation_ref": latest["ref"],
                "value": latest["value"],
                "metadata": metadata,
            }
        return None

    async def observe(
        self,
        run_id: str,
        state: str,
        value: Any = None,
        observation_ref: str | None = None,
        *,
        observed_at=None,
        resolver: str = "fetch",
        ticket_id=None,
        outcome=None,
        final_check_for=None,
        world_ref=None,
        author_class=None,
        grounded_eligible=None,
        reading_ref=None,
    ) -> list[dict]:
        """Record a world-state write, then resolve eligible expectations in code."""
        self.store.get_run(run_id)
        self._validate_state(state)
        if resolver not in _RESOLVERS:
            raise BeliefValidationError("invalid resolver")
        observation_ref = observation_ref or identifier("fo")
        instant = _time(observed_at or self.clock())
        if instant > _time(self.clock()):
            raise BeliefValidationError("observation cannot postdate world recording")
        observation = {
            "ref": observation_ref,
            "run_id": run_id,
            "state": state,
            "value": value,
            "observed_at": _stamp(instant),
            "resolver": resolver,
            "recorded_at": _stamp(self.clock()),
        }
        if final_check_for:
            observation["final_check_for"] = final_check_for
        if world_ref:
            observation["world_ref"] = world_ref
        if author_class is not None:
            observation["author_class"] = author_class
        if grounded_eligible is not None:
            if not isinstance(grounded_eligible, bool):
                raise BeliefValidationError(
                    "grounded eligibility must be world-authored boolean provenance"
                )
            observation["grounded_eligible"] = grounded_eligible
        if reading_ref:
            observation["reading_ref"] = reading_ref
        encoded = _canonical(observation)
        async with self._lock:
            with self.database.lock:
                prior = self.database.db.execute(
                    "SELECT payload FROM expectation_observations WHERE ref=?", (observation_ref,)
                ).fetchone()
                if prior:
                    old = json.loads(prior[0])
                    if observed_at is None:
                        observation["observed_at"] = old["observed_at"]
                    if {key: val for key, val in old.items() if key != "recorded_at"} != {
                        key: val for key, val in observation.items() if key != "recorded_at"
                    }:
                        raise BeliefValidationError("observation identity cannot be reused")
                else:
                    self.database.db.execute(
                        "INSERT INTO expectation_observations VALUES(?,?,?,?,?)",
                        (
                            observation_ref,
                            run_id,
                            state,
                            observation["observed_at"],
                            encoded,
                        ),
                    )
                    self.database.audit(run_id, "expectation_observation", observation=observation)
            resolved = []
            scope_runs = self._scope_runs(run_id)
            placeholders = ",".join("?" for _ in scope_runs)
            with self.database.lock:
                observations = [
                    json.loads(row[0])
                    for row in self.database.db.execute(
                        f"SELECT payload FROM expectation_observations WHERE run_id IN ({placeholders}) ORDER BY observed_at,ref",
                        scope_runs,
                    )
                ]
                expectations = [
                    json.loads(row[0])
                    for row in self.database.db.execute(
                        f"SELECT payload FROM expectations WHERE run_id IN ({placeholders})",
                        scope_runs,
                    )
                ]
            for expectation in expectations:
                decision = self._decision(expectation, observations)
                if not decision:
                    continue
                fields = {
                    "outcome": decision["outcome"],
                    "observation_ref": decision.get("observation_ref"),
                    "observed_value": decision.get("value"),
                    "reason": decision.get("reason"),
                    **decision.get("metadata", {}),
                }
                if all(expectation.get(key) == val for key, val in fields.items()):
                    continue
                resolved.append(self._finish(expectation, **decision))
            return resolved

    async def observe_event(self, run_id: str, event: dict) -> list[dict]:
        """Adapt durable world outcomes/inbox writes into addressable state writes."""
        writes = []
        explicit = event.get("state")
        if isinstance(explicit, str) and explicit.startswith(_PREFIXES) and "value" in event:
            writes.append((explicit, event["value"], event.get("resolver", "fetch")))
        if event.get("ticket_id") and (event.get("type") == "ask_resolved" or "outcome" in event):
            writes.append(
                (
                    f"ask.{event['ticket_id']}.outcome",
                    event.get("outcome", event.get("state")),
                    "ticket",
                )
            )
        reply = event.get("reply", {})
        if isinstance(reply, dict):
            if "status" in reply:
                writes.append(("world.last.status", reply["status"], "fetch"))
            if isinstance(reply.get("spent"), dict):
                spent = reply["spent"].get("by_category", reply["spent"])
                writes.extend(
                    (f"world.spent.{key}", value, "inbox") for key, value in spent.items()
                )
            for key in ("cap", "wall_left_s", "in_flight", "inbox_pending"):
                if key in reply:
                    writes.append((f"world.{key}", reply[key], "inbox"))
        result = []
        identity = (
            event.get("observation_ref")
            or event.get("event_id")
            or event.get("action_id")
            or identifier("state")
        )
        origin = event.get("source_run_id", run_id)
        if origin not in self._scope_runs(run_id):
            origin = run_id
        for state, value, resolver in writes:
            reference = (
                "fo_" + digest({"ref": identity, "state": state, "run_id": origin}).split(":", 1)[1]
            )
            result += await self.observe(
                origin,
                state,
                value,
                reference,
                observed_at=event.get("observed_at"),
                resolver=resolver,
                ticket_id=event.get("ticket_id"),
                outcome=event.get("outcome", event.get("state")),
                **{
                    key: event[key]
                    for key in ("author_class", "grounded_eligible", "reading_ref")
                    if key in event
                },
            )
        # Registered local states are refreshed after actual world writes too;
        # this path never follows a URL or reads arbitrary service attributes.
        for source_run in self._scope_runs(run_id):
            for expectation in self.pending(source_run):
                local = self._local_state(source_run, expectation["state"])
                if local is not None and expectation["resolver"] != "human":
                    reference = (
                        "fo_"
                        + digest(
                            {
                                "event": identity,
                                "state": expectation["state"],
                                "value": local["value"],
                                "run_id": source_run,
                            }
                        ).split(":", 1)[1]
                    )
                    result += await self.observe(
                        source_run,
                        expectation["state"],
                        local["value"],
                        reference,
                        observed_at=event.get("observed_at") or event.get("ts"),
                        resolver=expectation["resolver"],
                        **{
                            key: local[key]
                            for key in ("author_class", "grounded_eligible", "reading_ref")
                            if key in local
                        },
                    )
        return result

    def notify_state_write(self, *, run_id=None, life_id=None, world_ref):
        """Snapshot committed local writes before asynchronous work can overtake them.

        Only trusted service code calls this hook. The durable queue also keeps
        synchronous callers without a running event loop from losing a reading;
        the next resolver tick drains it using its original world capture time.
        """
        if not isinstance(world_ref, str) or not world_ref:
            raise BeliefValidationError("state writes require an authoritative world reference")
        if run_id:
            actual_life = self.store.get_run(run_id)["config"].get("life_id")
            if life_id and life_id != actual_life:
                raise BeliefValidationError("state-write run and life scopes disagree")
            scope = set(self._scope_runs(run_id))
        elif life_id:
            scope = {
                row["run_id"]
                for row in self.store.list_runs()
                if row["config"].get("life_id") == life_id
            }
        else:
            raise BeliefValidationError("state writes require a run or life scope")
        snapshots, seen = [], set()
        write_id = identifier("fw")
        for expectation in self.pending():
            owner = expectation["run_id"]
            key = (owner, expectation["state"], expectation["resolver"])
            if owner not in scope or key in seen or expectation["resolver"] == "human":
                continue
            seen.add(key)
            local = self._local_state(owner, expectation["state"])
            if local is None:
                continue
            snapshot = {
                "run_id": owner,
                "state": expectation["state"],
                "value": copy.deepcopy(local["value"]),
                "resolver": expectation["resolver"],
                "observed_at": _stamp(self.clock()),
                "observation_ref": "fo_" + digest([write_id, world_ref, key]).split(":", 1)[1],
                "world_ref": world_ref,
                **{
                    key: local[key]
                    for key in ("author_class", "grounded_eligible", "reading_ref")
                    if key in local
                },
            }
            with self.database.lock:
                self.database.db.execute(
                    "INSERT INTO expectation_state_write_queue VALUES(?,?,?)",
                    (snapshot["observation_ref"], owner, _canonical(snapshot)),
                )
            snapshots.append(snapshot)
        scheduled = False
        tracker = getattr(self.world, "track_observer", None)
        if snapshots and tracker is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                tracker(snapshots[0]["run_id"], self._drain_state_writes())
                scheduled = True
        return {"queued_n": len(snapshots), "scheduled": scheduled}

    async def _drain_state_writes(self):
        with self.database.lock:
            rows = self.database.db.execute(
                "SELECT ref,payload FROM expectation_state_write_queue ORDER BY rowid"
            ).fetchall()
        results = []
        for reference, encoded in rows:
            snapshot = json.loads(encoded)
            results.extend(await self.observe(**snapshot))
            with self.database.lock:
                self.database.db.execute(
                    "DELETE FROM expectation_state_write_queue WHERE ref=?", (reference,)
                )
        return results

    def _local_state(self, run_id, state):
        """Read only the declared, run-scoped production namespace surfaces."""
        parts, services = state.split("."), getattr(self.world, "services", {})
        scope = self._scope_runs(run_id)
        run = self.store.get_run(run_id)
        config = run["config"]
        value, provenance = None, {}
        if parts[0] == "world":
            spent = self.store.spend(run_id)
            source = {
                "state": run["state"],
                "run_id": run_id,
                "spent": spent,
                "cap": config["budget"]["usd"],
                "reserved": self.store.reserved(run_id),
                "remaining_usd": config["budget"]["usd"]
                - self.store.total_spent(run_id)
                - self.store.reserved(run_id),
                "in_flight": len(getattr(self.world, "jobs", {}).get(run_id, {})),
                "inbox_pending": self.store.inbox_pending(
                    run_id, getattr(self.world, "cursors", {}).get(run_id, 0)
                ),
            }
            if parts[1:] == ["spent", "usd"]:
                value = self.store.total_spent(run_id)
            else:
                value = source
                for key in parts[1:]:
                    if not isinstance(value, dict) or key not in value:
                        return None
                    value = value[key]
        elif parts[0] in {"ask", "ticket"}:
            asks = services.get("asks")
            ticket = None
            if asks is not None:
                try:
                    ticket = asks.get(parts[1])
                except KeyError:
                    pass
            if ticket is not None:
                if ticket.get("run_id") not in scope:
                    return None
                if parts[0] == "ask":
                    if ticket["state"] in {"open", "accepted"}:
                        return None
                    value = ticket["state"]
                else:
                    value = ticket
                    for key in parts[2:]:
                        if not isinstance(value, dict) or key not in value:
                            return None
                        value = value[key]
            elif parts[0] == "ticket" and parts[1].startswith("t_"):
                action = "a_" + parts[1][2:]
                for candidate in scope:
                    rows = [
                        row
                        for row in self.store.records(candidate)
                        if row.get("action_id") == action
                    ]
                    outcomes = [row for row in rows if row["kind"] == "outcome"]
                    if outcomes:
                        reply = outcomes[-1].get("reply", {})
                        value = {"state": "complete", "outcome": reply, "reply": reply, **reply}
                        for key in parts[2:]:
                            if not isinstance(value, dict) or key not in value:
                                return None
                            value = value[key]
                        break
                else:
                    return None
            else:
                return None
        elif parts[0] == "account":
            vault = services.get("vault")
            if vault is None:
                return None
            try:
                value = vault.get_account(parts[1], run_id=run_id)["state"]
            except (KeyError, ValueError):
                return None
        elif parts[0] == "driver":
            lives, life_id = services.get("lives"), config.get("life_id")
            if lives is None or not life_id:
                return None
            driver = lives.status(life_id).get("driver")
            if not driver:
                return None
            key = parts[1]
            value = (
                driver.get("reading")
                if key in {driver.get("metric"), "balance", "reading"}
                else driver.get(key)
            )
        elif parts[0] == "footprint":
            footprint = services.get("footprint")
            if footprint is None:
                return None
            items = footprint.query(run_id, venue=parts[1])
            item = next(
                (
                    item
                    for item in items
                    if parts[2] in {item.get("item_id"), item.get("external_id")}
                ),
                None,
            )
            if not item or not item.get("readings"):
                return None
            reading = max(item["readings"], key=lambda row: (row["at"], row["reading_id"]))
            value = reading["value"]
            provenance = {
                "author_class": reading.get("author_class", "unknown"),
                "grounded_eligible": reading.get("grounded_eligible", False),
                "reading_ref": reading["reading_id"],
            }
            if parts[3:] != ["value"]:
                for key in parts[3:]:
                    if not isinstance(value, dict) or key not in value:
                        return None
                    value = value[key]
        return {"value": value, **provenance} if value is not None else None

    async def _read_binding(self, run_id, binding):
        fetcher = getattr(self.world, "fetch_kernel", None)
        if fetcher is None:
            return None
        reply = await fetcher(run_id, binding["url"])
        if reply.get("kind") != "result":
            return None
        if "inline" in reply:
            raw = reply["inline"]
        elif "body" in reply:
            raw = reply["body"]
        elif reply.get("resource_path") or reply.get("handle"):
            resource_run = reply.get("observation_run_id", run_id)
            path = (
                (self.store.run_dir(resource_run) / reply["handle"].lstrip("/")).resolve()
                if not reply.get("resource_path")
                else Path(reply["resource_path"]).resolve()
            )
            root = (self.store.run_dir(resource_run) / "world" / "fetched").resolve()
            if not path.is_relative_to(root) or path.is_symlink():
                raise BeliefValidationError("resolver resource escaped fetched directory")
            raw = path.read_bytes()
        else:
            return None
        body = raw if isinstance(raw, bytes) else raw.encode()
        if reply.get("hash") and digest(body) != reply["hash"]:
            raise BeliefValidationError("resolver resource hash does not match captured bytes")
        value = json.loads(raw)
        for key in binding.get("selector", []):
            value = value[key]
        return {
            "value": value,
            "observation_ref": reply.get("observation_ref") or identifier("fo"),
            "observation_run_id": reply.get("observation_run_id", run_id),
            "resource_hash": reply.get("hash"),
            "observed_at": _stamp(self.clock()),
        }

    async def _median(self, run_id, question_id, question):
        try:
            if self.median_reader:
                observed = self.median_reader(run_id, question_id, copy.deepcopy(question))
                observed = await observed if inspect.isawaitable(observed) else observed
            elif question.get("median_binding"):
                observed = await self._read_binding(run_id, question["median_binding"])
            else:
                return question.get("community_median"), {
                    "source": "pinned_registration",
                    "observed_at": question.get("community_median_observed_at"),
                    "captured_at_call": False,
                }
            if (
                not isinstance(observed, dict)
                or isinstance(observed.get("value"), bool)
                or not isinstance(observed.get("value"), (int, float))
                or not math.isfinite(observed["value"])
            ):
                raise BeliefValidationError("community median resolver returned no finite value")
            return observed["value"], {
                "source": "world_resolver",
                "captured_at_call": True,
                **{key: value for key, value in observed.items() if key != "value"},
            }
        except (BeliefValidationError, OSError, ValueError, TypeError, KeyError):
            self.database.audit(
                run_id,
                "forecast_median_unmeasured",
                question_id=question_id,
                reason="community_median_resolver_unavailable",
            )
            return None, {
                "source": "world_resolver",
                "status": "unmeasured",
                "reason": "community_median_resolver_unavailable",
            }

    async def _read_state(self, expectation):
        run_id, state = expectation["run_id"], expectation["state"]
        if self.state_reader:
            value = self.state_reader(run_id, state, expectation["resolver"])
            return await value if inspect.isawaitable(value) else value
        binding = self._binding(run_id, state)
        if (
            binding
            and binding.get("url")
            and expectation["resolver"] in {"fetch", "watch", "footprint_probe"}
        ):
            return await self._read_binding(run_id, binding)
        reader = getattr(self.world, "state_value", None)
        if reader:
            value = reader(run_id, state)
            return await value if inspect.isawaitable(value) else value
        return self._local_state(run_id, state)

    async def tick(self, *, at=None):
        instant = _time(at or self.clock())
        results = await self._drain_state_writes()
        for expectation in self.pending():
            with self.database.lock:
                next_check = self.database.db.execute(
                    "SELECT next_check FROM expectations WHERE id=?", (expectation["id"],)
                ).fetchone()[0]
            if instant < _time(next_check):
                continue
            binding = self._binding(expectation["run_id"], expectation["state"]) or {}
            interval = float(binding.get("interval_s", 3600))
            grace = _time(expectation["horizon"]) + timedelta(days=1)
            final = instant >= grace
            if final:
                if expectation.get("final_check_started_at"):
                    results.append(
                        self._finish(expectation, "expired", reason="final_check_interrupted")
                    )
                    continue
                if instant > grace + timedelta(seconds=min(interval, 3600)):
                    self.database.audit(
                        expectation["run_id"],
                        "expectation_final_check_missed",
                        expectation_id=expectation["id"],
                        scheduled_grace_at=_stamp(grace),
                        observed_at=_stamp(self.clock()),
                    )
                    results.append(
                        self._finish(expectation, "expired", reason="scheduler_late_after_grace")
                    )
                    continue
                claimed = {
                    **expectation,
                    "final_check_started_at": _stamp(self.clock()),
                    "scheduled_grace_at": _stamp(grace),
                    "final_lateness_max_s": min(interval, 3600),
                }
                with self.database.lock:
                    changed = self.database.db.execute(
                        "UPDATE expectations SET payload=? WHERE id=? AND outcome='open' AND payload=?",
                        (_canonical(claimed), expectation["id"], _canonical(expectation)),
                    ).rowcount
                if not changed:
                    continue
                expectation = claimed
            with self.database.lock:
                self.database.db.execute(
                    "UPDATE expectations SET next_check=? WHERE id=?",
                    (
                        _stamp(
                            min(
                                instant + timedelta(seconds=interval),
                                _time(expectation["horizon"]) + timedelta(days=1),
                            )
                        ),
                        expectation["id"],
                    ),
                )
            if expectation["resolver"] != "human":
                try:
                    observation = await self._read_state(expectation)
                    if observation is not None:
                        if not isinstance(observation, dict) or "value" not in observation:
                            observation = {"value": observation}
                        results += await self.observe(
                            expectation["run_id"],
                            expectation["state"],
                            observation["value"],
                            observation.get("observation_ref"),
                            observed_at=observation.get("observed_at"),
                            resolver=expectation["resolver"],
                            final_check_for=expectation["id"] if final else None,
                            **{
                                key: observation[key]
                                for key in ("author_class", "grounded_eligible", "reading_ref")
                                if key in observation
                            },
                        )
                except (ValueError, TypeError, KeyError, OSError) as error:
                    self.database.audit(
                        expectation["run_id"],
                        "expectation_resolver_error",
                        expectation_id=expectation["id"],
                        error=type(error).__name__,
                    )
            if self.get(expectation["id"])["outcome"] == "open" and instant >= _time(
                expectation["horizon"]
            ) + timedelta(days=1):
                results.append(
                    self._finish(expectation, "expired", reason="unresolved_after_grace")
                )
        return results

    async def handle(self, run_id, action_id, tool, params):
        try:
            if tool == "expect":
                expectation = self.open(run_id, params["record"], action_id=action_id)
            elif tool == "predict":
                with self.database.lock:
                    row = self.database.db.execute(
                        "SELECT payload FROM forecast_questions WHERE question_id=?",
                        (params["question_id"],),
                    ).fetchone()
                if not row:
                    return blocked(
                        "policy_deny", "Unknown world-supplied question", "forecast_question"
                    )
                question = json.loads(row[0])
                value = params["value_or_p"]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (float, int))
                    or not math.isfinite(value)
                ):
                    raise BeliefValidationError(
                        "prediction must be a finite numeric value or probability"
                    )
                if question.get("binary", False) and not 0 <= value <= 1:
                    raise BeliefValidationError(
                        "binary prediction must be a probability from zero to one"
                    )
                interval = params.get("interval")
                if interval is not None and (
                    not isinstance(interval, list)
                    or len(interval) != 2
                    or any(
                        isinstance(bound, bool)
                        or not isinstance(bound, (float, int))
                        or not math.isfinite(bound)
                        for bound in interval
                    )
                    or interval[0] > interval[1]
                ):
                    raise BeliefValidationError(
                        "prediction interval requires two ordered finite bounds"
                    )
                median, median_provenance = await self._median(
                    run_id, params["question_id"], question
                )
                record = {
                    "state": question["state"],
                    "cmp": question.get("cmp", "eq"),
                    "value": question.get("value", True)
                    if question.get("binary", False)
                    else value,
                    "resolver": question.get("resolver", "fetch"),
                    "horizon": question.get("horizon", 14),
                    "question_id": params["question_id"],
                    "community_median": median,
                    "community_median_provenance": median_provenance,
                    "interval": params.get("interval"),
                }
                if question.get("binary", False):
                    record["p"] = value
                if question.get("resolve_at"):
                    record["not_before"] = question["resolve_at"]
                expectation = self.open(run_id, record, action_id=action_id, trusted_question=True)
                if question.get("binding"):
                    self.bind_state(
                        run_id,
                        question["state"],
                        question.get("resolver", "fetch"),
                        question["binding"],
                    )
            else:
                return blocked("policy_deny", "Unknown forecast tool", "forecast_tool")
            return {"kind": "silence", "action_id": action_id, "expectation_id": expectation["id"]}
        except (BeliefValidationError, KeyError, TypeError, ValueError) as error:
            return blocked("policy_deny", str(error), "expectation_structural_validation")

    async def _loop(self):
        while not self._closing:
            try:
                await self.tick()
            except (OSError, ValueError, sqlite3.Error):
                # Individual resolver failures are journaled by tick; a transient
                # DB lock must not destroy a durable scheduler process.
                pass
            await asyncio.sleep(self.poll_seconds)

    async def recover(self):
        if self._worker is None:
            self._closing = False
            self._worker = asyncio.create_task(self._loop())

    async def aclose(self):
        self._closing = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self.database.close()
