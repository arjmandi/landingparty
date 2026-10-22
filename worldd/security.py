"""Separate researcher authority from per-run agent capabilities."""

import hashlib
import hmac
import os
import secrets
import sqlite3
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse


class Capabilities:
    def __init__(self, data_dir: Path):
        self.db = sqlite3.connect(
            data_dir / "capabilities.sqlite3", isolation_level=None, check_same_thread=False
        )
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS capabilities "
            "(run_id TEXT PRIMARY KEY, digest TEXT NOT NULL, revoked INTEGER DEFAULT 0)"
        )
        (data_dir / "capabilities.sqlite3").chmod(0o600)

    def issue(self, run_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT OR REPLACE INTO capabilities VALUES(?,?,0)",
            (run_id, hashlib.sha256(token.encode()).hexdigest()),
        )
        return token

    def verify(self, run_id: str, token: str) -> bool:
        row = self.db.execute(
            "SELECT digest,revoked FROM capabilities WHERE run_id=?", (run_id,)
        ).fetchone()
        return bool(
            row
            and not row[1]
            and hmac.compare_digest(row[0], hashlib.sha256(token.encode()).hexdigest())
        )

    def revoke(self, run_id: str):
        self.db.execute("UPDATE capabilities SET revoked=1 WHERE run_id=?", (run_id,))

    def close(self):
        self.db.close()


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else ""


def protect_researcher(app):
    """Optional token on the trusted control listener; the agent listener is always scoped."""

    @app.middleware("http")
    async def authority(request: Request, call_next):
        required = os.environ.get("WORLD_CONTROL_TOKEN")
        if required and request.url.path not in {
            "/healthz",
            "/bridge",
            "/.well-known/http-message-signatures-directory",
        }:
            supplied = request.headers.get("x-world-control-token") or bearer(request)
            if not hmac.compare_digest(supplied, required):
                response = JSONResponse(
                    {"detail": "Researcher authorization required"}, status_code=401
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        if request.url.path != "/.well-known/http-message-signatures-directory":
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response


def create_agent_app(world):
    from fastapi import FastAPI, Query

    from .models import ActionRequest

    app = FastAPI(title="Landing Party agent boundary", docs_url=None, redoc_url=None)

    def denied(request, run_id):
        if not world.capabilities.verify(run_id, bearer(request)):
            return JSONResponse({"detail": "Invalid run capability"}, status_code=401)
        return None

    @app.post("/v1/act")
    async def act(body: ActionRequest, request: Request):
        error = denied(request, body.run_id)
        return error if error else await world.act(body.run_id, body.tool, body.params)

    @app.get("/v1/status")
    async def status(run_id: str, request: Request):
        error = denied(request, run_id)
        if error:
            return error
        state = world.status(run_id)
        # Researchers inspect the full tuple on the trusted listener.
        state.pop("tuple", None)
        return state

    @app.get("/v1/manifest")
    async def manifest(run_id: str, request: Request):
        error = denied(request, run_id)
        return error if error else world.manifest(run_id)

    @app.get("/v1/inbox")
    async def inbox(
        run_id: str,
        request: Request,
        since: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=500),
        wait_seconds: float = Query(0, ge=0, le=60),
    ):
        error = denied(request, run_id)
        return error if error else await world.inbox(run_id, since, limit, wait_seconds)

    return app
