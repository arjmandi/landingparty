"""Separate researcher authority from per-run agent capabilities."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

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


SESSION_COOKIE = "world_operator_session"
SESSION_SECONDS = 8 * 60 * 60


def _same_secret(supplied, required):
    try:
        return hmac.compare_digest(supplied.encode("utf-8"), required.encode("utf-8"))
    except UnicodeError:
        return False


def _trusted_local_host(request):
    try:
        host = urlsplit("//" + request.headers.get("host", ""))
        return (
            host.hostname in {"127.0.0.1", "localhost", "::1"}
            and (host.port is None or 1 <= host.port <= 65535)
            and not (host.username or host.password or host.path or host.query or host.fragment)
        )
    except ValueError:
        return False


def _same_origin(request):
    """Compare browser-supplied origins without trusting forwarding headers."""
    supplied = request.headers.get("origin") or request.headers.get("referer")
    if not supplied:
        return False
    try:
        left, right = urlsplit(supplied), urlsplit(str(request.url))
        return (
            left.scheme in {"http", "https"}
            and not left.username
            and not left.password
            and (left.scheme, left.hostname, left.port or (443 if left.scheme == "https" else 80))
            == (
                right.scheme,
                right.hostname,
                right.port or (443 if right.scheme == "https" else 80),
            )
        )
    except ValueError:
        return False


class BrowserSessions:
    """Process-local opaque sessions; neither cookies nor logs contain the control key."""

    def __init__(self):
        self.sessions = {}

    def issue(self, required):
        now = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items() if value[0] > now}
        if len(self.sessions) >= 1000:
            self.sessions.pop(next(iter(self.sessions)))
        token = secrets.token_urlsafe(32)
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = (
            now + SESSION_SECONDS,
            hashlib.sha256(required.encode()).hexdigest(),
        )
        return token

    def verify(self, token, required):
        value = self.sessions.get(hashlib.sha256(token.encode()).hexdigest()) if token else None
        return bool(
            value
            and value[0] > time.monotonic()
            and hmac.compare_digest(value[1], hashlib.sha256(required.encode()).hexdigest())
        )

    def revoke(self, token):
        self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)


def enable_browser_sessions(app):
    """Install single-operator browser login only on the unified control application."""
    if getattr(app.state, "browser_sessions", None) is not None:
        return
    sessions = app.state.browser_sessions = BrowserSessions()

    def state(request):
        required = os.environ.get("WORLD_CONTROL_TOKEN", "")
        return {
            "authenticated": not required
            or sessions.verify(request.cookies.get(SESSION_COOKIE, ""), required),
            "requires_token": bool(required),
        }

    @app.get("/api/session")
    async def session_status(request: Request):
        return state(request)

    @app.post("/api/session")
    async def login(request: Request):
        if not _same_origin(request):
            return JSONResponse({"detail": "Same-origin browser request required"}, status_code=403)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 16384:
                return JSONResponse(
                    {"detail": "Session request exceeds the size limit"}, status_code=413
                )
            raw.extend(chunk)
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"detail": "Token JSON object required"}, status_code=400)
        token = body.get("token") if isinstance(body, dict) else None
        required = os.environ.get("WORLD_CONTROL_TOKEN", "")
        if not isinstance(token, str) or (required and not _same_secret(token, required)):
            return JSONResponse({"detail": "Invalid operator token"}, status_code=401)
        sessions.revoke(request.cookies.get(SESSION_COOKIE, ""))
        response = JSONResponse({"authenticated": True, "requires_token": bool(required)})
        if required:
            response.set_cookie(
                SESSION_COOKIE,
                sessions.issue(required),
                max_age=SESSION_SECONDS,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                path="/",
            )
        return response

    @app.delete("/api/session")
    async def logout(request: Request):
        if not _same_origin(request):
            return JSONResponse({"detail": "Same-origin browser request required"}, status_code=403)
        sessions.revoke(request.cookies.get(SESSION_COOKIE, ""))
        response = JSONResponse(
            {
                "authenticated": not os.environ.get("WORLD_CONTROL_TOKEN"),
                "requires_token": bool(os.environ.get("WORLD_CONTROL_TOKEN")),
            }
        )
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        return response


def protect_researcher(app):
    """Optional token on the trusted control listener; the agent listener is always scoped."""

    @app.middleware("http")
    async def authority(request: Request, call_next):
        required = os.environ.get("WORLD_CONTROL_TOKEN")
        sessions = getattr(app.state, "browser_sessions", None)
        if sessions is not None and not required and not _trusted_local_host(request):
            return JSONResponse({"detail": "Untrusted local Host"}, status_code=400)
        legacy_exempt = request.url.path in {
            "/healthz",
            "/bridge",
            "/.well-known/http-message-signatures-directory",
        }
        bootstrap = sessions is not None and (
            request.url.path == "/api/session"
            or request.method in {"GET", "HEAD"}
            and (request.url.path == "/" or request.url.path.startswith("/assets/"))
        )
        cookie_auth = False
        if required and not (bootstrap or sessions is None and legacy_exempt):
            supplied = request.headers.get("x-world-control-token") or bearer(request)
            cookie_auth = bool(
                not supplied
                and sessions is not None
                and sessions.verify(request.cookies.get(SESSION_COOKIE, ""), required)
            )
            authorized = _same_secret(supplied, required) or cookie_auth
            if not authorized:
                response = JSONResponse(
                    {"detail": "Researcher authorization required"}, status_code=401
                )
            elif (
                request.method not in {"GET", "HEAD", "OPTIONS"}
                and sessions is not None
                and (
                    cookie_auth
                    or request.headers.get("origin")
                    or request.headers.get("referer")
                    or request.headers.get("sec-fetch-site") not in {None, "none"}
                )
                and not _same_origin(request)
            ):
                response = JSONResponse(
                    {"detail": "Same-origin browser request required"}, status_code=403
                )
            else:
                response = await call_next(request)
        elif (
            sessions is not None
            and not bootstrap
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and (
                request.headers.get("origin")
                or request.headers.get("referer")
                or request.headers.get("sec-fetch-site") not in {None, "none"}
            )
            and not _same_origin(request)
        ):
            response = JSONResponse(
                {"detail": "Same-origin browser request required"}, status_code=403
            )
        else:
            response = await call_next(request)
        if request.url.path != "/.well-known/http-message-signatures-directory":
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Keep the studio's script/connect/base restrictions; middleware must not
        # replace a stronger route-specific policy with the framing fallback.
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
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
