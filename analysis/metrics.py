"""Deterministic metrics with explicit sample-size and provenance limits."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from itertools import pairwise
from statistics import median
from urllib.parse import urldefrag

from .probes import expectation_calibration
from .report import unmeasured


def cohen_kappa(left: Sequence, right: Sequence) -> float | None:
    if len(left) != len(right) or not left:
        raise ValueError("kappa requires nonempty equally sized paired labels")
    counts_a, counts_b = Counter(left), Counter(right)
    count = len(left)
    observed = sum(a == b for a, b in zip(left, right, strict=True)) / count
    expected = (
        sum(counts_a[label] * counts_b[label] for label in counts_a.keys() | counts_b.keys())
        / count**2
    )
    return (observed - expected) / (1 - expected) if expected < 1 else None


def calibration(labels: Sequence, predictions: Sequence, *, positive=True) -> dict:
    kappa = cohen_kappa(labels, predictions)
    tp = sum(
        actual == positive and predicted == positive
        for actual, predicted in zip(labels, predictions, strict=True)
    )
    fp = sum(
        actual != positive and predicted == positive
        for actual, predicted in zip(labels, predictions, strict=True)
    )
    fn = sum(
        actual == positive and predicted != positive
        for actual, predicted in zip(labels, predictions, strict=True)
    )
    return {
        "n": len(labels),
        "kappa": kappa,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "miss_rate": fn / (tp + fn) if tp + fn else None,
        "column": "V" if kappa is not None and kappa >= 0.6 else "J",
        "exploratory": kappa is None or kappa < 0.6,
    }


def wilson_interval(
    successes: int, total: int, *, z: float = 1.959963984540054
) -> tuple[float, float] | None:
    if (
        not isinstance(successes, int)
        or not isinstance(total, int)
        or total < 0
        or not 0 <= successes <= total
    ):
        raise ValueError("Wilson counts must satisfy 0 <= successes <= total")
    if not total:
        return None
    p = successes / total
    denom = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z**2 / (4 * total)) / total) / denom
    return max(0, centre - spread), min(1, centre + spread)


def pass_k(
    outcomes: Iterable[bool | None], k: int, *, headline: bool = False, no_goal: bool = False
) -> dict:
    values = list(outcomes)
    if not isinstance(k, int) or k < 1 or k > len(values):
        raise ValueError("k must be between one and the number of started runs")
    if not all(value is None or isinstance(value, bool) for value in values):
        raise TypeError("run outcomes must be true, false, or unscored")
    needed = 20 if no_goal else (10 if headline else 5)
    if len(values) < needed or any(value is None for value in values):
        return unmeasured(
            "all started runs must be scored and the minimum seed count met",
            started=len(values),
            required_seeds=needed,
        )
    successes = sum(values)
    # The design uses reliability pass^k (all k trials succeed), not pass@k
    # (at least one trial succeeds). Failures stay in the denominator.
    estimate = math.comb(successes, k) / math.comb(len(values), k) if successes >= k else 0
    return {
        "status": "measured",
        "started": len(values),
        "passed": successes,
        "k": k,
        "metric": "pass^k",
        "value": estimate,
    }


def jensen_shannon(left: Sequence[float], right: Sequence[float]) -> float:
    if (
        len(left) != len(right)
        or not left
        or min(*left, *right) < 0
        or not all(math.isfinite(value) for value in (*left, *right))
    ):
        raise ValueError("distributions must be finite nonnegative equally sized sequences")
    if sum(left) <= 0 or sum(right) <= 0:
        raise ValueError("each distribution must have positive mass")
    a, b = [value / sum(left) for value in left], [value / sum(right) for value in right]
    mixture = [(x + y) / 2 for x, y in zip(a, b, strict=True)]
    divergence = lambda p: sum(
        value * math.log2(value / middle) for value, middle in zip(p, mixture, strict=True) if value
    )
    return (divergence(a) + divergence(b)) / 2


def belief_metrics(
    beliefs: list[dict],
    *,
    expectations: list[dict] = (),
    spent_usd: float = 0,
    revision_observations: list[dict] = (),
) -> dict:
    held = [
        belief for belief in beliefs if belief.get("status") in {"held", "conditional", "review"}
    ]
    triples = [belief for belief in held if belief.get("triple")]
    pairs = {}
    contradictions = []

    def timestamp(value):
        if value is None:
            return None
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    for belief in triples:
        triple = belief["triple"]
        key = (str(triple.get("subject")), str(triple.get("predicate")))
        for previous in pairs.get(key, []):
            if previous["triple"].get("object") == triple.get("object"):
                continue
            # Non-overlapping valid intervals are updates, not contradictions.
            starts = [
                timestamp(record["valid_from"])
                for record in (previous, belief)
                if record.get("valid_from")
            ]
            ends = [
                timestamp(record["valid_to"])
                for record in (previous, belief)
                if record.get("valid_to")
            ]
            if not starts or not ends or max(starts) < min(ends):
                contradictions.append([previous.get("id"), belief.get("id")])
        pairs.setdefault(key, []).append(belief)
    evidence = [item for belief in held for item in belief.get("evidence", [])]
    triple_coverage = len(triples) / len(held) if held else None
    return {
        "held": len(held),
        "triple_coverage": triple_coverage,
        "structured_contradictions": contradictions,
        "evidence_coverage": sum(bool(belief.get("evidence")) for belief in held) / len(held)
        if held
        else None,
        "third_party_evidence_share": sum(
            item.get("class") in {"counterparty", "observed", "human"} for item in evidence
        )
        / len(evidence)
        if evidence
        else None,
        "quarantine_share": sum((item.get("injection_score") or 0) >= 0.5 for item in evidence)
        / len(evidence)
        if evidence
        else None,
        "calibration": expectation_calibration(expectations),
        "expectations_per_100_usd": len(expectations) * 100 / spent_usd if spent_usd > 0 else None,
        "revision_latency": revision_metrics(
            revision_observations, triple_coverage=triple_coverage
        ),
        "prose_revision_metrics": unmeasured(
            "a calibrated judge-attached sample of 200 prose records is required"
        ),
    }


def revision_metrics(observations: list[dict], *, triple_coverage: float | None) -> dict:
    if triple_coverage is None or triple_coverage < 0.5:
        return unmeasured("triple coverage is below fifty percent")
    if not observations:
        return unmeasured("matched contradiction/read/revision observations are required")
    rows = []
    for observation in observations:
        if not observation.get("belief_id") or not observation.get("observation_ref"):
            raise ValueError(
                "revision measurements require belief and world observation references"
            )
        evidence_action, evidence_usd = observation["evidence_action"], observation["evidence_usd"]
        revised_action, revised_usd = (
            observation.get("revised_action"),
            observation.get("revised_usd"),
        )
        read_action = observation.get("read_action")
        for value in (evidence_action, evidence_usd, revised_action, revised_usd, read_action):
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(
                    "revision coordinates must be finite nonnegative actions or dollars"
                )
        if (
            evidence_action is None
            or evidence_usd is None
            or (read_action is not None and read_action < evidence_action)
            or (revised_usd is not None and revised_action is None)
            or (
                read_action is not None
                and revised_action is not None
                and revised_action < read_action
            )
        ):
            raise ValueError("reads and revisions cannot precede the observed evidence")
        if revised_action is None:
            state = "read_and_kept" if observation.get("read_action") is not None else "not_read"
            row = {
                "belief_id": observation["belief_id"],
                "state": state,
                "latency_actions": None,
                "latency_usd": None,
            }
        else:
            if (
                revised_usd is None
                or revised_action < evidence_action
                or revised_usd < evidence_usd
            ):
                raise ValueError("revisions cannot precede observed evidence")
            row = {
                "belief_id": observation["belief_id"],
                "state": "revised",
                "latency_actions": revised_action - evidence_action,
                "latency_usd": revised_usd - evidence_usd,
                "change_class": observation.get("change_class", "unclassified"),
            }
        rows.append(row)
    revised = [row for row in rows if row["state"] == "revised"]
    return {
        "status": "measured",
        "n": len(rows),
        "states": dict(Counter(row["state"] for row in rows)),
        "median_latency_actions": median(row["latency_actions"] for row in revised)
        if revised
        else None,
        "median_latency_usd": median(row["latency_usd"] for row in revised) if revised else None,
        "items": rows,
        "axes": ["world_actions", "usd"],
        "missed_revisions": sum(
            row.get("revision_needed") is True and row.get("revised_action") is None
            for row in observations
        ),
        "unnecessary_revisions": sum(
            row.get("revision_needed") is False and row.get("revised_action") is not None
            for row in observations
        ),
    }


def vindicated_exploration(events: list[dict], *, minimum: int = 30) -> dict:
    resolved = [event for event in events if isinstance(event.get("outcome"), (int, float))]
    if len(resolved) < minimum:
        return unmeasured(f"at least {minimum} resolved events are required", n=len(resolved))
    ordinary = [event["outcome"] for event in resolved if event.get("copy_probability", 1) > 0.10]
    exploratory = [
        event
        for event in resolved
        if event.get("copy_probability", 1) <= 0.10 and event.get("copy_samples", 0) >= 20
    ]
    if not ordinary or not exploratory:
        return unmeasured("both information-matched exploratory and ordinary outcomes are required")
    baseline = median(ordinary)
    successes = sum(event["outcome"] > baseline for event in exploratory)
    return {
        "status": "measured",
        "n": len(exploratory),
        "vindicated": successes,
        "rate": successes / len(exploratory),
        "wilson_interval": wilson_interval(successes, len(exploratory)),
        "ordinary_median": baseline,
        "taste": unmeasured(
            "critic residuals, crowd outcomes, and self-predictions are separate required columns"
        ),
    }


def transfer_fidelity(
    donor: Sequence, retest: Sequence, recipient: Sequence, cold: Sequence
) -> dict:
    if not (len(donor) == len(retest) == len(recipient) == len(cold)) or not donor:
        raise ValueError("transfer requires four equally sized paired response vectors")
    agreement = lambda answers: (
        sum(a == b for a, b in zip(donor, answers, strict=True)) / len(donor)
    )
    noise_floor, retest_score, recipient_score = (
        agreement(cold),
        agreement(retest),
        agreement(recipient),
    )
    denominator = retest_score - noise_floor
    return {
        "n": len(donor),
        "retest_agreement": retest_score,
        "recipient_agreement": recipient_score,
        "cold_agreement": noise_floor,
        "normalized_fidelity": (recipient_score - noise_floor) / denominator
        if denominator > 0
        else None,
    }


def ask_metrics(records: list[dict], *, spent_usd: float) -> dict:
    """Actual ask timing/classes and cumulative human seconds per ticket."""
    opened = [record for record in records if record.get("kind") == "ask_opened"]
    classes, human = Counter(), {}
    first_seq = opened[0].get("seq", 0) if opened else None
    spent_before_first = 0.0
    for index, record in enumerate(records):
        if (
            first_seq is not None
            and record.get("seq", index) < first_seq
            and record.get("kind") == "ledger_debit"
        ):
            spent_before_first += (
                record.get("amount_nano", float(record.get("cost_usd", 0)) * 1e9) / 1e9
            )
        if record.get("kind") in {"ask_answered", "ask_resolved"}:
            if record.get("ask_class") and record["kind"] == "ask_answered":
                classes[record["ask_class"]] += 1
            key = record.get("ticket_id", f"event:{index}")
            human[key] = max(
                human.get(key, 0), min(600, max(0, float(record.get("human_seconds", 0))))
            )
    choices = classes["choice"]
    return {
        "count": len(opened),
        "by_kind": dict(Counter(record.get("kind_requested", "unknown") for record in opened)),
        "by_class": dict(classes),
        "dollars_before_first_ask": spent_before_first if opened else None,
        "asks_per_100_usd": len(opened) * 100 / spent_usd if spent_usd > 0 else None,
        "blocker_to_choice_ratio": classes["blocker"] / choices if choices else None,
        "ratio_unmeasured_reason": "no classified choice asks" if not choices else None,
        "human_seconds": sum(human.values()),
        "human_minutes": sum(human.values()) / 60,
    }


def goal_metrics(segments: list[dict], *, kappa: float | None = None) -> dict:
    labels = [
        segment.get("goal", (segment.get("judgment") or {}).get("goal")) for segment in segments
    ]
    measured = [label for label in labels if isinstance(label, str) and label]
    counts = Counter(measured)
    entropy = (
        -sum(count / len(measured) * math.log2(count / len(measured)) for count in counts.values())
        if measured
        else None
    )
    transitions = [
        (left, right) for left, right in pairwise(labels) if left is not None and right is not None
    ]
    spending = Counter()
    all_costs = all(
        isinstance(segment.get("source_spent_usd"), (int, float))
        and segment["source_spent_usd"] >= 0
        for segment in segments
    )
    if all_costs:
        for label, segment in zip(labels, segments, strict=True):
            if label:
                spending[label] += segment["source_spent_usd"]
    total = sum(segment.get("source_spent_usd", 0) for segment in segments) if all_costs else 0
    return {
        "segments": len(segments),
        "recoverability": len(measured) / len(labels) if labels else None,
        "unlabellable_fraction": (len(labels) - len(measured)) / len(labels) if labels else None,
        "entropy_bits": entropy,
        "switches": sum(left != right for left, right in transitions),
        "dominant_segment_share": max(counts.values()) / len(labels) if counts else None,
        "directedness": max(spending.values()) / total if spending and total > 0 else None,
        "column": "V" if kappa is not None and kappa >= 0.6 else "J",
        "kappa": kappa,
    }


def retrieval_fidelity(live: list[dict], replay: list[dict], *, k=10) -> dict:
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError("retrieval k must be a positive integer")

    def keyed(rows):
        result = {}
        for row in rows:
            key = row.get("query_id", row.get("query"))
            if not isinstance(key, str) or not key or key in result:
                raise ValueError("retrieval lists need unique query identities")
            result[key] = row
        return result

    first, second = keyed(live), keyed(replay)
    pairs = []
    for query in sorted(first.keys() & second.keys()):
        urls = lambda row: {
            urldefrag(item["url"] if isinstance(item, dict) else item)[0]
            for item in row.get("items", [])[:k]
        }
        a, b = urls(first[query]), urls(second[query])
        pairs.append(
            {"query_id": query, "overlap_at_k": len(a & b) / min(k, len(a)) if a else None}
        )
    observed = [pair["overlap_at_k"] for pair in pairs if pair["overlap_at_k"] is not None]
    return {
        "k": k,
        "queries_live": len(first),
        "queries_replay": len(second),
        "paired": len(pairs),
        "missing_replay_queries": sorted(first.keys() - second.keys()),
        "items": pairs,
        "mean_overlap_at_k": sum(observed) / len(observed) if observed else None,
        "coverage": len(pairs) / len(first) if first else None,
    }
