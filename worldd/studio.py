"""Single-operator product API over the recorded world and native harness library."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, ValidationError

from .lane import load_catalog
from .models import RunConfig, StrictModel
from .run_views import RunViews
from .util import now


class StartRun(StrictModel):
    harness_id: str = Field(min_length=1, max_length=80)
    auth_mode: str = "api_key"
    model: str | None = Field(default=None, max_length=200)
    effort: str | None = Field(default=None, max_length=20)
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
    effort: str | None = Field(default=None, max_length=20)
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


class SwitchModel(StrictModel):
    model: str = Field(min_length=1, max_length=200)
    effort: str | None = Field(default=None, max_length=20)


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
        self.model_catalog = load_catalog()
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

    def _model_row(self, model, price, *, efforts, served=None, default_effort=None):
        """One model as a person chooses it: label, group, rates, effort levels."""
        entry = self._catalog_entry(model)
        levels = entry.get("efforts", []) if efforts is None else efforts
        return {
            "id": model,
            "label": entry.get("label", model),
            "group": entry.get("group", "earlier"),
            "input_usd": price.get("input") if price else None,
            "output_usd": price.get("output") if price else None,
            "priced": bool(price),
            "served": served,
            "efforts": list(levels),
            "default_effort": (default_effort or entry.get("default_effort")) if levels else None,
        }

    def _connection_models(self, item, table, connection):
        """What a connection can run: what it serves, crossed with what has a reviewed price.

        An API key lists what its provider served at validation. A Codex account lists
        its own catalogue. A Claude subscription has no list, so it is offered the
        reviewed catalogue. A model with no reviewed price is listed but not selectable
        wherever spend must be priced.
        """
        provider = item["provider"]
        priced = {
            name: row
            for name, row in table.items()
            if row.get("provider") == provider
            and self._catalog_entry(name).get("group") != "retired"
        }
        harness_levels = item.get("efforts", [])

        def narrowed(levels):
            return [level for level in levels if level in harness_levels]

        if connection == "api_key":
            served = self.settings.served_models(provider)
            names = list(priced) + [m for m in served or [] if m not in priced and m not in table]
            rows = [
                self._model_row(
                    name,
                    priced.get(name),
                    efforts=narrowed(self._catalog_entry(name).get("efforts", [])),
                    served=None if served is None else name in served,
                )
                for name in names
            ]
        else:
            auth = self.harnesses._auth()
            account = auth.served_models(item["id"]) if auth else None
            if account:
                rows = [
                    self._model_row(
                        row["id"],
                        priced.get(row["id"]),
                        efforts=narrowed(row["efforts"]),
                        served=True,
                        default_effort=row["default_effort"],
                    )
                    for row in account
                ]
            else:
                rows = [
                    self._model_row(
                        name,
                        row,
                        efforts=narrowed(self._catalog_entry(name).get("efforts", [])),
                    )
                    for name, row in priced.items()
                ]
        # Priced spend (a metered key, an estimated subscription) needs a reviewed price.
        needs_price = connection == "api_key" or bool(item.get("transcript"))
        for row in rows:
            row["selectable"] = row["served"] is not False and (row["priced"] or not needs_price)
        order = {"current": 0, "earlier": 1}
        return sorted(rows, key=lambda row: (order.get(row["group"], 2), not row["priced"]))

    async def catalog(self):
        actual = await self.harnesses.library()
        prices = self.settings.model_prices(self.world.model_pricing_base)
        table = prices.get("models", prices)
        items = []
        for source in actual["items"]:
            item = dict(source)
            item.setdefault("provider", "anthropic" if item["id"] == "claude-code" else "openai")
            item.setdefault("efforts", [])
            connection = item.get("subscription_auth") or {}
            subscribed = (
                connection.get("state") == "connected"
                and connection.get("auth_method") == "subscription"
            )
            installed = item.get("installed", False)
            provider_name = "Anthropic" if item["provider"] == "anthropic" else "OpenAI"
            estimated = bool(item.get("transcript"))
            key_models = self._connection_models(item, table, "api_key")
            account_models = self._connection_models(item, table, "subscription")
            connections = [
                {
                    "id": "api_key",
                    "label": f"{provider_name} API key",
                    "configured": bool(item.get("api_ready")),
                    "ready": bool(
                        installed
                        and item.get("api_ready")
                        and any(row["selectable"] for row in key_models)
                    ),
                    "reason": "Install the native client first."
                    if not installed
                    else f"Add your {provider_name} API key on this harness."
                    if not item.get("api_ready")
                    else "No model this key serves has a reviewed price yet."
                    if not any(row["selectable"] for row in key_models)
                    else None,
                    "billing": {"kind": "metered", "label": "Metered API", "usd_metered": True},
                    "models": key_models,
                },
                {
                    "id": "subscription",
                    "label": "Subscription account",
                    "configured": subscribed,
                    "ready": bool(installed and subscribed),
                    "reason": "Install the native client first."
                    if not installed
                    else "Connect your subscription account."
                    if not subscribed
                    else None,
                    "billing": {
                        "kind": "estimated" if estimated else "subscription",
                        "label": "Provider subscription · model dollars estimated at API prices"
                        if estimated
                        else "Provider subscription · model dollars unmeasured",
                        "usd_metered": False,
                    },
                    "models": account_models,
                },
            ]
            item.update(
                provider=item["provider"],
                kind=item.get("kind", "add-on"),
                description=item.get("description")
                or f"{provider_name}'s native coding agent with your world tools.",
                connection=self.connection_view(connection),
                connections=connections,
                # The flat list and modes are the earlier shape, kept for older clients.
                models=[
                    {"id": row["id"], "label": row["label"], "auth_modes": [entry["id"]]}
                    for entry in connections
                    for row in entry["models"]
                    if row["selectable"]
                ],
                modes=[
                    {key: entry[key] for key in ("id", "label", "ready", "reason", "billing")}
                    for entry in connections
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
        """The ready harness, connection, exact model and effort a request names."""
        library = await self.catalog()
        matching = [item for item in library["harnesses"] if item["id"] == request.harness_id]
        if len(matching) != 1:
            raise ValueError("Choose an installed harness from the library")
        item = matching[0]
        connection = next(row for row in item["connections"] if row["id"] == request.auth_mode)
        if not connection["ready"]:
            raise ValueError(connection["reason"] or "Harness setup is incomplete")
        if self.harnesses.is_busy(request.harness_id):
            raise ValueError("This harness account already has an active run")
        if self.reserved(request.harness_id):
            raise ValueError("A living life holds this harness; beam it up first")
        row = next((row for row in connection["models"] if row["id"] == request.model), None)
        if row is None or not row["selectable"]:
            raise ValueError(
                "Choose a model this connection serves"
                if row is not None and row["served"] is False
                else "This model has no reviewed price, so its spend cannot be metered or "
                "estimated. Choose a priced model, or add its rates to the world's price table."
                if row is not None or request.model
                else "Choose the exact model to run"
            )
        effort = request.effort or row["default_effort"]
        if effort is not None and effort not in row["efforts"]:
            raise ValueError(f"{row['label']} does not take that effort level on this harness")
        return item, row["id"], effort

    def _config(
        self, request, item, model, *, goal, packs, budget, model_metering="lane", effort=None
    ):
        return RunConfig(
            model_metering=model_metering,
            effort=effort,
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
            item, model, effort = await self._resolve(request)
            config = self._config(
                request,
                item,
                model,
                goal=request.goal.strip() if request.goal and request.goal.strip() else None,
                packs=list(dict.fromkeys(["deliver", *request.packs])),
                budget={"usd": request.budget_usd, "wall_s": request.wall_minutes * 60},
                # A subscription session is estimated wherever its client's stream is read.
                model_metering="transcript"
                if request.auth_mode == "subscription" and item.get("transcript")
                else "lane",
                effort=effort,
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
        self._check(request)
        objective = request.objective.strip() if request.objective else ""
        async with self._launch:
            item, model, effort = await self._resolve(request)
            # A subscription bypasses the model lane, so its life's balance is estimated
            # from the client's own stream; a harness whose stream is unverified cannot.
            metering = "transcript" if request.auth_mode == "subscription" else "lane"
            if metering == "transcript" and not item.get("transcript"):
                raise ValueError(
                    f"A {item['name']} life needs an API key for now: its subscription "
                    "transcript's usage reports are not yet confirmed for estimating a balance"
                )
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
                model_metering=metering,
                effort=effort,
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
                    "model_metering": metering,
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
            # Its personal log and footprint carry forward into the latest session.
            "latest_run_id": life["sessions"][-1]["run_id"] if life["sessions"] else None,
            "wake_rules": life["wake_rules"],
            "hold_reason": status.get("hold_reason"),
            "harness_id": config.get("harness_id"),
            "model": runner["cell"].get("model") if runner else None,
            "effort": config.get("effort"),
            "runner_state": runner["state"] if runner else None,
            "market": "market" in life.get("income_channels", []),
            "estimated": life.get("model_metering") == "transcript",
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

    def life_asks(self, life_id):
        """Every ticket the life raised, across its sessions, newest session first."""
        life = self._lives().get(life_id)
        if not self._runner(life_id):
            raise KeyError(life_id)
        return {
            "tickets": [
                ticket
                for item in reversed(life["sessions"])
                for ticket in self.views.asks(item["run_id"]).get("tickets", [])
            ]
        }

    def _catalog_entry(self, model):
        return self.model_catalog.get("models", {}).get(model, {})

    async def switch_model(self, life_id, request):
        """Resume a held life on another model: a new runner, the old one completed.

        The runner's configuration is hashed and frozen, so the switch plans a fresh
        runner rather than editing the old one; each session's own tuple names its model.
        """
        self.life(life_id)
        lives, studies = self._lives(), self._studies()
        life = lives.get(life_id)
        if life["ended"] or life["state"] != "model_hold":
            raise ValueError("Only a held life can switch model")
        old = self._runner(life_id)
        if old.get("study_binding"):
            raise ValueError("A study's life keeps its frozen configuration")
        config = dict(old["cell"]["config"])
        if request.model == old["cell"]["model"]:
            raise ValueError("Choose a different model than the one that failed")
        item = next(
            item
            for item in (await self.catalog())["harnesses"]
            if config["harness_id"].startswith(item["id"] + "@")
        )
        connection = next(
            row for row in item["connections"] if row["id"] == config["model_billing_mode"]
        )
        row = next((row for row in connection["models"] if row["id"] == request.model), None)
        if row is None or not row["selectable"]:
            raise ValueError("Choose a reviewed, current model this connection can run")
        effort = request.effort or row["default_effort"]
        if effort is not None and effort not in row["efforts"]:
            raise ValueError(f"{row['label']} does not take that effort level on this harness")
        config["effort"] = effort
        async with self._launch:
            await studies.pause_life(old["runner_id"], reason="model_switched")
            superseded = studies.get_life_runner(old["runner_id"])
            superseded.update(state="completed", completed_at=now(), reason="model_switched")
            studies._save_life_runner(superseded)
            config["model_snapshot_id"] = request.model
            runner = studies.plan_life(
                life_id, {"baseline": "native", "model": request.model, "config": config}
            )
            await studies.execute_life(
                runner["runner_id"], execute=True, budget_usd=life["budget"]["usd_life"]
            )
            with self.world.store._transaction():
                lives._save(
                    lives._load(life_id),
                    "model_switched",
                    model_from=old["cell"]["model"],
                    model_to=request.model,
                    reason=(life.get("hold_reason") or {}).get("kind"),
                )
            lives.resume_held(life_id, reason="model_switched", model=request.model)
        return self.life(life_id)

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

    @router.get("/api/lives/{life_id}/asks")
    async def life_asks(life_id: str):
        return studio.life_asks(life_id)

    @router.post("/api/lives/{life_id}/beam-up")
    async def beam_up(life_id: str):
        return await studio.beam_up(life_id)

    @router.post("/api/lives/{life_id}/model")
    async def switch_model(life_id: str, request: Request):
        try:
            body = SwitchModel.model_validate(await _body(request))
        except ValidationError:
            raise HTTPException(400, "Choose the model to resume on") from None
        return await studio.switch_model(life_id, body)

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
