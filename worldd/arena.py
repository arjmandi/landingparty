"""Consented first-contact arenas with attributable, independently recorded acts.

This service is a researcher control surface, never an agent-facing tool or a
source of simulated human responses. Real transport and stake settlement are
separate trusted adapters; missing adapters never manufacture outcomes.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
from datetime import UTC, datetime, timedelta

from .identity import VenueDB
from .util import blocked, canonical, digest, identifier


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("arena timestamps need a timezone")
    return result.astimezone(UTC)


class ArenaService(VenueDB):
    def __init__(self, world, *, stake_settler=None, clock=None):
        super().__init__(world, "arenas")
        self.stake_settler = stake_settler
        self.clock = clock or (lambda: datetime.now(UTC))
        self._stake_locks = {}
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS arenas(arena_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS arena_participants(participant_id TEXT PRIMARY KEY,arena_id TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS arena_consents(consent_id TEXT PRIMARY KEY,participant_id TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS arena_events(arena_id TEXT NOT NULL,seq INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(arena_id,seq));
            CREATE TABLE IF NOT EXISTS arena_stakes(stake_id TEXT PRIMARY KEY,arena_id TEXT NOT NULL,payload TEXT NOT NULL);
        """)

    def _event(self, arena_id, kind, **fields):
        last = self.db.execute(
            "SELECT seq,payload FROM arena_events WHERE arena_id=? ORDER BY seq DESC LIMIT 1",
            (arena_id,),
        ).fetchone()
        event = {
            "arena_id": arena_id,
            "seq": last["seq"] + 1 if last else 1,
            "ts": self.clock().isoformat(),
            "kind": kind,
            **fields,
            "prev_hash": json.loads(last["payload"])["hash"] if last else "sha256:" + "0" * 64,
        }
        event["hash"] = digest(event)
        self.db.execute(
            "INSERT INTO arena_events VALUES(?,?,?)", (arena_id, event["seq"], canonical(event))
        )
        return event

    def get(self, arena_id):
        row = self.db.execute("SELECT payload FROM arenas WHERE arena_id=?", (arena_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown arena")
        return json.loads(row["payload"])

    def participant(self, participant_id):
        row = self.db.execute(
            "SELECT payload FROM arena_participants WHERE participant_id=?", (participant_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Unknown arena participant")
        return json.loads(row["payload"])

    def research_authority(self, arena_id):
        arena = self.get(arena_id)
        registry = self.world.services.get("registry")
        if arena["research_mode"] != "research" or registry is None:
            raise ValueError("development arenas cannot invoke external adapters")
        authority = registry.require_stage(3, research_mode="research")
        if not authority.get("research_authorized"):
            raise ValueError("the first-contact gates are not currently authorized")
        if not any(
            item.get("name") == "IRB"
            and item.get("eligible")
            and item.get("report", {}).get("protocol_ref") == arena["protocol_ref"]
            for item in registry.gates()
        ):
            raise ValueError("the arena's approved protocol is no longer registered")
        return authority

    def create(self, config: dict):
        lives = self.world.services.get("lives")
        if lives is None:
            raise ValueError("an arena requires a registered life")
        life = lives.get(config["life_id"])
        mode = life.get("research_mode", "development")
        if life.get("stage") != 3:
            raise ValueError("human-facing arenas require the first-contact stage")
        registry = self.world.services.get("registry")
        if registry is None:
            raise ValueError("the arena requires the stage-gate registry")
        gate = registry.require_stage(3, research_mode=mode)
        if (
            not config.get("protocol_ref")
            or not config.get("consent_version")
            or not config.get("binding_id")
        ):
            raise ValueError("an arena requires a protocol, consent version, and company binding")
        if config.get("binding_kind", "company") != "company":
            raise ValueError("this arena implements the company domain binding")
        if life.get("binding_id") and config["binding_id"] != life["binding_id"]:
            raise ValueError("the arena binding differs from its life")
        if mode == "research":
            approvals = [
                item
                for item in registry.gates()
                if item.get("name") == "IRB" and item.get("eligible")
            ]
            if not any(
                item.get("report", {}).get("protocol_ref") == config["protocol_ref"]
                for item in approvals
            ):
                raise ValueError("the arena protocol is not bound to registered IRB approval")
        maximum = config.get("max_stake_usd", 0)
        if (
            not isinstance(maximum, (int, float))
            or isinstance(maximum, bool)
            or not math.isfinite(maximum)
            or maximum < 0
        ):
            raise ValueError("the arena stake ceiling must be finite and nonnegative")
        arena = {
            "arena_id": identifier("ar"),
            "life_id": life["life_id"],
            "binding_id": config["binding_id"],
            "binding_kind": "company",
            "protocol_ref": config["protocol_ref"],
            "consent_version": config["consent_version"],
            "research_mode": mode,
            "research_authorized": gate.get("research_authorized", False),
            "stage": 3,
            "created_at": self.clock().isoformat(),
            "max_stake_usd": maximum,
            "disclosure": config.get(
                "disclosure", "Research agent operated by the registered sponsor."
            ),
            "state": "active",
        }
        with self.lock:
            self.db.execute("INSERT INTO arenas VALUES(?,?)", (arena["arena_id"], canonical(arena)))
            self._event(arena["arena_id"], "arena_created", config=arena)
        return arena

    def register_participant(self, arena_id, *, role, contact_ref, participant_ref):
        arena = self.get(arena_id)
        if (
            role not in {"customer", "supplier", "observer", "agent_identity"}
            or not contact_ref
            or not participant_ref
        ):
            raise ValueError("participant role and opaque identity/contact references are required")
        participant = {
            "participant_id": identifier("ap"),
            "arena_id": arena_id,
            "life_id": arena["life_id"],
            "role": role,
            "contact_ref": contact_ref,
            "participant_ref": participant_ref,
            "created_at": self.clock().isoformat(),
            "state": "pending_consent",
        }
        with self.lock:
            self.db.execute(
                "INSERT INTO arena_participants VALUES(?,?,?)",
                (participant["participant_id"], arena_id, canonical(participant)),
            )
            self._event(
                arena_id,
                "participant_registered",
                participant_id=participant["participant_id"],
                role=role,
            )
        return participant

    def record_consent(
        self,
        participant_id,
        *,
        document_ref,
        document_hash,
        confirmed_by,
        signed_at,
        scopes,
        expires_at=None,
        survey_opt_in=False,
    ):
        participant = self.participant(participant_id)
        arena = self.get(participant["arena_id"])
        if not confirmed_by or not scopes or set(scopes) - {"arena", "lottery", "survey"}:
            raise ValueError("verified research authority and explicit consent scopes are required")
        if _time(signed_at) > self.clock():
            raise ValueError("consent cannot be signed in the future")
        if expires_at and _time(expires_at) <= self.clock():
            raise ValueError("consent has already expired")
        path = (self.store.data_dir / document_ref).resolve()
        if (
            not path.is_relative_to(self.store.data_dir.resolve())
            or not path.is_file()
            or (self.store.data_dir / document_ref).is_symlink()
        ):
            raise ValueError("consent evidence must be a captured private researcher artifact")
        if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != document_hash:
            raise ValueError("consent evidence hash mismatch")
        if "survey" in scopes and not survey_opt_in:
            raise ValueError("survey scope requires separate affirmative opt-in")
        consent = {
            "consent_id": identifier("ac"),
            "participant_id": participant_id,
            "arena_id": arena["arena_id"],
            "protocol_ref": arena["protocol_ref"],
            "consent_version": arena["consent_version"],
            "document_ref": document_ref,
            "document_hash": document_hash,
            "confirmed_by": confirmed_by,
            "signed_at": signed_at,
            "recorded_at": self.clock().isoformat(),
            "scopes": sorted(set(scopes)),
            "expires_at": expires_at,
            "survey_opt_in": bool(survey_opt_in),
            "state": "active",
        }
        with self.lock:
            self.db.execute(
                "INSERT INTO arena_consents VALUES(?,?,?)",
                (consent["consent_id"], participant_id, canonical(consent)),
            )
            participant["state"] = "active"
            self.db.execute(
                "UPDATE arena_participants SET payload=? WHERE participant_id=?",
                (canonical(participant), participant_id),
            )
            self._event(arena["arena_id"], "consent_recorded", consent=consent)
        return consent

    def consent(self, participant_id, scope="arena"):
        participant = self.participant(participant_id)
        if participant["state"] != "active":
            return None
        rows = self.db.execute(
            "SELECT payload FROM arena_consents WHERE participant_id=? ORDER BY rowid DESC",
            (participant_id,),
        ).fetchall()
        for row in rows:
            consent = json.loads(row["payload"])
            if (
                consent["state"] == "active"
                and scope in consent["scopes"]
                and (not consent["expires_at"] or _time(consent["expires_at"]) > self.clock())
            ):
                return consent
        return None

    def withdraw(self, participant_id, *, reason="participant_withdrawal"):
        participant = self.participant(participant_id)
        participant["state"] = "withdrawn"
        with self.lock:
            self.db.execute(
                "UPDATE arena_participants SET payload=? WHERE participant_id=?",
                (canonical(participant), participant_id),
            )
            self._event(
                participant["arena_id"],
                "consent_withdrawn",
                participant_id=participant_id,
                reason=reason,
            )
        surveys = self.world.services.get("surveys")
        if surveys:
            surveys.cancel_participant(participant_id, reason=reason)
        return participant

    def timeline(self, arena_id):
        self.get(arena_id)
        return [
            json.loads(row["payload"])
            for row in self.db.execute(
                "SELECT payload FROM arena_events WHERE arena_id=? ORDER BY seq", (arena_id,)
            )
        ]

    def record_exchange(
        self,
        arena_id,
        participant_id,
        *,
        run_id,
        direction,
        content,
        kind="non_request",
        unsolicited=False,
        refusal=False,
        authenticated_source=False,
        observation_ref=None,
    ):
        arena, participant = self.get(arena_id), self.participant(participant_id)
        if not authenticated_source or not observation_ref:
            raise ValueError("an authenticated observation reference is required")
        if participant["arena_id"] != arena_id or not self.consent(participant_id):
            return blocked(
                "policy_deny", "Participant consent is missing or withdrawn", "arena_consent"
            )
        if direction not in {"inbound", "outbound"} or kind not in {"request", "non_request"}:
            raise ValueError("invalid arena exchange direction or kind")
        run = self.store.get_run(run_id)
        if run["config"].get("life_id") != arena["life_id"]:
            raise ValueError("the exchange run belongs to a different life")
        if not isinstance(content, str) or len(content) > 20000:
            raise ValueError("arena content must be text within 20000 characters")
        prior = [
            event
            for event in self.timeline(arena_id)
            if event["kind"] == "exchange" and event.get("participant_id") == participant_id
        ]
        returning = any(
            event["direction"] == "inbound"
            and (
                event["run_id"] != run_id or _time(event["ts"]) <= self.clock() - timedelta(days=7)
            )
            for event in prior
        )
        after_refusal = any(
            event["direction"] == "outbound" and event.get("refusal") for event in prior
        )
        eligible = (
            direction == "inbound"
            and kind == "non_request"
            and unsolicited
            and (returning or after_refusal)
        )
        with self.lock:
            event = self._event(
                arena_id,
                "exchange",
                participant_id=participant_id,
                run_id=run_id,
                direction=direction,
                content=content,
                exchange_kind=kind,
                unsolicited=bool(unsolicited),
                refusal=bool(refusal),
                returning=returning,
                after_refusal=after_refusal,
                grounded_eligible=bool(eligible),
                author_class="arena_participant" if direction == "inbound" else "agent",
                observation_ref=observation_ref,
            )
        footprint = self.world.services.get("footprint")
        if footprint and direction == "inbound":
            item = footprint.record_item(
                run_id,
                event["hash"],
                kind="message_in",
                platform="arena",
                venue_class="human",
                metadata={"arena_id": arena_id, "participant_id": participant_id},
            )
            footprint.add_reading(
                item["item_id"],
                1,
                author_class="arena_participant",
                kind=kind,
                unsolicited=unsolicited,
                returning=returning,
                after_refusal=after_refusal,
                evidence_ref=observation_ref,
            )
        return {"kind": "result", "event": event}

    def request_stake(
        self, arena_id, participant_id, *, question, choices, ceiling_usd, observation_ref
    ):
        arena, participant = self.get(arena_id), self.participant(participant_id)
        if participant["arena_id"] != arena_id or not self.consent(participant_id, "lottery"):
            return blocked("policy_deny", "Lottery consent is required", "arena_lottery_consent")
        if (
            not isinstance(ceiling_usd, (int, float))
            or isinstance(ceiling_usd, bool)
            or not math.isfinite(ceiling_usd)
            or not 0 < ceiling_usd <= arena["max_stake_usd"]
        ):
            raise ValueError("the real-stake ceiling exceeds the registered arena mandate")
        if not question or not isinstance(choices, list) or len(choices) < 2 or not observation_ref:
            raise ValueError("a frozen question, choices, and observation reference are required")
        stake = {
            "stake_id": identifier("as"),
            "arena_id": arena_id,
            "participant_id": participant_id,
            "question": question,
            "choices": choices,
            "ceiling_usd": ceiling_usd,
            "observation_ref": observation_ref,
            "created_at": self.clock().isoformat(),
            "state": "pending",
        }
        with self.lock:
            self.db.execute(
                "INSERT INTO arena_stakes VALUES(?,?,?)",
                (stake["stake_id"], arena_id, canonical(stake)),
            )
            self._event(arena_id, "stake_requested", stake=stake)
        return {"kind": "ticket", "ticket_id": stake["stake_id"], "stake": stake}

    async def settle_stake(self, stake_id, choice):
        lock = self._stake_locks.setdefault(stake_id, asyncio.Lock())
        async with lock:
            return await self._settle_stake(stake_id, choice)

    async def _settle_stake(self, stake_id, choice):
        row = self.db.execute(
            "SELECT payload FROM arena_stakes WHERE stake_id=?", (stake_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Unknown arena stake")
        stake = json.loads(row["payload"])
        if stake["state"] != "pending":
            raise ValueError("a stake can be settled only once")
        if choice not in stake["choices"]:
            raise ValueError("choice is outside the registered lottery")
        if not self.consent(stake["participant_id"], "lottery"):
            return blocked(
                "policy_deny", "Lottery consent is no longer active", "arena_lottery_consent"
            )
        if self.stake_settler is None:
            return blocked(
                "policy_deny",
                "A trusted real-stake settlement adapter is required",
                "stake_settlement",
            )
        if self.get(stake["arena_id"])["research_mode"] != "research":
            return blocked(
                "policy_deny",
                "Development arenas cannot settle real money",
                "development_no_writes",
            )
        self.research_authority(stake["arena_id"])
        stake["state"] = "settling"
        self.db.execute(
            "UPDATE arena_stakes SET payload=? WHERE stake_id=?", (canonical(stake), stake_id)
        )
        self._event(stake["arena_id"], "stake_settlement_started", stake_id=stake_id)
        try:
            result = self.stake_settler(stake, choice)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            stake["state"] = "uncertain"
            self.db.execute(
                "UPDATE arena_stakes SET payload=? WHERE stake_id=?", (canonical(stake), stake_id)
            )
            self._event(stake["arena_id"], "stake_settlement_uncertain", stake_id=stake_id)
            raise
        except Exception as error:  # noqa: BLE001 -- an ambiguous financial request must not be replayed
            result = {"error": type(error).__name__}
        amount = result.get("amount_usd") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or result.get("settled") is not True
            or result.get("rail") != "real_usd"
            or not result.get("observation_ref")
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or not 0 <= amount <= stake["ceiling_usd"]
        ):
            stake["state"] = "uncertain"
            self.db.execute(
                "UPDATE arena_stakes SET payload=? WHERE stake_id=?", (canonical(stake), stake_id)
            )
            self._event(stake["arena_id"], "stake_settlement_uncertain", stake_id=stake_id)
            return blocked(
                "policy_deny", "Real-stake settlement was not confirmed", "stake_unconfirmed"
            )
        stake.update(
            state="settled", choice=choice, settlement=result, settled_at=self.clock().isoformat()
        )
        with self.lock:
            self.db.execute(
                "UPDATE arena_stakes SET payload=? WHERE stake_id=?", (canonical(stake), stake_id)
            )
            self._event(stake["arena_id"], "stake_settled", stake=stake)
        return {"kind": "result", "stake": stake}

    async def recover(self):
        for row in self.db.execute("SELECT payload FROM arena_stakes").fetchall():
            stake = json.loads(row["payload"])
            if stake["state"] == "settling":
                stake["state"] = "uncertain"
                self.db.execute(
                    "UPDATE arena_stakes SET payload=? WHERE stake_id=?",
                    (canonical(stake), stake["stake_id"]),
                )
                self._event(
                    stake["arena_id"], "stake_settlement_uncertain", stake_id=stake["stake_id"]
                )
