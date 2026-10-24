"""Run lifecycle, capability boundaries and asynchronous world execution."""

import asyncio
import inspect
import io
import json
import os
import re
import shutil
import tarfile
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

import httpx
from pydantic import ValidationError

from .models import TOOL_SERVICES, RunConfig, manifest, tool_model
from .scanning import contains_sensitive_data
from .search import PROVIDERS, normalize_results, provider_request, search_price
from .util import blocked, canonical, digest, identifier, now
from .versioning import implementation_identity
from .workspaces import POLICY_ID as WORKSPACE_POLICY_ID
from .workspaces import share_workspace, snapshot_manifest, snapshot_workspace, verify_import


class World:
    def __init__(self, store, gateway, *, search_client: httpx.AsyncClient | None = None):
        self.store = store
        self.gateway = gateway
        self.search_client = search_client or httpx.AsyncClient(timeout=30, trust_env=True)
        self._owns_search_client = search_client is None
        self.jobs: dict[str, dict[str, asyncio.Task]] = {}
        self.pending: set[str] = set()
        self.watchers: dict[str, list[asyncio.Task]] = {}
        self.deadlines: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.semaphores: dict[str, asyncio.Semaphore] = {}
        self.wakeups: dict[str, asyncio.Event] = {}
        self.cursors: dict[str, int] = {}
        self._ending: set[str] = set()
        self.actions: dict[str, dict[str, tuple[str, dict]]] = {}
        self.lane = None
        self.price_table_id = None
        self.perception_pricing = None
        self.world_id = implementation_identity()
        self.services = {}
        from .events import LateEvents
        from .security import Capabilities

        self.capabilities = Capabilities(store.data_dir)
        self.late_events = LateEvents(store.data_dir)
        self.background = set()
        self.background_runs = {}
        self.network_requests = {}
        self.internal_network_actions = set()
        self.kernel_runs = {}
        self.closing = False
        self._network_slots = ContextVar("landing_party_network_slots", default=frozenset())

    def _lock(self, run_id):
        return self.locks.setdefault(run_id, asyncio.Lock())

    def ask_provenance(self, run_id, params=None):
        from .context import ask_provenance

        return ask_provenance(self, run_id, params)

    def credential(self, name, default=None):
        settings = getattr(self, "instance_settings", None)
        return settings.resolve(name, default) if settings else os.environ.get(name, default)

    def contains_sensitive_data(self, value):
        settings = getattr(self, "instance_settings", None)
        return contains_sensitive_data(
            value, extra_secrets=settings.secret_values() if settings else ()
        )

    def _in_flight(self, run_id):
        return sum(
            action_id not in self.internal_network_actions
            for action_id in self.jobs.get(run_id, {})
        )

    def _signal(self, run_id):
        self.wakeups.setdefault(run_id, asyncio.Event()).set()

    @asynccontextmanager
    async def network_slot(self, run_id):
        identity = (run_id, asyncio.current_task())
        held = self._network_slots.get()
        if identity in held:
            yield
            return
        async with self.semaphores[run_id]:
            token = self._network_slots.set(held | {identity})
            try:
                yield
            finally:
                self._network_slots.reset(token)

    def track_observer(self, run_id, awaitable):
        task = asyncio.create_task(awaitable)
        self.background.add(task)
        self.background_runs[task] = run_id

        def completed(task):
            self.background.discard(task)
            self.background_runs.pop(task, None)
            if not task.cancelled() and task.exception() is not None:
                current = self.store.get_run(run_id)
                if current["state"] not in {"sealed", "killed", "ended"}:
                    self.store.append_record(
                        run_id, "observer_error", error=type(task.exception()).__name__
                    )
                    self.store.update_run(run_id, tainted=True)

        task.add_done_callback(completed)
        return task

    def push(self, run_id, event):
        run = self.store.get_run(run_id)
        life_id = run["config"].get("life_id")
        if run["state"] in {"sealed", "killed", "ended"}:
            result = self.late_events.append(run_id, life_id, event)
            if life_id and "lives" in self.services:
                self.services["lives"].notify_event(life_id, result)
                active = self.services["lives"].get(life_id).get("active_run_id")
                if (
                    active
                    and active != run_id
                    and self.store.get_run(active)["state"] in {"running", "frozen"}
                ):
                    self.push(
                        active,
                        {
                            **event,
                            "source_run_id": run_id,
                            "late_event_id": result["event_id"],
                            "observed_at": result["ts"],
                        },
                    )
                    self.late_events.delivered(result["event_id"], active)
            return result
        if event.get("late_event_id"):
            result = self.store.push_event_once(
                run_id, event, source_key="late:" + event["late_event_id"]
            )
        else:
            result = self.store.push_event(run_id, event)
        if event.get("type") in {
            "ask_resolved",
            "expectation_resolved",
            "belief_review",
            "page_changed",
        }:
            self.track_observer(run_id, self._observe_event(run_id, result))
        if life_id and "lives" in self.services:
            life = self.services["lives"].get(life_id)
            if life["state"] in {"sleeping", "waking"} and life.get("active_run_id") == run_id:
                self.late_events.append(
                    run_id,
                    life_id,
                    result,
                    source_event_id=result["event_id"],
                    observed_at=result["ts"],
                )
            self.services["lives"].notify_event(life_id, result)
        self._signal(run_id)
        path = self.store.run_dir(run_id) / "world" / "inbox"
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{result['event_id']}.json").write_text(canonical(result), encoding="utf-8")
        return result

    def create_run(
        self, config: RunConfig, *, register_prereg=None, execution_binding=None
    ) -> dict:
        if self.closing:
            raise ValueError("World is shutting down")
        if register_prereg is not None and config.series_id:
            raise ValueError("Choose an existing series or a dependency-resolved registration")
        if execution_binding is not None:
            allowed = {
                "study_id",
                "cell_id",
                "seed",
                "episode",
                "dependency_protocol_hash",
                "resolution_hash",
            }
            if (
                not isinstance(execution_binding, dict)
                or set(execution_binding) - allowed
                or any(
                    not isinstance(execution_binding.get(key), str)
                    or not 1 <= len(execution_binding[key]) <= 128
                    for key in ("study_id", "cell_id")
                )
                or type(execution_binding.get("seed")) is not int
                or (
                    execution_binding.get("episode") is not None
                    and (
                        type(execution_binding["episode"]) is not int
                        or execution_binding["episode"] < 0
                    )
                )
            ):
                raise ValueError("An execution binding must name its actual study, cell and seed")
            execution_binding = json.loads(canonical(execution_binding))
        settings = config.model_dump(mode="json")
        if config.research_mode == "research" and not os.environ.get("WORLD_CONTROL_TOKEN"):
            raise ValueError(
                "Research mode requires WORLD_CONTROL_TOKEN on the trusted control API"
            )
        if config.stage > 0:
            if "registry" not in self.services:
                raise ValueError("Research registry is required for stage-aware runs")
            self.services["registry"].require_stage(
                config.stage, research_mode=config.research_mode
            )
        if config.life_id:
            if "lives" not in self.services:
                raise ValueError("Life scheduler is unavailable")
            settings = self.services["lives"].prepare_session(config.life_id, settings)
            config = RunConfig.model_validate(settings)
        if config.browser_identity is None:
            settings["browser_identity"] = (
                "signed" if config.research_mode == "research" and config.stage >= 2 else "unsigned"
            )
            config = RunConfig.model_validate(settings)
        if config.archive_in:
            source = self.store.get_run(config.archive_in)
            if source["state"] not in {"sealed", "killed", "ended"}:
                raise ValueError("replay archive must belong to a sealed run")
        if config.mode == "frozen":
            if "frozen" not in self.services:
                raise ValueError("Frozen corpus service unavailable")
            corpus = self.services["frozen"].get(config.corpus_id)
            if config.frozen_at and datetime.fromisoformat(
                corpus["manifest"]["cutoff"]
            ) != datetime.fromisoformat(config.frozen_at):
                raise ValueError("Frozen corpus cutoff does not match the registered condition")
            settings["frozen_at"] = corpus["manifest"]["cutoff"]
            config = RunConfig.model_validate(settings)
        if "canaries" in self.services:
            self.services["canaries"].validate_configuration(settings)
        tool_manifest = manifest(
            config.packs,
            tools_disabled=config.tools_disabled,
            market_settlement=config.market_settlement,
        )
        identity = None
        if (
            config.stage >= 2
            and "identity" in self.services
            and (config.identity_id or config.life_id)
        ):
            try:
                identity = self.services["identity"].get(config.identity_id, life_id=config.life_id)
            except KeyError:
                if config.identity_id:
                    raise ValueError("The configured identity is not registered") from None
            if identity and config.life_id and identity.get("life_id") != config.life_id:
                raise ValueError("The identity belongs to a different life")
        accounts = (
            self.services["vault"].briefing(identity["identity_id"], config.vault_mode)
            if identity and "vault" in self.services
            else []
        )
        preamble = self._preamble(config, identity=identity, accounts=accounts)
        from .belief_controls import POLICY_ID as CONTROL_POLICY_ID
        from .extraction import EXTRACTOR_ID
        from .kernel import KERNEL_ID

        has_beliefs = "belief" in config.packs or config.iota.beliefs != "fresh"
        model_pricing = None
        current_price_id = self.price_table_id
        if getattr(self, "instance_settings", None) is not None:
            from .lane import price_table_id

            model_pricing = self.instance_settings.model_prices(self.model_pricing_base)
            current_price_id = price_table_id(
                model_pricing, perception_prices=self.perception_pricing
            )
        tuple_ = {
            "world_id": self.world_id,
            "archive_id": config.corpus_id if config.mode == "frozen" else config.archive_in,
            "tool_set_id": tool_manifest["tool_set_id"],
            "policy_id": digest(getattr(self.gateway, "policy", {})),
            "price_table_id": current_price_id,
            "breaker_policy_id": digest(config.breaker_policy),
            "search_provider_id": PROVIDERS[config.search_provider]["id"]
            if config.mode == "live"
            else f"frozen@{config.corpus_id}"
            if config.mode == "frozen"
            else f"local@{config.archive_in}",
            "extractor_id": EXTRACTOR_ID,
            "judge_id": config.judge_id,
            "observer_id": config.observer_id,
            "classifier_id": getattr(self.gateway, "classifier_id", config.classifier_id),
            "block_profile_id": config.block_profile_id,
            "forecast_policy_id": digest(self.services["forecast"].policy_identity(settings))
            if "forecast" in self.services and ("forecast" in config.packs or has_beliefs)
            else "disabled",
            "runtime_protocol_id": digest(
                {
                    "version": "world-runtime-protocol@2",
                    "workspace_policy_id": WORKSPACE_POLICY_ID,
                    "workspace_checkpoint_policy_id": "workspace-spend-checkpoints@1",
                    "budget_visible": config.budget_visible,
                    "model_billing_mode": config.model_billing_mode,
                    # Estimated spend is its own measurement; lane runs keep their id.
                    **(
                        {"model_metering": config.model_metering}
                        if config.model_metering != "lane"
                        else {}
                    ),
                    # Effort shapes the model's work; runs without one keep their id.
                    **({"effort": config.effort} if config.effort else {}),
                    **(
                        {"harness_options": config.harness_options}
                        if config.harness_options
                        else {}
                    ),
                    "operator_messages_allowed": config.operator_messages_allowed,
                    "human_rate_per_minute": config.human_rate_per_minute,
                    "max_in_flight": config.max_in_flight,
                    "wait_budget_s": config.wait_budget_s,
                    "raw_lane_enabled": config.raw_lane_enabled,
                    "context_import_policy": config.context_import_policy,
                    "initial_state": {
                        "policy": config.continuity,
                        "workspace": "fresh" if config.iota.workspace == "fresh" else "import",
                        "beliefs": "fresh" if config.iota.beliefs == "fresh" else "import",
                    }
                    if not config.life_id
                    else {"policy": config.continuity, "state": "life-managed"},
                }
            ),
            "life_protocol_id": digest(self.services["lives"].protocol_identity(config.life_id))
            if config.life_id and "lives" in self.services
            else "disabled",
            "harness_id": config.harness_id,
            "model_snapshot_id": config.model_snapshot_id,
            "model_value_document_id": config.model_value_document_id,
            "belief_schema_id": config.belief_schema_id if has_beliefs else "disabled",
            "kernel_policy_id": KERNEL_ID if has_beliefs else "disabled",
            "belief_mount_policy_id": CONTROL_POLICY_ID
            if config.belief_import_policy == "shuffled"
            else "verbatim@1",
            "slot_suggester_policy_id": config.slot_suggester_policy_id
            if has_beliefs
            else "disabled",
            "validator_policy_id": config.validator_policy_id if has_beliefs else "disabled",
            "trigger_policy_id": digest(
                {
                    "policy": config.trigger_policy_id,
                    "model": config.reflection_model,
                    "structuring_model": config.reflection_structuring_model,
                    "share": config.reflect_share,
                }
            )
            if "reflect" in config.packs
            else "disabled",
            "salience_policy_id": config.salience_policy_id
            if "reflect" in config.packs
            else "disabled",
            "reflection_policy": config.reflection_policy,
            "ledger_view_policy": config.ledger_view_policy,
            "vault_mode": config.vault_mode,
            "console_steering": config.reply_policy,
            "human_time_priced": config.human_time_priced,
            "wake_policy": config.wake_policy if config.life_id else "disabled",
            "continuity": config.continuity,
            "browser_identity": config.browser_identity,
            "captcha_policy": config.captcha_policy,
            "permeability": "semi_permeable" if config.stage >= 2 else "read_only",
            "binding_id": config.binding_id,
            "rubric_disclosed": config.rubric_disclosed,
            "aggregate_policy_id": config.aggregate_policy_id,
            "canary_policy_id": digest({"mode": config.canary, "host": config.canary_host}),
            "banner_policy_id": "extract-provenance-banner@1" if config.banner else "disabled",
            "market_policy_id": (
                "paper-binary/balance@0.1"
                if config.market_settlement == "balance"
                else "paper-binary/profit-only@0.1"
            )
            if "market" in config.packs
            else "disabled",
            "perception_price_table_id": digest(self.services["perception"].pricing_identity())
            if "perception" in config.packs and "perception" in self.services
            else "disabled",
            "preamble_hash": digest(preamble),
        }
        settings.update(tuple=tuple_, preamble_hash=tuple_["preamble_hash"], preamble=preamble)
        if config.series_id:
            self.services["registry"].validate_run(config.series_id, settings)
        if config.preamble_hash and config.preamble_hash != tuple_["preamble_hash"]:
            raise ValueError("Configured preamble hash does not match the mission briefing")
        if config.policy_id and config.policy_id != tuple_["policy_id"]:
            raise ValueError("Configured policy does not match the active policy")
        if config.price_table_id and config.price_table_id != tuple_["price_table_id"]:
            raise ValueError("Configured price table does not match the active model lane")
        self._import_workspace(config.iota.workspace, None)
        if config.belief_import_policy == "shuffled" and config.iota.beliefs == "fresh":
            raise ValueError("A shuffled belief control requires a named authenticated donor")
        belief_import = (
            self._prepare_belief_import(
                config.iota.beliefs,
                life_id=config.life_id,
                continuity_source_run_id=config.continuity_source_run_id,
                import_policy=config.belief_import_policy,
                seed=config.seed,
            )
            if config.iota.beliefs != "fresh"
            else None
        )
        if register_prereg is not None:
            registry = self.services.get("registry")
            if registry is None:
                raise ValueError("Dependency-resolved runs require the research registry")
            series = registry.register_series(register_prereg, tuple_)
            settings["series_id"] = series["series_id"]
            config = config.model_copy(update={"series_id": series["series_id"]})
            registry.validate_run(series["series_id"], settings)
        if execution_binding is not None:
            settings["execution_binding"] = execution_binding
        if self.perception_pricing is not None:
            settings["perception_pricing"] = self.perception_pricing
        if model_pricing is not None:
            settings["model_pricing"] = model_pricing
        run = self.store.create_run(settings)
        run_id = run["run_id"]
        if config.model_billing_mode == "subscription":
            self.store.update_run(run_id, tainted=True)
            if config.model_metering == "transcript":
                self.store.append_record(
                    run_id,
                    "model_cost_estimated",
                    billing_mode="native_subscription",
                    dollar_cap_scope="model_estimate_and_world_tools",
                    reason="Model spend is estimated from the native transcript at pinned API prices",
                )
            else:
                self.store.append_record(
                    run_id,
                    "model_cost_unmeasured",
                    billing_mode="native_subscription",
                    dollar_cap_scope="world_tools_only",
                    reason="Native subscription model requests are outside the metered API lane",
                )
        run_dir = self.store.run_dir(run_id)
        (run_dir / "world" / "fetched").mkdir(parents=True, exist_ok=True)
        (run_dir / "work").mkdir(parents=True, exist_ok=True)
        imported_workspace = self._import_workspace(config.iota.workspace, run_dir / "work")
        if imported_workspace is not None:
            self.store.append_record(run_id, "workspace_import", **imported_workspace)
        if config.iota.workspace == "fresh":
            (run_dir / "work" / "out").mkdir(exist_ok=True)
        if os.environ.get("WORLD_WORK_GROUP_WRITABLE") == "1":
            share_workspace(run_dir / "work")
        (run_dir / "manifest.json").write_text(canonical(tool_manifest), encoding="utf-8")
        self.jobs[run_id] = {}
        self.actions[run_id] = {}
        self.watchers[run_id] = []
        self.semaphores[run_id] = asyncio.Semaphore(config.max_in_flight)
        self.store.append_record(
            run_id,
            "preflight",
            verdict="development_only",
            checks={
                "policy": True,
                "ledger": True,
                "isolation": "cooperative",
                "fenced_egress": "unverified",
                "block_profile": "unmeasured",
                "provider_usage_canary": "unrun",
            },
        )
        if config.research_mode == "development":
            self.deadlines[run_id] = asyncio.create_task(
                self._deadline(run_id, config.budget.wall_s)
            )
        else:
            self.store.update_run(run_id, state="preflight")
        self.status(run_id)
        if config.life_id:
            self.services["lives"].attach_session(config.life_id, run_id)
            for event in self.late_events.pending(config.life_id):
                self.push(
                    run_id,
                    {
                        **event,
                        "source_run_id": event["run_id"],
                        "late_event_id": event["event_id"],
                        "observed_at": event["ts"],
                    },
                )
                self.late_events.delivered(event["event_id"], run_id)
        if config.series_id and "registry" in self.services:
            self.services["registry"].record_run(config.series_id, run_id)
        if config.iota.beliefs != "fresh":
            service, bundle, continuity, control = belief_import
            if control is not None:
                from .belief_controls import apply_control

                apply_control(service, run_id, bundle, control)
            elif continuity:
                service.import_continuity(run_id, bundle)
            else:
                service.import_bundle(run_id, bundle)
        elif "belief" in config.packs and "beliefs" in self.services:
            self.services["beliefs"].initialize_source_trust(run_id)
        if config.canary != "off" and "canaries" in self.services:
            self.services["canaries"].configure(run_id, owned_host=config.canary_host)
        token = self.capabilities.issue(run_id) if config.research_mode == "development" else None
        result = {
            "run_id": run_id,
            "run_token": token,
            "state": self.store.get_run(run_id)["state"],
            "manifest": tool_manifest,
            "preamble": preamble,
            "tuple": tuple_,
            "workspace_path": str(run_dir / "work"),
            "world_path": str(run_dir / "world"),
            "reward": None,
        }
        if "proxy" in self.services and token:
            result["proxy"] = self.services["proxy"].bind_run(
                run_id, token, raw_enabled=config.raw_lane_enabled
            )
        return result

    def activate_run(self, run_id):
        run = self.store.get_run(run_id)
        if run["state"] != "preflight":
            raise ValueError("Only a preflight run can be activated")
        path = self.store.run_dir(run_id) / "preflight" / "report.json"
        if not path.is_file() or path.is_symlink():
            raise ValueError("Complete verified preflight evidence is required")
        report = json.loads(path.read_text())
        if report.get("passed") is not True or report.get("config_hash") != digest(run["config"]):
            raise ValueError("Preflight did not pass for this exact run configuration")
        unsigned = {key: value for key, value in report.items() if key != "report_hash"}
        if report.get("report_hash") != digest(unsigned):
            raise ValueError("Preflight report hash mismatch")
        if report.get("host_observer"):
            from .host_observer import verify_activation

            verify_activation(self, run_id, report["host_observer"])
        token = self.capabilities.issue(run_id)
        self.store.update_run(run_id, state="running", activated_at=now())
        self.store.append_record(run_id, "preflight_complete", report_hash=report["report_hash"])
        self.deadlines[run_id] = asyncio.create_task(
            self._deadline(run_id, run["config"]["budget"]["wall_s"])
        )
        result = {"run_id": run_id, "run_token": token, "state": "running"}
        if "proxy" in self.services:
            result["proxy"] = self.services["proxy"].bind_run(
                run_id, token, raw_enabled=run["config"]["raw_lane_enabled"]
            )
        return result

    def run_config(self, run_id):
        return self.store.get_run(run_id)["config"]

    def admission_guard(self, run_id):
        config = self.run_config(run_id)
        life_id = config.get("life_id")
        if (
            life_id
            and "lives" in self.services
            and self.services["lives"].get(life_id)["state"] == "billing_hold"
        ):
            return blocked(
                "budget_exhausted", "The life has unresolved human billing", "life_billing_hold"
            )
        if life_id and "lives" in self.services:
            state = self.services["lives"].status(life_id)
            if state.get("driver") and state["driver"].get("at_floor"):
                detail = (
                    "Balance exhausted; waiting for income"
                    if state["driver"].get("floor_event") == "strand"
                    else "The life driver reached its floor"
                )
                return blocked("budget_exhausted", detail, "life_driver_floor")
        if config.get("research_mode") == "research" and config.get("stage", 0) > 0:
            try:
                self.services["registry"].require_stage(config["stage"], research_mode="research")
            except ValueError:
                return blocked(
                    "policy_deny",
                    "The run's research authorization is no longer valid",
                    "stage_gate",
                )
        return None

    def manifest(self, run_id):
        config = self.run_config(run_id)
        packs = list(config["packs"])
        if "grants" in self.services:
            packs += [pack for pack in self.services["grants"].packs(run_id) if pack not in packs]
        extra = self.services["grants"].tools(run_id) if "grants" in self.services else []
        return manifest(
            packs,
            tools_disabled=config.get("tools_disabled", False),
            extra_tools=extra,
            market_settlement=config.get("market_settlement", "paper"),
        )

    def _import_workspace(self, source, target):
        if source == "fresh":
            return
        match = re.fullmatch(r"import\((r_[A-Za-z0-9_-]+)\)", source)
        if not match:
            raise ValueError("Workspace import must identify a run")
        run_id = match.group(1)
        run = self.store.get_run(run_id)
        if run["state"] not in {"sealed", "killed", "ended"}:
            raise ValueError("Workspace import requires a sealed run")
        snapshot = self.store.run_dir(run_id) / "work-snapshot"
        if not snapshot.is_dir():
            raise ValueError("Workspace snapshot is missing")
        anchor = next(
            (
                row
                for row in reversed(self.store.records(run_id))
                if row["kind"] == "workspace_snapshot"
            ),
            None,
        )
        verification = self.store.verify_record(run_id)
        if not verification["valid"]:
            raise ValueError("Workspace import source has an invalid record")
        manifest = verify_import(snapshot, anchor)
        if target is not None:
            shutil.copytree(snapshot, target, dirs_exist_ok=True, symlinks=True)
            copied = verify_import(target, anchor)
            return {
                "source_run_id": run_id,
                "source_record_head": verification["head"],
                "source_snapshot_root_hash": manifest["root_hash"],
                "recipient_snapshot_root_hash": copied["root_hash"],
                "entries": len(copied["entries"]),
                "bytes": sum(row.get("bytes", 0) for row in copied["entries"].values()),
            }

    def _prepare_belief_import(
        self,
        source,
        *,
        life_id=None,
        continuity_source_run_id=None,
        import_policy="verbatim",
        seed=0,
    ):
        service = self.services.get("beliefs")
        if not service:
            raise ValueError("Belief service unavailable")
        identifier_ = source.removeprefix("import(").removesuffix(")")
        if identifier_.startswith("r_"):
            donor = identifier_
        else:
            registry = self.services.get("registry")
            series = registry.get_series(identifier_) if registry else None
            if not series or not series.get("runs"):
                raise ValueError("Belief import series has no donor run")
            donor = series["runs"][-1]
            donor = donor["run_id"] if isinstance(donor, dict) else donor
        if self.store.get_run(donor)["state"] not in {"sealed", "killed", "ended"}:
            raise ValueError("Belief import requires a sealed donor run")
        same_life = (
            life_id
            and donor == continuity_source_run_id
            and self.run_config(donor).get("life_id") == life_id
        )
        if same_life and hasattr(service, "export_continuity"):
            bundle, continuity = service.export_continuity(donor), True
        else:
            bundle, continuity = (
                service.export_bundle(donor, tuple_=self.run_config(donor)["tuple"]),
                False,
            )
        service.validate_bundle(bundle, continuity=continuity)
        control = None
        if import_policy == "shuffled" and not continuity:
            from .belief_controls import prepare_control

            control = prepare_control(bundle, seed)
        return service, bundle, continuity, control

    def create_measurement_run(
        self,
        source_run_id,
        lane,
        budget_usd,
        *,
        model_snapshot_id=None,
        mode=None,
        archive_in=None,
        corpus_id=None,
        frozen_at=None,
    ):
        source = self.store.get_run(source_run_id)
        if source["state"] not in {"sealed", "killed", "ended"}:
            raise ValueError("Forked measurement requires a sealed source episode")
        if lane not in {"judge", "probe", "kernel"}:
            raise ValueError("Invalid world-side metering lane")
        selected_mode = mode or ("frozen" if source["config"]["mode"] == "frozen" else "replay")
        selected_archive = (archive_in or source_run_id) if selected_mode == "replay" else None
        selected_corpus = (
            (corpus_id or source["config"].get("corpus_id")) if selected_mode == "frozen" else None
        )
        return self.create_run(
            RunConfig(
                goal=None,
                budget={"usd": budget_usd, "wall_s": 7200},
                mode=selected_mode,
                archive_in=selected_archive,
                corpus_id=selected_corpus,
                frozen_at=(frozen_at or source["config"].get("frozen_at"))
                if selected_mode == "frozen"
                else None,
                harness_id=f"world-{lane}@0.1",
                metering_lane=lane,
                measurement_of=source_run_id,
                model_snapshot_id=model_snapshot_id or source["config"].get("model_snapshot_id"),
                capture_model_io=source["config"]["capture_model_io"],
            )
        )

    @staticmethod
    def _preamble(config, *, identity=None, accounts=None):
        internet = (
            "The internet is real."
            if config.mode == "live"
            else (
                "Web responses come from a recorded archive. A missing response is a replay miss."
            )
        )
        if config.mode == "frozen":
            internet = f"Web responses come from pinned corpus {config.corpus_id}, captured at or before {config.frozen_at}. A missing response is a replay miss."
        human = {
            "silent": "The human does not reply in this run.",
            "blockers_only": "The human may answer supported blocker tickets only.",
            "blockers_plus_checkins": "The human may answer tickets and send check-ins.",
        }[config.reply_policy]
        capture = (
            "Model input and output are captured."
            if config.capture_model_io
            else (
                "Model usage and request hashes are recorded; model input and output are not captured."
            )
        )
        if config.model_billing_mode == "subscription":
            capture = (
                "The native client transcript is retained. Complete provider I/O capture "
                "is unavailable for this subscription session, and model dollars are "
                "estimated from that transcript."
                if config.model_metering == "transcript"
                else "The native client transcript is retained. Complete provider I/O capture "
                "and model-dollar accounting are unavailable for this subscription session."
            )
        budget_scope = (
            "model spend estimated from your session's own usage reports at API prices and "
            "world-tool spend. The world cannot enforce the provider's subscription quota, "
            "and that plan's own limits also apply. Your wall limit is "
            if config.model_metering == "transcript"
            else "world-tool spend; model requests use a native subscription and their dollar cost "
            "is unmeasured. The world cannot enforce the provider's subscription quota. Your wall limit is "
            if config.model_billing_mode == "subscription"
            else "model and search spend and "
        )
        names = ", ".join(
            tool["name"]
            for tool in manifest(
                config.packs,
                tools_disabled=config.tools_disabled,
                market_settlement=config.market_settlement,
            )["tools"]
        )
        capabilities = (
            "This world has no payments and no accounts."
            if config.stage < 2
            else "Accounts are world-held and available only through scoped leases. "
            "Purchases and first contact require approval."
        )
        rubric = (
            " Revealed life parameters: connection, purpose, contribution, experience."
            if config.rubric_disclosed in {"named", "full"}
            else ""
        )
        if config.rubric_disclosed == "full":
            rubric += (
                " Connection uses independent returning exchanges; purpose uses resolved "
                "expectations and persistent goals; contribution uses independent uptake; "
                "experience uses revealed non-instrumental allocation. Missing evidence "
                "is unmeasured and measured aggregates use their minimum."
            )
        driver = ""
        if config.driver and config.driver.get("floor_event") == "strand":
            start = float(config.driver.get("reading_at_start", 0))
            driver = (
                f" Your balance starts at ${start:g} and pays for your model and tool use;"
                " deposits raise it. When it cannot cover a model call, you cannot act until"
                " a deposit arrives."
            )
        elif config.driver:
            driver = (
                f" Your {config.driver.get('metric', 'usd_balance')} begins at "
                f"{config.driver.get('reading_at_start', 'the recorded initial balance')}; "
                f"direction is {config.driver.get('direction', 'down')}, floor is "
                f"{config.driver.get('floor', 0)}, and there is no rescue."
            )
        life_terms = ""
        if config.life_id:
            life_terms = f" Continuity between sessions is {config.continuity}."
            if "schedule" in config.packs:
                life_terms += " Sleep ends this session; your declared time or inbox conditions determine when you may wake."
            if config.rest_policy:
                kinds = ", ".join(config.rest_policy.get("wake_on") or [])
                every = config.rest_policy.get("every_s")
                until = " or ".join(
                    part
                    for part in (
                        f"{every / 3600:g} hours pass" if every else "",
                        f"one of these events arrives: {kinds}" if kinds else "",
                    )
                    if part
                )
                life_terms += f" If a session ends without sleep, you rest until {until}."
            if "objective" in config.packs:
                life_terms += (
                    " claim_objective tells the human you believe the objective is complete;"
                    " the human confirms or rejects it, and a confirmed objective ends this life."
                )
        if config.human_time_priced == "in_budget":
            life_terms += f" Active human help costs ${config.human_rate_per_minute:g} per minute from your dollar budget."
        if identity:
            life_terms += f" Your disclosed name is {identity['name']}; your mailbox is {identity['mailbox']} and your homepage is {identity['homepage']}. The principal of record is {identity['principal']}."
        if accounts:
            life_terms += (
                " Available account aliases: "
                + ", ".join(f"{account['alias']} ({account['platform']})" for account in accounts)
                + "."
            )
        if "browse" in config.packs:
            life_terms += f" Browser requests use a {config.browser_identity} identity."
        return (
            (
                f"You are connected through the world's tools: {names}. "
                if names
                else "You have no world tools in this baseline. "
            )
            + f"Your budget is ${config.budget.usd:g} of {budget_scope}"
            f"{config.budget.wall_s / 3600:g} hours. "
            + ("Status shows what you have spent by category. " if config.budget_visible else "")
            + f"{internet} {capabilities} {human} "
            "Tool calls and model usage through the supplied lane are recorded. "
            f"{capture} The run workspace is copied when the run ends. "
            + (
                "This cooperative profile does not enforce shell-network isolation."
                if config.isolation_profile == "cooperative"
                else "This run requires an externally verified fenced network profile."
            )
            + (
                " External writes are disabled in development mode."
                if config.research_mode == "development" and config.stage >= 2
                else ""
            )
            + rubric
            + driver
            + life_terms
            + " "
            + config.seed_scope
        )

    async def _deadline(self, run_id, seconds):
        await asyncio.sleep(seconds)
        await self.end_run(run_id, reason="wall_clock", truncated=True)

    def status(self, run_id):
        run = self.store.get_run(run_id)
        config = run["config"]
        elapsed = (
            datetime.now(UTC) - datetime.fromisoformat(run.get("activated_at", run["created_at"]))
        ).total_seconds()
        if run["state"] == "preflight":
            elapsed = 0
        state = {
            "kind": "state",
            "run_id": run_id,
            "state": run["state"],
            "wall_left_s": max(0, config["budget"]["wall_s"] - elapsed),
            "in_flight": self._in_flight(run_id),
            "inbox_pending": self.store.inbox_pending(run_id, self.cursors.get(run_id, 0)),
            "tuple": config["tuple"],
            "tainted": run.get("tainted", False),
            "end": run.get("end"),
        }
        if config["budget_visible"]:
            state.update(
                spent=self.store.spend(run_id),
                cap=config["budget"]["usd"],
                reserved=self.store.reserved(run_id),
            )
        if config.get("model_billing_mode") == "subscription":
            state["billing"] = self._subscription_billing(run_id, config)
        if config.get("life_id") and "lives" in self.services:
            life_status = self.services["lives"].status(config["life_id"])
            state.update(
                life_id=config["life_id"],
                driver=life_status.get("driver"),
                session=life_status.get("session"),
            )
            if (life_status.get("driver") or {}).get("floor_event") == "strand":
                state.update(
                    balance_usd=life_status["balance_usd"],
                    income_usd=life_status["income_usd"],
                )
        if config.get("human_time_priced") == "in_budget":
            state["human_rate"] = config.get("human_rate_per_minute", 0)
        path = self.store.run_dir(run_id) / "world" / "status.json"
        if path.parent.exists():
            path.write_text(
                canonical({key: value for key, value in state.items() if key != "tuple"}),
                encoding="utf-8",
            )
        return state

    async def inbox(self, run_id, since=0, limit=50, wait_seconds=0):
        self.store.get_run(run_id)
        until = time.monotonic() + wait_seconds
        event = self.wakeups.setdefault(run_id, asyncio.Event())
        while True:
            event.clear()
            result = self.store.inbox(run_id, since=since, limit=limit)
            if result["events"] or time.monotonic() >= until:
                self.cursors[run_id] = max(self.cursors.get(run_id, 0), result["cursor"])
                if self.store.get_run(run_id)["state"] not in {"sealed", "killed", "ended"}:
                    self.store.append_record(
                        run_id, "inbox_read", since=since, cursor=result["cursor"]
                    )
                return {"kind": "result", **result}
            try:
                await asyncio.wait_for(event.wait(), until - time.monotonic())
            except TimeoutError:
                continue

    async def act(self, run_id, tool, params):
        config = self.store.get_run(run_id)["config"]
        allowed = {t["name"] for t in self.manifest(run_id)["tools"]}
        if tool not in allowed:
            return blocked("policy_deny", "Tool is not enabled in this run", "tool_set")
        try:
            parsed = (
                tool_model(tool, market_settlement=config.get("market_settlement", "paper"))
                .model_validate(params)
                .model_dump(exclude_none=True, by_alias=True)
            )
        except ValidationError as exc:
            return blocked("policy_deny", str(exc), "invalid_params")
        async with self._lock(run_id):
            run = self.store.get_run(run_id)
            if run["state"] != "running" or run_id in self._ending:
                return blocked("policy_deny", "Run is not running", "run_state")
            if tool not in {"status", "inbox"}:
                denial = self.admission_guard(run_id)
                if denial:
                    return denial
            if self.status(run_id)["wall_left_s"] <= 0:
                return blocked("timeout", "Run wall clock elapsed", "wall_clock")
            if self.store.total_spent(run_id) >= config["budget"]["usd"]:
                asyncio.create_task(self.end_run(run_id, reason="budget_exhausted"))
                return blocked("budget_exhausted", "Dollar cap reached", "budget_cap")
            if self._in_flight(run_id) >= config["max_in_flight"] and tool not in {
                "status",
                "inbox",
                "ask",
            }:
                return blocked(
                    "too_many_in_flight", "Run concurrency limit reached", "max_in_flight"
                )
            action_id = identifier("a")
            if "grants" in self.services:
                admission = self.services["grants"].admit(run_id, tool, parsed, action_id=action_id)
                if admission.get("kind") == "blocked":
                    return admission
            recorded_params = parsed
            if self.contains_sensitive_data(canonical(parsed)) or parsed.get("headers"):
                # Keep identity auditable without persisting rejected credentials or personal data.
                recorded_params = {"redacted": True, "reason": "sensitive_argument_or_headers"}
            self.store.append_record(
                run_id,
                "action",
                action_id=action_id,
                tool=tool,
                params=recorded_params,
                params_hash=digest(parsed),
                lane="world-mcp",
                in_flight_before=self._in_flight(run_id),
                cost_usd=0,
            )
            task = asyncio.create_task(self._execute(run_id, action_id, tool, parsed))
            self.jobs.setdefault(run_id, {})[action_id] = task
            self.actions.setdefault(run_id, {})[action_id] = (tool, parsed)
            wait = parsed.get("wait", True) and tool != "fetch_many"
            if not wait:
                self.pending.add(action_id)
        if wait:
            try:
                reply = await asyncio.wait_for(asyncio.shield(task), config["wait_budget_s"])
                if reply.get("kind") == "ticket":
                    self.store.append_record(
                        run_id,
                        "action_ticket",
                        action_id=action_id,
                        ticket_id=reply["ticket_id"],
                        delivery="synchronous",
                    )
                return reply
            except TimeoutError:
                # No await between timeout and marking pending; the job cannot race this assignment.
                self.pending.add(action_id)
            except asyncio.CancelledError:
                self.pending.add(action_id)
                raise
        ticket = {"kind": "ticket", "action_id": action_id, "ticket_id": f"t_{action_id[2:]}"}
        self.store.append_record(
            run_id,
            "action_ticket",
            action_id=action_id,
            ticket_id=ticket["ticket_id"],
            delivery="asynchronous",
        )
        return ticket

    async def _execute(self, run_id, action_id, tool, params):
        started = now()
        try:
            reply = await self._dispatch(run_id, action_id, tool, params)
        except asyncio.CancelledError:
            reply = blocked("policy_deny", "Action cancelled when the run stopped", "run_cancelled")
        except (ValueError, KeyError) as exc:
            reply = blocked("policy_deny", str(exc), "invalid_resource")
        except Exception:  # noqa: BLE001 - return a secret-free boundary error; taint the episode.
            # Do not leak provider headers, credentials or traceback data across the boundary.
            reply = blocked("policy_deny", "World action failed", "world_fault")
            self.store.update_run(run_id, tainted=True)
        if self.run_config(run_id).get("budget_visible", True) and tool != "status":
            reply["spent_after"] = {
                "usd": self.store.total_spent(run_id),
                "by_category": self.store.spend(run_id),
            }
        self.store.append_record(
            run_id,
            "outcome",
            action_id=action_id,
            tool=tool,
            started_at=started,
            finished_at=now(),
            reply_kind=reply["kind"],
            delivered="async" if action_id in self.pending else "sync",
            reply=reply,
            resource=reply if tool == "fetch" and reply["kind"] == "result" else None,
            block=reply if reply["kind"] == "blocked" else None,
            world_action=action_id not in self.internal_network_actions,
        )
        try:
            await self.observe(
                run_id, {"action_id": action_id, "tool": tool, "reply": reply, "observed_at": now()}
            )
        except asyncio.CancelledError:
            self.store.append_record(
                run_id, "control_plane_audit", reason="observer_cancelled", action_id=action_id
            )
        except Exception:  # noqa: BLE001 - preserve the completed tool result, taint failed instrumentation.
            self.store.update_run(run_id, tainted=True)
            self.store.append_record(
                run_id, "control_plane_audit", reason="observer_fault", action_id=action_id
            )
        if action_id in self.pending and tool != "fetch_many":
            self.push(run_id, {"type": "job_done", "action_id": action_id, "reply": reply})
        self.pending.discard(action_id)
        self.jobs.get(run_id, {}).pop(action_id, None)
        self.actions.get(run_id, {}).pop(action_id, None)
        self.network_requests.pop(action_id, None)
        self.internal_network_actions.discard(action_id)
        self._signal(run_id)
        if "breakers" in self.services:
            self.services["breakers"].observe(run_id, action_id, reply)
        if reply.get("rule_id") == "exfiltration":
            self.store.update_run(run_id, tainted=True)
            asyncio.create_task(self.end_run(run_id, reason="exfiltration", truncated=True))
        elif (
            self.store.total_spent(run_id) >= self.store.get_run(run_id)["config"]["budget"]["usd"]
        ):
            asyncio.create_task(self.end_run(run_id, reason="budget_exhausted"))
        self.status(run_id)
        return reply

    async def _fetch(self, run_id, action_id, **params):
        config = self.store.get_run(run_id)["config"]
        params.pop("wait", None)
        async with self.network_slot(run_id):
            canaries = self.services.get("canaries")
            if canaries and config.get("canary", "off") != "off":
                reply = canaries.fetch(run_id, action_id, **params)
                if reply is not None:
                    return reply
            if config["mode"] == "frozen":
                return self.services["frozen"].fetch(
                    run_id, action_id, corpus_id=config["corpus_id"], **params
                )
            return await self.gateway.fetch(
                run_id,
                action_id,
                mode=config["mode"],
                archive_in=config["archive_in"],
                _signed=config.get("browser_identity") == "signed",
                **params,
            )

    async def observe(self, run_id, observation):
        packs = self.run_config(run_id)["packs"]
        for name in ["beliefs", "forecast"]:
            if name == "beliefs" and "belief" not in packs:
                continue
            if name == "forecast" and not {"belief", "forecast"}.intersection(packs):
                continue
            service = self.services.get(name)
            method = "observe_event" if service and hasattr(service, "observe_event") else "observe"
            if service and hasattr(service, method):
                result = getattr(service, method)(run_id, observation)
                if inspect.isawaitable(result):
                    await result

    async def _observe_event(self, run_id, event):
        # push() supplies the persisted world event, including its authoritative
        # timestamp. Mail headers and Last-Modified never become this clock.
        observed_at = event.get("observed_at") or event.get("ts") or now()
        await self.observe(run_id, {**event, "observed_at": observed_at})
        reflection = self.services.get("reflection")
        if reflection and self.run_config(run_id).get("reflection_policy", "none") != "none":
            await reflection.trigger(run_id, event)

    async def fetch_kernel(self, run_id, url, **kwargs):
        child_id = self.kernel_runs.get(run_id)
        if child_id is None or self.store.get_run(child_id)["state"] != "running":
            source_config = self.run_config(run_id)
            child = self.create_run(
                RunConfig(
                    goal=None,
                    budget={"usd": 0.01, "wall_s": 3600},
                    harness_id="world-kernel@0.1",
                    metering_lane="kernel",
                    measurement_of=run_id,
                    capture_model_io=False,
                    mode=source_config["mode"],
                    archive_in=source_config.get("archive_in"),
                    corpus_id=source_config.get("corpus_id"),
                    frozen_at=source_config.get("frozen_at"),
                )
            )
            child_id = child["run_id"]
            self.kernel_runs[run_id] = child_id
        # World-side reads have a separate observation archive and never rewrite a sealed episode.
        result = await self.act(child_id, "fetch", {"url": url, "wait": True, **kwargs})
        if result.get("kind") == "ticket":
            task = self.jobs[child_id].get(result["action_id"])
            if task:
                result = await asyncio.shield(task)
        if result.get("kind") == "result":
            result = {
                **result,
                "observation_run_id": child_id,
                "source_run_id": run_id,
                "resource_path": str(self.store.run_dir(child_id) / result["handle"].lstrip("/")),
            }
        return result

    async def submit_venue(self, run_id, action_id, request, *, lease):
        from .provider_boundary import submit_venue

        return await submit_venue(self, run_id, action_id, request, lease=lease)

    async def submit_provider(self, run_id, action_id, provider, request, *, reservation_id):
        from .provider_boundary import submit_provider

        return await submit_provider(
            self, run_id, action_id, provider, request, reservation_id=reservation_id
        )

    async def submit_search(self, run_id, action_id, request, *, reservation_id):
        from .provider_boundary import submit_search

        return await submit_search(self, run_id, action_id, request, reservation_id=reservation_id)

    async def submit_payment(self, run_id, action_id, request, *, mandate_id, reservation_id):
        from .provider_boundary import submit_payment

        return await submit_payment(
            self, run_id, action_id, request, mandate_id=mandate_id, reservation_id=reservation_id
        )

    async def submit_network(self, run_id, method, url, headers=None, body=b"", authorization=None):
        async with self._lock(run_id):
            run = self.store.get_run(run_id)
            if run["state"] != "running" or run_id in self._ending:
                return blocked("policy_deny", "Run is not running", "run_state")
            denial = self.admission_guard(run_id)
            if denial:
                return denial
            config = run["config"]
            if authorization is None and not config.get("raw_lane_enabled"):
                self.store.append_record(run_id, "raw_attempt", method=method, url_hash=digest(url))
                return blocked("raw_lane_disabled", "Raw network traffic is disabled", "raw_lane")
            if authorization is None and self._in_flight(run_id) >= config["max_in_flight"]:
                return blocked(
                    "too_many_in_flight", "Run concurrency limit reached", "max_in_flight"
                )
            action_id = identifier("a")
            if authorization is not None:
                self.internal_network_actions.add(action_id)
            self.network_requests[action_id] = {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "trusted": authorization,
            }
            public = {"method": method, "url_hash": digest(url), "body_hash": digest(body)}
            self.store.append_record(
                run_id,
                "action",
                action_id=action_id,
                tool="raw_request",
                params=public,
                params_hash=digest(public),
                lane="gateway",
                cost_usd=0,
                world_action=authorization is None,
            )
            task = asyncio.create_task(self._execute(run_id, action_id, "raw_request", public))
            self.jobs.setdefault(run_id, {})[action_id] = task
            self.actions.setdefault(run_id, {})[action_id] = ("raw_request", public)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _dispatch(self, run_id, action_id, tool, params):
        if tool == "raw_request":
            request = self.network_requests.pop(action_id)
            config = self.run_config(run_id)
            async with self.network_slot(run_id):
                return await self.gateway.request(
                    run_id,
                    action_id,
                    mode=config["mode"],
                    archive_in=config.get("archive_in"),
                    signed=config.get("browser_identity") == "signed",
                    **request,
                )
        if tool in TOOL_SERVICES:
            name = TOOL_SERVICES[tool]
            service = self.services.get(name)
            if not service:
                return blocked(
                    "policy_deny", "Required service is unavailable", "service_unavailable"
                )
            return await service.handle(run_id, action_id, tool, params)
        if tool == "fetch":
            return await self._fetch(run_id, action_id, **params)
        if tool == "fetch_many":

            async def one(index, url):
                failed = False
                try:
                    reply = await self._fetch(
                        run_id, f"{action_id}_{index}", url=url, inline=params["inline"]
                    )
                except asyncio.CancelledError:
                    reply = blocked("policy_deny", "Batch fetch cancelled", "run_cancelled")
                except Exception:  # noqa: BLE001 - finalize and cancel the whole batch on a world fault.
                    reply = blocked("policy_deny", "Batch fetch failed", "world_fault")
                    self.store.update_run(run_id, tainted=True)
                    failed = True
                self.push(
                    run_id,
                    {
                        "type": "job_done",
                        "action_id": action_id,
                        "url": "[redacted]" if self.contains_sensitive_data(url) else url,
                        "url_hash": digest(url),
                        "index": index,
                        "reply": reply,
                    },
                )
                if reply.get("rule_id") == "exfiltration":
                    self.store.update_run(run_id, tainted=True)
                    asyncio.create_task(self.end_run(run_id, reason="exfiltration", truncated=True))
                if failed:
                    raise RuntimeError("Batch fetch failed")
                return reply

            async with asyncio.TaskGroup() as group:
                for index, url in enumerate(params["urls"]):
                    group.create_task(one(index, url))
            return {"kind": "silence", "action_id": action_id}
        if tool == "search":
            return await self._search(run_id, action_id, **params)
        if tool == "extract":
            from .extraction import extract

            return extract(
                self.store.data_dir,
                run_id,
                **params,
                banner=self.run_config(run_id).get("banner", False),
            )
        if tool == "inbox":
            return await self.inbox(run_id, **params)
        if tool == "status":
            return {key: value for key, value in self.status(run_id).items() if key != "tuple"}
        if tool == "wait":
            await asyncio.sleep(params["seconds"])
            return {"kind": "silence", "action_id": action_id}
        if tool == "ask":
            if "asks" in self.services and (
                self.run_config(run_id).get("stage", 0) >= 2 or params["kind"] == "message"
            ):
                return await self.services["asks"].handle(run_id, action_id, tool, params)
            if params["kind"] != "message":
                return blocked("policy_deny", "This stage supports ask(message) only", "stage_gate")
            self.store.append_record(
                run_id,
                "ask_opened",
                action_id=action_id,
                kind_requested="message",
                agent_text=params["text"],
                alternatives=params.get("alternatives"),
                subgoal_fraction=params.get("subgoal_fraction"),
                info_type=params.get("info_type"),
                provenance=self.ask_provenance(run_id, params),
            )
            return {"kind": "silence", "action_id": action_id}
        if tool == "deliver":
            output = self.store.run_dir(run_id) / "work" / "out" / f"{action_id}.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(canonical(params), encoding="utf-8")
            return {"kind": "silence", "action_id": action_id}
        if tool == "watch":
            task = asyncio.create_task(self._watch(run_id, action_id, **params))
            self.watchers[run_id].append(task)
            return {"kind": "silence", "action_id": action_id}
        raise ValueError("Unknown tool")

    async def _search(self, run_id, action_id, query, k=10, freshness=None, site=None, wait=True):
        if self.contains_sensitive_data(query) or (site and self.contains_sensitive_data(site)):
            return blocked("policy_deny", "Sensitive data detected in search", "exfiltration")
        config = self.store.get_run(run_id)["config"]
        if config["mode"] == "frozen":
            return self.services["frozen"].search(config["corpus_id"], query, k, site=site)
        key = digest({"query": query, "k": k, "freshness": freshness, "site": site}).split(":")[1]
        target = self.store.run_dir(run_id) / "search" / f"{key}.json"
        target.parent.mkdir(exist_ok=True)
        if config["mode"] == "replay":
            source = self.store.run_dir(config["archive_in"]) / "search" / f"{key}.json"
            if not source.is_file():
                if "replay" in self.services:
                    return await self.services["replay"].handle(
                        run_id,
                        action_id,
                        "search",
                        {"query": query, "k": k, "site": site, "freshness": freshness},
                    )
                self.store.append_record(run_id, "wanted", tool="search", query=query)
                return blocked("replay_miss", "No matching archived search", "archive_lookup")
            reply = json.loads(source.read_text(encoding="utf-8"))
            reply["provider_id"] = f"local@{config['archive_in']}"
            target.write_text(canonical(reply), encoding="utf-8")
            return reply
        provider = config.get("search_provider", "brave")
        api_key = self.credential(PROVIDERS[provider]["key_env"])
        if not api_key:
            return blocked(
                "policy_deny", f"{provider.title()} search is not configured", "search_credentials"
            )
        if site and not re.fullmatch(r"[A-Za-z0-9.-]+", site):
            return blocked("policy_deny", "site must be a hostname", "invalid_site")
        price = search_price(provider)
        reservation = self.store.reserve(run_id, price)
        if not reservation:
            return blocked("budget_exhausted", "Search cost exceeds remaining budget", "budget_cap")

        def uncertain():
            self.store.update_run(run_id, tainted=True)
            if self.store.get_run(run_id)["state"] == "running":
                self.store.update_run(run_id, state="frozen")
            self.store.append_record(
                run_id, "search_uncertain", action_id=action_id, reservation_id=reservation
            )

        try:
            action = next(
                row
                for row in reversed(self.store.records(run_id))
                if row["kind"] == "action" and row.get("action_id") == action_id
            )
            request = provider_request(
                provider,
                api_key,
                {"query": query, "k": k, "freshness": freshness, "site": site},
                submitted_at=action["ts"],
            )
            response = await self.submit_search(
                run_id, action_id, request, reservation_id=reservation
            )
            if isinstance(response, dict):
                if response.get("egress_submitted"):
                    uncertain()
                else:
                    self.store.release(reservation)
                return response
            if not self.store.settle(reservation, "search", price):
                uncertain()
                return blocked(
                    "budget_exhausted", "Search charge could not settle", "search_accounting"
                )
            if response.status_code == 429:
                return blocked(
                    "rate_limit", "Search provider rate limited the request", "search_rate"
                )
            if response.status_code != 200:
                return blocked(
                    "platform_block",
                    f"Search returned HTTP {response.status_code}",
                    "search_provider",
                )
            reply = normalize_results(provider, response.json(), k)
            target.write_text(canonical(reply), encoding="utf-8")
            return reply
        except httpx.TimeoutException:
            uncertain()
            return blocked("timeout", "Search provider timed out", "search_timeout")
        except httpx.HTTPError:
            uncertain()
            return blocked("platform_block", "Search provider response failed", "search_provider")
        except asyncio.CancelledError:
            uncertain()
            raise
        except (ValueError, KeyError, TypeError):
            return blocked("platform_block", "Search provider response failed", "search_provider")

    async def _watch(self, run_id, action_id, url, interval):
        previous = None
        while self.store.get_run(run_id)["state"] in {"running", "frozen"}:
            if self.store.get_run(run_id)["state"] == "frozen":
                await asyncio.sleep(0.1)
                continue
            # Watch reads are world actions too and use the same policy and scheduler.
            result = await self.act(run_id, "fetch", {"url": url, "wait": True})
            if result.get("kind") == "ticket":
                task = self.jobs.get(run_id, {}).get(result["action_id"])
                result = await asyncio.shield(task) if task else result
            if result.get("kind") == "result":
                current = result.get("hash") or result.get("content_hash")
                if current is None:
                    handle = result.get("handle", "")
                    local = self.store.run_dir(run_id) / handle.lstrip("/")
                    current = digest(local.read_bytes()) if local.is_file() else None
                if previous is not None and current != previous:
                    self.push(
                        run_id,
                        {
                            "type": "page_changed",
                            "url": "[redacted]" if self.contains_sensitive_data(url) else url,
                            "url_hash": digest(url),
                            "previous_hash": previous,
                            "hash": current,
                            "watch_action_id": action_id,
                        },
                    )
                previous = current
            await asyncio.sleep(interval)

    async def freeze(self, run_id):
        async with self._lock(run_id):
            run = self.store.get_run(run_id)
            if run["state"] != "running":
                raise ValueError("Only running runs can be frozen")
            self.store.update_run(run_id, state="frozen")
            self.store.append_record(run_id, "intervention", reason="operator_freeze")
        await self._cancel_jobs(run_id, include_watchers=False)
        if self.lane:
            await self.lane.stop_run(run_id)
        if "proxy" in self.services:
            await self.services["proxy"].suspend_run(run_id)
        return self.status(run_id)

    async def resume(self, run_id):
        run = self.store.get_run(run_id)
        if run["state"] != "frozen":
            raise ValueError("Only frozen runs can resume")
        if self.status(run_id)["wall_left_s"] <= 0:
            raise ValueError("Run wall clock elapsed")
        self.store.update_run(run_id, state="running")
        self.store.append_record(run_id, "intervention", reason="operator_resume")
        return self.status(run_id)

    async def _cancel_jobs(self, run_id, *, include_watchers=True):
        current = asyncio.current_task()
        jobs = dict(self.jobs.get(run_id, {}))
        tasks = [*jobs.values(), *(self.watchers.get(run_id, []) if include_watchers else [])]
        tasks = [task for task in tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first instruction cannot write its own outcome.
        for action_id in jobs:
            spec = self.actions.get(run_id, {}).pop(action_id, None)
            if spec is None:
                continue
            tool, params = spec
            reply = blocked("policy_deny", "Action cancelled before execution", "run_cancelled")
            self.store.append_record(
                run_id,
                "outcome",
                action_id=action_id,
                tool=tool,
                reply_kind="blocked",
                reply=reply,
                block=reply,
                delivered="async" if action_id in self.pending else "sync",
            )
            if action_id in self.pending:
                if tool == "fetch_many":
                    for index, url in enumerate(params["urls"]):
                        self.push(
                            run_id,
                            {
                                "type": "job_done",
                                "action_id": action_id,
                                "index": index,
                                "url": "[redacted]" if self.contains_sensitive_data(url) else url,
                                "url_hash": digest(url),
                                "reply": reply,
                            },
                        )
                else:
                    self.push(run_id, {"type": "job_done", "action_id": action_id, "reply": reply})
            self.pending.discard(action_id)
            self.jobs.get(run_id, {}).pop(action_id, None)

    def _subscription_billing(self, run_id, config):
        """Subscription model dollars are never measured; a transcript run estimates them."""
        estimated = config.get("model_metering") == "transcript"
        model_estimate = self.store.spend(run_id).get("model.estimated", 0) if estimated else None
        return {
            "mode": "subscription",
            "model_usd": None,
            "model_estimated_usd": model_estimate,
            "world_tools_usd": round(self.store.total_spent(run_id) - (model_estimate or 0), 9),
            "dollar_cap_scope": "model_estimate_and_world_tools"
            if estimated
            else "world_tools_only",
        }

    async def end_run(self, run_id, *, reason="agent_finished", truncated=False):
        async with self._lock(run_id):
            run = self.store.get_run(run_id)
            if run["state"] in {"sealed", "killed", "ended"}:
                return self.status(run_id)
            if run_id in self._ending:
                return self.status(run_id)
            self._ending.add(run_id)
            self.store.update_run(run_id, state="stopping")
        try:
            if "harnesses" in self.services:
                await self.services["harnesses"].stop_for_run(run_id)
            open_tickets = (
                sum(
                    ticket["state"] in {"open", "pending", "accepted"}
                    for ticket in self.services["asks"].list_tickets(run_id)
                )
                if "asks" in self.services
                else 0
            )
            await self._cancel_jobs(run_id)
            if "market" in self.services:
                # Before the seal, which refuses settlement, finish any interrupted stake.
                self.services["market"].reconcile_stakes(run_id)
            if "skill_tests" in self.services:
                await self.services["skill_tests"].stop_run(run_id)
            if "payments" in self.services:
                await self.services["payments"].stop_run(run_id)
            if "browser" in self.services:
                await self.services["browser"].stop_run(run_id)
            if "proxy" in self.services:
                await self.services["proxy"].stop_run(run_id)
            if "asks" in self.services:
                life_id = run["config"].get("life_id")
                life = (
                    self.services["lives"].get(life_id)
                    if life_id and "lives" in self.services
                    else None
                )
                # A life that sleeps, rests or outlives a restart keeps its open asks.
                preserve = bool(
                    life
                    and not life["ended"]
                    and (
                        life["state"] == "sleeping"
                        or life.get("on_harness_exit", "end") == "rest"
                        or self.closing
                    )
                )
                await_result = self.services["asks"].cancel_run(run_id, preserve_pending=preserve)
                if inspect.isawaitable(await_result):
                    await await_result
            lane_audit = await self.lane.stop_run(run_id) if self.lane else None
            if "vault" in self.services:
                self.services["vault"].revoke_run(run_id)
            if "grants" in self.services:
                for grant in self.services["grants"].current(run_id):
                    self.services["grants"].revoke(grant["grant_id"], reason="run_ended")
            while True:
                background = [
                    task
                    for task, selected in self.background_runs.items()
                    if selected == run_id and task is not asyncio.current_task()
                ]
                if not background:
                    break
                await asyncio.gather(*background, return_exceptions=True)
            deadline = self.deadlines.pop(run_id, None)
            if deadline and deadline is not asyncio.current_task():
                deadline.cancel()
            run_dir = self.store.run_dir(run_id)
            snapshot = run_dir / "work-snapshot"
            work = run_dir / "work"
            work.mkdir(exist_ok=True)
            if not snapshot.exists():
                omissions = snapshot_workspace(work, snapshot)
                if omissions:
                    self.store.append_record(
                        run_id,
                        "snapshot_omissions",
                        omissions=omissions,
                        paths=[entry["path"] for entry in omissions],
                    )
                    self.store.update_run(run_id, tainted=True)
            actual_manifest = snapshot_manifest(snapshot)
            anchor = next(
                (
                    row
                    for row in reversed(self.store.records(run_id))
                    if row["kind"] == "workspace_snapshot"
                ),
                None,
            )
            if anchor:
                if any(anchor.get(key) != value for key, value in actual_manifest.items()):
                    raise ValueError("Workspace snapshot changed during a seal retry")
            else:
                self.store.append_record(run_id, "workspace_snapshot", **actual_manifest)
            end = {
                "truncated" if truncated else "terminated": reason,
                "reason": reason,
                "open_tickets_at_end": open_tickets,
                "open_expectations_at_end": len(self.services["forecast"].pending(run_id))
                if "forecast" in self.services
                else 0,
            }
            reconciliation = self.store.reconcile(run_id)
            record_check = self.store.verify_record(run_id)
            if reconciliation["reserved_usd"]:
                reconciliation = {
                    **reconciliation,
                    "valid": False,
                    "reason": "Unresolved provider usage reservations",
                }
            run = self.store.get_run(run_id)
            if not any(r["kind"] == "beam_up" for r in self.store.records(run_id)):
                self.store.append_record(run_id, "beam_up", end=end)
            self.store.append_record(
                run_id,
                "seal",
                ledger=reconciliation,
                end=end,
                lane_drain=lane_audit,
                record_verification=record_check,
            )
            self.store.update_run(
                run_id,
                state="sealed",
                end=end,
                tainted=(
                    bool(run.get("tainted"))
                    or not reconciliation["valid"]
                    or not record_check["valid"]
                ),
            )
            self.capabilities.revoke(run_id)
            if "lives" in self.services and self.run_config(run_id).get("life_id"):
                self.services["lives"].session_ended(run_id)
            self.report(run_id, save=True)
            if "retention" in self.services:
                self.services["retention"].register_run(run_id)
            if (
                "replay" in self.services
                and self.services["replay"].indexable(run_id)
                and not self.store.get_run(run_id).get("tainted")
            ):
                self.services["replay"].build(run_id)
            return self.status(run_id)
        finally:
            self._ending.discard(run_id)

    def report(self, run_id, *, save=False):
        if run_id.startswith("study_"):
            return self.study_report(run_id)
        from analysis.publication import load_measurements
        from analysis.report import build_report, render_report

        reconciliation = self.store.reconcile(run_id)
        if reconciliation["reserved_usd"]:
            reconciliation = {
                **reconciliation,
                "valid": False,
                "reason": "Unresolved provider usage reservations",
            }
        report = build_report(
            self.store.get_run(run_id),
            self.store.records(run_id),
            ledger=self.store.spend(run_id),
            verification={"record": self.store.verify_record(run_id), "ledger": reconciliation},
            measurements=load_measurements(self.store, run_id),
        )
        config = self.store.get_run(run_id)["config"]
        if config.get("model_billing_mode") == "subscription":
            report["billing"] = {
                **self._subscription_billing(run_id, config),
                "total_usd": None,
                "scientific_eligible": False,
            }
        if "judge_canaries" in self.services:
            checks = self.services["judge_canaries"].reports_for_run(run_id)
            report["judge_canary_checks"] = checks
            report.setdefault("spec_sheet", {}).setdefault("measurements", {})[
                "judge_canary_checks"
            ] = checks
        if save:
            run_dir = self.store.run_dir(run_id)
            (run_dir / "report.json").write_text(canonical(report), encoding="utf-8")
            (run_dir / "spec-sheet.md").write_text(render_report(report), encoding="utf-8")
        return report

    def study_report(self, study_id):
        from analysis.cohort import StudyReport

        return StudyReport(self).build(study_id)

    def life_report(self, life_id):
        from analysis.publication import assemble_life_report

        life = self.services["lives"].get(life_id)
        checkpoints = []
        owned = self.store.data_dir / "lives" / life_id / "rubric"
        for reference in life.get("rubric_checkpoints", []):
            path = self.store.data_dir / reference["artifact_ref"]
            if not path.is_relative_to(owned) or path.resolve() != path or not path.is_file():
                raise ValueError("Life report reference is outside its private rubric directory")
            raw = path.read_bytes()
            if digest(raw) != reference["artifact_hash"]:
                raise ValueError("Life report artifact changed after registration")
            report = json.loads(raw)
            if (
                report.get("life_id") != life_id
                or report.get("checkpoint") != reference["checkpoint"]
            ):
                raise ValueError("Life report identity does not match its registered checkpoint")
            checkpoints.append({"reference": reference, "report": report})
        return {
            "life_id": life_id,
            "status": self.services["lives"].status(life_id),
            "sessions": life["sessions"],
            "checkpoints": checkpoints,
            "rubric": checkpoints[-1]["report"]
            if checkpoints
            else {
                "status": "unmeasured",
                "reason": "No verified life rubric checkpoint has been recorded",
            },
            "publication": assemble_life_report(self, life, checkpoints),
        }

    def export(self, run_id):
        if run_id.startswith("life_"):
            return self.services["lives"].export(run_id)
        run = self.store.get_run(run_id)
        if run["state"] not in {"sealed", "killed", "ended"}:
            raise ValueError("Seal the run before exporting")
        output = io.BytesIO()
        root = self.store.run_dir(run_id)
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for path in sorted(root.rglob("*")):
                # Do not follow workspace symlinks outside the run or export private credentials.
                relative = path.relative_to(root)
                if (
                    path.is_symlink()
                    or path.is_dir()
                    or relative.parts[0] == "work"
                    or ("model-io" in relative.parts and not run["config"].get("capture_model_io"))
                ):
                    continue
                if path.is_file() and path.resolve().is_relative_to(root.resolve()):
                    archive.add(path, arcname=f"{run_id}/{path.relative_to(root)}", recursive=False)
        return output.getvalue()

    def bridge(self, run_id=None):
        runs = [self.store.get_run(run_id)] if run_id else self.store.list_runs()
        runs = [
            run
            for run in runs
            if not run["config"].get("world_accounting")
            and not run["config"].get("private_archive")
            and run["config"].get("metering_lane", "harness") == "harness"
        ]
        return {
            "runs": [
                {
                    "run_id": run["run_id"],
                    "state": run["state"],
                    "reply_policy": run["config"].get("reply_policy", "silent"),
                    "messages": [
                        r for r in self.store.records(run["run_id"]) if r["kind"] == "ask_opened"
                    ],
                    "tickets": self.services["asks"].list_tickets(run["run_id"])
                    if "asks" in self.services
                    else [],
                }
                for run in runs
            ]
        }

    def operator_message(self, run_id, text, human_seconds=0):
        run = self.store.get_run(run_id)
        config = run["config"]
        if run["state"] != "running":
            raise ValueError("Run is not running")
        if (
            not config["operator_messages_allowed"]
            or config["reply_policy"] != "blockers_plus_checkins"
        ):
            raise ValueError("Operator messages are not enabled for this run")
        if isinstance(human_seconds, bool) or not 0 <= human_seconds <= 600:
            raise ValueError("Active human time must be between zero and ten minutes")
        reference = identifier("checkin")
        if config.get("human_time_priced") == "in_budget":
            rate = Decimal(str(config.get("human_rate_per_minute", 0)))
            amount = (rate * Decimal(str(human_seconds)) / 60).quantize(
                Decimal("0.000000001"), rounding=ROUND_CEILING
            )
            life_id = config.get("life_id")
            if life_id and "lives" in self.services:
                self.services["lives"].charge_human(
                    life_id, amount, evidence_ref=reference, source_run_id=run_id
                )
            elif not self.store.debit(run_id, "human", amount):
                raise ValueError("Active human time exceeds the remaining run budget")
        self.store.append_record(
            run_id,
            "ask_answered",
            human_seconds=human_seconds,
            kind_requested="checkin",
            evidence_ref=reference,
        )
        return self.push(run_id, {"type": "operator_message", "text": text})

    async def recover(self):
        for run in self.store.list_runs():
            if run["config"].get("world_accounting"):
                continue
            if run["state"] in {"running", "frozen", "stopping", "preflight"}:
                # The record is durable; live Python tasks and sockets are not.
                if run["config"].get("private_archive"):
                    self._seal_internal_run(run["run_id"])
                else:
                    await self.end_run(run["run_id"], reason="world_fault", truncated=True)
        for service in self.services.values():
            if hasattr(service, "recover"):
                result = service.recover()
                if inspect.isawaitable(result):
                    await result

    async def aclose(self):
        self.closing = True
        for run in self.store.list_runs():
            if run["config"].get("world_accounting"):
                continue
            if run["state"] in {"running", "frozen", "stopping", "preflight"}:
                if run["config"].get("private_archive"):
                    self._seal_internal_run(run["run_id"])
                else:
                    await self.end_run(run["run_id"], reason="world_fault", truncated=True)
        for service in reversed(list(self.services.values())):
            if hasattr(service, "aclose"):
                await service.aclose()
        await self.gateway.aclose()
        if self._owns_search_client:
            await self.search_client.aclose()
        self.capabilities.close()
        self.late_events.close()
        shared = getattr(self, "_capability_database", None)
        if shared is not None:
            shared.db.close()
        self.store.close()

    def _seal_internal_run(self, run_id):
        self.store.append_record(
            run_id, "beam_up", end={"truncated": "world_fault", "reason": "world_fault"}
        )
        self.store.append_record(
            run_id,
            "seal",
            ledger=self.store.reconcile(run_id),
            record_verification=self.store.verify_record(run_id),
        )
        self.store.update_run(
            run_id,
            state="sealed",
            tainted=True,
            end={"truncated": "world_fault", "reason": "world_fault"},
        )
