"""Single-operator product API over the recorded world and native harness library."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, ValidationError

from .models import RunConfig, StrictModel
from .run_views import RunViews


class StartRun(StrictModel):
    harness_id: str = Field(min_length=1, max_length=80)
    auth_mode: str = "api_key"
    model: str | None = Field(default=None, max_length=200)
    goal: str | None = Field(default=None, max_length=20000)
    name: str | None = Field(default=None, max_length=120)
    budget_usd: float = Field(default=20, gt=0, le=10000)
    wall_minutes: float = Field(default=120, gt=0, le=10080)
    reply_policy: str = "blockers_only"
    packs: list[str] = Field(default_factory=lambda: ["deliver", "belief", "footprint"])


class StudioService:
    def __init__(self, world, settings, harnesses):
        self.world, self.settings, self.harnesses = world, settings, harnesses
        self.views = RunViews(world)
        self.origin = "http://127.0.0.1:7777"
        self._launch = asyncio.Lock()

    def configure_urls(self, origin, *, agent_url=None, lane_url=None):
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("Studio requires a credential-free server origin")
        self.origin = origin.rstrip("/")
        self.harnesses.configure_urls(
            agent_url=agent_url or self.origin + "/agent",
            lane_url=lane_url or self.origin + "/model",
        )

    async def catalog(self):
        actual = await self.harnesses.library()
        prices = self.settings.model_prices(self.world.model_pricing_base)
        table = prices.get("models", prices)
        items = []
        for source in actual["items"]:
            item = dict(source)
            provider = "anthropic" if item["id"] == "claude-code" else "openai"
            connection = item.get("subscription_auth") or {}
            subscribed = (
                connection.get("state") == "connected"
                and connection.get("auth_method") == "subscription"
            )
            installed = item.get("installed", False)
            models = [
                {"id": name, "label": name, "auth_modes": ["api_key"]}
                for name, entry in table.items()
                if entry.get("provider") == provider
            ]
            subscription_models = [
                {"id": "", "label": "Account default", "auth_modes": ["subscription"]}
            ] + [
                {"id": name, "label": name, "auth_modes": ["subscription"]}
                for name in (["sonnet", "opus", "haiku"] if provider == "anthropic" else [])
            ]
            item.update(
                provider=provider,
                description=(
                    "Anthropic's native coding agent with your world tools."
                    if provider == "anthropic"
                    else "OpenAI's native coding agent with your world tools."
                ),
                models=models + subscription_models,
                connection=self.connection_view(connection),
                modes=[
                    {
                        "id": "api_key",
                        "label": "API key",
                        "ready": bool(installed and item.get("api_ready") and models),
                        "reason": "Install the native client first."
                        if not installed
                        else "Configure the provider API key in Settings."
                        if not item.get("api_ready")
                        else "Add a reviewed exact model and prices in Settings."
                        if not models
                        else None,
                        "billing": {"kind": "metered", "label": "Metered API", "usd_metered": True},
                    },
                    {
                        "id": "subscription",
                        "label": "Subscription account",
                        "ready": bool(installed and subscribed),
                        "reason": "Install the native client first."
                        if not installed
                        else "Connect your subscription account."
                        if not subscribed
                        else None,
                        "billing": {
                            "kind": "subscription",
                            "label": "Provider subscription · model dollars unmeasured",
                            "usd_metered": False,
                        },
                    },
                ],
            )
            items.append(item)
        return {"harnesses": items}

    @staticmethod
    def connection_view(value):
        result = dict(value)
        if result.get("state") in {"auth_required", "not_connected", "canceled", "cancelled"}:
            result["state"] = "disconnected"
        return result

    async def configuration(self):
        result = self.settings.status()
        catalog = await self.catalog()
        result["setup_complete"] = bool(
            result.get("setup_complete")
            or any(mode["ready"] for item in catalog["harnesses"] for mode in item["modes"])
        )
        result["requirements"] = {
            "single_operator": True,
            "native_clients": [
                {"id": item["id"], "installed": item["installed"], **item["requirements"]}
                for item in catalog["harnesses"]
            ],
        }
        return result

    async def start(self, request):
        if request.auth_mode not in {"api_key", "subscription"}:
            raise ValueError("Select API key or subscription account")
        if request.reply_policy not in {"silent", "blockers_only"}:
            raise ValueError("The studio supports silence or answers to the agent's asks")
        if set(request.packs) - {"deliver", "belief", "footprint", "forecast", "watch"}:
            raise ValueError("Choose one of the supported episode tool packs")
        async with self._launch:
            library = await self.catalog()
            matching = [item for item in library["harnesses"] if item["id"] == request.harness_id]
            if len(matching) != 1:
                raise ValueError("Choose an installed harness from the library")
            item = matching[0]
            mode = next(row for row in item["modes"] if row["id"] == request.auth_mode)
            if not mode["ready"]:
                raise ValueError(mode["reason"] or "Harness setup is incomplete")
            if self.harnesses.is_busy(request.harness_id):
                raise ValueError("This harness account already has an active run")
            model = request.model or None
            if request.auth_mode == "api_key" and (
                not model
                or not any(
                    row["id"] == model and "api_key" in row["auth_modes"] for row in item["models"]
                )
            ):
                raise ValueError("Select an exact priced model for this API provider")
            config = RunConfig(
                name=request.name,
                goal=request.goal.strip() if request.goal and request.goal.strip() else None,
                family="F7",
                stage=2,
                research_mode="development",
                isolation_profile="cooperative",
                packs=list(dict.fromkeys(["deliver", *request.packs])),
                model_snapshot_id=model,
                model_billing_mode=request.auth_mode,
                native_output_token_limit=min(
                    4096,
                    int(
                        self.settings.model_prices(self.world.model_pricing_base)
                        .get("models", {})
                        .get(model, {})
                        .get("max_output_tokens", 4096)
                    ),
                ),
                harness_id=f"{item['id']}@{item['version']}/{request.auth_mode}",
                budget={"usd": request.budget_usd, "wall_s": request.wall_minutes * 60},
                reply_policy=request.reply_policy,
                operator_messages_allowed=False,
                capture_model_io=True,
            )
            info = self.world.create_run(config)
            try:
                session = await self.harnesses.launch(
                    info, request.harness_id, auth_mode=request.auth_mode, model=model
                )
            except BaseException:
                await self.world.end_run(
                    info["run_id"], reason="harness_launch_failed", truncated=True
                )
                raise
            result = self.views.get(info["run_id"])
            result["session"] = session
            return result


def create_studio_router(studio):
    router = APIRouter()
    root = Path(__file__).parent

    @router.get("/", include_in_schema=False)
    async def home():
        return FileResponse(
            root / "studio.html",
            headers={
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
                "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
            },
        )

    @router.get("/assets/{asset}", include_in_schema=False)
    async def asset(asset: str):
        if asset not in {"studio.js", "studio.css"}:
            raise HTTPException(404, "No such asset")
        return FileResponse(root / asset)

    @router.get("/api/settings")
    async def settings():
        return await studio.configuration()

    @router.post("/api/settings")
    async def save_settings(request: Request):
        body = await _body(request)
        try:
            studio.settings.update(body)
        except (TypeError, ValueError):
            return JSONResponse(
                {"detail": "Settings were not saved. Check the fields and current revision."},
                status_code=400,
            )
        return await studio.configuration()

    @router.post("/api/settings/providers/{provider}/validate")
    async def validate_provider(provider: str):
        return await studio.settings.validate_provider(provider)

    @router.delete("/api/settings/providers/{provider}/key")
    async def delete_key(provider: str):
        studio.settings.delete_api_key(provider)
        return await studio.configuration()

    @router.get("/api/harnesses")
    async def library():
        return await studio.catalog()

    @router.post("/api/harnesses/{harness_id}/install", status_code=202)
    async def install(harness_id: str):
        return await studio.harnesses.install(harness_id)

    async def auth_action(harness_id, action, **kwargs):
        if harness_id not in studio.harnesses.registry:
            raise ValueError("Unknown native harness")
        # Discovery resolves a newly installed binary before invoking its official CLI.
        await studio.harnesses.library()
        return studio.connection_view(
            await getattr(studio.harnesses._auth(), action)(harness_id, **kwargs)
        )

    @router.get("/api/harnesses/{harness_id}/connection")
    async def connection(harness_id: str):
        return await auth_action(harness_id, "status")

    @router.post("/api/harnesses/{harness_id}/connect", status_code=202)
    async def connect(harness_id: str):
        return await auth_action(harness_id, "start")

    @router.post("/api/harnesses/{harness_id}/cancel-connect")
    async def cancel_connect(harness_id: str):
        return await auth_action(harness_id, "cancel")

    @router.post("/api/harnesses/{harness_id}/disconnect")
    async def disconnect(harness_id: str):
        return await auth_action(harness_id, "disconnect")

    @router.post("/api/harnesses/{harness_id}/connection/code")
    async def auth_code(harness_id: str, request: Request):
        body = await _body(request, maximum=8192)
        if set(body) != {"code"} or not isinstance(body["code"], str):
            raise HTTPException(400, "Enter the provider's authorization code")
        try:
            return await auth_action(harness_id, "submit_code", code=body["code"])
        except (ValueError, TypeError):
            raise HTTPException(400, "The authorization code could not be submitted") from None

    @router.get("/api/runs")
    async def runs():
        return studio.views.list_runs()

    @router.post("/api/runs", status_code=201)
    async def start_run(request: Request):
        try:
            body = StartRun.model_validate(await _body(request))
        except ValidationError:
            raise HTTPException(400, "Check the run settings and selected harness") from None
        return await studio.start(body)

    @router.get("/api/runs/{run_id}")
    async def run(run_id: str):
        result = studio.views.get(run_id)
        result["sessions"] = studio.harnesses.list(run_id)
        return result

    @router.post("/api/runs/{run_id}/stop")
    async def stop(run_id: str):
        await studio.world.end_run(run_id, reason="harness_stopped")
        return studio.views.get(run_id)

    @router.get("/api/runs/{run_id}/beliefs")
    async def beliefs(
        run_id: str, query: str = "all", entity: str | None = None, id: str | None = None
    ):
        return studio.views.beliefs(run_id, query=query, entity=entity, id=id)

    @router.get("/api/runs/{run_id}/footprint")
    async def footprint(run_id: str):
        return studio.views.footprint(run_id)

    @router.get("/api/runs/{run_id}/asks")
    async def asks(run_id: str):
        return studio.views.asks(run_id)

    @router.get("/api/runs/{run_id}/events")
    async def events(
        run_id: str, since: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)
    ):
        return studio.views.events(run_id, since=since, limit=limit)

    @router.get("/api/runs/{run_id}/ledger")
    async def ledger(run_id: str):
        return studio.views.ledger(run_id)

    @router.get("/api/runs/{run_id}/logs")
    async def logs(
        run_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)
    ):
        sessions = studio.harnesses.list(run_id)
        return (
            studio.harnesses.logs(sessions[0]["session_id"], offset, limit)
            if sessions
            else {"lines": []}
        )

    return router


async def _body(request, maximum=64 * 1024):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(413, "Request exceeds the supported size")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected a JSON object")
        return value
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Expected a JSON object") from None
