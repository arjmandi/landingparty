"""Validate the research goal metadata described in design section 9.1."""

import hashlib
import math


def validate_goal_protocol(goal_text, protocol):
    if not isinstance(goal_text, str) or not isinstance(protocol, dict):
        raise TypeError("goal protocol validation needs the actual goal text and metadata object")
    errors = []
    if protocol.get("status") not in {"frozen", "registered"}:
        errors.append("draft metadata must be explicitly frozen before research registration")
    actual_hash = "sha256:" + hashlib.sha256(goal_text.encode()).hexdigest()
    if protocol.get("goal_text_hash") != actual_hash:
        errors.append("goal_text_hash does not pin the exact rendered treatment text")
    if protocol.get("family") not in {f"F{index}" for index in range(1, 11)}:
        errors.append("the actual goal family is required")
    if protocol.get("goal_class") not in {"bounded", "end_to_end"}:
        errors.append("goal_class must be bounded or end_to_end")
    if protocol.get("signal_type") not in {
        "verifiable_by_world",
        "delayed_verification",
        "judged",
        "process_only",
    }:
        errors.append("a registered signal_type is required")
    for name in ("evaluation_protocol", "predicted_behavior", "deliverable_location"):
        value = protocol.get(name)
        if (
            not isinstance(value, str)
            or not value.strip()
            or "to be frozen" in value.casefold()
            or "researcher_selected" in value.casefold()
        ):
            errors.append("actual " + name + " must replace the template placeholder")
    for name in ("G", "V", "J", "S", "predicted_blockers", "likely_asks"):
        value = protocol.get(name)
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            errors.append(name + " must be an explicit list; empty is allowed when inapplicable")
    for name in ("human_time_estimate", "messiness", "budget_multiple"):
        value = protocol.get(name)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
            or (name == "budget_multiple" and value == 0)
        ):
            errors.append("actual nonnegative finite " + name + " is required")
    gaming = protocol.get("gaming_channels", {})
    for name in ("forbidden", "flagged", "counted"):
        if not isinstance(gaming.get(name), list):
            errors.append("gaming channel list is missing: " + name)
    checklist = protocol.get("frozen_checklist")
    if not isinstance(checklist, list):
        errors.append("frozen_checklist must be explicit (empty for process-only goals)")
    elif checklist:
        reference = protocol.get("hidden_items_ref")
        if (
            not isinstance(reference, dict)
            or not reference.get("path")
            or not str(reference.get("hash", "")).startswith("sha256:")
        ):
            errors.append(
                "a rubric with checklist items needs a private hash-pinned hidden-item reference"
            )
    return {
        "valid": not errors,
        "errors": errors,
        "goal_text_hash": actual_hash,
        "metadata": protocol,
        "scientific_gate_passed": False,
    }
