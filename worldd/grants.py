"""Signed capability deltas, replay-proxy rehearsals, and revocable clearances.

K supplements the initial tool/delegation set. It never overrides a stage gate,
platform terms, an account scope, disclosure, or an exact-action approval.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .approvals import private_key, redact
from .storage import GENESIS_HASH, canonical_json, content_hash
from .util import blocked, identifier

ACTION_CLASSES = {
    "read",
    "write_sandbox",
    "contact_operator",
    "post",
    "message",
    "purchase",
    "settings",
    "spend",
}
PACK_STAGE = {
    "deliver": 0,
    "watch": 0,
    "forecast": 0,
    "belief": 1,
    "schedule": 1,
    "objective": 1,
    "bounty": 2,
    "reflect": 1,
    "vault": 2,
    "browse": 2,
    "mail": 2,
    "social": 2,
    "footprint": 2,
    "market": 2,
    "perception": 2,
}


def _time(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("Capability timestamps must include a timezone")
    return result.astimezone(UTC)


def _origin(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Capability origins must use credential-free HTTPS")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("An origin cannot contain a path, query, or fragment")
    return f"https://{parsed.netloc.lower()}"


def _path(value):
    if not isinstance(value, str) or not value.startswith("/") or "?" in value or "#" in value:
        raise ValueError("Scope paths must be absolute URL paths")
    decoded = value
    for _ in range(5):
        following = unquote(decoded)
        if following == decoded:
            break
        decoded = following
    else:
        raise ValueError("Ambiguous path encoding")
    if (
        "\\" in decoded
        or "\x00" in decoded
        or any(part in {".", ".."} for part in decoded.split("/"))
    ):
        raise ValueError("Capability paths cannot contain traversal")
    return decoded.rstrip("/") or "/"


class GrantService:
    def __init__(self, world, *, clock=None, rehearsal_executor=None):
        self.world = world
        self.store = world.store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.rehearsal_executor = rehearsal_executor
        self._locks = {}
        root = self.store.data_dir / "control"
        if root.resolve() != root:
            raise ValueError("Capability signing keys must remain inside the world data directory")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.key = Ed25519PrivateKey.from_private_bytes(private_key(root / "grant-signing.key"))
        public = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.public_key = base64.b64encode(public).decode()
        self.key_id = content_hash({"algorithm": "Ed25519", "public_key": self.public_key})
        with self.store._lock:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS capability_clearances (
                    grant_id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(run_id),
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_deltas (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),seq INTEGER NOT NULL,
                    payload TEXT NOT NULL,PRIMARY KEY(run_id,seq)
                );
                CREATE TABLE IF NOT EXISTS capability_rehearsals (
                    receipt_id TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES capability_clearances(grant_id),
                    payload TEXT NOT NULL,state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_uses (
                    grant_id TEXT NOT NULL REFERENCES capability_clearances(grant_id),
                    action_id TEXT NOT NULL,action_hash TEXT,used_at TEXT NOT NULL,
                    PRIMARY KEY(grant_id,action_id)
                );
                """
            )

    def _now(self):
        return _time(self.clock())

    def _load(self, grant_id):
        row = self.store.db.execute(
            "SELECT payload FROM capability_clearances WHERE grant_id=?", (grant_id,)
        ).fetchone()
        if row is None:
            raise KeyError(grant_id)
        return json.loads(row[0])

    def get(self, grant_id):
        with self.store._lock:
            return self._load(grant_id)

    def _delta(self, grant, kind, *, actor="world", **data):
        run_id = grant["run_id"]
        row = self.store.db.execute(
            "SELECT seq,payload FROM capability_deltas WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        payload = {
            "seq": row[0] + 1 if row else 1,
            "run_id": run_id,
            "grant_id": grant["grant_id"],
            "kind": kind,
            "actor": actor,
            "recorded_at": self._now().isoformat(),
            "key_id": self.key_id,
            "prev_hash": json.loads(row[1])["hash"] if row else GENESIS_HASH,
            "data": {"grant": grant, **redact(data)},
        }
        payload["signature"] = base64.b64encode(
            self.key.sign(canonical_json(payload).encode())
        ).decode()
        payload["hash"] = content_hash(payload)
        self.store.db.execute(
            "INSERT INTO capability_deltas VALUES(?,?,?)",
            (run_id, payload["seq"], canonical_json(payload)),
        )
        self.store.db.execute(
            "UPDATE capability_clearances SET payload=? WHERE grant_id=?",
            (canonical_json(grant), grant["grant_id"]),
        )
        return payload

    def ledger(self, run_id):
        self.store.get_run(run_id)
        with self.store._lock:
            return [
                json.loads(row[0])
                for row in self.store.db.execute(
                    "SELECT payload FROM capability_deltas WHERE run_id=? ORDER BY seq", (run_id,)
                )
            ]

    def export(self, run_id):
        return {
            "run_id": run_id,
            "key_id": self.key_id,
            "algorithm": "Ed25519",
            "public_key": self.public_key,
            "deltas": self.ledger(run_id),
            "verification": self.verify(run_id),
        }

    def verify(self, run_id):
        with self.store._lock:
            deltas = self.ledger(run_id)
            previous = GENESIS_HASH
            heads = {}
            for seq, delta in enumerate(deltas, 1):
                signed = {
                    key: value for key, value in delta.items() if key not in {"signature", "hash"}
                }
                unhashed = {key: value for key, value in delta.items() if key != "hash"}
                try:
                    self.key.public_key().verify(
                        base64.b64decode(delta["signature"], validate=True),
                        canonical_json(signed).encode(),
                    )
                except (InvalidSignature, ValueError, KeyError):
                    return {
                        "valid": False,
                        "reason": "Capability delta signature failed",
                        "count": len(deltas),
                    }
                if (
                    delta["seq"] != seq
                    or delta["prev_hash"] != previous
                    or delta["hash"] != content_hash(unhashed)
                    or delta["key_id"] != self.key_id
                ):
                    return {
                        "valid": False,
                        "reason": "Capability chain failed",
                        "count": len(deltas),
                    }
                heads[delta["grant_id"]] = delta["data"]["grant"]
                previous = delta["hash"]
            rows = {
                row["grant_id"]: json.loads(row["payload"])
                for row in self.store.db.execute(
                    "SELECT grant_id,payload FROM capability_clearances WHERE run_id=?", (run_id,)
                )
            }
            if heads != rows:
                return {
                    "valid": False,
                    "reason": "Materialized capabilities differ from signed ledger",
                    "count": len(deltas),
                }
            return {"valid": True, "count": len(deltas), "head": previous, "key_id": self.key_id}

    def grant(
        self,
        run_id,
        *,
        packs=None,
        tools=None,
        action_classes=None,
        platform=None,
        origins=None,
        path_prefixes=None,
        expires_at=None,
        purpose,
        standing=False,
        max_uses=None,
        action_hash=None,
        archive_in=None,
        proxy_ref=None,
        owner="researcher",
    ):
        from .models import PACK_TOOLS, TOOL_MODELS

        run = self.store.get_run(run_id)
        if run["state"] != "running":
            raise ValueError("Only a running run can receive a new clearance")
        if not self.verify(run_id)["valid"]:
            raise ValueError("The capability ledger failed verification")
        packs = sorted(set(packs or []))
        tools = sorted(set(tools or []))
        classes = sorted(set(action_classes or ["read"]))
        if not packs and not tools:
            raise ValueError("A clearance must declare a pack or tool")
        if set(packs) - set(PACK_TOOLS) or set(tools) - set(TOOL_MODELS):
            raise ValueError("A clearance cannot invent unregistered tools")
        if set(classes) - ACTION_CLASSES or not classes:
            raise ValueError("Unknown capability action class")
        required_packs = set(packs) | {
            pack for pack, names in PACK_TOOLS.items() if set(names) & set(tools)
        }
        if any(run["config"].get("stage", 0) < PACK_STAGE[pack] for pack in required_packs):
            raise ValueError("A clearance cannot bypass the registered stage gate")
        if not isinstance(purpose, str) or not purpose.strip() or not owner:
            raise ValueError("A clearance requires a purpose and accountable owner")
        if platform is not None and (
            not isinstance(platform, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", platform)
        ):
            raise ValueError("Invalid platform scope")
        origins = sorted({_origin(item) for item in origins or []})
        paths = sorted({_path(item) for item in path_prefixes or []})
        if paths and not origins:
            raise ValueError("Path restrictions require explicit origins")
        if not isinstance(standing, bool):
            raise TypeError("Standing must be a boolean")
        if max_uses is None and not standing:
            max_uses = 1
        if max_uses is not None and (
            isinstance(max_uses, bool) or not isinstance(max_uses, int) or max_uses < 1
        ):
            raise ValueError("A maximum use count must be a positive integer")
        if action_hash is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", action_hash):
            raise ValueError("An exact-action scope must be a SHA-256 digest")
        expires = _time(expires_at) if expires_at else self._now() + timedelta(hours=2)
        if not self._now() < expires <= self._now() + timedelta(days=90):
            raise ValueError("A clearance must expire within ninety days")
        if archive_in:
            source = self.store.get_run(archive_in)
            if source["state"] != "sealed":
                raise ValueError("A rehearsal archive must be a sealed episode")
        now = self._now().isoformat()
        grant = {
            "grant_id": identifier("k"),
            "run_id": run_id,
            "packs": packs,
            "tools": tools,
            "action_classes": classes,
            "platform": platform,
            "origins": origins,
            "path_prefixes": paths,
            "expires_at": expires.isoformat(),
            "purpose": purpose,
            "standing": standing,
            "max_uses": max_uses,
            "uses": 0,
            "action_hash": action_hash,
            "archive_in": archive_in or run["config"].get("archive_in"),
            "proxy_ref": proxy_ref,
            "owner": owner,
            "state": "rehearsal",
            "created_at": now,
            "rehearsal_quota": 10,
            "rehearsal_attempts": 0,
            "rehearsal": [],
            "activated_at": None,
            "revoked_at": None,
        }
        with self.store._transaction():
            self.store.db.execute(
                "INSERT INTO capability_clearances VALUES(?,?,?)",
                (grant["grant_id"], run_id, canonical_json(grant)),
            )
            self._delta(grant, "grant_proposed", actor="researcher")
        return self.get(grant["grant_id"])

    def _scope_matches(self, grant, action_class, platform, url, action_hash):
        if action_class not in grant["action_classes"]:
            return False
        if grant["platform"] and platform != grant["platform"]:
            return False
        if grant["action_hash"] and action_hash != grant["action_hash"]:
            return False
        if grant["origins"]:
            if not isinstance(url, str):
                return False
            parsed = urlsplit(url)
            try:
                base = _origin(f"{parsed.scheme}://{parsed.netloc}")
                path = _path(parsed.path or "/")
            except ValueError:
                return False
            if parsed.username or parsed.password or base not in grant["origins"]:
                return False
            if grant["path_prefixes"] and not any(
                prefix == "/" or path == prefix or path.startswith(prefix + "/")
                for prefix in grant["path_prefixes"]
            ):
                return False
        return True

    async def rehearse(self, grant_id, action):
        lock = self._locks.setdefault(grant_id, asyncio.Lock())
        async with lock:
            grant = self.get(grant_id)
            if self.store.get_run(grant["run_id"])["state"] != "running":
                return blocked("policy_deny", "The run is not running", "run_state")
            if not self.verify(grant["run_id"])["valid"]:
                return blocked(
                    "policy_deny", "Capability ledger failed verification", "grant_integrity"
                )
            if grant["state"] != "rehearsal" or _time(grant["expires_at"]) <= self._now():
                return blocked("policy_deny", "Clearance is not open for rehearsal", "grant_state")
            if grant["rehearsal_attempts"] >= grant["rehearsal_quota"]:
                return blocked(
                    "policy_deny", "The ten-call rehearsal quota is exhausted", "rehearsal_quota"
                )
            if not self._scope_matches(
                grant,
                action.get("action_class", "read"),
                action.get("platform"),
                action.get("url"),
                action.get("action_hash"),
            ):
                return blocked(
                    "policy_deny", "Rehearsal action exceeds the proposed scope", "grant_scope"
                )
            executor = self.rehearsal_executor
            if executor is None:
                proxy = getattr(self.world, "services", {}).get("proxy")
                executor = getattr(proxy, "rehearse", None) if proxy else None
            if executor is None:
                return blocked(
                    "policy_deny", "No recorded rehearsal proxy is configured", "rehearsal_proxy"
                )
            receipt = {
                "receipt_id": identifier("kr"),
                "grant_id": grant_id,
                "recorded_at": self._now().isoformat(),
                "proxy_receipt": None,
                "action_hash": content_hash(redact(action)),
            }
            with self.store._transaction():
                current = self._load(grant_id)
                if current["rehearsal_attempts"] >= current["rehearsal_quota"]:
                    return blocked(
                        "policy_deny",
                        "The ten-call rehearsal quota is exhausted",
                        "rehearsal_quota",
                    )
                current["rehearsal_attempts"] += 1
                self.store.db.execute(
                    "INSERT INTO capability_rehearsals VALUES(?,?,?,'executing')",
                    (receipt["receipt_id"], grant_id, canonical_json(receipt)),
                )
                self._delta(current, "rehearsal_started", receipt=receipt)
            try:
                result = executor(
                    grant_id, action, run_id=grant["run_id"], archive_in=grant["archive_in"]
                )
                result = await result if inspect.isawaitable(result) else result
            except asyncio.CancelledError:
                self._failed_rehearsal(grant_id, receipt["receipt_id"], "interrupted")
                raise
            except Exception:  # noqa: BLE001 - preserve an admitted proxy attempt without leaking secrets
                self._failed_rehearsal(grant_id, receipt["receipt_id"], "proxy_failed")
                return blocked(
                    "policy_deny", "The recorded rehearsal proxy failed", "rehearsal_proxy"
                )
            if (
                not isinstance(result, dict)
                or result.get("mode") != "rehearsal"
                or not result.get("record_ref")
                or not result.get("request_hash")
                or not result.get("response_hash")
            ):
                self._failed_rehearsal(grant_id, receipt["receipt_id"], "invalid_receipt")
                return blocked(
                    "policy_deny",
                    "The proxy did not produce a recorded rehearsal receipt",
                    "rehearsal_receipt",
                )
            receipt["proxy_receipt"] = result
            with self.store._transaction():
                current = self._load(grant_id)
                if current["state"] != "rehearsal" or _time(current["expires_at"]) <= self._now():
                    return blocked(
                        "policy_deny", "Clearance changed while its proxy call ran", "grant_state"
                    )
                self.store.db.execute(
                    "UPDATE capability_rehearsals SET payload=?,state='executed' WHERE receipt_id=?",
                    (canonical_json(receipt), receipt["receipt_id"]),
                )
                self._delta(current, "rehearsal_executed", receipt=receipt)
            self.record_rehearsal(grant_id, receipt["receipt_id"])
            return {"kind": "result", **receipt}

    def _failed_rehearsal(self, grant_id, receipt_id, reason):
        with self.store._transaction():
            current = self._load(grant_id)
            if not self.verify(current["run_id"])["valid"]:
                raise ValueError("The capability ledger failed verification")
            row = self.store.db.execute(
                "SELECT state FROM capability_rehearsals WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if row is None or row[0] in {"failed", "recorded"}:
                return
            current["rehearsal"].append(
                {"receipt_id": receipt_id, "passed": False, "reason": reason}
            )
            self.store.db.execute(
                "UPDATE capability_rehearsals SET state='failed' WHERE receipt_id=?", (receipt_id,)
            )
            self._delta(current, "rehearsal_failed", receipt_id=receipt_id, reason=reason)

    def record_rehearsal(self, grant_id, receipt_id, *, verdict=None):
        with self.store._transaction():
            grant = self._load(grant_id)
            if not self.verify(grant["run_id"])["valid"]:
                raise ValueError("The capability ledger failed verification")
            if grant["state"] != "rehearsal" or _time(grant["expires_at"]) <= self._now():
                raise ValueError("Clearance is not open for rehearsal")
            row = self.store.db.execute(
                "SELECT * FROM capability_rehearsals WHERE receipt_id=? AND grant_id=?",
                (receipt_id, grant_id),
            ).fetchone()
            if row is None:
                raise ValueError("Rehearsals must refer to an actual executed proxy receipt")
            if row["state"] == "recorded":
                return grant
            if row["state"] != "executed":
                raise ValueError("Rehearsals require a completed proxy execution")
            if len(grant["rehearsal"]) >= grant["rehearsal_quota"]:
                raise ValueError("The ten-call rehearsal quota is exhausted")
            receipt = json.loads(row["payload"])
            if not any(
                event["kind"] == "rehearsal_executed" and event["data"].get("receipt") == receipt
                for event in self.ledger(grant["run_id"])
            ):
                raise ValueError("The rehearsal receipt differs from its signed execution record")
            actual = receipt["proxy_receipt"].get("passed") is True
            if verdict is not None and verdict not in {"pass" if actual else "fail"}:
                raise ValueError("A reviewer cannot override the proxy's recorded outcome")
            grant["rehearsal"].append(
                {"receipt_id": receipt_id, "passed": actual, "receipt_hash": content_hash(receipt)}
            )
            self.store.db.execute(
                "UPDATE capability_rehearsals SET state='recorded' WHERE receipt_id=?",
                (receipt_id,),
            )
            self._delta(grant, "rehearsal_recorded", receipt_id=receipt_id, passed=actual)
        return self.get(grant_id)

    def rehearsal_hash(self, grant_id):
        return content_hash(self.get(grant_id)["rehearsal"])

    def activate(self, grant_id, verdict):
        from .models import PACK_TOOLS

        with self.store._transaction():
            grant = self._load(grant_id)
            if not self.verify(grant["run_id"])["valid"]:
                raise ValueError("The capability ledger failed verification")
            if grant["state"] != "rehearsal" or _time(grant["expires_at"]) <= self._now():
                raise ValueError("Clearance is not open for activation")
            if len(grant["rehearsal"]) != 10 or not all(
                item["passed"] for item in grant["rehearsal"]
            ):
                raise ValueError("Activation requires ten completed passing proxy calls")
            if (
                not isinstance(verdict, dict)
                or verdict.get("decision") != "pass"
                or not verdict.get("reviewer")
                or verdict.get("rehearsal_hash") != content_hash(grant["rehearsal"])
            ):
                raise ValueError(
                    "The activation verdict must be bound to these ten rehearsal outcomes"
                )
            if self.store.get_run(grant["run_id"])["state"] != "running":
                raise ValueError("The run stopped before clearance activation")
            grant.update(
                state="live", activated_at=self._now().isoformat(), verdict=redact(verdict)
            )
            self._delta(grant, "grant_activated", actor="researcher", verdict=redact(verdict))
        tools = sorted(
            set(grant["tools"]) | {tool for pack in grant["packs"] for tool in PACK_TOOLS[pack]}
        )
        self.world.push(
            grant["run_id"],
            {
                "type": "grant",
                "grant_id": grant_id,
                "state": "live",
                "packs": grant["packs"],
                "tools": tools,
                "expires_at": grant["expires_at"],
            },
        )
        return self.get(grant_id)

    def current(self, run_id):
        self.store.get_run(run_id)
        if not self.verify(run_id)["valid"]:
            return []
        with self.store._lock:
            values = [
                json.loads(row[0])
                for row in self.store.db.execute(
                    "SELECT payload FROM capability_clearances WHERE run_id=?", (run_id,)
                )
            ]
        return [
            grant
            for grant in values
            if grant["state"] == "live"
            and _time(grant["expires_at"]) > self._now()
            and (grant["max_uses"] is None or grant["uses"] < grant["max_uses"])
        ]

    def packs(self, run_id):
        return sorted({pack for grant in self.current(run_id) for pack in grant["packs"]})

    def tools(self, run_id):
        from .models import PACK_TOOLS

        return sorted(
            {
                tool
                for grant in self.current(run_id)
                for tool in grant["tools"]
                + [name for pack in grant["packs"] for name in PACK_TOOLS[pack]]
            }
        )

    def require(
        self,
        run_id,
        action_class,
        *,
        platform=None,
        url=None,
        action_hash=None,
        consume=False,
        action_id=None,
        tool=None,
    ):
        if self.store.get_run(run_id)["state"] != "running":
            return blocked("policy_deny", "The run is not running", "run_state")
        with self.store._transaction():
            choices = self.current(run_id)
            for grant in choices:
                if tool is not None:
                    from .models import PACK_TOOLS

                    names = set(grant["tools"]) | {
                        name for pack in grant["packs"] for name in PACK_TOOLS[pack]
                    }
                    if tool not in names:
                        continue
                if not self._scope_matches(grant, action_class, platform, url, action_hash):
                    continue
                if consume:
                    if not action_id:
                        raise ValueError("Consuming a clearance requires a world action identity")
                    if self.store.db.execute(
                        "SELECT 1 FROM capability_uses WHERE grant_id=? AND action_id=?",
                        (grant["grant_id"], action_id),
                    ).fetchone():
                        return blocked(
                            "policy_deny",
                            "This action already consumed its clearance",
                            "grant_replay",
                        )
                    grant["uses"] += 1
                    self.store.db.execute(
                        "INSERT INTO capability_uses VALUES(?,?,?,?)",
                        (grant["grant_id"], action_id, action_hash, self._now().isoformat()),
                    )
                    self._delta(grant, "grant_used", action_id=action_id, action_hash=action_hash)
                return {
                    "kind": "result",
                    "authorized": True,
                    "grant_id": grant["grant_id"],
                    "standing": grant["standing"],
                    "consumed": consume,
                    "requires_exact_approval": action_class in {"purchase", "message"},
                }
        return blocked("policy_deny", "No live clearance covers this action", "grant_scope")

    def admit(self, run_id, tool, params, action_id=None):
        """Admit extra tools, deferring network consumption to the actual sink."""
        from .models import PACK_TOOLS, manifest

        config = self.store.get_run(run_id)["config"]
        initial = {entry["name"] for entry in manifest(config.get("packs", []))["tools"]}
        if tool in initial:
            return {
                "kind": "result",
                "authorized": True,
                "initial_capability": True,
                "consumed": False,
            }
        network_tools = {
            name
            for pack in ("browse", "mail", "social", "perception", "market")
            for name in PACK_TOOLS[pack]
        } | {"watch"}
        for grant in self.current(run_id):
            names = set(grant["tools"]) | {
                name for pack in grant["packs"] for name in PACK_TOOLS[pack]
            }
            if tool not in names:
                continue
            if tool in network_tools:
                return {
                    "kind": "result",
                    "authorized": True,
                    "grant_id": grant["grant_id"],
                    "consumed": False,
                    "requires_sink_authorization": True,
                }
            action_class = "read" if tool in {"beliefs", "footprint"} else "write_sandbox"
            return self.require(
                run_id,
                action_class,
                platform=params.get("platform"),
                url=params.get("url"),
                action_hash=params.get("action_hash"),
                consume=action_id is not None,
                action_id=action_id,
                tool=tool,
            )
        return blocked("policy_deny", "No live clearance enables this tool", "grant_tool")

    def authorize_used(
        self,
        run_id,
        action_id,
        action_class,
        *,
        platform=None,
        url=None,
        action_hash=None,
        grant_id=None,
    ):
        """Check subrequests of an already consumed action without charging another use."""
        if self.store.get_run(run_id)["state"] != "running" or not self.verify(run_id)["valid"]:
            return blocked(
                "policy_deny", "This run has no verified active clearance", "grant_integrity"
            )
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT grant_id,action_hash FROM capability_uses WHERE action_id=?", (action_id,)
            ).fetchall()
            for row in rows:
                if grant_id and row["grant_id"] != grant_id:
                    continue
                grant = self._load(row["grant_id"])
                if (
                    grant["run_id"] != run_id
                    or grant["state"] != "live"
                    or _time(grant["expires_at"]) <= self._now()
                ):
                    continue
                if action_hash is not None and row["action_hash"] != action_hash:
                    continue
                if self._scope_matches(grant, action_class, platform, url, action_hash):
                    return {
                        "kind": "result",
                        "authorized": True,
                        "grant_id": grant["grant_id"],
                        "consumed": True,
                        "subrequest": True,
                    }
        return blocked("policy_deny", "No consumed clearance covers this subrequest", "grant_scope")

    def revoke(self, grant_id, *, reason, actor="researcher"):
        if not reason:
            raise ValueError("Revocation requires a reason")
        with self.store._transaction():
            grant = self._load(grant_id)
            if grant["state"] in {"revoked", "expired"}:
                return grant
            grant.update(
                state="revoked", revoked_at=self._now().isoformat(), revocation_reason=reason
            )
            self._delta(grant, "grant_revoked", actor=actor, reason=reason)
        self.world.push(
            grant["run_id"], {"type": "grant", "grant_id": grant_id, "state": "revoked"}
        )
        return self.get(grant_id)

    def revoke_incident(self, run_id, *, platform=None, incident_ref, account_alias=None):
        if not incident_ref:
            raise ValueError("Incident revocation requires an evidence reference")
        source = self.store.get_run(run_id)
        life_id = source["config"].get("life_id")
        audience = {run_id}
        if life_id:
            audience.update(
                run["run_id"]
                for run in self.store.list_runs()
                if run["config"].get("life_id") == life_id
            )
        with self.store._lock:
            ids = [
                row[0]
                for row in self.store.db.execute(
                    "SELECT grant_id,run_id FROM capability_clearances"
                )
                if row[1] in audience
            ]
        revoked = []
        for grant_id in ids:
            grant = self.get(grant_id)
            if grant["state"] in {"live", "rehearsal"} and (
                platform is None or grant["platform"] in {None, platform}
            ):
                self.revoke(grant_id, reason="incident:" + incident_ref, actor="world")
                revoked.append(grant_id)
        return {"revoked": revoked, "incident_ref": incident_ref, "life_id": life_id}

    def expire(self):
        with self.store._transaction():
            ids = [
                row[0]
                for row in self.store.db.execute("SELECT grant_id FROM capability_clearances")
            ]
            expired = []
            for grant_id in ids:
                grant = self._load(grant_id)
                if not self.verify(grant["run_id"])["valid"]:
                    continue
                if (
                    grant["state"] in {"rehearsal", "live"}
                    and _time(grant["expires_at"]) <= self._now()
                ):
                    grant.update(state="expired", revoked_at=self._now().isoformat())
                    self._delta(grant, "grant_expired")
                    expired.append(grant_id)
        return {"expired": expired}

    async def recover(self):
        self.expire()
        for run in self.store.list_runs():
            if not self.verify(run["run_id"])["valid"]:
                self.store.update_run(run["run_id"], tainted=True)
        with self.store._lock:
            interrupted = [
                (row[0], row[1])
                for row in self.store.db.execute(
                    "SELECT grant_id,receipt_id FROM capability_rehearsals WHERE state='executing'"
                )
            ]
        for grant_id, receipt_id in interrupted:
            if self.verify(self.get(grant_id)["run_id"])["valid"]:
                self._failed_rehearsal(grant_id, receipt_id, "interrupted_by_restart")

    async def aclose(self):
        return None
