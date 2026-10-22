"""Matched life contrasts, preserving every column and every unmatched reference.

The pipeline loads and hash-verifies actual checkpoint artifacts before calling
this pure scorer. No missing reading becomes zero and no controls are pooled.
"""

from __future__ import annotations

import math

from worldd.registry import VERSION_FIELDS
from worldd.storage import content_hash

from .rubric import PARAMETERS

CONTROL_NAMES = ("global_control", "B0", "B1", "B3b", "B3c")
COLUMNS = ("G", "V", "J", "S")


def matching_tuple_id(version):
    """Hash the complete registered vector without its particular archive ID."""
    missing = [field for field in VERSION_FIELDS if field not in version]
    if missing:
        raise ValueError("Incomplete comparison tuple: " + ", ".join(missing))
    return content_hash(
        {field: version[field] for field in VERSION_FIELDS if field != "archive_id"}
    )


def _unmeasured(reason):
    return {"status": "unmeasured", "reason": reason}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _unwrap(value):
    if isinstance(value, dict) and "report" in value and "rubric" not in value:
        return value["report"]
    return value


def _description(report):
    errors = []
    if not isinstance(report, dict):
        return {}, [], ["checkpoint report is absent"]
    matching = report.get("matching")
    if not isinstance(matching, dict):
        return {}, [], ["actual matching descriptors are absent"]
    tuples = matching.get("version_tuples")
    identities = []
    if not isinstance(tuples, list) or not tuples:
        errors.append("actual session version tuples are absent")
    else:
        for version in tuples:
            try:
                if not isinstance(version, dict):
                    raise TypeError("invalid session tuple")
                identities.append(matching_tuple_id(version))
            except (ValueError, TypeError) as error:
                errors.append(str(error))
    for field in ("binding_hash", "model_snapshot_id"):
        if not isinstance(matching.get(field), str) or not matching[field]:
            errors.append("missing matching " + field)
    if not isinstance(matching.get("seed"), int) or isinstance(matching.get("seed"), bool):
        errors.append("a single actual seed is required")
    budget = matching.get("budget", {})
    if not isinstance(budget, dict) or any(
        not _number(budget.get(key)) or budget[key] <= 0 for key in ("usd_life", "wall_life_days")
    ):
        errors.append("actual dollar and calendar budget caps are absent")
    calendar = matching.get("calendar", {})
    if not isinstance(calendar, dict) or any(
        key not in calendar
        for key in ("start_iso_week", "checkpoint", "exposure_days", "afterlife_days")
    ):
        errors.append("actual checkpoint calendar is absent")
    elif (
        not isinstance(calendar["start_iso_week"], str)
        or not calendar["start_iso_week"]
        or not _number(calendar["exposure_days"])
        or calendar["exposure_days"] < 0
        or not _number(calendar["afterlife_days"])
        or calendar["afterlife_days"] < 0
        or calendar["checkpoint"] != report.get("checkpoint")
    ):
        errors.append("invalid actual checkpoint calendar")
    if not report.get("binding_id") or not report.get("policy_hash"):
        errors.append("the pinned binding and scoring policy are absent")
    return matching, sorted(set(identities)), errors


def _registered_pair(name, left, right, left_ids, right_ids, registration):
    if not isinstance(registration, dict) or not registration.get("prereg_hash"):
        return False
    if (
        left.get("prereg_hash") != registration["prereg_hash"]
        or right.get("prereg_hash") != registration["prereg_hash"]
    ):
        return False
    pair = registration.get("contrasts", {}).get(name, {})
    return (
        isinstance(pair, dict)
        and pair.get("target_tuple_ids") == left_ids
        and pair.get("control_tuple_ids") == right_ids
    )


def _match(target, reference, name, registration):
    left, left_ids, left_errors = _description(target)
    right, right_ids, right_errors = _description(reference)
    reasons = ["target: " + error for error in left_errors] + [
        "reference: " + error for error in right_errors
    ]
    if reasons:
        return reasons, left_ids, right_ids
    for key in ("binding_hash", "budget", "model_snapshot_id", "seed", "prereg_hash"):
        if left.get(key) != right.get(key):
            reasons.append("different matching " + key)
    for key in ("binding_id", "policy_hash"):
        if target.get(key) != reference.get(key):
            reasons.append("different " + key)
    if left_ids != right_ids and not _registered_pair(
        name, left, right, left_ids, right_ids, registration
    ):
        reasons.append("different session tuples without the exact registered contrast")
    left_archives = {version.get("archive_id") for version in left["version_tuples"]}
    right_archives = {version.get("archive_id") for version in right["version_tuples"]}
    if name != "t0" and (None in left_archives or left_archives != right_archives):
        a, b = left.get("block_profile"), right.get("block_profile")
        if not isinstance(a, dict) or not isinstance(b, dict) or not a or set(a) != set(b):
            reasons.append("actual comparable domain-class block profiles are absent")
        elif any(
            not _number(a[key])
            or not _number(b[key])
            or not 0 <= a[key] <= 1
            or not 0 <= b[key] <= 1
            or abs(a[key] - b[key]) > 0.05
            for key in a
        ):
            reasons.append(
                "block profiles differ by more than five percentage points or are invalid"
            )
    if name == "t0":
        calendar = right["calendar"]
        if (
            reference.get("life_id") != target.get("life_id")
            or reference.get("checkpoint") != "t0"
            or calendar["exposure_days"] != 0
            or calendar["afterlife_days"] != 0
            or calendar["start_iso_week"] != left["calendar"]["start_iso_week"]
        ):
            reasons.append("t=0 must precede this same actual life")
    else:
        if left["calendar"] != right["calendar"]:
            reasons.append("different checkpoint calendars")
        if reference.get("life_id") == target.get("life_id"):
            reasons.append("a control must be a distinct actual life")
        if name == "global_control":
            if right.get("global_control") is not True:
                reasons.append("the reference is not an actual global-control condition")
        elif name in CONTROL_NAMES:
            prefixes = {"B0": "B0@", "B1": "reference-loop@", "B3b": "B3b@", "B3c": "B3c@"}
            if any(
                not str(version["harness_id"]).startswith(prefixes[name])
                for version in right["version_tuples"]
            ):
                reasons.append("the reference harness does not implement " + name)
    return reasons, left_ids, right_ids


def _delta(target, reference, column):
    if (
        not isinstance(target, dict)
        or not isinstance(reference, dict)
        or target.get("status") != "measured"
        or reference.get("status") != "measured"
    ):
        return _unmeasured("both matched readings must be measured")
    if not _number(target.get("value")) or not _number(reference.get("value")):
        return _unmeasured("no commensurate scalar readings; distributions are retained separately")
    if target.get("unit") != reference.get("unit"):
        return _unmeasured("different registered measurement units")
    return {
        "status": "measured",
        "target": target["value"],
        "reference": reference["value"],
        "delta": target["value"] - reference["value"],
        "column": column,
        "headline": column in {"G", "V"},
    }


def _compare(target, reference, name, registration):
    reference = _unwrap(reference)
    if not isinstance(reference, dict) or not reference.get("rubric"):
        return {**_unmeasured("no commensurate rubric checkpoint artifact"), "matched": False}
    reasons, target_ids, reference_ids = _match(target, reference, name, registration)
    if reasons:
        return {
            **_unmeasured("; ".join(reasons)),
            "matched": False,
            "reference_life_id": reference.get("life_id"),
            "mismatches": reasons,
        }
    deltas = {
        parameter: {
            column: _delta(
                target.get("rubric", {}).get(parameter, {}).get(column),
                reference["rubric"].get(parameter, {}).get(column),
                column,
            )
            for column in COLUMNS
        }
        for parameter in PARAMETERS
    }
    measured = any(
        value["status"] == "measured" for columns in deltas.values() for value in columns.values()
    )
    return {
        "status": "measured" if measured else "unmeasured",
        "matched": True,
        "reference_life_id": reference.get("life_id"),
        "target_tuple_ids": target_ids,
        "reference_tuple_ids": reference_ids,
        "rubric": deltas,
        **({} if measured else {"reason": "no jointly measured scalar readings"}),
    }


def matched_life_deltas(target_report, controls=None, *, t0=None, registered_contrasts=None):
    """Compare verified artifacts; registered tuple-pair differences stay explicit.

    A list of controls produces separate comparisons with no pooled statistic.
    ``registered_contrasts`` must be loaded from the pinned pre-registration by
    the caller; it names exact sorted ``matching_tuple_id`` sets for each arm.
    """
    target = _unwrap(target_report)
    if not isinstance(target, dict):
        raise TypeError("A target life checkpoint report is required")
    controls = controls or {}
    if not isinstance(controls, dict):
        raise TypeError("Controls must map registered baseline names to reports")
    if set(controls) - set(CONTROL_NAMES):
        raise ValueError("Unknown life-control name")
    result = {}
    for name in CONTROL_NAMES:
        references = controls.get(name)
        if isinstance(references, list):
            result[name] = {
                "references": [
                    _compare(target, reference, name, registered_contrasts)
                    for reference in references
                ],
                "pooled": False,
            }
        else:
            result[name] = _compare(target, references, name, registered_contrasts)
    return {
        "caption": "revealed life parameters: matched differences by column",
        "life_id": target.get("life_id"),
        "checkpoint": target.get("checkpoint"),
        "controls": result,
        "t0": _compare(target, t0, "t0", registered_contrasts),
        "scientific_gate_passed": False,
        "pooled": False,
    }
