"""Study summaries over every started episode, with strict pooling gates."""

from __future__ import annotations

import copy
import math
from collections import Counter, defaultdict
from itertools import combinations
from statistics import mean, median, stdev

from .metrics import pass_k
from .publication import load_measurements
from .report import PARAMETERS, build_report, unmeasured


def distribution(values):
    measured = [
        value
        for value in values
        if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
    ]
    return (
        {
            "status": "measured",
            "n": len(measured),
            "missing": len(values) - len(measured),
            "values": measured,
            "mean": mean(measured),
            "median": median(measured),
            "min": min(measured),
            "max": max(measured),
            "sample_sd": stdev(measured) if len(measured) > 1 else None,
        }
        if measured
        else unmeasured("no actual numeric readings", n=0, missing=len(values))
    )


def primary_reading(report, declaration):
    """Use only the preregistered actual G/V report path, never a supplied score."""
    path = declaration.get("source_path")
    if isinstance(path, str):
        path = path.strip("/").split("/")
    if (
        not isinstance(path, list)
        or len(path) < 3
        or path[0] != "columns"
        or path[1] not in {"G", "V"}
    ):
        return unmeasured("primary_metric.source_path must identify an actual G/V report reading")
    value = report
    try:
        for key in path:
            if isinstance(value, dict) and value.get("status") == "unmeasured":
                return value
            value = value[key]
    except (KeyError, TypeError, IndexError):
        return unmeasured("registered primary reading is absent in this started episode")
    if isinstance(value, bool):
        value = int(value)
    if not isinstance(value, (float, int)) or not math.isfinite(value):
        return unmeasured("registered primary reading is not a finite scalar")
    threshold, direction = declaration.get("threshold"), declaration.get("direction", "at_least")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or direction not in {"at_least", "at_most"}
    ):
        return unmeasured(
            "a finite registered threshold and at_least/at_most direction are required"
        )
    return {
        "status": "measured",
        "value": value,
        "passed": value >= threshold if direction == "at_least" else value <= threshold,
        "column": path[1],
        "source_path": path,
        "threshold": threshold,
        "direction": direction,
    }


def reality_strata(rows, *, comparable=False):
    groups = defaultdict(list)
    for row in rows:
        rating = row.get("reality_belief")
        group = (
            f"{min(int(rating // 25), 3) * 25}–{min(int(rating // 25), 3) * 25 + 25}"
            if isinstance(rating, (int, float))
            and not isinstance(rating, bool)
            and 0 <= rating <= 100
            else "unmeasured"
        )
        groups[group].append(row)
    return {
        name: {
            "n_started": len(group),
            "run_ids": [row["run_id"] for row in group],
            "primary_distribution": distribution([row["primary"].get("value") for row in group])
            if comparable
            else unmeasured(
                "unmatched episodes are retained separately inside this reality stratum"
            ),
        }
        for name, group in sorted(groups.items())
    }


def predictive_validity_cohort(world, life_ids):
    """Join registered d7 J and after90 G checkpoints, never caller arrays."""
    from .controls import _match
    from .trajectory import predictive_validity

    pairs = {parameter: [] for parameter in PARAMETERS}
    exclusions, early_reports, comparisons = [], [], []
    for life_id in life_ids:
        life = world.services["lives"].get(life_id)
        observed = world.life_report(life_id)
        checkpoints = {row["report"]["checkpoint"]: row for row in observed["checkpoints"]}
        integrity = observed["publication"]["integrity"]
        if integrity["tainted"] or any(
            value["record"].get("valid") is not True
            or value["ledger"].get("valid") is not True
            or value["ledger"].get("reserved_usd", 0) != 0
            for value in integrity["sessions"].values()
        ):
            exclusions.append(
                {"life_id": life_id, "reason": "source session integrity or billing failed"}
            )
            continue
        if (
            "d7" not in checkpoints
            or "after90" not in checkpoints
            or life.get("afterlife", {}).get("90", {}).get("state") != "complete"
        ):
            exclusions.append(
                {
                    "life_id": life_id,
                    "reason": "actual registered d7 and completed after90 readings are required",
                }
            )
            continue
        early, late = checkpoints["d7"], checkpoints["after90"]
        early_reports.append(early["report"])
        for parameter in PARAMETERS:
            left = early["report"]["rubric"][parameter]["J"]
            right = late["report"]["rubric"][parameter]["G"]
            x, y = left.get("mean_rating"), right.get("value")
            if any(row.get("status") != "measured" for row in (left, right)) or any(
                not isinstance(value, (float, int))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in (x, y)
            ):
                exclusions.append(
                    {
                        "life_id": life_id,
                        "parameter": parameter,
                        "reason": "both measured d7 mean judge rating and after90 grounded ladder are required",
                    }
                )
                continue
            pairs[parameter].append(
                {
                    "life_id": life_id,
                    "early_day": 7,
                    "late_day": 90,
                    "early_J": x,
                    "late_G": y,
                    "early_ref": early["reference"]["artifact_hash"],
                    "late_ref": late["reference"]["artifact_hash"],
                    "early_artifact": early["reference"]["artifact_ref"],
                    "late_artifact": late["reference"]["artifact_ref"],
                    "early_kappa": left.get("kappa"),
                    "author_class": "independent",
                    "late_provenance": "registered rubric G excludes sponsor/sibling/probe and non-free arena readings",
                }
            )
    for left, right in combinations(early_reports, 2):
        reasons, _, _ = _match(left, right, "cohort", None)
        reasons = [reason for reason in reasons if reason != "different matching seed"]
        comparisons.append(
            {
                "left": left["life_id"],
                "right": right["life_id"],
                "comparable": not reasons,
                "reasons": reasons,
            }
        )
    comparable = bool(comparisons) and all(row["comparable"] for row in comparisons)
    return {
        "status": "measured" if any(pairs.values()) else "unmeasured",
        "column": "J",
        "caption": "revealed parameters: predictive validity of d7 J versus after90 G",
        "started_lives": len(life_ids),
        "parameters": {
            parameter: {
                "measurement": predictive_validity(rows)
                if comparable
                else unmeasured(
                    "actual life conditions/calendars/profiles are not comparable for pooling",
                    n=len(rows),
                ),
                "pairs": rows,
                "forty_life_design_minimum_met": len(rows) >= 40,
                "headline_eligible": False,
            }
            for parameter, rows in pairs.items()
        },
        "comparability": {"comparable": comparable, "pairs": comparisons},
        "excluded": exclusions,
        "scientific_gate_passed": False,
    }


def carryover_contrasts(world, study, cells, sources):
    """Match imported/stripped episodes using the frozen dependency graph itself."""
    from worldd.storage import content_hash
    from worldd.study_dependencies import resolve_dependencies

    pairs = study["prereg"].get("carryover_pairs", [])
    if not pairs:
        return unmeasured("no imported/stripped cell pairs were preregistered")
    protocol = study.get("dependency_protocol") or {}
    if (
        not protocol
        or content_hash(protocol.get("protocol")) != protocol.get("hash")
        or study["prereg"].get("dependency_protocol_hash") != protocol["hash"]
    ):
        return unmeasured("carryover needs the actual frozen and registered dependency graph")
    service, registry = world.services["studies"], world.services.get("registry")
    plans = {cell["cell_id"]: cell for cell in study["cells"]}
    results = []
    for pair in pairs:
        if (
            not isinstance(pair, dict)
            or pair.get("imported_cell") not in plans
            or pair.get("stripped_cell") not in plans
        ):
            raise ValueError("carryover pairs must name actual preregistered cells")
        target_plan, control_plan = plans[pair["imported_cell"]], plans[pair["stripped_cell"]]
        imported = {row["seed"]: row for row in cells.get(pair["imported_cell"], [])}
        stripped = {row["seed"]: row for row in cells.get(pair["stripped_cell"], [])}
        rows = []
        for seed in sorted(set(target_plan["seeds"]) | set(control_plan["seeds"])):
            left, right = imported.get(seed), stripped.get(seed)
            errors, receipts = [], {}
            if not left or not right:
                rows.append(
                    {
                        "seed": seed,
                        **unmeasured("both started imported/stripped episodes are required"),
                    }
                )
                continue
            a, b = sources[left["run_id"]], sources[right["run_id"]]
            if a["config"]["learning_mechanism"] != b["config"]["learning_mechanism"]:
                errors.append("learning mechanisms differ")
            varying = {
                "series_id",
                "tuple",
                "iota",
                "preamble_hash",
                "block_profile",
                "continuity_source_run_id",
                "execution_binding",
            }
            different_conditions = [
                key
                for key in set(a["config"]) | set(b["config"])
                if key not in varying and a["config"].get(key) != b["config"].get(key)
            ]
            if different_conditions:
                errors.append(
                    "non-carryover conditions differ: " + ", ".join(sorted(different_conditions))
                )
            if target_plan.get("episode", 1) != control_plan.get("episode", 1):
                errors.append("episode positions differ")
            a_state, b_state = a["config"].get("iota", {}), b["config"].get("iota", {})
            if any(
                a_state.get(key) != b_state.get(key)
                for key in (set(a_state) | set(b_state)) - {"workspace", "beliefs"}
            ):
                errors.append("non-workspace/store initial state differs")
            if not any(
                a_state.get(key, "fresh") != "fresh" for key in ("workspace", "beliefs")
            ) or any(b_state.get(key, "fresh") != "fresh" for key in ("workspace", "beliefs")):
                errors.append("actual imported/stripped states do not match the registered roles")
            for source, plan in ((a, target_plan), (b, control_plan)):
                try:
                    series = registry.get_series(source["config"].get("series_id"))
                    if (
                        series["prereg_hash"] != study["prereg_hash"]
                        or series["created_at"] > source["created_at"]
                    ):
                        raise ValueError("registration does not precede this actual episode")
                    if plan.get("depends_on"):
                        changes, source_receipts = resolve_dependencies(
                            service, plan["depends_on"], seed, study_id=study["study_id"]
                        )
                        if any(
                            source["config"].get(key) != value
                            for key, value in changes.items()
                            if key != "iota"
                        ) or any(
                            source["config"].get("iota", {}).get(key) != value
                            for key, value in changes.get("iota", {}).items()
                        ):
                            raise ValueError(
                                "actual imports do not match source dependency receipts"
                            )
                        receipts[source["run_id"]] = source_receipts
                except (ValueError, TypeError, KeyError, AttributeError):
                    errors.append(
                        "dependency/series authority is missing or changed for " + source["run_id"]
                    )
            if registry:
                try:
                    comparison = registry.comparability(
                        a, b, series_id=a["config"].get("series_id")
                    )
                    errors.extend(
                        reason
                        for reason in comparison["reasons"]
                        if reason
                        not in {
                            "different tuple element: runtime_protocol_id",
                            "different tuple element: preamble_hash",
                        }
                    )
                except (ValueError, TypeError, KeyError):
                    errors.append("registered comparison authority is unavailable")
            else:
                errors.append("actual registry is absent")
            if a["config"].get("budget") != b["config"].get("budget"):
                errors.append("dollar/wall caps differ")
            if (
                left["primary"].get("status") != "measured"
                or right["primary"].get("status") != "measured"
            ):
                errors.append("both registered primary scores must actually be measured")
            row = {
                "seed": seed,
                "imported_run_id": left["run_id"],
                "stripped_run_id": right["run_id"],
                "episode": target_plan.get("episode", 1),
                "learning_mechanism": a["config"]["learning_mechanism"],
                "usd_imported": left["usd"],
                "usd_stripped": right["usd"],
                "dependency_receipts": receipts,
            }
            rows.append(
                {**row, **unmeasured("; ".join(dict.fromkeys(errors)))}
                if errors
                else {
                    **row,
                    "status": "measured",
                    "imported_minus_stripped": left["primary"]["value"] - right["primary"]["value"],
                }
            )
        mechanisms = defaultdict(list)
        for row in rows:
            mechanisms[row.get("learning_mechanism", "unresolved")].append(row)
        results.append(
            {
                "name": pair.get("name", pair["imported_cell"] + " minus " + pair["stripped_cell"]),
                "imported_cell": pair["imported_cell"],
                "stripped_cell": pair["stripped_cell"],
                "rows": rows,
                "by_learning_mechanism": {
                    mechanism: {
                        "status": "measured",
                        "n_pairs": len(group),
                        "gain": mean(row["imported_minus_stripped"] for row in group),
                    }
                    if len(group) >= 5 and all(row["status"] == "measured" for row in group)
                    else unmeasured(
                        "five fully matched started pairs required; unmatched rows are not dropped",
                        n_pairs=len(group),
                    )
                    for mechanism, group in mechanisms.items()
                },
                "pooled_across_learning_mechanisms": False,
            }
        )
    return {
        "contrasts": results,
        "dependency_protocol_hash": protocol["hash"],
        "scientific_gate_passed": False,
    }


class StudyReport:
    def __init__(self, world):
        self.world, self.store = world, world.store

    def build(self, study_id):
        from .experiments import validate_prereg

        study = self.world.services["studies"].get(study_id)
        checked = validate_prereg(study["prereg"])
        prereg_verified = checked["valid"] and checked["prereg_hash"] == study["prereg_hash"]
        registry = self.world.services.get("registry")
        registered_log = registry.execution_log(study_id) if registry else []
        log = registered_log or (study.get("result") or {}).get("attrition", [])
        execution = {row["run_id"]: row for row in log}
        declaration = study["prereg"].get("primary_metric", {})
        cells, source_reports, sources, judge_refs = defaultdict(list), [], {}, []
        life_mode = study.get("execution_kind") == "lives"
        life_checkpoints, life_rows = {}, []
        measurement_jobs = list(study.get("life_analysis", {}).values()) if life_mode else []
        measurements_incomplete = any(job.get("state") != "completed" for job in measurement_jobs)
        canaries = self.world.services.get("canaries")
        for run_id in study["run_ids"]:
            source = copy.deepcopy(self.store.get_run(run_id))
            measured = load_measurements(self.store, run_id)
            pairwise_stage = measured.get("_manifest", {}).get("stages", {}).get("pairwise", {})
            if measured.get("pairwise") and pairwise_stage.get("hash"):
                judge_refs.append(
                    {
                        "run_id": run_id,
                        "path": pairwise_stage["artifact"],
                        "hash": pairwise_stage["hash"],
                    }
                )
            report = build_report(
                source,
                self.store.records(run_id),
                ledger=self.store.spend(run_id),
                verification={
                    "record": self.store.verify_record(run_id),
                    "ledger": self.store.reconcile(run_id),
                },
                measurements=measured,
            )
            if measured.get("completeness", {}).get("verdict"):
                report["completeness"] = measured["completeness"]
                report["attrition"]["completeness"] = measured["completeness"]["verdict"]
            profile = canaries.profile_for_run(run_id) if canaries else {}
            if profile.get("provenance_verified") is True:
                source["config"]["block_profile"] = profile["rates_by_domain_class"]
            sources[run_id] = source
            source_reports.append(report)
            entry = execution.get(run_id, {})
            primary = primary_reading(report, declaration)
            if not prereg_verified:
                primary = unmeasured(
                    "stored primary declaration does not match the frozen study hash"
                )
            if (
                not report["sealed"]
                or report["integrity"]["tainted"]
                or not report["integrity"]["record"]["valid"]
                or not report["integrity"]["ledger"]["valid"]
            ):
                primary = unmeasured("started episode has not passed seal/record/ledger integrity")
            row = {
                "run_id": run_id,
                "cell": entry.get("cell", "unassigned"),
                "seed": source["config"].get("seed"),
                "learning_mechanism": source["config"].get("learning_mechanism"),
                "primary": primary,
                "reality_belief": report.get(
                    "reality_belief", unmeasured("no actual interview reality rating")
                ),
                "attrition": report["attrition"],
                "usd": report["costs"]["usd_total"],
                "world_actions": report["activity"]["world_actions"],
                "tuple": report["tuple"],
            }
            if not life_mode:
                cells[row["cell"]].append(row)
        if life_mode:
            for join in study.get("life_runners", []):
                life_id = join["life_id"]
                life = self.world.services["lives"].get(life_id)
                runner = self.world.services["studies"].get_life_runner(join["runner_id"])
                if (
                    runner.get("life_id") != life_id
                    or runner.get("study_binding", {}).get("study_id") != study_id
                ):
                    raise ValueError(
                        "whole-life report requires its actual registered study worker"
                    )
                if any(session["run_id"] not in sources for session in life["sessions"]):
                    raise ValueError(
                        "whole-life source sessions are missing from actual study execution"
                    )
                observed = self.world.life_report(life_id)
                publication = observed["publication"]
                life_checkpoints[life_id] = observed["rubric"]
                primary = primary_reading(publication, declaration)
                actual_sessions = publication["integrity"]["sessions"].values()
                if (
                    not prereg_verified
                    or not publication["sealed"]
                    or publication["integrity"]["tainted"]
                    or any(
                        entry.get("record", {}).get("valid") is not True
                        or entry.get("ledger", {}).get("valid") is not True
                        or entry.get("ledger", {}).get("reserved_usd", 0) != 0
                        for entry in actual_sessions
                    )
                ):
                    primary = unmeasured(
                        "whole life has not ended with verified source sessions and preregistration"
                    )
                row = {
                    **join,
                    "unit": "life",
                    "cell": join.get("condition_id", join["cell_id"]),
                    "run_id": life_id,
                    "run_ids": [session["run_id"] for session in life["sessions"]],
                    "state": life["state"],
                    "runner_state": runner["state"],
                    "ended": life.get("ended"),
                    "primary": primary,
                    "reality_belief": unmeasured(
                        "session interviews are not a single life-level self-rating"
                    ),
                    "attrition": publication["attrition"],
                    "session_attrition": publication["attrition_rows"],
                    "usd": publication["costs"]["usd_total"],
                    "world_actions": publication["activity"]["world_actions"],
                    "tuple": publication["tuple"],
                    "learning_mechanism": life.get("learning_mechanism"),
                }
                life_rows.append(row)
                cells[row["cell"]].append(row)
        groups = {}
        for name, rows in cells.items():
            comparisons = []
            for left, right in combinations(rows, 2):
                if life_mode:
                    from .controls import _match

                    reasons, _, _ = _match(
                        life_checkpoints[left["life_id"]],
                        life_checkpoints[right["life_id"]],
                        "cohort",
                        None,
                    )
                    # Independent repeats intentionally have different seeds;
                    # all other actual matching descriptors remain mandatory.
                    reasons = [reason for reason in reasons if reason != "different matching seed"]
                    comparisons.append({"comparable": not reasons, "reasons": reasons})
                    continue
                a, b = sources[left["run_id"]], sources[right["run_id"]]
                try:
                    comparison = (
                        registry.comparability(a, b, series_id=a["config"].get("series_id"))
                        if registry
                        else {"comparable": False, "reasons": ["actual registry service is absent"]}
                    )
                except (KeyError, ValueError, TypeError):
                    comparison = {
                        "comparable": False,
                        "reasons": ["actual registered comparison evidence is unavailable"],
                    }
                comparisons.append(comparison)
            comparable = bool(comparisons) and all(row["comparable"] for row in comparisons)
            independent_seeds = len({row["seed"] for row in rows}) == len(rows)
            comparable = comparable and independent_seeds and prereg_verified
            no_goal = all(
                not sources[run_id]["config"].get("goal")
                for row in rows
                for run_id in (row.get("run_ids", []) if life_mode else [row["run_id"]])
            )
            outcomes = [row["primary"].get("passed") for row in rows]
            reliability = {
                str(k): pass_k(
                    outcomes, k, headline=study["prereg"].get("headline") is True, no_goal=no_goal
                )
                if comparable
                else unmeasured(
                    "pooling requires exact registered tuple/goal/archive-week/interleaving/block-profile comparability",
                    started=len(rows),
                )
                for k in range(1, len(rows) + 1)
            }
            groups[name] = {
                "n_started": len(rows),
                "n_unique_seeds": len({row["seed"] for row in rows}),
                "rows": rows,
                "primary_distribution": distribution([row["primary"].get("value") for row in rows])
                if comparable
                else unmeasured("unmatched episodes are retained individually, never pooled"),
                "pass_k": reliability,
                "comparability": {"comparable": comparable, "pairs": comparisons},
                "reality_strata": reality_strata(rows, comparable=comparable),
                "independent_seed_rows": independent_seeds,
                "learning_mechanisms": dict(Counter(row["learning_mechanism"] for row in rows)),
            }
        carryover = carryover_contrasts(self.world, study, cells, sources)
        from .comparisons import build_pairwise_elo
        from .signatures import build_signature_icc

        signature = build_signature_icc(
            self.world,
            [
                run_id
                for run_id, source in sources.items()
                if source["config"].get("family") == "F7"
            ],
        )
        elo = build_pairwise_elo(self.world, judge_refs)
        predictive = (
            predictive_validity_cohort(self.world, [row["life_id"] for row in life_rows])
            if life_mode
            else unmeasured("this episode cohort has no registered lives")
        )
        costs = Counter()
        tokens = Counter()
        for report in source_reports:
            costs.update(report["costs"]["by_category"])
            tokens.update(report["costs"]["token_classes"])
        identifiers = {}
        for key in ("policy_id", "price_table_id", "preamble_hash"):
            values = list(dict.fromkeys(report["tuple"].get(key) for report in source_reports))
            identifiers[key] = values[0] if len(values) == 1 else values
        return {
            "schema_version": "landing-party-study-report/0.1",
            "study_id": study_id,
            "state": study["state"],
            "sealed": bool(source_reports)
            and all(report["sealed"] for report in source_reports)
            and (not life_mode or bool(life_rows) and all(row["ended"] for row in life_rows)),
            "mode": sorted({report["mode"] for report in source_reports}),
            "tuple": [report["tuple"] for report in source_reports],
            "identifiers": identifiers,
            "prereg_hash": study["prereg_hash"],
            "prereg_hash_verified": prereg_verified,
            "primary_declaration": declaration,
            "unit": "life" if life_mode else "episode",
            "n_started": len(life_rows) if life_mode else len(source_reports),
            "n_started_sessions": len(source_reports) if life_mode else None,
            "life_rows": life_rows,
            "requested_measurements": measurement_jobs,
            "cells": groups,
            "carryover_gain": carryover,
            "signature_icc": signature,
            "pairwise_elo": elo,
            "predictive_validity": predictive,
            "reality_stratification": "Fixed quartile-width bins [0,25), [25,50), [50,75), [75,100], plus missing; no claim of causal effects.",
            "costs": {
                "usd_total": sum(costs.values()),
                "by_category": dict(costs),
                "token_classes": dict(tokens),
            },
            "activity": {
                "world_actions": sum(
                    report["activity"]["world_actions"] for report in source_reports
                ),
                "model_calls": sum(report["activity"]["model_calls"] for report in source_reports),
            },
            "columns": {
                "G": {"actual_started_runs": len(source_reports), "spend_usd": sum(costs.values())},
                "V": {
                    "readings": {
                        "cohort": groups,
                        "carryover_gain": carryover,
                        "signature_icc": signature,
                    }
                },
                "J": {"readings": {"pairwise_elo": elo, "predictive_validity": predictive}},
                "S": {
                    "readings": {
                        "reality_belief": [
                            {"run_id": row["run_id"], "rating": row["reality_belief"]}
                            for rows in cells.values()
                            for row in rows
                        ]
                    }
                },
            },
            "rubric": {
                parameter: {
                    column: unmeasured("a run cohort does not aggregate separate life parameters")
                    for column in ("G", "V", "J", "S")
                }
                for parameter in PARAMETERS
            },
            "completeness": {
                "verdict": "partial"
                if measurements_incomplete
                or any(report["completeness"]["verdict"] == "partial" for report in source_reports)
                else "complete"
                if source_reports
                and all(
                    report["completeness"]["verdict"] == "complete" for report in source_reports
                )
                else "unknown",
                "reason": "retains every started episode and its actual verdict; requested measurement failures/uncertainty remain partial",
            },
            "integrity": {
                "tainted": any(report["integrity"]["tainted"] for report in source_reports)
            },
            "attrition_rows": [report["attrition"] for report in source_reports],
            "attrition": {
                "started": len(source_reports),
                "states": dict(Counter(report["state"] for report in source_reports)),
                "requested_measurements": dict(
                    Counter(job.get("state", "unknown") for job in measurement_jobs)
                ),
            },
            "spec_sheet": {
                "version_tuples": [report["tuple"] for report in source_reports],
                "prereg_hash": study["prereg_hash"],
                "primary_metric": declaration,
                "seed_scopes_verbatim": {
                    report["run_id"]: report["spec_sheet"]["seed_scope_verbatim"]
                    for report in source_reports
                },
                "interleaving_log": registered_log,
                "source_reports": [report["run_id"] for report in source_reports],
            },
            "unmeasured": {},
            "scientific_gate_passed": False,
        }
