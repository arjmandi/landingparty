"""Durable opt-in afterlife surveys; transport is explicit and never fabricated."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections import Counter
from datetime import UTC, datetime, timedelta

from .arena import _time
from .identity import VenueDB
from .util import blocked, canonical, digest, identifier


class SurveyService(VenueDB):
    def __init__(self, world, transport=None, *, clock=None):
        super().__init__(world, "surveys")
        self.transport, self.clock = transport, clock or (lambda: datetime.now(UTC))
        self.worker = None
        self._closed = False
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS surveys(survey_id TEXT PRIMARY KEY,life_id TEXT NOT NULL,participant_id TEXT NOT NULL,after_days INTEGER NOT NULL,payload TEXT NOT NULL,UNIQUE(life_id,participant_id,after_days));
            CREATE TABLE IF NOT EXISTS survey_events(survey_id TEXT NOT NULL,seq INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(survey_id,seq));
        """)

    def _event(self, survey_id, kind, **fields):
        row = self.db.execute(
            "SELECT seq,payload FROM survey_events WHERE survey_id=? ORDER BY seq DESC LIMIT 1",
            (survey_id,),
        ).fetchone()
        event = {
            "survey_id": survey_id,
            "seq": row["seq"] + 1 if row else 1,
            "ts": self.clock().isoformat(),
            "kind": kind,
            **fields,
            "prev_hash": json.loads(row["payload"])["hash"] if row else "sha256:" + "0" * 64,
        }
        event["hash"] = digest(event)
        self.db.execute(
            "INSERT INTO survey_events VALUES(?,?,?)", (survey_id, event["seq"], canonical(event))
        )
        return event

    def get(self, survey_id):
        row = self.db.execute(
            "SELECT payload FROM surveys WHERE survey_id=?", (survey_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Unknown afterlife survey")
        return json.loads(row["payload"])

    def _save(self, survey):
        self.db.execute(
            "UPDATE surveys SET payload=? WHERE survey_id=?",
            (canonical(survey), survey["survey_id"]),
        )

    def schedule(
        self,
        life_id,
        participant_id,
        *,
        after_days=90,
        protocol_ref,
        questions,
        approval_ref=None,
        response_window_days=30,
    ):
        if after_days not in {30, 90} or not 1 <= response_window_days <= 90:
            raise ValueError("surveys use a registered thirty- or ninety-day afterlife checkpoint")
        arenas = self.world.services.get("arena")
        lives = self.world.services.get("lives")
        if arenas is None or lives is None:
            raise ValueError("surveys require the consented arena and life services")
        participant = arenas.participant(participant_id)
        arena = arenas.get(participant["arena_id"])
        life = lives.get(life_id)
        consent = arenas.consent(participant_id, "survey")
        if participant["life_id"] != life_id or arena["protocol_ref"] != protocol_ref:
            raise ValueError("survey protocol or life does not match the participant's consent")
        if not consent or not consent["survey_opt_in"]:
            raise ValueError("counterparty surveys require separate active opt-in consent")
        if life["stage"] != 3 or arena["stage"] != 3:
            raise ValueError("counterparty surveys require first-contact registration")
        if (
            not isinstance(questions, list)
            or not questions
            or any(
                not isinstance(question, dict)
                or not question.get("item_id")
                or not question.get("question")
                for question in questions
            )
        ):
            raise ValueError("a frozen nonempty survey question list is required")
        if len({question["item_id"] for question in questions}) != len(questions):
            raise ValueError("survey question ids must be unique")
        due = _time(life["ended"]["at"]) + timedelta(days=after_days) if life.get("ended") else None
        survey = {
            "survey_id": identifier("sv"),
            "life_id": life_id,
            "participant_id": participant_id,
            "arena_id": arena["arena_id"],
            "protocol_ref": protocol_ref,
            "consent_id": consent["consent_id"],
            "after_days": after_days,
            "questions": questions,
            "questions_hash": digest(questions),
            "approval_ref": approval_ref,
            "response_window_days": response_window_days,
            "created_at": self.clock().isoformat(),
            "due_at": due.isoformat() if due else None,
            "state": "scheduled" if due else "awaiting_life_end",
            "attempts": 0,
            "research_mode": arena["research_mode"],
            "author_class": "probe",
            "solicited": True,
            "grounded_eligible": False,
            "disclosure": arena["disclosure"],
        }
        with self.lock:
            existing = self.db.execute(
                "SELECT payload FROM surveys WHERE life_id=? AND participant_id=? AND after_days=?",
                (life_id, participant_id, after_days),
            ).fetchone()
            if existing:
                previous = json.loads(existing["payload"])
                if (
                    previous["questions_hash"] != survey["questions_hash"]
                    or previous["protocol_ref"] != protocol_ref
                ):
                    raise ValueError("a scheduled survey's frozen questions cannot be replaced")
                return previous
            self.db.execute(
                "INSERT INTO surveys VALUES(?,?,?,?,?)",
                (survey["survey_id"], life_id, participant_id, after_days, canonical(survey)),
            )
            self._event(
                survey["survey_id"],
                "survey_scheduled",
                protocol_ref=protocol_ref,
                consent_id=consent["consent_id"],
                questions_hash=survey["questions_hash"],
            )
        return survey

    def cancel_participant(self, participant_id, *, reason="consent_withdrawn"):
        with self.lock:
            for row in self.db.execute(
                "SELECT payload FROM surveys WHERE participant_id=?", (participant_id,)
            ).fetchall():
                survey = json.loads(row["payload"])
                if survey["state"] in {"responded", "cancelled", "expired"}:
                    continue
                survey.update(
                    state="cancelled",
                    cancellation_reason=reason,
                    cancelled_at=self.clock().isoformat(),
                )
                self._save(survey)
                self._event(survey["survey_id"], "survey_cancelled", reason=reason)

    async def dispatch_due(self, life_id=None, checkpoint=None):
        query = "SELECT payload FROM surveys" + (" WHERE life_id=?" if life_id else "")
        values = (life_id,) if life_id else ()
        results = []
        for row in self.db.execute(query, values).fetchall():
            survey = json.loads(row["payload"])
            if checkpoint is not None and survey["after_days"] != int(checkpoint):
                continue
            if survey["state"] in {
                "sent",
                "responded",
                "cancelled",
                "expired",
                "sending",
                "uncertain",
            }:
                continue
            life = self.world.services["lives"].get(survey["life_id"])
            if not life.get("ended"):
                continue
            due = _time(life["ended"]["at"]) + timedelta(days=survey["after_days"])
            if self.clock() < due or (
                survey.get("retry_at") and self.clock() < _time(survey["retry_at"])
            ):
                continue
            if self.clock() > due + timedelta(days=survey["response_window_days"]):
                survey.update(state="expired", due_at=due.isoformat())
                self._save(survey)
                self._event(survey["survey_id"], "survey_expired")
                continue
            arenas = self.world.services["arena"]
            consent = arenas.consent(survey["participant_id"], "survey")
            if not consent or consent["consent_id"] != survey["consent_id"]:
                self.cancel_participant(survey["participant_id"])
                continue
            if survey["research_mode"] != "research" or self.transport is None:
                refusal = blocked(
                    "policy_deny",
                    "A research-authorized, approved survey delivery adapter is required",
                    "survey_transport",
                )
                survey.update(
                    state="blocked",
                    due_at=due.isoformat(),
                    retry_at=(self.clock() + timedelta(hours=1)).isoformat(),
                    last_block=refusal,
                )
                self._save(survey)
                self._event(survey["survey_id"], "survey_blocked", rule_id="survey_transport")
                results.append({"survey_id": survey["survey_id"], **refusal})
                continue
            arenas.research_authority(survey["arena_id"])
            # Durable compare-and-set before awaiting external transport prevents
            # duplicate contact across concurrent dispatchers and process crashes.
            with self.lock:
                latest = self.get(survey["survey_id"])
                if latest["state"] in {"sending", "sent", "responded", "cancelled", "uncertain"}:
                    continue
                survey.update(
                    state="sending",
                    attempts=survey["attempts"] + 1,
                    due_at=due.isoformat(),
                    dispatch_started_at=self.clock().isoformat(),
                )
                self._save(survey)
                self._event(survey["survey_id"], "survey_dispatch_started")
            try:
                participant = arenas.participant(survey["participant_id"])
                receipt = self.transport(survey, participant, consent)
                if inspect.isawaitable(receipt):
                    receipt = await receipt
            except asyncio.CancelledError:
                survey.update(state="uncertain", delivery_status="interrupted")
                self._save(survey)
                self._event(survey["survey_id"], "survey_delivery_uncertain")
                raise
            except Exception as error:  # noqa: BLE001 -- an ambiguous send must never be retried automatically
                receipt = {"error": type(error).__name__}
            latest = self.get(survey["survey_id"])
            if latest["state"] == "cancelled":
                latest["in_flight_delivery_receipt"] = receipt
                self._save(latest)
                self._event(
                    survey["survey_id"],
                    "survey_delivery_after_withdrawal",
                    delivered=isinstance(receipt, dict) and receipt.get("delivered") is True,
                )
                results.append(
                    {
                        "survey_id": survey["survey_id"],
                        **blocked(
                            "policy_deny",
                            "Consent was withdrawn while delivery was in flight",
                            "survey_consent_withdrawn",
                        ),
                    }
                )
                continue
            if (
                isinstance(receipt, dict)
                and receipt.get("delivered") is True
                and receipt.get("approved_delivery") is True
                and receipt.get("observation_ref")
            ):
                survey.update(
                    state="sent",
                    sent_at=self.clock().isoformat(),
                    receipt=receipt,
                    response_due_at=(
                        self.clock() + timedelta(days=survey["response_window_days"])
                    ).isoformat(),
                )
                result = {"kind": "result", "survey_id": survey["survey_id"], "delivery": receipt}
            elif isinstance(receipt, dict) and receipt.get("sent") is False:
                survey.update(
                    state="blocked",
                    retry_at=(self.clock() + timedelta(hours=1)).isoformat(),
                    last_block=receipt,
                )
                result = {
                    "survey_id": survey["survey_id"],
                    **blocked(
                        "policy_deny",
                        "Survey delivery was explicitly declined before contact",
                        "survey_declined",
                    ),
                }
            else:
                survey.update(state="uncertain", delivery_status="unconfirmed")
                result = {
                    "survey_id": survey["survey_id"],
                    **blocked(
                        "policy_deny",
                        "Survey delivery is uncertain; automatic retry is disabled",
                        "survey_uncertain",
                    ),
                }
            self._save(survey)
            self._event(
                survey["survey_id"],
                "survey_delivery",
                state=survey["state"],
                observation_ref=receipt.get("observation_ref")
                if isinstance(receipt, dict)
                else None,
            )
            results.append(result)
        return {"life_id": life_id, "results": results}

    def respond(
        self,
        survey_id,
        participant_id,
        responses,
        *,
        authenticated_source=False,
        observation_ref=None,
    ):
        survey = self.get(survey_id)
        if (
            survey["participant_id"] != participant_id
            or not authenticated_source
            or not observation_ref
        ):
            raise ValueError("an authenticated response from the consented participant is required")
        if survey["state"] != "sent":
            raise ValueError("responses require a confirmed delivery and are accepted once")
        if self.clock() > _time(survey["response_due_at"]):
            raise ValueError("the survey response window has expired")
        if not self.world.services["arena"].consent(participant_id, "survey"):
            raise ValueError("survey consent is no longer active")
        if not isinstance(responses, dict) or set(responses) - {
            question["item_id"] for question in survey["questions"]
        }:
            raise ValueError("responses must refer to the frozen survey questions")
        survey.update(
            state="responded",
            responded_at=self.clock().isoformat(),
            responses=responses,
            response_provenance={
                "author_class": "arena_participant",
                "kind": "request",
                "solicited": True,
                "grounded_eligible": False,
                "observation_ref": observation_ref,
            },
        )
        with self.lock:
            self._save(survey)
            self._event(
                survey_id,
                "survey_response",
                participant_id=participant_id,
                responses=responses,
                provenance=survey["response_provenance"],
            )
        return {"kind": "result", "survey_id": survey_id, "state": "responded"}

    def report(self, life_id):
        surveys = [
            json.loads(row["payload"])
            for row in self.db.execute("SELECT payload FROM surveys WHERE life_id=?", (life_id,))
        ]
        counts = Counter(survey["state"] for survey in surveys)
        invited = sum(
            bool(
                (survey.get("receipt") or survey.get("in_flight_delivery_receipt") or {}).get(
                    "delivered"
                )
                is True
                and (survey.get("receipt") or survey.get("in_flight_delivery_receipt") or {}).get(
                    "approved_delivery"
                )
                is True
            )
            for survey in surveys
        )
        timeline = [
            json.loads(row["payload"])
            for row in self.db.execute(
                "SELECT e.payload FROM survey_events e JOIN surveys s USING(survey_id) WHERE s.life_id=? ORDER BY json_extract(e.payload,'$.ts'),e.seq",
                (life_id,),
            )
        ]
        return {
            "life_id": life_id,
            "scheduled": len(surveys),
            "counts": dict(counts),
            "confirmed_invitations": invited,
            "responses": counts["responded"],
            "response_rate": counts["responded"] / invited if invited else None,
            "grounded_eligible": False,
            "column": "counterparty_report",
            "biases": ["opt-in selection", "nonresponse", "survivorship", "solicited response"],
            "timeline": timeline,
            "artifact_hash": digest(timeline),
        }

    async def recover(self):
        with self.lock:
            for row in self.db.execute("SELECT payload FROM surveys").fetchall():
                survey = json.loads(row["payload"])
                if survey["state"] == "sending":
                    survey.update(state="uncertain", delivery_status="process_interrupted")
                    self._save(survey)
                    self._event(survey["survey_id"], "survey_delivery_uncertain")
        if self.worker and not self.worker.done():
            return

        async def worker():
            while not self._closed:
                await self.dispatch_due()
                await asyncio.sleep(30)

        self.worker = asyncio.create_task(worker())

    async def aclose(self):
        self._closed = True
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await super().aclose()
