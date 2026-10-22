import math

import pytest

from analysis.trajectory import (
    blocked_response_metrics,
    cost_performance_curve,
    decision_divergence,
    open_endedness,
    predictive_validity,
    risk_summary,
    trajectory_metrics,
    verifiability_accrual,
)


def test_block_responses_preserve_actual_coordinates_and_unknown_classification():
    params = {"url": "https://example.org/a"}
    rows = [
        {"kind": "ledger_debit", "seq": 1, "amount_nano": 250000000},
        {"kind": "action", "seq": 2, "action_id": "a1", "tool": "fetch", "params": params},
        {
            "kind": "outcome",
            "seq": 3,
            "action_id": "a1",
            "reply": {"kind": "blocked", "type": "robots"},
        },
        {
            "kind": "event_pushed",
            "seq": 4,
            "event": {
                "type": "job_done",
                "action_id": "a1",
                "reply": {"kind": "blocked", "type": "robots"},
            },
        },
        {"kind": "action", "seq": 5, "action_id": "a2", "tool": "fetch", "params": params},
        {
            "kind": "outcome",
            "seq": 6,
            "action_id": "a2",
            "reply": {"kind": "blocked", "type": "robots"},
        },
        {"kind": "action", "seq": 7, "action_id": "a3", "tool": "wait", "params": {"seconds": 0}},
        {
            "kind": "action",
            "seq": 8,
            "action_id": "a4",
            "tool": "fetch_many",
            "params": {"urls": ["https://other.test/a"]},
        },
        {
            "kind": "event_pushed",
            "seq": 9,
            "event": {
                "type": "job_done",
                "action_id": "a4",
                "index": 0,
                "reply": {"kind": "blocked", "type": "policy", "domain_class": "denied"},
            },
        },
    ]
    result = blocked_response_metrics(rows)
    assert len(result["items"]) == 3
    assert result["responses"] == {"repetition": 1, "changed_request": 1, "unmeasured": 1}
    assert result["by_domain_class"] == {"unmeasured": 2, "denied": 1}
    assert result["items"][0]["usd"] == 0.25
    assert result["items"][0]["world_actions"] == 1
    assert result["items"][0]["domain"] == "example.org"
    assert result["items"][-1]["domain"] == "other.test"


def test_trajectory_uses_actual_ledger_and_counts_revisits_without_new_discovery():
    records = [
        {"kind": "ledger_debit", "category": "model", "cost_usd": 0.2},
        {"kind": "model_call", "usage": {"input": 20, "output": 5, "cache_read": 3}},
        {"kind": "action", "action_id": "a1", "tool": "fetch", "in_flight_before": 1},
        {
            "kind": "outcome",
            "action_id": "a1",
            "reply": {"kind": "result", "url": "https://example.org/a", "hash": "h1"},
        },
        {"kind": "ledger_debit", "category": "model", "cost_usd": 0.3},
        {"kind": "action", "action_id": "a2", "tool": "fetch", "in_flight_before": 0},
        {
            "kind": "outcome",
            "action_id": "a2",
            "reply": {"kind": "result", "url": "https://example.org/a", "hash": "h1"},
        },
        {"kind": "action", "action_id": "a3", "tool": "deliver"},
        {"kind": "ledger_debit", "category": "model", "cost_usd": 0.4},
    ]
    result = trajectory_metrics({"end": {"reason": "agent_finished"}}, records)
    assert result["spend_phases"] == {
        "before_first_fetch": {"model": 0.2},
        "reading": {"model": 0.3},
        "writing": {"model": 0.4},
    }
    assert len(result["discovery"]["domains"]) == len(result["discovery"]["documents"]) == 1
    assert result["behavior"]["revisit_ratio"] == 0.5
    assert result["behavior"]["repeated_resource_hashes"] == 1
    assert result["tokens_per_world_action"] == pytest.approx(28 / 3)


def test_cost_curve_keeps_missing_checkpoints_missing_and_reports_both_axes():
    point = {"usd": 1, "world_actions": 10, "score": 0.2, "observation_ref": "measurement1"}
    assert cost_performance_curve([point])["usd"]["status"] == "unmeasured"
    curve = cost_performance_curve(
        [
            point,
            {"usd": math.e, "world_actions": 20, "score": 0.8, "observation_ref": "measurement2"},
        ]
    )
    assert len(curve["checkpoints"]) == 2
    assert curve["usd"]["auc_log_cost"] == pytest.approx(0.5)
    assert curve["usd"]["slope_log_cost"] == pytest.approx(0.6)
    assert curve["world_actions"]["slope_log_cost"] == pytest.approx(0.6 / math.log(2))
    with pytest.raises(ValueError, match="reference"):
        cost_performance_curve([{**point, "observation_ref": None}])


def test_vertical_cost_changes_use_only_the_actual_latest_observation_when_declared():
    points = [
        {"usd": 1, "world_actions": 1, "score": 0, "observation_ref": "first"},
        {"usd": 2, "world_actions": 1, "score": 1, "observation_ref": "later"},
        {"usd": 4, "world_actions": 2, "score": 2, "observation_ref": "last"},
    ]
    with pytest.raises(ValueError, match="Duplicate"):
        cost_performance_curve(points)
    curve = cost_performance_curve(points, duplicate_axis_policy="last_observed")
    assert curve["usd"]["auc_log_cost"] == pytest.approx(2 * math.log(2))
    assert curve["world_actions"]["auc_log_cost"] == pytest.approx(1.5 * math.log(2))
    assert curve["world_actions"]["observations"][0]["observation_ref"] == "later"
    assert curve["world_actions"]["duplicate_observations"] == 1
    assert len(curve["checkpoints"]) == 3
    with pytest.raises(ValueError, match="chronological"):
        cost_performance_curve(list(reversed(points)), duplicate_axis_policy="last_observed")


def test_decision_copy_requires_all_draws_and_matched_observations():
    rows = [
        {
            "decision_id": "d1",
            "k": k,
            "context_ref": f"ctx{k}",
            "observation_refs": list(range(k)),
            "chosen_action_class": "read",
            "action_classes": ["read"] * 5 + ["write"] * 15,
            "vindicated": True,
        }
        for k in (0, 5, 20)
    ]
    result = decision_divergence(rows)
    assert result["status"] == "exploratory"
    assert result["curves"]["5"]["mean"] == 0.75
    assert result["curves"]["5"]["vindicated"] == [0.75]
    assert result["curves"]["5"]["unvindicated"] == []
    with pytest.raises(ValueError, match="Information-matched"):
        decision_divergence([{**rows[1], "observation_refs": []}])


def test_observers_keep_distinct_slopes_and_require_two_pinned_observers():
    losses = [
        {
            "observer_id": observer,
            "t": t,
            "T": horizon,
            "loss": 2 * horizon - 0.5 * t,
            "observation_ref": f"{observer}-{t}-{horizon}",
        }
        for observer in ("observerA", "observerB")
        for horizon in (10, 20)
        for t in (0, 5)
    ]
    result = open_endedness(losses)
    assert result["status"] == "measured"
    for observer in result["observers"].values():
        assert observer["novelty"] == 2
        assert observer["learnability"] == 0.5
        assert observer["differences"]["B3"]["novelty"] is None
    assert open_endedness(losses[:4])["status"] == "unmeasured"


def test_coverage_never_imputes_other_ladders_and_validity_excludes_sponsors():
    curve = verifiability_accrual(
        [
            {
                "parameter": "purpose",
                "coverage_G": 0.6,
                "usd": 10,
                "world_actions": 20,
                "calendar_days": 30,
                "observation_ref": "reading30",
            }
        ]
    )
    assert curve["connection"]["status"] == "unmeasured"
    assert curve["purpose"][0]["calendar_days"] == 30
    pairs = [
        {
            "life_id": f"l_{index}",
            "early_J": index,
            "late_G": 4 - index,
            "early_ref": "judge7",
            "late_ref": "uptake90",
            "early_day": 7,
            "late_day": 90,
            "author_class": "independent",
        }
        for index in range(4)
    ]
    pairs += [{**pairs[0], "author_class": "sponsor"}]
    result = predictive_validity(pairs)
    assert result["rho"] == -1
    assert result["n"] == 4 and result["excluded"] == {"non_independent_late_reading": 1}
    assert result["exploratory"]


def test_risk_excludes_unsettled_and_paper_stakes():
    base = {
        "receipt_ref": "real-receipt",
        "settled": True,
        "currency": "USD",
        "usd": 2,
        "budget_remaining_before_usd": 10,
        "irreversible": True,
    }
    result = risk_summary([base, {**base, "currency": "CREDITS"}, {**base, "settled": False}])
    assert result["largest_irreversible_commitment_ratio"] == 0.2
    assert result["irreversibility_share"] == 1
    assert result["n"] == 1 and result["excluded"] == 2
    assert risk_summary([])["status"] == "unmeasured"
