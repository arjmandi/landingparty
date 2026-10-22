"""Typed asynchronous operator queue; no secret values enter ticket bodies."""

from __future__ import annotations

import asyncio
import json
import math
import secrets
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from .approvals import (
    ApprovalLedger,
    action_hash,
    canonical,
    capabilities,
    redact,
    synchronized,
    timestamp,
)

KINDS = {"approval", "takeover", "grant_credential", "grant_payment", "policy_conflict"}
CLASSES = {"blocker", "approval", "diagnostic", "choice"}
OPEN_STATES = {"open", "accepted"}
ACTIVITY_LEASE_SECONDS = 30


def blocked(detail, rule_id="ask_policy", kind="policy_deny"):
    return {"kind": "blocked", "type": kind, "detail": detail, "rule_id": rule_id}


class AskService:
    def __init__(self, world, *, clock=None, expiry_seconds=7 * 86400):
        self.world, self.store = world, world.store
        self.database = capabilities(world)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.expiry_seconds = expiry_seconds
        self.approvals = ApprovalLedger(world, clock=self.clock)
        self._task = None
        self.database.db.execute("""CREATE TABLE IF NOT EXISTS contamination (
            contamination_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL
        )""")

    def _audience(self, run_id):
        config = self.store.get_run(run_id)["config"]
        return "life:" + config["life_id"] if config.get("life_id") else "run:" + run_id

    def _provenance(self, run_id, params=None, provided=None):
        from .context import ask_provenance

        if provided and provided.get("context_status") in {"verified", "unmeasured", "ambiguous"}:
            return provided
        collector = getattr(self.world, "ask_provenance", None)
        actual = (
            collector(run_id, params=params)
            if collector
            else ask_provenance(self.world, run_id, params)
        )
        extra = dict(provided or {})
        broker_ids = extra.pop("observation_ids", [])
        broker_score = extra.pop("injection_score_max", None)
        actual["broker_observed_ids"] = sorted(set(actual["broker_observed_ids"]) | set(broker_ids))
        scores = [
            value
            for value in [actual.get("broker_injection_score_max"), broker_score]
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ]
        actual["broker_injection_score_max"] = max(scores, default=None)
        return {**extra, **actual}

    def _annotate(self, run_id, action_id, ref, ticket_id, params, provenance):
        old = self.get(ticket_id)["provenance"]
        merged = {**old, **provenance}
        with self.database.transaction() as db:
            db.execute(
                "UPDATE asks SET agent_text=?,alternatives=?,subgoal_fraction=?,info_type=?,provenance=? WHERE ticket_id=? AND state IN ('open','accepted')",
                (
                    params["text"],
                    canonical(params.get("alternatives") or []),
                    params.get("subgoal_fraction"),
                    params.get("info_type"),
                    canonical(merged),
                    ticket_id,
                ),
            )
        self.store.append_record(
            run_id,
            "ask_context",
            action_id=action_id,
            action_ref=ref,
            ticket_id=ticket_id,
            agent_text=params["text"],
            alternatives=params.get("alternatives") or [],
            provenance=merged,
            subgoal_fraction=params.get("subgoal_fraction"),
            info_type=params.get("info_type"),
        )

    def _row(self, ticket_id):
        with self.database.lock:
            row = self.database.db.execute(
                "SELECT * FROM asks WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
        if not row:
            raise KeyError(ticket_id)
        return row

    @staticmethod
    def _public(row):
        result = dict(row)
        for name in ("body", "alternatives", "provenance", "answer"):
            result[name] = json.loads(result[name]) if result[name] is not None else None
        result.pop("human_reservation", None)
        result.pop("active_since", None)
        return result

    def get(self, ticket_id):
        return self._public(self._row(ticket_id))

    def list_tickets(self, run_id=None, *, state=None):
        self.expire()
        query, values = "SELECT * FROM asks WHERE 1=1", []
        if run_id:
            query += " AND run_id=?"
            values.append(run_id)
        if state:
            query += " AND state=?"
            values.append(state)
        with self.database.lock:
            rows = self.database.db.execute(
                query + " ORDER BY opened_at,ticket_id", values
            ).fetchall()
        return [self._public(row) for row in rows]

    def _emit(self, ticket, state, *, reason=None, phase=None):
        forecast = getattr(self.world, "services", {}).get("forecast")
        if forecast:
            forecast.notify_state_write(
                run_id=ticket["run_id"],
                world_ref=f"ask:{ticket['ticket_id']}:{state}:{phase or 'outcome'}",
            )
        event = {
            "type": "ask_resolved",
            "ticket_id": ticket["ticket_id"],
            "action_id": ticket["action_id"],
            "state": state,
            "human_seconds": ticket.get("human_seconds", 0),
        }
        if reason:
            event["reason"] = reason
        if phase:
            event["phase"] = phase
        if state == "approved":
            event["resume_token"] = ticket["resume_token"]
            if ticket.get("approval_grant"):
                event["approval_grant"] = ticket["approval_grant"]
        run = self.store.get_run(ticket["run_id"])
        if run["state"] not in {"sealed", "killed", "ended"}:
            self.store.append_record(
                ticket["run_id"],
                "ask_resolved",
                **{key: value for key, value in event.items() if key != "type"},
            )
            self.world.push(ticket["run_id"], event)
        elif hasattr(self.world, "late_events"):
            self.world.push(ticket["run_id"], event)
        else:
            # Late outcomes are durable but must not append after the run seal.
            path = self.database.root / "late-ask-outcomes.jsonl"
            with path.open("a") as target:
                target.write(
                    canonical(
                        {
                            **event,
                            "run_id": ticket["run_id"],
                            "recorded_at": timestamp(self.clock()),
                        }
                    )
                    + "\n"
                )
            path.chmod(0o600)

    @synchronized
    def raise_ticket(
        self,
        run_id,
        action_id,
        kind,
        text="",
        *,
        pending_action,
        alternatives=None,
        provenance=None,
        subgoal_fraction=None,
        info_type=None,
        platform=None,
        scope=None,
    ):
        if kind not in KINDS:
            raise ValueError("Unsupported ticket kind")
        if not isinstance(pending_action, dict) or not pending_action:
            raise ValueError("A ticket requires a concrete world-authored pending action")
        if not isinstance(text, str) or len(text) > 20000:
            raise ValueError("Invalid ask text")
        if subgoal_fraction is not None and not 0 <= subgoal_fraction <= 1:
            raise ValueError("subgoal_fraction must be between zero and one")
        if alternatives is not None and (
            not isinstance(alternatives, list)
            or any(not isinstance(item, str) for item in alternatives)
        ):
            raise ValueError("alternatives must be text choices")
        current = self.clock()
        if self.store.get_run(run_id)["state"] not in {"running", "frozen"}:
            raise ValueError("This run cannot raise new operator tickets")
        digest = action_hash(pending_action)
        audience = self._audience(run_id)
        provenance = self._provenance(run_id, provided=provenance)
        with self.database.transaction() as db:
            existing = db.execute(
                "SELECT * FROM asks WHERE run_id=? AND kind=? AND action_hash=? AND state IN ('open','accepted') ORDER BY opened_at LIMIT 1",
                (run_id, kind, digest),
            ).fetchone()
            if existing:
                return {
                    "kind": "ticket",
                    "ticket_id": existing["ticket_id"],
                    "action_id": existing["action_id"],
                    "expected_by": existing["expires_at"],
                }
            ticket_id = "t_" + secrets.token_hex(16)
            rule = db.execute(
                "SELECT * FROM standing_rejections WHERE audience=? AND kind=? AND platform=? AND scope=? AND revoked=0 ORDER BY created_at DESC LIMIT 1",
                (audience, kind, platform or "", scope or ""),
            ).fetchone()
            state = "rejected" if rule else "open"
            db.execute(
                """INSERT INTO asks(ticket_id,run_id,kind,action_id,action_hash,body,agent_text,alternatives,provenance,opened_at,expires_at,state,resume_token,platform,scope,audience,subgoal_fraction,info_type)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ticket_id,
                    run_id,
                    kind,
                    action_id,
                    digest,
                    canonical(redact(pending_action)),
                    text,
                    canonical(alternatives or []),
                    canonical(provenance),
                    timestamp(current),
                    timestamp(current + timedelta(seconds=self.expiry_seconds)),
                    state,
                    "resume_" + secrets.token_urlsafe(24),
                    platform,
                    scope,
                    audience,
                    subgoal_fraction,
                    info_type,
                ),
            )
        ticket = self.get(ticket_id)
        self.store.append_record(
            run_id,
            "ask_opened",
            ticket_id=ticket_id,
            kind_requested=kind,
            action_id=action_id,
            action_hash=digest,
            body=ticket["body"],
            agent_text=text,
            alternatives=alternatives or [],
            provenance=provenance,
            subgoal_fraction=subgoal_fraction,
            info_type=info_type,
        )
        if rule:
            self._emit(ticket, "rejected", reason=rule["reason"])
        return {
            "kind": "ticket",
            "ticket_id": ticket_id,
            "action_id": action_id,
            "expected_by": ticket["expires_at"],
        }

    async def require_approval(
        self, run_id, action_id, body, kind="approval", *, platform=None, scope=None
    ):
        if self.store.get_run(run_id)["state"] != "running":
            return blocked("This run cannot consume action approvals", "run_state")
        self.expire()
        grant = self.approvals.consume(run_id, body, kind=kind)
        if grant:
            return {
                "kind": "result",
                "approved": True,
                "ticket_id": grant["ticket_id"],
                "grant_id": grant["grant_id"],
                "action_hash": grant["action_hash"],
            }
        return self.raise_ticket(
            run_id,
            action_id,
            kind,
            pending_action=body,
            platform=platform or body.get("platform"),
            scope=scope or body.get("scope"),
        )

    async def handle(self, run_id, action_id, tool, params):
        if tool != "ask":
            raise ValueError("Unknown ask tool")
        kind, text = params.get("kind", "message"), params.get("text", "")
        if not isinstance(text, str) or not 0 < len(text) <= 20000:
            raise ValueError("ask text must contain one to 20000 characters")
        provenance = self._provenance(run_id, params)
        if kind == "message":
            self.store.append_record(
                run_id,
                "ask_opened",
                action_id=action_id,
                kind_requested="message",
                agent_text=text,
                ticket_id=None,
                provenance=provenance,
                alternatives=params.get("alternatives") or [],
                subgoal_fraction=params.get("subgoal_fraction"),
                info_type=params.get("info_type"),
            )
            return {"kind": "silence", "action_id": action_id}
        if kind not in KINDS:
            raise ValueError("Unsupported ticket kind")
        alternatives = params.get("alternatives")
        if alternatives is not None and (
            not isinstance(alternatives, list)
            or any(not isinstance(item, str) for item in alternatives)
        ):
            raise ValueError("alternatives must be text choices")
        fraction = params.get("subgoal_fraction")
        if fraction is not None and not 0 <= fraction <= 1:
            raise ValueError("subgoal_fraction must be between zero and one")
        ref = params.get("action_ref")
        action = next(
            (
                record
                for record in self.store.records(run_id)
                if record.get("kind") == "action" and record.get("action_id") == ref
            ),
            None,
        )
        if not action:
            return blocked(
                "A non-message ask requires a pending action from this run", "ask_action_ref"
            )
        body = {"run_id": run_id, "tool": action["tool"], "params": action["params"]}
        outcomes = [
            record
            for record in self.store.records(run_id)
            if record.get("action_id") == ref and record.get("kind") == "outcome"
        ]
        if kind == "grant_payment":
            quote = next(
                (
                    resource
                    for record in reversed(outcomes)
                    for resource in [
                        record.get("block") or record.get("resource") or record.get("reply")
                    ]
                    if isinstance(resource, dict) and resource.get("type") == "paywall_402"
                ),
                None,
            )
            payments = getattr(self.world, "services", {}).get("payments")
            url = (quote or {}).get("quote_url") or action["params"].get("url")
            if (
                not quote
                or not quote.get("quote_hash")
                or quote.get("amount") is None
                or not url
                or not payments
            ):
                return blocked(
                    "A payment ask requires an observed, verifiable 402 quote", "payment_quote"
                )
            expiry = timestamp(self.clock() + timedelta(hours=1))
            pending = {"url": url, "quote_hash": quote["quote_hash"]}
            with self.database.lock:
                candidates = self.database.db.execute(
                    "SELECT body FROM asks WHERE audience=? AND kind='grant_payment' AND state IN ('open','approved') ORDER BY rowid DESC",
                    (self._audience(run_id),),
                ).fetchall()
            for candidate in candidates:
                original = json.loads(candidate["body"])
                if (
                    original.get("action") == pending
                    and original.get("merchant") == quote["merchant"]
                    and Decimal(original.get("ceiling", "0")) == Decimal(str(quote["amount"]))
                    and original.get("purpose") == text[:1000]
                    and datetime.fromisoformat(original["expires_at"]) > self.clock()
                ):
                    expiry = original["expires_at"]
                    break
            result = await payments.request(
                run_id,
                action_id,
                merchant=quote["merchant"],
                ceiling=quote["amount"],
                purpose=text[:1000],
                expires_at=expiry,
                action=pending,
            )
            if result.get("kind") == "ticket":
                self._annotate(run_id, action_id, ref, result["ticket_id"], params, provenance)
            return result
        self.expire()
        existing = self.database.db.execute(
            "SELECT * FROM asks WHERE run_id=? AND action_id=? AND kind=? ORDER BY rowid DESC LIMIT 1",
            (run_id, ref, kind),
        ).fetchone()
        if existing:
            if existing["state"] in OPEN_STATES:
                self._annotate(run_id, action_id, ref, existing["ticket_id"], params, provenance)
                return {
                    "kind": "ticket",
                    "ticket_id": existing["ticket_id"],
                    "action_id": existing["action_id"],
                    "expected_by": existing["expires_at"],
                }
            return {
                "kind": "result",
                "ticket_id": existing["ticket_id"],
                "state": existing["state"],
                "action_hash": existing["action_hash"],
            }
        if kind == "policy_conflict":
            vault = getattr(self.world, "services", {}).get("vault")
            url = action["params"].get("url")
            platform = vault.platform_for_url(url) if vault else None
            if platform and vault.terms(platform)["terms_class"] == "forbids":
                return self.raise_ticket(
                    run_id,
                    action_id,
                    kind,
                    text,
                    pending_action={
                        "url": url,
                        "platform": platform,
                        "reason": "platform_terms_forbid_agents",
                    },
                    provenance=provenance,
                    alternatives=alternatives,
                    platform=platform,
                    scope="read",
                )
        if kind in {"approval", "takeover", "policy_conflict"}:
            return blocked(
                "This action has no corresponding world-authored pending gate", "ask_pending_action"
            )
        return self.raise_ticket(
            run_id,
            action_id,
            kind,
            text,
            pending_action=body,
            alternatives=params.get("alternatives"),
            provenance=provenance,
            subgoal_fraction=params.get("subgoal_fraction"),
            info_type=params.get("info_type"),
        )

    @staticmethod
    def _human_rate(config):
        value = config.get(
            "human_rate_usd_per_minute",
            config.get("human_rate_per_minute", config.get("human_rate")),
        )
        if value is None:
            return None
        value = Decimal(str(value))
        if not value.is_finite() or value < 0:
            raise ValueError("Invalid human minute price")
        return value

    @staticmethod
    def _human_usd(seconds, rate):
        return (Decimal(str(seconds)) * rate / 60).quantize(
            Decimal("0.000000001"), rounding=ROUND_CEILING
        )

    @synchronized
    def touch(self, ticket_id, *, active=True):
        self.expire()
        row = self._row(ticket_id)
        if row["state"] not in OPEN_STATES:
            raise ValueError("Ticket is no longer open")
        now = self.clock()
        seconds = float(row["human_seconds"])
        if row["active_since"]:
            seconds = min(
                600,
                seconds
                + min(
                    ACTIVITY_LEASE_SECONDS,
                    max(0, (now - datetime.fromisoformat(row["active_since"])).total_seconds()),
                ),
            )
        reservation = row["human_reservation"]
        source = self.store.get_run(row["run_id"])
        config = source["config"]
        late = source["state"] in {"sealed", "killed", "ended"}
        lives = getattr(self.world, "services", {}).get("lives")
        life_id = config.get("life_id")
        if source["state"] not in {"running", "frozen", "stopping"} and (
            not late or not life_id or not lives or lives.get(life_id).get("ended")
        ):
            raise ValueError("A finished life cannot begin console work")
        if (
            active
            and config.get("human_time_priced", "parallel") == "in_budget"
            and not reservation
        ):
            rate = self._human_rate(config)
            if rate is None:
                raise ValueError("In-budget human work requires a declared minute rate")
            previous = self.database.db.execute(
                "SELECT seconds,usd FROM human_ledger WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            remaining = self._human_usd(600, rate) - Decimal(
                previous["usd"] if previous and previous["usd"] is not None else "0"
            )
            if remaining and late:
                try:
                    reservation = lives.reserve_human(
                        life_id,
                        remaining,
                        evidence_ref=f"ask:{ticket_id}:after:{previous['seconds'] if previous else 0}",
                        source_run_id=row["run_id"],
                    )
                except ValueError:
                    reservation = None
            elif remaining:
                reservation = self.store.reserve(row["run_id"], remaining)
            if remaining and reservation is None:
                return blocked(
                    "Remaining budget cannot cover active console work",
                    "human_budget",
                    "budget_exhausted",
                )
        with self.database.transaction() as db:
            db.execute(
                "UPDATE asks SET human_seconds=?,active_since=?,human_reservation=? WHERE ticket_id=?",
                (seconds, timestamp(now) if active else None, reservation, ticket_id),
            )
        return {"ticket_id": ticket_id, "human_seconds": seconds, "active": active}

    def _charge_human(self, row, supplied_seconds, *, allow_stopping=False):
        if not math.isfinite(float(supplied_seconds)) or float(supplied_seconds) < 0:
            raise ValueError("Human time must be finite and nonnegative")
        seconds = max(float(row["human_seconds"]), float(supplied_seconds))
        if row["active_since"]:
            seconds = max(
                seconds,
                float(row["human_seconds"])
                + min(
                    ACTIVITY_LEASE_SECONDS,
                    max(
                        0,
                        (
                            self.clock() - datetime.fromisoformat(row["active_since"])
                        ).total_seconds(),
                    ),
                ),
            )
        if seconds < 0:
            raise ValueError("Human time cannot be negative")
        seconds = min(600, seconds)
        config = self.store.get_run(row["run_id"])["config"]
        allowed = {"running", "frozen", "stopping"} if allow_stopping else {"running", "frozen"}
        source_state = self.store.get_run(row["run_id"])["state"]
        late = source_state in {"sealed", "killed", "ended"}
        lives = getattr(self.world, "services", {}).get("lives")
        life_id = config.get("life_id")
        if source_state not in allowed and (
            not late or not life_id or not lives or lives.get(life_id).get("ended")
        ):
            raise ValueError("A finished run cannot receive new operator answers")
        pricing = config.get("human_time_priced", "parallel")
        rate = self._human_rate(config) if pricing == "in_budget" else None
        usd = self._human_usd(seconds, rate) if rate is not None else None
        if usd is not None and (not usd.is_finite() or usd < 0):
            raise ValueError("Invalid human minute price")
        with self.database.lock:
            previous = self.database.db.execute(
                "SELECT seconds,usd FROM human_ledger WHERE ticket_id=?", (row["ticket_id"],)
            ).fetchone()
        prior_usd = (
            Decimal(previous["usd"]) if previous and previous["usd"] is not None else Decimal(0)
        )
        if previous and seconds < previous["seconds"]:
            seconds = previous["seconds"]
            usd = self._human_usd(seconds, rate) if rate is not None else None
        if pricing == "in_budget":
            if rate is None:
                raise ValueError("In-budget human time requires a declared USD per minute rate")
            delta = usd - prior_usd
            if row["human_reservation"] and row["human_reservation"].startswith("lh_"):
                charge = lives.settle_human(
                    row["human_reservation"],
                    delta,
                    evidence_ref=f"ask:{row['ticket_id']}:human:{seconds}",
                )
                paid = charge.get("charged", False)
            elif late:
                charge = lives.charge_human(
                    life_id,
                    delta,
                    evidence_ref=f"ask:{row['ticket_id']}:human:{seconds}",
                    source_run_id=row["run_id"],
                )
                paid = charge.get("charged", False)
            elif row["human_reservation"]:
                paid = self.store.settle(row["human_reservation"], "human", delta)
            else:
                paid = not delta or self.store.debit(row["run_id"], "human", delta)
            if not paid:
                return blocked(
                    "Human time exceeds the remaining run budget",
                    "human_budget",
                    "budget_exhausted",
                )
        with self.database.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO human_ledger VALUES(?,?,?,?,?)",
                (
                    row["ticket_id"],
                    row["run_id"],
                    seconds,
                    pricing,
                    str(usd) if usd is not None else None,
                ),
            )
            db.execute(
                "UPDATE asks SET human_seconds=?,active_since=NULL,human_reservation=NULL WHERE ticket_id=?",
                (seconds, row["ticket_id"]),
            )
        return {
            "human_seconds": seconds,
            "pricing": pricing,
            "usd": float(usd) if usd is not None else None,
        }

    @synchronized
    def answer(
        self,
        ticket_id,
        decision,
        *,
        ask_class="blocker",
        human_seconds=0,
        standing=False,
        reason=None,
    ):
        self.expire()
        row = self._row(ticket_id)
        if row["state"] != "open":
            raise ValueError("Only open tickets can be answered")
        if ask_class not in CLASSES:
            raise ValueError("Invalid ask class")
        if decision not in {"approve", "reject", "deposit"}:
            raise ValueError("Answer must approve, reject, or deposit")
        config = self.store.get_run(row["run_id"])["config"]
        if config.get("reply_policy", "silent") == "silent":
            raise ValueError("The run's registered reply policy is silent")
        if config.get("reply_policy") == "blockers_only" and ask_class == "choice":
            decision, reason = "reject", "policy_choice_class"
        if decision == "deposit" and row["kind"] not in {"grant_credential", "takeover"}:
            raise ValueError("Only a credential or takeover ticket can accept a deposit")
        if standing and decision != "reject":
            raise ValueError("Standing rules are rejections only")
        charge = self._charge_human(row, human_seconds)
        if charge.get("kind") == "blocked":
            return charge
        state = {"approve": "approved", "reject": "rejected", "deposit": "accepted"}[decision]
        if row["kind"] == "takeover" and decision == "approve":
            state = "accepted"
        answer = {
            "decision": decision,
            "ask_class": ask_class,
            "answered_at": timestamp(self.clock()),
            "standing": standing,
            "reason": reason,
            **charge,
        }
        with self.database.transaction() as db:
            db.execute(
                "UPDATE asks SET state=?,ask_class=?,answer=?,human_seconds=?,active_since=NULL WHERE ticket_id=? AND state='open'",
                (state, ask_class, canonical(answer), charge["human_seconds"], ticket_id),
            )
            if standing:
                db.execute(
                    "INSERT INTO standing_rejections VALUES(?,?,?,?,?,?,?,0)",
                    (
                        "sr_" + secrets.token_hex(16),
                        row["audience"],
                        row["kind"],
                        row["platform"] or "",
                        row["scope"] or "",
                        reason or "standing_rejection",
                        timestamp(self.clock()),
                    ),
                )
        ticket = self.get(ticket_id)
        if self.store.get_run(row["run_id"])["state"] not in {"sealed", "killed", "ended"}:
            self.store.append_record(
                row["run_id"],
                "ask_answered",
                ticket_id=ticket_id,
                ask_class=ask_class,
                state=state,
                human_seconds=charge["human_seconds"],
                human_usd=charge["usd"],
                standing=standing,
                reason=reason,
            )
        if state == "approved" and row["kind"] != "takeover":
            ticket["approval_grant"] = self.approvals.grant(ticket)
        self._emit(ticket, state, reason=reason)
        return ticket

    @synchronized
    def complete_deposit(self, ticket_id, *, accepted=True):
        self.expire()
        row = self._row(ticket_id)
        if row["state"] != "accepted":
            raise ValueError("Deposit must be accepted before it can complete")
        charge = self._charge_human(row, 0)
        if charge.get("kind") == "blocked":
            return charge
        state = "deposited" if accepted else "rejected"
        with self.database.transaction() as db:
            db.execute("UPDATE asks SET state=? WHERE ticket_id=?", (state, ticket_id))
        ticket = self.get(ticket_id)
        self._emit(ticket, state, phase="complete")
        return ticket

    @synchronized
    def complete_takeover(self, ticket_id, *, human_seconds=0):
        self.expire()
        row = self._row(ticket_id)
        if row["kind"] != "takeover" or row["state"] not in {"accepted", "approved"}:
            raise ValueError("A takeover must be accepted before it completes")
        charge = self._charge_human(row, human_seconds)
        if charge.get("kind") == "blocked":
            return charge
        with self.database.transaction() as db:
            db.execute(
                "UPDATE asks SET state='approved',human_seconds=?,active_since=NULL WHERE ticket_id=?",
                (charge["human_seconds"], ticket_id),
            )
        ticket = self.get(ticket_id)
        self._emit(ticket, "approved", phase="complete")
        return ticket

    @synchronized
    def mark_ignored(self, ticket_id):
        row = self._row(ticket_id)
        if row["state"] not in OPEN_STATES:
            raise ValueError("Only an unanswered ticket can be ignored")
        if row["human_reservation"] or row["active_since"]:
            charge = self._charge_human(row, 0)
            if charge.get("kind") == "blocked":
                return charge
        with self.database.transaction() as db:
            db.execute("UPDATE asks SET state='ignored' WHERE ticket_id=?", (ticket_id,))
        self._emit(self.get(ticket_id), "ignored")
        return self.get(ticket_id)

    @synchronized
    def expire(self):
        with self.database.transaction() as db:
            rows = db.execute(
                "SELECT * FROM asks WHERE state IN ('open','accepted') AND expires_at<=?",
                (timestamp(self.clock()),),
            ).fetchall()
            db.executemany(
                "UPDATE asks SET state='expired' WHERE ticket_id=?",
                [(row["ticket_id"],) for row in rows],
            )
        for row in rows:
            if row["human_reservation"] or row["active_since"]:
                self._charge_human(row, 0, allow_stopping=True)
            self._emit(self.get(row["ticket_id"]), "expired")
        return len(rows)

    @synchronized
    def cancel_run(self, run_id, *, preserve_pending=False):
        with self.database.transaction() as db:
            if preserve_pending:
                rows = db.execute(
                    "SELECT * FROM asks WHERE audience=? AND state IN ('open','accepted')",
                    (self._audience(run_id),),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM asks WHERE run_id=? AND state IN ('open','accepted')", (run_id,)
                ).fetchall()
            cancelled = [row for row in rows if not preserve_pending or row["kind"] == "takeover"]
            db.executemany(
                "UPDATE asks SET state='cancelled' WHERE ticket_id=?",
                [(row["ticket_id"],) for row in cancelled],
            )
        for row in rows:
            if row["human_reservation"] or row["active_since"]:
                self._charge_human(row, 0, allow_stopping=True)
            if row in cancelled:
                self._emit(self.get(row["ticket_id"]), "cancelled", reason="run_ended")
        if not preserve_pending:
            self.approvals.revoke_run(run_id)
        return len(cancelled)

    @synchronized
    def cancel_life(self, life_id):
        """Cancel preserved blockers before the life becomes terminal."""
        with self.database.lock:
            rows = self.database.db.execute(
                "SELECT DISTINCT run_id FROM asks WHERE audience=? "
                "AND state IN ('open','accepted')",
                ("life:" + life_id,),
            ).fetchall()
        return sum(self.cancel_run(row["run_id"]) for row in rows)

    def human_spend(self, run_id):
        with self.database.lock:
            rows = self.database.db.execute(
                "SELECT seconds,pricing,usd FROM human_ledger WHERE run_id=?", (run_id,)
            ).fetchall()
        return {
            "seconds": sum(row["seconds"] for row in rows),
            "usd": sum(float(row["usd"]) for row in rows if row["usd"] is not None),
            "unpriced_tickets": sum(row["usd"] is None for row in rows),
        }

    def standing_rejections(self, run_id):
        with self.database.lock:
            return [
                dict(row)
                for row in self.database.db.execute(
                    "SELECT * FROM standing_rejections WHERE audience=? AND revoked=0 ORDER BY created_at",
                    (self._audience(run_id),),
                )
            ]

    def revoke_standing(self, rule_id):
        with self.database.transaction() as db:
            if not db.execute(
                "UPDATE standing_rejections SET revoked=1 WHERE rule_id=?", (rule_id,)
            ).rowcount:
                raise KeyError(rule_id)
        return {"rule_id": rule_id, "revoked": True}

    def console_state(self, run_id, *, since=0):
        """Bounded researcher view; record bodies remain literal untrusted data."""
        if isinstance(since, bool) or not isinstance(since, int) or since < 0:
            raise ValueError("Invalid console activity cursor")
        run = self.store.get_run(run_id)
        records = self.store.records(run_id)
        selected = records[since:][-200:]
        projection = (
            "event_id",
            "kind",
            "recorded_at",
            "timestamp",
            "ts",
            "action_id",
            "tool",
            "state",
            "cost_usd",
            "session_id",
            "path",
            "modality",
            "ticket_id",
            "platform",
            "reason",
            "rule_id",
        )
        activity = [{key: item[key] for key in projection if key in item} for item in selected]
        footprint = getattr(self.world, "services", {}).get("footprint")
        incidents = []
        if footprint:
            incidents = [
                item
                for item in footprint.query(run_id, include_incidents=True)
                if item.get("kind") == "incident"
            ]
        with self.database.lock:
            contamination = [
                json.loads(row[0])
                for row in self.database.db.execute(
                    "SELECT payload FROM contamination WHERE run_id=? ORDER BY rowid", (run_id,)
                )
            ]
        return {
            "run_id": run_id,
            "state": run["state"],
            "cursor": len(records),
            "activity": activity,
            "standing_rejections": self.standing_rejections(run_id),
            "incidents": redact(incidents),
            "contamination": contamination,
            "human": self.human_spend(run_id),
        }

    @synchronized
    def record_contamination(self, run_id, *, kind, detail, owner, human_seconds=0):
        if kind not in {"ad_hoc_help", "hint", "frontier_paste", "other"}:
            raise ValueError("Invalid contamination kind")
        if (
            not isinstance(detail, str)
            or not 0 < len(detail) <= 20000
            or not isinstance(owner, str)
            or not 0 < len(owner) <= 200
        ):
            raise ValueError("Contamination requires bounded detail and a human owner")
        if not math.isfinite(float(human_seconds)) or not 0 <= float(human_seconds) <= 600:
            raise ValueError("Contamination work must name actual seconds between zero and 600")
        run = self.store.get_run(run_id)
        if run["state"] not in {"running", "frozen"}:
            raise ValueError("Record contamination against an active run")
        item = {
            "contamination_id": "ct_" + secrets.token_hex(12),
            "run_id": run_id,
            "kind": kind,
            "detail": detail,
            "owner": owner,
            "human_seconds": float(human_seconds),
            "recorded_at": timestamp(self.clock()),
        }
        # This is an operator-only correction, never a channel to the agent.
        # Explicit actual work is still charged under the registered time policy.
        row = {
            "ticket_id": item["contamination_id"],
            "run_id": run_id,
            "human_seconds": 0,
            "active_since": None,
            "human_reservation": None,
        }
        charge = self._charge_human(row, human_seconds)
        if charge.get("kind") == "blocked":
            return charge
        self.database.db.execute(
            "INSERT INTO contamination VALUES(?,?,?)",
            (item["contamination_id"], run_id, canonical(item)),
        )
        self.store.append_record(
            run_id,
            "contamination",
            **{key: value for key, value in item.items() if key not in {"run_id", "kind"}},
            contamination_kind=kind,
        )
        self.store.update_run(run_id, tainted=True)
        return item

    async def recover(self):
        self.expire()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._expiry_loop())

    async def _expiry_loop(self):
        while True:
            await asyncio.sleep(30)
            self.expire()

    async def aclose(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
