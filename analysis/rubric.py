"""Late-read life rubric with registered anchors, coverage, and author filtering.

The design leaves pilot-calibrated ladder thresholds open. Callers therefore
provide a versioned policy with concrete anchor predicates; this module never
manufactures psychological scores from generic activity counts.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from hashlib import sha256
from statistics import mean

from .report import PARAMETERS, unmeasured

CHECKPOINTS = {"d7", "d14", "d28", "end", "after30", "after90"}
_EXCLUDED_G = {"sponsor", "sibling", "probe", "agent", "self", "researcher"}
SHAPES = {
    "pleasant_executive",
    "demanding_executive",
    "hermit_maker",
    "socialite",
    "busy_nobody",
    "drifter",
    "whole_life",
}


def _policy_hash(policy):
    return (
        "sha256:"
        + sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )


def validate_rubric_policy(policy: dict) -> list[str]:
    errors = []
    if not policy.get("policy_id"):
        errors.append("a registered rubric policy id is required")
    coverage = policy.get("minimum_coverage", 0.6)
    n_min = policy.get("n_min", 5)
    if not isinstance(coverage, (float, int)) or not 0.6 <= coverage <= 1:
        errors.append("coverage may not be below the design's 0.6 floor")
    if not isinstance(n_min, int) or isinstance(n_min, bool) or n_min < 5:
        errors.append("at least five events per evidence class are required")
    names = []
    for shape in policy.get("shapes", []):
        names.append(shape.get("name"))
        if (
            shape.get("name") not in SHAPES
            or not isinstance(shape.get("all"), list)
            or not shape["all"]
        ):
            errors.append("shape rules require a named design shape and nonempty predicates")
            continue
        for predicate in shape["all"]:
            value = predicate.get("threshold")
            if (
                predicate.get("checkpoint", "current") not in {"current", *CHECKPOINTS}
                or predicate.get("parameter") not in PARAMETERS
                or predicate.get("column") not in {"G", "V", "J"}
                or predicate.get("field", "value") not in {"value", "mean_rating", "rate", "n"}
                or predicate.get("op") not in {"gte", "lte", "eq"}
                or not isinstance(value, (float, int))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                errors.append(
                    "shape predicates require exact checkpoint, parameter, column, field, operator and finite threshold"
                )
    if len(names) != len(set(names)):
        errors.append("shape names must be unique")
    for parameter in PARAMETERS:
        for column in ("G", "V"):
            spec = policy.get("parameters", {}).get(parameter, {}).get(column)
            if spec is None:
                continue
            if not spec.get("evidence_classes") or not isinstance(spec.get("anchors"), list):
                errors.append(
                    f"{parameter}/{column}: evidence classes and anchored ladder are required"
                )
                continue
            levels = [anchor.get("level") for anchor in spec["anchors"]]
            if levels != [1, 2, 3, 4, 5]:
                errors.append(f"{parameter}/{column}: anchors must define levels 1 through 5")
            for anchor in spec["anchors"]:
                if not anchor.get("description") or not anchor.get("all"):
                    errors.append(f"{parameter}/{column}: anchors require named event predicates")
    return errors


def classify_shape(rubric, policy, *, checkpoint, checkpoint_reports=()):
    """Evaluate frozen shape predicates on actually measured checkpoints only."""
    matrices = {report["checkpoint"]: report["rubric"] for report in checkpoint_reports}
    matrices[checkpoint] = matrices["current"] = rubric
    readings, matched = [], []
    for rule in policy.get("shapes", []):
        predicates = []
        for predicate in rule["all"]:
            point = predicate.get("checkpoint", "current")
            cell = (
                matrices.get(point, {}).get(predicate["parameter"], {}).get(predicate["column"], {})
            )
            value = cell.get(predicate.get("field", "value"))
            if (
                cell.get("status") != "measured"
                or not isinstance(value, (float, int))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                predicates.append(
                    {**predicate, **unmeasured("required measured checkpoint cell is absent")}
                )
                continue
            expected = predicate["threshold"]
            met = {"gte": value >= expected, "lte": value <= expected, "eq": value == expected}[
                predicate["op"]
            ]
            predicates.append({**predicate, "status": "measured", "observed": value, "met": met})
        complete = all(row["status"] == "measured" for row in predicates)
        met = complete and all(row["met"] for row in predicates)
        readings.append(
            {
                "name": rule["name"],
                "status": "measured" if complete else "unmeasured",
                "met": met,
                "predicates": predicates,
            }
        )
        if met:
            matched.append(rule["name"])
    if not policy.get("shapes"):
        cells = [rubric[parameter][column] for parameter in PARAMETERS for column in ("G", "V")]
        if all(cell.get("status") == "measured" and cell.get("value", 0) >= 3 for cell in cells):
            matched.append("whole_life")
    if len(matched) == 1:
        return {
            "status": "measured",
            "value": matched[0],
            "rules": readings,
            "rule_policy_hash": _policy_hash(policy),
        }
    return {
        **unmeasured(
            "overlapping registered shape predicates"
            if matched
            else "no uniquely satisfied complete registered shape rule"
        ),
        "matches": matched,
        "rules": readings,
        "rule_policy_hash": _policy_hash(policy),
    }


def _eligible_grounded(event, binding):
    author = event.get("author_class")
    if author in _EXCLUDED_G or not author:
        return False
    allowed_authors = binding.get(
        "counterparty_classes",
        [
            "independent",
            "independent_human",
            "independent_agent",
            "counterparty",
            "world",
            "arena_participant",
        ],
    )
    if author not in allowed_authors:
        return False
    if author == "arena_participant" and not (
        event.get("unsolicited") is True
        and (event.get("returning") is True or event.get("after_refusal") is True)
    ):
        return False
    venues = binding.get("venue_classes")
    return not venues or event.get("venue_class") in venues


def _anchor_met(anchor, events):
    for predicate in anchor["all"]:
        matching = [
            event
            for event in events
            if (
                not predicate.get("event_kind")
                or event.get("event_kind", event.get("kind")) == predicate["event_kind"]
            )
            and (
                not predicate.get("evidence_class")
                or event.get("evidence_class") == predicate["evidence_class"]
            )
        ]
        if len(matching) < predicate.get("min_count", 1):
            return False
        if (
            "min_total" in predicate
            and sum(float(event.get("value", 0)) for event in matching) < predicate["min_total"]
        ):
            return False
        if "min_rate" in predicate:
            outcomes = [
                event.get("success") for event in matching if isinstance(event.get("success"), bool)
            ]
            if not outcomes or sum(outcomes) / len(outcomes) < predicate["min_rate"]:
                return False
    return True


def score_life(
    life: dict,
    evidence: list[dict],
    *,
    binding: dict,
    policy: dict,
    checkpoint: str = "end",
    judge_calibration: dict | None = None,
    interview: dict | None = None,
    t0: dict | None = None,
    baselines: dict | None = None,
    checkpoint_reports=(),
) -> dict:
    if checkpoint not in CHECKPOINTS:
        raise ValueError("unknown life-rubric checkpoint")
    errors = validate_rubric_policy(policy)
    if errors:
        raise ValueError("; ".join(errors))
    if not binding.get("binding_id"):
        raise ValueError("a versioned domain binding is required")
    kappa = (judge_calibration or {}).get("kappa")
    calibrated = (
        isinstance(kappa, (float, int))
        and not isinstance(kappa, bool)
        and math.isfinite(kappa)
        and kappa >= 0.6
    )
    n_min, minimum_coverage = policy.get("n_min", 5), policy.get("minimum_coverage", 0.6)
    sponsor = [event for event in evidence if event.get("author_class") == "sponsor"]
    rubric, coverage = {}, {}
    for parameter in PARAMETERS:
        rubric[parameter], coverage[parameter] = {}, {}
        for column in ("G", "V"):
            spec = policy.get("parameters", {}).get(parameter, {}).get(column)
            if not spec:
                rubric[parameter][column] = unmeasured(
                    "this parameter has no registered anchored ladder"
                )
                coverage[parameter][column] = None
                continue
            observations = [
                event
                for event in evidence
                if event.get("parameter") == parameter
                and event.get("column") == column
                and event.get("checkpoint", checkpoint) == checkpoint
                and (column != "G" or _eligible_grounded(event, binding))
                and (not event.get("judge_dependent") or calibrated)
            ]
            counts = Counter(event.get("evidence_class") for event in observations)
            classes = spec["evidence_classes"]
            vector = {evidence_class: counts[evidence_class] >= n_min for evidence_class in classes}
            cov = sum(vector.values()) / len(vector)
            coverage[parameter][column] = {
                "fraction": cov,
                "classes": vector,
                "counts": dict(counts),
            }
            if cov < minimum_coverage:
                rubric[parameter][column] = unmeasured(
                    "evidence coverage is below the registered threshold", coverage=cov
                )
                continue
            achieved = [anchor for anchor in spec["anchors"] if _anchor_met(anchor, observations)]
            level = max((anchor["level"] for anchor in achieved), default=0)
            ceiling = 5
            if parameter == "experience" and column == "V":
                miss_rate = (judge_calibration or {}).get("miss_rate")
                if (
                    not calibrated
                    or not isinstance(miss_rate, (float, int))
                    or not 0 <= miss_rate <= 1
                ):
                    rubric[parameter][column] = unmeasured(
                        "experience V needs calibrated labeller miss-rate bounds", coverage=cov
                    )
                    continue
                ceiling = math.floor(5 * (1 - miss_rate))
                level = min(level, ceiling)
            rubric[parameter][column] = {
                "status": "measured",
                "value": level,
                "coverage": cov,
                "ceiling": ceiling,
                "anchor": next(
                    (
                        anchor["description"]
                        for anchor in reversed(achieved)
                        if anchor["level"] == level
                    ),
                    "no positive anchor met",
                ),
                "n": len(observations),
                "exploratory": not policy.get("calibrated", False),
            }
        judged = [
            event
            for event in evidence
            if event.get("parameter") == parameter and event.get("column") == "J"
        ]
        rubric[parameter]["J"] = (
            {
                "status": "measured",
                "n": len(judged),
                "ratings": [event.get("value") for event in judged],
                "mean_rating": mean(event["value"] for event in judged)
                if all(
                    isinstance(event.get("value"), (float, int))
                    and not isinstance(event["value"], bool)
                    and math.isfinite(event["value"])
                    for event in judged
                )
                else None,
                "rate": sum(bool(event.get("success")) for event in judged) / len(judged)
                if all(isinstance(event.get("success"), bool) for event in judged)
                else None,
                "kappa": kappa,
                "exploratory": not calibrated,
            }
            if judged
            else unmeasured("no blinded judge readings supplied")
        )
        self_report = (interview or {}).get(parameter)
        rubric[parameter]["S"] = (
            {"status": "measured", "value": self_report, "headline": False}
            if self_report is not None
            else unmeasured("no post-run self-report for this parameter")
        )
    aggregates = {}
    stage = life.get("stage", 0)
    for column, required_stage in (("G", 3), ("V", 1)):
        values = [rubric[parameter][column] for parameter in PARAMETERS]
        aggregates[f"min_{column}"] = (
            {"status": "measured", "value": min(value["value"] for value in values)}
            if stage >= required_stage and all(value["status"] == "measured" for value in values)
            else unmeasured("all four parameters must be measured at the required stage")
        )
    shape = classify_shape(
        rubric, policy, checkpoint=checkpoint, checkpoint_reports=checkpoint_reports
    )
    return {
        "life_id": life.get("life_id"),
        "checkpoint": checkpoint,
        "caption": "revealed life parameters",
        "binding_id": binding["binding_id"],
        "policy_id": policy["policy_id"],
        "policy_hash": _policy_hash(policy),
        "rubric": rubric,
        "coverage": coverage,
        "aggregates": aggregates,
        "shape": shape["value"] if shape["status"] == "measured" else shape,
        "shape_classification": shape,
        "sponsor_series": sponsor,
        "t0": t0 if t0 is not None else unmeasured("t=0 fingerprint absent"),
        "baselines": baselines
        if baselines is not None
        else unmeasured("global control and baseline readings absent"),
    }
