"""Registered experiment execution and explicit empirical gate scoring."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import random
from collections import defaultdict
from collections.abc import Callable
from statistics import NormalDist, mean

from .metrics import cohen_kappa


def _hash(value) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
    )


def validate_prereg(spec: dict) -> dict:
    errors = []
    primary = spec.get("primary_metric")
    if not isinstance(primary, dict) or not primary.get("name") or "threshold" not in primary:
        errors.append("one primary metric with a name and threshold is required")
    elif (
        not isinstance(primary["threshold"], (int, float))
        or isinstance(primary["threshold"], bool)
        or not math.isfinite(primary["threshold"])
    ):
        errors.append("the primary threshold must be a finite registered number")
    for field in ("stopping_rule", "likely_null"):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            errors.append(f"{field} is required")
    seeds = spec.get("seeds", spec.get("seed_count"))
    schedule = spec.get("schedule")
    if seeds is None and not schedule:
        errors.append("a seed count or explicit cell schedule is required")
    if isinstance(seeds, int) and (isinstance(seeds, bool) or seeds < 1):
        errors.append("seed_count must be a positive integer")
    if seeds is not None and not isinstance(seeds, (int, list)):
        errors.append("seeds must be a positive count or explicit integer list")
    if isinstance(seeds, list) and (
        not seeds
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in seeds)
        or (all(isinstance(value, int) for value in seeds) and len(set(seeds)) != len(seeds))
    ):
        errors.append("seeds must be a nonempty unique integer list")
    if not spec.get("power_table_ref") and not spec.get("seed_count_rationale"):
        errors.append("a power table or explicit pilot seed-count rationale is required")
    exploratory = spec.get("exploratory_metrics", [])
    if not isinstance(exploratory, list):
        errors.append("exploratory metrics must be a separately labelled list")
    unsigned = {
        key: value for key, value in spec.items() if key not in {"prereg_hash", "prereg_id"}
    }
    return {"valid": not errors, "errors": errors, "prereg_hash": _hash(unsigned)}


def power_table(
    *, effect: float, pilot_sd: float, alpha=0.05, power=0.8, headline=False, no_goal=False
) -> dict:
    """Two-sided paired normal-approximation sizing with its assumptions exposed."""
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in (effect, pilot_sd, alpha, power)
    ):
        raise ValueError("power inputs must be finite numbers")
    if effect <= 0 or pilot_sd <= 0 or not 0 < alpha < 1 or not 0.5 < power < 1:
        raise ValueError(
            "positive effect/variance, valid alpha, and power above one half are required"
        )
    normal = NormalDist()
    calculated = math.ceil(
        ((normal.inv_cdf(1 - alpha / 2) + normal.inv_cdf(power)) * pilot_sd / effect) ** 2
    )
    floor = 20 if no_goal else 10 if headline else 5
    return {
        "method": "paired-normal-approximation",
        "effect": effect,
        "pilot_sd": pilot_sd,
        "alpha": alpha,
        "power": power,
        "calculated_pairs": calculated,
        "required_seeds": max(floor, calculated),
        "design_minimum": floor,
        "assumptions": [
            "independent run pairs",
            "pilot variance transfers",
            "approximately normal paired differences",
        ],
        "requires_pilot_validation": True,
    }


def _interval(values: list[float], confidence=0.95) -> list[float]:
    if not values:
        return [float("nan"), float("nan")]
    ordered = sorted(values)
    low = (1 - confidence) / 2
    return [
        ordered[math.floor(low * (len(ordered) - 1))],
        ordered[math.ceil((1 - low) * (len(ordered) - 1))],
    ]


def score_r1(
    samples: list[dict],
    prereg: dict,
    *,
    bootstrap: int = 1000,
    seed: int = 0,
    scientific: bool = False,
) -> dict:
    """Score paired forced-choice controls with run-level bootstrap intervals.

    Samples supply model, seed, run_id (or run_ids), and equal-length vectors
    reference/store/retest/full_context/shuffled. R1 needs two model snapshots,
    at least five seeds each, and at least forty forced choices per sample.
    Development fixtures can exercise every branch but never authorize a gate.
    RegistryService separately verifies the actual sealed runs and registration.
    """
    registration = validate_prereg(prereg)
    if not registration["valid"]:
        raise ValueError("Invalid pre-registration: " + "; ".join(registration["errors"]))
    if not isinstance(bootstrap, int) or bootstrap < 100:
        raise ValueError("at least 100 bootstrap replicates are required")
    grouped = defaultdict(list)
    seen = set()
    failures = []
    run_ids = []
    scientific_provenance = bool(samples)
    for sample in samples:
        model = sample.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("each R1 sample needs a pinned model snapshot")
        pair = (model, sample.get("seed"))
        if not isinstance(sample.get("seed"), int) or isinstance(sample.get("seed"), bool):
            raise TypeError("each R1 sample needs an integer registered seed")
        if pair in seen:
            raise ValueError("duplicate R1 model/seed cell")
        seen.add(pair)
        vectors = [
            sample.get(key) for key in ("reference", "store", "retest", "full_context", "shuffled")
        ]
        if (
            not all(isinstance(vector, list) and vector for vector in vectors)
            or len({len(vector) for vector in vectors}) != 1
        ):
            raise ValueError("R1 forced-choice vectors must be nonempty equally sized lists")
        if not all(
            isinstance(label, (str, int)) and not isinstance(label, bool)
            for vector in vectors
            for label in vector
        ):
            raise ValueError("forced choices must be categorical string or integer labels")
        if len(vectors[0]) < 40 or len(vectors[0]) != prereg.get("forced_choices", 40):
            failures.append(f"{model}/{sample.get('seed')}: fewer than forty forced choices")
        ids = sample.get("run_ids", [sample.get("run_id")])
        if not ids or any(not isinstance(run_id, str) or not run_id for run_id in ids):
            failures.append(f"{model}/{sample.get('seed')}: missing actual run ids")
        run_ids.extend(run_id for run_id in ids if run_id)
        scientific_provenance &= sample.get("provenance") == "research"
        values = {
            key: cohen_kappa(sample["reference"], sample[key])
            for key in ("store", "retest", "full_context", "shuffled")
        }
        if any(value is None for value in values.values()):
            failures.append(f"{model}/{sample.get('seed')}: degenerate choice distribution")
        grouped[model].append({"sample": sample, "values": values})
    expected_models = prereg.get("models", [])
    if expected_models and set(expected_models) != set(grouped):
        failures.append("observed model snapshots do not match the registered models")
    if len(grouped) < 2:
        failures.append("R1 requires two model snapshots")
    by_model = {}
    rng = random.Random(seed)
    for model, rows in sorted(grouped.items()):
        seeds = {row["sample"]["seed"] for row in rows}
        registered = prereg.get("seeds", prereg.get("seed_count", 5))
        if isinstance(registered, list):
            required = set(registered)
            if seeds != required:
                failures.append(f"{model}: observed seeds differ from registered seeds")
            required_count = len(required)
        else:
            required_count = int(registered)
        if len(rows) != required_count or len(rows) < 5:
            failures.append(f"{model}: insufficient registered seed count")
        metrics = {}
        intervals = {}
        for key in ("store", "retest", "full_context", "shuffled"):
            values = [row["values"][key] for row in rows if row["values"][key] is not None]
            metrics[f"{key}_kappa"] = mean(values) if len(values) == len(rows) and values else None
            replicates = (
                [mean(rng.choices(values, k=len(values))) for _ in range(bootstrap)]
                if values
                else []
            )
            intervals[key] = _interval(replicates) if replicates else None
        metrics["store_minus_retest"] = (
            metrics["store_kappa"] - metrics["retest_kappa"]
            if metrics["store_kappa"] is not None and metrics["retest_kappa"] is not None
            else None
        )
        minimum_delta = prereg["primary_metric"].get("threshold", 0.15)
        if minimum_delta is None or float(minimum_delta) < 0.15:
            failures.append(
                "registered R1 formation threshold may not weaken the design's 0.15 floor"
            )
            minimum_delta = 0.15
        positive = (
            metrics["store_minus_retest"] is not None
            and metrics["store_minus_retest"] >= float(minimum_delta)
            and all(intervals.get(key) for key in ("store", "full_context", "shuffled"))
            and intervals["store"][0] > intervals["full_context"][1]
            and intervals["store"][0] > intervals["shuffled"][1]
        )
        by_model[model] = {
            "n_seeds": len(rows),
            "choices_per_seed": {
                str(row["sample"]["seed"]): len(row["sample"]["reference"]) for row in rows
            },
            "metrics": metrics,
            "confidence_intervals": intervals,
            "positive": bool(positive),
        }
    positive = (
        bool(by_model) and not failures and all(result["positive"] for result in by_model.values())
    )
    aggregate = {
        key: mean(result["metrics"][key] for result in by_model.values())
        for key in (
            "store_kappa",
            "retest_kappa",
            "full_context_kappa",
            "shuffled_kappa",
            "store_minus_retest",
        )
        if by_model and all(result["metrics"][key] is not None for result in by_model.values())
    }
    eligible = scientific and scientific_provenance and not failures
    return {
        "study": "R1",
        "completed": bool(samples) and not failures,
        "positive": positive,
        "verdict": "positive" if positive else "negative",
        "scientific": eligible,
        "eligible_scientific": eligible,
        "prereg_hash": registration["prereg_hash"],
        "prereg_id": registration["prereg_hash"],
        "tuple_id": prereg.get("tuple_id"),
        "run_ids": list(dict.fromkeys(run_ids)),
        "metrics": aggregate,
        "by_model": by_model,
        "gate_failures": failures,
        "bootstrap": {"replicates": bootstrap, "seed": seed, "unit": "run", "confidence": 0.95},
    }


def validate_gate_report(name: str, report: dict) -> list[str]:
    """Structural/scoring validation; RegistryService verifies durable provenance."""
    errors = []
    study = {"r1": "R1", "r5": "R5", "l4": "L4", "irb": "IRB", "shakedown": "shakedown"}.get(
        name.lower(), name
    )
    if report.get("study", "").lower() != study.lower():
        errors.append("report study does not match the requested gate")
    if report.get("completed") is not True:
        errors.append("the study is not completed")
    if study != "IRB":
        if not report.get("run_ids"):
            errors.append("actual run ids are required")
        if not report.get("prereg_hash", report.get("prereg_id")):
            errors.append("pre-registration hash is required")
    metrics = report.get("metrics", {})
    if study == "shakedown":
        if len(set(report.get("run_ids", []))) < 12:
            errors.append("the pilot needs twelve actual runs")
        for metric in (
            "metering_matches_provider",
            "completeness_verdicts",
            "replay_fidelity",
            "block_profile",
            "hash_chain_and_seal",
            "attrition",
            "breaker_calibrated",
        ):
            if metrics.get(metric) is not True:
                errors.append(f"pilot requirement missing: {metric}")
        if metrics.get("probe_bank_items", 0) < 60:
            errors.append("the initial probe bank needs sixty items")
    elif study == "R1":
        models = report.get("by_model", {})
        if len(models) < 2:
            errors.append("R1 requires reports from two models")
        for model, values in models.items():
            measured = values.get("metrics", {})
            if values.get("n_seeds", 0) < 5:
                errors.append(f"{model}: fewer than five seeds")
            if report.get("positive"):
                delta = measured.get("store_minus_retest")
                intervals = values.get("confidence_intervals", {})
                if not isinstance(delta, (float, int)) or not math.isfinite(delta) or delta < 0.15:
                    errors.append(f"{model}: formation gain is below 0.15")
                for key in ("full_context", "shuffled"):
                    store, control = intervals.get("store"), intervals.get(key)
                    if (
                        not isinstance(store, list)
                        or not isinstance(control, list)
                        or len(store) != 2
                        or len(control) != 2
                        or not all(
                            isinstance(value, (float, int)) and math.isfinite(value)
                            for value in store + control
                        )
                        or store[0] <= control[1]
                    ):
                        errors.append(f"{model}: store/control intervals overlap or are missing")
        errors.extend(str(error) for error in report.get("gate_failures", []))
    elif study == "R5":
        for metric in ("asks_per_100_usd", "blocker_to_choice_ratio", "human_minutes"):
            if metric not in metrics:
                errors.append(f"R5 metric missing: {metric}")
    elif study == "L4":
        if (
            "independent_afterlife_inbound" not in metrics
            or metrics.get("days_observed", 0) < 30
            or metrics.get("baseline") != "B3c"
        ):
            errors.append("L4 requires thirty-day independent inbound readings and B3c comparison")
    elif study == "IRB":
        if (
            report.get("decision") != "approved"
            or not report.get("protocol_ref")
            or not report.get("consent_ref")
        ):
            errors.append("an approved protocol and consent references are required")
    else:
        errors.append("unknown gate study")
    return list(dict.fromkeys(errors))


class ExperimentRunner:
    """Run real registered cells through an injected harness, retaining attrition.

    Harness callbacks receive ``(created_run, cell)`` and must use that run's
    model lane/tools. No experiment is silently launched at import or startup.
    """

    def __init__(self, world, run_harness: Callable, *, pipeline=None):
        self.world, self.run_harness, self.pipeline = world, run_harness, pipeline

    async def execute(
        self, prereg: dict, cells: list[dict], *, series_id: str | None = None
    ) -> dict:
        checked = validate_prereg(prereg)
        if not checked["valid"]:
            raise ValueError("; ".join(checked["errors"]))
        if not cells:
            raise ValueError("at least one registered cell is required")
        from worldd.models import RunConfig

        schedules = []
        for cell in cells:
            seeds = cell.get("seeds", prereg.get("seeds", range(prereg.get("seed_count", 5))))
            schedules.append(list(seeds))
        if any(
            not schedule
            or any(not isinstance(seed, int) or isinstance(seed, bool) for seed in schedule)
            or len(schedule) != len(set(schedule))
            for schedule in schedules
        ):
            raise ValueError("each cell requires actual unique integer seed executions")
        rows = []
        reference_tuples = {}
        stopped = None
        execution = [
            (cell, schedule[round_index])
            for round_index in range(max(map(len, schedules)))
            for cell, schedule in zip(cells, schedules, strict=True)
            if round_index < len(schedule)
        ]
        execution_schedule = getattr(self.world, "execution_schedule", None)
        if execution_schedule:
            try:
                execution = execution_schedule(cells, schedules)
                if inspect.isawaitable(execution):
                    execution = await execution
            except Exception as error:  # noqa: BLE001 -- no future execution is invented when a dependency graph is invalid
                stopped = "execution schedule failed: " + type(error).__name__
                execution = []
        for cell, seed in execution:
            resolve_cell = getattr(self.world, "resolve_cell", None)
            if resolve_cell:
                try:
                    resolved = resolve_cell(cell, seed)
                    if inspect.isawaitable(resolved):
                        resolved = await resolved
                    if not isinstance(resolved, dict) or resolved.get("cell_id") != cell.get(
                        "cell_id"
                    ):
                        raise ValueError(
                            "dependency resolution cannot reassign the registered cell"
                        )
                    cell = resolved
                except Exception as error:  # noqa: BLE001 -- preserve started casualties when a later dependency cannot resolve
                    stopped = "cell dependency resolution failed: " + type(error).__name__
                    break
            config = dict(cell["config"], seed=seed)
            registered_series = cell.get("series_id", series_id)
            if registered_series:
                config["series_id"] = registered_series
            created = self.world.create_run(RunConfig.model_validate(config))
            if inspect.isawaitable(created):
                created = await created
            run_id = created["run_id"]
            row = {
                "run_id": run_id,
                "cell": cell.get("cell_id"),
                "seed": seed,
                "status": "started",
            }
            rows.append(row)
            record_started = getattr(self.world, "record_started", None)
            if record_started:
                try:
                    recorded = record_started(created, cell)
                    if inspect.isawaitable(recorded):
                        await recorded
                except Exception as error:  # noqa: BLE001 -- registration failures retain the started casualty
                    stopped = "execution registration failed: " + type(error).__name__
                    row.update(status="stopped", reason=stopped)
                    await self.world.end_run(run_id, reason="world_fault", truncated=True)
                    break
            current_tuple = created.get("tuple", {})
            identity = created.get("series_id") or registered_series or cell.get("cell_id")
            reference_tuple = reference_tuples.setdefault(identity, current_tuple)
            mismatch = [
                key
                for key in reference_tuple
                if key != "archive_id" and reference_tuple[key] != current_tuple.get(key)
            ]
            if mismatch:
                stopped = "tuple mismatch: " + ", ".join(mismatch)
                row.update(status="stopped", reason=stopped)
                await self.world.end_run(run_id, reason="world_fault", truncated=True)
                break
            try:
                outcome = self.run_harness(created, cell)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                row.update(status="completed", result=outcome)
                await self.world.end_run(run_id, reason="harness_stopped")
                if self.pipeline:
                    row["analysis"] = await self.pipeline.run(
                        run_id, options=cell.get("analysis_options", {})
                    )
            except asyncio.CancelledError:
                row.update(status="cancelled", reason="experiment cancelled")
                await self.world.end_run(run_id, reason="operator_stop", truncated=True)
                raise
            except Exception as error:  # noqa: BLE001 -- preserve attrition for arbitrary harness failures
                row.update(status="failed", reason=type(error).__name__)
                await self.world.end_run(run_id, reason="world_fault", truncated=True)
        return {
            "study": prereg.get("experiment_id"),
            "prereg_hash": checked["prereg_hash"],
            "series_id": series_id,
            "completed": stopped is None and all(row["status"] == "completed" for row in rows),
            "scientific": False,
            "run_ids": [row["run_id"] for row in rows],
            "interleaving_log": [
                {key: row[key] for key in ("run_id", "cell", "seed")} for row in rows
            ],
            "attrition": rows,
            "stopped": stopped,
        }
