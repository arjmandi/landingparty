"""Private capability persistence and signed, single-use exact-action approvals."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

import httpx


def timestamp(value=None):
    return (value or datetime.now(UTC)).isoformat(timespec="microseconds")


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def action_hash(body):
    return "sha256:" + hashlib.sha256(canonical(body).encode()).hexdigest()


def redact(value):
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if key.lower()
            in {
                "password",
                "authorization",
                "x-api-key",
                "api_key",
                "access_token",
                "refresh_token",
                "secret",
                "credential",
                "credentials",
                "cookie",
                "cookies",
                "storage_state",
                "storage_state_ref",
            }
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def private_key(path: Path, size=32):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Private keys must not be symlinks")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        data = path.read_bytes()
        if len(data) != size:
            raise ValueError("Invalid private key length")
        if path.stat().st_mode & 0o077:
            raise ValueError("Private key permissions must be 0600")
        return data
    data = secrets.token_bytes(size)
    with os.fdopen(descriptor, "wb") as target:
        target.write(data)
    return data


class CapabilityDB:
    """One private database shared by asks, approvals, and the vault services."""

    def __init__(self, world):
        self.root = world.store.data_dir / "control"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            self.root / "capabilities.sqlite3", isolation_level=None, check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS asks (
                ticket_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
                action_id TEXT NOT NULL, action_hash TEXT NOT NULL, body TEXT NOT NULL,
                agent_text TEXT NOT NULL, alternatives TEXT NOT NULL, provenance TEXT NOT NULL,
                opened_at TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL,
                ask_class TEXT, answer TEXT, resume_token TEXT NOT NULL,
                platform TEXT, scope TEXT, audience TEXT NOT NULL,
                subgoal_fraction REAL, info_type TEXT, human_seconds REAL NOT NULL DEFAULT 0,
                active_since TEXT, human_reservation TEXT
            );
            CREATE INDEX IF NOT EXISTS asks_run_state ON asks(run_id,state);
            CREATE TABLE IF NOT EXISTS standing_rejections (
                rule_id TEXT PRIMARY KEY, audience TEXT NOT NULL, kind TEXT NOT NULL,
                platform TEXT NOT NULL, scope TEXT NOT NULL, reason TEXT NOT NULL,
                created_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS approval_grants (
                grant_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL, action_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
                state TEXT NOT NULL, payload TEXT NOT NULL, signature TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS human_ledger (
                ticket_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seconds REAL NOT NULL,
                pricing TEXT NOT NULL, usd TEXT
            );
            CREATE TABLE IF NOT EXISTS terms (
                platform TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE, metadata TEXT NOT NULL,
                encrypted BLOB NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
                lease_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, run_id TEXT NOT NULL,
                action_id TEXT NOT NULL, scope TEXT NOT NULL, request_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES accounts(account_id)
            );
            CREATE INDEX IF NOT EXISTS leases_action ON leases(run_id,action_id);
            CREATE TABLE IF NOT EXISTS contacts (
                account_id TEXT NOT NULL, platform TEXT NOT NULL, counterparty TEXT NOT NULL,
                contacted_at TEXT NOT NULL, PRIMARY KEY(account_id,platform,counterparty)
            );
        """)
        (self.root / "capabilities.sqlite3").chmod(0o600)
        self.signing_key = private_key(self.root / "approvals.key")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def sign(self, body):
        return hmac.new(self.signing_key, canonical(body).encode(), hashlib.sha256).hexdigest()


def capabilities(world):
    service = getattr(world, "_capability_database", None)
    if service is None:
        service = CapabilityDB(world)
        world._capability_database = service
    return service


def synchronized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.database.lock:
            return method(self, *args, **kwargs)

    return guarded


class ApprovalLedger:
    def __init__(self, world, *, clock=None):
        self.world, self.store = world, world.store
        self.database = capabilities(world)
        self.clock = clock or (lambda: datetime.now(UTC))

    def _identity(self, run_id):
        identities = getattr(self.world, "services", {}).get("identity")
        if not identities:
            return None
        config = self.store.get_run(run_id)["config"]
        try:
            identity = identities.get(run_id=run_id, life_id=config.get("life_id"))
        except KeyError:
            return None
        if identity.get("ended_at") or (
            config.get("identity_id") and config["identity_id"] != identity["identity_id"]
        ):
            return None
        return identity["identity_id"]

    def grant(self, ticket):
        config = self.store.get_run(ticket["run_id"])["config"]
        payload = {
            "grant_id": "g_" + secrets.token_hex(16),
            "ticket_id": ticket["ticket_id"],
            "run_id": ticket["run_id"],
            "action_hash": ticket["action_hash"],
            "expires_at": ticket["expires_at"],
            "single_use": True,
            "kind": ticket["kind"],
            "life_id": config.get("life_id"),
            "identity_id": self._identity(ticket["run_id"]),
            "stage": config.get("stage", 0),
        }
        signature = self.database.sign(payload)
        with self.database.transaction() as db:
            db.execute(
                "INSERT INTO approval_grants VALUES(?,?,?,?,?,?,?,?)",
                (
                    payload["grant_id"],
                    payload["ticket_id"],
                    payload["run_id"],
                    payload["action_hash"],
                    payload["expires_at"],
                    "available",
                    canonical(payload),
                    signature,
                ),
            )
        if self.store.get_run(ticket["run_id"])["state"] not in {"sealed", "killed", "ended"}:
            self.store.append_record(
                ticket["run_id"],
                "grant",
                **{key: value for key, value in payload.items() if key not in {"run_id", "kind"}},
                grant_kind=payload["kind"],
                signature=signature,
            )
        return {**payload, "signature": signature}

    def _successor(self, payload, run_id):
        source = self.store.get_run(payload["run_id"])
        current = self.store.get_run(run_id)
        life_id = payload.get("life_id")
        lives = getattr(self.world, "services", {}).get("lives")
        if (
            not life_id
            or not lives
            or source["state"] != "sealed"
            or source["config"].get("life_id") != life_id
            or current["config"].get("life_id") != life_id
            or current["config"].get("stage", 0) < payload.get("stage", 0)
            or not payload.get("identity_id")
            or self._identity(payload["run_id"]) != payload["identity_id"]
            or self._identity(run_id) != payload["identity_id"]
        ):
            return False
        life = lives.get(life_id)
        sessions = [session["run_id"] for session in life["sessions"]]
        return (
            not life.get("ended")
            and payload["run_id"] in sessions
            and run_id in sessions
            and sessions.index(run_id) > sessions.index(payload["run_id"])
        )

    def consume(self, run_id, body, *, kind=None):
        if self.store.get_run(run_id)["state"] != "running":
            return None
        wanted = action_hash(body)
        consumed = None
        with self.database.transaction() as db:
            rows = db.execute(
                "SELECT * FROM approval_grants WHERE action_hash=? AND state='available' ORDER BY rowid",
                (wanted,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                if not hmac.compare_digest(row["signature"], self.database.sign(payload)):
                    raise ValueError("Approval signature verification failed")
                if any(
                    payload.get(key) != row[key]
                    for key in ("grant_id", "ticket_id", "run_id", "action_hash", "expires_at")
                ):
                    raise ValueError("Approval signature does not cover the stored action binding")
                if kind is not None and payload.get("kind") != kind:
                    continue
                if datetime.fromisoformat(row["expires_at"]) <= self.clock():
                    db.execute(
                        "UPDATE approval_grants SET state='expired' WHERE grant_id=?",
                        (row["grant_id"],),
                    )
                    continue
                if payload["run_id"] != run_id and not self._successor(payload, run_id):
                    continue
                db.execute(
                    "UPDATE approval_grants SET state='consumed' WHERE grant_id=? AND state='available'",
                    (row["grant_id"],),
                )
                consumed = {**payload, "signature": row["signature"]}
                break
        if consumed and consumed["run_id"] != run_id:
            proof = {
                "grant_id": consumed["grant_id"],
                "ticket_id": consumed["ticket_id"],
                "source_run_id": consumed["run_id"],
                "successor_run_id": run_id,
                "life_id": consumed["life_id"],
                "identity_id": consumed["identity_id"],
                "action_hash": consumed["action_hash"],
                "original_signature": consumed["signature"],
                "consumed_at": timestamp(self.clock()),
            }
            consumed["transfer_proof"] = {**proof, "signature": self.database.sign(proof)}
            self.store.append_record(run_id, "approval_transferred", **consumed["transfer_proof"])
        return consumed

    def revoke_run(self, run_id):
        with self.database.transaction() as db:
            db.execute(
                "UPDATE approval_grants SET state='revoked' WHERE run_id=? AND state='available'",
                (run_id,),
            )


class PaymentNotSubmitted(ValueError):
    """A trusted boundary proved that no payment request was submitted."""

    def __init__(self, rule_id="payment_not_submitted"):
        self.rule_id = rule_id
        super().__init__("The payment was blocked before submission")


class PaymentMandateLedger:
    """Single-use 402/payment mandates; only a verified paid receipt settles spend.

    A trusted payment adapter is researcher configuration. No payment credential
    or adapter URL can be supplied by an agent action or a merchant's page.
    Unknown payment outcomes retain the dollar hold and taint the run, avoiding
    both duplicate payments and claims that unknown spend was free.
    """

    def __init__(self, world, *, adapter=None, clock=None):
        self.world, self.store = world, world.store
        self.database = capabilities(world)
        self.clock = clock or (lambda: datetime.now(UTC))
        endpoint = os.environ.get("WORLD_PAYMENT_API_URL")
        self.adapter = adapter or (HTTPPaymentAdapter(endpoint) if endpoint else None)
        with self.database.lock:
            self.database.db.execute("""CREATE TABLE IF NOT EXISTS payment_mandates (
                mandate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, merchant TEXT NOT NULL,
                action_hash TEXT NOT NULL, payload TEXT NOT NULL, signature TEXT NOT NULL,
                state TEXT NOT NULL, reservation_id TEXT, receipt TEXT
            )""")

    @staticmethod
    def approval_body(merchant, ceiling, purpose, expires_at, action):
        from .vault import origin

        amount = Decimal(str(ceiling))
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Payment ceiling must be finite and positive")
        if not isinstance(action, dict) or not action.get("url"):
            raise ValueError("A payment mandate requires a concrete pending action")
        if origin(merchant) != merchant.rstrip("/") or origin(action["url"]) != origin(merchant):
            raise ValueError("Merchant must be the exact HTTPS origin of the pending action")
        if not merchant.startswith("https://"):
            raise ValueError("Payment merchants require HTTPS")
        expiry = datetime.fromisoformat(expires_at)
        if expiry.tzinfo is None:
            raise ValueError("Mandate expiry must include its UTC offset")
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 1000:
            raise ValueError("A payment mandate requires a bounded purpose")
        return {
            "merchant": origin(merchant),
            "ceiling": str(amount),
            "purpose": purpose,
            "expires_at": timestamp(expiry),
            "action": action,
        }

    async def request(self, run_id, action_id, *, merchant, ceiling, purpose, expires_at, action):
        if (
            self.store.get_run(run_id)["config"].get("stage", 0) < 3
            or self.store.get_run(run_id)["state"] != "running"
        ):
            raise ValueError("Payment mandates require an active first-contact run")
        body = self.approval_body(merchant, ceiling, purpose, expires_at, action)
        asks = getattr(self.world, "services", {}).get("asks")
        if not asks:
            return {
                "kind": "blocked",
                "type": "policy_deny",
                "detail": "A payment requires operator approval",
                "rule_id": "payment_approval",
            }
        existing = asks.approvals.consume(run_id, body, kind="grant_payment")
        if existing:
            return {"kind": "result", "mandate": self._issue(run_id, body, existing)}
        return asks.raise_ticket(
            run_id,
            action_id,
            "grant_payment",
            pending_action=body,
            platform=body["merchant"],
            scope="payment",
        )

    def _issue(self, run_id, body, approval):
        if self.store.get_run(run_id)["config"].get("stage", 0) < 3:
            raise ValueError("Payment mandates require the first-contact stage")
        if datetime.fromisoformat(body["expires_at"]) <= self.clock():
            raise ValueError("A payment mandate cannot already be expired")
        payload = {
            "mandate_id": "mandate_" + secrets.token_hex(16),
            "run_id": run_id,
            "approval_ticket_id": approval["ticket_id"],
            "single_use": True,
            **body,
            "action_hash": action_hash(body["action"]),
        }
        signature = self.database.sign(payload)
        with self.database.transaction() as db:
            db.execute(
                "INSERT INTO payment_mandates VALUES(?,?,?,?,?,?,?,NULL,NULL)",
                (
                    payload["mandate_id"],
                    run_id,
                    body["merchant"],
                    payload["action_hash"],
                    canonical(payload),
                    signature,
                    "available",
                ),
            )
        self.store.append_record(
            run_id,
            "payment_mandate",
            **{key: value for key, value in payload.items() if key != "run_id"},
            signature=signature,
        )
        return {**payload, "signature": signature}

    def create(self, run_id, *, merchant, ceiling, purpose, expires_at, action):
        if (
            self.store.get_run(run_id)["config"].get("stage", 0) < 3
            or self.store.get_run(run_id)["state"] != "running"
        ):
            raise ValueError("Payment mandates require an active first-contact run")
        body = self.approval_body(merchant, ceiling, purpose, expires_at, action)
        asks = getattr(self.world, "services", {}).get("asks")
        grant = asks.approvals.consume(run_id, body, kind="grant_payment") if asks else None
        if not grant:
            raise ValueError("A signed approval for this exact payment mandate is required")
        return self._issue(run_id, body, grant)

    def list_for_run(self, run_id):
        with self.database.lock:
            rows = self.database.db.execute(
                "SELECT * FROM payment_mandates WHERE run_id=?", (run_id,)
            ).fetchall()
        return [
            {
                **json.loads(row["payload"]),
                "state": row["state"],
                "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
            }
            for row in rows
        ]

    async def pay(
        self,
        run_id,
        action_id,
        mandate_id,
        action,
        amount,
        *,
        adapter=None,
        category="paid_content",
    ):
        from .vault import origin

        adapter = adapter or self.adapter
        config = self.store.get_run(run_id)["config"]
        if category not in {"paid_content", "payments"}:
            raise ValueError("Invalid payment ledger category")
        if not adapter:
            return {
                "kind": "blocked",
                "type": "policy_deny",
                "detail": "No payment adapter is configured",
                "rule_id": "payment_adapter",
            }
        if config.get("stage", 0) < 3 or self.store.get_run(run_id)["state"] != "running":
            return {
                "kind": "blocked",
                "type": "policy_deny",
                "detail": "This run cannot make payments",
                "rule_id": "payment_stage",
            }
        if config.get("research_mode", "development") != "research" and not getattr(
            adapter, "test_only", False
        ):
            return {
                "kind": "blocked",
                "type": "policy_deny",
                "detail": "Development runs cannot make live payments",
                "rule_id": "development_boundary",
            }
        value = Decimal(str(amount))
        if not value.is_finite() or value <= 0:
            raise ValueError("Payment amount must be finite and positive")
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM payment_mandates WHERE mandate_id=? AND run_id=?",
                (mandate_id, run_id),
            ).fetchone()
            if not row or row["state"] != "available":
                raise ValueError("Payment mandate is absent, used, or revoked")
            payload = json.loads(row["payload"])
            if not hmac.compare_digest(row["signature"], self.database.sign(payload)) or any(
                payload[key] != row[key]
                for key in ("mandate_id", "run_id", "merchant", "action_hash")
            ):
                raise ValueError("Payment mandate signature verification failed")
            if payload["action_hash"] != action_hash(action) or payload["merchant"] != origin(
                action["url"]
            ):
                raise ValueError("Payment mandate does not authorize this exact merchant action")
            if datetime.fromisoformat(payload["expires_at"]) <= self.clock() or value > Decimal(
                payload["ceiling"]
            ):
                raise ValueError("Payment mandate is expired or its ceiling would be exceeded")
            reservation = self.store.reserve(run_id, value)
            if reservation is None:
                return {
                    "kind": "blocked",
                    "type": "budget_exhausted",
                    "detail": "Remaining budget cannot cover this payment",
                    "rule_id": "payment_budget",
                }
            db.execute(
                "UPDATE payment_mandates SET state='in_flight',reservation_id=? WHERE mandate_id=? AND state='available'",
                (reservation, mandate_id),
            )
        settled = False
        try:
            kwargs = {}
            if isinstance(adapter, HTTPPaymentAdapter):
                kwargs["context"] = {
                    "world": self.world,
                    "run_id": run_id,
                    "action_id": action_id,
                    "mandate_id": mandate_id,
                    "reservation_id": reservation,
                }
            outcome = adapter.pay(
                merchant=payload["merchant"],
                amount=str(value),
                purpose=payload["purpose"],
                action=action,
                payment_id=mandate_id,
                **kwargs,
            )
            receipt = (
                await asyncio.wait_for(outcome, timeout=30)
                if inspect.isawaitable(outcome)
                else outcome
            )
            if (
                isinstance(receipt, dict)
                and receipt.get("paid") is False
                and receipt.get("declined") is True
            ):
                self.store.release(reservation)
                with self.database.transaction() as db:
                    db.execute(
                        "UPDATE payment_mandates SET state='declined' WHERE mandate_id=?",
                        (mandate_id,),
                    )
                settled = True
                self.store.append_record(
                    run_id, "payment_declined", mandate_id=mandate_id, action_id=action_id
                )
                return {
                    "kind": "blocked",
                    "type": "policy_deny",
                    "detail": "Payment was declined",
                    "rule_id": "payment_declined",
                }
            if (
                not isinstance(receipt, dict)
                or receipt.get("paid") is not True
                or not receipt.get("receipt_ref")
                or receipt.get("currency", "USD") != "USD"
                or Decimal(str(receipt.get("amount"))) != value
                or origin(receipt.get("merchant", "")) != payload["merchant"]
            ):
                raise ValueError(
                    "The payment adapter did not return a verifiable matching paid receipt"
                )
            if not self.store.settle(reservation, category, value):
                raise ValueError("The paid receipt exceeds its dollar reservation")
            public = {
                "receipt_ref": receipt["receipt_ref"],
                "amount": str(value),
                "merchant": payload["merchant"],
                "currency": "USD",
            }
            with self.database.transaction() as db:
                db.execute(
                    "UPDATE payment_mandates SET state='paid',receipt=? WHERE mandate_id=?",
                    (canonical(public), mandate_id),
                )
            self.store.append_record(
                run_id,
                "payment",
                action_id=action_id,
                mandate_id=mandate_id,
                category=category,
                cost_usd=float(value),
                **public,
            )
            settled = True
            return {
                "kind": "result",
                "paid": True,
                **public,
                "access_headers": receipt.get("access_headers", {}),
            }
        except PaymentNotSubmitted as exc:
            self.store.release(reservation)
            with self.database.transaction() as db:
                db.execute(
                    "UPDATE payment_mandates SET state='not_submitted' WHERE mandate_id=?",
                    (mandate_id,),
                )
            settled = True
            self.store.append_record(
                run_id,
                "payment_not_submitted",
                mandate_id=mandate_id,
                action_id=action_id,
                rule_id=exc.rule_id,
            )
            return {
                "kind": "blocked",
                "type": "policy_deny",
                "detail": "The payment was blocked before submission",
                "rule_id": exc.rule_id,
            }
        except (
            OSError,
            TimeoutError,
            RuntimeError,
            TypeError,
            ValueError,
            InvalidOperation,
            httpx.HTTPError,
        ):
            return {
                "kind": "blocked",
                "type": "policy_deny",
                "detail": "Payment outcome requires operator reconciliation",
                "rule_id": "payment_uncertain",
            }
        finally:
            if not settled:
                with self.database.transaction() as db:
                    db.execute(
                        "UPDATE payment_mandates SET state='uncertain' WHERE mandate_id=?",
                        (mandate_id,),
                    )
                current = self.store.get_run(run_id)
                self.store.update_run(
                    run_id,
                    tainted=True,
                    **({"state": "frozen"} if current["state"] == "running" else {}),
                )
                self.store.append_record(
                    run_id,
                    "payment_uncertain",
                    action_id=action_id,
                    mandate_id=mandate_id,
                    reservation_id=reservation,
                )

    async def handle_402(self, request):
        run_id = request["run_id"]
        action = {"url": request["url"], "quote_hash": request.get("quote_hash")}
        if not request.get("quote_hash") or request.get("amount") is None:
            return {
                "kind": "blocked",
                "type": "paywall_402",
                "detail": "Payment quote is not verifiable",
                "rule_id": "payment_quote",
            }
        with self.database.lock:
            row = self.database.db.execute(
                "SELECT mandate_id FROM payment_mandates WHERE run_id=? AND action_hash=? AND state='available' ORDER BY rowid LIMIT 1",
                (run_id, action_hash(action)),
            ).fetchone()
        if row:
            return await self.pay(
                run_id,
                request.get("action_id", "payment_402"),
                row["mandate_id"],
                action,
                request["amount"],
            )
        return {
            "kind": "blocked",
            "type": "paywall_402",
            "detail": "No approved payment mandate matches this quote",
            "rule_id": "payment_mandate",
            "price": request["amount"],
            "merchant": request.get("merchant"),
        }

    async def recover(self):
        with self.database.transaction() as db:
            rows = db.execute(
                "SELECT mandate_id,run_id FROM payment_mandates WHERE state='in_flight'"
            ).fetchall()
            db.execute("UPDATE payment_mandates SET state='uncertain' WHERE state='in_flight'")
        for row in rows:
            self.store.update_run(row["run_id"], tainted=True)

    async def aclose(self):
        closer = getattr(self.adapter, "aclose", None)
        if closer:
            outcome = closer()
            if inspect.isawaitable(outcome):
                await outcome


class HTTPPaymentAdapter:
    """Researcher-owned HTTPS broker with idempotent requests and paid receipts.

    The operator configures the endpoint and key binding. Merchant pages and
    agent tool arguments cannot choose either. The broker's receipt is checked
    independently by PaymentMandateLedger before any dollars are reported paid.
    """

    test_only = False

    def __init__(self, endpoint, *, key_env="WORLD_PAYMENT_API_KEY", client=None):
        from urllib.parse import urlsplit

        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Payment broker requires a configured credential-free HTTPS endpoint")
        self.endpoint, self.key_env = endpoint, key_env
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        self._owns_client = client is None

    async def pay(self, *, merchant, amount, purpose, action, payment_id, context=None):
        context = context or {}
        world = context.get("world")
        if world is None or not hasattr(world, "submit_payment"):
            raise PaymentNotSubmitted("payment_boundary_missing")
        key = os.getenv(self.key_env)
        if not key:
            raise PaymentNotSubmitted("payment_credential_missing")
        request = self.client.build_request(
            "POST",
            self.endpoint,
            headers={"Authorization": "Bearer " + key, "Idempotency-Key": payment_id},
            json={
                "merchant": merchant,
                "amount": amount,
                "currency": "USD",
                "purpose": purpose,
                "action": action,
                "payment_id": payment_id,
            },
        )
        response = await world.submit_payment(
            context["run_id"],
            context["action_id"],
            request,
            mandate_id=context["mandate_id"],
            reservation_id=context["reservation_id"],
        )
        if isinstance(response, dict):
            if response.get("kind") == "blocked" and response.get("egress_submitted") is False:
                raise PaymentNotSubmitted(response.get("rule_id", "payment_not_submitted"))
            raise TypeError("The payment boundary returned no verifiable receipt")
        if response.status_code == 402:
            return {"paid": False, "declined": True}
        if not 200 <= response.status_code < 300:
            raise httpx.HTTPStatusError(
                "Payment broker did not return a paid receipt",
                request=response.request,
                response=response,
            )
        return response.json()

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()
