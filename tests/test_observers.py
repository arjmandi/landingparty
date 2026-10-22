import json
import math

import pytest

from analysis.observers import ByteNgramObserver, measure_observers
from worldd.util import digest


def test_prediction_loss_uses_actual_probabilities_and_training_prefix():
    observer = ByteNgramObserver(order=1, alpha=1, background=b"", background_hash=digest(b""))
    assert observer.loss([], b"aaaa") == pytest.approx(math.log(256))
    assert observer.loss([b"a" * 100], b"aaaa") < observer.loss([], b"aaaa")
    assert observer.loss([], b"aaaa") == pytest.approx(math.log(256))
    with pytest.raises(ValueError, match="hash"):
        ByteNgramObserver(order=1, alpha=1, background=b"changed", background_hash=digest(b""))


def test_two_observers_write_actual_hash_pinned_losses_without_future_training(tmp_path):
    artifacts = [
        {"body": body, "hash": digest(body), "observation_ref": f"artifact{index}"}
        for index, body in enumerate((b"aaaa", b"abababab", b"abcabcabc"))
    ]
    specs = [
        {"order": order, "alpha": 0.5, "background": b"abab", "background_hash": digest(b"abab")}
        for order in (1, 2)
    ]
    result = measure_observers(
        artifacts, specs, coordinates=[(0, 2), (0, 3), (1, 2), (1, 3)], output_dir=tmp_path
    )
    assert result["metrics"]["status"] == "measured"
    assert len(result["rows"]) == 8
    assert not result["scientific_gate_evidence"]
    for row in result["rows"]:
        reference = result["reference_artifacts"][row["observation_ref"]]
        raw = __import__("pathlib").Path(reference["path"]).read_bytes()
        assert digest(raw) == reference["hash"]
        receipt = json.loads(raw)
        assert len(receipt["training_refs"]) == row["t"]
        assert receipt["target_ref"] not in receipt["training_refs"]
        assert receipt["loss"] >= 0
    with pytest.raises(ValueError, match="Coordinates"):
        measure_observers(artifacts, specs, coordinates=[(2, 2)], output_dir=tmp_path)


def test_equivalent_observers_cannot_count_twice_and_forged_capture_rejected(tmp_path):
    stream = [{"body": b"x", "hash": digest(b"x"), "observation_ref": "a1"}]
    spec = {"order": 1, "alpha": 1, "background": b"", "background_hash": digest(b"")}
    with pytest.raises(ValueError, match="distinct"):
        measure_observers(
            stream, [spec, {**spec, "alpha": 1.0}], coordinates=[(0, 1)], output_dir=tmp_path
        )
    with pytest.raises(ValueError, match="matching byte hash"):
        measure_observers(
            [{**stream[0], "body": b"changed"}],
            [spec, {**spec, "order": 2}],
            coordinates=[(0, 1)],
            output_dir=tmp_path,
        )
