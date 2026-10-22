"""Encrypted, fill-only delegation broker. No agent tool returns credentials."""

from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .approvals import action_hash, canonical, capabilities, private_key, timestamp

SCOPES = {"read", "post", "message", "purchase", "settings"}
KINDS = {"password", "oauth", "cookie_only", "api_key"}
LABELS = {"agent_native", "bot_labelled", "human_registered"}
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


def blocked(detail, rule_id="vault_scope", kind="policy_deny"):
    return {"kind": "blocked", "type": kind, "detail": detail, "rule_id": rule_id}


def origin(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("An account origin must be an HTTP(S) origin without credentials")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Remote credentials require HTTPS")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def normalized_path(url):
    path = urlsplit(url).path or "/"
    for _ in range(5):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    else:
        raise ValueError("Ambiguous URL path encoding")
    if "\\" in path or "\x00" in path:
        raise ValueError("Ambiguous URL path separators")
    return posixpath.normpath("/" + path.lstrip("/"))


class VaultService:
    def __init__(self, world, *, clock=None, key=None, terms=None):
        self.world, self.store = world, world.store
        self.database = capabilities(world)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._provided_key = key
        self._fernet = None
        self.root = self.store.data_dir / "vault"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        source = Path(__file__).parents[1] / "policy/terms.json"
        self.seed_terms = (
            terms if terms is not None else json.loads(source.read_text())["platforms"]
        )

    def _cipher(self):
        if self._fernet is None:
            from cryptography.fernet import Fernet

            value = self._provided_key or os.getenv("WORLD_VAULT_KEY")
            if value is None:
                value = base64.urlsafe_b64encode(private_key(self.root / "master.key"))
            if isinstance(value, str):
                value = value.encode()
            self._fernet = Fernet(value)
        return self._fernet

    def review_terms(
        self,
        platform,
        terms_class,
        *,
        source_url,
        reviewed_at=None,
        origin_url=None,
        agent_native=False,
        permits_human_registered_agents=False,
    ):
        """Researcher control; a fresh documented review is required for every platform."""
        if not _ALIAS.fullmatch(platform) or terms_class not in {
            "permits",
            "permits_labelled",
            "forbids",
            "unknown",
        }:
            raise ValueError("Invalid platform or terms class")
        if not source_url or not isinstance(source_url, str):
            raise ValueError("A terms review must cite its source")
        review = self.clock() if reviewed_at is None else datetime.fromisoformat(reviewed_at)
        if review.tzinfo is None or review > self.clock():
            raise ValueError("Terms review time must be an observed, nonfuture UTC time")
        seed = self.seed_terms.get(platform, {})
        value = {
            "platform": platform,
            "terms_class": terms_class,
            "terms_reviewed_at": timestamp(review),
            "source_url": source_url,
            "origin": origin(origin_url or seed.get("origin", source_url)),
            "agent_native": bool(agent_native or seed.get("agent_native")),
            "permits_human_registered_agents": bool(permits_human_registered_agents),
        }
        with self.database.transaction() as db:
            db.execute("INSERT OR REPLACE INTO terms VALUES(?,?)", (platform, canonical(value)))
        return value

    def terms(self, platform):
        with self.database.lock:
            row = self.database.db.execute(
                "SELECT payload FROM terms WHERE platform=?", (platform,)
            ).fetchone()
        return (
            json.loads(row["payload"])
            if row
            else {
                "platform": platform,
                **self.seed_terms.get(platform, {"terms_class": "unknown"}),
                "terms_reviewed_at": None,
            }
        )

    def _validate_terms(self, platform, label):
        terms = self.terms(platform)
        if terms["terms_class"] in {"unknown", "forbids"}:
            raise ValueError("Platform terms do not permit delegated agent access")
        reviewed = terms.get("terms_reviewed_at")
        if not reviewed or self.clock() - datetime.fromisoformat(reviewed) > timedelta(days=90):
            raise ValueError("A current, source-cited platform terms review is required")
        if terms["terms_class"] == "permits_labelled" and label not in {
            "bot_labelled",
            "agent_native",
        }:
            raise ValueError("This platform requires a labelled agent account")
        if label == "agent_native" and not terms.get("agent_native"):
            raise ValueError("An agent-native account label requires an agent-native venue")
        if label == "human_registered" and not terms.get("permits_human_registered_agents"):
            raise ValueError(
                "Human-registered account delegation requires explicit platform permission"
            )
        return terms

    def provision(
        self,
        *,
        alias,
        platform,
        identity_id,
        kind,
        label,
        credential=None,
        credentials=None,
        scope_classes=None,
        scopes=None,
        principal=None,
        agent_identity=None,
        run_id=None,
        origin_url=None,
        scope_rules=None,
    ):
        """Researcher-only secure deposit. Returns metadata, never the deposited values."""
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
            raise ValueError("Invalid account alias")
        if kind not in KINDS or label not in LABELS:
            raise ValueError("Invalid credential kind or account label")
        scopes = list(scope_classes or scopes or ["read"])
        if not scopes or not set(scopes).issubset(SCOPES):
            raise ValueError("Invalid delegation scopes")
        if not identity_id or not principal:
            raise ValueError("An account requires an identity and principal of record")
        value = credential if credential is not None else credentials
        if not isinstance(value, dict) or not value:
            raise ValueError("A secure deposit must contain a credential object")
        terms = self._validate_terms(platform, label)
        account_origin = origin(origin_url or terms["origin"])
        rules = scope_rules or {"read": [account_origin + "/"]}
        for scope, prefixes in rules.items():
            if scope not in scopes or not isinstance(prefixes, list) or not prefixes:
                raise ValueError("Scope rules must name delegated scopes and URL prefixes")
            for prefix in prefixes:
                if origin(prefix) != account_origin:
                    raise ValueError("A delegation's URL scope cannot escape its account origin")
        metadata = {
            "account_id": "acct_" + secrets.token_hex(16),
            "alias": alias,
            "platform": platform,
            "identity_id": identity_id,
            "kind": kind,
            "label": label,
            "terms_class": terms["terms_class"],
            "terms_reviewed_at": terms["terms_reviewed_at"],
            "scope_classes": sorted(set(scopes)),
            "origin": account_origin,
            "scope_rules": rules,
            "state": "ready",
            "attribution": {
                "principal": principal,
                "agent_identity": agent_identity or identity_id,
                "run_id": run_id,
            },
        }
        encrypted = self._cipher().encrypt(canonical(value).encode())
        with self.database.transaction() as db:
            if db.execute("SELECT 1 FROM accounts WHERE alias=?", (alias,)).fetchone():
                raise ValueError("Account alias already exists; use a secure credential update")
            db.execute(
                "INSERT INTO accounts VALUES(?,?,?,?,?,?)",
                (
                    metadata["account_id"],
                    alias,
                    canonical(metadata),
                    encrypted,
                    timestamp(self.clock()),
                    "ready",
                ),
            )
        if run_id and self.store.get_run(run_id)["state"] not in {"sealed", "killed", "ended"}:
            self.store.append_record(
                run_id,
                "credential_deposited",
                account_id=metadata["account_id"],
                alias=alias,
                platform=platform,
                kind_requested=kind,
            )
        return metadata

    def get_account(self, alias, *, run_id=None):
        with self.database.lock:
            row = self.database.db.execute(
                "SELECT metadata,state FROM accounts WHERE alias=? OR account_id=?", (alias, alias)
            ).fetchone()
        if not row:
            raise KeyError(alias)
        value = {**json.loads(row["metadata"]), "state": row["state"]}
        if run_id:
            identity, principal = self._run_identity(run_id)
            if identity != value["identity_id"]:
                raise ValueError("Account belongs to another agent identity")
            if principal and value["attribution"]["principal"] != principal:
                raise ValueError("Account belongs to another principal of record")
        return value

    def _run_identity(self, run_id):
        config = self.store.get_run(run_id)["config"]
        claimed = config.get("identity_id")
        identities = getattr(self.world, "services", {}).get("identity")
        if identities:
            try:
                identity = identities.get(run_id=run_id, life_id=config.get("life_id"))
            except KeyError:
                raise ValueError(
                    "No disclosed identity is registered for this run or life"
                ) from None
            if claimed and claimed != identity["identity_id"]:
                raise ValueError("The configured identity does not belong to this run or life")
            return identity["identity_id"], identity.get("principal")
        # Direct service fixtures may supply a trusted identity in run config;
        # production World always registers IdentityService.
        if not claimed:
            raise ValueError("Delegated credentials require a disclosed run identity")
        return claimed, None

    def catalogue(self, run_id):
        config = self.store.get_run(run_id)["config"]
        if config.get("vault_mode", "identity_only") != "catalogue":
            return []
        with self.database.lock:
            aliases = [
                row["alias"] for row in self.database.db.execute("SELECT alias FROM accounts")
            ]
        result = []
        for alias in aliases:
            try:
                result.append(self.get_account(alias, run_id=run_id))
            except ValueError:
                continue
        return result

    def briefing(self, identity_id, mode="identity_only"):
        """Safe pre-run inventory, disclosed only in the registered catalogue cell."""
        if mode not in {"identity_only", "catalogue", "on_ask"}:
            raise ValueError("Unknown vault briefing mode")
        if mode != "catalogue" or not identity_id:
            return []
        with self.database.lock:
            rows = self.database.db.execute("SELECT metadata,state FROM accounts").fetchall()
        result = []
        for row in rows:
            account = json.loads(row["metadata"])
            if account["identity_id"] == identity_id:
                result.append(
                    {
                        "alias": account["alias"],
                        "platform": account["platform"],
                        "state": row["state"],
                    }
                )
        return sorted(result, key=lambda account: account["alias"])

    def request_within_scope(self, account, scope, request):
        if scope not in account["scope_classes"]:
            return False
        url = request.get("url")
        if not isinstance(url, str):
            return False
        try:
            if origin(url) != account["origin"]:
                return False
        except ValueError:
            return False
        try:
            path = normalized_path(url)
        except ValueError:
            return False
        for prefix in account["scope_rules"].get(scope, []):
            parsed = urlsplit(prefix)
            prefix_path = normalized_path(prefix)
            if parsed.path.endswith("/"):
                prefix_path += "/" if prefix_path != "/" else ""
            # A prefix ending at a path segment cannot authorize /pay-elsewhere.
            if path == prefix_path.rstrip("/") or path.startswith(
                prefix_path if prefix_path.endswith("/") else prefix_path + "/"
            ):
                return True
        return False

    def _first_contact(self, account, scope, request):
        if scope not in {"message", "post"}:
            return False
        counterparty = str(
            request.get("recipient")
            or request.get("to")
            or request.get("counterparty")
            or "__platform__"
        )
        with self.database.lock:
            exists = self.database.db.execute(
                "SELECT 1 FROM contacts WHERE account_id=? AND platform=? AND counterparty=?",
                (account["account_id"], account["platform"], counterparty),
            ).fetchone()
        return not exists

    def record_contact(self, alias, *, counterparty=None):
        account = self.get_account(alias)
        with self.database.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO contacts VALUES(?,?,?,?)",
                (
                    account["account_id"],
                    account["platform"],
                    str(counterparty or "__platform__"),
                    timestamp(self.clock()),
                ),
            )

    async def authorize(self, run_id, action_id, alias, scope, request):
        run = self.store.get_run(run_id)
        config = run["config"]
        if run["state"] != "running":
            return blocked("This run cannot use delegated credentials", "run_state")
        if config.get("stage", 0) < 2:
            return blocked("Delegated accounts are available from the outpost stage", "stage_gate")
        try:
            account = self.get_account(alias, run_id=run_id)
            self._validate_terms(account["platform"], account["label"])
        except KeyError:
            asks = getattr(self.world, "services", {}).get("asks")
            if asks:
                return asks.raise_ticket(
                    run_id,
                    action_id,
                    "grant_credential",
                    pending_action={"alias": alias, "scope": scope, "request": request},
                )
            return blocked("This account has not been provisioned", "account_missing", "login_wall")
        except ValueError as exc:
            return blocked(str(exc), "platform_terms", "platform_block")
        if account["state"] != "ready":
            return blocked("This account is not currently usable", "account_state", "login_wall")
        if not self.request_within_scope(account, scope, request):
            return blocked("The requested action is outside this delegation's scope", "lease_scope")
        operation = request.get("operation") or request.get("tool")
        pack = (
            "mail"
            if operation == "send"
            else "social"
            if operation in {"post", "reply", "follow", "vote"}
            else "browse"
        )
        grants = getattr(self.world, "services", {}).get("grants")
        supplemental = grants is not None and pack not in config.get("packs", [])
        if supplemental:
            clearance = grants.require(
                run_id,
                scope,
                platform=account["platform"],
                url=request["url"],
                action_hash=action_hash(request),
                consume=False,
                tool=operation,
            )
            if clearance.get("kind") != "result":
                return clearance
        if scope == "purchase" or self._first_contact(account, scope, request):
            asks = getattr(self.world, "services", {}).get("asks")
            if not asks:
                return blocked("This action requires an operator approval", "approval_required")
            body = {
                "platform": account["platform"],
                "account_id": account["account_id"],
                "scope": scope,
                "request": request,
            }
            verdict = await asks.require_approval(
                run_id, action_id, body, platform=account["platform"], scope=scope
            )
            if verdict.get("kind") != "result" or not verdict.get("approved"):
                return verdict
        clearance = None
        if supplemental:
            clearance = grants.require(
                run_id,
                scope,
                platform=account["platform"],
                url=request["url"],
                action_hash=action_hash(request),
                consume=True,
                action_id=action_id,
                tool=operation,
            )
            if clearance.get("kind") != "result":
                return clearance
        expiry = self.clock() + timedelta(hours=2)
        lease = {
            "lease_id": "lease_" + secrets.token_hex(16),
            "account_id": account["account_id"],
            "run_id": run_id,
            "action_id": action_id,
            "scopes": [scope],
            "request_hash": action_hash(request),
            "expires_at": timestamp(expiry),
        }
        if clearance:
            lease["grant_id"] = clearance["grant_id"]
        with self.database.transaction() as db:
            db.execute(
                "INSERT INTO leases VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    lease["lease_id"],
                    account["account_id"],
                    run_id,
                    action_id,
                    scope,
                    lease["request_hash"],
                    lease["expires_at"],
                    "issued",
                    timestamp(self.clock()),
                ),
            )
        self.store.append_record(
            run_id,
            "lease_issued",
            **{key: value for key, value in lease.items() if key != "run_id"},
        )
        return {"kind": "result", "lease": lease}

    def consume(self, lease_id, run_id, action_id, scope, request):
        """Internal fill-only call. The returned object must never become a tool reply."""
        from cryptography.fernet import InvalidToken

        if self.store.get_run(run_id)["state"] != "running":
            raise ValueError("This run cannot use delegated credentials")

        with self.database.transaction() as db:
            row = db.execute(
                "SELECT l.*,a.encrypted,a.metadata,a.state AS account_state FROM leases l JOIN accounts a ON a.account_id=l.account_id WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
            if (
                not row
                or row["run_id"] != run_id
                or row["action_id"] != action_id
                or row["scope"] != scope
                or row["request_hash"] != action_hash(request)
            ):
                raise ValueError("Lease does not authorize this exact action")
            if (
                row["state"] != "issued"
                or row["account_state"] != "ready"
                or datetime.fromisoformat(row["expires_at"]) <= self.clock()
            ):
                raise ValueError("Lease is used, revoked, expired, or unavailable")
            account = json.loads(row["metadata"])
            identity_id, principal = self._run_identity(run_id)
            if identity_id != account["identity_id"] or (
                principal and principal != account["attribution"]["principal"]
            ):
                raise ValueError("The lease account no longer belongs to this run identity")
            self._validate_terms(account["platform"], account["label"])
            try:
                credential = json.loads(self._cipher().decrypt(row["encrypted"]))
            except (InvalidToken, ValueError):
                raise ValueError(
                    "Secure credential could not be opened; a new deposit is required"
                ) from None
            db.execute(
                "UPDATE leases SET state='used' WHERE lease_id=? AND state='issued'", (lease_id,)
            )
        self.store.append_record(
            run_id,
            "credential_used",
            lease_id=lease_id,
            account_id=row["account_id"],
            action_id=action_id,
            scopes=[scope],
            request_hash=row["request_hash"],
        )
        return credential

    def replace_credential(self, alias, credential):
        if not isinstance(credential, dict) or not credential:
            raise ValueError("Credential must be a nonempty object")
        encrypted = self._cipher().encrypt(canonical(credential).encode())
        with self.database.transaction() as db:
            changed = db.execute(
                "UPDATE accounts SET encrypted=?,state='ready' WHERE alias=?", (encrypted, alias)
            ).rowcount
            if not changed:
                raise KeyError(alias)
        return self.get_account(alias)

    def update_storage_state(self, alias, state):
        """Internal browser save; never revive revoked accounts or stale secrets."""
        if not isinstance(state, dict):
            raise TypeError("Browser storage state must be an object")
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT encrypted,state FROM accounts WHERE alias=?", (alias,)
            ).fetchone()
            if not row:
                raise KeyError(alias)
            if row["state"] == "ready":
                credential = json.loads(self._cipher().decrypt(row["encrypted"]))
                encrypted = self._cipher().encrypt(
                    canonical({**credential, "storage_state": state}).encode()
                )
                db.execute(
                    "UPDATE accounts SET encrypted=? WHERE alias=? AND state='ready'",
                    (encrypted, alias),
                )
        return self.get_account(alias)

    def revoke(self, alias, *, reason="operator_revoked"):
        account = self.get_account(alias)
        with self.database.transaction() as db:
            db.execute(
                "UPDATE accounts SET state='revoked' WHERE account_id=?", (account["account_id"],)
            )
            db.execute(
                "UPDATE leases SET state='revoked' WHERE account_id=? AND state='issued'",
                (account["account_id"],),
            )
        return {"account_id": account["account_id"], "state": "revoked", "reason": reason}

    revoke_account = revoke

    def revoke_run(self, run_id):
        with self.database.transaction() as db:
            return db.execute(
                "UPDATE leases SET state='revoked' WHERE run_id=? AND state='issued'", (run_id,)
            ).rowcount

    async def handle(self, run_id, action_id, tool, params):
        if tool == "open_session":
            browser = getattr(self.world, "services", {}).get("browser")
            if not browser:
                return blocked("Browser service is unavailable", "browser_unavailable")
            return await browser.handle(run_id, action_id, tool, params)
        raise ValueError("Unknown vault tool")

    async def recover(self):
        # Browser processes never survive a restored machine; old unconsumed leases do not either.
        with self.database.transaction() as db:
            db.execute("UPDATE leases SET state='revoked' WHERE state='issued'")

    async def aclose(self):
        return None
