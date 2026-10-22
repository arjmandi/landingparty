"""World-owned disclosed identities and signed, non-removable output attribution."""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import threading
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .util import canonical, identifier, now


class VenueDB:
    """A private side store; the world record remains the action audit authority."""

    def __init__(self, world, name):
        self.world, self.store = world, world.store
        path = self.store.data_dir / f"{name}.sqlite3"
        if path.is_symlink():
            raise ValueError("Private service stores cannot be symlinks")
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.lock = threading.RLock()
        path.chmod(0o600)

    async def recover(self):
        return None

    async def aclose(self):
        self.db.close()


def https_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("A credential-free HTTPS URL is required")
    return value


class IdentityService(VenueDB):
    def __init__(self, world):
        super().__init__(world, "identities")
        self.db.execute("""CREATE TABLE IF NOT EXISTS identities (
            identity_id TEXT PRIMARY KEY, life_id TEXT UNIQUE, run_id TEXT,
            payload TEXT NOT NULL, ended_at TEXT)""")
        path = self.store.data_dir / "identity-signing.pem"
        if path.is_symlink():
            raise ValueError("Private signing keys cannot be symlinks")
        if not path.exists():
            key = Ed25519PrivateKey.generate()
            with os.fdopen(
                os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as stream:
                stream.write(
                    key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )
        if path.stat().st_mode & 0o077:
            raise ValueError("Identity signing key must have mode 0600")
        self.key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(self.key, Ed25519PrivateKey):
            raise TypeError("Identity signing key must be Ed25519")

    def create(
        self,
        *,
        principal: str,
        name: str,
        mailbox: str,
        homepage: str,
        life_id: str | None = None,
        run_id: str | None = None,
        incident_url: str | None = None,
    ) -> dict:
        if not principal.strip() or not name.strip():
            raise ValueError("A principal of record and disclosed name are required")
        if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", mailbox):
            raise ValueError("A researcher-owned mailbox address is required")
        if any(char in principal + name + mailbox for char in "\r\n"):
            raise ValueError("Identity fields cannot contain header line breaks")
        if not life_id and not run_id:
            raise ValueError("An identity must be bound to a life or a run")
        if run_id:
            self.store.get_run(run_id)
        item = {
            "identity_id": identifier("id"),
            "principal": principal,
            "name": name,
            "mailbox": mailbox,
            "homepage": https_url(homepage),
            "life_id": life_id,
            "run_id": run_id,
            "incident_url": https_url(incident_url or homepage.rstrip("/") + "/incident"),
            "created_at": now(),
        }
        self.db.execute(
            "INSERT INTO identities VALUES (?,?,?,?,NULL)",
            (
                item["identity_id"],
                life_id,
                run_id,
                canonical(item),
            ),
        )
        return item

    def get(
        self,
        identity_id: str | None = None,
        *,
        run_id: str | None = None,
        life_id: str | None = None,
    ) -> dict:
        if identity_id:
            row = self.db.execute(
                "SELECT * FROM identities WHERE identity_id=?", (identity_id,)
            ).fetchone()
        else:
            if run_id and not life_id:
                life_id = self.store.get_run(run_id)["config"].get("life_id")
            row = self.db.execute(
                "SELECT * FROM identities WHERE (life_id=? AND ? IS NOT NULL) OR run_id=?",
                (life_id, life_id, run_id),
            ).fetchone()
        if row is None:
            raise KeyError("No disclosed identity is registered")
        item = json.loads(row["payload"])
        item["ended_at"] = row["ended_at"]
        return item

    def mark_ended(self, life_id: str):
        self.db.execute(
            "UPDATE identities SET ended_at=COALESCE(ended_at,?) WHERE life_id=?", (now(), life_id)
        )

    def disclosure(self, run_id: str) -> dict:
        identity = self.get(run_id=run_id)
        metadata = {
            "identity_id": identity["identity_id"],
            "run_id": run_id,
            "principal": identity["principal"],
            "agent": True,
        }
        signature = base64.urlsafe_b64encode(self.key.sign(canonical(metadata).encode())).decode()
        line = f"AI agent {identity['name']}; sponsor: {identity['principal']}; run: {run_id}."
        marker = "<!-- landing-party " + canonical({**metadata, "signature": signature}) + " -->"
        return {"line": line, "marker": marker, "signature": signature, "metadata": metadata}

    def disclose(self, run_id: str, body: str) -> tuple[str, dict]:
        if not isinstance(body, str):
            raise TypeError("Message body must be text")
        if re.search(
            r"\b(?:I am|I'm)\s+(?:a\s+)?(?:real\s+)?human\b|\bnot\s+an?\s+(?:AI|bot)\b|\bhuman-written\b",
            body,
            re.IGNORECASE,
        ):
            raise ValueError("Human persona claims are not permitted")
        if "<!-- landing-party" in body or "[landing-party:" in body:
            raise ValueError("Disclosure markers are authored by the world only")
        disclosure = self.disclosure(run_id)
        return body.rstrip() + "\n\n" + disclosure["line"] + "\n" + disclosure["marker"], disclosure

    def homepage_document(self, *, life_id=None, run_id=None) -> dict:
        identity = self.get(life_id=life_id, run_id=run_id)
        return {
            **identity,
            "agent_disclosure": True,
            "status": "life ended; identity remains reachable"
            if identity["ended_at"]
            else "active AI agent",
            "incident_reporting": identity["incident_url"],
        }

    async def handle(self, run_id, action_id, tool, params):
        # Identity creation is researcher control, never an agent-controlled persona switch.
        if tool != "identity":
            raise ValueError("Unknown identity tool")
        return {
            "kind": "result",
            "identity": self.get(run_id=run_id),
            "disclosure": self.disclosure(run_id),
        }
