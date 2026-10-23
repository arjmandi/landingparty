"""One money account per life: every movement in or out, on the existing ledgers."""

from __future__ import annotations

from decimal import Decimal

from .storage import content_hash

# Who funded each inflow decides its measurement column; it is never collapsed.
CHANNEL_AUTHORS = {
    "recorded": {"human_attested"},
    "bounty": {"sponsor"},
    "market": {"sponsor"},
    "independent": {"independent_human", "independent_agent"},
}


class BankService:
    def __init__(self, world):
        self.world = world
        self.store = world.store

    def _lives(self):
        lives = getattr(self.world, "services", {}).get("lives")
        if lives is None:
            raise ValueError("The bank needs the life scheduler")
        return lives

    def account(self, life_id: str) -> dict:
        """Balance, spend by category, inflows by channel, and every movement."""
        lives = self._lives()
        life = lives.get(life_id)
        status = lives.status(life_id)
        movements, spent, inflows = [], {}, {}
        for session in life["sessions"]:
            for category, usd in self.store.spend(session["run_id"]).items():
                if not usd:
                    continue
                spent[category] = spent.get(category, 0) + usd
                movements.append(
                    {
                        "direction": "out",
                        "channel": category,
                        "usd": usd,
                        "run_id": session["run_id"],
                        "at": session.get("ended_at") or session["started_at"],
                        # Priced from the session's own transcript, not metered at the lane.
                        "estimated": category == "model.estimated",
                    }
                )
        for event in lives.events(life_id):
            if event["kind"] != "income":
                continue
            usd = float(
                Decimal(
                    event.get("amount_nano", int(Decimal(str(event["amount_usd"])) * 1_000_000_000))
                )
                / 1_000_000_000
            )
            inflows[event["source"]] = inflows.get(event["source"], 0) + usd
            movements.append(
                {
                    "direction": "in",
                    "channel": event["source"],
                    "usd": usd,
                    "evidence_ref": event["evidence_ref"],
                    "author_class": event.get("author_class"),
                    "memo": event.get("memo"),
                    "at": event["recorded_at"],
                }
            )
        movements.sort(key=lambda item: item["at"])
        return {
            "life_id": life_id,
            "balance_usd": status["balance_usd"],
            "remaining_usd": status["remaining_usd"],
            "income_usd": status["income_usd"],
            "spent_usd": status["spent_usd"],
            "spent_by_category": spent,
            "income_by_channel": inflows,
            "channels": sorted(life.get("income_channels") or []),
            "movements": movements,
        }

    def deposit(
        self,
        life_id: str,
        usd,
        channel: str,
        *,
        evidence_ref: str,
        author_class: str,
        memo: str | None = None,
    ) -> dict:
        """Credit income once per evidence and tell the agent through its inbox."""
        if author_class not in CHANNEL_AUTHORS.get(channel, set()):
            raise ValueError("This channel's income has a different provenance")
        lives = self._lives()
        _, applied = lives.apply_income(
            life_id, usd, channel, evidence_ref=evidence_ref, author_class=author_class, memo=memo
        )
        if applied:
            self._announce(life_id, usd, channel, evidence_ref=evidence_ref, memo=memo)
        return {"applied": applied, **self.account(life_id)}

    def record(self, life_id: str, *, usd, source: str, evidence: str, request_id: str):
        """An operator-recorded deposit: income the agent earned, attested by the human."""
        if not isinstance(source, str) or not source.strip() or len(source) > 500:
            raise ValueError("Name where the deposit came from")
        # Evidence is what separates earned income from a gift, which no life may receive.
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2000:
            raise ValueError("A deposit needs its evidence: a receipt or a statement line")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError("A deposit needs its request identifier")
        reference = "recorded:" + content_hash({"life_id": life_id, "request_id": request_id})
        memo = source.strip() + " — " + evidence.strip()
        return self.deposit(
            life_id,
            usd,
            "recorded",
            evidence_ref=reference,
            author_class="human_attested",
            memo=memo,
        )

    def _announce(self, life_id, usd, channel, *, evidence_ref, memo):
        lives = self._lives()
        life = lives.get(life_id)
        if not life["sessions"]:
            return
        target = life["active_run_id"] or life["sessions"][-1]["run_id"]
        event = {
            "type": "deposit",
            "usd": str(usd),
            "channel": channel,
            "evidence_ref": evidence_ref,
            "balance_after": lives.status(life_id)["balance_usd"],
        }
        if memo:
            event["memo"] = memo
        # A sealed target queues the event for the next session and may wake a resting life.
        self.world.push(target, event)
