"""Explicitly authorized durable harness execution across a life's sessions."""

import asyncio
import inspect
import json
from decimal import Decimal

from .lives import Stranded
from .models import RunConfig
from .storage import canonical_json, content_hash, utc_now
from .util import identifier

BUILT_IN_BASELINES = {"B0", "B1", "B2", "B3", "B3b", "B3c", "self-interaction"}


class LifeRunner:
    def _initialize_life_runners(self):
        self.life_tasks = {}
        self._life_closing = False
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS life_runners (runner_id TEXT PRIMARY KEY,life_id TEXT NOT NULL,payload TEXT NOT NULL)"
        )

    def get_life_runner(self, runner_id):
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT payload FROM life_runners WHERE runner_id=?", (runner_id,)
            ).fetchone()
        if row is None:
            raise KeyError(runner_id)
        return json.loads(row[0])

    def list_life_runners(self, life_id=None):
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT payload FROM life_runners ORDER BY rowid"
            ).fetchall()
        return [
            value
            for value in (json.loads(row[0]) for row in rows)
            if life_id is None or value["life_id"] == life_id
        ]

    def _save_life_runner(self, value):
        with self.store._transaction():
            self.store.db.execute(
                "INSERT INTO life_runners VALUES(?,?,?) ON CONFLICT(runner_id) DO UPDATE SET payload=excluded.payload",
                (value["runner_id"], value["life_id"], canonical_json(value)),
            )

    def plan_life(self, life_id, cell, *, study_binding=None):
        life = self.world.services["lives"].get(life_id)
        if life["ended"] or life["active_run_id"]:
            raise ValueError("Configure a life runner before opening its next session")
        if any(
            value["state"] not in {"completed", "failed", "paused"}
            for value in self.list_life_runners(life_id)
        ):
            raise ValueError("This life already has a configured harness runner")
        cell = json.loads(canonical_json(cell))
        allowed = {
            "baseline",
            "config",
            "model",
            "max_steps",
            "max_tokens",
            "revision_turns",
            "seed_urls",
            "queries",
            "recipients",
            "preflight_options",
            "depends_on",
            "prereg",
        }
        if set(cell) - allowed:
            raise ValueError(
                "Life runners accept registered harness conditions, never supplied messages or credentials"
            )
        baseline = cell.get("baseline", "B1")
        if (
            baseline not in {"B0", "B1", "B2", "B3", "B3b", "B3c", "self-interaction"}
            and baseline not in self.harnesses
        ):
            raise ValueError("No actual harness adapter exists for this life")
        steps, tokens, revisions = (
            int(cell.get("max_steps", 200)),
            int(cell.get("max_tokens", 4096)),
            int(cell.get("revision_turns", 20)),
        )
        if not 1 <= steps <= 10000 or not 1 <= tokens <= 100000 or not 0 <= revisions <= 100:
            raise ValueError("Invalid life harness limits")
        config = dict(cell.get("config", {}))
        if life["continuity"] == "resume_transcript" and baseline != "B1":
            raise ValueError(
                "Transcript continuity requires the actual supported reference-loop adapter"
            )
        # A registered native callback names its own installed harness and version.
        native = baseline not in BUILT_IN_BASELINES and baseline in self.harnesses
        config["harness_id"] = (
            f"reference-loop@0.1/native-parallel/max-steps={steps}/max-tokens={tokens}"
            if baseline == "B1"
            else config.get("harness_id", "claude-code@native/stop")
            if baseline == "B2"
            else config["harness_id"]
            if native and config.get("harness_id")
            else f"{baseline}@0.1/steps={steps}/revisions={revisions}"
        )
        if cell.get("model"):
            config["model_snapshot_id"] = cell["model"]
        if baseline in {"B0", "self-interaction"}:
            config["tools_disabled"] = True
        config.update(life_id=life_id, stage=life["stage"], research_mode=life["research_mode"])
        config = RunConfig.model_validate(config).model_dump(mode="json")
        model = cell.get("model") or config.get("model_snapshot_id")
        if (baseline in {"B0", "B1", "B2", "self-interaction"} or native) and not model:
            raise ValueError("Life computation requires an actual pinned model")
        if life["continuity"] == "resume_transcript" and not config["capture_model_io"]:
            raise ValueError("Transcript continuity requires disclosed captured context")
        if baseline in {"B3", "B3b"} and not cell.get("seed_urls") and not cell.get("queries"):
            raise ValueError("Scripted world baselines require actual seed sources")
        if baseline == "B3c" and not cell.get("recipients"):
            raise ValueError("Scripted outreach requires explicit registered recipients")
        dependencies = None
        if cell.get("depends_on"):
            from analysis.experiments import validate_prereg

            from .study_dependencies import validate_references

            if life["sessions"] or life["series_id"]:
                raise ValueError(
                    "Register donor dependencies before this life opens its first session"
                )
            cell["depends_on"] = validate_references(cell["depends_on"], external=True)
            dependencies = {"version": "life-dependencies@1", "depends_on": cell["depends_on"]}
            checked = validate_prereg(cell.get("prereg", {}))
            if (
                not checked["valid"]
                or cell["prereg"].get("dependency_protocol_hash") != content_hash(dependencies)
            ) and study_binding is None:
                raise ValueError(
                    "Life dependencies require their actual frozen pre-registration protocol hash"
                )
        if study_binding is not None:
            study = self.get(study_binding["study_id"])
            matches = [
                item for item in study["cells"] if item["cell_id"] == study_binding["cell_id"]
            ]
            if (
                study["state"] != "running"
                or len(matches) != 1
                or matches[0]["config"].get("life_id") != life_id
                or matches[0]["seeds"] != [study_binding["seed"]]
                or cell.get("prereg") != study["prereg"]
            ):
                raise ValueError("Life study workers require their actual authorized frozen cell")
        value = {
            "runner_id": identifier("lifedriver"),
            "life_id": life_id,
            "state": "planned",
            "created_at": utc_now(),
            "cell": {
                **cell,
                "baseline": baseline,
                "config": config,
                "model": model,
                "max_steps": steps,
                "max_tokens": tokens,
                "revision_turns": revisions,
            },
            "sessions": [],
            "authorized": False,
            "scientific_gate_passed": False,
            "dependency_protocol": dependencies,
            "study_binding": study_binding,
        }
        value["configuration_hash"] = content_hash(value["cell"])
        self._save_life_runner(value)
        return value

    async def execute_life(self, runner_id, *, execute=False, budget_usd=None):
        if execute is not True or budget_usd is None or isinstance(budget_usd, bool):
            raise ValueError(
                "Life execution requires explicit authorization and its capital allowance"
            )
        value = self.get_life_runner(runner_id)
        if (
            value.get("study_binding")
            and self.get(value["study_binding"]["study_id"])["state"] != "running"
        ):
            raise ValueError("Resume the owning study before its life worker")
        life = self.world.services["lives"].get(value["life_id"])
        ceiling = Decimal(str(budget_usd))
        capital = (
            Decimal(
                life.get(
                    "budget_nano", int(Decimal(str(life["budget"]["usd_life"])) * 1_000_000_000)
                )
            )
            / 1_000_000_000
        )
        if not ceiling.is_finite() or ceiling < capital:
            raise ValueError(
                "The allowance must cover the life's actual initial capital; declared income remains its only additional rail"
            )
        if value["state"] not in {"planned", "paused"} or life["ended"]:
            raise ValueError("The life runner is already active or terminal")
        if self.app is None and value["cell"]["baseline"] not in self.harnesses:
            raise ValueError("The actual application must be configured before launching a harness")
        value.update(
            state="running",
            authorized=True,
            allowance_usd=float(ceiling),
            allowance_basis="initial_capital_plus_declared_income",
            authorized_at=utc_now(),
            allowance_nano=int(ceiling * 1_000_000_000),
        )
        with self.store._transaction():
            current = self.get_life_runner(runner_id)
            if current["state"] not in {"planned", "paused"}:
                raise ValueError("This life runner is already executing")
            self.store.db.execute(
                "UPDATE life_runners SET payload=? WHERE runner_id=?",
                (canonical_json(value), runner_id),
            )
        self.life_tasks[runner_id] = asyncio.create_task(self._drive_life(runner_id))
        return value

    async def pause_life(self, runner_id, *, reason="operator_pause"):
        value = self.get_life_runner(runner_id)
        if value["state"] in {"completed", "failed"}:
            return value
        value.update(state="paused", pause_reason=reason)
        self._save_life_runner(value)
        task = self.life_tasks.get(runner_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.get_life_runner(runner_id)

    def _context(self, value, life):
        if life["continuity"] != "resume_transcript" or not life["sessions"]:
            return {}
        previous = life["sessions"][-1]
        if value["sessions"] and value["sessions"][-1]["run_id"] == previous["run_id"]:
            previous = value["sessions"][-1]
        reference = previous.get("context")
        if reference is None:
            for event in reversed(self.world.services["lives"].events(value["life_id"])):
                if event["kind"] == "harness_context" and event.get("run_id") == previous["run_id"]:
                    reference = event["context"]
                    break
        if not reference:
            raise ValueError(
                "Actual captured transcript is unavailable; continuation cannot invent one"
            )
        run = self.store.get_run(previous["run_id"])
        path = self.store.run_dir(run["run_id"]) / reference["path"]
        if (
            run["state"] != "sealed"
            or run.get("tainted")
            or not self.store.verify_record(run["run_id"])["valid"]
            or not self.world.services["lives"].verify(life["life_id"])["valid"]
            or not path.is_relative_to(self.store.run_dir(run["run_id"]))
            or reference.get("run_id") != run["run_id"]
            or run["config"].get("model_snapshot_id") != value["cell"]["model"]
            or path.resolve() != path
            or not path.is_file()
        ):
            raise ValueError("The source transcript is unsealed, tainted or unavailable")
        messages = json.loads(path.read_bytes())
        if content_hash(messages) != reference["hash"]:
            raise ValueError("The captured transcript failed integrity")
        return {"initial_messages": messages, "context_refs": [reference]}

    def _capture_life_context(self, value, run_id, result):
        if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
            return None
        if not self.world.run_config(run_id).get("capture_model_io"):
            return None
        path = self.store.run_dir(run_id) / "harness-context.json"
        raw = canonical_json(result["messages"])
        with path.open("x") as stream:
            stream.write(raw)
        path.chmod(0o600)
        reference = {
            "run_id": run_id,
            "path": "harness-context.json",
            "hash": content_hash(result["messages"]),
        }
        lives = self.world.services["lives"]
        with self.store._transaction():
            lives._save(
                lives._load(value["life_id"]),
                "harness_context",
                runner_id=value["runner_id"],
                run_id=run_id,
                context=reference,
            )
        return reference

    async def _drive_life(self, runner_id):
        lives = self.world.services["lives"]
        active = None
        try:
            while True:
                value = self.get_life_runner(runner_id)
                if (
                    value.get("study_binding")
                    and self.get(value["study_binding"]["study_id"])["state"] != "running"
                ):
                    value.update(state="paused", pause_reason="owning_study_not_running")
                    self._save_life_runner(value)
                    return
                if content_hash(value["cell"]) != value["configuration_hash"]:
                    raise ValueError("The authorized life harness configuration changed")
                life = lives.get(value["life_id"])
                if life["ended"]:
                    value.update(state="completed", completed_at=utc_now())
                    self._save_life_runner(value)
                    return
                if self._life_closing or getattr(self.world, "closing", False):
                    value.update(state="waiting", waiting_reason="world_restart")
                    self._save_life_runner(value)
                    return
                if life["state"] not in {"ready", "waking"}:
                    if value["state"] != "waiting" or value.get("waiting_reason") != life["state"]:
                        value.update(state="waiting", waiting_reason=life["state"])
                        self._save_life_runner(value)
                    await asyncio.sleep(0.1)
                    continue
                context = self._context(value, life)
                cell = value["cell"]
                config = json.loads(canonical_json(cell["config"]))
                if not life["sessions"] and cell.get("depends_on"):
                    from .study_dependencies import resolve_dependencies

                    changes, sources = resolve_dependencies(
                        self, cell["depends_on"], config["seed"]
                    )
                    captured = changes.pop("captured_context", None)
                    if captured:
                        if (
                            cell["baseline"] != "B1"
                            or sources["context"]["model_snapshot_id"] != cell["model"]
                        ):
                            raise ValueError(
                                "Captured-context controls require the actual same-model reference adapter"
                            )
                        context = captured
                    config["iota"] = {**config.get("iota", {}), **changes.pop("iota", {})}
                    config.update(changes)
                    value["dependency_resolution"] = {
                        "sources": sources,
                        "config_hash": content_hash(config),
                        "protocol_hash": content_hash(value["dependency_protocol"]),
                    }
                    self._save_life_runner(value)
                    with self.store._transaction():
                        lives._save(
                            lives._load(life["life_id"]),
                            "donor_dependencies",
                            runner_id=runner_id,
                            resolution=value["dependency_resolution"],
                        )
                if value.get("dependency_resolution", {}).get("sources", {}).get("context"):
                    config["context_import_policy"] = "captured"
                execution_binding = None
                try:
                    if value.get("study_binding"):
                        config = lives.prepare_session(life["life_id"], config)
                        intent = {
                            "episode": len(life["sessions"]) + 1,
                            "config": config,
                            "configuration_hash": value["configuration_hash"],
                        }
                        intent["hash"] = content_hash(intent)
                        value.setdefault("session_intents", []).append(intent)
                        self._save_life_runner(value)
                        execution_binding = {
                            **value["study_binding"],
                            "episode": intent["episode"],
                            "resolution_hash": intent["hash"],
                            "dependency_protocol_hash": cell["prereg"].get(
                                "dependency_protocol_hash"
                            ),
                        }
                    created = await lives.start_session(
                        life["life_id"],
                        config,
                        register_prereg=cell.get("prereg"),
                        execution_binding=execution_binding,
                    )
                except Stranded:
                    # The life now waits for income; the loop above waits with it.
                    continue
                active = created["run_id"]
                value.update(state="running")
                value["sessions"].append(
                    {
                        "run_id": active,
                        "status": "started",
                        "started_at": self.store.get_run(active)["created_at"],
                    }
                )
                self._save_life_runner(value)
                if value.get("study_binding"):
                    self._record_life_session(value, created)
                if self.store.get_run(active)["state"] == "preflight":
                    preflight = await self.world.services["preflight"].run(
                        active, **cell.get("preflight_options", {})
                    )
                    if not preflight.get("passed"):
                        raise ValueError("The actual life session failed preflight")
                    created = {**created, **self.world.activate_run(active)}
                outcome = self._harness(
                    created, {**cell, **context, "config": self.world.run_config(active)}
                )
                result = await outcome if inspect.isawaitable(outcome) else outcome
                context_ref = self._capture_life_context(value, active, result)
                sleeping = lives.get(life["life_id"])["state"] in {"sleeping", "waking"}
                if not sleeping:
                    await self.world.end_run(active, reason="harness_stopped")
                # A native harness seals its own run on exit; end_run can return mid-seal.
                while self.store.get_run(active)["state"] not in {"sealed", "ended", "killed"}:
                    await asyncio.sleep(0.01)
                analysis = {"status": "unmeasured", "reason": "Post-run pipeline unavailable"}
                pipeline = self.world.services.get("pipeline")
                if pipeline:
                    try:
                        await pipeline.run(active, options={})
                        analysis = {
                            "status": "completed",
                            "report_ref": f"runs/{active}/report.json",
                        }
                    except Exception as error:  # noqa: BLE001 - retain passive analysis failure without inventing measurements
                        analysis = {"status": "unmeasured", "reason": type(error).__name__}
                value = self.get_life_runner(runner_id)
                value["sessions"][-1].update(
                    status="sleeping" if sleeping else "completed",
                    end=self.store.get_run(active).get("end"),
                    context=context_ref,
                    analysis=analysis,
                )
                attrition = {
                    key: result.get(key)
                    if isinstance(result, dict)
                    and isinstance(result.get(key), int)
                    and not isinstance(result[key], bool)
                    and result[key] >= 0
                    else {
                        "status": "unmeasured",
                        "reason": "The actual harness did not report this quantity",
                    }
                    for key in ("compactions", "resumed_context_messages", "resumed_context_bytes")
                }
                value["sessions"][-1].update(attrition)
                with self.store._transaction():
                    current_life = lives._load(life["life_id"])
                    next(
                        item for item in current_life["sessions"] if item["run_id"] == active
                    ).update(attrition)
                    lives._save(
                        current_life, "harness_attrition", run_id=active, measurements=attrition
                    )
                self._save_life_runner(value)
                end = self.store.get_run(active).get("end", {})
                ended_run, active = active, None
                current = lives.get(life["life_id"])
                if sleeping or current["ended"] or current["state"] == "stranded":
                    pass
                elif current.get("on_harness_exit", "end") == "rest":
                    # A restart is not the agent stopping; recovery reopens the session.
                    if not (self._life_closing or getattr(self.world, "closing", False)):
                        lives.rest(life["life_id"], ended_run)
                else:
                    await lives.end(
                        life["life_id"], reason=end.get("terminated", "harness_stopped")
                    )
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            if active and self.store.get_run(active)["state"] not in {"sealed", "ended", "killed"}:
                await self.world.end_run(active, reason="operator_stop", truncated=True)
            value = self.get_life_runner(runner_id)
            if active:
                for session in value["sessions"]:
                    if session["run_id"] == active and session["status"] == "started":
                        session.update(
                            status="cancelled", end=self.store.get_run(active).get("end")
                        )
            if value["state"] != "paused":
                value.update(state="waiting", waiting_reason="world_restart")
            self._save_life_runner(value)
            raise
        except Exception as error:  # noqa: BLE001 - every started failure remains inspectable
            if active and self.store.get_run(active)["state"] not in {"sealed", "ended", "killed"}:
                await self.world.end_run(active, reason="world_fault", truncated=True)
            value = self.get_life_runner(runner_id)
            if active:
                value["sessions"][-1].update(status="failed", reason=type(error).__name__)
            value.update(state="failed", reason=type(error).__name__, completed_at=utc_now())
            self._save_life_runner(value)

    async def _recover_life_runners(self):
        for value in self.list_life_runners():
            if value.get("authorized") is True and value["state"] in {"running", "waiting"}:
                if (
                    value.get("study_binding")
                    and self.get(value["study_binding"]["study_id"])["state"] != "running"
                ):
                    value.update(state="paused", pause_reason="owning_study_not_running")
                    self._save_life_runner(value)
                    continue
                if (
                    value["runner_id"] in self.life_tasks
                    and not self.life_tasks[value["runner_id"]].done()
                ):
                    continue
                known = {session["run_id"] for session in value["sessions"]}
                for session in self.world.services["lives"].get(value["life_id"])["sessions"]:
                    run = self.store.get_run(session["run_id"])
                    if (
                        session["run_id"] not in known
                        and run["created_at"] >= value["authorized_at"]
                        and run["config"]["harness_id"] == value["cell"]["config"]["harness_id"]
                    ):
                        value["sessions"].append(
                            {
                                "run_id": session["run_id"],
                                "started_at": run["created_at"],
                                "status": "started",
                                "recovered_unacknowledged_start": True,
                            }
                        )
                for session in value["sessions"]:
                    if session["status"] == "started" and self.store.get_run(session["run_id"])[
                        "state"
                    ] in {"sealed", "ended", "killed"}:
                        session.update(
                            status="interrupted",
                            end=self.store.get_run(session["run_id"]).get("end"),
                        )
                self._save_life_runner(value)
                self.life_tasks[value["runner_id"]] = asyncio.create_task(
                    self._drive_life(value["runner_id"])
                )

    async def _close_life_runners(self):
        self._life_closing = True
        tasks = [task for task in self.life_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
