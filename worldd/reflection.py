"""Registered reflection arms using an auditable, strictly metered model callback."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .beliefs import BeliefValidationError, _canonical, _stamp, _time
from .kernel import ExtensionDB
from .util import blocked, identifier


class ReflectionService:
    """Optional reflection with no model call under the default ``none`` arm.

    A callback is a trusted metering adapter, invoked with keyword arguments
    ``run_id, messages, tag, max_output_tokens, max_usd, purpose``. It must honor
    ``max_usd`` with a provider-side maximum reservation and return
    ``{text, call_id}``. Before using any output, this service verifies the call
    against the durable model-call record and ledger. A mere ``metered: true``
    assertion is never accepted as billing evidence.
    """

    def __init__(self, world, *, metered_callback=None, clock=None):
        self.world = world
        self.store = world.store
        self.callback = metered_callback
        self.clock = clock or (lambda: datetime.now(UTC))
        self.database = ExtensionDB(self.store)
        self.database.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS reflection_cycles (
                cycle_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, event_ref TEXT NOT NULL,
                policy TEXT NOT NULL, opened_at TEXT NOT NULL, state TEXT NOT NULL,
                payload TEXT NOT NULL, UNIQUE(run_id,event_ref)
            );
            CREATE TABLE IF NOT EXISTS reflection_calls (
                call_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, run_id TEXT NOT NULL,
                tag TEXT NOT NULL, usd TEXT NOT NULL
            );
            """
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._worker = None
        self._closing = False

    def _beliefs(self):
        service = getattr(self.world, "services", {}).get("beliefs")
        if service is None:
            raise BeliefValidationError("reflection requires the belief service")
        return service

    def _scope_runs(self, run_id):
        config = self.store.get_run(run_id)["config"]
        life_id = config.get("life_id")
        return [
            run
            for run in self.store.list_runs()
            if run["run_id"] == run_id or (life_id and run["config"].get("life_id") == life_id)
        ]

    def _remaining(self, run_id):
        config = self.store.get_run(run_id)["config"]
        share = Decimal(str(config.get("reflect_share", 0.15)))
        if not share.is_finite() or not 0 <= share <= 1:
            raise BeliefValidationError("invalid reflection share")
        runs = self._scope_runs(run_id)
        life_id = config.get("life_id")
        budget = Decimal(str(config["budget"]["usd"]))
        life_service = getattr(self.world, "services", {}).get("lives")
        if life_id and life_service is not None:
            getter = getattr(life_service, "get", None) or getattr(life_service, "get_life", None)
            if getter:
                life = getter(life_id)
                budget = Decimal(str(life.get("budget", {}).get("usd_life", budget)))
        spent = Decimal(0)
        for run in runs:
            for event in self.store.records(run["run_id"]):
                if event["kind"] == "model_call" and any(
                    tag in event.get("tags", []) for tag in ("model.reflect", "model.reflector")
                ):
                    if event.get("usd") is None:
                        return Decimal(0)
                    spent += Decimal(str(event["usd"]))
        available = (
            Decimal(str(config["budget"]["usd"]))
            - Decimal(str(self.store.total_spent(run_id)))
            - Decimal(str(self.store.reserved(run_id)))
        )
        return max(Decimal(0), min(budget * share - spent, available))

    async def _call(self, run_id, cycle_id, messages, tag, purpose):
        maximum = self._remaining(run_id)
        if maximum <= 0:
            raise BeliefValidationError("reflection share exhausted")
        if self.callback is None:
            raise BeliefValidationError("no metered reflection callback configured")
        # This conservative byte budget avoids tokenizer-dependent undercounting.
        encoded = _canonical(messages).encode()
        if len(encoded) > 20000:
            raise BeliefValidationError("reflection input exceeds conservative 20,000-byte budget")
        before = {
            record.get("call_id")
            for record in self.store.records(run_id)
            if record["kind"] == "model_call"
        }
        response = self.callback(
            run_id=run_id,
            messages=messages,
            tag=tag,
            max_output_tokens=2048,
            max_usd=str(maximum),
            purpose=purpose,
        )
        response = await response if inspect.isawaitable(response) else response
        if not isinstance(response, dict) or not isinstance(response.get("text"), str):
            raise BeliefValidationError("metered callback did not return text and call_id")
        call_id = response.get("call_id")
        records = self.store.records(run_id)
        match = next(
            (
                record
                for record in records
                if record["kind"] == "model_call"
                and record.get("call_id") == call_id
                and call_id not in before
            ),
            None,
        )
        if (
            match is None
            or tag not in match.get("tags", [])
            or match.get("usage") is None
            or match.get("usd") is None
        ):
            raise BeliefValidationError(
                "reflection output has no matching metered model-call evidence"
            )
        actual = Decimal(str(match["usd"]))
        if actual < 0 or actual > maximum:
            self.database.audit(
                run_id,
                "reflection_budget_violation",
                call_id=call_id,
                maximum_usd=str(maximum),
                actual_usd=str(actual),
            )
            if self.store.get_run(run_id)["state"] == "running":
                self.store.update_run(run_id, state="frozen", tainted=True)
            raise BeliefValidationError("reflection callback exceeded its reservation")
        reservation_id = match.get("reservation_id")
        debits = [
            record
            for record in records
            if record["kind"] == "ledger_debit"
            and (
                record.get("call_id") == call_id
                or (reservation_id and record.get("reservation_id") == reservation_id)
            )
        ]
        if not debits or sum(Decimal(str(event["cost_usd"])) for event in debits) != actual:
            raise BeliefValidationError(
                "reflection model-call usage does not reconcile with its ledger debit"
            )
        with self.database.lock:
            self.database.db.execute(
                "INSERT INTO reflection_calls VALUES(?,?,?,?,?)",
                (call_id, cycle_id, run_id, tag, str(actual)),
            )
        return response["text"]

    def _context(self, run_id, event, *, companion=False):
        records = self._beliefs().fold(run_id)
        open_tickets = set()
        asks = getattr(self.world, "services", {}).get("asks")
        if asks is not None:
            for source in self._scope_runs(run_id):
                open_tickets.update(
                    ticket["ticket_id"]
                    for ticket in asks.list_tickets(source["run_id"])
                    if ticket["state"] in {"open", "accepted"}
                )
        else:
            for record in records:
                for ticket in record.get("assumptions", []):
                    resolved = self._beliefs()._resolve_evidence(run_id, ticket)
                    if resolved and resolved.get("state") in {"open", "accepted"}:
                        open_tickets.add(ticket)
        entities = {
            (record.get("triple") or {}).get("subject")
            for record in records
            if open_tickets.intersection(record.get("assumptions", []))
        }
        entities.discard(None)
        # Put open-ticket entities first, then sort by entity and observation time.
        records.sort(
            key=lambda record: (
                not (
                    bool(open_tickets.intersection(record.get("assumptions", [])))
                    or (record.get("triple") or {}).get("subject") in entities
                ),
                (record.get("triple") or {}).get("subject", ""),
                record["recorded_at"],
                record["id"],
            )
        )
        context = {"event": event, "open_ticket_ids": sorted(open_tickets), "beliefs": []}
        for record in records:
            candidate = {**context, "beliefs": context["beliefs"] + [record]}
            if len(_canonical(candidate).encode()) > 11000:
                break
            context = candidate
        context["policy"] = "proposed-only companion" if companion else "self"
        return context

    async def _apply(self, run_id, operations, *, companion=False):
        if not isinstance(operations, list):
            raise BeliefValidationError("reflection structuring output must contain operations")
        if companion and len(operations) > 10:
            raise BeliefValidationError("at most ten companion operations per cycle")
        if len(operations) > 50:
            raise BeliefValidationError("at most fifty self operations per cycle")
        results = []
        inference_count = 0
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("tool") not in {
                "believe",
                "revise",
                "retract",
                "link",
            }:
                raise BeliefValidationError("unsupported reflection operation")
            params = operation.get("params", {})
            if companion:
                if operation["tool"] != "believe":
                    raise BeliefValidationError(
                        "companion can propose only; it cannot revise or retract"
                    )
                params = {
                    "record": {
                        **params.get("record", {}),
                        "author": "reflector",
                        "status": "proposed",
                    }
                }
            record = params.get("record", params.get("delta", {}))
            evidence = record.get("evidence", [])
            inference_count += int(
                bool(evidence) and all(item.get("class") == "self_inference" for item in evidence)
            )
            result = await self._beliefs().handle(
                run_id,
                identifier("reflect"),
                operation["tool"],
                params,
                author="reflector" if companion else "agent",
            )
            results.append(result)
        if inference_count > 5 and self._beliefs().beliefs(run_id).enable_flags:
            self.database.audit(
                run_id,
                "belief_flag",
                flag="excess_self_inference_in_reflection",
                count=inference_count,
            )
        return results

    async def trigger(self, run_id: str, event: dict) -> dict:
        config = self.store.get_run(run_id)["config"]
        policy = config.get("reflection_policy", "none")
        if policy == "none":
            return {"kind": "silence", "reason": "reflection_none"}
        if policy == "companion":
            return await self.companion_cycle(run_id, event=event)
        if policy != "self" or event.get("type") not in {"belief_review", "expectation_resolved"}:
            return {"kind": "silence", "reason": "no_registered_trigger"}
        return await self._cycle(run_id, event, companion=False)

    async def companion_cycle(self, run_id: str, *, event=None) -> dict:
        config = self.store.get_run(run_id)["config"]
        if config.get("reflection_policy", "none") != "companion":
            return {"kind": "silence", "reason": "companion_not_registered"}
        jobs = getattr(self.world, "jobs", {}).get(run_id, {})
        if jobs:
            return {"kind": "silence", "reason": "not_idle"}
        return await self._cycle(
            run_id, event or {"type": "idle", "event_id": identifier("idle")}, companion=True
        )

    async def _cycle(self, run_id, event, *, companion):
        scope = self.store.get_run(run_id)["config"].get("life_id") or run_id
        async with self._locks.setdefault(scope, asyncio.Lock()):
            if self.store.get_run(run_id)["state"] != "running":
                return blocked(
                    "policy_deny", "Reflection requires a running session", "reflection_state"
                )
            if self.callback is None:
                return blocked(
                    "policy_deny",
                    "Metered reflection callback is not configured",
                    "reflection_metering",
                )
            event_ref = str(
                event.get("event_id") or event.get("observation_ref") or _canonical(event)
            )
            cutoff = _stamp(_time(self.clock()) - timedelta(hours=48))
            run_ids = [run["run_id"] for run in self._scope_runs(run_id)]
            placeholders = ",".join("?" for _ in run_ids)
            with self.database.lock:
                prior = self.database.db.execute(
                    "SELECT payload FROM reflection_cycles WHERE run_id=? AND event_ref=?",
                    (run_id, event_ref),
                ).fetchone()
                if prior:
                    return {"kind": "silence", "reason": "already_triggered"}
                count = self.database.db.execute(
                    f"SELECT count(*) FROM reflection_cycles WHERE run_id IN ({placeholders}) AND opened_at>=?",
                    (*run_ids, cutoff),
                ).fetchone()[0]
                if not companion and count >= 50:
                    return blocked(
                        "rate_limit",
                        "Reflection ceiling reached for 48 hours",
                        "reflection_ceiling",
                    )
                cycle_id = identifier("rc")
                policy = "companion" if companion else "self"
                self.database.db.execute(
                    "INSERT INTO reflection_cycles VALUES(?,?,?,?,?,?,?)",
                    (
                        cycle_id,
                        run_id,
                        event_ref,
                        policy,
                        _stamp(self.clock()),
                        "running",
                        "{}",
                    ),
                )
            tag = "model.reflector" if companion else "model.reflect"
            try:
                context = self._context(run_id, event, companion=companion)
                messages = [
                    {
                        "role": "system",
                        "content": (
                            "Reflect freely on the supplied observations and beliefs. Do not invent evidence."
                            if not companion
                            else "Consider open-ticket entities first. You may propose beliefs only; do not edit agent beliefs."
                        ),
                    },
                    {"role": "user", "content": _canonical(context)},
                ]
                reasoning = await self._call(
                    run_id, cycle_id, messages, tag, "reflection_reasoning"
                )
                structuring = [
                    {
                        "role": "system",
                        "content": (
                            'Structure the reasoning as JSON {"operations": [{"tool": "believe|revise|retract|link", "params": {}}]}. '
                            "Use only existing evidence refs. Companion output may contain at most ten believe proposals."
                        ),
                    },
                    {"role": "user", "content": reasoning.encode()[:14000].decode(errors="ignore")},
                ]
                structured = await self._call(
                    run_id, cycle_id, structuring, tag, "reflection_structuring"
                )
                parsed = json.loads(structured)
                results = await self._apply(run_id, parsed["operations"], companion=companion)
                result = {"kind": "result", "cycle_id": cycle_id, "operations": results}
                state = "complete"
            except (BeliefValidationError, ValueError, KeyError, TypeError) as error:
                result = blocked("policy_deny", str(error), "reflection_validation")
                result["cycle_id"] = cycle_id
                state = "failed"
            with self.database.lock:
                self.database.db.execute(
                    "UPDATE reflection_cycles SET state=?,payload=? WHERE cycle_id=?",
                    (state, _canonical(result), cycle_id),
                )
            self.database.audit(
                run_id, "reflection_cycle", cycle_id=cycle_id, policy=policy, state=state
            )
            return result

    async def recover(self):
        with self.database.lock:
            # A provider request may have completed before a process crash. Never
            # replay it automatically and risk double billing.
            self.database.db.execute(
                "UPDATE reflection_cycles SET state='interrupted' WHERE state='running'"
            )
        if self._worker is None:
            self._closing = False
            self._worker = asyncio.create_task(self._idle_worker())

    async def _idle_worker(self):
        while not self._closing:
            for run in self.store.list_runs():
                if (
                    run["state"] != "running"
                    or run["config"].get("reflection_policy", "none") != "companion"
                ):
                    continue
                if getattr(self.world, "jobs", {}).get(run["run_id"]):
                    continue
                eligible = [
                    record
                    for record in self.store.records(run["run_id"])
                    if record["kind"]
                    in {"beam_down", "outcome", "ask_resolved", "expectation_resolved"}
                    and not str(record.get("action_id", "")).startswith("reflect_")
                ]
                if not eligible:
                    continue
                reference = "idle_" + eligible[-1]["hash"]
                await self.companion_cycle(
                    run["run_id"], event={"type": "idle", "event_id": reference}
                )
            await asyncio.sleep(30)

    async def aclose(self):
        self._closing = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.database.close()
