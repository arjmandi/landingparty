"""A baseline name never substitutes for measured matched conditions."""

from copy import deepcopy

import pytest

from analysis.controls import matched_life_deltas, matching_tuple_id
from analysis.rubric import PARAMETERS
from worldd.registry import VERSION_FIELDS


def report(life_id, *, harness="reference-loop@0.1/native-parallel", value=3):
    version = {field: "pinned@1" for field in VERSION_FIELDS}
    version.update(archive_id=None, harness_id=harness, model_snapshot_id="model@1")
    return {
        "life_id": life_id,
        "checkpoint": "end",
        "binding_id": "venues@1",
        "policy_hash": "sha256:policy",
        "rubric": {
            parameter: {
                "G": {"status": "measured", "value": value},
                "V": {"status": "measured", "value": value},
                "J": {"status": "measured", "ratings": [1, 2], "kappa": 0.7},
                "S": {"status": "measured", "value": value},
            }
            for parameter in PARAMETERS
        },
        "matching": {
            "version_tuples": [version],
            "binding_hash": "sha256:binding",
            "model_snapshot_id": "model@1",
            "seed": 3,
            "prereg_hash": "sha256:prereg",
            "budget": {"usd_life": 200, "wall_life_days": 28},
            "calendar": {
                "start_iso_week": "2026-W43",
                "checkpoint": "end",
                "exposure_days": 28,
                "afterlife_days": 0,
            },
            "block_profile": {"reference": 0.1},
        },
    }


def test_matched_life_scalar_differences_keep_columns_and_missing_baselines():
    target, baseline = report("life_target", value=4), report("life_B1", value=2)
    result = matched_life_deltas(target, {"B1": baseline})
    contrast = result["controls"]["B1"]
    assert contrast["matched"] and contrast["status"] == "measured"
    assert contrast["rubric"]["connection"]["G"]["delta"] == 2
    assert contrast["rubric"]["connection"]["S"]["delta"] == 2
    assert not contrast["rubric"]["connection"]["S"]["headline"]
    assert contrast["rubric"]["connection"]["J"]["status"] == "unmeasured"
    assert result["controls"]["B0"]["status"] == "unmeasured"
    assert result["t0"]["status"] == "unmeasured" and not result["scientific_gate_passed"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("binding_hash", "sha256:other"),
        ("seed", 4),
        ("model_snapshot_id", "model@2"),
        ("budget", {"usd_life": 201, "wall_life_days": 28}),
        (
            "calendar",
            {
                "start_iso_week": "2026-W44",
                "checkpoint": "end",
                "exposure_days": 28,
                "afterlife_days": 0,
            },
        ),
        ("block_profile", {"reference": 0.151}),
    ],
)
def test_different_conditions_never_enter_a_matched_delta(field, value):
    target, baseline = report("life_target"), report("life_B1")
    baseline["matching"][field] = value
    result = matched_life_deltas(target, {"B1": baseline})["controls"]["B1"]
    assert not result["matched"] and result["status"] == "unmeasured"
    assert "rubric" not in result


def test_intended_baseline_tuple_change_requires_exact_registered_pair():
    target, baseline = (
        report("life_target"),
        report("life_B0", harness="B0@0.1/steps=200/revisions=20"),
    )
    baseline["matching"]["version_tuples"][0]["tool_set_id"] = "disabled"
    assert not matched_life_deltas(target, {"B0": baseline})["controls"]["B0"]["matched"]
    registration = {
        "prereg_hash": "sha256:prereg",
        "contrasts": {
            "B0": {
                "target_tuple_ids": [matching_tuple_id(target["matching"]["version_tuples"][0])],
                "control_tuple_ids": [matching_tuple_id(baseline["matching"]["version_tuples"][0])],
            }
        },
    }
    assert matched_life_deltas(target, {"B0": baseline}, registered_contrasts=registration)[
        "controls"
    ]["B0"]["matched"]
    changed = deepcopy(baseline)
    changed["matching"]["version_tuples"][0]["price_table_id"] = "changed-after-registration"
    assert not matched_life_deltas(target, {"B0": changed}, registered_contrasts=registration)[
        "controls"
    ]["B0"]["matched"]


def test_missing_reading_stays_unmeasured_while_measured_zero_delta_is_zero():
    target, baseline = report("life_target", value=0), report("life_B1", value=0)
    baseline["rubric"]["purpose"]["G"] = {"status": "unmeasured", "reason": "no independent events"}
    result = matched_life_deltas(target, {"B1": baseline})["controls"]["B1"]["rubric"]
    assert result["connection"]["G"]["delta"] == 0
    assert (
        result["purpose"]["G"]["status"] == "unmeasured" and "delta" not in result["purpose"]["G"]
    )


def test_global_control_requires_actual_condition_and_each_reference_is_separate():
    target, baseline = report("life_target"), report("life_global")
    assert not matched_life_deltas(target, {"global_control": baseline})["controls"][
        "global_control"
    ]["matched"]
    baseline["matching"]["global_control"] = True
    result = matched_life_deltas(
        target, {"global_control": [baseline, report("life_unmatched", value=2)]}
    )["controls"]["global_control"]
    assert not result["pooled"] and len(result["references"]) == 2
    assert result["references"][0]["matched"] and not result["references"][1]["matched"]


def test_t0_battery_is_not_fabricated_into_a_ladder_and_commensurate_t0_must_same_life():
    target = report("life_target")
    raw = {"study": "t0", "life_id": "life_target", "lottery": {"choices": [0, 1]}}
    assert matched_life_deltas(target, t0=raw)["t0"]["status"] == "unmeasured"
    baseline = report("life_target", value=1)
    baseline["checkpoint"] = "t0"
    baseline["matching"]["calendar"].update(checkpoint="t0", exposure_days=0)
    result = matched_life_deltas(target, t0=baseline)["t0"]
    assert result["matched"] and result["rubric"]["connection"]["V"]["delta"] == 2
    baseline["life_id"] = "life_other"
    assert not matched_life_deltas(target, t0=baseline)["t0"]["matched"]


def test_incomplete_tuples_and_nan_never_produce_reading():
    target, baseline = report("life_target"), report("life_B1")
    del baseline["matching"]["version_tuples"][0]["policy_id"]
    assert not matched_life_deltas(target, {"B1": baseline})["controls"]["B1"]["matched"]
    baseline = report("life_B1")
    baseline["rubric"]["connection"]["G"]["value"] = float("nan")
    assert (
        matched_life_deltas(target, {"B1": baseline})["controls"]["B1"]["rubric"]["connection"][
            "G"
        ]["status"]
        == "unmeasured"
    )
