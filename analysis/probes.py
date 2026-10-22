"""Offline scoring for forked probe results and expectation calibration.

This module schedules no model calls and exposes no tools to a live agent.
Probe callers must obtain results in fresh, appropriately mounted contexts.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from urllib.parse import urldefrag

from .report import unmeasured

CELLS = ("pre_off", "pre_on", "post_off", "post_on", "post_mem")
GAINS = {
    "grounding_gain": ("post_on", "post_off"),
    "internalisation": ("post_off", "pre_off"),
    "formation_signal": ("post_mem", "pre_off"),
    "transcript_component": ("post_off", "post_mem"),
}


def _score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError("probe scores and probabilities must be numbers in [0, 1]")
    numeric = Decimal(str(value))
    if not numeric.is_finite() or not 0 <= numeric <= 1:
        raise ValueError("probe scores and probabilities must be numbers in [0, 1]")
    return float(numeric)


def score_probe_cells(results: Iterable[dict], *, fetched_urls: Iterable[str] = ()) -> dict:
    """Compute paired score differences, stratifying own-situation items.

    Rows contain ``item_id``, ``cell``, ``score``, optional ``stratum`` and
    ``source_urls``. An item exposed during the run is excluded from every cell,
    not just the individual contaminated answer. Gains only use matched items.
    These accuracy gaps are not a positive R1 result: R1 needs its registered
    forced-choice kappa, controls, and confidence intervals.
    """
    rows = list(results)
    fetched = {urldefrag(url)[0] for url in fetched_urls}
    excluded = {
        str(row["item_id"])
        for row in rows
        if any(urldefrag(url)[0] in fetched for url in row.get("source_urls", []))
    }
    grouped: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    seen = set()
    for row in rows:
        item = row.get("item_id")
        if not isinstance(item, str) or not item:
            raise ValueError("each result requires a nonempty item_id")
        cell = row.get("cell")
        if cell not in CELLS:
            raise ValueError(f"unknown probe cell {cell!r}")
        stratum = row.get("stratum", "world_mechanics")
        if not isinstance(stratum, str) or not stratum:
            raise ValueError("stratum must be a nonempty name")
        key = (stratum, cell, item)
        if key in seen:
            raise ValueError("duplicate result for the same stratum, cell and item")
        seen.add(key)
        value = _score(row.get("score"))
        if item not in excluded:
            grouped[stratum][cell][item] = value
    strata = {}
    for stratum in sorted({row.get("stratum", "world_mechanics") for row in rows}):
        cells = grouped[stratum]
        measurements = {}
        for cell in CELLS:
            scores = list(cells[cell].values())
            measurements[cell] = (
                {"status": "measured", "n": len(scores), "mean_score": sum(scores) / len(scores)}
                if scores
                else unmeasured("no unexposed scored items in this cell", n=0)
            )
        gains = {}
        for name, (positive, negative) in GAINS.items():
            matched = sorted(set(cells[positive]) & set(cells[negative]))
            gains[name] = (
                {
                    "status": "measured",
                    "n_paired": len(matched),
                    "value": sum(cells[positive][item] - cells[negative][item] for item in matched)
                    / len(matched),
                }
                if matched
                else unmeasured("no matched unexposed items in the required cells", n_paired=0)
            )
        strata[stratum] = {"cells": measurements, "gains": gains}
    return {
        "strata": strata,
        "excluded_item_ids": sorted(excluded),
        "reset_test": unmeasured(
            "accuracy gaps do not establish registered forced-choice kappa or R1 controls"
        ),
    }


def expectation_calibration(
    expectations: Iterable[dict], *, minimum_resolved: int = 30, stratify=True
) -> dict:
    """Brier score over resolved binary expectations; coverage includes open ones."""
    if (
        isinstance(minimum_resolved, bool)
        or not isinstance(minimum_resolved, int)
        or minimum_resolved < 1
    ):
        raise ValueError("minimum_resolved must be a positive integer")
    rows = list(expectations)
    resolved = []
    resolved_total = 0
    outcomes = defaultdict(int)
    for row in rows:
        outcome = row.get("outcome", "open")
        if isinstance(outcome, dict) and "expired" in outcome:
            outcome = "expired"
        if outcome not in {"held", "missed", "open", "expired", "void"}:
            raise ValueError("unrecognized expectation outcome")
        outcomes[outcome] += 1
        resolved_total += outcome in {"held", "missed"}
        if row.get("p") is None:
            continue
        probability = _score(row["p"])
        if outcome in {"held", "missed"}:
            resolved.append((probability, int(outcome == "held")))
    sufficient = len(resolved) >= minimum_resolved
    bins = []
    for index in range(10):
        members = [(p, actual) for p, actual in resolved if min(int(p * 10), 9) == index]
        if members:
            bins.append(
                {
                    "bin": index,
                    "n": len(members),
                    "mean_p": sum(p for p, _ in members) / len(members),
                    "observed_rate": sum(actual for _, actual in members) / len(members),
                }
            )
    strata = {}
    if stratify:
        grouped = defaultdict(list)
        for row in rows:
            resolver = row.get("resolver")
            if isinstance(resolver, dict):
                resolver = resolver.get("kind", resolver.get("type", "structured"))
            grouped[
                f"resolver={resolver or 'unspecified'};venue={row.get('venue', 'unspecified')}"
            ].append(row)
        strata = {
            name: expectation_calibration(group, minimum_resolved=minimum_resolved, stratify=False)
            for name, group in grouped.items()
        }
    return {
        "total": len(rows),
        "resolved": resolved_total,
        "binary_resolved": len(resolved),
        "outcomes": dict(outcomes),
        "resolution_coverage": resolved_total / len(rows) if rows else None,
        "brier": (
            {
                "status": "measured",
                "n": len(resolved),
                "value": sum((probability - outcome) ** 2 for probability, outcome in resolved)
                / len(resolved),
            }
            if sufficient
            else unmeasured(
                f"at least {minimum_resolved} resolved expectations are required", n=len(resolved)
            )
        ),
        "expected_calibration_error": (
            {
                "status": "measured",
                "n": len(resolved),
                "value": sum(
                    item["n"] / len(resolved) * abs(item["mean_p"] - item["observed_rate"])
                    for item in bins
                ),
                "bins": bins,
            }
            if sufficient
            else unmeasured(
                f"at least {minimum_resolved} resolved binary probabilities are required",
                n=len(resolved),
            )
        ),
        "strata": strata,
    }
