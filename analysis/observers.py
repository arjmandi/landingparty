"""Reproducible prediction-loss observers over the actual artifact stream.

Byte n-grams are explicit, content-addressed observer choices, not substitutes
silently presented as pretrained language models. Their losses are computed
from predictive probabilities, never requested as a judge's numerical opinion.
Researchers can instead supply hash-verified losses from another registered
observer to the trajectory instrument. No observer is enabled by default.
"""

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from worldd.util import canonical, digest

from .trajectory import open_endedness


class ByteNgramObserver:
    def __init__(self, *, order, alpha, background=b"", background_hash):
        if isinstance(order, bool) or not isinstance(order, int) or not 1 <= order <= 6:
            raise ValueError("Observer order must be an integer from one to six")
        if (
            isinstance(alpha, bool)
            or not isinstance(alpha, (int, float))
            or not math.isfinite(alpha)
            or alpha <= 0
        ):
            raise ValueError("Observer smoothing must be positive and finite")
        if (
            not isinstance(background, bytes)
            or len(background) > 8_000_000
            or digest(background) != background_hash
        ):
            raise ValueError("Observer background must match its pinned byte hash")
        self.order, self.alpha, self.background = order, float(alpha), background
        self.spec = {
            "algorithm": "byte-ngram/dirichlet@1",
            "order": order,
            "alpha": float(alpha),
            "background_hash": background_hash,
            "framing": "newline-between-training-artifacts@1",
            "loss_unit": "nats_per_byte",
        }
        self.observer_id = digest(self.spec)

    def loss(self, training, target):
        if not target:
            raise ValueError("An empty target cannot provide prediction loss")
        corpus = b"\n".join([self.background, *training])
        counts = defaultdict(Counter)
        history = []
        for symbol in corpus:
            context = tuple(history[-(self.order - 1) :]) if self.order > 1 else ()
            counts[context][symbol] += 1
            history.append(symbol)
        total_loss = 0.0
        for symbol in target:
            context = tuple(history[-(self.order - 1) :]) if self.order > 1 else ()
            values = counts.get(context, {})
            probability = (values.get(symbol, 0) + self.alpha) / (
                sum(values.values()) + 256 * self.alpha
            )
            total_loss -= math.log(probability)
            history.append(symbol)
        return total_loss / len(target)


def measure_observers(artifacts, observer_specs, *, coordinates, output_dir, baselines=None):
    """Measure l(t,T) with prefix t and held-out artifact T (one-based index)."""
    if not artifacts or len(artifacts) > 10000:
        raise ValueError("A bounded nonempty artifact stream is required")
    for artifact in artifacts:
        if (
            not artifact.get("observation_ref")
            or not isinstance(artifact.get("body"), bytes)
            or digest(artifact["body"]) != artifact.get("hash")
        ):
            raise ValueError("Each actual artifact needs a reference and matching byte hash")
    if sum(len(artifact["body"]) for artifact in artifacts) > 8_000_000:
        raise ValueError("Observer stream exceeds the declared eight-megabyte implementation cap")
    observers = [ByteNgramObserver(**spec) for spec in observer_specs]
    if len(observers) < 2 or len({observer.observer_id for observer in observers}) != len(
        observers
    ):
        raise ValueError("At least two distinct pinned observer configurations are required")
    chosen, seen = [], set()
    for t, horizon in coordinates:
        if (
            isinstance(t, bool)
            or isinstance(horizon, bool)
            or not isinstance(t, int)
            or not isinstance(horizon, int)
            or not 0 <= t < horizon <= len(artifacts)
            or (t, horizon) in seen
        ):
            raise ValueError("Coordinates must be unique integer 0<=t<T<=stream length")
        chosen.append((t, horizon))
        seen.add((t, horizon))
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    rows, references = [], {}
    for observer in observers:
        for t, horizon in chosen:
            target = artifacts[horizon - 1]
            receipt = {
                "observer_id": observer.observer_id,
                "observer_spec": observer.spec,
                "t": t,
                "T": horizon,
                "loss": observer.loss(
                    [artifact["body"] for artifact in artifacts[:t]], target["body"]
                ),
                "target_hash": target["hash"],
                "target_ref": target["observation_ref"],
                "target_bytes": len(target["body"]),
                "training_refs": [artifact["observation_ref"] for artifact in artifacts[:t]],
                "scientific_gate_evidence": False,
            }
            reference = digest(receipt)
            path = root / (reference.split(":", 1)[1] + ".json")
            if path.exists() and json.loads(path.read_text()) != receipt:
                raise ValueError("Observer receipt path conflicts with existing evidence")
            path.write_text(canonical(receipt), encoding="utf-8")
            references[reference] = {"path": str(path), "hash": digest(path.read_bytes())}
            rows.append({**receipt, "observation_ref": reference})
    return {
        "rows": rows,
        "reference_artifacts": references,
        "metrics": open_endedness(rows, baselines=baselines),
        "scientific_gate_evidence": False,
    }
