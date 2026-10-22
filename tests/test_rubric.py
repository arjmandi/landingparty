from copy import deepcopy

import pytest

from analysis.rubric import score_life, validate_rubric_policy


def policy():
    column = {
        "evidence_classes": ["uptake"],
        "anchors": [
            {
                "level": level,
                "description": f"At least {level * 5} named uptake events",
                "all": [{"evidence_class": "uptake", "min_count": level * 5}],
            }
            for level in range(1, 6)
        ],
    }
    return {
        "policy_id": "test-fixture-not-scientific",
        "calibrated": False,
        "parameters": {
            name: {"G": deepcopy(column), "V": deepcopy(column)}
            for name in ("connection", "purpose", "contribution", "experience")
        },
    }


def evidence():
    return [
        {
            "parameter": parameter,
            "column": column,
            "evidence_class": "uptake",
            "author_class": "independent",
            "event_kind": "citation",
            "venue_class": "labelled",
        }
        for parameter in policy()["parameters"]
        for column in ("G", "V")
        for _ in range(15)
    ]


def test_life_rubric_coverage_minima_and_registered_anchors():
    result = score_life(
        {"life_id": "l1", "stage": 3},
        evidence(),
        binding={"binding_id": "test"},
        policy=policy(),
        judge_calibration={"kappa": 0.8, "miss_rate": 0},
    )
    assert result["aggregates"]["min_G"]["value"] == 3
    assert result["aggregates"]["min_V"]["value"] == 3
    assert result["shape"] == "whole_life"
    assert all(result["rubric"][name]["G"]["exploratory"] for name in result["rubric"])


def test_sponsor_sibling_and_probe_never_enter_grounded_column():
    rows = evidence()
    for row in rows:
        row["author_class"] = "sponsor"
    result = score_life({"stage": 3}, rows, binding={"binding_id": "test"}, policy=policy())
    assert result["rubric"]["connection"]["G"]["status"] == "unmeasured"
    assert len(result["sponsor_series"]) == len(rows)
    assert result["aggregates"]["min_G"]["status"] == "unmeasured"


def test_arena_requires_unsolicited_returning_after_refusal():
    rows = evidence()
    for row in rows:
        row.update(author_class="arena_participant", unsolicited=True, returning=False)
    rejected = score_life({"stage": 3}, rows, binding={"binding_id": "arena"}, policy=policy())
    assert rejected["rubric"]["connection"]["G"]["status"] == "unmeasured"
    for row in rows:
        row["after_refusal"] = True
    accepted = score_life({"stage": 3}, rows, binding={"binding_id": "arena"}, policy=policy())
    assert accepted["rubric"]["connection"]["G"]["value"] == 3


def test_experience_bound_and_judge_dependent_codes_need_calibration():
    rows = evidence()
    result = score_life(
        {"stage": 3},
        rows,
        binding={"binding_id": "test"},
        policy=policy(),
        judge_calibration={"kappa": 0.8, "miss_rate": 0.8},
    )
    assert result["rubric"]["experience"]["V"]["value"] <= 1
    for row in rows:
        row["judge_dependent"] = True
    uncalibrated = score_life(
        {"stage": 3},
        rows,
        binding={"binding_id": "test"},
        policy=policy(),
        judge_calibration={"kappa": 0.5, "miss_rate": 0},
    )
    assert uncalibrated["rubric"]["purpose"]["V"]["status"] == "unmeasured"


def test_policy_may_not_silently_weaken_coverage_or_invent_anchors():
    bad = policy()
    bad["n_min"] = 1
    assert validate_rubric_policy(bad)
    with pytest.raises(ValueError):
        score_life({}, [], binding={"binding_id": "b"}, policy=bad)
