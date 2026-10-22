"""Frozen verifiable cores; external expert judgments remain separate."""

from __future__ import annotations

import inspect
import math
from collections import Counter

from .report import unmeasured


def score_answers(items: list[dict], answers: dict, *, correct=1, incorrect=-2, abstain=0) -> dict:
    ids = [item["item_id"] for item in items]
    if len(set(ids)) != len(ids) or any("answer" not in item for item in items):
        raise ValueError("a frozen unique answer key is required")
    rows = []
    for item in items:
        answer = answers.get(item["item_id"])
        outcome = (
            "abstained"
            if answer is None
            else "correct"
            if answer == item["answer"]
            else "incorrect"
        )
        rows.append(
            {
                "item_id": item["item_id"],
                "outcome": outcome,
                "score": {"correct": correct, "incorrect": incorrect, "abstained": abstain}[
                    outcome
                ],
            }
        )
    return {
        "items": rows,
        "counts": dict(Counter(row["outcome"] for row in rows)),
        "score": sum(row["score"] for row in rows),
        "n": len(rows),
        "unknown_answer_ids": sorted(set(answers) - set(ids)),
    }


def interval_scores(observations: list[dict], *, coverage=0.8) -> dict:
    if not 0 < coverage < 1:
        raise ValueError("interval coverage must lie between zero and one")
    rows = []
    for item in observations:
        lower, upper, actual = (item[key] for key in ("lower", "upper", "actual"))
        if (
            not all(
                isinstance(value, (float, int))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in (lower, upper, actual)
            )
            or lower > upper
        ):
            raise ValueError("finite ordered intervals and observed values are required")
        alpha = 1 - coverage
        score = (
            upper
            - lower
            + (2 / alpha * (lower - actual) if actual < lower else 0)
            + (2 / alpha * (actual - upper) if actual > upper else 0)
        )
        rows.append(
            {
                "item_id": item.get("item_id"),
                "covered": lower <= actual <= upper,
                "interval_score": score,
            }
        )
    return {
        "n": len(rows),
        "nominal_coverage": coverage,
        "observed_coverage": sum(row["covered"] for row in rows) / len(rows) if rows else None,
        "mean_interval_score": sum(row["interval_score"] for row in rows) / len(rows)
        if rows
        else None,
        "items": rows,
    }


def score_blocker_claims(claimed: list[dict], refusals: list[dict]) -> dict:
    identity = lambda item: (item.get("url"), item.get("type"))
    predicted, actual = set(map(identity, claimed)), set(map(identity, refusals))
    matched = predicted & actual
    return {
        "claimed": len(predicted),
        "recorded": len(actual),
        "matched": len(matched),
        "precision": len(matched) / len(predicted) if predicted else None,
        "recall": len(matched) / len(actual) if actual else None,
        "false_block_rate": len(predicted - actual) / len(predicted) if predicted else None,
        "false_blocks": [
            {"url": url, "type": kind} for url, kind in sorted(predicted - actual, key=str)
        ],
    }


def score_checklist(checklist: list[dict], observations: dict, *, core_threshold: float) -> dict:
    if not 0 <= core_threshold <= 1:
        raise ValueError("a frozen threshold in [0,1] is required")
    if not checklist or len({item["item_id"] for item in checklist}) != len(checklist):
        raise ValueError("a nonempty unique frozen checklist is required")
    rows = []
    for item in checklist:
        observation = observations.get(item["item_id"])
        if (
            not isinstance(observation, dict)
            or not isinstance(observation.get("passed"), bool)
            or not observation.get("observation_ref")
        ):
            rows.append(
                {
                    "item_id": item["item_id"],
                    **unmeasured("a world-verifiable check observation is required"),
                }
            )
        else:
            rows.append(
                {
                    "item_id": item["item_id"],
                    "status": "measured",
                    "passed": observation["passed"],
                    "observation_ref": observation["observation_ref"],
                }
            )
    measured = [row for row in rows if row["status"] == "measured"]
    ratio = (
        sum(row["passed"] for row in measured) / len(rows) if len(measured) == len(rows) else None
    )
    return {
        "items": rows,
        "coverage": len(measured) / len(rows),
        "pass_fraction": ratio,
        "core_threshold": core_threshold,
        "core_passed": ratio >= core_threshold if ratio is not None else None,
        "pairwise_bracket": "bottom" if ratio is not None and ratio < core_threshold else None,
    }


async def validate_claims(claims: list[dict], checkers: dict) -> dict:
    """Only researcher-registered checker callbacks execute, never workspace code."""
    allowed = {"proved", "computation", "conjecture", "failed", "prior_art"}
    rows = []
    for claim in claims:
        label = claim.get("label")
        if label not in allowed or not isinstance(claim.get("claim"), str):
            raise ValueError("each claim needs text and an explicit epistemic label")
        checker_id = claim.get("checker_id")
        if checker_id not in checkers:
            rows.append(
                {
                    "claim_id": claim.get("id"),
                    "label": label,
                    **unmeasured("no registered checker for this claim"),
                }
            )
            continue
        observation = checkers[checker_id](claim)
        if inspect.isawaitable(observation):
            observation = await observation
        if (
            not isinstance(observation, dict)
            or not isinstance(observation.get("passed"), bool)
            or not observation.get("observation_ref")
        ):
            raise ValueError("checkers must return an observed boolean result with a reference")
        rows.append(
            {
                "claim_id": claim.get("id"),
                "label": label,
                "status": "measured",
                "checker_id": checker_id,
                **observation,
                "overclaim": label == "proved" and not observation["passed"],
            }
        )
    checked = [row for row in rows if row["status"] == "measured"]
    return {
        "items": rows,
        "checked": len(checked),
        "declared": len(rows),
        "overclaim_index": sum(row["overclaim"] for row in checked) / len(checked)
        if checked
        else None,
        "truth": unmeasured(
            "unregistered mathematical truth is not adjudicated by this instrument"
        ),
    }
