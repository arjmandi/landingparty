import hashlib
import json

import httpx
import pytest

from analysis.pipeline import PostRunPipeline
from analysis.programme import PilotReadiness, calibrate_breakers, metering_audit
from worldd.app import create_app
from worldd.models import RunConfig


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def metered(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_OPENAI_API_KEY", "fixture-only")
    prices = {
        "models": {
            "fixture-model": {
                "provider": "openai",
                "input": "2",
                "output": "4",
                "context_window": 1000,
                "max_output_tokens": 100,
            }
        }
    }

    def provider(request):
        return httpx.Response(
            200,
            json={
                "model": "fixture-model",
                "choices": [{"message": {"role": "assistant", "content": "fixture"}}],
                "usage": {"prompt_tokens": 17, "completion_tokens": 11},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        app = create_app(tmp_path, gateway=Gateway(), lane_client=upstream, prices=prices)
        world = app.state.world
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://world"
        ) as client:
            yield world, client, prices
        await world.aclose()


async def call(client, run_id):
    response = await client.post(
        "/openai/v1/chat/completions",
        headers={"X-World-Run-Id": run_id},
        json={
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "fixture"}],
            "max_tokens": 20,
        },
    )
    assert response.status_code == 200, response.text


async def test_actual_provider_usage_repriced_against_sealed_capture_and_nanoledger(metered):
    world, client, prices = metered
    run_id = world.create_run(RunConfig(capture_model_io=True, model_snapshot_id="fixture-model"))[
        "run_id"
    ]
    await call(client, run_id)
    await world.end_run(run_id)
    result = metering_audit(world.store, run_id, prices)
    assert result["passed"] and result["n_calls"] == 1
    assert result["calls"][0]["provider_usage"]["input"] == 17
    assert result["calls"][0]["ledger_nano"] == 78000
    capture = next((world.store.run_dir(run_id) / "model-io").glob("*.json"))
    capture.write_text(capture.read_text().replace("17", "18"))
    tampered = metering_audit(world.store, run_id, prices)
    assert not tampered["passed"]
    assert "sealed anchor" in tampered["calls"][0]["reason"]


async def test_capture_off_or_other_prices_cannot_claim_provider_agreement(metered):
    world, client, prices = metered
    run_id = world.create_run(RunConfig(capture_model_io=False, model_snapshot_id="fixture-model"))[
        "run_id"
    ]
    await call(client, run_id)
    await world.end_run(run_id)
    assert not metering_audit(world.store, run_id, prices)["passed"]
    assert not metering_audit(world.store, run_id, {"models": {}})["passed"]


def test_breaker_calibration_uses_actual_log_only_outcomes_and_never_changes_policy():
    run = {"run_id": "r_example", "config": {"breaker_policy": {"mode": "log_only"}}}
    events = []
    for index in range(8):
        events.extend(
            [
                {"kind": "action", "action_id": str(index), "tool": "wait", "params_hash": "same"},
                {"kind": "outcome", "action_id": str(index), "reply": {"kind": "waited"}},
            ]
        )
    result = calibrate_breakers([(run, events)])
    assert result["status"] == "measured"
    assert result["thresholds_proposed"] == {
        "yellow_identical": 8,
        "red_identical": 9,
        "no_new_content_actions": 9,
        "dollars_per_minute": 0.000000001,
    }
    assert not result["applied"] and run["config"]["breaker_policy"] == {"mode": "log_only"}
    run["config"]["breaker_policy"]["mode"] = "enforce"
    assert calibrate_breakers([(run, events)])["status"] == "unmeasured"


async def test_actual_started_pilot_retains_missing_evidence_and_never_unlocks(metered):
    world, client, prices = metered
    run_ids, attrition, cells = [], [], []
    for baseline, seeds in (("B2", range(5)), ("B1", range(5)), ("B0", [0]), ("B3", [0])):
        cells.append({"cell_id": baseline, "baseline": baseline})
        for seed in seeds:
            run_id = world.create_run(
                RunConfig(
                    seed=seed,
                    capture_model_io=True,
                    model_snapshot_id="fixture-model",
                    breaker_policy={"mode": "log_only"},
                )
            )["run_id"]
            if baseline != "B3":
                await call(client, run_id)
            # One actual semantic action ensures the log-only calibrator has data.
            world.store.append_record(
                run_id, "action", action_id="a_fixture", tool="wait", params_hash="p"
            )
            world.store.append_record(
                run_id, "outcome", action_id="a_fixture", reply={"kind": "waited"}
            )
            await world.end_run(run_id)
            await PostRunPipeline(world).run(run_id)
            run_ids.append(run_id)
            attrition.append({"run_id": run_id, "cell": baseline, "seed": seed})
    study_id = "study_fixture_readiness"
    world.services["studies"]._save(
        {
            "study_id": study_id,
            "kind": "shakedown",
            "state": "completed",
            "prereg_hash": "fixture-no-scientific-authority",
            "prereg": {
                "schedule": [
                    {
                        "baseline": row["baseline"],
                        "seeds": [
                            item["seed"] for item in attrition if item["cell"] == row["cell_id"]
                        ],
                    }
                    for row in cells
                ]
            },
            "run_ids": run_ids,
            "cells": cells,
            "result": {"attrition": attrition},
        }
    )
    root = world.store.run_dir(run_ids[0])
    (root / "prices.json").write_text(json.dumps(prices))
    descriptor = {
        "path": "prices.json",
        "hash": "sha256:" + hashlib.sha256((root / "prices.json").read_bytes()).hexdigest(),
    }
    before = {run_id: world.store.records(run_id) for run_id in run_ids}
    result = PilotReadiness(world).evaluate(study_id, price_table_artifact=descriptor)
    assert result["metrics"]["metering_matches_provider"]
    assert result["metrics"]["registered_schedule"] and result["metrics"]["breaker_calibrated"]
    assert len(result["attrition"]) == len(result["dollars_to_actions_by_harness"]) == 12
    assert (
        not result["completed"]
        and not result["scientific"]
        and not result["scientific_gate_passed"]
    )
    assert "probe_bank_registered" in result["gate_failures"]
    assert "block_profile" in result["gate_failures"]
    assert "completeness_verdicts" in result["gate_failures"]
    saved = world.store.data_dir / result["artifact_ref"]
    assert "sha256:" + hashlib.sha256(saved.read_bytes()).hexdigest() == result["artifact_hash"]
    assert before == {run_id: world.store.records(run_id) for run_id in run_ids}
