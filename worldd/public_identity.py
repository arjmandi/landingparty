"""Public disclosed identity and bounded third-party incident intake.

The public app has no researcher controls. A report cannot revoke authority:
only authenticated human triage can turn it into an owned incident.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .approvals import private_key
from .identity import VenueDB
from .util import canonical, digest, identifier, now


class IncidentReportService(VenueDB):
    def __init__(self, world):
        super().__init__(world, "public-incidents")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS public_incident_reports (
                report_id TEXT PRIMARY KEY, identity_id TEXT NOT NULL,
                created_at TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS incident_intake_rates (
                identity_id TEXT NOT NULL, reporter_hash TEXT NOT NULL, received_at TEXT NOT NULL);
        """)
        root = self.store.data_dir / "keys"
        root.mkdir(mode=0o700, exist_ok=True)
        self.rate_key = private_key(root / "incident-intake.key")
        self._locks = {}

    def identity(self, identity_id):
        return self.world.services["identity"].get(identity_id)

    def published_identity(self, identity_id):
        identity = self.identity(identity_id)
        public = {
            key: identity[key]
            for key in (
                "identity_id",
                "name",
                "principal",
                "homepage",
                "mailbox",
                "incident_url",
                "ended_at",
            )
        }
        metadata = {
            "identity_id": identity_id,
            "agent": True,
            "principal": identity["principal"],
            "run_id": identity.get("run_id"),
            "life_id": identity.get("life_id"),
            "ended_at": identity["ended_at"],
        }
        if not metadata["run_id"] and identity.get("life_id"):
            metadata["run_id"] = next(
                (
                    run["run_id"]
                    for run in self.store.list_runs()
                    if run["config"].get("life_id") == identity["life_id"]
                    and not run["config"].get("private_archive")
                ),
                None,
            )
        key = self.world.services["identity"].key
        public["disclosure"] = {
            "metadata": metadata,
            "signature": base64.urlsafe_b64encode(key.sign(canonical(metadata).encode())).decode(),
        }
        from cryptography.hazmat.primitives import serialization

        public["verification_key"] = base64.urlsafe_b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode()
        return public

    def resolve_path(self, host, path):
        """Exact registered publisher host/path; the public cannot select a platform."""
        identities = self.world.services["identity"]
        matches = []
        for row in identities.db.execute("SELECT identity_id FROM identities"):
            identity = identities.get(row["identity_id"])
            for key, page in (("homepage", "home"), ("incident_url", "incident")):
                parsed = urlsplit(identity[key])
                if (parsed.hostname or "").lower() == host.lower() and parsed.path.rstrip(
                    "/"
                ) == path.rstrip("/"):
                    matches.append((identity["identity_id"], page))
        return matches[0] if len(matches) == 1 else None

    def receive(self, identity_id, *, report, contact="", remote_address):
        identity = self.identity(identity_id)
        if (
            not isinstance(report, str)
            or not report.strip()
            or len(report) > 8000
            or not isinstance(contact, str)
            or len(contact) > 500
        ):
            raise ValueError(
                "Report text is required (maximum 8000 characters); contact is optional (maximum 500)"
            )
        stamp = now()
        window = (datetime.fromisoformat(stamp) - timedelta(hours=1)).isoformat()
        reporter_hash = hmac.new(
            self.rate_key, str(remote_address).encode(), hashlib.sha256
        ).hexdigest()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                count = self.db.execute(
                    "SELECT COUNT(*) FROM incident_intake_rates WHERE identity_id=? AND received_at>=?",
                    (identity_id, window),
                ).fetchone()[0]
                sender_count = self.db.execute(
                    "SELECT COUNT(*) FROM incident_intake_rates WHERE reporter_hash=? AND received_at>=?",
                    (reporter_hash, window),
                ).fetchone()[0]
                if count >= 20 or sender_count >= 5:
                    self.db.execute("ROLLBACK")
                    return {"rate_limited": True}
                report_id = identifier("ir")
                payload = {
                    "report_id": report_id,
                    "identity_id": identity_id,
                    "owner": identity["principal"],
                    "created_at": stamp,
                    "retention_until": (
                        datetime.fromisoformat(stamp) + timedelta(days=90)
                    ).isoformat(),
                    "report": report,
                    "contact": contact,
                    "body_hash": digest({"report": report, "contact": contact}),
                    "verified": False,
                }
                self.db.execute(
                    "INSERT INTO public_incident_reports VALUES (?,?,?,'pending',?)",
                    (report_id, identity_id, stamp, canonical(payload)),
                )
                self.db.execute(
                    "INSERT INTO incident_intake_rates VALUES (?,?,?)",
                    (identity_id, reporter_hash, stamp),
                )
                self.db.execute("DELETE FROM incident_intake_rates WHERE received_at<?", (window,))
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        return {"receipt_id": report_id, "received": True}

    def reports(self, state="pending", run_id=None):
        if state not in {"pending", "processing", "accepted", "dismissed", "all"}:
            raise ValueError("Unknown report state")
        identity_id = None
        if run_id:
            try:
                identity_id = self.world.services["identity"].get(run_id=run_id)["identity_id"]
            except KeyError:
                return []
        return [
            {**json.loads(row["payload"]), "state": row["state"]}
            for row in self.db.execute(
                "SELECT * FROM public_incident_reports WHERE state=? OR ?='all' ORDER BY created_at",
                (state, state),
            )
            if identity_id is None or row["identity_id"] == identity_id
        ]

    async def triage(
        self,
        report_id,
        *,
        decision,
        run_id=None,
        platform=None,
        incident_kind="complaint",
        owner=None,
        account_alias=None,
    ):
        if decision not in {"accept", "dismiss"}:
            raise ValueError("A human must accept or dismiss the unverified report")
        async with self._locks.setdefault(report_id, asyncio.Lock()):
            row = self.db.execute(
                "SELECT * FROM public_incident_reports WHERE report_id=?", (report_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Unknown public report")
            report = json.loads(row["payload"])
            if row["state"] in {"accepted", "dismissed"}:
                return {
                    "report_id": report_id,
                    "state": row["state"],
                    "incident_ref": report.get("incident_ref"),
                }
            if row["state"] != "pending":
                raise ValueError(
                    "Interrupted triage requires operator reconciliation; it is not retried automatically"
                )
            if decision == "accept":
                actual = self.world.services["identity"].get(run_id=run_id) if run_id else None
                if not actual or actual["identity_id"] != row["identity_id"] or not platform:
                    raise ValueError(
                        "Accepted reports require an actual source run of this identity and a human-selected platform"
                    )
                if incident_kind not in {
                    "complaint",
                    "takedown",
                    "suspension",
                    "spam",
                    "retraction",
                }:
                    raise ValueError("Unknown incident kind")
            report.update(owner=owner or report["owner"], reviewed_at=now())
            if not isinstance(report["owner"], str) or not report["owner"].strip():
                raise ValueError("A responsible human owner is required")
            if decision == "accept":
                self.db.execute(
                    "UPDATE public_incident_reports SET state='processing',payload=? WHERE report_id=?",
                    (canonical(report), report_id),
                )
                result = await self.world.services["footprint"].incident(
                    run_id,
                    platform=platform,
                    incident_kind=incident_kind,
                    owner=report["owner"],
                    account_alias=account_alias,
                    detail=f"Public report {report_id}; body hash {report['body_hash']}",
                )
                report["incident_ref"] = result["item"]["item_id"]
                report["revocation_confirmed"] = result["revocation_confirmed"]
            state = "accepted" if decision == "accept" else "dismissed"
            self.db.execute(
                "UPDATE public_incident_reports SET state=?,payload=? WHERE report_id=?",
                (state, canonical(report), report_id),
            )
            return {
                "report_id": report_id,
                "state": state,
                "incident_ref": report.get("incident_ref"),
                "revocation_confirmed": report.get("revocation_confirmed"),
            }

    def prune_expired(self, now):
        if now.tzinfo is None:
            raise ValueError("Retention requires an aware world time")
        pruned = 0
        for row in self.db.execute("SELECT report_id,payload FROM public_incident_reports"):
            payload = json.loads(row["payload"])
            if "report" in payload and datetime.fromisoformat(payload["retention_until"]) <= now:
                payload.pop("report", None)
                payload.pop("contact", None)
                payload["body_expired"] = True
                self.db.execute(
                    "UPDATE public_incident_reports SET payload=? WHERE report_id=?",
                    (canonical(payload), row["report_id"]),
                )
                pruned += 1
        if pruned:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"pruned": pruned}


def create_public_app(world):
    app = FastAPI(
        title="Disclosed agent identity and incident reporting",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    service = world.services["incident_reports"]

    @app.middleware("http")
    async def private_response_headers(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            }
        )
        return response

    def page(identity_id, incident=False):
        identity = service.published_identity(identity_id)
        name, principal = html.escape(identity["name"]), html.escape(identity["principal"])
        status = (
            "This life has ended; the incident path remains open."
            if identity["ended_at"]
            else "This is an active AI agent."
        )
        marker = html.escape(
            canonical({**identity["disclosure"], "verification_key": identity["verification_key"]}),
            quote=True,
        )
        content = f'<h1>{name}</h1><p data-landing-party="{marker}">AI agent; sponsor and principal of record: {principal}.</p><p>{status}</p>'
        if incident:
            content += '<h2>Report an incident</h2><p>Your report goes to the responsible researcher for review.</p><form method="post"><label>What happened?<textarea name="report" maxlength="8000" required></textarea></label><label>Contact (optional)<input name="contact" maxlength="500"></label><button>Submit report</button></form>'
        else:
            content += (
                '<p><a href="'
                + html.escape(identity["incident_url"], quote=True)
                + '">Report an incident to the responsible researcher</a></p>'
            )
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Disclosed AI agent</title><body>'
            + content
            + "</body></html>"
        )

    async def intake(identity_id, request):
        try:
            service.identity(identity_id)
        except KeyError:
            return JSONResponse({"detail": "Unknown published identity"}, status_code=404)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                return JSONResponse(
                    {"detail": "Report exceeds the intake size limit"}, status_code=413
                )
        try:
            content_type = request.headers.get("content-type", "").split(";", 1)[0]
            if content_type == "application/json":
                payload = json.loads(body)
            elif content_type == "application/x-www-form-urlencoded":
                values = parse_qs(body.decode(), max_num_fields=2, keep_blank_values=True)
                if any(len(value) != 1 for value in values.values()):
                    raise ValueError("Duplicate fields are not accepted")
                payload = {key: value[0] for key, value in values.items()}
            else:
                return JSONResponse(
                    {"detail": "Submit JSON or an ordinary report form"}, status_code=415
                )
            if not isinstance(payload, dict) or set(payload) - {"report", "contact"}:
                raise ValueError("Only report and optional contact are accepted")
            result = service.receive(
                identity_id,
                report=payload.get("report"),
                contact=payload.get("contact", ""),
                remote_address=request.client.host if request.client else "unknown",
            )
        except (ValueError, TypeError, UnicodeError):
            return JSONResponse(
                {"detail": "A bounded report and optional contact are required"}, status_code=400
            )
        if result.get("rate_limited"):
            return JSONResponse(
                {"detail": "Please wait before submitting another report"},
                status_code=429,
                headers={"Retry-After": "3600"},
            )
        return JSONResponse(result, status_code=202)

    @app.get("/identities/{identity_id}")
    async def homepage(identity_id):
        try:
            return page(identity_id)
        except KeyError:
            return JSONResponse({"detail": "Unknown published identity"}, status_code=404)

    @app.api_route("/identities/{identity_id}/incident", methods=["GET", "POST"])
    async def report(identity_id, request: Request):
        if request.method == "POST":
            return await intake(identity_id, request)
        try:
            return page(identity_id, True)
        except KeyError:
            return JSONResponse({"detail": "Unknown published identity"}, status_code=404)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def published_alias(path, request: Request):
        target = service.resolve_path(request.url.hostname or "", request.url.path)
        if target is None:
            return JSONResponse({"detail": "No published identity at this path"}, status_code=404)
        identity_id, section = target
        if request.method == "POST":
            return (
                await intake(identity_id, request)
                if section == "incident"
                else JSONResponse({"detail": "Method not allowed"}, status_code=405)
            )
        return page(identity_id, section == "incident")

    return app
