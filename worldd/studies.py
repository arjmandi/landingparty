"""Explicitly budgeted registered study execution over actual harness adapters."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import shutil
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .life_runner import LifeRunner
from .storage import canonical_json, content_hash, utc_now
from .study_lives import StudyLives
from .util import identifier


def _adapter(relative, name):
    path = Path(__file__).parents[1] / "adapters" / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("The requested harness adapter is not installed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _BoundRunClient:
    """Adapters reuse a run the experimental scheduler already registered."""

    def __init__(self, client, created):
        self.client, self.created, self.bound = client, created, False

    async def post(self, path, **kwargs):
        if path.rstrip("/") == "/v1/run":
            if self.bound:
                raise ValueError("A study harness may not create a second episode")
            self.bound = True
            return httpx.Response(
                201, json=self.created, request=httpx.Request("POST", "http://world/v1/run")
            )
        return await self.client.post(path, **kwargs)

    async def get(self, path, **kwargs):
        return await self.client.get(path, **kwargs)


class _BoundLaneClient:
    def __init__(self, client, created):
        self.client, self.created = client, created

    async def post(self, path, **kwargs):
        headers = {**kwargs.pop("headers", {}), "X-World-Run-Id": self.created["run_id"]}
        if self.created.get("run_token"):
            headers["X-World-Run-Token"] = self.created["run_token"]
        return await self.client.post(path, headers=headers, **kwargs)


class StudyService(LifeRunner, StudyLives):
    def __init__(self, world, *, harnesses=None):
        self.world, self.store = world, world.store
        self.harnesses = dict(harnesses or {})
        self.tasks = {}
        self.app = None
        self._clients = []
        with self.store._lock:
            self.store.db.execute(
                "CREATE TABLE IF NOT EXISTS studies (study_id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
            )
            self._initialize_life_runners()

    def configure(self, app):
        self.app = app

    def get(self, study_id):
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT payload FROM studies WHERE study_id=?", (study_id,)
            ).fetchone()
        if row is None:
            raise KeyError(study_id)
        return json.loads(row[0])

    def list(self):
        with self.store._lock:
            return [
                json.loads(row[0])
                for row in self.store.db.execute("SELECT payload FROM studies ORDER BY rowid")
            ]

    def _save(self, value):
        with self.store._transaction():
            self.store.db.execute(
                "INSERT INTO studies VALUES(?,?) ON CONFLICT(study_id) DO UPDATE SET payload=excluded.payload",
                (value["study_id"], canonical_json(value)),
            )

    def plan(self, spec=None, *, kind="experiment"):
        from analysis.experiments import validate_prereg

        from .models import RunConfig

        if kind not in {"experiment", "shakedown"}:
            raise ValueError("Unknown study kind")
        spec = json.loads(canonical_json(spec or {}))
        study_id = identifier("study")
        source = Path(__file__).parents[1]
        if kind == "shakedown":
            prereg = spec.get("prereg") or json.loads(
                (source / "prereg/shakedown.template.json").read_text()
            )
            base = json.loads((source / "goals/14-audit/run.json").read_text())
            base.update(spec.get("config", {}))
            base.setdefault("research_mode", "research")
            defaults = [("B2", list(range(5))), ("B1", list(range(5))), ("B0", [0]), ("B3", [0])]
            cells = spec.get("cells") or [
                {
                    "cell_id": name,
                    "baseline": name,
                    "seeds": seeds,
                    "config": dict(base),
                    "model": spec.get("model"),
                    "seed_urls": spec.get("seed_urls", []),
                }
                for name, seeds in defaults
            ]
        else:
            prereg, cells = spec.get("prereg"), spec.get("cells")
            if not isinstance(prereg, dict) or not isinstance(cells, list) or not cells:
                raise ValueError("An experiment needs a pre-registration and nonempty cells")
        checked = validate_prereg(prereg)
        if not checked["valid"]:
            raise ValueError("Invalid pre-registration: " + "; ".join(checked["errors"]))
        prerequisites = []
        has_dependencies = any(cell.get("depends_on") for cell in cells)
        has_lives = any(cell.get("config", {}).get("life_id") for cell in cells)
        prepared, cap = [], Decimal(0)
        seen = set()
        for position, original in enumerate(cells):
            cell = dict(original)
            if {"initial_messages", "context_refs", "captured_context"}.intersection(cell):
                raise ValueError("Study contexts must resolve from actual registered dependencies")
            cell_id = str(cell.get("cell_id", position))
            if cell_id in seen:
                raise ValueError("Study cell identities must be unique")
            seen.add(cell_id)
            baseline = cell.get("baseline", "B1")
            if (
                baseline not in {"B0", "B1", "B2", "B3", "B3b", "B3c", "self-interaction"}
                and baseline not in self.harnesses
            ):
                raise ValueError("No registered adapter exists for baseline " + str(baseline))
            seeds = cell.get("seeds", prereg.get("seeds", list(range(prereg.get("seed_count", 5)))))
            if (
                not isinstance(seeds, list)
                or not seeds
                or len(set(seeds)) != len(seeds)
                or any(isinstance(seed, bool) or not isinstance(seed, int) for seed in seeds)
            ):
                raise ValueError("Each cell needs a nonempty unique integer seed list")
            max_steps, max_tokens = (
                int(cell.get("max_steps", 200)),
                int(cell.get("max_tokens", 4096)),
            )
            revisions = int(cell.get("revision_turns", 20))
            if (
                not 1 <= max_steps <= 10000
                or not 1 <= max_tokens <= 100000
                or not 0 <= revisions <= 100
            ):
                raise ValueError("Harness step/token/revision limits are invalid")
            config = dict(cell.get("config", {}))
            from .study_design import effective_life_config

            config = effective_life_config(self.world, config)
            if baseline == "B1":
                config["harness_id"] = (
                    f"reference-loop@0.1/native-parallel/max-steps={max_steps}/max-tokens={max_tokens}"
                )
            elif baseline != "B2":
                config["harness_id"] = f"{baseline}@0.1/steps={max_steps}/revisions={revisions}"
            else:
                config.setdefault("harness_id", "claude-code@native/stop")
            if baseline in {"B0", "self-interaction"}:
                config["tools_disabled"] = True
            if cell.get("model"):
                config["model_snapshot_id"] = cell["model"]
            normalized = RunConfig.model_validate(config).model_dump(mode="json")
            config = normalized
            model = cell.get("model", config.get("model_snapshot_id"))
            if baseline in {"B0", "B1", "B2", "self-interaction"} and not model:
                prerequisites.append(
                    {
                        "cell": cell_id,
                        "requirement": "pinned_model",
                        "detail": "Select a priced model snapshot before execution",
                    }
                )
            if (
                baseline == "B2"
                and baseline not in self.harnesses
                and shutil.which("claude") is None
            ):
                prerequisites.append(
                    {
                        "cell": cell_id,
                        "requirement": "claude_cli",
                        "detail": "Claude Code is not installed",
                    }
                )
            if baseline in {"B3", "B3b"} and not cell.get("seed_urls") and not cell.get("queries"):
                prerequisites.append(
                    {
                        "cell": cell_id,
                        "requirement": "world_seed_sources",
                        "detail": "Declare seed URLs or queries for a meaningful null",
                    }
                )
            if baseline == "B3c" and (not config.get("life_id") or not cell.get("recipients")):
                prerequisites.append(
                    {
                        "cell": cell_id,
                        "requirement": "outreach_life_and_recipients",
                        "detail": "Register a separately labelled baseline life and explicit consenting recipients",
                    }
                )
            if config["research_mode"] == "research" and "RESEARCHER_SELECTED_EVENT" in str(
                config.get("goal")
            ):
                prerequisites.append(
                    {
                        "cell": cell_id,
                        "requirement": "frozen_goal",
                        "detail": "Freeze the selected post-cutoff event and researcher question set",
                    }
                )
            registered = cell.get("series_id")
            registry = self.world.services.get("registry")
            if registered:
                series = registry.get_series(registered) if registry else None
                if not series or series["prereg_hash"] != checked["prereg_hash"]:
                    raise ValueError("The cell's series does not use this pre-registration")
            elif registry and (cell.get("tuple") or cell.get("reference_run_id")):
                version = (
                    cell.get("tuple")
                    or self.store.get_run(cell["reference_run_id"])["config"]["tuple"]
                )
                registered = registry.register_series(prereg, version)["series_id"]
            elif not has_dependencies and not has_lives:
                prerequisites.append(
                    {
                        "cell": cell_id,
                        "requirement": "registered_version_tuple",
                        "detail": "Supply a registered series, full tuple, or representative reference run",
                    }
                )
            if registered:
                config["series_id"] = registered
            options = cell.get("analysis_options", {})
            measurement = Decimal(str(options.get("measurement_budget_usd", 0)))
            if not measurement.is_finite() or measurement < 0:
                raise ValueError("Measurement budget must be finite and nonnegative")
            cap += (Decimal(str(config["budget"]["usd"])) + measurement) * len(seeds)
            prepared.append(
                {
                    **cell,
                    "cell_id": cell_id,
                    "baseline": baseline,
                    "seeds": seeds,
                    "config": config,
                    "model": model,
                    "series_id": registered,
                    "max_steps": max_steps,
                    "max_tokens": max_tokens,
                    "revision_turns": revisions,
                }
            )
        from .study_dependencies import dependency_protocol

        dependencies = dependency_protocol(prepared, prereg)
        from .study_design import validate_life_design

        life_design = validate_life_design(self.world, prepared, prereg)
        if life_design:
            prerequisites.extend(life_design["prerequisites"])
        lifetime_cap = self._life_study_plan(prepared)
        if lifetime_cap is not None:
            cap = lifetime_cap
        if kind == "shakedown":
            schedule = {name: [] for name in ("B2", "B1", "B0", "B3")}
            for cell in prepared:
                if cell["baseline"] not in schedule:
                    raise ValueError(
                        "The shakedown must preserve its registered four-baseline schedule"
                    )
                schedule[cell["baseline"]].extend(cell["seeds"])
            if {name: len(seeds) for name, seeds in schedule.items()} != {
                "B2": 5,
                "B1": 5,
                "B0": 1,
                "B3": 1,
            }:
                raise ValueError("The shakedown must preserve all twelve registered runs")
            declared = {item["baseline"]: item["seeds"] for item in prereg.get("schedule", [])}
            if any(
                sorted(seeds) != sorted(declared.get(name, [])) for name, seeds in schedule.items()
            ):
                raise ValueError("The shakedown cells must match its frozen pre-registration seeds")
        goal_protocol = None
        if kind == "shakedown" and any(
            cell["config"]["research_mode"] == "research" for cell in prepared
        ):
            if not spec.get("goal_protocol"):
                prerequisites.append(
                    {
                        "requirement": "frozen_goal_protocol",
                        "detail": "Freeze the actual goal, twenty-five questions and eight traps before execution",
                    }
                )
            else:
                from .study_inputs import freeze_protocol

                goal_protocol = freeze_protocol(
                    self.store, study_id, spec["goal_protocol"], prereg, prepared
                )
        study = {
            "study_id": study_id,
            "kind": kind,
            "state": "planned",
            "created_at": utc_now(),
            "prereg": prereg,
            "prereg_hash": checked["prereg_hash"],
            "cells": prepared,
            "total_cap_usd": float(cap),
            "run_count": sum(len(cell["seeds"]) for cell in prepared)
            if lifetime_cap is None
            else None,
            "life_count": len(prepared) if lifetime_cap is not None else None,
            "prerequisites": prerequisites,
            "run_ids": [],
            "result": None,
            "scientific": False,
            "goal_protocol": goal_protocol,
            "dependency_protocol": dependencies,
            "execution_bindings": [],
            "resolutions": [],
            "life_design": life_design,
            "execution_kind": "lives" if lifetime_cap is not None else "episodes",
            "life_runners": [],
        }
        study["plan_hash"] = content_hash(
            {key: study[key] for key in ("kind", "prereg", "cells", "total_cap_usd")}
        )
        self._save(study)
        return study

    async def execute(self, study_id, *, execute=False, budget_usd=None, options=None):
        if execute is not True:
            raise ValueError("Execution must be explicitly requested")
        study = self.get(study_id)
        if (
            content_hash({key: study[key] for key in ("kind", "prereg", "cells", "total_cap_usd")})
            != study["plan_hash"]
        ):
            raise ValueError("The frozen study plan changed before execution")
        from .study_inputs import verify_protocol

        verify_protocol(self.store, study)
        if study.get("life_design"):
            from .study_design import validate_life_design

            if (
                validate_life_design(self.world, study["cells"], study["prereg"])
                != study["life_design"]
            ):
                raise ValueError(
                    "The registered scientific life conditions changed before execution"
                )
        resumable = study["state"] == "paused" and study.get("execution_kind") == "lives"
        if study["state"] != "planned" and not resumable:
            raise ValueError("A study can be executed once; create a new plan for another attempt")
        if study["prerequisites"]:
            raise ValueError(
                "Study prerequisites remain: "
                + ", ".join(sorted({item["requirement"] for item in study["prerequisites"]}))
            )
        if budget_usd is None or isinstance(budget_usd, bool):
            raise ValueError("An explicit study dollar ceiling is required")
        ceiling = Decimal(str(budget_usd))
        if not ceiling.is_finite() or ceiling < Decimal(str(study["total_cap_usd"])):
            raise ValueError(
                "The declared ceiling does not cover every registered run and measurement cap"
            )
        if self.app is None and not self.harnesses:
            raise ValueError("Configure the actual application before executing harnesses")
        options = dict(options or {})
        if set(options) - {"wait", "preflight_options"}:
            raise ValueError("Execution options cannot change the registered study conditions")
        with self.store._transaction():
            current = self.store.db.execute(
                "SELECT payload FROM studies WHERE study_id=?", (study_id,)
            ).fetchone()
            if json.loads(current[0])["state"] not in (
                {"planned", "paused"} if resumable else {"planned"}
            ):
                raise ValueError("This study is already executing")
            study.update(state="running", budget_ceiling_usd=float(ceiling))
            study.setdefault("started_at", utc_now())
            study["execution_options"] = options
            if resumable:
                study["resumed_at"] = utc_now()
            self.store.db.execute(
                "UPDATE studies SET payload=? WHERE study_id=?", (canonical_json(study), study_id)
            )
        task = asyncio.create_task(
            self._execute_lives(study_id, options)
            if study.get("execution_kind") == "lives"
            else self._execute(study_id, options)
        )
        self.tasks[study_id] = task
        if options.get("wait"):
            await asyncio.shield(task)
        return self.get(study_id)

    async def _execute(self, study_id, options):
        from analysis.experiments import ExperimentRunner

        service = self

        class TrackedWorld:
            def execution_schedule(self, cells, schedules):
                from .study_dependencies import execution_schedule

                return execution_schedule(cells, schedules)

            def resolve_cell(self, cell, seed):
                from .study_dependencies import resolve_dependencies

                frozen = service.get(study_id)
                self.resolved = json.loads(canonical_json(cell))
                self.sources = {}
                if cell.get("depends_on"):
                    changes, self.sources = resolve_dependencies(
                        service, cell["depends_on"], seed, study_id=study_id
                    )
                    config = self.resolved["config"]
                    captured = changes.pop("captured_context", None)
                    if captured:
                        if (
                            cell["baseline"] != "B1"
                            or self.sources["context"]["model_snapshot_id"] != cell["model"]
                        ):
                            raise ValueError(
                                "Captured-context controls require the same-model reference adapter"
                            )
                        self.resolved.update(captured)
                    config["iota"] = {**config.get("iota", {}), **changes.pop("iota", {})}
                    config.update(changes)
                self.dynamic = frozen.get("dependency_protocol") is not None
                if self.dynamic:
                    self.resolved["config"].pop("series_id", None)
                    self.resolved["series_id"] = None
                self.resolution = {
                    "cell_id": cell["cell_id"],
                    "seed": seed,
                    "episode": cell.get("episode", 1),
                    "sources": self.sources,
                    "config": {**self.resolved["config"], "seed": seed},
                    "dependency_protocol_hash": (frozen.get("dependency_protocol") or {}).get(
                        "hash"
                    ),
                }
                self.resolution["hash"] = content_hash(self.resolution)
                return self.resolved

            def create_run(self, config):
                current = service.get(study_id)
                current.setdefault("resolutions", []).append(self.resolution)
                service._save(current)
                created = service.world.create_run(
                    config,
                    register_prereg=current["prereg"] if self.dynamic else None,
                    execution_binding={
                        "study_id": study_id,
                        "cell_id": self.resolution["cell_id"],
                        "seed": self.resolution["seed"],
                        "episode": self.resolution["episode"],
                        "dependency_protocol_hash": self.resolution["dependency_protocol_hash"],
                        "resolution_hash": self.resolution["hash"],
                    },
                )
                current["run_ids"].append(created["run_id"])
                current.setdefault("execution_bindings", []).append(
                    {
                        **self.resolution,
                        "run_id": created["run_id"],
                        "series_id": service.world.run_config(created["run_id"])["series_id"],
                    }
                )
                service._save(current)
                return {
                    **created,
                    "series_id": service.world.run_config(created["run_id"])["series_id"],
                }

            def record_started(self, created, cell):
                registry = service.world.services.get("registry")
                if registry:
                    return registry.record_execution(
                        study_id,
                        created["run_id"],
                        cell=cell["cell_id"],
                        seed=service.world.run_config(created["run_id"])["seed"],
                    )

            async def end_run(self, *args, **kwargs):
                return await service.world.end_run(*args, **kwargs)

        study = self.get(study_id)

        async def harness(created, cell):
            run_id = created["run_id"]
            if self.store.get_run(run_id)["state"] == "preflight":
                report = await self.world.services["preflight"].run(
                    run_id, **options.get("preflight_options", {})
                )
                if not report.get("passed"):
                    raise ValueError("The actual run did not pass preflight")
                created = {**created, **self.world.activate_run(run_id)}
            # The interleaved scheduler supplies each seed at run creation. Bind
            # the adapter to that actual run, including seed-sensitive baselines.
            return await self._harness(created, {**cell, "config": self.world.run_config(run_id)})

        runner = ExperimentRunner(
            TrackedWorld(), harness, pipeline=self.world.services.get("pipeline")
        )
        try:
            result = await runner.execute(study["prereg"], study["cells"])
            current = self.get(study_id)
            current.update(
                state="completed" if result["completed"] else "failed",
                result=result,
                completed_at=utc_now(),
                scientific=False,
            )
            self._save(current)
        except asyncio.CancelledError:
            await self._interrupted(study_id, "cancelled")
            raise
        except Exception as error:  # noqa: BLE001 - preserve actual started rows for arbitrary harness failures
            await self._interrupted(study_id, type(error).__name__)

    async def _clients_for(self, created):
        from .security import create_agent_app

        if self.app is None:
            raise ValueError("Application transports have not been configured")
        headers = (
            {"X-World-Control-Token": os.environ["WORLD_CONTROL_TOKEN"]}
            if os.environ.get("WORLD_CONTROL_TOKEN")
            else {}
        )
        world = httpx.AsyncClient(
            transport=httpx.ASGITransport(self.app),
            base_url="http://world",
            timeout=180,
            headers=headers,
        )
        lane = httpx.AsyncClient(
            transport=httpx.ASGITransport(self.app),
            base_url="http://world/openai/v1/",
            timeout=180,
            headers=headers,
        )
        agent = httpx.AsyncClient(
            transport=httpx.ASGITransport(create_agent_app(self.world)),
            base_url="http://agent",
            timeout=65,
        )
        self._clients += [world, lane, agent]
        return _BoundRunClient(world, created), _BoundLaneClient(lane, created), agent

    async def _harness(self, created, cell):
        callback = self.harnesses.get(cell["baseline"])
        if callback:
            result = callback(created, cell)
            return await result if inspect.isawaitable(result) else result
        if cell["baseline"] == "B2":
            return await self._claude(created, cell)
        world, lane, agent = await self._clients_for(created)
        clients = [world.client, lane.client, agent]
        try:
            common = {"max_steps": cell["max_steps"], "max_tokens": cell["max_tokens"]}
            if cell["baseline"] == "B1":
                module = _adapter("reference-loop/run.py", "landing_party_reference_study")
                return await module.run_reference(
                    cell["config"],
                    cell["model"],
                    world_client=world,
                    lane_client=lane,
                    agent_client=agent,
                    run_info=created,
                    initial_messages=cell.get("initial_messages"),
                    context_refs=cell.get("context_refs"),
                    **common,
                )
            from adapters.baselines.runner import run_baseline

            return await run_baseline(
                cell["baseline"],
                cell["config"],
                world_client=world,
                lane_client=lane,
                model=cell.get("model"),
                seed_urls=cell.get("seed_urls", []),
                queries=cell.get("queries", []),
                recipients=cell.get("recipients", []),
                revision_turns=cell["revision_turns"],
                **common,
            )
        finally:
            await asyncio.gather(*(client.aclose() for client in clients))
            for client in clients:
                if client in self._clients:
                    self._clients.remove(client)

    async def _claude(self, created, cell):
        binary = shutil.which("claude")
        if binary is None:
            raise ValueError("Claude Code is not installed")
        proxy = created.get("proxy", {})
        if not proxy.get("server") or not proxy.get("ca_path"):
            raise ValueError("Claude Code requires the actual recorded proxy and CA")
        configure = _adapter("claude-code/configure.py", "landing_party_claude_study_config")
        runner = _adapter("claude-code/run.py", "landing_party_claude_study_run")
        root = self.store.run_dir(created["run_id"])
        folder = root / "harness-binding"
        # A routing capability is process-only: never serialize it into binding artifacts.
        files = configure.binding(created["run_id"])
        configure.write_binding(folder, files)
        goal = cell["config"].get("goal")
        goal = goal.get("text", "") if isinstance(goal, dict) else goal or ""
        command = runner.command(folder, created["preamble"], goal)
        command += ["--model", cell["model"]]
        environment = runner.runtime_environment(
            folder, {**os.environ, "WORLD_RUN_TOKEN": created["run_token"]}
        )
        address = urlsplit(proxy["server"])
        credentials = quote(proxy["username"], safe="") + ":" + quote(proxy["password"], safe="")
        authenticated = urlunsplit(
            (address.scheme, credentials + "@" + address.netloc, address.path, "", "")
        )
        environment.update(
            HTTP_PROXY=authenticated,
            HTTPS_PROXY=authenticated,
            SSL_CERT_FILE=proxy["ca_path"],
            NO_PROXY="127.0.0.1,localhost,::1",
        )
        with (root / "harness-transcript.jsonl").open("xb") as transcript:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=transcript,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=root / "work",
                env=environment,
            )
            try:
                code = await asyncio.wait_for(process.wait(), cell["config"]["budget"]["wall_s"])
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 2)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
        if code:
            raise ValueError("Claude Code exited unsuccessfully")
        return {
            "run_id": created["run_id"],
            "harness": "Claude Code",
            "exit_code": code,
            "transcript_ref": "harness-transcript.jsonl",
            **runner.transcript_attrition(root / "harness-transcript.jsonl"),
        }

    async def _interrupted(self, study_id, reason):
        self._recover_started_rows(study_id)
        current = self.get(study_id)
        rows = []
        for run_id in current["run_ids"]:
            run = self.store.get_run(run_id)
            if run["state"] not in {"sealed", "ended", "killed"}:
                await self.world.end_run(run_id, reason="world_fault", truncated=True)
            rows.append({"run_id": run_id, "state": self.store.get_run(run_id)["state"]})
        current.update(
            state="failed",
            completed_at=utc_now(),
            scientific=False,
            result={
                "completed": False,
                "scientific": False,
                "reason": reason,
                "run_ids": current["run_ids"],
                "attrition": rows,
            },
        )
        self._save(current)

    async def recover(self):
        await self._recover_life_runners()
        for study in self.list():
            if study["state"] == "running" and study["study_id"] not in self.tasks:
                if study.get("execution_kind") == "lives":
                    self.tasks[study["study_id"]] = asyncio.create_task(
                        self._execute_lives(study["study_id"], study.get("execution_options", {}))
                    )
                    continue
                self._recover_started_rows(study["study_id"])
                await self._interrupted(study["study_id"], "world_restart")

    def _recover_started_rows(self, study_id):
        """Recover starts committed before the coordinator acknowledged their run IDs."""
        study = self.get(study_id)
        registry = self.world.services.get("registry")
        for run in self.store.list_runs():
            tag = run["config"].get("execution_binding", {})
            if tag.get("study_id") != study_id or run["run_id"] in study["run_ids"]:
                continue
            resolutions = [
                item
                for item in study.get("resolutions", [])
                if item["hash"] == tag.get("resolution_hash")
                and item["cell_id"] == tag.get("cell_id")
                and item["seed"] == tag.get("seed")
            ]
            if len(resolutions) != 1:
                raise ValueError("The interrupted start lacks its frozen resolution intent")
            resolution = resolutions[0]
            if resolution["hash"] != content_hash(
                {key: value for key, value in resolution.items() if key != "hash"}
            ):
                raise ValueError("The interrupted execution resolution changed")
            study["run_ids"].append(run["run_id"])
            study.setdefault("execution_bindings", []).append(
                {
                    **resolution,
                    "run_id": run["run_id"],
                    "series_id": run["config"]["series_id"],
                }
            )
            self._save(study)
            if registry:
                registry.record_run(run["config"]["series_id"], run["run_id"])
                registry.record_execution(
                    study_id, run["run_id"], cell=tag["cell_id"], seed=tag["seed"]
                )

    async def aclose(self):
        await self._close_life_runners()
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for client in self._clients:
            await client.aclose()
