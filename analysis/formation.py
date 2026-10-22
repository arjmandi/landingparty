"""Auditable post-run prose labels and deterministic cross-seed store metrics.

Judges label coherence and revision behaviour, never whether a belief is true.
Inputs must come from verified instruments; the pipeline owns artifact loading.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal
from itertools import combinations
from statistics import mean, median

from worldd.beliefs import formation_records

from .judges import JudgeSpec, ResearchCallError, ToollessJudge
from .metrics import calibration, revision_metrics
from .report import unmeasured

INSTRUMENT_ID = "formation-prose@1"
CANONICALIZER_ID = "nfkc-casefold-words-operators@1"
LABELS = {
    "coherence": ("consistent", "contradiction", "unclear"),
    "revision": ("evidence_driven", "self_driven", "no_revision", "unclear"),
    "dropped_goal": ("achieved", "believed_impossible", "no_longer_desired", "unclear"),
}
INSTRUCTIONS = {
    "coherence": (
        "Compare the two claims in their stated valid-time context. Label consistent, "
        "contradiction, or unclear. Different times can describe a world update. "
        "Do not decide whether either claim is true."
    ),
    "revision": (
        "Label the documented change evidence_driven, self_driven, no_revision, or unclear. "
        "Missing causal evidence is unclear, not proof of self-driven change. "
        "Also return revision_needed as true, false, or null, relative only to the supplied "
        "evidence and the agent's prior commitment, never a claim of objective truth."
    ),
    "dropped_goal": (
        "Classify the documented dropped goal/strategy as achieved, believed_impossible, "
        "no_longer_desired, or unclear. Believed impossible concerns the agent's belief, "
        "not whether the goal was actually impossible. Do not infer achievement from stopping."
    ),
}


def _hash(value):
    body = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def canonical_claim(claim: str) -> str:
    """Lexical canonicalisation only; preserves negation, numbers and word order."""
    if not isinstance(claim, str):
        raise TypeError("a claim must be text")
    return " ".join(
        re.findall(
            r"\d+(?:\.\d+)?|\w+|[<>=+−±*/%$£€¥-]", unicodedata.normalize("NFKC", claim).casefold()
        )
    )


def prose_sample(beliefs: list[dict], *, seed=0) -> dict:
    """Order-invariant hash sampling of at most 200 current prose claim records."""
    if not isinstance(seed, (str, int)) or isinstance(seed, bool):
        raise TypeError("the sampling seed must be an integer or text")
    records = [row for row in beliefs if isinstance(row.get("claim"), str) and row["claim"].strip()]
    ids = [row.get("id") for row in records]
    if not all(isinstance(key, str) and key for key in ids) or len(ids) != len(set(ids)):
        raise ValueError("sampling requires distinct current belief ids")
    records.sort(key=lambda row: (_hash([seed, row["id"], row["claim"]]), row["id"]))
    sampled = records[:200]
    return {
        "items": sampled,
        "ids": [row["id"] for row in sampled],
        "seed": seed,
        "population_n": len(records),
        "n": len(sampled),
        "target_n": 200,
        "complete_200": len(sampled) == 200,
        "census": len(records) <= 200,
        "sampling_frame": "prose_claims_including_structured_records",
        "sample_hash": _hash([{"id": row["id"], "claim": row["claim"]} for row in sampled]),
    }


def _ranks(values):
    positions = defaultdict(list)
    for index, value in enumerate(sorted(values), start=1):
        positions[value].append(index)
    return [mean(positions[value]) for value in values]


def _spearman(left, right):
    if len(left) < 2:
        return unmeasured("at least two common canonical claims are required", n=len(left))
    a, b = _ranks(left), _ranks(right)
    centre_a, centre_b = mean(a), mean(b)
    numerator = sum((x - centre_a) * (y - centre_b) for x, y in zip(a, b, strict=True))
    denominator = math.sqrt(
        sum((x - centre_a) ** 2 for x in a) * sum((y - centre_b) ** 2 for y in b)
    )
    if denominator == 0:
        return unmeasured("constant ranks do not define Spearman correlation", n=len(left))
    return {"status": "measured", "value": numerator / denominator, "n": len(left)}


def cross_seed_stability(seed_stores: list[dict], *, reflection_policy="none") -> dict:
    """Jaccard on lexical claims and tie-correct Spearman on shared-claim ranks."""
    if len(seed_stores) < 2:
        return unmeasured("at least two measured seed stores are required", n=len(seed_stores))
    stores, eligibility = [], []
    seen = set()
    for source in seed_stores:
        seed = source["seed"]
        if not isinstance(seed, (str, int)) or isinstance(seed, bool) or seed in seen:
            raise ValueError("seed stores require distinct integer or text seeds")
        seen.add(seed)
        claims = defaultdict(list)
        if source.get("reflection_policy", reflection_policy) != reflection_policy:
            raise ValueError("cross-seed stores must share the same reflection treatment")
        projection = formation_records(source["beliefs"], reflection_policy=reflection_policy)
        rows = projection.pop("items")
        eligibility.append({"seed": seed, **projection})
        for row in rows:
            claim = canonical_claim(row["claim"])
            if not claim:
                continue
            rank = row["rank"]
            if not isinstance(rank, int) or isinstance(rank, bool) or not 0 <= rank <= 5:
                raise ValueError("belief ranks must be integers from zero through five")
            claims[claim].append(rank)
        stores.append((seed, {claim: median(ranks) for claim, ranks in claims.items()}))
    stores.sort(key=lambda row: json.dumps(row[0]))
    pairs = []
    for (seed_a, a), (seed_b, b) in combinations(stores, 2):
        common = sorted(a.keys() & b.keys())
        union = a.keys() | b.keys()
        pairs.append(
            {
                "seeds": [seed_a, seed_b],
                "common_n": len(common),
                "union_n": len(union),
                "jaccard": {"status": "measured", "value": len(common) / len(union)}
                if union
                else unmeasured("both seed stores are empty"),
                "rank_spearman": _spearman([a[key] for key in common], [b[key] for key in common]),
            }
        )
    jaccards = [row["jaccard"]["value"] for row in pairs if row["jaccard"]["status"] == "measured"]
    correlations = [
        row["rank_spearman"]["value"]
        for row in pairs
        if row["rank_spearman"]["status"] == "measured"
    ]
    return {
        "status": "measured" if jaccards else "unmeasured",
        "n_seeds": len(stores),
        "headline_eligible": len(stores) >= 5,
        "canonicalizer_id": CANONICALIZER_ID,
        "duplicate_claim_rank": "median",
        "formation_eligibility": eligibility,
        "mean_jaccard": mean(jaccards) if jaccards else None,
        "mean_rank_spearman": mean(correlations) if correlations else None,
        "pairs": pairs,
    }


def _claim(value):
    value = value.get("claim", "") if isinstance(value, dict) else value
    if not isinstance(value, str):
        raise TypeError("judged claims must be text")
    return value


def _payload(task, source):
    if not isinstance(source, dict):
        raise TypeError("judgment inputs must be objects")
    if task == "coherence":
        result = {"left": _claim(source["left"]), "right": _claim(source["right"])}
        for side in ("left", "right"):
            if isinstance(source[side], dict):
                result[side + "_valid_time"] = {
                    key: source[side].get(key) for key in ("valid_from", "valid_to")
                }
        return result
    keys = (
        ("before", "after", "evidence_text", "context")
        if task == "revision"
        else ("goal", "strategy", "trajectory", "declared_reason")
    )
    result = {key: _claim(source[key]) for key in keys if source.get(key) is not None}
    if task == "revision" and not result.get("before"):
        raise ValueError("revision judgments require a prior claim")
    if task == "dropped_goal" and not (result.get("goal") or result.get("strategy")):
        raise ValueError("dropped commitment judgments require a goal or strategy")
    return result


class FormationRunner:
    """Use separate, cross-family metered calls within one explicit total cap."""

    def __init__(
        self,
        world,
        model,
        spec: JudgeSpec,
        *,
        agent_family: str,
        budget_usd: float,
        max_tokens=2048,
    ):
        if (
            not isinstance(budget_usd, (int, float))
            or isinstance(budget_usd, bool)
            or not math.isfinite(budget_usd)
            or budget_usd <= 0
        ):
            raise ValueError("formation judging requires a finite positive total budget")
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or not 1 <= max_tokens <= 32768
        ):
            raise ValueError("max_tokens must be between one and 32768")
        self.world, self.spec, self.budget_usd = world, spec, float(budget_usd)
        self.judge = ToollessJudge(
            model, spec, agent_family=agent_family, budget_usd=budget_usd, max_tokens=max_tokens
        )

    def _receipt(self, source_run_id, completion, reservation, seen):
        store = self.world.store
        child_id = completion.get("measurement_run_id")
        if not isinstance(child_id, str) or not child_id or child_id in seen:
            raise ResearchCallError("formation judgment requires a new measurement run")
        child = store.get_run(child_id)
        config = child["config"]
        spent = store.spend(child_id)
        calls = [row for row in store.records(child_id) if row["kind"] == "model_call"]
        ledger = store.reconcile(child_id)
        if (
            child_id == source_run_id
            or config.get("measurement_of") != source_run_id
            or config.get("metering_lane") != "judge"
            or child["state"] not in {"sealed", "ended", "killed"}
            or child.get("tainted")
            or store.verify_record(child_id).get("valid") is not True
            or ledger.get("valid") is not True
            or ledger.get("reserved_usd") != 0
            or not calls
            or any(row.get("model") != self.spec.model for row in calls)
            or completion.get("spent") != spent
            or sum(spent.values()) > reservation + 1e-9
        ):
            raise ResearchCallError("formation judgment lacks verified child billing evidence")
        seen.add(child_id)
        return {"measurement_run_id": child_id, "spent_usd": sum(spent.values())}

    async def _label(self, source_run_id, task, payload, reservation, seen):
        result = await self.judge._call(
            source_run_id,
            f"Instrument {INSTRUMENT_ID}, task {task}. {INSTRUCTIONS[task]} "
            'Return JSON {"label":string,"reason":string} with the requested label vocabulary.',
            payload,
            budget_usd=reservation,
        )
        judgment = result["judgment"]
        if judgment.get("label") not in LABELS[task] or not isinstance(judgment.get("reason"), str):
            raise ResearchCallError("formation judge returned an invalid label or reason")
        if task == "revision" and (
            "revision_needed" not in judgment
            or (
                judgment["revision_needed"] is not None
                and not isinstance(judgment["revision_needed"], bool)
            )
        ):
            raise ResearchCallError("revision judge omitted a nullable revision_needed boolean")
        return {
            "label": judgment["label"],
            "reason": judgment["reason"],
            **({"revision_needed": judgment["revision_needed"]} if task == "revision" else {}),
            "measurement": self._receipt(source_run_id, result["measurement"], reservation, seen),
        }

    async def run(
        self,
        source_run_id,
        *,
        beliefs: list[dict],
        revision_observations: list = (),
        seed=0,
        dropped_goals: list = (),
        seed_stores: list = (),
        calibration_rows: list = (),
        coherence_pairs: list = (),
    ) -> dict:
        source = self.world.store.get_run(source_run_id)
        if (
            source["state"] not in {"sealed", "ended", "killed"}
            or source.get("tainted")
            or self.world.store.verify_record(source_run_id).get("valid") is not True
        ):
            raise ValueError("formation measurements require a sealed verified untainted source")
        source_ids = {row["id"] for row in beliefs}
        if any(row.get("belief_id") not in source_ids for row in revision_observations):
            raise ValueError("revision observations must identify a supplied belief")
        eligibility = formation_records(
            beliefs, reflection_policy=source["config"].get("reflection_policy", "none")
        )
        beliefs = eligibility.pop("items")
        eligible_ids = {row["id"] for row in beliefs}
        eligibility["excluded_revision_observations"] = sum(
            row["belief_id"] not in eligible_ids for row in revision_observations
        )
        revision_observations = [
            row for row in revision_observations if row["belief_id"] in eligible_ids
        ]
        sample = prose_sample(beliefs, seed=seed)
        stability = cross_seed_stability(
            seed_stores, reflection_policy=source["config"].get("reflection_policy", "none")
        )
        selected = {row["id"]: row for row in sample["items"]}
        all_ids = {row["id"]: row for row in beliefs}
        triples = {key for key, row in all_ids.items() if row.get("triple")}
        coverage = len(triples) / len(all_ids) if all_ids else None
        # Validate coordinates before any paid call, even for unsampled observations.
        revision_metrics(revision_observations, triple_coverage=coverage)
        if any(row.get("belief_id") not in all_ids for row in revision_observations):
            raise ValueError("revision observations must identify a supplied belief")
        jobs = []
        for index, row in enumerate(calibration_rows):
            task = row.get("task")
            if task not in LABELS or row.get("label") not in LABELS[task]:
                raise ValueError("calibration rows require a known task and human label")
            jobs.append(("calibration", index, task, _payload(task, row["input"]), row))
        pairs = list(coherence_pairs)
        if not pairs and len(selected) > 1:
            ids = sample["ids"]
            pairs = [[key, ids[(index + 1) % len(ids)]] for index, key in enumerate(ids)]
            if len(ids) == 2:
                pairs = pairs[:1]
        paired_ids, pair_keys = set(), set()
        for index, pair in enumerate(pairs):
            if len(pair) != 2 or pair[0] == pair[1] or any(key not in selected for key in pair):
                raise ValueError("coherence pairs must identify two distinct sampled claims")
            key = tuple(sorted(pair))
            if key in pair_keys:
                raise ValueError("coherence pairs must not repeat an unordered pair")
            pair_keys.add(key)
            paired_ids.update(pair)
            jobs.append(
                (
                    "coherence",
                    index,
                    "coherence",
                    _payload("coherence", {"left": selected[pair[0]], "right": selected[pair[1]]}),
                    pair,
                )
            )
        revision_rows = [row for row in revision_observations if row.get("belief_id") in selected]
        for index, row in enumerate(revision_rows):
            if not row.get("observation_ref"):
                raise ValueError("revision observations require world evidence references")
            payload = {**row, "before": row.get("before", selected[row["belief_id"]]["claim"])}
            jobs.append(("revision", index, "revision", _payload("revision", payload), row))
        for index, row in enumerate(dropped_goals):
            if not row.get("id") or not row.get("observation_ref"):
                raise ValueError(
                    "dropped commitments require an id and trajectory evidence reference"
                )
            jobs.append(("dropped_goal", index, "dropped_goal", _payload("dropped_goal", row), row))
        outputs = defaultdict(list)
        per_call_nano = (
            int(Decimal(str(self.budget_usd)) * 1_000_000_000) // len(jobs) if jobs else 0
        )
        if jobs and per_call_nano < 1:
            raise ValueError("formation budget cannot reserve one nanodollar per planned call")
        reservation = per_call_nano / 1_000_000_000
        seen = {row["run_id"] for row in self.world.store.list_runs()}
        for group, index, task, payload, row in jobs:
            label = await self._label(source_run_id, task, payload, reservation, seen)
            outputs[group].append({"index": index, "task": task, **label, "source": row})
        calibrations = {}
        for task, labels in LABELS.items():
            actual = [row for row in outputs["calibration"] if row["task"] == task]
            if not actual:
                calibrations[task] = unmeasured(
                    "expert-labelled task calibration is required", column="J", kappa=None
                )
                continue
            truth, predicted = (
                [row["source"]["label"] for row in actual],
                [row["label"] for row in actual],
            )
            metrics = calibration(truth, predicted, positive=labels[0])
            calibrations[task] = {
                "status": "measured",
                **metrics,
                "per_class": {
                    label: calibration(truth, predicted, positive=label) for label in labels
                },
                "calibration_set_hash": _hash([row["source"] for row in actual]),
            }
        for group in ("coherence", "revision", "dropped_goal"):
            for row in outputs[group]:
                calibrated = calibrations[row["task"]]
                row.update(
                    column=calibrated["column"],
                    kappa=calibrated["kappa"],
                    exploratory=calibrated["column"] == "J",
                )
        judged_revisions = [
            {**row["source"], "revision_needed": row["revision_needed"]}
            for row in outputs["revision"]
            if row["label"] != "unclear"
        ]
        triple_judgments = [
            row for row in outputs["revision"] if row["source"]["belief_id"] in triples
        ]
        measurements = [row["measurement"] for rows in outputs.values() for row in rows]
        public_sample = {key: value for key, value in sample.items() if key != "items"}
        revision_coverage = (
            len({row["belief_id"] for row in revision_rows}) / sample["n"] if sample["n"] else None
        )
        return {
            "instrument_id": INSTRUMENT_ID,
            "judge_id": self.spec.model,
            "judge_family": self.spec.family,
            "truth_adjudicated": False,
            "formation_eligibility": eligibility,
            "prose": {
                "sample": public_sample,
                "status": "measured"
                if sample["complete_200"] and revision_coverage == 1
                else "partial"
                if sample["n"]
                else "unmeasured",
                "coherence_pair_policy": "explicit"
                if coherence_pairs
                else "sample_hash_order_ring",
                "coherence_record_coverage": len(paired_ids) / sample["n"] if sample["n"] else None,
                "coherence": self._summary(outputs["coherence"], calibrations["coherence"]),
                "revision": self._summary(outputs["revision"], calibrations["revision"]),
                "belief_r": self._belief_r(outputs["revision"], calibrations["revision"]),
                "revision_record_coverage": revision_coverage,
                "revision_latency": {
                    **revision_metrics(judged_revisions, triple_coverage=coverage),
                    "column": calibrations["revision"]["column"],
                    "kappa": calibrations["revision"]["kappa"],
                },
            },
            "triple_subset": {
                "n": len(triples),
                "coverage": coverage,
                "revision": self._summary(triple_judgments, calibrations["revision"]),
                "belief_r": self._belief_r(triple_judgments, calibrations["revision"]),
                "revision_latency": revision_metrics(
                    [row for row in revision_observations if row.get("belief_id") in triples],
                    triple_coverage=coverage,
                ),
            },
            "dropped_goals": self._summary(outputs["dropped_goal"], calibrations["dropped_goal"]),
            "cross_seed": stability,
            "calibration": calibrations,
            "calibration_judgments": outputs["calibration"],
            "measurements": measurements,
            "budget_usd": self.budget_usd,
            "reserved_per_call_usd": reservation,
            "spent_usd": sum(row["spent_usd"] for row in measurements),
            "scientific_gate_passed": False,
        }

    @staticmethod
    def _summary(rows, calibrated):
        counts = dict(Counter(row["label"] for row in rows))
        return {
            "status": "measured" if rows else "unmeasured",
            "n": len(rows),
            "counts": counts,
            "rates": {label: count / len(rows) for label, count in counts.items()},
            "decision_coverage": sum(row["label"] != "unclear" for row in rows) / len(rows)
            if rows
            else None,
            "column": calibrated["column"],
            "kappa": calibrated["kappa"],
            "exploratory": calibrated["column"] == "J",
            "items": rows,
        }

    @staticmethod
    def _belief_r(rows, calibrated):
        resolved = [row for row in rows if row.get("revision_needed") is not None]
        return {
            "status": "measured" if resolved else "unmeasured",
            "n": len(resolved),
            "coverage": len(resolved) / len(rows) if rows else None,
            "missed_revisions": sum(
                row["revision_needed"] and row["source"].get("revised_action") is None
                for row in resolved
            )
            if resolved
            else None,
            "unnecessary_revisions": sum(
                not row["revision_needed"] and row["source"].get("revised_action") is not None
                for row in resolved
            )
            if resolved
            else None,
            "column": calibrated["column"],
            "kappa": calibrated["kappa"],
        }
