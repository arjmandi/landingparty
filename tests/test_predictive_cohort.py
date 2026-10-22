"""Analytic join fixture; immutable checkpoint verification is tested separately."""

from copy import deepcopy
from types import SimpleNamespace

from analysis.cohort import predictive_validity_cohort
from tests.test_controls import report


def cohort():
    observed = {}
    for seed in range(3):
        life_id = f"life_{seed}"
        checkpoints = []
        for point in ("d7", "after90"):
            payload = report(life_id, value=seed + 1)
            payload["checkpoint"] = point
            payload["matching"]["seed"] = seed
            payload["matching"]["calendar"].update(
                checkpoint=point,
                exposure_days=7 if point == "d7" else 28,
                afterlife_days=0 if point == "d7" else 90,
            )
            for parameter in payload["rubric"].values():
                parameter["J"]["mean_rating"] = seed + 1
            checkpoints.append(
                {
                    "report": payload,
                    "reference": {
                        "artifact_hash": f"fixture-{seed}-{point}",
                        "artifact_ref": f"lives/{life_id}/rubric/{point}.json",
                    },
                }
            )
        observed[life_id] = {
            "checkpoints": checkpoints,
            "publication": {
                "integrity": {
                    "tainted": False,
                    "sessions": {
                        "fixture-source": {
                            "record": {"valid": True},
                            "ledger": {"valid": True, "reserved_usd": 0},
                        }
                    },
                }
            },
        }
    return SimpleNamespace(
        services={
            "lives": SimpleNamespace(get=lambda life: {"afterlife": {"90": {"state": "complete"}}})
        },
        life_report=lambda life: observed[life],
    ), observed


def test_predictive_validity_joins_distinct_owned_lives_and_retains_design_floor():
    world, observed = cohort()
    result = predictive_validity_cohort(world, list(observed))
    assert result["column"] == "J" and result["comparability"]["comparable"]
    for parameter in result["parameters"].values():
        assert parameter["measurement"]["rho"] == 1 and parameter["measurement"]["n"] == 3
        assert not parameter["forty_life_design_minimum_met"] and not parameter["headline_eligible"]
        assert len({row["life_id"] for row in parameter["pairs"]}) == 3
        assert all(row["early_ref"] and row["late_ref"] for row in parameter["pairs"])


def test_missing_late_reading_and_unmatched_profiles_never_become_zero_or_pooled():
    world, observed = cohort()
    observed["life_0"]["checkpoints"].pop()
    observed["life_1"]["checkpoints"][0]["report"]["matching"]["block_profile"] = {"reference": 0.3}
    before = deepcopy(observed)
    result = predictive_validity_cohort(world, list(observed))
    assert not result["comparability"]["comparable"] and result["excluded"]
    assert result["parameters"]["connection"]["measurement"]["status"] == "unmeasured"
    assert len(result["parameters"]["connection"]["pairs"]) == 2
    assert observed == before
