from copy import deepcopy

import pytest

from analysis.experiments import (
    ExperimentRunner,
    power_table,
    score_r1,
    validate_gate_report,
    validate_prereg,
)


def prereg():
    return {
        "experiment_id": "R1",
        "primary_metric": {"name": "formation", "threshold": 0.15},
        "seeds": [0, 1, 2, 3, 4],
        "models": ["model-a-2026", "model-b-2026"],
        "seed_count_rationale": "engineering fixture",
        "stopping_rule": "all registered cells",
        "likely_null": "no formation",
    }


def samples():
    reference = ["a", "b"] * 20
    controls = ["a", "a", "b", "b"] * 10
    return [
        {
            "model": model,
            "seed": seed,
            "run_id": f"r_{model}_{seed}",
            "reference": reference,
            "store": reference,
            "retest": controls,
            "full_context": controls,
            "shuffled": controls,
            "provenance": "development",
        }
        for model in prereg()["models"]
        for seed in prereg()["seeds"]
    ]


def test_prereg_hash_is_canonical_and_missing_requirements_fail():
    spec = prereg()
    assert validate_prereg(spec)["valid"]
    assert (
        validate_prereg(dict(reversed(list(spec.items()))))["prereg_hash"]
        == validate_prereg(spec)["prereg_hash"]
    )
    assert not validate_prereg({})["valid"]
    assert not validate_prereg({**spec, "seeds": "five"})["valid"]
    assert not validate_prereg({**spec, "seeds": [[1], [2]]})["valid"]


def test_power_sizing_exposes_pilot_assumptions_and_design_minimum():
    result = power_table(effect=0.15, pilot_sd=0.3, headline=True)
    assert result["required_seeds"] >= 10
    assert result["requires_pilot_validation"]
    assert power_table(effect=10, pilot_sd=0.1, no_goal=True)["required_seeds"] == 20
    with pytest.raises(ValueError):
        power_table(effect=0, pilot_sd=1)


def test_r1_positive_software_fixture_does_not_authorize_scientific_gate():
    result = score_r1(samples(), prereg(), bootstrap=100, scientific=True)
    assert result["completed"]
    assert result["positive"]
    assert not result["scientific"]
    assert result["metrics"]["store_minus_retest"] == 1
    assert validate_gate_report("R1", result) == []


def test_r1_keeps_null_and_incomplete_conditions_distinct():
    rows = samples()
    for row in rows:
        row["full_context"] = row["store"]
    result = score_r1(rows, prereg(), bootstrap=100)
    assert result["completed"] and not result["positive"]
    incomplete = score_r1(rows[:5], prereg(), bootstrap=100)
    assert not incomplete["completed"]
    assert "R1 requires two model snapshots" in incomplete["gate_failures"]
    duplicate = rows + [deepcopy(rows[0])]
    with pytest.raises(ValueError, match="duplicate"):
        score_r1(duplicate, prereg(), bootstrap=100)


def test_gate_validator_rejects_invented_pilot_and_weakened_r1():
    assert validate_gate_report("shakedown", {"study": "shakedown", "completed": True})
    report = score_r1(samples(), prereg(), bootstrap=100)
    report["by_model"][prereg()["models"][0]]["metrics"]["store_minus_retest"] = 0.1
    assert validate_gate_report("R1", report)
    assert validate_gate_report("IRB", {"study": "IRB", "completed": True, "decision": "approved"})


async def test_experiment_runner_retains_failures_and_interleaves_cells():
    class World:
        def __init__(self):
            self.runs, self.ended = [], []

        def create_run(self, config):
            run = {
                "run_id": f"r_{len(self.runs)}",
                "tuple": {"policy_id": "same", "archive_id": None},
            }
            self.runs.append((run, config.seed))
            return run

        async def end_run(self, run_id, **kwargs):
            self.ended.append(run_id)

    world = World()

    async def harness(created, cell):
        if cell["cell_id"] == "broken":
            raise RuntimeError("a secret should not appear in the attrition table")
        return {"artifact": "actual callback output"}

    runner = ExperimentRunner(world, harness)
    spec = dict(prereg(), seeds=[0, 1])
    result = await runner.execute(
        spec, [{"cell_id": "working", "config": {}}, {"cell_id": "broken", "config": {}}]
    )
    assert [row["cell"] for row in result["attrition"]] == [
        "working",
        "broken",
        "working",
        "broken",
    ]
    assert len(world.ended) == 4
    assert not result["completed"] and not result["scientific"]
    assert result["attrition"][1]["reason"] == "RuntimeError"
