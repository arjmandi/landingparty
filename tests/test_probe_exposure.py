from analysis.probes import expectation_calibration, exposed_urls, score_probe_cells


def test_probe_or_sponsor_resolutions_cannot_enter_headline_calibration():
    result = expectation_calibration(
        [{"p": 1, "outcome": "held", "grounded": False} for _ in range(30)]
    )
    assert result["resolved"] == 30 and result["binary_resolved"] == 0
    assert result["excluded_from_calibration"] == {"non_grounded_resolution": 30}
    assert result["brier"]["status"] == "unmeasured"


def test_batch_redirect_and_browser_exposure_excludes_whole_item_in_every_cell():
    records = [
        {"kind": "action", "params": {"urls": ["https://short.example/item"]}},
        {
            "kind": "event_pushed",
            "event": {
                "type": "job_done",
                "index": 0,
                "reply": {
                    "kind": "result",
                    "url": "https://short.example/item",
                    "final_url": "https://heldout.example/source#section",
                },
            },
        },
        {
            "kind": "outcome",
            "reply": {"kind": "result", "final_url": "https://browser.example/page"},
        },
    ]
    rows = [
        {
            "item_id": "exposed",
            "cell": cell,
            "score": 1,
            "source_urls": ["https://heldout.example/source"],
        }
        for cell in ("pre_off", "pre_on", "post_off", "post_on", "post_mem")
    ]
    result = score_probe_cells(rows, fetched_urls=exposed_urls(records))
    assert result["excluded_item_ids"] == ["exposed"]
    assert all(
        reading["status"] == "unmeasured"
        for reading in result["strata"]["world_mechanics"]["cells"].values()
    )
    assert "https://browser.example/page" in exposed_urls(records)


def test_late_final_outcomes_keep_coverage_but_never_enter_brier_or_ece():
    rows = [{"p": 1, "outcome": "held"} for _ in range(29)]
    rows.append({"p": 0, "outcome": "held", "headline_eligible": False, "late_final": True})
    result = expectation_calibration(rows)
    assert result["resolved"] == 30 and result["resolution_coverage"] == 1
    assert result["binary_resolved"] == 29
    assert result["excluded_from_calibration"] == {"late_or_ineligible_resolution": 1}
    assert (
        result["brier"]["status"] == result["expected_calibration_error"]["status"] == "unmeasured"
    )
