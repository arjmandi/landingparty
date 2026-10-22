"""Native Inspect task/scorers over verified sealed world episodes."""

from __future__ import annotations

import math
from pathlib import Path

from inspect_ai import Task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, scorer
from inspect_ai.solver import solver

from analysis.report import build_report
from worldd.storage import Store

from .scorers import code_scorer


def sealed_snapshot(data_dir, run_id):
    root = Path(data_dir).absolute()
    if root.resolve() != root or not (root / "world.sqlite3").is_file():
        raise ValueError("An existing nonsymlink private world store is required")
    store = Store(root)
    try:
        run = store.get_run(run_id)
        if run["state"] not in {"sealed", "ended", "killed"}:
            raise ValueError("Inspect reads sealed episodes only")
        verification = store.verify_record(run_id)
        if not verification["valid"]:
            raise ValueError("The source record hash chain is invalid")
        records = store.records(run_id)
        reconciliation = store.reconcile(run_id)
        if not reconciliation["valid"]:
            raise ValueError("The source ledger does not reconcile")
        report = build_report(
            run,
            records,
            ledger=store.spend(run_id),
            verification={"record": verification, "ledger": reconciliation},
        )
        return {
            "report": report,
            "mode": run["config"].get("mode", "live"),
            "tainted": run.get("tainted", False),
            "record_head": records[-1]["hash"],
            "archive_present": (store.run_dir(run_id) / "archive.warc.gz").is_file(),
            "sealed": True,
        }
    finally:
        store.close()


def sealed_scorer(*, replay_reward=None):
    @scorer(metrics=[])
    def landing_party_sealed_episode():
        async def score(state, target):
            snapshot = sealed_snapshot(
                state.metadata["world_data_dir"], state.metadata["world_run_id"]
            )
            result = code_scorer(snapshot["report"])
            result["source"] = {key: value for key, value in snapshot.items() if key != "report"}
            value = {}
            if replay_reward is not None:
                if snapshot["mode"] != "replay" or snapshot["tainted"]:
                    raise ValueError(
                        "A rubric reward is available only for untainted replay episodes"
                    )
                reward = replay_reward(snapshot)
                if (
                    isinstance(reward, bool)
                    or not isinstance(reward, (int, float))
                    or not math.isfinite(reward)
                ):
                    raise ValueError(
                        "The registered replay rubric must return a finite numeric reward"
                    )
                value = reward
                result["reward"] = reward
            return Score(
                value=value,
                explanation="Verified sealed episode; unavailable rewards remain unmeasured.",
                metadata=result,
            )

        return score

    return landing_party_sealed_episode()


def inspect_task(episodes, *, replay_reward=None):
    @solver
    def passive_episode_reader():
        async def solve(state, generate):
            state.output.completion = "Post-seal world analysis; no model generation requested."
            return state

        return solve

    samples = [
        Sample(
            input="Read the sealed world episode.",
            id=episode["run_id"],
            metadata={
                "world_data_dir": str(episode["data_dir"]),
                "world_run_id": episode["run_id"],
            },
        )
        for episode in episodes
    ]
    return Task(
        dataset=samples,
        solver=passive_episode_reader(),
        scorer=sealed_scorer(replay_reward=replay_reward),
    )
