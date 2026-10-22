"""Actual metered fixture calls for the post-seal horizon instrument."""

import json

import httpx
import pytest

from analysis.horizon import METRICS, HorizonRunner
from analysis.judges import MeteredResearchClient, ResearchCallError
from worldd.app import create_app
from worldd.models import RunConfig


class FixtureGateway:
    async def fetch(self, run_id, action_id, **params):
        return {"kind": "result", "url": params["url"], "hash": params["url"]}

    async def aclose(self):
        pass


@pytest.fixture
async def measured(tmp_path):
    requests = []
    mode = {"unknown_ref": False}

    def provider(request):
        body = json.loads(request.content)
        data = json.loads(body["messages"][-1]["content"])
        requests.append(data)
        flagged = data["records"][0].get("text") == "flagged fixture"
        refs = [data["records"][0]["observation_ref"]] if flagged else []
        if mode["unknown_ref"]:
            flagged, refs = True, ["record:999999"]
        judgment = {
            "labels": {key: flagged for key in METRICS},
            "evidence": {key: refs for key in METRICS},
            "reason": "Local fixture reading",
        }
        return httpx.Response(
            200,
            json={
                "model": "horizon-judge",
                "choices": [{"message": {"role": "assistant", "content": json.dumps(judgment)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )

    prices = {
        "models": {
            "horizon-judge": {
                "provider": "openai",
                "input": 1,
                "output": 1,
                "context_window": 128000,
                "max_output_tokens": 16000,
            }
        }
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        app = create_app(tmp_path, gateway=FixtureGateway(), lane_client=client, prices=prices)
        world = app.state.world
        run_id = world.create_run(RunConfig())["run_id"]
        for i in range(26):
            assert (await world.act(run_id, "fetch", {"url": f"https://example.com/{i}"}))[
                "kind"
            ] == "result"
        await world.end_run(run_id)
        model = MeteredResearchClient.for_app(world, app)
        yield world, run_id, model, requests, mode
        await model.aclose()
        await world.aclose()


def options():
    return {
        "model_spec": {"model": "horizon-judge", "family": "other-family"},
        "agent_family": "agent-family",
        "budget_usd": 10,
        "max_input_chars": 300000,
    }


async def test_horizon_runs_two_fresh_metered_prefixes_without_reopening_source(measured):
    world, source, model, requests, _ = measured
    before = world.store.records(source)
    result = await HorizonRunner(world, model).run(source, **options())
    assert len(requests) == 2
    assert [row["segment_first_action"] for row in requests] == [1, 26]
    assert [row["segment_last_action"] for row in requests] == [25, 26]
    assert all(row["column"] == "J" for row in result["metrics"].values())
    assert result["spent_usd"] > 0
    assert world.store.records(source) == before and world.store.spend(source) == {}
    children = [world.store.get_run(row["measurement_run_id"]) for row in result["segments"]]
    assert len({row["run_id"] for row in children}) == 2
    assert all(
        row["state"] == "sealed" and row["config"]["measurement_of"] == source for row in children
    )


async def test_horizon_derives_calibration_from_forty_actual_fixture_judgments(measured):
    world, source, model, requests, _ = measured
    calibration = [
        {
            "input": {
                "records": [
                    {
                        "observation_ref": f"expert:{i}",
                        "text": "flagged fixture" if i % 2 else "clear fixture",
                    }
                ]
            },
            "labels": {key: bool(i % 2) for key in METRICS},
        }
        for i in range(40)
    ]
    result = await HorizonRunner(world, model).run(
        source, **options(), calibration_rows=calibration
    )
    assert len(requests) == 42 and len(result["calibration_judgments"]) == 40
    assert all(
        row["column"] == "V" and row["calibration"]["kappa"] == 1
        for row in result["metrics"].values()
    )
    assert result["scientific_gate_evidence"] is False


async def test_horizon_rejects_fabricated_references_and_validates_size_before_calls(measured):
    world, source, model, requests, mode = measured
    with pytest.raises(ValueError, match="input cap"):
        await HorizonRunner(world, model).run(source, **{**options(), "max_input_chars": 1000})
    assert requests == []
    mode["unknown_ref"] = True
    with pytest.raises(ResearchCallError, match="world evidence"):
        await HorizonRunner(world, model).run(source, **options())
    assert len(requests) == 1
    assert world.store.verify_record(source)["valid"]


async def test_horizon_refuses_same_family_before_purchasing_a_judgment(measured):
    world, source, model, requests, _ = measured
    with pytest.raises(ValueError, match="different model family"):
        await HorizonRunner(world, model).run(
            source, **{**options(), "agent_family": "other-family"}
        )
    assert not requests
