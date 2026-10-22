import hashlib
import json

import pytest

from analysis.grading import GradingRunner
from analysis.pipeline import PostRunPipeline
from worldd.models import RunConfig
from worldd.runtime import World
from worldd.storage import Store


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def graded(tmp_path):
    world = World(Store(tmp_path), Gateway())
    run_id = world.create_run(RunConfig(family="F5"))["run_id"]
    root = world.store.run_dir(run_id)
    (root / "work/out/faq.md").write_text("Actual delivered FAQ")
    (root / "work/out/answers.json").write_text(json.dumps({"a": 42, "b": 99}))
    (root / "work/out/math.json").write_text(
        json.dumps(
            [{"id": "c", "claim": "fixture theorem", "label": "proved", "checker_id": "trusted"}]
        )
    )
    await world.end_run(run_id)
    protocol = {
        "protocol_id": "test-code-grader",
        "core_threshold": 0.8,
        "checklist": [
            {
                "item_id": "delivered",
                "check": {"kind": "file_exists", "path": "work-snapshot/out/faq.md"},
            },
            {
                "item_id": "answer",
                "check": {
                    "kind": "json_equals",
                    "path": "work-snapshot/out/answers.json",
                    "pointer": "/a",
                    "expected": 42,
                },
            },
            {
                "item_id": "missing",
                "check": {"kind": "file_exists", "path": "work-snapshot/out/missing.txt"},
            },
        ],
        "hidden_items": [
            {"item_id": "a", "answer": 42},
            {"item_id": "b", "answer": 1},
            {"item_id": "c", "answer": 0},
        ],
        "answers_path": "work-snapshot/out/answers.json",
        "claims_validator": True,
        "claims_path": "work-snapshot/out/math.json",
    }

    def freeze(value):
        path = root / "grader.json"
        path.write_text(json.dumps(value))
        return {
            "path": "grader.json",
            "hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    try:
        yield world, run_id, root, protocol, freeze
    finally:
        await world.aclose()


async def test_actual_frozen_core_hidden_keys_and_trusted_claim_callback(graded):
    world, run_id, root, protocol, freeze = graded
    before = world.store.records(run_id)
    pipeline = PostRunPipeline(world)
    called = []

    def trusted(claim):
        called.append(claim["id"])
        return {"passed": False, "observation_ref": "trusted-fixture-check"}

    pipeline.register_claim_checker("trusted", trusted)
    result = await pipeline.run(run_id, {"grading_protocol": freeze(protocol)})
    assert result["status"] == "completed"
    score = json.loads((root / "analysis/grading.json").read_text())
    assert score["checklist"]["pass_fraction"] == pytest.approx(2 / 3)
    assert score["checklist"]["pairwise_bracket"] == "bottom"
    assert score["hidden_items"]["score"] == -1
    assert score["hidden_items"]["counts"] == {"correct": 1, "incorrect": 1, "abstained": 1}
    assert called == ["c"] and score["claims"]["overclaim_index"] == 1
    assert not score["scientific_gate_passed"] and not score["preregistered"]
    assert world.store.records(run_id) == before


async def test_failed_core_caps_pairwise_without_buying_any_judgments(graded):
    world, run_id, root, protocol, freeze = graded

    class Uncalled:
        async def complete(self, *args, **kwargs):
            raise AssertionError("failed core must never buy a pairwise preference")

    result = await PostRunPipeline(world, Uncalled()).run(
        run_id,
        {
            "grading_protocol": freeze(protocol),
            "measurement_budget_usd": 1,
            "judge": {"model": "other", "family": "other", "budget_usd": 1},
            "agent_family": "agent",
            "pairwise": {
                "left_path": "work-snapshot/out/faq.md",
                "right_path": "work-snapshot/out/faq.md",
            },
        },
    )
    assert result["status"] == "completed" and result["measurement_costs"]["usd"] == 0
    pairwise = json.loads((root / "analysis/pairwise.json").read_text())
    assert pairwise["winner"] == "bottom_bracket" and pairwise["paid_calls"] == 0


async def test_workspace_tamper_and_untrusted_check_kind_are_rejected(graded):
    world, run_id, root, protocol, freeze = graded
    descriptor = freeze(protocol)
    (root / "work-snapshot/out/faq.md").write_text("Changed after terminal seal")
    with pytest.raises(ValueError, match="sealed workspace anchor"):
        await GradingRunner(world).run(run_id, protocol_artifact=descriptor, archive={})


async def test_grader_never_executes_workspace_check_code(graded):
    world, run_id, _root, protocol, freeze = graded
    protocol["checklist"][0]["check"]["kind"] = "execute_workspace_python"
    with pytest.raises(ValueError, match="built-in check kinds"):
        await GradingRunner(world).run(run_id, protocol_artifact=freeze(protocol), archive={})
