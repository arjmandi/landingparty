"""HTTP control plane. Bind only to the researcher's trusted interface."""

import inspect
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import __version__
from .gateway import Gateway
from .lane import create_lane_router, load_prices, price_table_id
from .models import ActionRequest, EndRequest, KillRequest, OperatorMessage, RunConfig, RunReference
from .runtime import World
from .security import protect_researcher
from .storage import Store


def create_app(
    data_dir: str | Path | None = None,
    *,
    gateway=None,
    search_client: httpx.AsyncClient | None = None,
    lane_client: httpx.AsyncClient | None = None,
    prices: dict | None = None,
) -> FastAPI:
    store = Store(data_dir or os.environ.get("WORLD_DATA_DIR", "data"))
    policy_path = Path(
        os.environ.get("WORLD_POLICY_FILE", Path(__file__).parents[1] / "policy/default.json")
    )
    if gateway is None:
        from .dns import SecureDNSResolver
        from .signing import WorldSigner

        resolver = (
            SecureDNSResolver(os.environ["WORLD_DOH_URL"])
            if os.environ.get("WORLD_DOH_URL")
            else None
        )
        signer = (
            WorldSigner(store.data_dir, os.environ["WORLD_IDENTITY_URL"])
            if os.environ.get("WORLD_IDENTITY_URL")
            else None
        )
        gateway = Gateway(
            store.data_dir,
            policy=json.loads(policy_path.read_text()),
            resolver=resolver,
            signer=signer,
        )
    world = World(store, gateway, search_client=search_client)
    from .breakers import BreakerService
    from .extensions import initialize

    initialize(world)
    if "workspace_checkpoints" in world.services:
        store.ledger_observer = world.services["workspace_checkpoints"].observe_debit
    if "payments" in world.services:
        gateway.payment_handler = world.services["payments"].handle_402
    world.services["breakers"] = BreakerService(world)

    @asynccontextmanager
    async def lifespan(app):
        await world.recover()
        yield
        await world.aclose()

    app = FastAPI(title="Landing Party worldd", version=__version__, lifespan=lifespan)
    protect_researcher(app)
    app.state.world = world
    app.state.store = store
    internal_token = secrets.token_urlsafe(32)

    def observe_usage(**values):
        target = getattr(world.services.get("browser"), "observe_model_usage", None)
        if target:
            result = target(**values)
            if inspect.isawaitable(result):
                world.track_observer(values["run_id"], result)

    world.perception_pricing = (
        world.services["perception"].pricing_identity() if "perception" in world.services else {}
    )
    lane_router = create_lane_router(
        store,
        client=lane_client,
        prices=prices,
        perception_prices=world.perception_pricing,
        internal_token=internal_token,
        capabilities=world.capabilities,
        usage_observer=observe_usage,
        admission_guard=world.admission_guard,
    )
    world.internal_model_token = internal_token
    world.lane = lane_router.lane_manager
    world.price_table_id = price_table_id(
        prices if prices is not None else load_prices(), perception_prices=world.perception_pricing
    )
    app.state.lane_router = lane_router
    app.include_router(lane_router)
    from .model_callbacks import configure_models

    configure_models(app, world, prices=prices)
    if "studies" in world.services:
        world.services["studies"].configure(app)
    from .api_ext import create_service_router

    app.include_router(create_service_router(world))

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError):
        return JSONResponse({"detail": "Run or resource not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/healthz")
    async def health():
        store.db.execute("SELECT 1").fetchone()
        return {
            "status": "ok",
            "version": __version__,
            "world_id": world.world_id,
            "supported_stages": [0, 1, 2, 3],
            "services": sorted(world.services),
            "research_gates": world.services["registry"].gates()
            if "registry" in world.services
            else [],
        }

    @app.get("/.well-known/http-message-signatures-directory")
    async def signing_directory():
        signer = getattr(gateway, "signer", None)
        if signer is None:
            return JSONResponse({"detail": "HTTP signing is not configured"}, status_code=404)
        return JSONResponse(signer.directory(), headers={"Cache-Control": "public, max-age=300"})

    @app.post("/v1/run", status_code=201)
    async def start(config: RunConfig):
        return world.create_run(config)

    @app.get("/v1/runs")
    async def runs():
        return {"runs": store.list_runs()}

    @app.get("/v1/status")
    async def status(run_id: str):
        return world.status(run_id)

    @app.get("/v1/manifest")
    async def manifest(run_id: str):
        return world.manifest(run_id)

    @app.post("/v1/act")
    async def act(action: ActionRequest):
        return await world.act(action.run_id, action.tool, action.params)

    @app.get("/v1/inbox")
    async def inbox(
        run_id: str,
        since: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=500),
        wait_seconds: float = Query(0, ge=0, le=60),
    ):
        return await world.inbox(run_id, since=since, limit=limit, wait_seconds=wait_seconds)

    @app.post("/v1/beam-up")
    async def beam_up(body: EndRequest):
        return await world.end_run(body.run_id, reason=body.reason)

    @app.post("/v1/freeze")
    async def freeze(body: RunReference):
        return await world.freeze(body.run_id)

    @app.post("/v1/resume")
    async def resume(body: RunReference):
        return await world.resume(body.run_id)

    @app.post("/v1/kill")
    async def kill(body: KillRequest):
        if body.dry_run:
            return await world.services["preflight"].kill_check(body.run_id)
        return await world.end_run(body.run_id, reason="operator_stop", truncated=True)

    @app.get("/v1/report")
    async def report(
        run_id: str,
        format: Literal["json", "markdown", "html", "csv"] = "json",
    ):
        result = world.life_report(run_id) if run_id.startswith("life_") else world.report(run_id)
        if format == "json":
            return result
        from analysis.publication import render_publication

        media = {"markdown": "text/markdown", "html": "text/html", "csv": "text/csv"}
        extension = {"markdown": "md", "html": "html", "csv": "csv"}[format]
        return Response(
            render_publication(result, format=format),
            media_type=media[format],
            headers={"Content-Disposition": f'attachment; filename="{run_id}.{extension}"'},
        )

    @app.get("/v1/export")
    async def export(run_id: str):
        return Response(
            world.export(run_id),
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{run_id}.tar.gz"'},
        )

    @app.get("/v1/bridge")
    async def bridge(run_id: str | None = None):
        return world.bridge(run_id)

    @app.post("/v1/bridge/message")
    async def operator_message(body: OperatorMessage):
        return world.operator_message(**body.model_dump())

    @app.post("/v1/ask/resolve")
    async def resolve_ask(body: dict):
        if "asks" not in world.services:
            raise ValueError("Ask service unavailable")
        values = dict(body)
        values.pop("run_id", None)
        return world.services["asks"].answer(**values)

    @app.get("/bridge", response_class=HTMLResponse)
    async def console():
        return (Path(__file__).with_name("console.html")).read_text(encoding="utf-8")

    return app
