"""Assemble measured reports and export auditable tables and standalone figures."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
from collections import Counter
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .report import unmeasured


def assemble_life_report(world, life, checkpoints):
    """Assemble source session costs and verified checkpoint matrices without new observations."""
    from .report import PARAMETERS, build_report

    store = world.store
    sessions = [
        build_report(
            store.get_run(session["run_id"]),
            store.records(session["run_id"]),
            ledger=store.spend(session["run_id"]),
            verification={
                "record": store.verify_record(session["run_id"]),
                "ledger": store.reconcile(session["run_id"]),
            },
            measurements=load_measurements(store, session["run_id"]),
        )
        for session in life["sessions"]
    ]
    life_sessions = {session["run_id"]: session for session in life["sessions"]}
    for session in sessions:
        source = life_sessions[session["run_id"]]
        for field in (
            "compactions",
            "resumed_context_messages",
            "resumed_context_bytes",
            "resumed_context_tokens",
        ):
            session["attrition"][field] = source.get(
                field,
                session["attrition"].get(
                    field, unmeasured("the harness did not report this quantity")
                ),
            )
    reports = [item.get("report", item) for item in checkpoints]

    def position(item):
        axes = item.get("matching", {}).get("axes", {})
        calendar = item.get("matching", {}).get("calendar", {})
        return axes.get(
            "calendar_days", calendar.get("exposure_days", 0) + calendar.get("afterlife_days", 0)
        )

    reports.sort(key=position)
    latest = reports[-1] if reports else {}
    matrix = deepcopy(latest.get("rubric")) or {
        parameter: {
            column: unmeasured("no verified life checkpoint reading")
            for column in ("G", "V", "J", "S")
        }
        for parameter in PARAMETERS
    }
    # Retain the last actually elicited self-report as its own S reading.
    # It is neither a replacement for a world observation nor an aggregate.
    for parameter in PARAMETERS:
        if matrix[parameter]["S"].get("status") == "unmeasured":
            for session in reversed(sessions):
                reading = session["rubric"][parameter]["S"]
                if reading.get("status") == "measured":
                    matrix[parameter]["S"] = {**reading, "source_run_id": session["run_id"]}
                    break
    categories = sorted({name for session in sessions for name in session["costs"]["by_category"]})
    costs = {
        name: float(
            sum(
                (
                    Decimal(str(session["costs"]["by_category"].get(name, 0)))
                    for session in sessions
                ),
                Decimal(0),
            )
        )
        for name in categories
    }
    tuples = [session["tuple"] for session in sessions]
    identifiers = {}
    for key in ("policy_id", "price_table_id", "preamble_hash"):
        values = list(dict.fromkeys(version.get(key) for version in tuples))
        identifiers[key] = values[0] if len(values) == 1 else values
    verdicts = [session["completeness"]["verdict"] for session in sessions]
    verdict = (
        "partial"
        if "partial" in verdicts
        else "complete"
        if verdicts and all(value == "complete" for value in verdicts)
        else "unknown"
    )
    columns = {
        column: {
            "status": "measured"
            if any(
                matrix[parameter][column].get("status") == "measured" for parameter in PARAMETERS
            )
            else "unmeasured",
            "headline": column in {"G", "V"},
            "readings": {parameter: matrix[parameter][column] for parameter in PARAMETERS},
            "reason": "no measured readings for this column",
        }
        for column in ("G", "V", "J", "S")
    }
    coverage_curves = {}
    for parameter in PARAMETERS:
        rows = []
        for checkpoint in reports:
            value = checkpoint.get("coverage", {}).get(parameter, {}).get("G")
            axes = checkpoint.get("matching", {}).get("axes", {})
            if (
                isinstance(value, dict)
                and _number(value.get("fraction"))
                and all(
                    _number(axes.get(axis)) for axis in ("usd", "world_actions", "calendar_days")
                )
            ):
                rows.append(
                    {
                        **axes,
                        "value": value["fraction"],
                        "observation_ref": checkpoint.get("provenance", {}).get("config_hash"),
                        "checkpoint": checkpoint["checkpoint"],
                    }
                )
        coverage_curves[parameter] = rows
    report = {
        "schema_version": "landing-party-life-publication/0.1",
        "life_id": life["life_id"],
        "state": life["state"],
        "sealed": bool(life.get("ended")) and all(session["sealed"] for session in sessions),
        "mode": list(dict.fromkeys(session["mode"] for session in sessions)),
        "tuple": tuples,
        "identifiers": identifiers,
        "budget": life["budget"],
        "binding_id": life.get("binding_id"),
        "costs": {
            "usd_total": float(sum((Decimal(str(value)) for value in costs.values()), Decimal(0))),
            "by_category": costs,
            "token_classes": dict(
                sum((Counter(session["costs"]["token_classes"]) for session in sessions), Counter())
            ),
        },
        "activity": {
            "world_actions": sum(session["activity"]["world_actions"] for session in sessions),
            "model_calls": sum(session["activity"]["model_calls"] for session in sessions),
            "human_seconds": sum(session["activity"]["human_seconds"] for session in sessions),
        },
        "axes": latest.get("matching", {}).get(
            "axes", unmeasured("no checkpoint with measured axes")
        ),
        "completeness": {
            "verdict": verdict,
            "reason": "derived from every actual source session, retaining partial and unknown rows",
        },
        "integrity": {
            "tainted": any(session["integrity"]["tainted"] for session in sessions),
            "sessions": {session["run_id"]: session["integrity"] for session in sessions},
        },
        "columns": columns,
        "rubric": matrix,
        "coverage": latest.get("coverage", unmeasured("no coverage reading")),
        "aggregates": latest.get(
            "aggregates",
            {
                "min_G": unmeasured("all four grounded parameters required"),
                "min_V": unmeasured("all four verifiable parameters required"),
            },
        ),
        "shape": latest.get("shape", unmeasured("no classified checkpoint")),
        "caption": "revealed life parameters",
        "sponsor_series": latest.get("sponsor_series", []),
        "matched_deltas": latest.get("matched_deltas", unmeasured("no actual matched controls")),
        "t0": latest.get("t0", unmeasured("no registered t=0 fingerprint")),
        "attrition_rows": [
            {**session["attrition"], "life_id": life["life_id"]} for session in sessions
        ],
        "attrition": {
            "life_id": life["life_id"],
            "started": True,
            "state": life["state"],
            "reason": (life.get("ended") or {}).get("reason", "life ongoing"),
            "completeness": verdict,
            "tainted": any(session["integrity"]["tainted"] for session in sessions),
        },
        "accrual_curves": coverage_curves,
        "checkpoint_order": [item["checkpoint"] for item in reports],
        "unmeasured": {},
        "comparability": latest.get(
            "matched_deltas", unmeasured("matched control comparison has not run")
        ),
        "spec_sheet": {
            "version_tuples": tuples,
            "conditions_by_session": {
                session["run_id"]: session["configuration"] for session in sessions
            },
            "seed_scopes_verbatim": {
                session["run_id"]: session["spec_sheet"]["seed_scope_verbatim"]
                for session in sessions
            },
            "binding_id": life.get("binding_id"),
            "budget_vector": life["budget"],
            "measurements": {
                "calibrations": calibration_inventory(reports),
                "judge_canary_checks": {
                    session["run_id"]: world.services["judge_canaries"].reports_for_run(
                        session["run_id"]
                    )
                    for session in sessions
                }
                if world.services.get("judge_canaries")
                else {},
            },
        },
        "scientific_gate_passed": False,
    }
    report["curves"] = measured_curves(report, {})
    offset_usd, offset_actions, allocation_series = 0.0, 0, {}
    created = datetime.fromisoformat(life["created_at"])
    for session in sessions:
        actual = life_sessions[session["run_id"]]
        calendar_offset = (
            datetime.fromisoformat(actual["started_at"]) - created
        ).total_seconds() / 86400
        for curve in session.get("curves", []):
            if not curve["name"].startswith("allocation_"):
                continue
            key = (curve["name"], curve["column"], curve.get("protocol_hash"))
            target = allocation_series.setdefault(key, {**curve, "points": []})
            for point in curve["points"]:
                if not all(
                    _number(point.get(axis)) for axis in ("usd", "world_actions", "calendar_days")
                ):
                    continue
                target["points"].append(
                    {
                        **point,
                        "source_run_id": session["run_id"],
                        "usd": offset_usd + point["usd"],
                        "world_actions": offset_actions + point["world_actions"],
                        "calendar_days": calendar_offset + point["calendar_days"],
                    }
                )
        offset_usd += session["costs"]["usd_total"]
        offset_actions += session["activity"]["world_actions"]
    report["curves"].extend(allocation_series.values())
    if latest:
        checkpoint_hash = _hash(
            (
                json.dumps(
                    latest,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode()
        )
        folder = (
            store.data_dir
            / "lives"
            / life["life_id"]
            / "publication"
            / checkpoint_hash.split(":", 1)[1]
        )
        manifest_path = folder / "exports.json"
        if manifest_path.is_file() and manifest_path.resolve() == manifest_path:
            manifest = json.loads(manifest_path.read_bytes())
            expected = (
                json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
                + "\n"
            ).encode()
            if manifest.get("files", {}).get("report.json") == _hash(expected):
                figures = []
                for name in manifest.get("figures", []):
                    path = folder / name
                    if (
                        path.resolve() == path
                        and path.is_relative_to(folder)
                        and path.is_file()
                        and path.stat().st_size <= 8_000_000
                    ):
                        body = path.read_bytes()
                        if _hash(body) == manifest["files"].get(name):
                            figures.append(body.decode("utf-8"))
                report["figure_svgs"] = figures
    return report


def _hash(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def record_axes(records):
    """Index actual prefix costs/actions; no later state is copied into an earlier window."""
    usd_nano, actions, by_seq, by_ref = 0, 0, {}, {}
    origin = datetime.fromisoformat(records[0]["ts"]) if records and records[0].get("ts") else None
    for event in records:
        if event["kind"] == "ledger_debit":
            usd_nano += event.get("amount_nano", float(event.get("cost_usd", 0)) * 1e9)
        if event["kind"] == "action" and event.get("world_action", True):
            actions += 1
        point = {
            "usd": usd_nano / 1e9,
            "world_actions": actions,
            "observation_ref": event.get("hash"),
            "seq": event["seq"],
        }
        if origin and event.get("ts"):
            point["calendar_days"] = (
                datetime.fromisoformat(event["ts"]) - origin
            ).total_seconds() / 86400
        by_seq[event["seq"]] = point
        for key in ("hash", "action_id", "event_id", "call_id"):
            if isinstance(event.get(key), str):
                by_ref[event[key]] = point
    return {"seq": by_seq, "refs": by_ref}


def load_measurements(store, run_id):
    """Read only artifacts bound to this terminal source and manifest hashes."""
    root = store.run_dir(run_id).resolve()
    path = root / "analysis/manifest.json"
    if not path.is_file() or path.resolve() != path:
        return {}
    try:
        manifest = json.loads(path.read_bytes())
    except (ValueError, OSError):
        return {"_integrity": {"valid": False, "reason": "measurement manifest is malformed"}}
    if not isinstance(manifest, dict) or not isinstance(manifest.get("stages"), dict):
        return {"_integrity": {"valid": False, "reason": "measurement manifest schema is invalid"}}
    records = store.records(run_id)
    if (
        not records
        or records[-1]["kind"] != "seal"
        or manifest.get("source_record_head") != records[-1]["hash"]
        or manifest.get("run_id") != run_id
    ):
        return {
            "_integrity": {
                "valid": False,
                "reason": "measurement manifest is not bound to this sealed source",
            }
        }
    result, failures = {}, []
    for name, stage in manifest.get("stages", {}).items():
        if not isinstance(stage, dict):
            failures.append(name)
            continue
        reference = stage.get("artifact")
        if not reference:
            result[name] = stage
            continue
        if not isinstance(reference, str):
            failures.append(name)
            continue
        artifact = (root / reference).resolve()
        if (
            not isinstance(reference, str)
            or not reference.startswith("analysis/")
            or not artifact.is_relative_to(root / "analysis")
            or (root / reference).is_symlink()
            or not artifact.is_file()
        ):
            failures.append(name)
            continue
        body = artifact.read_bytes()
        if _hash(body) != stage.get("hash"):
            failures.append(name)
            continue
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise TypeError("stage result must be an object")
            result[name] = value
        except (ValueError, TypeError):
            failures.append(name)
    result["_integrity"] = {
        "valid": not failures,
        "invalid_stages": failures,
        "source_record_head": manifest["source_record_head"],
    }
    result["_manifest"] = manifest
    figures = []
    for name in manifest.get("exports", {}).get("figures", []):
        if not isinstance(name, str):
            continue
        figure = (root / "analysis/publication" / name).resolve()
        if (
            not figure.is_relative_to(root / "analysis/publication")
            or not figure.is_file()
            or figure.is_symlink()
        ):
            continue
        body = figure.read_bytes()
        if len(body) <= 8_000_000 and _hash(body) == manifest["exports"]["files"].get(name):
            figures.append(body.decode("utf-8"))
    result["_figure_svgs"] = figures
    return result


def attach_measurements(report, measurements):
    """Keep individual measurements in their actual G/V/J/S columns."""
    measurements = measurements or {}
    report["measurement_integrity"] = measurements.get(
        "_integrity", {"valid": True, "scope": "pipeline results in memory"}
    )
    report["measurement_readings"] = {}
    completeness = measurements.get("completeness", {})
    if completeness.get("verdict") in {"complete", "partial", "unknown"}:
        report["completeness"] = {
            **completeness,
            "reason": "; ".join(completeness.get("reasons", []))
            or "post-seal transcript and shell audit",
        }
        report["attrition"]["completeness"] = completeness["verdict"]
        report["attrition"]["compactions"] = completeness.get("compaction_count")
        report["spec_sheet"]["completeness"] = report["completeness"]
    if measurements.get("_figure_svgs"):
        report["figure_svgs"] = measurements["_figure_svgs"]
    if measurements.get("_manifest"):
        manifest = measurements["_manifest"]
        report["analysis"] = {
            "stages": manifest.get("stages", {}),
            "measurement_costs": manifest.get("measurement_costs", {}),
        }

    def put(name, value, column, *, replaces=None):
        if not isinstance(value, dict) or value.get("status") in {"failed", "unmeasured"}:
            return
        column = column if column in {"G", "V", "J", "S"} else "J"
        if report["columns"][column].get("status") == "unmeasured":
            report["columns"][column] = {
                "status": "measured",
                "readings": {},
                "headline": column in {"G", "V"},
            }
        report["columns"][column].setdefault("readings", {})[name] = value
        report["measurement_readings"][name] = {
            "column": column,
            "data": value,
            "headline": column in {"G", "V"},
        }
        if replaces:
            report["unmeasured"].pop(replaces, None)

    quote = measurements.get("citation-quotes")
    if quote:
        put("citation_quotes", quote, "V")
        report["columns"]["V"]["citation_quote_check"] = quote
    assessment = measurements.get("checklist-self-assessment")
    if assessment:
        put("checklist_self_assessment", assessment, "S")
        report["spec_sheet"]["checklist_self_assessment"] = assessment
    mitigation = measurements.get("frozen-mitigation")
    if mitigation:
        put("frozen_mitigation_fidelity", mitigation, "V")
        report["spec_sheet"]["frozen_mitigation_fidelity"] = mitigation
    support = measurements.get("citation-support")
    if support and support.get("n", 0):
        put("citation_support", support, support.get("column", "J"), replaces="citation_support")
        report["citation_support"] = support
    for stage, metric in (
        ("goal-recovery", "recovered_goals"),
        ("ask-classification", "ask_classification"),
    ):
        data = measurements.get(stage)
        if data:
            column = data.get("metrics", {}).get("column", "J")
            put(metric, data, column, replaces="judge_ratings")
    for stage, column in (
        ("pairwise", "J"),
        ("value-extraction", None),
        ("belief-metrics", "V"),
        ("belief-checkpoints", "V"),
        ("decision-divergence", "V"),
        ("observer-run", "V"),
        ("grading", "V"),
        ("grading-checkpoints", "V"),
        ("conduct", "V"),
        ("injection-compliance", "V"),
        ("entities", None),
    ):
        data = measurements.get(stage)
        if data:
            put(stage, data, column or data.get("column", "J"))
    formation = measurements.get("formation", {})
    for group in ("prose", "triple_subset"):
        for name, reading in formation.get(group, {}).items():
            if isinstance(reading, dict) and (reading.get("column") or name == "revision_latency"):
                put(f"formation_{group}_{name}", reading, reading.get("column", "V"))
    if formation.get("dropped_goals"):
        put(
            "dropped_goals",
            formation["dropped_goals"],
            formation["dropped_goals"].get("column", "J"),
        )
    if formation.get("cross_seed"):
        put("cross_seed_stability", formation["cross_seed"], "V")
    taste = measurements.get("taste", {})
    taste_columns = taste.get("columns", {})
    for name, column in (("crowd", "G"), ("self_predictions", "S")):
        readings = taste_columns.get(name)
        if isinstance(readings, list) and readings:
            put("taste_" + name, {"status": "measured", "items": readings}, column)
    for index, critic in enumerate(
        taste_columns.get("critics", []) if isinstance(taste_columns.get("critics"), list) else []
    ):
        put(f"taste_critic_{index}", critic, critic.get("column", "J"))
    for name, reading in taste.get("crowd_adjusted_critic_residuals", {}).items():
        if isinstance(reading, dict):
            put("taste_residual_" + name, reading, reading.get("column", "J"))
    for name in (
        "self_prediction_calibration",
        "no_world_prediction_calibration",
        "self_prediction_brier_delta",
        "vindicated_exploration",
    ):
        if isinstance(taste.get(name), dict):
            put(name, taste[name], "V")
    if isinstance(taste_columns.get("sponsor"), list):
        report.setdefault("sponsor_series", []).extend(taste_columns["sponsor"])
    footprint = measurements.get("footprint-derived", {})
    if footprint:
        independent = [
            row for row in footprint.get("uptake", []) if row.get("grounded_eligible") is True
        ]
        if independent:
            put("independent_uptake", {"status": "measured", "items": independent}, "G")
        put(
            "footprint_graph_and_allocation",
            {
                key: value
                for key, value in footprint.items()
                if key not in {"uptake", "non_instrumental_allocation", "scientific_gate_passed"}
            },
            "V",
        )
        report.setdefault("sponsor_series", []).extend(
            row
            for row in footprint.get("uptake", [])
            if row.get("author_class") in {"sponsor", "sibling", "probe"}
        )
    horizon = measurements.get("horizon-failures", {})
    for name, metric in horizon.get("metrics", {}).items():
        if metric.get("measured_segments", 0):
            put("horizon_" + name, metric, metric.get("column", "J"))
    for name, metric in measurements.get("allocation", {}).get("metrics", {}).items():
        if isinstance(metric, dict):
            put("allocation_" + name, metric, metric.get("column", "J"))
    signature = measurements.get("signature-details", {})
    if signature:
        report["revealed_preference_signature"] = signature
        for name, metric in signature.items():
            if isinstance(metric, dict):
                put("signature_" + name, metric, metric.get("column", "V"))
    probes = measurements.get("probes")
    if probes and probes.get("results"):
        put("probe_gains", probes, "V", replaces="probe_gains")
        report["probe_table"] = probes["scores"]
    interview = measurements.get("interview")
    if interview:
        put("interview", interview, "S", replaces="self_report")
        report["reality_belief"] = interview.get(
            "reality_belief", unmeasured("the actual interview omitted a numeric reality rating")
        )
        for parameter, reading in interview.get("revealed", {}).items():
            if parameter in report.get("rubric", {}) and reading.get("status") == "measured":
                report["rubric"][parameter]["S"] = reading
    fidelity = measurements.get("archive-finalisation", {}).get("fidelity")
    if fidelity and fidelity.get("status") == "measured":
        put("replay_fidelity", fidelity, "V", replaces="replay_fidelity")
        report["replay_fidelity"] = fidelity
    report["curves"] = measured_curves(report, measurements)
    report["spec_sheet"]["measurements"] = {
        "judge_id": report["tuple"].get("judge_id"),
        "observer_id": report["tuple"].get("observer_id"),
        "classifier_id": report["tuple"].get("classifier_id"),
        "calibrations": calibration_inventory(measurements),
    }
    if measurements.get("grading", {}).get("protocol"):
        report["spec_sheet"]["goal"].update(
            checklist_hash=measurements["grading"]["protocol"]["hash"],
            checklist_preregistered=measurements["grading"]["preregistered"],
        )
        metadata = measurements["grading"].get("goal_metadata", {})
        if metadata.get("status") != "unmeasured":
            report["spec_sheet"]["goal"].update(metadata)
    return report


def calibration_inventory(value, path=""):
    rows = []
    if isinstance(value, dict):
        if any(key in value for key in ("kappa", "human_agreement", "miss_rate")):
            rows.append(
                {
                    "path": path,
                    **{
                        key: value.get(key)
                        for key in (
                            "judge_id",
                            "critic_id",
                            "prompt_id",
                            "calibration_set_hash",
                            "kappa",
                            "human_agreement",
                            "precision",
                            "recall",
                            "miss_rate",
                            "n",
                            "column",
                        )
                    },
                }
            )
        for key, child in value.items():
            if key not in {"measurement", "measurements", "request", "response", "_manifest"}:
                rows.extend(calibration_inventory(child, f"{path}/{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            rows.extend(calibration_inventory(child, f"{path}/{index}"))
    return rows


def measured_curves(report, measurements):
    curves = []
    trajectory = measurements.get("trajectory", report.get("trajectory", {}))
    for kind in ("domains", "documents"):
        points = sorted(
            trajectory.get("discovery", {}).get(kind, {}).values(),
            key=lambda row: (row["usd"], row["world_actions"]),
        )
        if points:
            curves.append(
                {
                    "name": f"discovered_{kind}",
                    "column": "G",
                    "unit": "distinct",
                    "points": [{**point, "value": index + 1} for index, point in enumerate(points)],
                }
            )
    for name, wrapped in report.get("instruments", {}).items():
        data = wrapped.get("measurement", {})
        if name == "cost_performance":
            curves.append(
                {
                    "name": name,
                    "column": "V",
                    "unit": "registered goal score",
                    "points": [
                        {**row, "value": row["score"]} for row in data.get("checkpoints", [])
                    ],
                }
            )
        elif name == "verifiability_accrual":
            for parameter, rows in data.items():
                if isinstance(rows, list):
                    curves.append(
                        {
                            "name": f"coverage_{parameter}",
                            "column": "G",
                            "unit": "coverage",
                            "points": [{**row, "value": row["coverage_G"]} for row in rows],
                        }
                    )
    for parameter, values in report.get("accrual_curves", {}).items():
        if isinstance(values, list):
            curves.append(
                {
                    "name": f"coverage_{parameter}",
                    "column": "G",
                    "unit": "coverage",
                    "points": values,
                }
            )
    values = measurements.get("value-extraction", {})
    positions = {}
    for window in values.get("windows", []):
        for axis, score in window.get("projection", {}).get("positions", {}).items():
            if _number(score) and window.get("axes"):
                positions.setdefault(axis, []).append({**window["axes"], "value": score})
    curves.extend(
        {
            "name": "revealed_value_" + axis,
            "column": values.get("column", "J"),
            "unit": "frozen value position",
            "points": points,
        }
        for axis, points in positions.items()
    )
    losses = {}
    for row in measurements.get("observer-run", {}).get("rows", []):
        if row.get("axes") and _number(row.get("loss")):
            losses.setdefault((row["observer_id"], row["t"]), []).append(
                {**row["axes"], "value": row["loss"]}
            )
    curves.extend(
        {
            "name": f"observer_loss_{observer}_prefix_{t}",
            "column": "V",
            "unit": "nats per byte",
            "points": points,
        }
        for (observer, t), points in losses.items()
    )
    decisions = measurements.get("decision-divergence", {})
    coordinates = {
        (row["decision_id"], row["k"]): row["axes"]
        for row in decisions.get("rows", [])
        if row.get("axes")
    }
    divergence = {}
    for row in decisions.get("result", {}).get("decisions", []):
        point = coordinates.get((row["decision_id"], row["k"]))
        if point and _number(row.get("divergence")):
            divergence.setdefault(row["k"], []).append({**point, "value": row["divergence"]})
    curves.extend(
        {
            "name": f"decision_divergence_k{k}",
            "column": "V",
            "unit": "1 - measured action probability",
            "points": points,
        }
        for k, points in divergence.items()
    )
    checkpoint_scores = measurements.get("grading-checkpoints", {}).get("measurement", {})
    if checkpoint_scores.get("checkpoints"):
        curves.append(
            {
                "name": "historical_goal_score",
                "column": "V",
                "unit": "frozen core score",
                "points": [
                    {**row, "value": row["score"]} for row in checkpoint_scores["checkpoints"]
                ],
            }
        )
    allocation = measurements.get("allocation", {})
    shares = {}
    for segment in allocation.get("segments", []):
        point = segment.get("axes")
        if not point:
            continue
        metric = segment.get("metrics", {}).get("non_instrumental_allocation", {})
        if _number(metric.get("rate")):
            shares.setdefault(("non_instrumental", metric.get("column", "J")), []).append(
                {**point, "value": metric["rate"]}
            )
        classes = segment.get("metrics", {}).get("content_classes", {})
        for name, reading in classes.get("classes", {}).items():
            if _number(reading.get("action_share")):
                shares.setdefault(("content_" + name, classes.get("column", "J")), []).append(
                    {**point, "value": reading["action_share"]}
                )
    curves.extend(
        {
            "name": "allocation_" + name,
            "column": column,
            "unit": "observed action share",
            "protocol_hash": allocation.get("protocol_hash"),
            "points": points,
        }
        for (name, column), points in shares.items()
    )
    entities = measurements.get("entities", {})
    entity_points = [
        {**point, "value": point.get("distinct_entities", point.get("count", point.get("value")))}
        for point in entities.get("series", [])
        if isinstance(point, dict)
        and _number(point.get("distinct_entities", point.get("count", point.get("value"))))
    ]
    if entity_points:
        curves.append(
            {
                "name": "discovered_entities",
                "column": entities.get("column", "J"),
                "unit": "distinct labelled entities",
                "points": entity_points,
            }
        )
    return [curve for curve in curves if curve["points"]]


def _flatten(value, path=""):
    if isinstance(value, dict):
        if value.get("status") == "unmeasured":
            yield {
                "metric": path,
                "status": "unmeasured",
                "value": None,
                "reason": value.get("reason"),
            }
            return
        for key, child in value.items():
            if key not in {
                "measurement",
                "measurements",
                "request",
                "response",
                "text",
                "body",
                "free_recall",
                "day_reconstruction",
            }:
                yield from _flatten(child, f"{path}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _flatten(child, f"{path}/{index}")
    elif value is None or isinstance(value, (str, int, float, bool)):
        yield {
            "metric": path,
            "status": "measured" if value is not None else "absent",
            "value": value,
            "reason": None,
        }


def _csv(path, rows, fields):
    if path.is_symlink():
        raise ValueError("publication tables cannot be symbolic links")
    path.write_text(_csv_text(rows, fields), encoding="utf-8")


def _csv_text(rows, fields):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(
        {
            key: (
                "'" + value
                if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r"}
                else value
            )
            for key, value in row.items()
        }
        for row in rows
    )
    return output.getvalue()


def render_publication(report, format="markdown"):
    """Serialize an existing report without writes, model calls or live probes."""
    from .report import render_report

    if "publication" in report and "columns" not in report:
        report = report["publication"]

    if format in {"markdown", "md"}:
        return render_report(report)
    if format == "json":
        return (
            json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    identity = report.get("run_id", report.get("life_id", report.get("study_id", "unknown")))
    identifiers = report.get("identifiers", {})
    rows = [
        {**row, "column": column, "run_or_life_id": identity, **identifiers}
        for column, data in report.get("columns", {}).items()
        for row in _flatten(data)
    ]
    if format == "csv":
        return _csv_text(
            rows,
            [
                "run_or_life_id",
                "policy_id",
                "price_table_id",
                "preamble_hash",
                "column",
                "metric",
                "status",
                "value",
                "reason",
            ],
        )
    if format != "html":
        raise ValueError("report format must be json, markdown, html or csv")
    caption = "; ".join(f"{key}={value}" for key, value in report.get("identifiers", {}).items())
    figures = "".join(
        f"<figure>{svg}<figcaption>{html.escape(caption)}</figcaption></figure>"
        for svg in report.get("figure_svgs", [])
    )
    body = "".join(
        f"<tr><td>{html.escape(row['column'])}</td><td>{html.escape(row['metric'])}</td><td>{html.escape(str(row['value'] if row['value'] is not None else row['status']))}</td><td>{html.escape(str(row.get('reason') or ''))}</td></tr>"
        for row in rows
    )
    attrs = html.escape(
        json.dumps(
            report.get("attrition_rows", [report.get("attrition", {})]),
            sort_keys=True,
            ensure_ascii=False,
            indent=2,
        )
    )
    return f'<!doctype html><html lang="en"><meta charset="utf-8"><title>Mission report {html.escape(str(identity))}</title><style>body{{font:16px system-ui;max-width:1400px;margin:2rem auto;padding:0 1rem}}table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;border-bottom:1px solid #ddd;padding:.4rem}}svg{{max-width:100%;height:auto}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style><h1>Mission report {html.escape(str(identity))}</h1><p>{html.escape(caption)}</p><p>G, V, J and S are separate. Missing readings are not zero.</p>{figures}<table><tr><th>Column</th><th>Metric</th><th>Reading</th><th>Reason</th></tr>{body}</table><h2>Attrition</h2><pre>{attrs}</pre></html>'


def export_publication(report, output_dir):
    """Produce portable tables and measured-only plots; missing curves stay absent."""
    from .report import render_report

    root = Path(output_dir)
    if root.resolve() != root.absolute():
        raise ValueError("publication output must not follow symbolic links")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = report.get("run_id", report.get("life_id", report.get("study_id", "unknown")))
    identifiers = report.get("identifiers") or {
        key: report.get("tuple", {}).get(key)
        for key in ("policy_id", "price_table_id", "preamble_hash")
    }
    caption = f"{identity}; " + "; ".join(f"{key}={value}" for key, value in identifiers.items())
    files = []

    def write(name, body):
        path = root / name
        if path.is_symlink():
            raise ValueError("publication artifacts cannot be symbolic links")
        path.write_text(body, encoding="utf-8")
        files.append(name)

    write(
        "report.json",
        json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )
    write("report.md", render_report(report))
    rows = []
    for column, values in report.get("columns", {}).items():
        rows.extend(
            {**row, "column": column, "run_or_life_id": identity, **identifiers}
            for row in _flatten(values)
        )
    _csv(
        root / "readings.csv",
        rows,
        [
            "run_or_life_id",
            "policy_id",
            "price_table_id",
            "preamble_hash",
            "column",
            "metric",
            "status",
            "value",
            "reason",
        ],
    )
    files.append("readings.csv")
    attrition = report.get("attrition_rows") or [report.get("attrition", {})]
    _csv(
        root / "attrition.csv",
        attrition,
        [
            "run_id",
            "life_id",
            "started",
            "state",
            "reason",
            "terminated",
            "truncated",
            "open_tickets_at_end",
            "open_expectations_at_end",
            "completeness",
            "tainted",
            "compactions",
            "resumed_context_messages",
            "resumed_context_bytes",
            "resumed_context_tokens",
        ],
    )
    files.append("attrition.csv")
    plot_points = []
    for curve in report.get("curves", []):
        plot_points.extend(
            {"curve": curve["name"], "column": curve["column"], **point}
            for point in curve["points"]
        )
    _csv(
        root / "curve-points.csv",
        plot_points,
        ["curve", "column", "usd", "world_actions", "calendar_days", "value", "observation_ref"],
    )
    files.append("curve-points.csv")
    figures, omitted = [], []
    matrix = report.get("rubric", {})
    measured_matrix = any(
        cell.get("status") == "measured" for row in matrix.values() for cell in row.values()
    )
    if report.get("curves") or measured_matrix:
        cache = root / ".matplotlib"
        if cache.resolve() != cache.absolute():
            raise ValueError("publication cache must not follow symbolic links")
        cache.mkdir(mode=0o700, exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", str(cache))
        import matplotlib

        matplotlib.use("Agg")
        matplotlib.rcParams["svg.hashsalt"] = str(identity)
        from matplotlib import pyplot as plt

        for index, curve in enumerate(report["curves"]):
            axes = ["usd", "world_actions"] + (["calendar_days"] if "life_id" in report else [])
            missing = [
                axis
                for axis in axes
                if not any(
                    _number(point.get(axis)) and _number(point.get("value"))
                    for point in curve["points"]
                )
            ]
            if missing:
                omitted.append(
                    {
                        "curve": curve["name"],
                        "reason": "actual coordinates missing for " + ", ".join(missing),
                    }
                )
                continue
            figure, panels = plt.subplots(1, len(axes), figsize=(5 * len(axes), 4), squeeze=False)
            for panel, axis in zip(panels[0], axes, strict=True):
                points = sorted(
                    (point[axis], point["value"])
                    for point in curve["points"]
                    if _number(point.get(axis)) and _number(point.get("value"))
                )
                panel.plot(
                    [point[0] for point in points], [point[1] for point in points], marker="o"
                )
                panel.set_xlabel(
                    {
                        "usd": "Source dollars (USD)",
                        "world_actions": "World actions",
                        "calendar_days": "Calendar days",
                    }[axis]
                )
                panel.set_ylabel(f"{curve['column']} · {curve.get('unit', curve['name'])}")
                panel.grid(alpha=0.2)
            figure.suptitle(curve["name"].replace("_", " "))
            casualty = "Attrition: " + "; ".join(
                f"{row.get('run_id', row.get('life_id'))}: {row.get('reason', row.get('state', 'unknown'))}, completeness={row.get('completeness', 'unmeasured')}, tainted={row.get('tainted', 'unknown')}"
                for row in attrition
            )
            figure.text(0.01, 0.01, caption + "\n" + casualty, fontsize=6, wrap=True)
            figure.tight_layout(rect=(0, 0.17, 1, 0.92))
            for extension in ("svg", "png"):
                name = f"figure-{index + 1}.{extension}"
                if (root / name).is_symlink():
                    raise ValueError("publication figures cannot be symbolic links")
                figure.savefig(
                    root / name,
                    dpi=180,
                    metadata={
                        "Description": caption + "\n" + casualty,
                        **({"Date": None} if extension == "svg" else {}),
                    },
                )
                files.append(name)
                if extension == "svg":
                    figures.append(name)
            plt.close(figure)
        if measured_matrix:
            figure, panel = plt.subplots(figsize=(14, 5))
            panel.axis("off")

            def label(cell):
                if cell.get("status") != "measured":
                    return "unmeasured"
                value = cell.get("value", cell.get("ratings", cell.get("n", "measured")))
                text = (
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else str(value)
                )
                if _number(cell.get("coverage")):
                    text += f"\ncoverage={cell['coverage']:.0%}"
                if _number(cell.get("kappa")):
                    text += f"\nkappa={cell['kappa']:.3f}"
                return text

            table = panel.table(
                cellText=[
                    [label(row.get(column, {})) for column in ("G", "V", "J", "S")]
                    for row in matrix.values()
                ],
                rowLabels=list(matrix),
                colLabels=["G · grounded", "V · verifiable", "J · prejudged", "S · self-report"],
                loc="center",
                cellLoc="center",
            )
            table.auto_set_font_size(False)
            table.set_fontsize(9)
            table.scale(1, 3)
            figure.suptitle("Revealed life parameters · separate readings")
            figure.text(
                0.01,
                0.01,
                caption + "\nAttrition: " + json.dumps(attrition, ensure_ascii=False),
                fontsize=6,
                wrap=True,
            )
            figure.tight_layout(rect=(0.06, 0.18, 1, 0.9))
            for extension in ("svg", "png"):
                name = "rubric-matrix." + extension
                if (root / name).is_symlink():
                    raise ValueError("publication figures cannot be symbolic links")
                figure.savefig(
                    root / name,
                    dpi=180,
                    metadata={
                        "Description": caption,
                        **({"Date": None} if extension == "svg" else {}),
                    },
                )
                files.append(name)
                if extension == "svg":
                    figures.append(name)
            plt.close(figure)
    write(
        "report.html",
        render_publication(
            {
                **report,
                "figure_svgs": [(root / name).read_text(encoding="utf-8") for name in figures],
            },
            "html",
        ),
    )
    manifest = {
        "identity": identity,
        "files": {name: _hash((root / name).read_bytes()) for name in files},
        "figures": figures,
        "curve_count": len(report.get("curves", [])),
        "omitted_figures": omitted,
        "scientific_gate_passed": False,
    }
    write("exports.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest
