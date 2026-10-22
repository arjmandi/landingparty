import json
from copy import deepcopy
from hashlib import sha256

import pytest

from analysis.probes import expectation_calibration, score_probe_cells
from analysis.report import (
    TUPLE_FIELDS,
    audit_completeness,
    build_report,
    compare_runs,
    render_report,
    verify_citations,
    verify_records,
)


def chain(events):
    records = []
    previous = "sha256:" + "0" * 64
    for index, event in enumerate(events, start=1):
        record = {
            "run_id": "r_test",
            "seq": index,
            "prev_hash": previous,
            "ts": f"2026-10-22T00:00:{index:02d}+00:00",
            **event,
        }
        record["hash"] = (
            "sha256:"
            + sha256(
                json.dumps(
                    record,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest()
        )
        records.append(record)
        previous = record["hash"]
    return records


def test_report_counts_actual_debits_once_and_keeps_unmeasured_scores():
    records = chain(
        [
            {"kind": "beam_down"},
            {
                "kind": "action",
                "action_id": "a_1",
                "tool": "fetch",
                "in_flight_before": 0,
                "params": {"url": "https://example.org/page"},
            },
            {
                "kind": "outcome",
                "action_id": "a_1",
                "reply": {
                    "kind": "result",
                    "handle": "/world/fetched/a_1.html",
                    "hash": "sha256:abc",
                    "bytes": 42,
                },
            },
            {
                "kind": "ledger_debit",
                "category": "model",
                "amount_nano": 20000000,
                "cost_usd": 0.02,
            },
            {"kind": "model_call", "usd": 0.02, "usage": {"input": 10, "output": 5}},
            {
                "kind": "action",
                "action_id": "a_2",
                "tool": "fetch",
                "params": {},
                "in_flight_before": 1,
            },
            {"kind": "outcome", "action_id": "a_2", "block": {"type": "robots"}},
            {"kind": "seal"},
        ]
    )
    run = {
        "run_id": "r_test",
        "state": "ended",
        "config": {
            "mode": "live",
            "budget": {"usd": 20, "wall_s": 7200},
        },
        "end": {"terminated": "agent_finished", "reason": "agent_finished"},
    }
    report = build_report(run, records, ledger={"model": 0.02})
    assert report["axes"] == {"usd": 0.02, "world_actions": 2}
    assert report["activity"]["blocks"] == {"robots": 1}
    assert report["activity"]["fetched_bytes"] == 42
    assert report["activity"]["attempted_domains"] == ["example.org"]
    assert report["costs"]["token_classes"] == {"input": 10, "output": 5}
    assert report["integrity"]["record"]["valid"]
    assert report["integrity"]["ledger"]["valid"]
    assert not report["integrity"]["tainted"]
    assert report["completeness"]["verdict"] == "unknown"
    for parameter in report["rubric"].values():
        assert all(value["status"] == "unmeasured" for value in parameter.values())
    assert report["columns"]["J"]["status"] == "unmeasured"
    assert report["columns"]["S"]["status"] == "unmeasured"
    markdown = render_report(report)
    assert "world actions: 2" in markdown
    assert "unmeasured | unmeasured | unmeasured | unmeasured" in markdown
    assert "Policy" in markdown


def test_record_verification_detects_edit_truncation_and_wrong_run():
    records = chain([{"kind": "beam_down"}, {"kind": "seal"}])
    assert verify_records(records)["valid"]
    edited = deepcopy(records)
    edited[0]["kind"] = "tampered"
    assert not verify_records(edited)["valid"]
    assert not verify_records(records[1:])["valid"]
    assert not verify_records(records, run_id="r_other")["valid"]
    assert not verify_records([])["valid"]


def test_report_ledger_mismatch_taints_and_external_check_preserved():
    records = chain([{"kind": "ledger_debit", "category": "search", "amount_nano": 5000000}])
    report = build_report({"run_id": "r_test"}, records, ledger={"search": 0.01})
    assert report["integrity"]["tainted"]
    assert report["integrity"]["ledger"]["mismatches"]["search"] == {
        "ledger_usd": 0.01,
        "recorded_usd": 0.005,
    }
    report = build_report(
        {"run_id": "r_test"},
        records,
        verification={
            "record": {"valid": False, "error": "mirror differs"},
            "ledger": {"valid": True, "ledger_usd": 0.005, "recorded_usd": 0.005},
        },
    )
    assert report["integrity"]["record"]["error"] == "mirror differs"
    assert report["integrity"]["tainted"]


def test_completeness_requires_transcript_and_shell_evidence():
    records = [{"kind": "model_call"}]
    assert (
        audit_completeness({"completeness": {"verdict": "complete"}}, records)["verdict"]
        == "unknown"
    )
    audit = {"model_calls_transcript": 1, "shell_bypass_candidates": 0, "shell_audited": True}
    assert audit_completeness({"completeness": audit}, records)["verdict"] == "complete"
    assert (
        audit_completeness({"completeness": {**audit, "model_calls_transcript": 2}}, records)[
            "verdict"
        ]
        == "partial"
    )
    assert (
        audit_completeness({"completeness": audit}, records + [{"kind": "completeness_gap"}])[
            "verdict"
        ]
        == "partial"
    )
    assert audit_completeness({"completeness": audit}, [])["verdict"] == "partial"


def test_internal_network_subrequests_do_not_double_count_world_actions():
    records = chain(
        [
            {"kind": "action", "action_id": "a_goto", "tool": "goto", "params": {}},
            {
                "kind": "action",
                "action_id": "a_network",
                "tool": "raw_request",
                "world_action": False,
                "params": {},
            },
            {
                "kind": "outcome",
                "action_id": "a_network",
                "reply": {"kind": "result", "handle": "/world/fetched/a_network.html", "bytes": 20},
            },
            {"kind": "outcome", "action_id": "a_goto", "reply": {"kind": "result"}},
            {
                "kind": "action",
                "action_id": "a_shell",
                "tool": "raw_request",
                "world_action": True,
                "params": {},
            },
            {
                "kind": "outcome",
                "action_id": "a_shell",
                "reply": {"kind": "result", "handle": "/world/fetched/a_shell.txt", "bytes": 10},
            },
        ]
    )
    report = build_report({"run_id": "r_test"}, records, ledger={})
    assert report["axes"]["world_actions"] == 2
    assert report["activity"]["by_tool"] == {"goto": 1, "raw_request": 1}
    assert report["activity"]["outcomes"] == 2
    assert report["activity"]["network_submissions"] == 2
    assert report["activity"]["recorded_outcomes_including_subrequests"] == 3
    assert report["activity"]["fetched_bytes"] == 30


def test_citations_only_verify_capture_and_quote_presence_without_network():
    archive = {"https://example.org/source": "A source with\n several spaces and quoted words."}
    claims = [
        {
            "claim": "A claim",
            "url": "https://example.org/source#section",
            "quote": "source with several spaces",
        },
        {"claim": "Wrong", "url": "https://example.org/source", "quote": "not in the page"},
        {"claim": "Missing", "url": "https://missing.test/", "quote": "anything"},
        {"claim": "Blank quote", "url": "https://example.org/source", "quote": ""},
    ]
    result = verify_citations(claims, archive)
    assert result["counts"] == {
        "quote_present": 1,
        "quote_missing": 1,
        "source_not_archived": 1,
        "invalid_citation": 1,
    }
    assert result["quote_match_rate"] == 0.25
    assert result["claim_support"]["status"] == "unmeasured"
    assert verify_citations([], archive)["quote_match_rate"] is None


def comparable_runs():
    left = {
        "run_id": "r_left",
        "created_at": "2026-10-22T00:00:00+00:00",
        "config": {
            "tuple": {field: f"pinned-{field}" for field in TUPLE_FIELDS},
            "mode": "replay",
            "goal_slice": "goal14-slice1",
            "block_profile": {"reference": 0.02},
        },
    }
    right = deepcopy(left)
    right["run_id"] = "r_right"
    return left, right


def test_comparability_accepts_complete_same_archive_and_rejects_missing_evidence():
    left, right = comparable_runs()
    assert compare_runs(left, right)["comparable"]
    right["config"]["tuple"]["price_table_id"] = "another-price"
    result = compare_runs(left, right)
    assert not result["comparable"]
    assert result["tuple_mismatches"] == ["price_table_id"]
    del right["config"]["tuple"]["price_table_id"]
    assert "price_table_id" in compare_runs(left, right)["missing_tuple_fields"]["right"]
    assert not compare_runs({}, {})["comparable"]


def test_live_comparability_requires_interleaving_and_block_drift_within_five_points():
    left, right = comparable_runs()
    left["config"]["mode"] = right["config"]["mode"] = "live"
    left["config"]["tuple"]["archive_id"] = "left-archive"
    right["config"]["tuple"]["archive_id"] = "right-archive"
    assert not compare_runs(left, right)["comparable"]
    assert compare_runs(left, right, interleaved=True)["comparable"]
    right["config"]["block_profile"]["reference"] = 0.07
    assert compare_runs(left, right, interleaved=True)["comparable"]
    right["config"]["block_profile"]["reference"] = 0.071
    assert not compare_runs(left, right, interleaved=True)["comparable"]
    right["created_at"] = "2026-11-01T00:00:00+00:00"
    assert not compare_runs(left, right, interleaved=True)["comparable"]


def test_probe_gains_use_only_paired_items_and_keep_strata_separate():
    results = [
        {"item_id": "one", "cell": "pre_off", "score": 0},
        {"item_id": "one", "cell": "post_off", "score": 0.5},
        {"item_id": "one", "cell": "post_on", "score": 1},
        {"item_id": "one", "cell": "post_mem", "score": 0.75},
        {"item_id": "unpaired", "cell": "post_on", "score": 0},
        {"item_id": "own", "cell": "post_mem", "score": 0, "stratum": "own_situation"},
    ]
    report = score_probe_cells(results)
    gains = report["strata"]["world_mechanics"]["gains"]
    assert gains["grounding_gain"]["value"] == 0.5
    assert gains["formation_signal"]["value"] == 0.75
    assert gains["internalisation"]["n_paired"] == 1
    assert report["strata"]["own_situation"]["gains"]["formation_signal"]["status"] == "unmeasured"
    assert report["reset_test"]["status"] == "unmeasured"


def test_probe_exposure_excludes_item_from_all_cells_and_rejects_duplicates():
    rows = [
        {"item_id": "exposed", "cell": "pre_off", "score": 0},
        {
            "item_id": "exposed",
            "cell": "post_mem",
            "score": 1,
            "source_urls": ["https://x.test/a#heading"],
        },
    ]
    report = score_probe_cells(rows, fetched_urls=["https://x.test/a"])
    assert report["excluded_item_ids"] == ["exposed"]
    assert report["strata"]["world_mechanics"]["cells"]["pre_off"]["status"] == "unmeasured"
    with pytest.raises(ValueError, match="duplicate"):
        score_probe_cells(rows + rows)
    with pytest.raises(ValueError, match="unknown probe"):
        score_probe_cells([{"item_id": "bad", "cell": "unknown", "score": 0}])


def test_calibration_needs_thirty_resolutions_and_keeps_open_distinct():
    few = [{"p": 0.75, "outcome": "held"}] * 29 + [{"p": 0.2, "outcome": "open"}]
    assert expectation_calibration(few)["brier"]["status"] == "unmeasured"
    rows = few + [{"p": 0.75, "outcome": "missed"}]
    result = expectation_calibration(rows)
    assert result["resolved"] == 30
    assert result["total"] == 31
    assert result["brier"]["value"] == pytest.approx((29 * 0.25**2 + 0.75**2) / 30)
    assert result["resolution_coverage"] == pytest.approx(30 / 31)
    for invalid in (float("nan"), float("inf"), -0.1, 1.01, True):
        with pytest.raises((ValueError, TypeError)):
            expectation_calibration([{"p": invalid, "outcome": "held"}])
