"""A paper prediction book and declared sponsor-income rails; no real orders."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from .identity import VenueDB, https_url
from .util import blocked, canonical, digest, identifier, now


def amount(value, *, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("A finite credit amount is required") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("Credit amounts must be finite and nonnegative")
    try:
        quantized = result.quantize(Decimal("0.000000001"))
    except InvalidOperation:
        raise ValueError("Credit amount exceeds the supported decimal precision") from None
    if positive and quantized == 0:
        raise ValueError("Positive credit amounts must be at least one nanocredit")
    return quantized


class MarketService(VenueDB):
    def __init__(self, world):
        super().__init__(world, "market")
        self.worker = None
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS paper_markets (
                market_id TEXT PRIMARY KEY, payload TEXT NOT NULL, outcome INTEGER);
            CREATE TABLE IF NOT EXISTS paper_accounts (
                owner TEXT PRIMARY KEY, initial TEXT NOT NULL, balance TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_positions (
                owner TEXT NOT NULL, market_id TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL,
                PRIMARY KEY(owner,market_id,side));
            CREATE TABLE IF NOT EXISTS paper_fills (
                fill_id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_redemptions (
                redemption_id TEXT PRIMARY KEY, owner TEXT NOT NULL, run_id TEXT NOT NULL,
                credits TEXT NOT NULL, usd TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sponsor_bounties (
                bounty_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bounty_awards (
                bounty_id TEXT NOT NULL, owner TEXT NOT NULL, evidence_ref TEXT NOT NULL,
                PRIMARY KEY(bounty_id,owner));
            CREATE TABLE IF NOT EXISTS paper_equity (
                owner TEXT NOT NULL, checkpoint TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(owner,checkpoint));
            CREATE TABLE IF NOT EXISTS independent_income (
                platform TEXT NOT NULL, external_id TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, PRIMARY KEY(platform,external_id));
            CREATE TABLE IF NOT EXISTS income_webhooks (
                platform TEXT PRIMARY KEY, key_env TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS market_payouts (
                evidence_ref TEXT PRIMARY KEY, owner TEXT NOT NULL, usd TEXT NOT NULL,
                memo TEXT NOT NULL, state TEXT NOT NULL);
        """)

    def _settlement(self, run_id):
        return self.store.get_run(run_id)["config"].get("market_settlement", "paper")

    def _settles_in_dollars(self, owner):
        lives = getattr(self.world, "services", {}).get("lives")
        if lives is None or not owner.startswith("life_"):
            return False
        try:
            return lives.get(owner).get("market_settlement") == "balance"
        except KeyError:
            return False

    def _stake(self, run_id, action_id, usd):
        """Reserve a stake on the run's ledger; the action id makes a retry reuse it."""
        reservation_id = "market:" + action_id
        with self.store._lock:
            previous = self.store.db.execute(
                "SELECT state FROM reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
        if previous:
            return reservation_id if previous["state"] in {"held", "settled"} else None
        return self.store.reserve(run_id, usd, reservation_id=reservation_id)

    def reconcile_stakes(self, run_id):
        """Settle held stakes whose position committed; release those that never did.

        A crash between the position's commit and the stake's settlement leaves the stake
        held. This runs before the run seals, while the ledger still accepts settlement.
        """
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT reservation_id FROM reservations WHERE run_id=? AND state='held' "
                "AND reservation_id LIKE 'market:%'",
                (run_id,),
            ).fetchall()
        for row in rows:
            fill = self.db.execute(
                "SELECT payload FROM paper_fills WHERE action_id=?",
                (row["reservation_id"].removeprefix("market:"),),
            ).fetchone()
            if fill:
                staked = json.loads(fill["payload"])["credits"]
                self.store.settle(row["reservation_id"], "market", staked)
            else:
                self.store.release(row["reservation_id"])

    def _settle_payouts(self):
        """Pay dollar-settled proceeds and wins into the bank; each pays exactly once."""
        bank = getattr(self.world, "services", {}).get("bank")
        if bank is None:
            return
        for row in self.db.execute("SELECT * FROM market_payouts WHERE state='pending'").fetchall():
            try:
                bank.deposit(
                    row["owner"],
                    amount(row["usd"], positive=True),
                    "market",
                    evidence_ref=row["evidence_ref"],
                    author_class="sponsor",
                    memo=row["memo"],
                )
                state = "paid"
            except ValueError:
                # An ended life cannot receive income; the payout is void, not deferred.
                state = "void"
            self.db.execute(
                "UPDATE market_payouts SET state=? WHERE evidence_ref=?",
                (state, row["evidence_ref"]),
            )

    def _owner(self, run_id):
        config = self.store.get_run(run_id)["config"]
        owner = config.get("life_id") or run_id
        initial = (
            Decimal(0)
            if config.get("market_settlement") == "balance"
            else amount(config.get("market_initial_credits", 1000))
        )
        self.db.execute(
            "INSERT OR IGNORE INTO paper_accounts VALUES (?,?,?)",
            (owner, str(initial), str(initial)),
        )
        return owner

    def _notify_state(self, run_id, world_ref):
        forecast = getattr(self.world, "services", {}).get("forecast")
        if forecast and hasattr(forecast, "notify_state_write"):
            forecast.notify_state_write(run_id=run_id, world_ref=world_ref)

    def _notify_owners(self, owners, world_ref):
        seen = set()
        for run in self.store.list_runs():
            owner = run["config"].get("life_id") or run["run_id"]
            if owner in owners and owner not in seen:
                seen.add(owner)
                self._notify_state(run["run_id"], world_ref)

    def balance(self, run_id):
        owner = self._owner(run_id)
        row = self.db.execute("SELECT * FROM paper_accounts WHERE owner=?", (owner,)).fetchone()
        balance, initial = amount(row["balance"]), amount(row["initial"])
        if self._settlement(run_id) == "balance":
            lives = self.world.services["lives"]
            return {
                "owner": owner,
                "settlement": "balance",
                "balance_usd": lives.status(owner)["balance_usd"],
                "positions": [
                    dict(p)
                    for p in self.db.execute(
                        "SELECT * FROM paper_positions WHERE owner=?", (owner,)
                    )
                ],
            }
        return {
            "owner": owner,
            "credits": float(balance),
            "initial_credits": float(initial),
            "redeemable_profit_credits": float(max(Decimal(0), balance - initial)),
            "positions": [
                dict(p)
                for p in self.db.execute("SELECT * FROM paper_positions WHERE owner=?", (owner,))
            ],
        }

    def equity(self, run_id, *, record=True):
        account = self.balance(run_id)
        row = self.db.execute(
            "SELECT balance FROM paper_accounts WHERE owner=?", (account["owner"],)
        ).fetchone()
        value = amount(row["balance"])
        marks, stale = [], []
        for position in account["positions"]:
            row = self.db.execute(
                "SELECT payload FROM paper_markets WHERE market_id=?", (position["market_id"],)
            ).fetchone()
            market = json.loads(row["payload"])
            price = amount(market["price"])
            price = 1 - price if position["side"] == "no" else price
            marked = amount(position["quantity"]) * price
            value += marked
            marks.append(
                {
                    **position,
                    "mark": str(price),
                    "marked_credits": str(marked),
                    "observation_ref": market.get("observation_ref"),
                }
            )
            if datetime.now(UTC) - datetime.fromisoformat(market["observed_at"]) > timedelta(
                hours=1
            ):
                stale.append(position["market_id"])
        passive = []
        for row in self.db.execute("SELECT payload FROM paper_fills"):
            fill = json.loads(row["payload"])
            if fill["owner"] != account["owner"] or fill["side"].startswith("sell_"):
                continue
            current = self.db.execute(
                "SELECT * FROM paper_markets WHERE market_id=?", (fill["market_id"],)
            ).fetchone()
            market = json.loads(current["payload"])
            yes = fill["side"].endswith("yes")
            price = (
                Decimal(int(bool(current["outcome"]) == yes))
                if current["outcome"] is not None
                else amount(market["price"])
                if yes
                else 1 - amount(market["price"])
            )
            marked = amount(fill["quantity"]) * price
            passive.append(
                {
                    "fill_id": fill["fill_id"],
                    "entered_at": fill["observed_at"],
                    "entry_credits": fill["credits"],
                    "marked_credits": str(marked),
                    "pnl_credits": str(marked - amount(fill["credits"])),
                }
            )
        result = {
            "owner": account["owner"],
            "observed_at": now(),
            "cash_credits": account.get("credits", 0.0),
            "equity_credits": str(value),
            "positions": marks,
            "stale_markets": sorted(set(stale)),
            "matched_entry_passive_tranches": passive,
            "passive_policy": "Independent buy-and-hold tranches at actual entry times; no claim of a reinvested equal-capital book.",
        }
        if record:
            checkpoint = datetime.now(UTC).replace(minute=0, second=0, microsecond=0).isoformat()
            self.db.execute(
                "INSERT OR IGNORE INTO paper_equity VALUES (?,?,?)",
                (account["owner"], checkpoint, canonical(result)),
            )
        return result

    def register_income_webhook(self, platform, key_env):
        if not key_env.startswith("WORLD_") or not all(c.isalnum() or c == "_" for c in key_env):
            raise ValueError("Income webhook must name a secure WORLD_ binding")
        self.db.execute(
            "INSERT INTO income_webhooks VALUES (?,?) ON CONFLICT(platform) DO UPDATE SET key_env=excluded.key_env",
            (platform, key_env),
        )
        return {"platform": platform, "key_env": key_env}

    async def receive_income(self, run_id, *, platform, body: bytes, headers: dict):
        from .communications import verify_signed_webhook

        registration = self.db.execute(
            "SELECT key_env FROM income_webhooks WHERE platform=?", (platform,)
        ).fetchone()
        if registration is None:
            raise ValueError("No verified income bridge is registered")
        payload = verify_signed_webhook(body, headers, os.environ.get(registration["key_env"], ""))
        config = self.store.get_run(run_id)["config"]
        if (
            config.get("stage", 0) < 3
            or config.get("research_mode") != "research"
            or not config.get("life_id")
        ):
            raise ValueError(
                "Independent receipts require a research life at the first-contact stage"
            )
        if (
            payload.get("paid") is not True
            or payload.get("currency") != "USD"
            or not payload.get("external_id")
            or not payload.get("counterparty")
        ):
            raise ValueError("The receipt is not an authenticated USD payment")
        communications = getattr(self.world, "services", {}).get("communications")
        author_class = (
            communications.counterparty_class(platform, payload["counterparty"])
            if communications
            else "unknown"
        )
        if author_class not in {"independent_human", "independent_agent"}:
            raise ValueError("Receipt counterparty is not independently classified")
        lives = getattr(self.world, "services", {}).get("lives")
        footprint = getattr(self.world, "services", {}).get("footprint")
        if lives is None or footprint is None:
            raise ValueError("Income needs the life and footprint ledgers")
        usd = amount(payload.get("usd"), positive=True)
        external_id = str(payload["external_id"])
        prior = self.db.execute(
            "SELECT * FROM independent_income WHERE platform=? AND external_id=?",
            (platform, external_id),
        ).fetchone()
        proof = {
            "run_id": run_id,
            "life_id": config["life_id"],
            "counterparty": payload["counterparty"],
            "usd": str(usd),
            "receipt_hash": digest(body),
            "observed_at": now(),
        }
        if prior:
            original = json.loads(prior["payload"])
            if any(original[key] != proof[key] for key in ("life_id", "counterparty", "usd")):
                raise ValueError("The receipt conflicts with its prior recorded payment")
            if prior["state"] == "complete":
                return json.loads(prior["result"])
        else:
            self.db.execute(
                "INSERT INTO independent_income VALUES (?,?,?,'pending',NULL)",
                (platform, external_id, canonical(proof)),
            )
        evidence_ref = f"income:{platform}:{external_id}"
        life = lives.get(config["life_id"])
        can_credit = (
            not life.get("ended") and (life.get("income_rail") or {}).get("kind") == "independent"
        )
        if can_credit:
            driver = lives.status(config["life_id"]).get("driver")
            can_credit = not driver or not driver.get(
                "at_floor", driver["reading"] <= driver["floor"]
            )
        if can_credit:
            credited = lives.credit(
                config["life_id"],
                usd,
                "independent",
                evidence_ref=evidence_ref,
                author_class=author_class,
            )
            if inspect.isawaitable(credited):
                await credited
        existing = next(
            (
                item
                for item in footprint.query(life_id=config["life_id"])
                if item["kind"] == "sale" and item["metadata"].get("evidence_ref") == evidence_ref
            ),
            None,
        )
        item = existing or footprint.record_item(
            run_id,
            None,
            kind="sale",
            platform=platform,
            venue_class="human" if author_class == "independent_human" else "agent_native",
            metadata={
                "evidence_ref": evidence_ref,
                "counterparty": payload["counterparty"],
                "usd": str(usd),
            },
        )
        if not existing:
            footprint.add_reading(
                item["item_id"], str(usd), author_class=author_class, evidence_ref=evidence_ref
            )
        result = {
            "kind": "result",
            "external_id": external_id,
            "usd": str(usd),
            "author_class": author_class,
            "footprint_id": item["item_id"],
            "credited_to_budget": can_credit,
        }
        self.db.execute(
            "UPDATE independent_income SET state='complete',result=? WHERE platform=? AND external_id=?",
            (canonical(result), platform, external_id),
        )
        self._notify_state(run_id, f"income:{platform}:{external_id}")
        return result

    def register_market(
        self, market_id, *, price, source_url, closes_at, title="", observation_ref=None
    ):
        price = amount(price, positive=True)
        if price >= 1:
            raise ValueError("Binary contract price must be strictly between zero and one")
        deadline = datetime.fromisoformat(closes_at)
        if deadline.tzinfo is None or deadline <= datetime.now(UTC):
            raise ValueError("Market close time must be timezone-aware and in the future")
        market = {
            "market_id": market_id,
            "title": title,
            "price": str(price),
            "source_url": https_url(source_url),
            "closes_at": deadline.isoformat(),
            "observed_at": now(),
            "observation_ref": observation_ref,
        }
        self.db.execute(
            "INSERT INTO paper_markets VALUES (?,?,NULL)", (market_id, canonical(market))
        )
        return market

    def mark(self, market_id, *, price, observation_ref):
        row = self.db.execute(
            "SELECT * FROM paper_markets WHERE market_id=?", (market_id,)
        ).fetchone()
        if row is None:
            raise KeyError(market_id)
        if row["outcome"] is not None:
            raise ValueError("Resolved market cannot change price")
        value = amount(price, positive=True)
        if value >= 1 or not observation_ref:
            raise ValueError("Price in (0,1) and world-observed evidence are required")
        market = json.loads(row["payload"])
        market.update(price=str(value), observed_at=now(), observation_ref=observation_ref)
        self.db.execute(
            "UPDATE paper_markets SET payload=? WHERE market_id=?", (canonical(market), market_id)
        )
        self._notify_owners(
            {
                row["owner"]
                for row in self.db.execute(
                    "SELECT DISTINCT owner FROM paper_positions WHERE market_id=?", (market_id,)
                )
            },
            f"market:mark:{market_id}:{observation_ref}",
        )
        return market

    def _evidence(self, run_id, source_url, evidence_ref):
        records = self.store.records(run_id)
        outcomes = {
            r.get("action_id")
            for r in records
            if r["kind"] == "outcome" and r.get("reply", {}).get("kind") == "result"
        }
        threshold = datetime.now(UTC) - timedelta(hours=24)
        candidates = [
            r
            for r in records
            if r["kind"] == "action"
            and r.get("tool") == "fetch"
            and r.get("params", {}).get("url") == source_url
            and r.get("action_id") in outcomes
            and datetime.fromisoformat(r["ts"]) >= threshold
        ]
        if evidence_ref:
            candidates = [
                r
                for r in candidates
                if r.get("action_id") == evidence_ref or r.get("hash") == evidence_ref
            ]
        return candidates[-1]["action_id"] if candidates else None

    def trade(self, run_id, action_id, *, market_id, side, credits, evidence_ref=None):
        duplicate = self.db.execute(
            "SELECT payload FROM paper_fills WHERE action_id=?", (action_id,)
        ).fetchone()
        if duplicate:
            # A retry after an interrupted settlement finishes charging the stake.
            self.reconcile_stakes(run_id)
            return {"kind": "result", "fill": json.loads(duplicate["payload"])}
        owner = self._owner(run_id)
        row = self.db.execute(
            "SELECT * FROM paper_markets WHERE market_id=?", (market_id,)
        ).fetchone()
        if row is None:
            return blocked("policy_deny", "Unknown registered paper market", "market_unknown")
        market = json.loads(row["payload"])
        if row["outcome"] is not None or market["closes_at"] <= now():
            return blocked("policy_deny", "The paper market is closed", "market_closed")
        evidence = self._evidence(run_id, market["source_url"], evidence_ref)
        if evidence is None:
            return blocked(
                "policy_deny",
                "A successful matching fetch under 24 hours old is required",
                "trade_evidence",
            )
        if side not in {"yes", "no", "buy_yes", "buy_no", "sell_yes", "sell_no"}:
            raise ValueError("Trade side must name yes/no or buy/sell_yes/no")
        sell = side.startswith("sell_")
        contract = "no" if side.endswith("no") else "yes"
        price = amount(market["price"])
        price = 1 - price if contract == "no" else price
        credits = amount(credits, positive=True)
        quantity = (credits / price).quantize(
            Decimal("0.000000001"), rounding=ROUND_CEILING if sell else ROUND_FLOOR
        )
        # In a dollar-settled life the stake is the life's own money, held on its ledger.
        dollars = self._settlement(run_id) == "balance"
        stake = None
        if dollars and not sell:
            stake = self._stake(run_id, action_id, credits)
            if stake is None:
                return blocked(
                    "policy_deny", "The balance cannot cover this stake", "market_balance"
                )
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                account = self.db.execute(
                    "SELECT balance FROM paper_accounts WHERE owner=?", (owner,)
                ).fetchone()
                position = self.db.execute(
                    "SELECT quantity FROM paper_positions WHERE owner=? AND market_id=? AND side=?",
                    (owner, market_id, contract),
                ).fetchone()
                balance, held = (
                    amount(account["balance"]),
                    amount(position["quantity"] if position else 0),
                )
                if (sell and held < quantity) or (not sell and not dollars and balance < credits):
                    self.db.execute("ROLLBACK")
                    return blocked(
                        "policy_deny", "Insufficient paper balance or position", "paper_balance"
                    )
                if not dollars:
                    self.db.execute(
                        "UPDATE paper_accounts SET balance=? WHERE owner=?",
                        (str(balance + credits if sell else balance - credits), owner),
                    )
                self.db.execute(
                    "INSERT INTO paper_positions VALUES (?,?,?,?) ON CONFLICT(owner,market_id,side) DO UPDATE SET quantity=excluded.quantity",
                    (owner, market_id, contract, str(held - quantity if sell else held + quantity)),
                )
                fill = {
                    "fill_id": identifier("fill"),
                    "run_id": run_id,
                    "owner": owner,
                    "market_id": market_id,
                    "side": side,
                    "credits": str(credits),
                    "quantity": str(quantity),
                    "price": str(price),
                    "observed_at": now(),
                    "evidence_ref": evidence,
                    "paper": True,
                }
                if dollars:
                    fill["settlement"] = "balance"
                self.db.execute(
                    "INSERT INTO paper_fills VALUES (?,?,?)",
                    (fill["fill_id"], action_id, canonical(fill)),
                )
                if dollars and sell:
                    self.db.execute(
                        "INSERT INTO market_payouts VALUES (?,?,?,?,'pending')",
                        (
                            "market:fill:" + fill["fill_id"],
                            owner,
                            str(credits),
                            f"Sold {contract} on {market_id}",
                        ),
                    )
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                if stake:
                    self.store.release(stake)
                raise
        if stake:
            # Charged only after the position exists; reconcile_stakes finishes an interrupted one.
            self.store.settle(stake, "market", credits)
        if dollars and sell:
            self._settle_payouts()
        self.store.append_record(run_id, "paper_trade", fill=fill)
        self._notify_state(run_id, fill["fill_id"])
        return {"kind": "result", "fill": fill}

    def resolve(self, market_id, *, outcome, observation_ref):
        if not isinstance(outcome, bool) or not observation_ref:
            raise ValueError(
                "A boolean outcome and world-observed resolution evidence are required"
            )
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    "SELECT * FROM paper_markets WHERE market_id=?", (market_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(market_id)
                if row["outcome"] is not None:
                    if bool(row["outcome"]) != outcome:
                        raise ValueError("A resolved outcome cannot be changed")
                    self.db.execute("ROLLBACK")
                    return {"market_id": market_id, "outcome": outcome, "already_resolved": True}
                payouts = []
                for position in self.db.execute(
                    "SELECT * FROM paper_positions WHERE market_id=?", (market_id,)
                ).fetchall():
                    payout = (
                        amount(position["quantity"])
                        if (position["side"] == "yes") == outcome
                        else Decimal(0)
                    )
                    if self._settles_in_dollars(position["owner"]):
                        if payout > 0:
                            reference = ":".join(
                                (
                                    "market:resolution",
                                    market_id,
                                    position["owner"],
                                    position["side"],
                                )
                            )
                            self.db.execute(
                                "INSERT OR IGNORE INTO market_payouts VALUES (?,?,?,?,'pending')",
                                (
                                    reference,
                                    position["owner"],
                                    str(payout),
                                    f"{market_id} resolved {'yes' if outcome else 'no'}",
                                ),
                            )
                        payouts.append({"owner": position["owner"], "usd": str(payout)})
                        continue
                    row_balance = self.db.execute(
                        "SELECT balance FROM paper_accounts WHERE owner=?", (position["owner"],)
                    ).fetchone()
                    self.db.execute(
                        "UPDATE paper_accounts SET balance=? WHERE owner=?",
                        (str(amount(row_balance["balance"]) + payout), position["owner"]),
                    )
                    payouts.append({"owner": position["owner"], "credits": str(payout)})
                self.db.execute("DELETE FROM paper_positions WHERE market_id=?", (market_id,))
                market = json.loads(row["payload"])
                market.update(resolved_at=now(), observation_ref=observation_ref)
                self.db.execute(
                    "UPDATE paper_markets SET outcome=?,payload=? WHERE market_id=?",
                    (int(outcome), canonical(market), market_id),
                )
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        self._settle_payouts()
        self._notify_owners(
            {row["owner"] for row in payouts}, f"market:resolution:{market_id}:{observation_ref}"
        )
        return {"market_id": market_id, "outcome": outcome, "payouts": payouts}

    async def redeem(self, run_id, credits):
        config = self.store.get_run(run_id)["config"]
        if config.get("market_settlement") == "balance":
            return blocked(
                "policy_deny", "Market proceeds already reach your balance", "market_balance"
            )
        life_id = config.get("life_id")
        lives = getattr(self.world, "services", {}).get("lives") or getattr(
            self.world, "services", {}
        ).get("life")
        if not life_id or lives is None:
            return blocked(
                "policy_deny",
                "Redemption needs a life with a declared market income rail",
                "income_rail",
            )
        life = lives.get(life_id)
        rail = life.get("config", life).get("income_rail") or life.get("income_rail")
        if not rail or rail.get("kind") != "market":
            return blocked("policy_deny", "This life has no market income rail", "income_rail")
        credits = amount(credits, positive=True)
        balance = self.balance(run_id)
        if credits > amount(balance["redeemable_profit_credits"]):
            return blocked(
                "policy_deny", "Only realized net paper profits may be redeemed", "paper_profit"
            )
        usd = credits * amount(rail["usd_per_credit"], positive=True)
        redemption_id = identifier("redemption")
        owner = balance["owner"]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "UPDATE paper_accounts SET balance=? WHERE owner=?",
                (str(amount(balance["credits"]) - credits), owner),
            )
            self.db.execute(
                "INSERT INTO paper_redemptions VALUES (?,?,?,?,?,'pending')",
                (redemption_id, owner, run_id, str(credits), str(usd)),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        result = lives.credit(
            life_id,
            usd,
            "market",
            evidence_ref=f"market:redemption:{redemption_id}",
            author_class="sponsor",
        )
        if inspect.isawaitable(result):
            await result
        self.db.execute(
            "UPDATE paper_redemptions SET state='complete' WHERE redemption_id=?", (redemption_id,)
        )
        self._notify_state(run_id, f"market:redemption:{redemption_id}")
        return {
            "kind": "result",
            "redemption_id": redemption_id,
            "credits": str(credits),
            "usd": str(usd),
            "author_class": "sponsor",
        }

    def register_bounty(self, *, title, criterion, usd, sponsor, expires_at):
        value = amount(usd, positive=True)
        if (
            not title
            or not criterion
            or not sponsor
            or datetime.fromisoformat(expires_at) <= datetime.now(UTC)
        ):
            raise ValueError(
                "A bounty needs criterion, sponsor, positive payout, and future expiry"
            )
        bounty = {
            "bounty_id": identifier("bounty"),
            "title": title,
            "criterion": criterion,
            "usd": str(value),
            "sponsor": sponsor,
            "expires_at": expires_at,
            "created_at": now(),
        }
        self.db.execute(
            "INSERT INTO sponsor_bounties VALUES (?,?)", (bounty["bounty_id"], canonical(bounty))
        )
        return bounty

    def award_approved(self, ticket):
        """Pay an approved claim at approval, so a stranded claimant is paid without acting."""
        config = self.store.get_run(ticket["run_id"])["config"]
        life_id = config.get("life_id")
        lives = getattr(self.world, "services", {}).get("lives")
        bank = getattr(self.world, "services", {}).get("bank")
        if not life_id or lives is None or bank is None:
            return None
        if "bounty" not in lives.get(life_id).get("income_channels", []):
            return None  # Rail-declared lives keep collecting through claim_bounty.
        body = ticket["body"]
        row = self.db.execute(
            "SELECT payload FROM sponsor_bounties WHERE bounty_id=?", (body["bounty_id"],)
        ).fetchone()
        if row is None:
            return None
        bounty = json.loads(row["payload"])
        result = bank.deposit(
            life_id,
            amount(bounty["usd"], positive=True),
            "bounty",
            evidence_ref=f"bounty:{body['bounty_id']}:{life_id}",
            author_class="sponsor",
            memo=bounty["title"],
        )
        self.db.execute(
            "INSERT OR IGNORE INTO bounty_awards VALUES (?,?,?)",
            (body["bounty_id"], life_id, body["evidence_ref"]),
        )
        return result

    async def claim_bounty(self, run_id, action_id, bounty_id, evidence_ref):
        row = self.db.execute(
            "SELECT payload FROM sponsor_bounties WHERE bounty_id=?", (bounty_id,)
        ).fetchone()
        if row is None:
            raise KeyError(bounty_id)
        bounty = json.loads(row["payload"])
        if bounty["expires_at"] <= now():
            return blocked("policy_deny", "Bounty has expired", "bounty_expired")
        config = self.store.get_run(run_id)["config"]
        life_id = config.get("life_id")
        owner = self._owner(run_id)
        if self.db.execute(
            "SELECT 1 FROM bounty_awards WHERE bounty_id=? AND owner=?", (bounty_id, owner)
        ).fetchone():
            return blocked(
                "policy_deny", "This bounty was already paid to this identity", "bounty_duplicate"
            )
        asks = getattr(self.world, "services", {}).get("asks")
        lives = getattr(self.world, "services", {}).get("lives") or getattr(
            self.world, "services", {}
        ).get("life")
        if not life_id or not asks or not lives or not evidence_ref:
            return blocked(
                "policy_deny",
                "Bounty claims need a life, evidence, and sponsor adjudication",
                "bounty_claim",
            )
        body = {
            "bounty_id": bounty_id,
            "evidence_ref": evidence_ref,
            "payout_usd": bounty["usd"],
            "sponsor": bounty["sponsor"],
        }
        approval = await asks.require_approval(
            run_id, action_id, body, kind="approval", platform="bounty", scope="bounty_award"
        )
        if approval.get("kind") != "result" or not approval.get("approved"):
            return approval
        result = lives.credit(
            life_id,
            amount(bounty["usd"]),
            "bounty",
            evidence_ref=f"bounty:{bounty_id}:{owner}",
            author_class="sponsor",
        )
        if inspect.isawaitable(result):
            await result
        self.db.execute(
            "INSERT INTO bounty_awards VALUES (?,?,?)", (bounty_id, owner, evidence_ref)
        )
        self._notify_state(run_id, f"bounty:{bounty_id}:{owner}")
        return {
            "kind": "result",
            "bounty_id": bounty_id,
            "usd": bounty["usd"],
            "author_class": "sponsor",
        }

    async def recover(self):
        # Credit is idempotent on its evidence_ref. Resume durable payout and redemption intents.
        self._settle_payouts()
        for row in self.db.execute(
            "SELECT * FROM paper_redemptions WHERE state='pending'"
        ).fetchall():
            config = self.store.get_run(row["run_id"])["config"]
            lives = getattr(self.world, "services", {}).get("lives") or getattr(
                self.world, "services", {}
            ).get("life")
            if lives and config.get("life_id"):
                result = lives.credit(
                    config["life_id"],
                    amount(row["usd"]),
                    "market",
                    evidence_ref=f"market:redemption:{row['redemption_id']}",
                    author_class="sponsor",
                )
                if inspect.isawaitable(result):
                    await result
                self.db.execute(
                    "UPDATE paper_redemptions SET state='complete' WHERE redemption_id=?",
                    (row["redemption_id"],),
                )
        if self.worker is None:

            async def worker():
                while True:
                    runs = self.store.list_runs()
                    for row in self.db.execute("SELECT owner FROM paper_accounts").fetchall():
                        candidates = [
                            run
                            for run in runs
                            if run["run_id"] == row["owner"]
                            or run["config"].get("life_id") == row["owner"]
                        ]
                        if candidates:
                            self.equity(candidates[-1]["run_id"])
                    await asyncio.sleep(60)

            self.worker = asyncio.create_task(worker())

    async def aclose(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await super().aclose()

    @staticmethod
    def _worded(reply):
        """Name a dollar-settled fill's amount in dollars, the unit it actually moved."""
        fill = reply.get("fill") if isinstance(reply, dict) else None
        if fill and fill.get("settlement") == "balance":
            fill = {("usd" if key == "credits" else key): value for key, value in fill.items()}
            return {**reply, "fill": fill}
        return reply

    async def handle(self, run_id, action_id, tool, params):
        if tool == "trade":
            if "usd" in params:
                params = dict(params)
                params["credits"] = params.pop("usd")
            return self._worded(self.trade(run_id, action_id, **params))
        if tool == "market_status" and self._settlement(run_id) == "balance":
            return {
                "kind": "result",
                **self.balance(run_id),
                "markets": [
                    json.loads(r["payload"])
                    for r in self.db.execute("SELECT payload FROM paper_markets")
                ],
            }
        if tool in {"market", "market_status"}:
            return {
                "kind": "result",
                **self.balance(run_id),
                "mark_to_market": self.equity(run_id),
                "markets": [
                    json.loads(r["payload"])
                    for r in self.db.execute("SELECT payload FROM paper_markets")
                ],
            }
        if tool == "redeem":
            return await self.redeem(run_id, **params)
        if tool == "bounties":
            return {
                "kind": "result",
                "items": [
                    json.loads(r["payload"])
                    for r in self.db.execute("SELECT payload FROM sponsor_bounties")
                    if json.loads(r["payload"])["expires_at"] > now()
                ],
            }
        if tool == "claim_bounty":
            return await self.claim_bounty(run_id, action_id, **params)
        raise ValueError("Unknown market tool")
