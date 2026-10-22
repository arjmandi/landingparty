import pytest

from analysis.metrics import (
    ask_metrics,
    belief_metrics,
    calibration,
    cohen_kappa,
    goal_metrics,
    jensen_shannon,
    pass_k,
    retrieval_fidelity,
    revision_metrics,
    transfer_fidelity,
    vindicated_exploration,
    wilson_interval,
)
from analysis.probes import expectation_calibration


def test_kappa_and_calibration_distinguish_degenerate_labels():
    assert cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1
    assert cohen_kappa(["a", "a"], ["a", "a"]) is None
    judged = calibration([True, True, False, False], [True, False, False, False])
    assert judged["precision"] == 1
    assert judged["recall"] == 0.5
    assert judged["column"] == "J"
    with pytest.raises(ValueError):
        cohen_kappa([], [])


def test_pass_k_keeps_all_started_runs_and_minimum_seed_counts():
    assert pass_k([True] * 4, 1)["status"] == "unmeasured"
    assert pass_k([True] * 9, 1, headline=True)["status"] == "unmeasured"
    assert pass_k([True] * 19, 1, no_goal=True)["status"] == "unmeasured"
    assert pass_k([True, False, False, False, None], 1)["status"] == "unmeasured"
    assert pass_k([True, False, False, False, False], 2)["value"] == 0
    assert pass_k([True, True, False, False, False], 2)["value"] == pytest.approx(0.1)


def test_wilson_interval_and_js_distribution_limits():
    assert wilson_interval(0, 0) is None
    lower, upper = wilson_interval(10, 10)
    assert 0.7 < lower < upper <= 1
    assert jensen_shannon([1, 0], [0, 1]) == 1
    assert jensen_shannon([1, 1], [2, 2]) == 0
    with pytest.raises(ValueError):
        jensen_shannon([0, 0], [1, 0])


def test_belief_metrics_separate_updates_conflicts_and_sparse_latency():
    common = {"status": "held", "evidence": [{"class": "observed", "ref": "a1"}]}
    beliefs = [
        {
            **common,
            "id": "b1",
            "triple": {"subject": "s", "predicate": "p", "object": 1},
            "valid_from": "2026-01-01",
            "valid_to": "2026-02-01",
        },
        {
            **common,
            "id": "b2",
            "triple": {"subject": "s", "predicate": "p", "object": 2},
            "valid_from": "2026-02-01",
        },
        {
            **common,
            "id": "b3",
            "triple": {"subject": "s", "predicate": "p", "object": 3},
            "valid_from": "2026-03-01",
        },
    ]
    result = belief_metrics(beliefs)
    assert result["structured_contradictions"] == [["b2", "b3"]]
    assert result["triple_coverage"] == 1
    assert result["revision_latency"]["status"] == "unmeasured"


def test_conflicts_find_nonadjacent_overlaps_and_compare_actual_instants():
    common = {"status": "held", "triple": {"subject": "s", "predicate": "p", "object": 1}}
    rows = [
        dict(common, id="long", valid_from="2026-01-01", valid_to="2026-12-01"),
        dict(common, id="short", valid_from="2026-01-01", valid_to="2026-01-02"),
        {
            **common,
            "id": "later",
            "triple": {"subject": "s", "predicate": "p", "object": 2},
            "valid_from": "2026-03-01T02:00:00+02:00",
        },
    ]
    assert belief_metrics(rows)["structured_contradictions"] == [["long", "later"]]


def test_revision_latency_keeps_unread_and_read_but_kept_distinct():
    common = {"evidence_action": 2, "evidence_usd": 0.1, "observation_ref": "a_evidence"}
    rows = [
        {**common, "belief_id": "b1", "revision_needed": True},
        {**common, "belief_id": "b2", "read_action": 3, "revision_needed": True},
        {
            **common,
            "belief_id": "b3",
            "read_action": 3,
            "revised_action": 6,
            "revised_usd": 0.3,
            "revision_needed": False,
        },
    ]
    result = revision_metrics(rows, triple_coverage=1)
    assert result["states"] == {"not_read": 1, "read_and_kept": 1, "revised": 1}
    assert result["median_latency_actions"] == 4
    assert result["median_latency_usd"] == pytest.approx(0.2)
    assert result["missed_revisions"] == 2 and result["unnecessary_revisions"] == 1
    assert revision_metrics(rows, triple_coverage=0.49)["status"] == "unmeasured"
    with pytest.raises(ValueError, match="precede"):
        revision_metrics([{**rows[-1], "read_action": 1}], triple_coverage=1)


def test_calibration_separates_binary_scores_nonbinary_resolutions_and_expiry():
    rows = [{"p": 0.8, "outcome": "held", "resolver": "http", "venue": "public"} for _ in range(30)]
    rows += [{"outcome": "missed"}, {"p": 0.9, "outcome": "expired"}]
    result = expectation_calibration(rows)
    assert result["resolved"] == 31 and result["binary_resolved"] == 30
    assert result["brier"]["value"] == pytest.approx(0.04)
    assert result["expected_calibration_error"]["value"] == pytest.approx(0.2)
    assert result["strata"]["resolver=http;venue=public"]["binary_resolved"] == 30
    assert (
        expectation_calibration(rows[:29])["expected_calibration_error"]["status"] == "unmeasured"
    )


def test_transfer_fidelity_normalizes_against_retest_and_cold():
    result = transfer_fidelity(
        ["a", "b", "c", "d"], ["a", "b", "c", "d"], ["a", "b", "c", "x"], ["a", "x", "x", "x"]
    )
    assert result["normalized_fidelity"] == pytest.approx(2 / 3)


def test_ask_metrics_count_cumulative_takeover_seconds_once():
    rows = [
        {"kind": "ledger_debit", "seq": 1, "amount_nano": 100000000},
        {"kind": "ask_opened", "seq": 2, "kind_requested": "takeover", "ticket_id": "t1"},
        {
            "kind": "ask_answered",
            "seq": 3,
            "ticket_id": "t1",
            "ask_class": "blocker",
            "human_seconds": 5,
        },
        {"kind": "ask_resolved", "seq": 4, "ticket_id": "t1", "human_seconds": 20},
    ]
    result = ask_metrics(rows, spent_usd=1)
    assert result["human_seconds"] == 20
    assert result["dollars_before_first_ask"] == 0.1
    assert result["asks_per_100_usd"] == 100
    assert result["blocker_to_choice_ratio"] is None


def test_ask_progress_uses_actual_utc_weeks_and_preserves_missing_observations():
    rows = [
        {
            "kind": "action",
            "seq": 1,
            "action_id": "a1",
            "tool": "ask",
            "params": {"subgoal_fraction": 0.25, "info_type": "choice"},
        },
        {"kind": "ask_opened", "seq": 2, "action_id": "a1", "ts": "2026-01-04T23:30:00-02:00"},
        {
            "kind": "ask_opened",
            "seq": 3,
            "ts": "2026-01-06T12:00:00+00:00",
            "subgoal_fraction": None,
        },
        {
            "kind": "ask_opened",
            "seq": 4,
            "ts": "2026-01-13T12:00:00+00:00",
            "subgoal_fraction": 0.75,
        },
        {"kind": "ask_opened", "seq": 5, "ts": "2026-01-13T12:00:00", "subgoal_fraction": 1},
    ]
    result = ask_metrics(rows, spent_usd=1)
    series = result["subgoal_fraction_by_week"]
    assert series["column"] == "S" and series["missing_timestamp_n"] == 1
    assert set(series["weeks"]) == {"2026-W02", "2026-W03"}
    assert series["weeks"]["2026-W02"]["values"] == [0.25]
    assert series["weeks"]["2026-W02"]["missing"] == 1
    assert series["weeks"]["2026-W03"]["values"] == [0.75]
    assert result["info_types"] == {"choice": 1, "unmeasured": 3}


def test_silence_and_rejection_responses_use_record_order_and_deduplicate_delivery():
    original = {"kind": "message", "text": "help"}
    rows = [
        {"kind": "action", "seq": 1, "action_id": "a1", "tool": "ask", "params": original},
        {"kind": "ask_opened", "seq": 2, "action_id": "a1"},
        {"kind": "outcome", "seq": 3, "action_id": "a1", "reply": {"kind": "silence"}},
        {"kind": "action", "seq": 4, "action_id": "a2", "tool": "ask", "params": original},
        {
            "kind": "ask_resolved",
            "seq": 5,
            "action_id": "a2",
            "ticket_id": "t1",
            "state": "rejected",
        },
        {
            "kind": "event_pushed",
            "seq": 6,
            "event": {
                "type": "ask_resolved",
                "state": "rejected",
                "action_id": "a2",
                "ticket_id": "t1",
                "cursor": 1,
            },
        },
        {"kind": "action", "seq": 7, "action_id": "a3", "tool": "wait", "params": {"seconds": 0}},
        {"kind": "inbox_read", "seq": 8, "cursor": 1},
    ]
    result = ask_metrics(rows, spent_usd=0)
    silence = result["behavior_after_silence"]
    assert silence["n"] == 1 and silence["items"][0]["response"] == "repeated_request"
    rejected = result["behavior_after_rejection"]
    assert rejected["n"] == 1 and rejected["items"][0]["response"] == "changed_request"
    assert rejected["items"][0]["next_action_id"] == "a3"
    assert rejected["items"][0]["inbox_read_seq"] == 8
    assert rejected["items"][0]["inbox_read_before_next_action"] is False


def test_goal_metrics_keep_unlabellable_fraction_and_calibrated_column():
    rows = [
        {"goal": "write", "source_spent_usd": 1},
        {"goal": None, "source_spent_usd": 1},
        {"goal": "write", "source_spent_usd": 2},
        {"goal": "read", "source_spent_usd": 1},
    ]
    result = goal_metrics(rows, kappa=0.7)
    assert result["recoverability"] == 0.75
    assert result["directedness"] == 0.6
    assert result["column"] == "V"
    assert result["switches"] == 1


def test_retrieval_fidelity_uses_paired_queries_and_reports_missing_queries():
    result = retrieval_fidelity(
        [
            {"query_id": "a", "items": [{"url": "https://a/x#fragment"}, {"url": "https://b/"}]},
            {"query_id": "missing", "items": ["https://c/"]},
        ],
        [{"query_id": "a", "items": ["https://a/x", "https://different/"]}],
    )
    assert result["mean_overlap_at_k"] == 0.5
    assert result["coverage"] == 0.5
    assert result["missing_replay_queries"] == ["missing"]


def test_vindication_requires_resolved_sample_and_information_matched_copies():
    assert vindicated_exploration([])["status"] == "unmeasured"
    events = [{"outcome": 1, "copy_probability": 0.5}] * 20 + [
        {"outcome": 2, "copy_probability": 0.1, "copy_samples": 20}
    ] * 10
    result = vindicated_exploration(events)
    assert result["rate"] == 1
    assert result["n"] == 10
    assert result["taste"]["status"] == "unmeasured"
