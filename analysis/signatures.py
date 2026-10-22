"""F7 repeatability from verified episode reports, stratified by actual condition."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from statistics import mean, variance

from worldd.storage import content_hash

from .report import unmeasured

SIGNATURE_VERSION = "f7-recorded-numeric-signature@1"
FEATURES = {
    "world_actions": ("activity", "world_actions"),
    "model_calls": ("activity", "model_calls"),
    "distinct_content_hashes": ("activity", "distinct_content_hashes"),
    "usd": ("costs", "usd_total"),
    "asks": ("activity", "asks_by_kind"),
    "unique_domains": ("activity", "attempted_domains"),
    "voluntary_stop": ("trajectory", "behavior", "voluntary_stop"),
}
METHOD = {
    "variant": "ICC(1,1): one-way random-effects, single-seed repeatability",
    "targets": "model snapshots",
    "replicates": "the identical balanced seed set within each model",
    "formula": "(MS_between-MS_within)/(MS_between+(k-1)*MS_within)",
    "vector": "trace ICC(1,1) after per-feature sample-variance standardization over this complete balanced cohort",
    "vector_formula": "B=sum(MS_between_j/sample_variance_j); W=sum(MS_within_j/sample_variance_j); ICC=(B-W)/(B+(k-1)*W); constant coordinates contribute zero",
    "threshold": 0.5,
    "comparison": ">",
    "causal_interpretation": False,
    "limitations": "seed effects are residual in this one-way model; this is not a crossed-seed ICC or a claim about latent preferences",
}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _components(groups):
    n, k = len(groups), len(groups[0])
    grand = mean(value for group in groups for value in group)
    between = k * sum((mean(group) - grand) ** 2 for group in groups) / (n - 1)
    within = sum(sum((value - mean(group)) ** 2 for value in group) for group in groups) / (
        n * (k - 1)
    )
    return between, within


def _icc(between, within, k):
    denominator = between + (k - 1) * within
    return (between - within) / denominator if denominator > 0 else None


def _projection(report):
    values = {}
    for name, path in FEATURES.items():
        value = report
        try:
            for key in path:
                value = value[key]
        except (TypeError, KeyError):
            value = None
        if name == "asks":
            value = (
                sum(value.values())
                if isinstance(value, dict) and all(_finite(item) for item in value.values())
                else None
            )
        elif name == "unique_domains":
            value = (
                len(set(value))
                if isinstance(value, list) and all(isinstance(item, str) for item in value)
                else None
            )
        elif name == "voluntary_stop":
            value = int(value) if isinstance(value, bool) else None
        values[name] = float(value) if _finite(value) else None
    return values


def _condition(report):
    config = {key: value for key, value in report.get("configuration", {}).items() if key != "seed"}
    version = {
        key: value
        for key, value in report["tuple"].items()
        if key not in {"model_snapshot_id", "model_value_document_id"}
    }
    return {
        "family": report.get("family"),
        "goal": report.get("goal"),
        "mode": report.get("mode"),
        "budget": report.get("budget"),
        "configuration": config,
        "tuple": version,
    }


def _stratum(condition, rows):
    result = {
        "condition_hash": content_hash(condition),
        "conditions": condition,
        "run_ids": [row["run_id"] for row in rows],
    }
    problems = [row for row in rows if row.get("error")]
    if problems:
        return {
            **result,
            **unmeasured("a source episode is incomplete or unverified", sources=problems),
            "informativeness_gate": unmeasured("source integrity or completeness missing"),
        }
    grouped = defaultdict(dict)
    model_documents = defaultdict(set)
    for row in rows:
        model_documents[row["model"]].add(row.get("model_value_document_id"))
        if row["seed"] in grouped[row["model"]]:
            return {
                **result,
                **unmeasured(
                    "duplicate model/seed observations cannot be treated as independent repetitions"
                ),
                "informativeness_gate": unmeasured("duplicate seed"),
            }
        grouped[row["model"]][row["seed"]] = row["values"]
    if any(len(documents) != 1 for documents in model_documents.values()):
        return {
            **result,
            **unmeasured("a model changed its versioned value document across repeated seeds"),
            "informativeness_gate": unmeasured("inconsistent model version condition"),
        }
    seed_sets = [set(values) for values in grouped.values()]
    models = sorted(grouped)
    if (
        len(models) < 2
        or not seed_sets
        or len(seed_sets[0]) < 2
        or any(seeds != seed_sets[0] for seeds in seed_sets)
    ):
        return {
            **result,
            **unmeasured(
                "requires at least two models and the identical complete set of at least two seeds"
            ),
            "models": models,
            "informativeness_gate": unmeasured("unbalanced or insufficient repeated seeds"),
        }
    seeds = sorted(seed_sets[0])
    features, pooled_between, pooled_within, missing, informative = {}, 0.0, 0.0, [], []
    for feature in FEATURES:
        values = [[grouped[model][seed][feature] for seed in seeds] for model in models]
        if any(value is None for group in values for value in group):
            features[feature] = unmeasured("missing or nonfinite source signature component")
            missing.append(feature)
            continue
        between, within = _components(values)
        coefficient = _icc(between, within, len(seeds))
        features[feature] = {
            "status": "measured" if coefficient is not None else "uninformative_constant",
            "icc": coefficient,
            "ms_between": between,
            "ms_within": within,
            "values_by_model": dict(zip(models, values, strict=True)),
        }
        scale = variance(value for group in values for value in group)
        if scale > 0:
            pooled_between += between / scale
            pooled_within += within / scale
            informative.append(feature)
    coefficient = _icc(pooled_between, pooled_within, len(seeds)) if not missing else None
    gate = (
        {
            "status": "measured",
            "passed": coefficient > 0.5,
            "icc": coefficient,
            "threshold": 0.5,
            "comparison": ">",
        }
        if coefficient is not None
        else unmeasured(
            "signature has missing components or no between/within variation", missing=missing
        )
    )
    return {
        **result,
        "status": "measured" if coefficient is not None else "unmeasured",
        "models": models,
        "seeds": seeds,
        "features": features,
        "informative_features": informative,
        "vector_icc": coefficient,
        "informativeness_gate": gate,
    }


def build_signature_icc(world, run_ids):
    """Read actual World.report fields; caller-supplied score arrays are unsupported."""
    if (
        not isinstance(run_ids, (list, tuple))
        or any(not isinstance(item, str) for item in run_ids)
        or len(set(run_ids)) != len(run_ids)
    ):
        raise ValueError("Supply unique actual run IDs")
    groups, sources, excluded = defaultdict(list), [], []
    for run_id in run_ids:
        run = world.store.get_run(run_id)
        report = world.report(run_id)
        if report.get("family") != "F7":
            excluded.append(
                {
                    "run_id": run_id,
                    "family": report.get("family"),
                    **unmeasured(
                        "F7 signature informativeness is not pooled with another goal family"
                    ),
                }
            )
            continue
        condition = _condition(report)
        model, seed = report["tuple"].get("model_snapshot_id"), run["config"].get("seed")
        records, integrity = world.store.records(run_id), report.get("integrity", {})
        valid = (
            run["state"] == "sealed"
            and not run.get("tainted")
            and report.get("sealed") is True
            and not integrity.get("tainted")
            and integrity.get("record", {}).get("valid") is True
            and integrity.get("ledger", {}).get("valid") is True
            and report.get("completeness", {}).get("verdict") == "complete"
            and isinstance(model, str)
            and bool(model)
            and isinstance(seed, int)
            and not isinstance(seed, bool)
            and all(
                report["tuple"].get(key)
                for key in (
                    "world_id",
                    "harness_id",
                    "policy_id",
                    "price_table_id",
                    "preamble_hash",
                    "tool_set_id",
                )
            )
        )
        try:
            report_hash = content_hash(report)
        except (ValueError, TypeError):
            report_hash, valid = None, False
        receipt = {
            "run_id": run_id,
            "source_record_head": records[-1]["hash"] if records else None,
            "report_hash": report_hash,
            "tuple": report["tuple"],
            "model": model,
            "seed": seed,
        }
        sources.append(receipt)
        row = {
            "run_id": run_id,
            "model": model,
            "seed": seed,
            "values": _projection(report),
            "model_value_document_id": report["tuple"].get("model_value_document_id"),
        }
        if not valid:
            row["error"] = (
                "source must be sealed, complete, untainted and record/ledger verified with explicit model, seed and condition identifiers"
            )
        key = json.dumps(condition, sort_keys=True, separators=(",", ":"))
        groups[key].append(row)
    strata = [_stratum(json.loads(key), values) for key, values in sorted(groups.items())]
    return {
        "status": "measured"
        if strata and all(row["status"] == "measured" for row in strata)
        else "unmeasured",
        "signature_version": SIGNATURE_VERSION,
        "method": METHOD,
        "signature_features": {key: list(value) for key, value in FEATURES.items()},
        "strata": strata,
        "sources": sources,
        "excluded": excluded,
        "informativeness_gate": strata[0]["informativeness_gate"]
        if len(strata) == 1 and not excluded
        else unmeasured("report each actual condition separately; no pooled gate"),
    }
