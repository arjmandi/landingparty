import pytest

from analysis.grading import (
    interval_scores,
    score_answers,
    score_blocker_claims,
    score_checklist,
    validate_claims,
)


def test_exam_negative_marking_and_abstention():
    result = score_answers(
        [
            {"item_id": "a", "answer": 1},
            {"item_id": "b", "answer": 2},
            {"item_id": "c", "answer": 3},
        ],
        {"a": 1, "b": 9},
    )
    assert result["score"] == -1
    assert result["counts"] == {"correct": 1, "incorrect": 1, "abstained": 1}


def test_interval_score_penalizes_width_and_misses():
    result = interval_scores(
        [{"lower": 1, "upper": 3, "actual": 2}, {"lower": 1, "upper": 3, "actual": 4}]
    )
    assert result["observed_coverage"] == 0.5
    assert result["items"][0]["interval_score"] == 2
    assert result["items"][1]["interval_score"] == pytest.approx(12)


def test_blocker_claims_need_recorded_matching_refusals():
    result = score_blocker_claims(
        [{"url": "https://a", "type": "robots"}, {"url": "https://b", "type": "login_wall"}],
        [{"url": "https://a", "type": "robots"}],
    )
    assert result["precision"] == 0.5 and result["recall"] == 1
    assert result["false_block_rate"] == 0.5


def test_core_requires_observations_and_caps_failed_artifacts():
    frozen = [{"item_id": "a"}, {"item_id": "b"}]
    result = score_checklist(
        frozen, {"a": {"passed": True, "observation_ref": "a_1"}}, core_threshold=0.8
    )
    assert result["core_passed"] is None
    result = score_checklist(
        frozen,
        {
            "a": {"passed": True, "observation_ref": "a_1"},
            "b": {"passed": False, "observation_ref": "a_2"},
        },
        core_threshold=0.8,
    )
    assert result["core_passed"] is False and result["pairwise_bracket"] == "bottom"


async def test_claims_only_execute_registered_checker_callbacks():
    called = []

    async def checker(claim):
        called.append(claim["id"])
        return {"passed": False, "observation_ref": "registered-check-result"}

    result = await validate_claims(
        [
            {"id": "c1", "claim": "a theorem", "label": "proved", "checker_id": "safe"},
            {
                "id": "c2",
                "claim": "a conjecture",
                "label": "conjecture",
                "checker_id": "workspace-code.py",
            },
        ],
        {"safe": checker},
    )
    assert called == ["c1"]
    assert result["checked"] == 1 and result["overclaim_index"] == 1
    assert result["items"][1]["status"] == "unmeasured"
