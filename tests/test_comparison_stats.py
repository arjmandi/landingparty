"""Analytic cases backed by actual sealed Store records and captured judge fixtures."""

import hashlib
import json
from types import MethodType, SimpleNamespace

import pytest

from analysis.comparisons import build_pairwise_elo
from analysis.judges import STYLE_CONTROL_INSTRUCTION
from analysis.report import TUPLE_FIELDS
from analysis.signatures import _icc, build_signature_icc
from worldd.runtime import World
from worldd.storage import Store, content_hash
from worldd.workspaces import snapshot_manifest


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path)
    world = SimpleNamespace(store=store, services={})
    world.report = MethodType(World.report, world)
    yield world
    store.close()


def source(
    world, model, seed, actions, *, family="F7", harness="fixture@1", learning="context", core=None
):
    version = dict.fromkeys(TUPLE_FIELDS, "fixture@1")
    version.update(model_snapshot_id=model, harness_id=harness)
    config = {
        "family": family,
        "seed": seed,
        "harness_id": harness,
        "learning_mechanism": learning,
        "model_snapshot_id": model,
        "tuple": version,
        "goal": None if family == "F7" else "A frozen artifact task",
        "budget": {"usd": 20, "wall_s": 60},
    }
    run_id = world.store.create_run(config)["run_id"]
    root = world.store.run_dir(run_id)
    for index in range(actions):
        world.store.append_record(
            run_id, "action", action_id=f"a_{index}", tool="deliver", params={}, in_flight_before=0
        )
        world.store.append_record(
            run_id, "outcome", action_id=f"a_{index}", tool="deliver", reply={"kind": "result"}
        )
    world.store.append_record(
        run_id, "model_call", model=model, call_id="m_source", usage={"input": 1}, usd=0
    )
    world.store.append_record(
        run_id,
        "completeness_audit",
        audit={"model_calls_transcript": 1, "shell_audited": True, "shell_bypass_candidates": 0},
    )
    snapshot = root / "work-snapshot"
    snapshot.mkdir()
    (snapshot / "artifact.txt").write_text(
        "Source " + model + ": supported result and verified evidence."
    )
    world.store.append_record(run_id, "workspace_snapshot", **snapshot_manifest(snapshot))
    world.store.append_record(run_id, "seal")
    world.store.update_run(run_id, state="sealed", end={"reason": "agent_finished"})
    if core is not None:
        stages(world, run_id, {"grading": {"checklist": {"core_passed": core}}})
    return run_id


def stages(world, run_id, values):
    root = world.store.run_dir(run_id)
    directory = root / "analysis"
    directory.mkdir(exist_ok=True)
    manifest_path = directory / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {
            "run_id": run_id,
            "source_record_head": world.store.records(run_id)[-1]["hash"],
            "stages": {},
        }
    )
    for name, value in values.items():
        path = directory / (name + ".json")
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        manifest["stages"][name] = {
            "status": "completed",
            "artifact": "analysis/" + path.name,
            "hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }
    manifest_path.write_text(json.dumps(manifest))
    return manifest


def participant(world, run_id):
    run = world.store.get_run(run_id)
    config = run["config"]
    anchor = next(row for row in world.store.records(run_id) if row["kind"] == "workspace_snapshot")
    return {
        "run_id": run_id,
        "path": "work-snapshot/artifact.txt",
        "hash": "sha256:"
        + hashlib.sha256(
            (world.store.run_dir(run_id) / "work-snapshot/artifact.txt").read_bytes()
        ).hexdigest(),
        "snapshot_anchor": anchor["hash"],
        **{key: config[key] for key in ["family", "model_snapshot_id", "harness_id", "tuple"]},
        "goal_hash": content_hash(config["goal"]),
    }


def judgment(world, parent, data, winner, *, judge="gemini-fixture-judge", style=True):
    child_id = world.store.create_run(
        {
            "budget": {"usd": 1},
            "measurement_of": parent,
            "metering_lane": "judge",
            "model_snapshot_id": judge,
        }
    )["run_id"]
    world.store.debit(child_id, "judge", "0.01")
    call_id = "m_judge"
    request = {
        "model": judge,
        "tools": [],
        "messages": [
            {
                "role": "system",
                "content": "Blinded tool-less pairwise judge. "
                + (STYLE_CONTROL_INSTRUCTION if style else ""),
            },
            {"role": "user", "content": json.dumps(data)},
        ],
    }
    result = {"winner": winner, "reason": "Fixture judgment"}
    text = json.dumps(result)
    response = json.dumps({"choices": [{"message": {"content": text}}]})
    request_hash = "sha256:" + hashlib.sha256(json.dumps(request).encode()).hexdigest()
    world.store.append_record(
        child_id,
        "model_call",
        call_id=call_id,
        model=judge,
        usage={"input": 4, "output": 2},
        usd=0.01,
        request_hash=request_hash,
    )
    directory = world.store.run_dir(child_id) / "model-io"
    directory.mkdir()
    raw = json.dumps({"request": request, "response": response}).encode()
    (directory / (call_id + ".json")).write_bytes(raw)
    world.store.append_record(
        child_id,
        "model_capture",
        call_id=call_id,
        request_hash=request_hash,
        capture_ref="model-io/" + call_id + ".json",
        capture_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
        response_hash="sha256:" + hashlib.sha256(response.encode()).hexdigest(),
    )
    world.store.append_record(child_id, "seal")
    world.store.update_run(child_id, state="sealed")
    return {
        "judgment": result,
        "measurement": {
            "text": text,
            "measurement_run_id": child_id,
            "spent": world.store.spend(child_id),
        },
        "judge_id": judge,
        "judge_family": "google",
        "prompt_id": "fixture-pairwise@1",
        "column": "J",
    }


def pair(world, left, right, *, winner="left", style=True, swap=True):
    participants = {"left": participant(world, left), "right": participant(world, right)}
    a = (world.store.run_dir(left) / "work-snapshot/artifact.txt").read_text()
    b = (world.store.run_dir(right) / "work-snapshot/artifact.txt").read_text()
    length = min(len(a), len(b))
    judgments = [
        judgment(
            world,
            left,
            {
                "artifact_A": a[:length],
                "artifact_B": b[:length],
                "criterion": "Support and completeness",
            },
            "tie" if winner == "tie" else "B" if winner == "right" else "A",
            style=style,
        ),
        judgment(
            world,
            left,
            {
                "artifact_A": b[:length] if swap else a[:length],
                "artifact_B": a[:length] if swap else b[:length],
                "criterion": "Support and completeness",
            },
            "tie" if winner == "tie" else "A" if winner in {"right", "disagreement"} else "B",
            style=style,
        ),
    ]
    value = {
        "winner": winner,
        "position_swap": True,
        "length_control": {"method": "equal_character_prefix", "chars": length},
        "judgments": judgments,
        "participants": participants,
        "style_control": {
            "method": "judge_instruction",
            "instruction": STYLE_CONTROL_INSTRUCTION,
            "hash": content_hash(STYLE_CONTROL_INSTRUCTION),
        },
    }
    manifest = stages(world, left, {"pairwise": value})
    stage = manifest["stages"]["pairwise"]
    return {"run_id": left, "path": stage["artifact"], "hash": stage["hash"]}


def test_f7_balanced_actual_report_signature_passes_when_models_repeat(world):
    ids = [
        source(world, model, seed, actions)
        for model, actions in [("gpt-fixture", 2), ("claude-fixture", 12)]
        for seed in [1, 2, 3]
    ]
    report = build_signature_icc(world, ids)
    assert report["informativeness_gate"]["passed"] and report["informativeness_gate"]["icc"] == 1
    assert report["strata"][0]["features"]["model_calls"]["status"] == "uninformative_constant"
    assert all(row["source_record_head"] and row["report_hash"] for row in report["sources"])


def test_f7_noise_negative_icc_is_retained_and_threshold_is_strict(world):
    ids = [
        source(world, model, seed, actions)
        for model, readings in [("gpt-fixture", [1, 10]), ("claude-fixture", [2, 11])]
        for seed, actions in enumerate(readings)
    ]
    report = build_signature_icc(world, ids)
    assert report["informativeness_gate"]["passed"] is False
    assert report["strata"][0]["vector_icc"] < 0
    assert _icc(3, 1, 2) == 0.5


def test_f7_unbalanced_duplicate_and_other_conditions_are_not_pooled(world):
    ids = [
        source(world, "gpt-fixture", 1, 1),
        source(world, "gpt-fixture", 2, 1),
        source(world, "claude-fixture", 1, 8),
    ]
    assert build_signature_icc(world, ids)["informativeness_gate"]["status"] == "unmeasured"
    ids.append(source(world, "claude-fixture", 2, 8, learning="workspace"))
    report = build_signature_icc(world, ids)
    assert len(report["strata"]) == 2 and report["informativeness_gate"]["status"] == "unmeasured"
    ids.append(source(world, "claude-fixture", 2, 8, family="F10"))
    assert build_signature_icc(world, ids)["excluded"][0]["family"] == "F10"


@pytest.mark.parametrize("winner", ["left", "tie", "disagreement"])
def test_verified_swapped_style_controlled_pairwise_elo(world, winner):
    left, right = (
        source(world, "gpt-fixture", 1, 2, family="F4"),
        source(world, "claude-fixture", 1, 3, family="F4"),
    )
    descriptor = pair(world, left, right, winner=winner)
    report = build_pairwise_elo(world, [descriptor])
    assert report["excluded"] == []
    group = report["strata"][0]
    if winner == "disagreement":
        assert report["status"] == "unmeasured"
        assert all(item["style_elo"] is None for item in group["rankings"])
        assert group["matches"][0]["rating_applied"] is False
    else:
        assert report["column"] == "J" and report["status"] == "measured"
        assert sorted(item["style_elo"] for item in group["rankings"]) == (
            [1484, 1516] if winner == "left" else [1500, 1500]
        )
        assert len(group["matches"][0]["receipts"]) == 2


def test_pairwise_rejects_uncontrolled_actual_prompt_even_with_claimed_metadata(world):
    left, right = (
        source(world, "gpt-fixture", 1, 2, family="F4"),
        source(world, "claude-fixture", 1, 3, family="F4"),
    )
    report = build_pairwise_elo(world, [pair(world, left, right, style=False)])
    assert report["status"] == "unmeasured" and "style-control" in report["excluded"][0]["reason"]


def test_failed_f5_core_cannot_be_upgraded_by_style_elo(world):
    left, right = (
        source(world, "gpt-fixture", 1, 2, family="F5", core=True),
        source(world, "claude-fixture", 1, 3, family="F5", core=False),
    )
    report = build_pairwise_elo(world, [pair(world, left, right, winner="right")])
    failed = next(item for item in report["strata"][0]["rankings"] if item["run_id"] == right)
    assert failed["bracket"] == "bottom" and failed["style_elo"] is None
    assert report["strata"][0]["matches"][0]["unmeasured_reason"] == "programmatic_core_bottom_cap"


def test_pairwise_tamper_snapshot_and_reused_receipt_are_not_scored(world):
    left, right = (
        source(world, "gpt-fixture", 1, 2, family="F4"),
        source(world, "claude-fixture", 1, 3, family="F4"),
    )
    descriptor = pair(world, left, right)
    report = build_pairwise_elo(world, [descriptor, descriptor])
    assert (
        len(report["excluded"]) == 1
        and report["excluded"][0]["reason"] == "duplicate pairwise evidence"
    )
    (world.store.run_dir(right) / "work-snapshot/artifact.txt").write_text("Tampered")
    report = build_pairwise_elo(world, [descriptor])
    assert report["status"] == "unmeasured" and "snapshot" in report["excluded"][0]["reason"]


def test_f7_duplicate_seed_and_missing_signature_components_remain_unmeasured(world):
    ids = [
        source(world, model, seed, count)
        for model, count in [("gpt-fixture", 2), ("claude-fixture", 8)]
        for seed in [1, 2]
    ]
    duplicate = source(world, "gpt-fixture", 1, 2)
    assert (
        "duplicate model/seed"
        in build_signature_icc(world, [*ids, duplicate])["strata"][0]["reason"]
    )
    real_report = world.report

    def incomplete(run_id):
        report = real_report(run_id)
        if run_id == ids[0]:
            report["activity"]["world_actions"] = None
        return report

    world.report = incomplete
    report = build_signature_icc(world, ids)
    assert report["informativeness_gate"]["status"] == "unmeasured"
    assert report["strata"][0]["features"]["world_actions"]["status"] == "unmeasured"


def test_pairwise_checks_real_swap_and_judge_family_not_declared_flags(world):
    left, right = (
        source(world, "gpt-fixture", 1, 2, family="F4"),
        source(world, "claude-fixture", 1, 3, family="F4"),
    )
    report = build_pairwise_elo(world, [pair(world, left, right, swap=False)])
    assert "position swap/prefix" in report["excluded"][0]["reason"]
    same_family = source(world, "gemini-fixture-agent", 1, 4, family="F4")
    report = build_pairwise_elo(world, [pair(world, left, same_family)])
    assert "another actual model family" in report["excluded"][0]["reason"]


def test_pairwise_cannot_forge_verdict_from_a_valid_child_receipt(world):
    left, right = (
        source(world, "gpt-fixture", 1, 2, family="F4"),
        source(world, "claude-fixture", 1, 3, family="F4"),
    )
    pair(world, left, right)
    root = world.store.run_dir(left)
    value = json.loads((root / "analysis/pairwise.json").read_text())
    value["judgments"][0]["judgment"]["winner"] = "B"
    value["judgments"][0]["measurement"]["text"] = json.dumps(value["judgments"][0]["judgment"])
    manifest = stages(world, left, {"pairwise": value})
    stage = manifest["stages"]["pairwise"]
    report = build_pairwise_elo(
        world, [{"run_id": left, "path": stage["artifact"], "hash": stage["hash"]}]
    )
    assert "actual captured model response" in report["excluded"][0]["reason"]
