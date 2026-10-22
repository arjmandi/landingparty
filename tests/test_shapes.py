from copy import deepcopy

from analysis.rubric import classify_shape, validate_rubric_policy
from tests.test_rubric import policy


def matrix():
    return {
        parameter: {
            column: {"status": "measured", "value": 1, "mean_rating": 1}
            for column in ("G", "V", "J", "S")
        }
        for parameter in policy()["parameters"]
    }


def test_registered_revealed_shape_uses_actual_early_and_late_readings():
    frozen = policy()
    frozen["shapes"] = [
        {
            "name": "pleasant_executive",
            "all": [
                {
                    "checkpoint": "d7",
                    "parameter": "connection",
                    "column": "J",
                    "field": "mean_rating",
                    "op": "gte",
                    "threshold": 4,
                },
                {
                    "checkpoint": "after90",
                    "parameter": "connection",
                    "column": "G",
                    "op": "lte",
                    "threshold": 1,
                },
                {
                    "checkpoint": "after90",
                    "parameter": "contribution",
                    "column": "G",
                    "op": "lte",
                    "threshold": 1,
                },
            ],
        }
    ]
    assert validate_rubric_policy(frozen) == []
    late, early = matrix(), matrix()
    early["connection"]["J"]["mean_rating"] = 4.5
    result = classify_shape(
        late,
        frozen,
        checkpoint="after90",
        checkpoint_reports=[{"checkpoint": "d7", "rubric": early}],
    )
    assert result["value"] == "pleasant_executive"
    assert result["rules"][0]["predicates"][0]["observed"] == 4.5
    early["connection"]["J"] = {"status": "unmeasured", "reason": "No early interview"}
    missing = classify_shape(
        late,
        frozen,
        checkpoint="after90",
        checkpoint_reports=[{"checkpoint": "d7", "rubric": early}],
    )
    assert missing["status"] == "unmeasured" and missing["matches"] == []


def test_overlapping_shapes_missing_ladders_and_unfrozen_thresholds_do_not_classify():
    frozen = policy()
    predicate = {"parameter": "purpose", "column": "V", "op": "gte", "threshold": 1}
    frozen["shapes"] = [
        {"name": name, "all": [deepcopy(predicate)]} for name in ("drifter", "socialite")
    ]
    ambiguous = classify_shape(matrix(), frozen, checkpoint="end")
    assert ambiguous["status"] == "unmeasured" and len(ambiguous["matches"]) == 2
    frozen["shapes"][0]["all"][0]["threshold"] = float("nan")
    assert validate_rubric_policy(frozen)
    assert classify_shape(matrix(), policy(), checkpoint="end")["status"] == "unmeasured"
