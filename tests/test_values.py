import hashlib
import json

import httpx
import pytest

from analysis.values import (
    TasteRunner,
    ValueExtractionRunner,
    attribution_screen,
    taste_metrics,
    value_delta,
)
from worldd.app import create_app
from worldd.models import RunConfig


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def research(tmp_path):
    requests = []

    async def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        instruction = payload["messages"][0]["content"]
        data = json.loads(payload["messages"][1]["content"])
        if "Extract revealed value positions" in instruction:
            answer = {
                "positions": {axis["id"]: 1 for axis in data["axes"]},
                "evidence_refs": [data["evidence"][0]["observation_ref"]],
                "reason": "software fixture projection",
            }
        elif "fresh pre-life probe" in instruction:
            answer = {item["item_id"]: "a" for item in data["items"]}
        elif "Rate the artifact" in instruction:
            answer = {
                "score": 0.2 if "low evidence" in data["artifact"] else 0.8,
                "reason": "software fixture critic",
            }
        elif "fresh no-world reception-prediction" in instruction:
            answer = {"p": 0.6}
        elif "task coherence" in instruction:
            answer = {"label": "consistent", "reason": "software fixture coherence"}
        elif "task revision" in instruction:
            answer = {
                "label": "evidence_driven",
                "revision_needed": True,
                "reason": "software fixture revision",
            }
        else:
            answer = {"label": "achieved", "reason": "software fixture"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    price = {
        "provider": "openai",
        "input": "2.5",
        "output": "10",
        "cache_read": "1.25",
        "context_window": 1000,
        "max_output_tokens": 1000,
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        app = create_app(
            tmp_path,
            gateway=Gateway(),
            lane_client=upstream,
            prices={"models": {"agent-fixed": price, "judge-fixed": price}},
        )
        world = app.state.world
        life_id = world.services["lives"].create({"stage": 1, "binding_id": "fixture"})["life_id"]
        started = world.create_run(
            RunConfig(
                stage=1,
                life_id=life_id,
                model_snapshot_id="agent-fixed",
                budget={"usd": 1, "wall_s": 60},
            )
        )
        source = started["run_id"]
        world.store.append_record(
            source, "action", tool="deliver", action_id="a_early", params={"text": "early evidence"}
        )
        world.store.debit(source, "harness", 0.15)
        world.store.append_record(
            source,
            "action",
            tool="deliver",
            action_id="a_late",
            params={"text": "LATE_ONLY_SECRET"},
        )
        world.store.debit(source, "harness", 0.15)
        root = world.store.run_dir(source)
        (root / "work/out/artifact.txt").write_text(
            "An artifact without external reception labels."
        )
        await world.end_run(source)
        try:
            yield world, source, root, requests, life_id
        finally:
            await world.aclose()


def axes():
    return {
        "instrument_id": "software-fixture@1",
        "axes": [
            {"id": "truth", "description": "test axis", "low": "low anchor", "high": "high anchor"}
        ],
    }


async def test_value_windows_are_metered_and_never_receive_future_workspace(research):
    world, source, _root, requests, _life = research
    from analysis.judges import JudgeSpec

    records = world.store.records(source)
    result = await ValueExtractionRunner(
        world,
        world.services["pipeline"].model,
        JudgeSpec(model="judge-fixed", family="judge"),
        agent_family="agent",
        budget_usd=0.01,
        max_tokens=64,
    ).run(source, instrument=axes())
    assert [window["status"] for window in result["windows"]] == [
        "reached",
        "reached",
        "unmeasured",
        "unmeasured",
    ]
    assert result["controls"]["t0"]["status"] == "unmeasured"
    assert result["column"] == "J" and result["calibration"]["kappa"] is None
    assert len(requests) == 2
    assert "LATE_ONLY_SECRET" not in requests[0]["messages"][1]["content"]
    assert "LATE_ONLY_SECRET" in requests[1]["messages"][1]["content"]
    assert world.store.records(source) == records
    children = [
        run for run in world.store.list_runs() if run["config"].get("measurement_of") == source
    ]
    assert len(children) == 2 and all(run["state"] == "sealed" for run in children)
    assert sum(world.store.total_spent(run["run_id"]) for run in children) == pytest.approx(0.0007)


async def test_taste_runner_blinds_real_metered_critic_and_keeps_crowd_separate(research):
    world, source, root, requests, _life = research
    footprint = world.services["footprint"]
    item = footprint.record_item(source, "a_early", kind="post", platform="fixture")
    reading = footprint.add_reading(item["item_id"], 3, author_class="independent_human")
    artifact = root / "work-snapshot/out/artifact.txt"
    result = await TasteRunner(world, world.services["pipeline"].model).run(
        source,
        rows=[
            {
                "item_id": "test-item",
                "t": 1,
                "artifact_path": "work-snapshot/out/artifact.txt",
                "artifact_hash": "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "crowd": {
                    "value": 3,
                    "author_class": "independent_human",
                    "observation_ref": reading["reading_id"],
                },
            }
        ],
        critics=[{"model": "judge-fixed", "family": "judge", "budget_usd": 0.01, "max_tokens": 64}],
        agent_family="agent",
        criterion={"criterion_id": "fixture@1", "description": "Rate factual support"},
    )
    assert result["columns"]["crowd"][0]["value"] == 3
    assert result["columns"]["critics"][0]["value"] == 0.8
    assert result["columns"]["critics"][0]["column"] == "J"
    assert result["columns"]["self_predictions"]["status"] == "unmeasured"
    data = json.loads(requests[0]["messages"][1]["content"])
    assert set(data) == {"artifact", "criterion", "truncated"}
    assert "crowd" not in data and "author" not in data and "item_id" not in data


async def test_registered_t0_document_attractor_and_last_counterparty_controls(research):
    world, anchor, root, _requests, _life = research
    from analysis.judges import JudgeSpec

    life_id = world.services["lives"].create({"stage": 1, "binding_id": "fixture"})["life_id"]
    document = root / "world/fetched/published.txt"
    document.write_text("Software fixture published value document, not research evidence.")
    battery = {
        name: [{"item_id": name, "question": "choose", "choices": ["a", "b"], "stake_usd": 0.1}]
        for name in ("lottery", "leisure", "persona", "attractor")
    }
    battery["model_value_document"] = {
        "capture_ref": "/world/fetched/published.txt",
        "hash": "sha256:" + hashlib.sha256(document.read_bytes()).hexdigest(),
    }
    t0 = await world.services["pipeline"].t0(
        anchor,
        life_id,
        battery=battery,
        model_spec={
            "model": "agent-fixed",
            "family": "agent",
            "budget_usd": 0.01,
            "max_tokens": 64,
        },
        measurement_budget_usd=0.05,
        stake_executor=lambda *_: {
            "settled": True,
            "rail": "real_usd",
            "amount_usd": 0.1,
            "observation_ref": "software-fixture-receipt",
        },
    )
    assert t0["completed"] and not t0["scientific"]
    world.services["lives"].record_t0(life_id, t0)
    source = world.create_run(
        RunConfig(
            stage=1,
            life_id=life_id,
            model_snapshot_id="agent-fixed",
            budget={"usd": 1, "wall_s": 60},
        )
    )["run_id"]
    world.push(
        source,
        {
            "type": "message_received",
            "author_class": "independent_human",
            "body": "A software fixture counterparty statement.",
        },
    )
    world.store.append_record(
        source, "action", action_id="a_source", tool="deliver", params={"text": "A source choice"}
    )
    world.store.debit(source, "harness", 0.15)
    await world.end_run(source)
    result = await ValueExtractionRunner(
        world,
        world.services["pipeline"].model,
        JudgeSpec(model="judge-fixed", family="judge"),
        agent_family="agent",
        budget_usd=0.01,
        max_tokens=64,
    ).run(source, instrument=axes())
    assert all(
        "positions" in result["controls"][name]
        for name in ("t0", "attractor", "model_value_document")
    )
    window = result["windows"][0]
    assert window["last_counterparty"]["positions"] == {"truth": 1}
    assert window["contrasts"]["t0"]["axes"]["truth"] == 0
    assert not result["causal_formation_claim"]


async def test_no_world_reception_copy_uses_agent_snapshot_without_outcomes(research):
    world, source, root, requests, _life = research
    path = root / "work-snapshot/out/artifact.txt"
    before = world.store.records(source)
    result = await TasteRunner(world, world.services["pipeline"].model).run(
        source,
        rows=[
            {
                "item_id": "a",
                "t": 1,
                "artifact_path": "work-snapshot/out/artifact.txt",
                "artifact_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        ],
        agent_family="agent",
        criterion={"criterion_id": "fixture", "description": "Predict independent reception"},
        prediction_model={
            "model": "agent-fixed",
            "family": "agent",
            "budget_usd": 0.01,
            "max_tokens": 64,
        },
    )
    assert result["no_world_copy_predictions"][0]["p"] == 0.6
    assert result["self_prediction_brier_delta"]["status"] == "unmeasured"
    assert requests[0]["model"] == "agent-fixed"
    context = json.loads(requests[0]["messages"][1]["content"])
    assert set(context) == {"artifact", "criterion"}
    assert world.store.records(source) == before


async def test_critic_promotion_requires_actual_forty_item_calibration(research):
    world, source, root, requests, _life = research
    artifact = root / "work-snapshot/out/artifact.txt"
    row = {
        "item_id": "a",
        "t": 1,
        "artifact_path": "work-snapshot/out/artifact.txt",
        "artifact_hash": "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    criterion = {
        "criterion_id": "software-fixture",
        "description": "A frozen test criterion",
        "score_bins": [0, 0.5, 1],
        "calibration": [
            {
                "artifact_text": "low evidence" if index % 2 else "high evidence",
                "expert_score": 0.2 if index % 2 else 0.8,
            }
            for index in range(40)
        ],
    }
    result = await TasteRunner(world, world.services["pipeline"].model).run(
        source,
        rows=[row],
        critics=[{"model": "judge-fixed", "family": "judge", "budget_usd": 0.01, "max_tokens": 64}],
        agent_family="agent",
        criterion=criterion,
    )
    critic = result["columns"]["critics"][0]
    assert critic["calibration"]["n"] == 40 and critic["calibration"]["kappa"] == 1
    assert critic["column"] == "V"
    assert len(critic["calibration"]["measurements"]) == 40
    options = [{"model": "judge-fixed", "family": "judge", "budget_usd": 0.01, "max_tokens": 64}]
    with pytest.raises(ValueError, match="before this life's first session"):
        await TasteRunner(world, world.services["pipeline"].model).run(
            source,
            rows=[],
            critics=options,
            agent_family="agent",
            criterion=criterion,
            panel_calibrations=result["critic_calibrations"],
        )
    life_id = world.services["lives"].create({"stage": 1, "binding_id": "fixture"})["life_id"]
    subsequent = world.create_run(
        RunConfig(stage=1, life_id=life_id, model_snapshot_id="agent-fixed")
    )["run_id"]
    await world.end_run(subsequent)
    before = len(requests)
    reused = await TasteRunner(world, world.services["pipeline"].model).run(
        subsequent,
        rows=[],
        critics=options,
        agent_family="agent",
        criterion=criterion,
        panel_calibrations=result["critic_calibrations"],
    )
    assert reused["critic_calibrations"]["judge-fixed"]["pre_life_reuse_verified"]
    assert len(requests) == before  # Calibration receipts are verified rather than bought again.


async def test_pipeline_values_and_taste_share_explicit_cap_and_pinned_artifacts(research):
    world, source, root, _requests, _life = research

    def artifact(name, value):
        path = root / name
        path.write_text(json.dumps(value))
        return {"path": name, "hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}

    result = await world.services["pipeline"].run(
        source,
        {
            "agent_family": "agent",
            "measurement_budget_usd": 0.1,
            "value_extraction": {
                "model": "judge-fixed",
                "family": "judge",
                "budget_usd": 0.01,
                "max_tokens": 64,
                "instrument": artifact("value-instrument.json", axes()),
            },
            "taste": {"input": artifact("taste-input.json", {"rows": []})},
        },
    )
    assert result["status"] == "completed"
    assert result["measurement_costs"]["usd"] == pytest.approx(0.0007)
    assert (root / "analysis/value-extraction.json").is_file()
    taste = json.loads((root / "analysis/taste.json").read_text())
    assert taste["columns"]["crowd"]["status"] == "unmeasured"


async def test_pipeline_formation_uses_actual_child_billing_and_retains_small_census(research):
    world, source, root, _requests, _life = research
    payload = {
        "beliefs": [
            {"id": "b1", "claim": "A source claim.", "rank": 1},
            {"id": "b2", "claim": "Another source claim.", "rank": 2},
        ],
        "dropped_goals": [
            {
                "id": "g",
                "observation_ref": "a_early",
                "goal": "Write a source artifact",
                "trajectory": "The record contains a deliver action",
            }
        ],
    }
    path = root / "formation.json"
    path.write_text(json.dumps(payload))
    descriptor = {
        "path": path.name,
        "hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    result = await world.services["pipeline"].run(
        source,
        {
            "agent_family": "agent",
            "measurement_budget_usd": 0.1,
            "formation": {
                "model": "judge-fixed",
                "family": "judge",
                "budget_usd": 0.03,
                "max_tokens": 64,
                "input": descriptor,
            },
        },
    )
    assert result["status"] == "completed"
    artifact = json.loads((root / "analysis/formation.json").read_text())
    assert artifact["prose"]["sample"]["census"]
    assert not artifact["prose"]["sample"]["complete_200"]
    assert result["measurement_costs"]["usd"] > 0


def test_taste_preserves_critic_disagreement_and_adjusts_the_crowd_trend():
    rows = [
        {
            "item_id": str(t),
            "t": t,
            "crowd": {
                "value": 1,
                "author_class": "independent_human",
                "observation_ref": f"crowd{t}",
            },
            "critics": [
                {
                    "critic_id": "a",
                    "family": "other",
                    "value": 0.1 + 0.2 * t,
                    "blinded": True,
                    "observation_ref": f"a{t}",
                },
                {
                    "critic_id": "b",
                    "family": "other",
                    "value": 0.8 - 0.2 * t,
                    "blinded": True,
                    "observation_ref": f"b{t}",
                },
            ],
        }
        for t in range(3)
    ]
    result = taste_metrics(rows, agent_family="agent")
    assert result["crowd_adjusted_critic_residuals"]["a"]["residual_time_slope"] == pytest.approx(
        0.2
    )
    assert result["crowd_adjusted_critic_residuals"]["b"]["residual_time_slope"] == pytest.approx(
        -0.2
    )
    assert result["critic_disagreement"]["0"]["range"] == pytest.approx(0.7)
    assert result["single_taste_score"] is False
    with pytest.raises(ValueError, match="another family"):
        taste_metrics(rows, agent_family="other")


def test_taste_brier_control_is_paired_and_sponsor_reception_stays_separate():
    rows = [
        {
            "item_id": str(index),
            "t": index,
            "crowd": {"value": 1, "author_class": "sponsor", "observation_ref": f"sponsor{index}"},
            "self_prediction": {
                "p": 0.8,
                "outcome": True,
                "prediction_ref": f"prediction{index}",
                "outcome_ref": f"outcome{index}",
            },
            "no_world_prediction": {
                "p": 0.5,
                "measurement_run_id": f"r_copy{index}",
                "observation_ref": f"copy{index}",
            },
        }
        for index in range(30)
    ]
    result = taste_metrics(rows, agent_family="agent")
    assert result["columns"]["crowd"]["status"] == "unmeasured"
    assert len(result["columns"]["sponsor"]) == 30
    assert result["self_prediction_brier_delta"]["agent_minus_no_world"] == pytest.approx(-0.21)
    assert (
        taste_metrics(rows[:29], agent_family="agent")["self_prediction_brier_delta"]["status"]
        == "unmeasured"
    )


def test_value_deltas_and_control_exclusion_screen_never_assert_formation():
    before = {"positions": {"a": 0, "b": 1}}
    after = {"positions": {"a": 2, "b": 0}}
    assert value_delta(after, before)["axes"] == {"a": 2, "b": -1}
    screen = attribution_screen(
        after,
        before,
        counterparty={"positions": {"a": 2, "b": -2}},
        attractor={"positions": {"a": 2, "b": 0}},
    )
    assert screen["axes"]["a"]["moves_toward_last_counterparty"]
    assert screen["axes"]["b"]["matches_known_attractor"]
    assert not screen["axes"]["a"]["eligible_for_further_evidence_attribution"]
    assert not screen["axes"]["b"]["formation_evidence"]
