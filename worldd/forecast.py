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
        if not isinstance(state, str) or not state.startswith(_PREFIXES) or len(state) > 1000:
            raise BeliefValidationError("state must use the addressable world namespace")

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
            or len(set(assumptions)) != len(assumptions)
        ):
            raise BeliefValidationError("at most four distinct ticket assumptions")
        beliefs = getattr(self.world, "services", {}).get("beliefs")
        if assumptions and beliefs is not None:
            beliefs._check_assumptions(run_id, supplied)
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

    def _finish(self, expectation, outcome, *, observation_ref=None, value=None, reason=None):
        result = {**expectation, "outcome": outcome, "resolved_at": _stamp(self.clock())}
        if observation_ref:
            result["observation_ref"] = observation_ref
            result["observed_value"] = value
        if reason:
            result["reason"] = reason
        with self.database.lock:
            changed = self.database.db.execute(
                "UPDATE expectations SET outcome=?,payload=? WHERE id=? AND outcome='open'",
                (outcome, _canonical(result), expectation["id"]),
            ).rowcount
        if not changed:
            return self.get(expectation["id"])
        event = self.database.audit(
            expectation["run_id"], "expectation_resolved", expectation=result
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
        encoded = _canonical(observation)
        async with self._lock:
            with self.database.lock:
                prior = self.database.db.execute(
                    "SELECT payload FROM expectation_observations WHERE ref=?", (observation_ref,)
                ).fetchone()
                if prior:
                    old = json.loads(prior[0])
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
            expectations = [item for source_run in scope_runs for item in self.pending(source_run)]
            for expectation in expectations:
                if (
                    ticket_id
                    and ticket_id in expectation["assumptions"]
                    and outcome in {"rejected", "expired", "ignored", "cancelled"}
                ):
                    resolved.append(self._finish(expectation, "void", reason="assumption_not_met"))
                    continue
                if expectation["state"] != state or resolver != expectation["resolver"]:
                    continue
                if instant < _time(expectation["opened_at"]):
                    continue
                if expectation.get("not_before") and instant < _time(expectation["not_before"]):
                    continue
                if instant > _time(expectation["horizon"]) + timedelta(days=1):
                    continue
                if expectation["assumptions"]:
                    # Resolve only after every assumption is world-recorded as approved.
                    with self.database.lock:
                        placeholders = ",".join("?" for _ in scope_runs)
                        observations = [
                            json.loads(row[0])
                            for row in self.database.db.execute(
                                f"SELECT payload FROM expectation_observations WHERE run_id IN ({placeholders}) ORDER BY observed_at,rowid",
                                scope_runs,
                            )
                        ]
                    states = {item["state"]: item["value"] for item in observations}
                    if not all(
                        states.get(f"ask.{ticket}.outcome") in {"approved", "deposited", "complete"}
                        for ticket in expectation["assumptions"]
                    ):
                        continue
                try:
                    held = bool(_compare(value, expectation.get("value"), expectation["cmp"]))
                except (TypeError, ValueError, KeyError):
                    continue
                resolved.append(
                    self._finish(
                        expectation,
                        "held" if held else "missed",
                        observation_ref=observation_ref,
                        value=value,
                    )
                )
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
        for state, value, resolver in writes:
            reference = "fo_" + digest({"ref": identity, "state": state}).split(":", 1)[1]
            result += await self.observe(
                run_id,
                state,
                value,
                reference,
                observed_at=event.get("observed_at"),
                resolver=resolver,
                ticket_id=event.get("ticket_id"),
                outcome=event.get("outcome", event.get("state")),
            )
        return result

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
            raw = path.read_text()
        else:
            return None
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
        return None

    async def tick(self, *, at=None):
        instant = _time(at or self.clock())
        results = []
        for expectation in self.pending():
            with self.database.lock:
                next_check = self.database.db.execute(
                    "SELECT next_check FROM expectations WHERE id=?", (expectation["id"],)
                ).fetchone()[0]
            if instant < _time(next_check):
                continue
            binding = self._binding(expectation["run_id"], expectation["state"]) or {}
            interval = float(binding.get("interval_s", 3600))
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
