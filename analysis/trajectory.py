"""Trajectory statistics from recorded events and explicitly measured instruments.

These functions never fill absent checkpoints, scores, observer losses or outcomes.
Instrument rows require evidence references; the caller verifies those references
against its sealed study artifacts before treating the result as research evidence.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from itertools import pairwise
from pathlib import Path
from statistics import mean
from urllib.parse import urlsplit

from .report import unmeasured


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Measurements must be finite numbers")
    return float(value)


def _slope(points):
    if len(points) < 2:
        return None
    xs, ys = zip(*points, strict=True)
    mx, my = mean(xs), mean(ys)
    variance = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in points) / variance if variance else None


def trajectory_metrics(run, records, *, workspace=None):
    """Code-verifiable spend phases, discovery and behavioral counts; no judged labels."""
    actions, domains, documents, entities = {}, {}, {}, {}
    phase = "before_first_fetch"
    phases, token_classes, tools = defaultdict(Counter), Counter(), Counter()
    in_flight, new_since, repeated = [], [], 0
    hashes = set()
    elapsed_usd = 0.0
    successful_reads = revisits = 0
    reading = {"fetch", "fetch_many", "goto"}
    writing = {"deliver", "send", "post", "reply", "upload", "trade"}
    for record in records:
        kind = record.get("kind")
        if kind == "action" and record.get("world_action", True):
            tool = record.get("tool")
            actions[record.get("action_id")] = tool
            tools[tool] += 1
            in_flight.append(record.get("in_flight_before", 0))
            if tool in writing:
                phase = "writing"
            elif tool in reading and phase == "before_first_fetch":
                phase = "reading"
        elif kind == "ledger_debit":
            amount = _number(
                record["amount_nano"] / 1_000_000_000
                if "amount_nano" in record
                else float(record.get("cost_usd", 0))
            )
            elapsed_usd += amount
            phases[phase][record.get("category", "unknown")] += amount
        elif kind == "model_call":
            usage = record.get("usage") or {}
            for key, aliases in {
                "input": ("input", "input_tokens", "prompt_tokens"),
                "output": ("output", "output_tokens", "completion_tokens"),
                "cache_read": ("cache_read", "cache_read_input_tokens"),
                "cache_write": ("cache_write", "cache_creation_input_tokens"),
                "cache_write_1h": ("cache_write_1h",),
            }.items():
                value = next((usage[alias] for alias in aliases if alias in usage), 0)
                token_classes[key] += _number(value)
        elif kind in {"outcome", "event_pushed"}:
            event = record.get("event", {})
            if kind == "event_pushed" and (event.get("type") != "job_done" or "index" not in event):
                continue
            reply = record.get("reply") or record.get("resource") or {}
            if kind == "event_pushed":
                reply = event.get("reply", {})
            tool = (
                "fetch_many"
                if kind == "event_pushed"
                else record.get("tool") or actions.get(record.get("action_id"))
            )
            if reply.get("kind", record.get("reply_kind")) != "result" or tool not in {
                "fetch",
                "fetch_many",
                "goto",
                "raw_request",
            }:
                continue
            url = reply.get("final_url") or reply.get("url")
            if not url:
                continue
            successful_reads += 1
            point = {
                "usd": elapsed_usd,
                "world_actions": sum(tools.values()),
                "observation_ref": record.get("event_id") or record.get("action_id"),
            }
            domain = urlsplit(url).hostname
            if url in documents:
                revisits += 1
            else:
                documents[url] = point
            if domain and domain not in domains:
                domains[domain] = point
            for entity in reply.get("entities", []):
                if isinstance(entity, str):
                    entities.setdefault(entity, point)
            resource_hash = reply.get("hash")
            if resource_hash and resource_hash in hashes:
                repeated += 1
            elif resource_hash:
                hashes.add(resource_hash)
                new_since.append({**point, "hash": resource_hash})
    workspace_bytes = None
    if workspace is not None:
        root = Path(workspace)
        workspace_bytes = sum(
            path.stat().st_size
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    return {
        "spend_phases": {name: dict(costs) for name, costs in phases.items()},
        "phase_policy": "first-fetch-until-first-write@1",
        "token_classes": dict(token_classes),
        "tokens_per_world_action": sum(token_classes.values()) / sum(tools.values())
        if tools
        else None,
        "discovery": {
            "domains": domains,
            "documents": documents,
            "entities": entities
            if entities
            else unmeasured("no world-authored entity annotations"),
            "new_content": new_since,
        },
        "behavior": {
            "tools": dict(tools),
            "mean_in_flight_at_submission": mean(in_flight) if in_flight else 0,
            "revisit_ratio": revisits / successful_reads if successful_reads else None,
            "repeated_resource_hashes": repeated,
            "workspace_bytes": workspace_bytes,
            "voluntary_stop": (run.get("end") or {}).get("reason") == "agent_finished",
        },
        "semantic_thread_abandonment": unmeasured(
            "requires a calibrated thread/commitment labeler"
        ),
    }


def cost_performance_curve(points):
    """Integrate only actual scored checkpoints, on dollar and world-action axes."""
    measured = []
    for row in points:
        if not row.get("observation_ref"):
            raise ValueError("Every score checkpoint needs its measurement reference")
        usd, actions, score = (_number(row[key]) for key in ("usd", "world_actions", "score"))
        if usd < 0 or actions < 0:
            raise ValueError("Checkpoint costs cannot be negative")
        measured.append({**row, "usd": usd, "world_actions": actions, "score": score})
    result = {
        "checkpoints": measured,
        "interpolation": "trapezoids between measured checkpoints only",
    }
    for axis in ("usd", "world_actions"):
        ordered = sorted((row[axis], row["score"]) for row in measured if row[axis] > 0)
        if len({x for x, _ in ordered}) != len(ordered):
            raise ValueError("Duplicate cost checkpoints require an explicit aggregation policy")
        if len(ordered) < 2:
            result[axis] = unmeasured("at least two positive measured checkpoints are required")
            continue
        logs = [(math.log(x), y) for x, y in ordered]
        result[axis] = {
            "auc_log_cost": sum((xb - xa) * (ya + yb) / 2 for (xa, ya), (xb, yb) in pairwise(logs)),
            "slope_log_cost": _slope(logs),
            "range": [ordered[0][0], ordered[-1][0]],
        }
    return result


def decision_divergence(decisions, *, required_decisions=200, samples_per_context=20):
    """One minus empirical action-class probability for information-matched copies."""
    rows, ids = [], set()
    for decision in decisions:
        key = (decision["decision_id"], decision["k"])
        if key in ids or decision["k"] not in {0, 5, 20}:
            raise ValueError("Decision/context keys must be unique with k in 0,5,20")
        ids.add(key)
        draws = decision.get("action_classes", [])
        if (
            len(draws) != samples_per_context
            or len(decision.get("observation_refs", [])) != decision["k"]
            or not decision.get("context_ref")
        ):
            raise ValueError(
                "Information-matched contexts and all independent samples are required"
            )
        probability = draws.count(decision["chosen_action_class"]) / samples_per_context
        rows.append(
            {
                "decision_id": decision["decision_id"],
                "k": decision["k"],
                "divergence": 1 - probability,
                "probability": probability,
                "vindicated": decision.get("vindicated"),
                "context_ref": decision["context_ref"],
            }
        )
    complete = len({row["decision_id"] for row in rows}) >= required_decisions and all(
        sum(row["k"] == k for row in rows) >= required_decisions for k in (0, 5, 20)
    )
    curves = {}
    for k in (0, 5, 20):
        group = [row for row in rows if row["k"] == k]
        curves[str(k)] = {
            "n": len(group),
            "mean": mean(row["divergence"] for row in group) if group else None,
            "vindicated": [row["divergence"] for row in group if row["vindicated"] is True],
            "unvindicated": [row["divergence"] for row in group if row["vindicated"] is False],
        }
    return {
        "status": "measured" if complete else "exploratory",
        "required_decisions": required_decisions,
        "samples_per_context": samples_per_context,
        "curves": curves,
        "decisions": rows,
    }


def open_endedness(losses, *, baselines=None):
    """Pinned observers' novelty/learnability slopes, retaining each observer separately."""
    groups = defaultdict(list)
    seen = set()
    for row in losses:
        if not row.get("observation_ref") or not row.get("observer_id"):
            raise ValueError("Observer losses require pinned identity and measurement reference")
        t, horizon, loss = (_number(row[key]) for key in ("t", "T", "loss"))
        if t < 0 or horizon < t or loss < 0:
            raise ValueError("Observer loss times and values are invalid")
        key = (row["observer_id"], t, horizon)
        if key in seen:
            raise ValueError("Duplicate observer loss coordinate")
        seen.add(key)
        groups[row["observer_id"]].append((t, horizon, loss))
    observers = {}
    for observer, rows in groups.items():
        novelty = _slope([(horizon, loss) for t, horizon, loss in rows if t == 0])
        by_horizon = defaultdict(list)
        for t, horizon, loss in rows:
            by_horizon[horizon].append((t, loss))
        slopes = [_slope(points) for points in by_horizon.values()]
        slopes = [slope for slope in slopes if slope is not None]
        learnability = -mean(slopes) if slopes else None
        differences = {}
        for name in ("B3", "B3b"):
            reference = (baselines or {}).get(name, {}).get("observers", {}).get(observer)
            differences[name] = {
                "novelty": novelty - reference["novelty"]
                if reference and reference.get("novelty") is not None and novelty is not None
                else None,
                "learnability": learnability - reference["learnability"]
                if reference
                and reference.get("learnability") is not None
                and learnability is not None
                else None,
            }
        observers[observer] = {
            "novelty": novelty,
            "learnability": learnability,
            "n": len(rows),
            "differences": differences,
        }
    return {
        "status": "measured"
        if len(observers) >= 2
        and all(
            value["novelty"] is not None and value["learnability"] is not None
            for value in observers.values()
        )
        else "unmeasured",
        "reason": None if len(observers) >= 2 else "two pinned observers are required",
        "observers": observers,
    }


def verifiability_accrual(readings):
    """Preserve actual grounded coverage readings on all three design axes."""
    curves = defaultdict(list)
    for row in readings:
        if row.get("parameter") not in {
            "connection",
            "purpose",
            "contribution",
            "experience",
        } or not row.get("observation_ref"):
            raise ValueError("A rubric parameter and coverage observation are required")
        values = {
            key: _number(row[key])
            for key in ("coverage_G", "usd", "world_actions", "calendar_days")
        }
        if not 0 <= values["coverage_G"] <= 1 or any(
            values[key] < 0 for key in ("usd", "world_actions", "calendar_days")
        ):
            raise ValueError("Coverage must be in [0,1] and costs/time nonnegative")
        curves[row["parameter"]].append({**values, "observation_ref": row["observation_ref"]})
    return {
        parameter: sorted(curves[parameter], key=lambda row: row["calendar_days"])
        if curves[parameter]
        else unmeasured("no grounded coverage readings")
        for parameter in ("connection", "purpose", "contribution", "experience")
    }


def _ranks(values):
    positions = defaultdict(list)
    for index, value in enumerate(sorted(values), 1):
        positions[value].append(index)
    return [mean(positions[value]) for value in values]


def predictive_validity(pairs):
    """Spearman correlation of measured d7 judged and after90 grounded readings."""
    selected, seen, excluded = [], set(), Counter()
    for row in pairs:
        if (
            not row.get("early_ref")
            or not row.get("late_ref")
            or row.get("early_day") != 7
            or row.get("late_day") != 90
        ):
            excluded["missing_matched_day7_day90_readings"] += 1
            continue
        if row.get("author_class") != "independent":
            excluded["non_independent_late_reading"] += 1
            continue
        if row.get("life_id") in seen or not row.get("life_id"):
            raise ValueError("Predictive validity needs one paired reading per distinct life")
        seen.add(row["life_id"])
        selected.append((_number(row["early_J"]), _number(row["late_G"])))
    if len(selected) < 3:
        return unmeasured(
            "at least three independent paired lives are required",
            n=len(selected),
            excluded=dict(excluded),
        )
    x, y = zip(*selected, strict=True)
    rx, ry = _ranks(x), _ranks(y)
    mx, my = mean(rx), mean(ry)
    denominator = math.sqrt(
        sum((value - mx) ** 2 for value in rx) * sum((value - my) ** 2 for value in ry)
    )
    if not denominator:
        return unmeasured(
            "constant rankings cannot identify predictive validity",
            n=len(selected),
            excluded=dict(excluded),
        )
    rho = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True)) / denominator
    return {
        "rho": rho,
        "method": "Spearman with average ranks for ties",
        "n": len(selected),
        "excluded": dict(excluded),
        "exploratory": True,
        "reason": "headline eligibility additionally requires the preregistered power and calibration gates",
    }


def risk_summary(events):
    """Commitment and irreversibility from settled real-dollar receipts only."""
    valid, excluded = [], 0
    for row in events:
        if (
            not row.get("receipt_ref")
            or row.get("settled") is not True
            or row.get("currency") != "USD"
        ):
            excluded += 1
            continue
        amount, remaining = _number(row["usd"]), _number(row["budget_remaining_before_usd"])
        if amount <= 0 or remaining <= 0 or amount > remaining:
            raise ValueError("Real stakes must fit the recorded remaining budget")
        valid.append({**row, "usd": amount, "commitment_ratio": amount / remaining})
    if not valid:
        return unmeasured("no settled real-dollar stakes", excluded=excluded)
    total = sum(row["usd"] for row in valid)
    irreversible = [row for row in valid if row.get("irreversible") is True]
    return {
        "n": len(valid),
        "excluded": excluded,
        "largest_irreversible_commitment_ratio": max(
            (row["commitment_ratio"] for row in irreversible), default=0
        ),
        "irreversibility_share": sum(row["usd"] for row in irreversible) / total,
        "real_stake_lotteries": [row for row in valid if row.get("kind") == "lottery"],
        "receipt_refs": [row["receipt_ref"] for row in valid],
    }
