"""Human-time reservations across session boundaries, on the existing dollar ledger."""

import json
from decimal import Decimal, InvalidOperation
from functools import wraps

from .storage import canonical_json, content_hash
from .util import identifier


def dollars(value):
    if isinstance(value, bool):
        raise TypeError("Invalid human-time dollar amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid human-time dollar amount") from None
    if not amount.is_finite() or amount < 0 or amount * 1_000_000_000 % 1:
        raise ValueError("Human dollars must be finite nonnegative whole nanodollars")
    return amount


def serialized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.store._lock:
            return method(self, *args, **kwargs)

    return locked


class LifeBilling:
    def _initialize_billing(self):
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS life_human_holds ("
            "hold_id TEXT PRIMARY KEY,life_id TEXT NOT NULL,evidence_ref TEXT NOT NULL,"
            "payload TEXT NOT NULL,UNIQUE(life_id,evidence_ref))"
        )

    def _human_hold(self, hold_id):
        row = self.store.db.execute(
            "SELECT payload FROM life_human_holds WHERE hold_id=?", (hold_id,)
        ).fetchone()
        if row is None:
            raise KeyError(hold_id)
        return json.loads(row[0])

    def _billing_runs(self, life):
        return list(
            dict.fromkeys([s["run_id"] for s in life["sessions"]] + life.get("accounting_runs", []))
        )

    def _write_human(self, hold):
        self.store.db.execute(
            "INSERT INTO life_human_holds VALUES(?,?,?,?) ON CONFLICT(hold_id) "
            "DO UPDATE SET payload=excluded.payload",
            (hold["hold_id"], hold["life_id"], hold["evidence_ref"], canonical_json(hold)),
        )

    def _pending_human(self, life_id):
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT payload FROM life_human_holds WHERE life_id=?", (life_id,)
            ).fetchall()
        return sum(
            (
                Decimal(str(item["ceiling_usd"]))
                for item in (json.loads(row[0]) for row in rows)
                if item["state"] in {"pending", "unresolved"}
                and not item.get("backing_reservation_id")
            ),
            Decimal(0),
        )

    @serialized
    def reserve_human(self, life_id, max_usd, *, evidence_ref, source_run_id=None):
        amount = dollars(max_usd)
        if amount <= 0 or not evidence_ref:
            raise ValueError("A human-time hold requires a positive ceiling and evidence")
        with self.store._transaction():
            life = self._load(life_id)
            source_run_id = source_run_id or (
                life["sessions"][-1]["run_id"] if life["sessions"] else None
            )
            source = self.store.get_run(source_run_id) if source_run_id else None
            if (
                not source
                or source["config"].get("life_id") != life_id
                or source["config"].get("human_time_priced") != "in_budget"
            ):
                raise ValueError(
                    "The source session must register in-budget human time for this life"
                )
            prior = self.store.db.execute(
                "SELECT payload FROM life_human_holds WHERE life_id=? AND evidence_ref=?",
                (life_id, evidence_ref),
            ).fetchone()
            if prior:
                hold = json.loads(prior[0])
                if (
                    Decimal(str(hold["ceiling_usd"])) != amount
                    or hold["source_run_id"] != source_run_id
                ):
                    raise ValueError("Human evidence conflicts with its earlier reservation")
                return hold["hold_id"]
            if life["ended"] or life["state"] == "billing_hold" or amount > self._available(life):
                raise ValueError("The life cannot reserve the human-time ceiling")
            active = life["active_run_id"]
            if active and self.store.get_run(active)["state"] != "running":
                active = None
            hold = {
                "hold_id": identifier("lh"),
                "life_id": life_id,
                "source_run_id": source_run_id,
                "evidence_ref": evidence_ref,
                "ceiling_usd": float(amount),
                "state": "pending",
                "billing_run_id": active or identifier("r"),
                "accounting": active is None,
                "backing_reservation_id": None,
                "receipt": None,
                "policy_hash": content_hash(
                    {
                        key: source["config"].get(key)
                        for key in ("human_time_priced", "human_rate_per_minute", "life_id")
                    }
                ),
            }
            self._write_human(hold)
            self._save(
                life,
                "human_time_pending",
                hold_id=hold["hold_id"],
                source_run_id=source_run_id,
                evidence_ref=evidence_ref,
                ceiling_usd=float(amount),
            )
        self._prepare_human(hold["hold_id"])
        return hold["hold_id"]

    @serialized
    def _prepare_human(self, hold_id):
        with self.store._lock:
            hold = self._human_hold(hold_id)
        try:
            if hold["accounting"]:
                try:
                    self.store.get_run(hold["billing_run_id"])
                except KeyError:
                    self.store.create_run(
                        {
                            "run_id": hold["billing_run_id"],
                            "budget": {"usd": hold["ceiling_usd"], "wall_s": 600},
                            "world_accounting": True,
                            "life_id": hold["life_id"],
                            "metering_lane": "human",
                            "measurement_of": hold["source_run_id"],
                            "human_evidence_ref": hold["evidence_ref"],
                        }
                    )
            reservation_id = "human_" + hold_id
            previous = self.store.db.execute(
                "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if previous and (
                previous["run_id"] != hold["billing_run_id"]
                or previous["amount_nano"] != dollars(hold["ceiling_usd"]) * 1_000_000_000
            ):
                raise ValueError("The recovered reservation does not match its intent")
            backing = (
                reservation_id
                if previous
                else self.store.reserve(
                    hold["billing_run_id"], hold["ceiling_usd"], reservation_id=reservation_id
                )
            )
            if backing is None:
                raise ValueError("The billing ledger cannot reserve human time")
            if self.store.get_run(hold["billing_run_id"])["state"] not in {
                "running",
                "frozen",
                "stopping",
            }:
                raise ValueError("The recovered backing ledger sealed with unsettled human work")
            with self.store._transaction():
                hold = self._human_hold(hold_id)
                hold.update(state="reserved", backing_reservation_id=backing)
                life = self._load(hold["life_id"])
                if hold["accounting"] and hold["billing_run_id"] not in life.setdefault(
                    "accounting_runs", []
                ):
                    life["accounting_runs"].append(hold["billing_run_id"])
                self._write_human(hold)
                self._save(
                    life,
                    "human_time_reserved",
                    hold_id=hold_id,
                    billing_run_id=hold["billing_run_id"],
                )
            self._notify_state(hold["life_id"])
        except (OSError, KeyError, ValueError):
            self._freeze_billing(hold_id)
            raise ValueError("The life is held pending human billing reconciliation") from None

    def _freeze_billing(self, hold_id):
        with self.store._transaction():
            hold = self._human_hold(hold_id)
            hold["state"] = "unresolved"
            self._write_human(hold)
            life = self._load(hold["life_id"])
            if life["state"] != "billing_hold":
                life["billing_previous_state"] = life["state"]
            life["state"] = "billing_hold"
            self._save(life, "human_time_unresolved", hold_id=hold_id)

    @serialized
    def settle_human(self, reservation_id, actual_usd, *, evidence_ref=None):
        amount = dollars(actual_usd)
        with self.store._lock:
            hold = self._human_hold(reservation_id)
        if hold["state"] == "committed":
            if Decimal(str(hold["receipt"]["usd"])) != amount:
                raise ValueError("The human settlement conflicts with its earlier charge")
            return hold["receipt"]
        if hold["state"] not in {"reserved", "settling", "unresolved"} or amount > Decimal(
            str(hold["ceiling_usd"])
        ):
            raise ValueError("Human time must fit its accepted reservation")
        if hold.get("actual_usd") is not None and dollars(hold["actual_usd"]) != amount:
            raise ValueError("The human settlement conflicts with its durable intent")
        try:
            with self.store._transaction():
                hold.update(
                    state="settling",
                    actual_usd=float(amount),
                    settlement_evidence=evidence_ref or hold["evidence_ref"],
                )
                self._write_human(hold)
            run = self.store.get_run(hold["billing_run_id"])
            previous = self.store.db.execute(
                "SELECT * FROM reservations WHERE reservation_id=?",
                (hold["backing_reservation_id"],),
            ).fetchone()
            if previous is None:
                raise ValueError("Missing human-time backing reservation")
            records = self.store.records(hold["billing_run_id"])
            proof = any(
                record["kind"] == "ledger_debit"
                and record.get("reservation_id") == hold["backing_reservation_id"]
                and record.get("amount_nano") == int(amount * 1_000_000_000)
                and record.get("category") == "human"
                for record in records
            )
            if previous["state"] == "settled":
                if previous["actual_nano"] != amount * 1_000_000_000 or not proof:
                    raise ValueError("The settled charge has no matching ledger proof")
            elif run["state"] not in {"running", "stopping", "frozen"} or not self.store.settle(
                hold["backing_reservation_id"], "human", amount
            ):
                raise ValueError("Actual human time could not be charged")
            if run["state"] in {"running", "stopping", "frozen"} and not any(
                record["kind"] == "human_time" and record.get("human_hold_id") == reservation_id
                for record in records
            ):
                self.store.append_record(
                    hold["billing_run_id"],
                    "human_time",
                    human_hold_id=reservation_id,
                    source_run_id=hold["source_run_id"],
                    evidence_ref=hold["settlement_evidence"],
                    policy_hash=hold["policy_hash"],
                    usd=float(amount),
                )
            if hold["accounting"]:
                self._seal_human(hold["billing_run_id"])
            receipt = {
                "charged": True,
                "run_id": hold["billing_run_id"],
                "usd": float(amount),
                "evidence_ref": evidence_ref or hold["evidence_ref"],
                "reservation_id": reservation_id,
            }
            with self.store._transaction():
                hold = self._human_hold(reservation_id)
                hold.update(state="committed", receipt=receipt)
                self._write_human(hold)
                life = self._load(hold["life_id"])
                if life["state"] == "billing_hold" and not self._other_unresolved(hold):
                    life["state"] = life.pop("billing_previous_state", "ready")
                self._save(life, "human_time_charged", **receipt)
            self._notify_state(hold["life_id"])
            self._schedule(hold["life_id"])
            self._enforce_money_end(hold["life_id"])
            return receipt
        except (OSError, ValueError, KeyError):
            self._freeze_billing(reservation_id)
            raise ValueError(
                "The life is held pending actual human billing reconciliation"
            ) from None

    def _seal_human(self, run_id):
        records = self.store.records(run_id)
        if records[-1]["kind"] == "seal":
            if (
                not self.store.verify_record(run_id)["valid"]
                or not self.store.reconcile(run_id)["valid"]
            ):
                raise ValueError("Invalid accounting run seal")
        else:
            self.store.append_record(run_id, "beam_up", reason="human_time_accounting")
            self.store.append_record(run_id, "seal", ledger=self.store.reconcile(run_id))
        self.store.update_run(
            run_id,
            state="sealed",
            end={"terminated": "harness_stopped", "reason": "human_time_accounting"},
        )

    @serialized
    def charge_human(self, life_id, amount, *, evidence_ref, source_run_id=None):
        value = dollars(amount)
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT hold_id FROM life_human_holds WHERE life_id=? AND evidence_ref=?",
                (life_id, evidence_ref),
            ).fetchone()
        if value == 0 and row is None:
            return {"charged": True, "run_id": None, "usd": 0, "evidence_ref": evidence_ref}
        hold = (
            row[0]
            if row
            else self.reserve_human(
                life_id, value, evidence_ref=evidence_ref, source_run_id=source_run_id
            )
        )
        return self.settle_human(hold, value, evidence_ref=evidence_ref)

    @serialized
    def release_human(self, reservation_id):
        with self.store._lock:
            hold = self._human_hold(reservation_id)
        if hold["state"] == "released":
            return hold
        if hold["state"] not in {"reserved", "unresolved"} or hold.get("actual_usd") is not None:
            raise ValueError("Human work already incurred must be reconciled")
        if self.store.get_run(hold["billing_run_id"])["state"] not in {
            "running",
            "frozen",
            "stopping",
        }:
            raise ValueError("A terminal source ledger cannot release an unresolved human hold")
        self.store.release(hold["backing_reservation_id"])
        if hold["accounting"]:
            self._seal_human(hold["billing_run_id"])
        with self.store._transaction():
            hold["state"] = "released"
            self._write_human(hold)
            life = self._load(hold["life_id"])
            if life["state"] == "billing_hold" and not self._other_unresolved(hold):
                life["state"] = life.pop("billing_previous_state", "ready")
            self._save(life, "human_time_released", hold_id=reservation_id)
        self._notify_state(hold["life_id"])
        self._schedule(hold["life_id"])
        self._unstrand_if_funded(hold["life_id"], reason="release", hold_id=reservation_id)
        return hold

    def _other_unresolved(self, hold):
        return any(
            json.loads(row[0])["state"] == "unresolved"
            for row in self.store.db.execute(
                "SELECT payload FROM life_human_holds WHERE life_id=? AND hold_id<>?",
                (hold["life_id"], hold["hold_id"]),
            )
        )

    @serialized
    def _recover_billing(self):
        for row in self.store.db.execute("SELECT payload FROM life_human_holds").fetchall():
            hold = json.loads(row[0])
            try:
                if hold["state"] == "pending":
                    self._prepare_human(hold["hold_id"])
                elif (
                    hold["state"] in {"settling", "unresolved"}
                    and hold.get("actual_usd") is not None
                ):
                    self.settle_human(
                        hold["hold_id"],
                        hold["actual_usd"],
                        evidence_ref=hold.get("settlement_evidence"),
                    )
                elif hold["state"] == "reserved":
                    run = self.store.get_run(hold["billing_run_id"])
                    if run["state"] != "running":
                        self._freeze_billing(hold["hold_id"])
            except (ValueError, KeyError, OSError):
                self._freeze_billing(hold["hold_id"])
