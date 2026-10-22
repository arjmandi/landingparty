"""Belief measurements from exact world-record budget prefixes, never final folds."""

from __future__ import annotations

import math
from collections import Counter
from decimal import ROUND_CEILING, Decimal

from worldd.beliefs import formation_records

from .metrics import belief_metrics, revision_metrics
from .report import unmeasured


def _coordinates(records, seq):
    prefix = [row for row in records if row["seq"] <= seq]
    internal = {
        row.get("action_id")
        for row in prefix
        if row["kind"] == "outcome" and row.get("world_action") is False
    }
    return (
        sum(row["kind"] == "action" and row.get("action_id") not in internal for row in prefix),
        sum(row.get("amount_nano", 0) for row in prefix if row["kind"] == "ledger_debit")
        / 1_000_000_000,
    )


def _expectations(prefix):
    heads = {}
    for row in prefix:
        if row["kind"] in {
            "expectation_open",
            "expectation_resolved",
            "expectation_resolution_replayed",
        }:
            value = row.get("expectation")
            if isinstance(value, dict):
                heads[value["id"]] = value
    return list(heads.values())


def _read_ids(value):
    if isinstance(value, list):
        return {key for row in value for key in _read_ids(row)}
    if isinstance(value, dict):
        return {value["id"]} if isinstance(value.get("id"), str) else set()
    return set()


def _revision_rows(service, run_id, snapshot, prefix, *, reflection_policy="none"):
    version_seq = {row["version_id"]: row["seq"] for row in prefix if row.get("version_id")}
    observation_seq = {
        row["observation_ref"]: row["seq"] for row in prefix if row["kind"] == "belief_observation"
    }
    actions = {row["action_id"]: row for row in prefix if row["kind"] == "action"}
    reads = [
        row
        for row in prefix
        if row["kind"] == "outcome"
        and actions.get(row.get("action_id"), {}).get("tool") == "beliefs"
        and row.get("reply", {}).get("kind") == "result"
    ]
    rows = []
    for observation in snapshot["observations"]:
        seq = observation_seq.get(observation["observation_ref"])
        if seq is None:  # Imported old evidence is an initial condition, not new local latency.
            continue
        before = service.checkpoint_fold(run_id, seq - 1)["beliefs"]
        state = {row["id"]: row for row in before}
        for belief_id in service._attachments(state, observation):
            record = state[belief_id]
            if not formation_records([record], reflection_policy=reflection_policy)["included_n"]:
                continue
            triple, other = record.get("triple"), observation.get("triple")
            known = "supports" in observation or bool(
                triple
                and other
                and triple["subject"] == other.get("subject")
                and triple["predicate"] == other.get("predicate")
            )
            if not known:
                continue
            needed = observation.get("supports") is False or bool(
                triple and other and triple["object"] != other.get("object")
            )
            changes = [
                event
                for event in snapshot["semantic_events"]
                if event["belief"]["id"] == belief_id
                and event["belief"]["op"] in {"revise", "retract"}
                and version_seq.get(event["belief"]["version_id"], 0) > seq
            ]
            changed = (
                min(changes, key=lambda event: version_seq[event["belief"]["version_id"]])
                if changes
                else None
            )
            revised_seq = version_seq[changed["belief"]["version_id"]] if changed else None
            delivered = [
                row
                for row in reads
                if row["seq"] > seq
                and (revised_seq is None or row["seq"] <= revised_seq)
                and belief_id in _read_ids(row.get("reply", {}).get("data"))
            ]
            read_seq = min((row["seq"] for row in delivered), default=None)
            action, usd = _coordinates(prefix, seq)
            revised_action, revised_usd = (
                _coordinates(prefix, revised_seq) if revised_seq is not None else (None, None)
            )
            rows.append(
                {
                    "belief_id": belief_id,
                    "observation_ref": observation["observation_ref"],
                    "evidence_action": action,
                    "evidence_usd": usd,
                    "read_action": _coordinates(prefix, read_seq)[0]
                    if read_seq is not None
                    else None,
                    "revised_action": revised_action,
                    "revised_usd": revised_usd,
                    "revision_needed": needed,
                    "change_class": record["change_class"],
                    "rank": record["rank"],
                    "triple": bool(triple),
                    "world_expectation_id": observation.get("expectation", {}).get("id")
                    if observation.get("expectation", {}).get("outcome") in {"held", "missed"}
                    and observation["expectation"].get("grounded", True)
                    and observation["expectation"].get("headline_eligible", True)
                    else None,
                    "read_observability": "delivered_belief_queries_only",
                }
            )
    return rows


def _churn(snapshot, *, reflection_policy="none", triple_only=False):
    heads, drivers = {}, Counter()
    for event in snapshot["semantic_events"]:
        row = event["belief"]
        old = heads.get(row["id"])
        confirmed = reflection_policy != "companion" or (
            old and old.get("author", "agent") == "agent" and bool(old.get("reconfirmed_from"))
        )
        if (
            old
            and confirmed
            and row["op"] in {"revise", "retract"}
            and (not triple_only or old.get("triple"))
        ):
            previous = {item["ref"] for item in old["evidence"]}
            linked = any(
                item["ref"] not in previous and item["class"] != "self_inference"
                for item in row["evidence"]
            )
            drivers[
                "new_nonself_evidence_linked" if linked else "without_new_nonself_reference"
            ] += 1
        heads[row["id"]] = row
    total = sum(drivers.values())
    return {
        "status": "measured",
        "n": total,
        "counts": dict(drivers),
        "rates": {key: value / total for key, value in drivers.items()} if total else {},
        "definition": "new nonself evidence references versus unlinked revision churn",
        "causal_claim": False,
        "subset": "prior_triples" if triple_only else "all_prior_records",
        "confirmation_rule": "agent_reconfirmed_before_revision"
        if reflection_policy == "companion"
        else "all_records",
    }


def _preservation(service, run_id, snapshot, prefix):
    violations, checked = [], 0
    for row in prefix:
        if row["kind"] != "belief_observation":
            continue
        observation = next(
            (
                item
                for item in snapshot["observations"]
                if item["observation_ref"] == row["observation_ref"]
            ),
            None,
        )
        if observation is None:
            continue
        before = {
            item["id"]: item for item in service.checkpoint_fold(run_id, row["seq"] - 1)["beliefs"]
        }
        after = {
            item["id"]: item for item in service.checkpoint_fold(run_id, row["seq"])["beliefs"]
        }
        related = set(service._attachments(before, observation))
        frontier = related.copy()
        for _ in range(3):
            children = {
                key
                for key, belief in before.items()
                if any(
                    link["relation"] == "derived_from" and link["to"] in frontier
                    for link in belief["links"]
                )
            }
            frontier = children - related
            related.update(children)
        for key in before.keys() & after.keys() - related:
            if before[key] != after[key]:
                violations.append(
                    {
                        "belief_id": key,
                        "observation_ref": observation["observation_ref"],
                        "changed_fields": sorted(
                            field
                            for field in before[key].keys() | after[key].keys()
                            if before[key].get(field) != after[key].get(field)
                        ),
                    }
                )
        checked += 1
    return {
        "status": "measured",
        "events_checked": checked,
        "n": len(violations),
        "items": violations,
    }


def _rank_curve(rows):
    bins = []
    for rank in range(6):
        resolved = {}
        for row in rows:
            if row["rank"] == rank and row["world_expectation_id"]:
                key = row["world_expectation_id"]
                resolved[key] = resolved.get(key, False) or row["revision_needed"]
        count = len(resolved)
        contradicted = sum(resolved.values())
        bins.append(
            {
                "rank": rank,
                "resolved_expectations": count,
                "world_contradicted": contradicted,
                "fraction": contradicted / count if count >= 30 else None,
                "status": "measured" if count >= 30 else "unmeasured",
            }
        )
    return {
        "minimum_resolved_per_bin": 30,
        "bins": bins,
        "definition": "world-resolved expectation contradiction by prior belief rank; no truth adjudication",
    }


def belief_checkpoints(world, run_id, *, fractions=(0.1, 0.25, 0.5, 1.0)):
    """Measure reached budget fractions using immutable world/semantic prefixes."""
    store, service = world.store, world.services.get("beliefs")
    run = store.get_run(run_id)
    if service is None:
        return unmeasured("belief service is unavailable")
    if (
        run["state"] not in {"sealed", "ended", "killed"}
        or run.get("tainted")
        or store.verify_record(run_id).get("valid") is not True
        or store.reconcile(run_id).get("valid") is not True
    ):
        raise ValueError(
            "belief checkpoints require a sealed verified untainted source and reconciled ledger"
        )
    if not fractions or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= 1
        for value in fractions
    ):
        raise ValueError("budget fractions must lie in (0,1]")
    cap = Decimal(str(run["config"]["budget"]["usd"])) * 1_000_000_000
    records = store.records(run_id)
    checkpoints = []
    for fraction in sorted(set(fractions)):
        threshold = int((cap * Decimal(str(fraction))).to_integral_value(rounding=ROUND_CEILING))
        spent, anchor = 0, None
        for record in records:
            if record["kind"] == "ledger_debit":
                spent += record["amount_nano"]
            if spent >= threshold:
                anchor = record
                break
        if anchor is None:
            checkpoints.append(
                unmeasured(
                    "budget fraction was not reached", fraction=fraction, target_usd=threshold / 1e9
                )
            )
            continue
        snapshot = service.checkpoint_fold(run_id, anchor["seq"])
        if snapshot["legacy_import_boundary_missing"]:
            checkpoints.append(
                unmeasured("legacy import lacks an exact semantic boundary", fraction=fraction)
            )
            continue
        reflection_policy = run["config"].get("reflection_policy", "none")
        eligibility = formation_records(snapshot["beliefs"], reflection_policy=reflection_policy)
        eligible = eligibility.pop("items")
        prefix = [row for row in records if row["seq"] <= anchor["seq"]]
        revisions = _revision_rows(
            service, run_id, snapshot, prefix, reflection_policy=reflection_policy
        )
        triple_coverage = (
            sum(bool(row.get("triple")) for row in eligible) / len(eligible) if eligible else None
        )
        metrics = belief_metrics(
            eligible,
            expectations=_expectations(prefix),
            spent_usd=spent / 1e9,
            revision_observations=revisions,
        )
        checkpoints.append(
            {
                "status": "measured",
                "fraction": fraction,
                "spent_usd": spent / 1e9,
                "anchor_seq": anchor["seq"],
                "anchor_hash": anchor["hash"],
                "anchor_ts": anchor["ts"],
                "beliefs": eligible,
                "formation_eligibility": eligibility,
                "metrics": metrics,
                "revision_rate": _churn(snapshot, reflection_policy=reflection_policy),
                "triple_revision_rate": _churn(
                    snapshot, reflection_policy=reflection_policy, triple_only=True
                ),
                "revision_eligibility": {
                    "n": len(revisions),
                    "rule": "active_agent_reconfirmed_at_observation"
                    if reflection_policy == "companion"
                    else "active_at_observation_including_later_retractions",
                },
                "triple_revision_latency": revision_metrics(
                    [row for row in revisions if row["triple"]], triple_coverage=triple_coverage
                ),
                "rank_falsification_curve": _rank_curve(revisions),
                "preservation": _preservation(service, run_id, snapshot, prefix),
                "boundary_policy": snapshot["boundary_policy"],
                "truth_adjudicated": False,
            }
        )
    return {
        "status": "measured"
        if all(row["status"] == "measured" for row in checkpoints)
        else "partial",
        "run_id": run_id,
        "checkpoints": checkpoints,
        "scientific_gate_passed": False,
    }
