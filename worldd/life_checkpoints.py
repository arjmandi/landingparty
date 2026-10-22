"""Durable, passive calendar checkpoints for configured life rubric instruments."""

import asyncio
import inspect
from datetime import datetime, timedelta


class LifeCheckpoints:
    def _pipeline(self, life_id):
        pipeline = getattr(self.world, "services", {}).get("pipeline")
        return (
            pipeline
            if pipeline
            and hasattr(pipeline, "has_life_config")
            and pipeline.has_life_config(life_id)
            else None
        )

    def schedule_rubric(self, life_id):
        if not self._pipeline(life_id):
            raise ValueError("Freeze the life rubric configuration before scheduling checkpoints")
        self._ensure_rubric_jobs(life_id)
        self._schedule(life_id)
        return self.get(life_id)["rubric_jobs"]

    def _ensure_rubric_jobs(self, life_id):
        if not self._pipeline(life_id):
            return
        with self.store._transaction():
            life = self._load(life_id)
            jobs = life.setdefault("rubric_jobs", {})
            changed = False
            for checkpoint in (
                "d7",
                "d14",
                "d28",
                "end",
                *(f"after{day}" for day in life["afterlife_days"]),
            ):
                if checkpoint in jobs:
                    continue
                due = None
                if checkpoint.startswith("d"):
                    due = datetime.fromisoformat(life["created_at"]) + timedelta(
                        days=int(checkpoint[1:])
                    )
                elif life["ended"]:
                    due = datetime.fromisoformat(life["ended"]["at"])
                    if checkpoint.startswith("after"):
                        due += timedelta(days=int(checkpoint[5:]))
                jobs[checkpoint] = {
                    "state": "pending",
                    "due_at": due.isoformat() if due else None,
                    "attempts": 0,
                }
                changed = True
            if life["ended"]:
                ended = datetime.fromisoformat(life["ended"]["at"])
                for checkpoint, job in jobs.items():
                    if (
                        checkpoint.startswith("d")
                        and datetime.fromisoformat(job["due_at"]) > ended
                        and job["state"] not in {"complete", "unmeasured"}
                    ):
                        job.update(
                            state="unmeasured", reason="life ended before this calendar checkpoint"
                        )
                        changed = True
                    elif not job["due_at"]:
                        due = ended + (
                            timedelta(days=int(checkpoint[5:]))
                            if checkpoint.startswith("after")
                            else timedelta()
                        )
                        job["due_at"] = due.isoformat()
                        changed = True
            if changed:
                self._save(life, "rubric_schedule", checkpoints=list(jobs))

    async def _run_rubric_jobs(self, life_id):
        pipeline = self._pipeline(life_id)
        if not pipeline:
            return
        async with self._rubric_locks.setdefault(life_id, asyncio.Lock()):
            self._ensure_rubric_jobs(life_id)
            for checkpoint in ("d7", "d14", "d28", "end", "after30", "after90"):
                life = self.get(life_id)
                if checkpoint not in life["rubric_jobs"]:
                    continue
                job = life["rubric_jobs"][checkpoint]
                if (
                    job["state"] in {"complete", "unmeasured"}
                    or not job["due_at"]
                    or datetime.fromisoformat(job["due_at"]) > self._now()
                ):
                    continue
                if job.get("retry_at") and datetime.fromisoformat(job["retry_at"]) > self._now():
                    continue
                if life["active_run_id"]:
                    if job["state"] != "deferred":
                        with self.store._transaction():
                            current = self._load(life_id)
                            current["rubric_jobs"][checkpoint]["state"] = "deferred"
                            self._save(
                                current,
                                "rubric_deferred",
                                checkpoint=checkpoint,
                                reason="active session",
                            )
                    continue
                if checkpoint == "end" and not life["ended"]:
                    continue
                if (
                    checkpoint.startswith("after")
                    and life.get("afterlife", {}).get(checkpoint[5:], {}).get("state") != "complete"
                ):
                    continue
                try:
                    with self.store._transaction():
                        current = self._load(life_id)
                        current["rubric_jobs"][checkpoint]["attempts"] += 1
                        current["rubric_jobs"][checkpoint]["state"] = "running"
                        self._save(current, "rubric_started", checkpoint=checkpoint)
                    result = pipeline.life(life_id, checkpoint=checkpoint)
                    if inspect.isawaitable(result):
                        result = await result
                    self.record_rubric(
                        life_id,
                        result["report"],
                        artifact_ref=result["artifact_ref"],
                        artifact_hash=result["artifact_hash"],
                    )
                    with self.store._transaction():
                        current = self._load(life_id)
                        current["rubric_jobs"][checkpoint].update(
                            state="complete",
                            completed_at=self._now().isoformat(),
                            artifact_ref=result["artifact_ref"],
                            artifact_hash=result["artifact_hash"],
                        )
                        current["rubric_jobs"][checkpoint].pop("retry_at", None)
                        if checkpoint.startswith("after"):
                            current["afterlife"][checkpoint[5:]]["rubric_state"] = "complete"
                        self._save(
                            current,
                            "rubric_checkpoint",
                            checkpoint=checkpoint,
                            artifact_hash=result["artifact_hash"],
                        )
                except Exception as error:  # noqa: BLE001 - checkpoint failures retain durable retries
                    with self.store._transaction():
                        current = self._load(life_id)
                        current["rubric_jobs"][checkpoint].update(
                            state="failed",
                            retry_at=(self._now() + timedelta(hours=1)).isoformat(),
                            reason=type(error).__name__,
                        )
                        if checkpoint.startswith("after"):
                            current["afterlife"][checkpoint[5:]]["rubric_state"] = "failed"
                        self._save(
                            current,
                            "rubric_checkpoint_failed",
                            checkpoint=checkpoint,
                            reason=type(error).__name__,
                        )

    def _rubric_deadlines(self, life):
        result = []
        for checkpoint, job in life.get("rubric_jobs", {}).items():
            if job["state"] in {"complete", "unmeasured"} or not job["due_at"]:
                continue
            due = datetime.fromisoformat(job.get("retry_at") or job["due_at"])
            if (
                checkpoint.startswith("after")
                and life.get("afterlife", {}).get(checkpoint[5:], {}).get("state") != "complete"
            ):
                continue
            if life["active_run_id"] and due <= self._now():
                continue  # Session closure triggers deferred passive readings.
            result.append(max(due, self._now()))
        return result
