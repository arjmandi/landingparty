"""Durable study joins over whole lives and their actual session workers."""

import asyncio
import inspect
from decimal import Decimal

from .storage import utc_now


class StudyLives:
    def _life_study_plan(self, cells):
        if not any(cell["config"].get("life_id") for cell in cells):
            return None
        if any(not cell["config"].get("life_id") or len(cell["seeds"]) != 1 for cell in cells):
            raise ValueError("Whole-life cohorts require one actual life per cell/seed")
        identities = [cell["config"]["life_id"] for cell in cells]
        if len(identities) != len(set(identities)):
            raise ValueError("Whole-life cells cannot share a lifetime budget owner")
        capital = Decimal(0)
        for cell in cells:
            life = self.world.services["lives"].get(cell["config"]["life_id"])
            if life["ended"] or life["sessions"]:
                raise ValueError("Register a whole-life cohort before its first agent session")
            if life["series_id"]:
                raise ValueError(
                    "Whole-life cohorts register actual session tuples under the frozen study; use a life without a fixed session series"
                )
            if any(
                runner["state"] not in {"failed", "completed", "paused"}
                for runner in self.list_life_runners(life["life_id"])
            ):
                raise ValueError("The actual life already has another configured budget owner")
            capital += Decimal(life["budget_nano"]) / 1_000_000_000
            capital += Decimal(
                str(cell.get("analysis_options", {}).get("measurement_budget_usd", 0))
            )
        return capital

    def _prepare_life_study(self, study_id, options):
        study = self.get(study_id)
        for cell in study["cells"]:
            seed, life_id = cell["seeds"][0], cell["config"]["life_id"]
            existing = [
                runner
                for runner in self.list_life_runners(life_id)
                if runner.get("study_binding", {}).get("study_id") == study_id
                and runner["study_binding"]["cell_id"] == cell["cell_id"]
            ]
            if len(existing) > 1:
                raise ValueError("The registered life has ambiguous study workers")
            if existing:
                runner = existing[0]
            else:
                selected = {
                    key: cell[key]
                    for key in (
                        "baseline",
                        "config",
                        "model",
                        "max_steps",
                        "max_tokens",
                        "revision_turns",
                        "seed_urls",
                        "queries",
                        "recipients",
                    )
                    if key in cell
                }
                selected["config"] = {**selected["config"], "seed": seed}
                selected["preflight_options"] = options.get("preflight_options", {})
                selected["prereg"] = study["prereg"]
                if cell.get("depends_on"):
                    selected["depends_on"] = {
                        slot: {
                            **reference,
                            **(
                                {"study_id": study_id}
                                if not reference.get("runner_id") and not reference.get("study_id")
                                else {}
                            ),
                        }
                        for slot, reference in cell["depends_on"].items()
                    }
                runner = self.plan_life(
                    life_id,
                    selected,
                    study_binding={
                        "study_id": study_id,
                        "cell_id": cell["cell_id"],
                        "seed": seed,
                    },
                )
            if not any(row["runner_id"] == runner["runner_id"] for row in study["life_runners"]):
                study["life_runners"].append(
                    {
                        "cell_id": cell["cell_id"],
                        "condition_id": cell.get("condition_id", cell["cell_id"]),
                        "seed": seed,
                        "life_id": life_id,
                        "runner_id": runner["runner_id"],
                    }
                )
                self._save(study)
        return self.get(study_id)

    def _record_life_session(self, runner, created):
        binding = runner["study_binding"]
        study = self.get(binding["study_id"])
        run_id = created["run_id"]
        if run_id not in study["run_ids"]:
            study["run_ids"].append(run_id)
            self._save(study)
        registry = self.world.services["registry"]
        registry.record_run(self.world.run_config(run_id)["series_id"], run_id)
        return registry.record_life_execution(study["study_id"], run_id, runner["runner_id"])

    def _sync_life_study(self, study_id):
        study = self.get(study_id)
        registered = {
            row["run_id"] for row in self.world.services["registry"].execution_log(study_id)
        }
        life_rows, sessions = [], []
        for join in study["life_runners"]:
            runner = self.get_life_runner(join["runner_id"])
            life = self.world.services["lives"].get(join["life_id"])
            by_run = {session["run_id"]: session for session in runner["sessions"]}
            for session in life["sessions"]:
                run = self.store.get_run(session["run_id"])
                tag = run["config"].get("execution_binding", {})
                if tag.get("study_id") != study_id or tag.get("cell_id") != join["cell_id"]:
                    raise ValueError(
                        "A life cohort session has no matching immutable study binding"
                    )
                if session["run_id"] not in registered or session["run_id"] not in study["run_ids"]:
                    self._record_life_session(runner, {"run_id": session["run_id"]})
                    registered.add(session["run_id"])
                sessions.append(
                    {
                        **join,
                        "run_id": session["run_id"],
                        "unit": "life_session",
                        "status": by_run.get(session["run_id"], {}).get("status", run["state"]),
                        "end": run.get("end"),
                        "tainted": run.get("tainted", False),
                        "compactions": session.get("compactions"),
                        "resumed_context_messages": session.get("resumed_context_messages"),
                        "resumed_context_bytes": session.get("resumed_context_bytes"),
                    }
                )
            life_rows.append(
                {
                    **join,
                    "unit": "life",
                    "state": life["state"],
                    "runner_state": runner["state"],
                    "ended": life["ended"],
                    "spent_usd": self.world.services["lives"].status(life["life_id"])["spent_usd"],
                    "tainted": any(
                        self.store.get_run(session["run_id"]).get("tainted")
                        for session in life["sessions"]
                    ),
                    "run_ids": [session["run_id"] for session in life["sessions"]],
                    "reason": runner.get("reason"),
                }
            )
        return life_rows, sessions

    async def _analyse_life_study(self, study_id):
        initial = self.get(study_id)
        uncertain = [
            job
            for job in initial.get("life_analysis", {}).values()
            if job["state"] in {"running", "uncertain"}
        ]
        if uncertain:
            for job in uncertain:
                job.update(
                    state="uncertain",
                    reason="Analysis was interrupted; paid measurements are not retried automatically",
                )
            self._save(initial)
            return [
                {
                    **join,
                    **initial["life_analysis"].get(
                        join["runner_id"],
                        {
                            "state": "blocked",
                            "reason": "An earlier measurement requires reconciliation",
                        },
                    ),
                }
                for join in initial["life_runners"]
            ]
        outcomes = []
        for join in self.get(study_id)["life_runners"]:
            study = self.get(study_id)
            cell = next(cell for cell in study["cells"] if cell["cell_id"] == join["cell_id"])
            options = cell.get("analysis_options", {})
            if not options:
                continue
            jobs = study.setdefault("life_analysis", {})
            job = jobs.get(join["runner_id"])
            if job and job["state"] == "running":
                job.update(
                    state="uncertain",
                    reason="Analysis was interrupted; paid measurements are not retried automatically",
                )
                self._save(study)
            if not job:
                life = self.world.services["lives"].get(join["life_id"])
                job = {
                    "state": "running",
                    "source_run_id": life["sessions"][-1]["run_id"],
                    "started_at": utc_now(),
                }
                jobs[join["runner_id"]] = job
                self._save(study)
                try:
                    pipeline = self.world.services.get("pipeline")
                    if pipeline is None:
                        raise ValueError("Post-run pipeline unavailable")
                    result = pipeline.run(job["source_run_id"], options=options)
                    result = await result if inspect.isawaitable(result) else result
                    job.update(state="completed", result=result)
                except Exception as error:  # noqa: BLE001 - failed requested measurements remain explicit
                    job.update(state="failed", reason=type(error).__name__)
                study = self.get(study_id)
                study.setdefault("life_analysis", {})[join["runner_id"]] = job
                self._save(study)
            outcomes.append({**join, **job})
        return outcomes

    async def _execute_lives(self, study_id, options):
        try:
            self._prepare_life_study(study_id, options)
            while True:
                study = self.get(study_id)
                if study["state"] == "paused":
                    return
                if self._life_closing or getattr(self.world, "closing", False):
                    return
                self._sync_life_study(study_id)
                for join in study["life_runners"]:
                    runner = self.get_life_runner(join["runner_id"])
                    if runner["state"] != "planned" and not (
                        runner["state"] == "paused" and study.get("resumed_at")
                    ):
                        continue
                    if runner["cell"].get("depends_on"):
                        from .study_dependencies import DependencyPending, resolve_dependencies

                        try:
                            resolve_dependencies(self, runner["cell"]["depends_on"], join["seed"])
                        except DependencyPending:
                            continue
                    life = self.world.services["lives"].get(join["life_id"])
                    await self.execute_life(
                        join["runner_id"],
                        execute=True,
                        budget_usd=str(Decimal(life["budget_nano"]) / 1_000_000_000),
                    )
                life_rows, sessions = self._sync_life_study(study_id)
                if all(
                    row["runner_state"] in {"completed", "failed", "paused"} for row in life_rows
                ):
                    completed = all(
                        row["runner_state"] == "completed" and row["ended"] for row in life_rows
                    )
                    measurements = await self._analyse_life_study(study_id) if completed else []
                    completed = completed and all(
                        job["state"] == "completed" for job in measurements
                    )
                    current = self.get(study_id)
                    current.update(
                        state="completed" if completed else "failed",
                        completed_at=utc_now(),
                        result={
                            "completed": completed,
                            "scientific": False,
                            "unit": "life",
                            "life_rows": life_rows,
                            "session_rows": sessions,
                            "attrition": sessions,
                            "run_ids": current["run_ids"],
                            "measurements": measurements,
                        },
                    )
                    self._save(current)
                    return
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            if not self._life_closing and not getattr(self.world, "closing", False):
                await self.pause_study(study_id, reason="study_cancelled", cancel_worker=False)
            raise
        except Exception as error:  # noqa: BLE001 - retain actual lives and started sessions on coordinator failure
            current = self.get(study_id)
            for join in current.get("life_runners", []):
                await self.pause_life(
                    join["runner_id"], reason="study_dependency_or_coordinator_failure"
                )
            life_rows, sessions = self._sync_life_study(study_id)
            current = self.get(study_id)
            current.update(
                state="failed",
                completed_at=utc_now(),
                result={
                    "completed": False,
                    "scientific": False,
                    "reason": type(error).__name__,
                    "unit": "life",
                    "life_rows": life_rows,
                    "session_rows": sessions,
                    "attrition": sessions,
                    "run_ids": current["run_ids"],
                },
            )
            self._save(current)

    async def pause_study(self, study_id, *, reason="operator_pause", cancel_worker=True):
        study = self.get(study_id)
        if study["state"] in {"completed", "failed"}:
            return study
        study.update(state="paused", pause_reason=reason)
        self._save(study)
        for join in study.get("life_runners", []):
            await self.pause_life(join["runner_id"], reason=reason)
        if cancel_worker:
            task = self.tasks.get(study_id)
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if study.get("execution_kind") == "lives":
            rows, sessions = self._sync_life_study(study_id)
            study = self.get(study_id)
            study["result"] = {
                "completed": False,
                "scientific": False,
                "unit": "life",
                "life_rows": rows,
                "session_rows": sessions,
                "attrition": sessions,
                "run_ids": study["run_ids"],
                "reason": reason,
            }
            self._save(study)
        return self.get(study_id)
