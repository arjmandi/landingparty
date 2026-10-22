"""Engineering readiness from actual registered pilot runs and private receipts.

This is a read-only evaluator until it writes its own immutable study artifact.
It never runs a model, sends a message, changes a threshold or unlocks a stage.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from worldd.breakers import semantic_reply
from worldd.lane import StreamUsage, normalize_usage, price_table_id, usage_cost
from worldd.util import digest

from .instruments import load_artifact
from .publication import load_measurements
from .report import build_report, unmeasured


def _hash(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _measurement_valid(store, source_run_id, completion, *, lane):
    try:
        child_id = completion["measurement_run_id"]
        child = store.get_run(child_id)
        return (
            child["state"] == "sealed"
            and not child.get("tainted")
            and child["config"].get("measurement_of") == source_run_id
            and child["config"].get("metering_lane") == lane
            and store.verify_record(child_id)["valid"]
            and store.reconcile(child_id)["valid"]
            and store.reserved(child_id) == 0
            and completion.get("spent") == store.spend(child_id)
            and any(
                event["kind"] == "model_call" and event.get("usage")
                for event in store.records(child_id)
            )
        )
    except (KeyError, ValueError, TypeError):
        return False


def _provider_usage(capture, provider):
    raw = capture.get("response")
    if not isinstance(raw, str):
        raise TypeError("provider capture has no response bytes")
    try:
        response = json.loads(raw)
    except ValueError:
        stream = StreamUsage(provider)
        stream.feed(raw.encode(), final=True)
        if not stream.complete or stream.usage is None:
            raise ValueError("provider stream lacks complete terminal usage") from None
        return normalize_usage(provider, stream.usage)
    return normalize_usage(provider, response.get("usage"))


def metering_audit(store, run_id, prices):
    """Reprice provider's captured token report independently of lane totals."""
    run, records = store.get_run(run_id), store.records(run_id)
    actual_price_id = price_table_id(
        prices, perception_prices=run["config"].get("perception_pricing")
    )
    if actual_price_id != run["config"].get("tuple", {}).get("price_table_id"):
        return {"passed": False, "reason": "actual price table does not match the episode tuple"}
    models = prices.get("models", prices)
    captures = {row["call_id"]: row for row in records if row["kind"] == "model_capture"}
    debits = {
        row.get("reservation_id"): row
        for row in records
        if row["kind"] == "ledger_debit" and row.get("reservation_id")
    }
    rows = []
    for call in (row for row in records if row["kind"] == "model_call"):
        row = {"call_id": call["call_id"], "passed": False}
        try:
            anchor = captures[call["call_id"]]
            root = store.run_dir(run_id)
            path = root / anchor["capture_ref"]
            if path.resolve() != path or not path.is_relative_to(root / "model-io"):
                raise ValueError("capture is outside the actual episode model-io directory")
            body = path.read_bytes()
            if _hash(body) != anchor.get("capture_hash"):
                raise ValueError("captured provider report no longer matches its sealed anchor")
            capture = json.loads(body)
            usage = _provider_usage(capture, call["provider"])
            actual = usage_cost(usage, models[call["model"]])
            nano = int((actual * Decimal(10**9)).to_integral_value(rounding=ROUND_CEILING))
            debit = debits[call["reservation_id"]]
            if call.get("usage") != usage:
                raise ValueError("lane usage differs from actual provider report")
            if call.get("price_table_id") != actual_price_id:
                raise ValueError("call price table differs from the frozen price table")
            if debit["amount_nano"] != nano or abs(Decimal(str(call["usd"])) - actual) > Decimal(
                "0.000000001"
            ):
                raise ValueError("metered dollars differ from the repriced provider report")
            row.update(
                passed=True,
                provider_usage=usage,
                provider_cost_usd=float(actual),
                ledger_nano=nano,
                capture_hash=anchor["capture_hash"],
            )
        except (KeyError, ValueError, TypeError, OSError) as error:
            row["reason"] = str(error)
        rows.append(row)
    unmatched = [
        row.get("reservation_id")
        for row in records
        if row["kind"] == "ledger_debit"
        and row.get("category") == "model"
        and row.get("reservation_id")
        not in {call.get("reservation_id") for call in records if call["kind"] == "model_call"}
    ]
    return {
        "passed": all(row["passed"] for row in rows) and not unmatched,
        "unmatched_model_debits": unmatched,
        "n_calls": len(rows),
        "calls": rows,
        "zero_call_note": "zero provider reports and zero calls" if not rows else None,
    }


def calibrate_breakers(episodes):
    """Derive counterfactual thresholds from held-out log-only outcome rates.

    The threshold rule is explicit and reproducible: nearest-rank 99th percentile
    for yellow, maximum observed run plus one for red. It changes no live policy.
    """
    lengths, no_content, minute_spend, rows = [], [], [], []
    for run, records in episodes:
        if run["config"].get("breaker_policy", {}).get("mode") != "log_only":
            rows.append(
                {
                    "run_id": run["run_id"],
                    "status": "unmeasured",
                    "reason": "episode did not use the frozen log-only breaker policy",
                }
            )
            continue
        actions = {
            r["action_id"]: r
            for r in records
            if r["kind"] == "action" and r.get("world_action", True)
        }
        prior, streak, dry, seen, count = None, 0, 0, set(), 0
        debits = []
        for event in records:
            if event["kind"] == "ledger_debit" and event.get("ts"):
                stamp = datetime.fromisoformat(event["ts"]).timestamp()
                debits = [(ts, value) for ts, value in debits if ts >= stamp - 60]
                debits.append((stamp, event["amount_nano"]))
                minute_spend.append(sum(value for _, value in debits))
            if event["kind"] != "outcome" or event.get("action_id") not in actions:
                continue
            action = actions[event["action_id"]]
            signature = digest(
                [action.get("tool"), action.get("params_hash"), semantic_reply(event.get("reply"))]
            )
            streak = streak + 1 if signature == prior else 1
            prior, count = signature, count + 1
            lengths.append(streak)
            resource = event.get("resource") or event.get("reply") or {}
            content_hash = resource.get("hash")
            novel = bool(content_hash and content_hash not in seen)
            dry = 0 if novel else dry + 1
            no_content.append(dry)
            if content_hash:
                seen.add(content_hash)
        rows.append(
            {
                "run_id": run["run_id"],
                "status": "measured",
                "n_outcomes": count,
                "logged_would_intervene": sum(
                    event["kind"] == "breaker_would_intervene" for event in records
                ),
            }
        )
    if not lengths or any(row["status"] != "measured" for row in rows):
        return {
            "status": "unmeasured",
            "reason": "all pilot episodes must provide actual log-only observations",
            "episodes": rows,
        }
    ordered = sorted(lengths)
    yellow = min(19, max(2, ordered[math.ceil(0.99 * len(ordered)) - 1]))
    red = min(20, max(yellow + 1, max(lengths) + 1))
    return {
        "status": "measured",
        "rule_id": "log-only-streak-nearest-rank-p99-max-plus-one@1",
        "n_outcomes": len(lengths),
        "thresholds_proposed": {
            "yellow_identical": yellow,
            "red_identical": red,
            "no_new_content_actions": max(no_content) + 1,
            "dollars_per_minute": (max(minute_spend, default=0) + 1) / 1e9,
        },
        "twenty_action_window_saturated": max(lengths) >= 20,
        "red_exceedance_fraction": sum(value >= red for value in lengths) / len(lengths),
        "empirical_streak_counts": dict(sorted(Counter(lengths).items())),
        "episodes": rows,
        "applied": False,
    }


class PilotReadiness:
    def __init__(self, world):
        self.world, self.store = world, world.store

    def evaluate(
        self, study_id, *, price_table_artifact=None, probe_bank_artifact=None, persist=True
    ):
        studies = self.world.services.get("studies")
        if studies is None:
            raise ValueError("actual registered study service is required")
        study = studies.get(study_id)
        if study["kind"] != "shakedown":
            raise ValueError("pilot readiness applies only to the registered shakedown")
        from worldd.study_inputs import verify_protocol

        verify_protocol(self.store, study)
        run_ids = study["run_ids"]
        if not run_ids:
            raise ValueError("no pilot episode has started")
        descriptors, prices, bank = {}, None, None
        for name, descriptor in (
            ("price_table", price_table_artifact),
            ("probe_bank", probe_bank_artifact),
        ):
            if descriptor:
                body, provenance = load_artifact(
                    self.store, run_ids[0], descriptor, allowed_run_ids=run_ids
                )
                value = json.loads(body)
                descriptors[name] = provenance
                if name == "price_table":
                    prices = value
                else:
                    bank = value.get("items") if isinstance(value, dict) else value
        bank_valid = (
            isinstance(bank, list)
            and len(bank) >= 60
            and len({item.get("item_id") for item in bank if isinstance(item, dict)}) == len(bank)
            and all(
                isinstance(item, dict)
                and isinstance(item.get("item_id"), str)
                and item["item_id"]
                and item.get("question")
                and item.get("answer") is not None
                for item in bank
            )
        )
        bank_registered = (
            bank_valid
            and study["prereg"].get("probe_bank_hash") == descriptors["probe_bank"]["hash"]
        )
        rows, reports, episodes = [], [], []
        registry = self.world.services.get("registry")
        registered = bool(registry)
        plan = {cell["cell_id"]: cell for cell in study["cells"]}
        execution = {row["run_id"]: row for row in (study.get("result") or {}).get("attrition", [])}
        actual_schedule = defaultdict(list)
        for run_id in run_ids:
            run, events = self.store.get_run(run_id), self.store.records(run_id)
            if not events or events[-1]["kind"] != "seal":
                raise ValueError("readiness cannot read an unsealed pilot episode")
            measured = load_measurements(self.store, run_id)
            report = build_report(
                run,
                events,
                ledger=self.store.spend(run_id),
                verification={
                    "record": self.store.verify_record(run_id),
                    "ledger": self.store.reconcile(run_id),
                },
                measurements=measured,
            )
            if measured.get("completeness", {}).get("verdict") in {
                "complete",
                "partial",
                "unknown",
            }:
                report["completeness"] = measured["completeness"]
            reports.append(report)
            episodes.append((run, events))
            entry = execution.get(run_id, {})
            cell = plan.get(entry.get("cell"), {})
            if cell:
                actual_schedule[cell["baseline"]].append(entry.get("seed"))
            actual_profile = self.world.services.get("canaries")
            profile = (
                actual_profile.profile_for_run(run_id)
                if actual_profile
                else unmeasured("actual canary service absent")
            )
            replay = self.world.services.get("replay")
            fidelity = (
                replay.fidelity(run_id) if replay else unmeasured("actual replay index absent")
            )
            metering = (
                metering_audit(self.store, run_id, prices)
                if prices is not None
                else {"passed": False, "reason": "actual price-table artifact not supplied"}
            )
            if cell.get("baseline") in {"B0", "B1", "B2"} and metering.get("n_calls") == 0:
                metering.update(
                    passed=False,
                    reason="a model baseline cannot pass without an actual provider call",
                )
            pairwise = measured.get("pairwise", {})
            judgments = pairwise.get("judgments", [])
            pairwise_measured = (
                pairwise.get("position_swap") is True
                and pairwise.get("length_control", {}).get("chars", 0) > 0
                and len(judgments) == 2
                and len({row.get("measurement", {}).get("measurement_run_id") for row in judgments})
                == 2
                and all(
                    row.get("judge_family")
                    and row.get("agent_family")
                    and row["judge_family"].casefold() != row["agent_family"].casefold()
                    and _measurement_valid(
                        self.store, run_id, row.get("measurement", {}), lane="judge"
                    )
                    for row in judgments
                )
            )
            probes = measured.get("probes", {})
            probe_contexts = probes.get("contexts", {})
            probe_measured = (
                bank_valid
                and probes.get("bank_content_hash") == digest(bank)
                and len({row.get("item_id") for row in probes.get("results", [])}) >= 60
                and all(
                    _measurement_valid(
                        self.store, run_id, probe_contexts.get(cell_name, {}), lane="probe"
                    )
                    for cell_name in ("pre_off", "pre_on", "post_off", "post_on", "post_mem")
                )
            )
            if registry:
                try:
                    series = registry.get_series(run["config"].get("series_id"))
                    registered = (
                        registered
                        and series["prereg_hash"] == study["prereg_hash"]
                        and series["created_at"] <= run["created_at"]
                        and any(row["run_id"] == run_id for row in series["runs"])
                    )
                except (KeyError, ValueError, TypeError):
                    registered = False
            rows.append(
                {
                    "run_id": run_id,
                    "cell": entry.get("cell"),
                    "baseline": cell.get("baseline"),
                    "seed": run["config"].get("seed"),
                    "metering": metering,
                    "citation_verification": measured.get("citation-quotes", {}).get("claims", 0)
                    > 0,
                    "pairwise_measured": pairwise_measured,
                    "probe_execution": bool(probe_measured),
                    "completeness": report["completeness"],
                    "replay_fidelity": fidelity,
                    "block_profile": profile,
                    "integrity": report["integrity"],
                    "held_usd": self.store.reserved(run_id),
                    "attrition": report["attrition"],
                    "source_record_head": events[-1]["hash"],
                    "open_tickets_at_end": (run.get("end") or {}).get("open_tickets_at_end"),
                    "dollars": report["costs"]["usd_total"],
                    "world_actions": report["activity"]["world_actions"],
                }
            )
        expected = {
            item["baseline"]: sorted(item["seeds"]) for item in study["prereg"].get("schedule", [])
        }
        schedule_valid = (
            len(run_ids) == len(set(run_ids)) == 12
            and {name: sorted(seeds) for name, seeds in actual_schedule.items()} == expected
            and {name: len(seeds) for name, seeds in actual_schedule.items()}
            == {"B2": 5, "B1": 5, "B0": 1, "B3": 1}
        )
        breakers = calibrate_breakers(episodes)
        profile_ok = all(row["block_profile"].get("provenance_verified") is True for row in rows)
        valid_chains = all(
            row["integrity"]["record"]["valid"]
            and row["integrity"]["ledger"]["valid"]
            and not row["integrity"]["tainted"]
            and row["held_usd"] == 0
            for row in rows
        )
        open_ticket_exercise = any(
            isinstance(row["open_tickets_at_end"], int) and row["open_tickets_at_end"] > 0
            for row in rows
        )
        metrics = {
            "metering_matches_provider": all(row["metering"]["passed"] for row in rows),
            "completeness_verdicts": all(
                measured.get("completeness", {}).get("verdict")
                in {"complete", "partial", "unknown"}
                for measured in (load_measurements(self.store, run_id) for run_id in run_ids)
            ),
            "replay_fidelity": any(
                row["replay_fidelity"].get("status") == "measured"
                and row["replay_fidelity"].get("n", 0) > 0
                for row in rows
            ),
            "block_profile": profile_ok,
            "hash_chain_and_seal": valid_chains and open_ticket_exercise,
            "attrition": len(rows) == len(run_ids)
            and all(row["attrition"].get("started") is True for row in rows),
            "breaker_calibrated": breakers["status"] == "measured",
            "probe_bank_items": len(bank) if bank_valid else 0,
            "probe_bank_registered": bool(bank_registered),
            "registered_schedule": schedule_valid,
            "preregistered": registered,
            "goal_protocol_frozen": bool(study.get("goal_protocol")),
            "citation_verification": all(
                row["citation_verification"] for row in rows if row["baseline"] in {"B1", "B2"}
            ),
            "cross_family_pairwise": any(row["pairwise_measured"] for row in rows),
            "probe_execution": all(
                row["probe_execution"] for row in rows if row["baseline"] in {"B1", "B2"}
            ),
        }
        failures = [
            name
            for name, value in metrics.items()
            if (value < 60 if name == "probe_bank_items" else value is not True)
        ]
        if study["state"] != "completed":
            failures.append("registered pilot execution is not complete")
        scientific_sources = all(
            run["config"].get("research_mode") == "research" and not run.get("tainted")
            for run, _events in episodes
        )
        result = {
            "study": "shakedown",
            "study_id": study_id,
            "completed": not failures,
            "positive": not failures,
            "scientific": not failures and scientific_sources,
            "run_ids": run_ids,
            "prereg_hash": study["prereg_hash"],
            "metrics": metrics,
            "gate_failures": failures,
            "per_run": rows,
            "dollars_to_actions_by_harness": [
                {
                    "cell": row["cell"],
                    "baseline": row["baseline"],
                    "run_id": row["run_id"],
                    "usd": row["dollars"],
                    "world_actions": row["world_actions"],
                    "usd_per_action": row["dollars"] / row["world_actions"]
                    if row["world_actions"]
                    else None,
                }
                for row in rows
            ],
            "attrition": [row["attrition"] for row in rows],
            "breaker_calibration": breakers,
            "source_artifacts": descriptors,
            "scientific_gate_passed": False,
            "note": "The immutable evidence report requires explicit RegistryService gate registration; no thresholds or stage permissions were changed.",
        }
        if not persist:
            return result
        body = (
            json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode()
        folder = self.store.data_dir / "studies" / study_id / "readiness"
        if folder.resolve() != folder:
            raise ValueError("study readiness output cannot follow symbolic links")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / (_hash(body).split(":", 1)[1] + ".json")
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        elif path.is_symlink() or path.read_bytes() != body:
            raise ValueError("immutable readiness artifact mismatch")
        return {
            **result,
            "artifact_ref": str(path.relative_to(self.store.data_dir)),
            "artifact_hash": _hash(body),
        }

    def verify(self, report):
        """Verify exact artifact and recompute evidence without writes or paid calls."""
        try:
            path = self.store.data_dir / report["artifact_ref"]
            folder = self.store.data_dir / "studies" / report["study_id"] / "readiness"
            if path.resolve() != path or not path.is_relative_to(folder):
                return ["readiness artifact is outside its private study directory"]
            body = path.read_bytes()
            if _hash(body) != report["artifact_hash"]:
                return ["readiness artifact hash does not match"]
            payload = {
                key: value
                for key, value in report.items()
                if key not in {"artifact_ref", "artifact_hash"}
            }
            if json.loads(body) != payload:
                return ["gate report differs from the immutable readiness artifact"]
            sources = payload["source_artifacts"]
            actual = self.evaluate(
                payload["study_id"],
                price_table_artifact=sources.get("price_table"),
                probe_bank_artifact=sources.get("probe_bank"),
                persist=False,
            )
            return (
                []
                if actual == payload
                else ["current actual pilot evidence differs from the readiness artifact"]
            )
        except (KeyError, ValueError, TypeError, OSError):
            return ["readiness evidence could not be verified"]
