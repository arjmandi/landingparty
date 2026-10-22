"""Passive reports over episode records; no network requests or live instruments.

Scores that need a judge, a probe, or a delayed observation stay unmeasured. In
particular, a quote appearing in an archive is not evidence that it supports a
claim, and a valid record is not proof that a cooperative harness used the lane.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from statistics import mean
from typing import Any
from urllib.parse import urldefrag, urlsplit

TUPLE_FIELDS = (
    "world_id",
    "archive_id",
    "tool_set_id",
    "policy_id",
    "price_table_id",
    "breaker_policy_id",
    "search_provider_id",
    "extractor_id",
    "judge_id",
    "observer_id",
    "classifier_id",
    "block_profile_id",
    "harness_id",
    "model_snapshot_id",
    "model_value_document_id",
    "belief_schema_id",
    "validator_policy_id",
    "trigger_policy_id",
    "salience_policy_id",
    "reflection_policy",
    "ledger_view_policy",
    "vault_mode",
    "console_steering",
    "human_time_priced",
    "wake_policy",
    "continuity",
    "browser_identity",
    "captcha_policy",
    "permeability",
    "binding_id",
    "rubric_disclosed",
    "aggregate_policy_id",
    "preamble_hash",
    "canary_policy_id",
    "market_policy_id",
    "perception_price_table_id",
)
PARAMETERS = ("connection", "purpose", "contribution", "experience")
ZERO_HASH = "sha256:" + "0" * 64


def unmeasured(reason: str, **details: Any) -> dict:
    """An explicit absence of a measurement; never a numerical zero."""
    return {"status": "unmeasured", "reason": reason, **details}


def _config(run: Mapping) -> dict:
    return {**run, **(run.get("config") or {})}


def _tuple(run: Mapping) -> dict:
    config = _config(run)
    supplied = config.get("tuple") or config.get("version_tuple") or {}
    fields = dict.fromkeys((*TUPLE_FIELDS, *supplied))
    return {key: supplied.get(key, config.get(key)) for key in fields}


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _money(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("costs must be finite nonnegative numbers") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("costs must be finite nonnegative numbers")
    return parsed


def _reply(record: Mapping) -> dict:
    for field in ("reply", "result"):
        if isinstance(record.get(field), dict):
            return record[field]
    return {}


def verify_records(records: list[dict], *, run_id: str | None = None) -> dict:
    """Verify a supplied chain, without claiming the persisted mirror was checked."""
    previous = ZERO_HASH
    expected_run = run_id or (records[0].get("run_id") if records else None)
    for seq, record in enumerate(records, start=1):
        if record.get("seq") != seq or record.get("prev_hash") != previous:
            return {"valid": False, "count": len(records), "error": f"chain break at seq {seq}"}
        if record.get("run_id") != expected_run:
            return {"valid": False, "count": len(records), "error": f"run mismatch at seq {seq}"}
        unsigned = {key: value for key, value in record.items() if key != "hash"}
        try:
            encoded = json.dumps(
                unsigned,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        except (ValueError, TypeError):
            return {"valid": False, "count": len(records), "error": f"invalid JSON at seq {seq}"}
        digest = "sha256:" + sha256(encoded).hexdigest()
        if record.get("hash") != digest:
            return {"valid": False, "count": len(records), "error": f"hash mismatch at seq {seq}"}
        previous = digest
    return {
        "valid": bool(records),
        "count": len(records),
        "head": previous,
        "scope": "provided_records",
        "persisted_mirror_checked": False,
        **({"error": "empty record"} if not records else {}),
    }


def audit_completeness(run: dict, records: list[dict]) -> dict:
    """Require affirmative transcript and shell-audit evidence for completeness."""
    model_calls = sum(record.get("kind") == "model_call" for record in records)
    gaps = [record for record in records if record.get("kind") == "completeness_gap"]
    audit = dict(run.get("completeness") or {})
    for record in records:
        if record.get("kind") == "completeness_audit":
            audit.update(record.get("audit") or record)
    transcript_calls = audit.get("model_calls_transcript")
    bypasses = audit.get("shell_bypass_candidates")
    shell_audited = audit.get("shell_audited") is True
    if audit.get("verdict") == "partial" and audit.get("reasons"):
        verdict, reason = "partial", "; ".join(str(item) for item in audit["reasons"])
    elif gaps or (isinstance(bypasses, int) and bypasses > 0):
        verdict, reason = "partial", "recorded completeness gaps or shell bypass candidates"
    elif isinstance(transcript_calls, int) and transcript_calls != model_calls:
        verdict, reason = "partial", "transcript and model-lane call counts differ"
    elif (
        isinstance(transcript_calls, int)
        and not isinstance(transcript_calls, bool)
        and transcript_calls > 0
        and transcript_calls == model_calls
        and shell_audited
        and bypasses == 0
    ):
        verdict, reason = (
            "complete",
            "lane count matches transcript and shell audit found no bypass",
        )
    else:
        verdict, reason = "unknown", "transcript reconciliation and shell audit not both supplied"
    return {
        "verdict": verdict,
        "reason": reason,
        "model_calls_lane": model_calls,
        "model_calls_transcript": transcript_calls,
        "shell_bypass_candidates": bypasses,
        "shell_audited": shell_audited,
        "recorded_gaps": len(gaps),
    }


def _recorded_spend(records: list[dict]) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for record in records:
        if record.get("kind") != "ledger_debit":
            continue
        category = str(record.get("category", "unknown"))
        value = (
            _money(record["amount_nano"]) / Decimal(1_000_000_000)
            if "amount_nano" in record
            else _money(record.get("cost_usd", 0))
        )
        values[category] = values.get(category, Decimal(0)) + value
    return values


def _costs(ledger: dict | None, records: list[dict]) -> tuple[dict, dict]:
    recorded = _recorded_spend(records)
    if ledger is None:
        return (
            {key: float(value) for key, value in sorted(recorded.items())},
            unmeasured("authoritative ledger was not supplied"),
        )
    supplied = ledger.get("by_category", ledger.get("spent", ledger))
    if not isinstance(supplied, dict):
        raise TypeError("ledger must map categories to dollars")
    actual = {str(key): _money(value) for key, value in supplied.items()}
    categories = sorted(actual.keys() | recorded.keys())
    mismatch = {
        key: {"ledger_usd": float(actual.get(key, 0)), "recorded_usd": float(recorded.get(key, 0))}
        for key in categories
        if actual.get(key, 0) != recorded.get(key, 0)
    }
    return (
        {key: float(value) for key, value in sorted(actual.items())},
        {
            "valid": not mismatch,
            "mismatches": mismatch,
            "ledger_usd": float(sum(actual.values(), Decimal(0))),
            "recorded_usd": float(sum(recorded.values(), Decimal(0))),
        },
    )


def build_report(
    run: dict,
    records: list[dict],
    *,
    ledger: dict | None = None,
    verification: dict | None = None,
) -> dict:
    """Summarize a run without contacting the agent, a provider, or the internet.

    Pass Store verification as ``{'record': verify_record(...), 'ledger': reconcile(...)}``.
    Ledger amounts and outcomes are counted separately so model_call and action
    cost annotations cannot charge the same dollars twice.
    """
    config = _config(run)
    version = _tuple(run)
    supplied_verification = verification or {}
    by_category, ledger_check = _costs(ledger, records)
    integrity = {
        "record": supplied_verification.get(
            "record", verify_records(records, run_id=run.get("run_id"))
        ),
        "ledger": supplied_verification.get("ledger", ledger_check),
    }
    all_actions = [record for record in records if record.get("kind") == "action"]
    actions = [record for record in all_actions if record.get("world_action") is not False]
    internal_ids = {
        record.get("action_id") for record in all_actions if record.get("world_action") is False
    }
    all_outcomes = [record for record in records if record.get("kind") == "outcome"]
    outcomes = [record for record in all_outcomes if record.get("action_id") not in internal_ids]
    models = [record for record in records if record.get("kind") == "model_call"]
    action_ids = [record.get("action_id") for record in actions if record.get("action_id")]
    outcome_ids = [record.get("action_id") for record in outcomes if record.get("action_id")]
    blocks: Counter = Counter()
    domains: set[str] = set()
    resource_hashes: set[str] = set()
    fetched_bytes = 0
    latencies: list[float] = []
    fetches = 0
    for action in actions:
        params = action.get("params") or {}
        urls = params.get("urls") or [params.get("url")]
        for url in urls:
            if isinstance(url, str):
                host = urlsplit(url).hostname
                if host:
                    domains.add(host.lower())
    batch_ids = {
        action.get("action_id") for action in actions if action.get("tool") == "fetch_many"
    }
    observations = all_outcomes + [
        {"reply": record["event"]["reply"], "action_id": record["event"].get("action_id")}
        for record in records
        if record.get("kind") == "event_pushed"
        and isinstance(record.get("event"), dict)
        and record["event"].get("type") == "job_done"
        and record["event"].get("action_id") in batch_ids
        and isinstance(record["event"].get("reply"), dict)
    ]
    seen_handles: set[str] = set()
    for outcome in observations:
        reply = _reply(outcome)
        block = outcome.get("block") or (reply if reply.get("kind") == "blocked" else {})
        if block:
            blocks[str(block.get("type", "unknown"))] += 1
        resource = outcome.get("resource") or reply.get("resource") or reply
        if resource.get("handle") and resource["handle"] not in seen_handles:
            seen_handles.add(resource["handle"])
            fetches += 1
            fetched_bytes += int(resource.get("bytes") or 0)
            if resource.get("hash"):
                resource_hashes.add(resource["hash"])
        started, finished = _date(outcome.get("started_at")), _date(outcome.get("finished_at"))
        if started and finished:
            latencies.append(max(0.0, (finished - started).total_seconds()))
    concurrency = [
        record["in_flight_before"]
        for record in actions
        if isinstance(record.get("in_flight_before"), int)
    ]
    token_classes: Counter = Counter()
    for record in models:
        usage = record.get("usage") or {}
        for token_class in ("input", "output", "cache_read", "cache_write"):
            for alias in {
                "input": ("input", "input_tokens", "prompt_tokens"),
                "output": ("output", "output_tokens", "completion_tokens"),
                "cache_read": ("cache_read", "cache_read_input_tokens"),
                "cache_write": ("cache_write", "cache_creation_input_tokens"),
            }[token_class]:
                value = usage.get(alias)
                if isinstance(value, int) and not isinstance(value, bool):
                    token_classes[token_class] += value
                    break
    first = next((_date(record.get("ts")) for record in records if _date(record.get("ts"))), None)
    last = next(
        (_date(record.get("ts")) for record in reversed(records) if _date(record.get("ts"))), None
    )
    wall_s = max(0.0, (last - first).total_seconds()) if first and last else None
    completeness = audit_completeness(run, records)
    end = dict(run.get("end") or {})
    if not end:
        for record in reversed(records):
            if record.get("kind") in {"beam_up", "seal"} and isinstance(record.get("end"), dict):
                end = dict(record["end"])
                break
    total = float(sum((_money(value) for value in by_category.values()), Decimal(0)))
    from .metrics import ask_metrics
    from .trajectory import trajectory_metrics

    ask_summary = ask_metrics(records, spent_usd=total)
    tainted = bool(run.get("tainted")) or any(
        check.get("valid") is False for check in integrity.values()
    )
    unmeasured_entries = {
        "judge_ratings": "a calibrated, blinded cross-family judge has not been run",
        "self_report": "the two-stage post-run interview has not been run",
        "probe_gains": "the five-cell forked-context probe battery has not been run",
        "citation_support": "claim entailment and archived citation verification were not supplied",
        "replay_fidelity": "paired live/replay retrieval lists have not been compared",
        "pass_k": "a complete preregistered multi-seed cohort is required",
        "block_profile": "a fixed 300-URL canary crawl is required",
        "life_rubric": "no life evidence, calibrated labels, or afterlife readings were supplied",
    }
    rubric = {
        parameter: {
            column: unmeasured(unmeasured_entries["life_rubric"]) for column in ("G", "V", "J", "S")
        }
        for parameter in PARAMETERS
    }
    expectation_outcomes = Counter()
    for record in records:
        if record.get("kind") == "expectation_resolved":
            outcome = record.get("outcome")
            expectation_outcomes[str(outcome) if isinstance(outcome, str) else "unspecified"] += 1
    missing_tuple = [key for key, value in version.items() if value is None]
    report = {
        "schema_version": "landing-party-report/0.1",
        "run_id": run.get("run_id"),
        "state": run.get("state", "unknown"),
        "sealed": any(record.get("kind") == "seal" for record in records),
        "mode": config.get("mode", "live"),
        "tuple": version,
        "identifiers": {
            key: version[key] for key in ("policy_id", "price_table_id", "preamble_hash")
        },
        "configuration": {
            key: config.get(key)
            for key in (
                "iota",
                "learning_mechanism",
                "budget_visible",
                "reply_policy",
                "operator_messages_allowed",
                "human_time_priced",
                "console_steering",
                "capture_model_io",
                "isolation_profile",
                "packs",
                "reflection_policy",
                "vault_mode",
                "max_in_flight",
                "harness_id",
                "seed",
            )
        },
        "budget": config.get("budget"),
        "goal": config.get("goal"),
        "family": config.get("family"),
        "costs": {
            "usd_total": total,
            "by_category": by_category,
            "token_classes": dict(token_classes),
        },
        "axes": {"usd": total, "world_actions": len(actions)},
        "activity": {
            "world_actions": len(actions),
            "outcomes": len(outcomes),
            "network_submissions": sum(
                record.get("tool") == "raw_request" for record in all_actions
            ),
            "recorded_outcomes_including_subrequests": len(all_outcomes),
            "model_calls": len(models),
            "by_tool": dict(
                sorted(Counter(str(action.get("tool", "unknown")) for action in actions).items())
            ),
            "blocks": dict(sorted(blocks.items())),
            "attempted_domains": sorted(domains),
            "fetched_resources": fetches,
            "fetched_bytes": fetched_bytes,
            "distinct_content_hashes": len(resource_hashes),
            "actions_without_outcome": len(set(action_ids) - set(outcome_ids)),
            "duplicate_outcomes": len(outcome_ids) - len(set(outcome_ids)),
            "in_flight_before": {
                "max": max(concurrency) if concurrency else None,
                "mean": mean(concurrency) if concurrency else None,
            },
            "asks_by_kind": dict(
                Counter(
                    str(
                        record.get(
                            "ask_kind",
                            record.get("ticket_kind", record.get("kind_requested", "unknown")),
                        )
                    )
                    for record in records
                    if record.get("kind") == "ask_opened"
                )
            ),
            "human_seconds": ask_summary["human_seconds"],
            "asks": ask_summary,
        },
        "time": {
            "recorded_wall_s": wall_s,
            "mean_action_latency_s": mean(latencies) if latencies else None,
        },
        "trajectory": trajectory_metrics(run, records),
        "integrity": {**integrity, "tainted": tainted},
        "completeness": completeness,
        "end": end,
        "attrition": {
            "run_id": run.get("run_id"),
            "started": True,
            "state": run.get("state", "unknown"),
            "terminated": end.get("terminated"),
            "truncated": end.get("truncated"),
            "reason": end.get("reason", "run has not recorded an end"),
            "open_tickets_at_end": end.get("open_tickets_at_end"),
            "open_expectations_at_end": end.get("open_expectations_at_end"),
            "completeness": completeness["verdict"],
            "tainted": tainted,
        },
        "columns": {
            "G": {
                "spend_usd": total,
                "blocks": dict(blocks),
                "resolved_expectations": dict(expectation_outcomes),
            },
            "V": {
                "record_integrity": integrity["record"],
                "ledger_integrity": integrity["ledger"],
                "archived_citation_support": unmeasured(unmeasured_entries["citation_support"]),
            },
            "J": unmeasured(unmeasured_entries["judge_ratings"]),
            "S": unmeasured(unmeasured_entries["self_report"]),
        },
        "rubric": rubric,
        "aggregates": {
            "min_G": unmeasured("all four grounded parameters must be measured"),
            "min_V": unmeasured("all four verifiable parameters must be measured"),
        },
        "unmeasured": {key: unmeasured(reason) for key, reason in unmeasured_entries.items()},
        "comparability": {
            "status": "not_assessed",
            "missing_tuple_fields": missing_tuple,
            "reason": "a second run, goal slice, block profile, and archive or interleaving evidence are required",
        },
    }
    return report


def render_report(report: dict) -> str:
    """Render the mission report as Markdown, retaining the four score columns."""
    identifiers = report.get("identifiers") or {}
    costs, activity = report.get("costs", {}), report.get("activity", {})
    lines = [
        f"# Mission report: {report.get('run_id', 'unknown')}",
        "",
        (
            f"State: {report.get('state')}. Mode: {report.get('mode')}. "
            f"Sealed: {str(report.get('sealed', False)).lower()}."
        ),
        "",
        (
            f"Policy `{identifiers.get('policy_id')}`; price table `{identifiers.get('price_table_id')}`; "
            f"preamble `{identifiers.get('preamble_hash')}`."
        ),
        "",
        (
            f"Recorded spend: ${costs.get('usd_total', 0):.9f}; "
            f"world actions: {activity.get('world_actions', 0)}; model calls: {activity.get('model_calls', 0)}."
        ),
        "",
        "| Category | USD |",
        "|---|---:|",
    ]
    lines.extend(f"| {key} | {value:.9f} |" for key, value in costs.get("by_category", {}).items())
    if not costs.get("by_category"):
        lines.append("| No recorded debits | 0.000000000 |")
    lines.extend(
        [
            "",
            f"Completeness: **{report['completeness']['verdict']}** — {report['completeness']['reason']}.",
            f"Tainted: {str(report['integrity']['tainted']).lower()}.",
            "",
            "| Column | Observation |",
            "|---|---|",
            "| G — grounded | Recorded spend, typed blocks, world-resolved expectations |",
            "| V — verifiable | Record and ledger checks; citation support remains unmeasured |",
            f"| J — prejudged | Unmeasured: {report['columns']['J']['reason']} |",
            f"| S — self-report | Unmeasured: {report['columns']['S']['reason']} |",
            "",
            "## Life rubric",
            "",
            "| Parameter | G | V | J | S |",
            "|---|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {name} | unmeasured | unmeasured | unmeasured | unmeasured |" for name in PARAMETERS
    )
    lines.extend(
        [
            "",
            "A run-level activity summary is not a scored life. Unmeasured values do not enter an aggregate.",
            "",
            "## Casualty report",
            "",
            "```json",
            json.dumps(report.get("attrition"), ensure_ascii=False, sort_keys=True, indent=2),
            "```",
            "",
            "## Unmeasured",
            "",
        ]
    )
    lines.extend(
        f"- {name}: {value['reason']}." for name, value in report.get("unmeasured", {}).items()
    )
    analysis = report.get("analysis") or {}
    if analysis.get("stages"):
        lines.extend(
            [
                "",
                "## Post-run measurements",
                "",
                "| Stage | Execution | Private artifact |",
                "|---|---|---|",
            ]
        )
        for name, value in analysis["stages"].items():
            lines.append(
                f"| {name} | {value.get('status', 'unknown')} | {value.get('artifact', value.get('reason', value.get('error', '')))} |"
            )
        lines.extend(
            [
                "",
                f"Separate measurement spend: ${analysis.get('measurement_costs', {}).get('usd', 0):.6f}. Source-run spend excludes these child runs.",
                "Execution status does not establish a scientific gate; coverage and calibration remain in each artifact.",
            ]
        )
    lines.extend(
        [
            "",
            "## Version tuple",
            "",
            "```json",
            json.dumps(report.get("tuple"), ensure_ascii=False, sort_keys=True, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def verify_citations(claims: list[dict], archive: Mapping[str, str]) -> dict:
    """Check source capture and quote presence, never semantic entailment.

    ``archive`` maps captured URLs to already extracted text. This routine never
    re-fetches a live source or follows a citation outside the supplied archive.
    Each claim supplies ``claim``, ``url``, and a nonempty ``quote``.
    """
    canonical = {urldefrag(url)[0]: text for url, text in archive.items()}
    rows = []
    for index, claim in enumerate(claims):
        url, quote, text = claim.get("url"), claim.get("quote"), claim.get("claim")
        if not all(isinstance(value, str) and value.strip() for value in (url, quote, text)):
            status = "invalid_citation"
        elif urldefrag(url)[0] not in canonical:
            status = "source_not_archived"
        else:
            normalized_quote = " ".join(quote.split())
            normalized_source = " ".join(canonical[urldefrag(url)[0]].split())
            status = "quote_present" if normalized_quote in normalized_source else "quote_missing"
        rows.append({"index": index, "url": url, "status": status})
    counts = Counter(row["status"] for row in rows)
    return {
        "claims": len(rows),
        "counts": dict(counts),
        "items": rows,
        "quote_match_rate": counts["quote_present"] / len(rows) if rows else None,
        "claim_support": unmeasured("quote presence does not establish claim entailment"),
        "citation_recall": unmeasured(
            "a frozen complete set of required citations was not supplied"
        ),
    }


def compare_runs(left: dict, right: dict, *, interleaved: bool = False) -> dict:
    """Apply the design's strict tuple, goal, archive/week and block-profile gates.

    ``interleaved=True`` is affirmative researcher evidence from the series log;
    merely running in the same week never establishes an interleaved experiment.
    """
    a, b = _tuple(left), _tuple(right)
    ca, cb = _config(left), _config(right)
    fields = set(TUPLE_FIELDS) | set(a) | set(b)
    missing = {
        side: [
            field
            for field in sorted(fields)
            if field != "archive_id" and version.get(field) is None
        ]
        for side, version in (("left", a), ("right", b))
    }
    mismatches = [
        field for field in sorted(fields) if field != "archive_id" and a.get(field) != b.get(field)
    ]
    reasons = []
    if any(missing.values()):
        reasons.append("version tuple is incomplete")
    if mismatches:
        reasons.append("version tuple differs")
    goal_slice_a = ca.get("goal_slice", {"goal": ca.get("goal"), "family": ca.get("family")})
    goal_slice_b = cb.get("goal_slice", {"goal": cb.get("goal"), "family": cb.get("family")})
    if goal_slice_a != goal_slice_b:
        reasons.append("goal slices differ")
    elif goal_slice_a == {"goal": None, "family": None}:
        reasons.append("goal slice is unspecified")
    same_archive = a["archive_id"] is not None and a["archive_id"] == b["archive_id"]
    created_a, created_b = _date(left.get("created_at")), _date(right.get("created_at"))
    same_week = bool(
        created_a and created_b and created_a.isocalendar()[:2] == created_b.isocalendar()[:2]
    )
    live_interleaved = ca.get("mode") == cb.get("mode") == "live" and same_week and interleaved
    if not same_archive and not live_interleaved:
        reasons.append(
            "neither a common archive nor same-week interleaved live execution is established"
        )
    profiles = [ca.get("block_profile"), cb.get("block_profile")]
    profile_deltas = {}
    if all(isinstance(profile, dict) and profile for profile in profiles):
        keys = set(profiles[0]) | set(profiles[1])
        if set(profiles[0]) != set(profiles[1]):
            reasons.append("block-profile domain classes differ")
        else:
            for key in sorted(keys):
                try:
                    va, vb = (Decimal(str(profile[key])) for profile in profiles)
                    if not all(value.is_finite() and 0 <= value <= 1 for value in (va, vb)):
                        raise ValueError("invalid block rate")
                    profile_deltas[key] = float(abs(va - vb))
                    if abs(va - vb) > Decimal("0.05"):
                        reasons.append(
                            f"block-profile drift exceeds five percentage points for {key}"
                        )
                except (InvalidOperation, ValueError):
                    reasons.append(f"block profile has an invalid rate for {key}")
    else:
        reasons.append("block-profile rates were not supplied")
    return {
        "comparable": not reasons,
        "reasons": reasons,
        "tuple_mismatches": mismatches,
        "missing_tuple_fields": missing,
        "block_profile_deltas": profile_deltas,
        "same_archive": same_archive,
        "same_iso_week": same_week,
        "interleaved_execution_verified": interleaved,
    }
