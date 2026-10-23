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


class StartLife(StrictModel):
    harness_id: str = Field(min_length=1, max_length=80)
    auth_mode: str = "api_key"
    model: str | None = Field(default=None, max_length=200)
    objective: str | None = Field(default=None, max_length=20000)
    name: str | None = Field(default=None, max_length=120)
    balance_usd: float = Field(default=20, gt=0, le=10000)
    session_minutes: float = Field(default=120, gt=0, le=10080)
    reply_policy: str = "blockers_only"
    packs: list[str] = Field(default_factory=lambda: ["deliver", "belief", "footprint"])
    market: bool = False


class RecordDeposit(StrictModel):
    usd: float = Field(gt=0, le=100000)
    source: str = Field(min_length=1, max_length=500)
    evidence: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(min_length=8, max_length=100)


class PostBounty(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    criterion: str = Field(min_length=1, max_length=2000)
    usd: float = Field(gt=0, le=10000)
    days: float = Field(default=7, gt=0, le=365)


# How long a resumed life waits for its harness before treating the launch as a rest.
LAUNCH_ATTEMPTS, LAUNCH_RETRY_S = 10, 3.0
TERMINAL_RUNS = {"sealed", "ended", "killed"}


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

    @staticmethod
    def _check(request):
        if request.auth_mode not in {"api_key", "subscription"}:
            raise ValueError("Select API key or subscription account")
        if request.reply_policy not in {"silent", "blockers_only"}:
            raise ValueError("The studio supports silence or answers to the agent's asks")
        if set(request.packs) - {"deliver", "belief", "footprint", "forecast", "watch"}:
            raise ValueError("Choose one of the supported episode tool packs")

    async def _resolve(self, request):
        """The installed, ready harness and exact priced model a request names."""
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
        if self.reserved(request.harness_id):
            raise ValueError("A living life holds this harness; beam it up first")
        model = request.model or None
        if request.auth_mode == "api_key" and (
            not model
            or not any(
                row["id"] == model and "api_key" in row["auth_modes"] for row in item["models"]
            )
        ):
            raise ValueError("Select an exact priced model for this API provider")
        return item, model

    def _config(self, request, item, model, *, goal, packs, budget):
        return RunConfig(
            name=request.name,
            goal=goal,
            family="F7",
            stage=2,
            research_mode="development",
            isolation_profile="cooperative",
            packs=packs,
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
            budget=budget,
            reply_policy=request.reply_policy,
            operator_messages_allowed=False,
            capture_model_io=True,
        )

    async def start(self, request):
        self._check(request)
        async with self._launch:
            item, model = await self._resolve(request)
            config = self._config(
                request,
                item,
                model,
                goal=request.goal.strip() if request.goal and request.goal.strip() else None,
                packs=list(dict.fromkeys(["deliver", *request.packs])),
                budget={"usd": request.budget_usd, "wall_s": request.wall_minutes * 60},
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

    def _lives(self):
        return self.world.services["lives"]

    def _studies(self):
        return self.world.services["studies"]

    def _runner(self, life_id):
        runners = [
            value
            for value in self._studies().list_life_runners(life_id)
            if value["cell"]["baseline"] == "native"
        ]
        return runners[-1] if runners else None

    def reserved(self, harness_id):
        """A Studio life holds its harness from birth until it ends, resting included."""
        for life in self._lives().list():
            runner = None if life["ended"] else self._runner(life["life_id"])
            if runner and runner["cell"]["config"]["harness_id"].startswith(harness_id + "@"):
                return True
        return False

    async def start_life(self, request):
        """A life on a balance, with no objective unless one is given; it rests and strands."""
        if request.auth_mode != "api_key":
            raise ValueError(
                "A life's balance needs metered model spend; subscriptions can't be metered"
            )
        self._check(request)
        objective = request.objective.strip() if request.objective else ""
        async with self._launch:
            item, model = await self._resolve(request)
            # Every Studio life may claim bounties; trading stays an explicit opt-in.
            packs = [
                "deliver",
                "schedule",
                "bounty",
                *request.packs,
                *(["market"] if request.market else []),
            ]
            config = self._config(
                request,
                item,
                model,
                goal=None,
                packs=list(dict.fromkeys(packs)),
                # The session may use everything the life has left; the life caps it.
                budget={"usd": 10000, "wall_s": request.session_minutes * 60},
            ).model_dump(mode="json")
            lives = self._lives()
            life = lives.create(
                {
                    "stage": 2,
                    "research_mode": "development",
                    "name": request.name,
                    "objective": objective or None,
                    "budget": {"usd_life": request.balance_usd, "wall_life_days": None},
                    "continuity": "fresh_context_workspace",
                    "wake_policy": "time_or_events",
                    "reply_policy": request.reply_policy,
                    "driver": {"metric": "usd_balance", "floor": 0},
                    "on_exhaustion": "strand",
                    "on_harness_exit": "rest",
                    "income_channels": [
                        "recorded",
                        "bounty",
                        *(["market"] if request.market else []),
                    ],
                    "market_settlement": "balance" if request.market else "paper",
                    "afterlife_days": [],
                }
            )
            life_id = life["life_id"]
            try:
                runner = self._studies().plan_life(
                    life_id, {"baseline": "native", "model": model, "config": config}
                )
                await self._studies().execute_life(
                    runner["runner_id"], execute=True, budget_usd=request.balance_usd
                )
            except BaseException:
                await lives.end(life_id, reason="studio_launch_failed")
                raise
        return self.life(life_id)

    async def native_session(self, created, cell):
        """Life-runner callback: one native harness session, awaited until it is sealed."""
        config = cell["config"]
        harness_id = config["harness_id"].split("@")[0]
        auth_mode = config.get("model_billing_mode", "api_key")
        for attempt in range(LAUNCH_ATTEMPTS):
            try:
                session = await self.harnesses.launch(
                    created, harness_id, auth_mode=auth_mode, model=cell["model"]
                )
                break
            except ValueError as error:
                # Startup recovery can still hold a stale session; a later attempt may pass.
                if attempt + 1 == LAUNCH_ATTEMPTS:
                    self.world.store.append_record(
                        created["run_id"], "harness_launch_failed", reason=str(error)[:500]
                    )
                    await self.world.end_run(
                        created["run_id"], reason="harness_launch_failed", truncated=True
                    )
                    return {}
                await asyncio.sleep(LAUNCH_RETRY_S)
        task = self.harnesses.tasks.get(session["session_id"])
        if task is not None:
            # Shielded: pausing the runner must not cancel the session's own cleanup and seal.
            await asyncio.shield(task)
        else:
            # A session this process does not own still ends with its run's seal.
            while self.world.store.get_run(created["run_id"])["state"] not in TERMINAL_RUNS:
                await asyncio.sleep(0.25)
        return {}

    def _life_summary(self, life):
        lives = self._lives()
        status = lives.status(life["life_id"])
        runner = self._runner(life["life_id"])
        config = runner["cell"]["config"] if runner else {}
        return {
            "life_id": life["life_id"],
            "name": life.get("name"),
            "objective": life.get("objective"),
            "life_state": status["life_state"],
            "balance_usd": status["balance_usd"],
            "spent_usd": status["spent_usd"],
            "income_usd": status["income_usd"],
            "created_at": life["created_at"],
            "ended": life["ended"],
            "sessions": len(life["sessions"]),
            "active_run_id": life["active_run_id"],
            "wake_rules": life["wake_rules"],
            "harness_id": config.get("harness_id"),
            "model": runner["cell"].get("model") if runner else None,
            "runner_state": runner["state"] if runner else None,
            "market": "market" in life.get("income_channels", []),
        }

    def lives(self):
        return {
            "lives": [
                self._life_summary(life)
                for life in reversed(self._lives().list())
                if self._runner(life["life_id"])
            ]
        }

    def life(self, life_id):
        life = self._lives().get(life_id)
        if not self._runner(life_id):
            raise KeyError(life_id)
        runs = {row["run_id"]: row for row in self.views.list_runs()["runs"]}
        return {
            "life": self._life_summary(life),
            "account": self.world.services["bank"].account(life_id),
            "sessions": [
                runs[item["run_id"]]
                for item in reversed(life["sessions"])
                if item["run_id"] in runs
            ],
        }

    async def beam_up(self, life_id):
        self.life(life_id)
        await self._lives().end(life_id, reason="beam_up")
        return self.life(life_id)

    def record_deposit(self, life_id, request):
        self.life(life_id)
        self.world.services["bank"].record(
            life_id,
            usd=request.usd,
            source=request.source,
            evidence=request.evidence,
            request_id=request.request_id,
        )
        return self.life(life_id)

    def bounties(self):
        market = self.world.services["market"]
        return {
            "bounties": [
                json.loads(row["payload"])
                for row in market.db.execute(
                    "SELECT payload FROM sponsor_bounties ORDER BY rowid DESC"
                )
            ]
        }

    def post_bounty(self, request):
        from datetime import UTC, datetime, timedelta

        self.world.services["market"].register_bounty(
            title=request.title,
            criterion=request.criterion,
            usd=request.usd,
            sponsor="operator",
            expires_at=(datetime.now(UTC) + timedelta(days=request.days)).isoformat(),
        )
        return self.bounties()


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

    @router.get("/api/lives")
    async def lives():
        return studio.lives()

    @router.post("/api/lives", status_code=201)
    async def start_life(request: Request):
        try:
            body = StartLife.model_validate(await _body(request))
        except ValidationError:
            raise HTTPException(400, "Check the life settings and selected harness") from None
        return await studio.start_life(body)

    @router.get("/api/lives/{life_id}")
    async def life(life_id: str):
        return studio.life(life_id)

    @router.post("/api/lives/{life_id}/beam-up")
    async def beam_up(life_id: str):
        return await studio.beam_up(life_id)

    @router.post("/api/lives/{life_id}/deposits", status_code=201)
    async def deposit(life_id: str, request: Request):
        try:
            body = RecordDeposit.model_validate(await _body(request))
        except ValidationError:
            raise HTTPException(
                400, "Enter an amount, where it came from, and its evidence"
            ) from None
        return studio.record_deposit(life_id, body)

    @router.get("/api/bounties")
    async def bounties():
        return studio.bounties()

    @router.post("/api/bounties", status_code=201)
    async def post_bounty(request: Request):
        try:
            body = PostBounty.model_validate(await _body(request))
        except ValidationError:
            raise HTTPException(400, "A bounty needs a title, criterion and payout") from None
        return studio.post_bounty(body)

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
