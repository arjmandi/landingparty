"""Saved classifier files must agree with actual metered captures and expert keys."""

# ruff: noqa: F811 -- pytest fixture registration imports intentionally match parameters

import copy
import json

import pytest

from analysis.allocation import AllocationRunner
from analysis.classifier_evidence import verify_saved_allocation
from analysis.pipeline import PostRunPipeline
from tests.test_allocation import calibration_rows, instrument, measured, options  # noqa: F401
from tests.test_rubric import policy
from worldd.models import RunConfig
from worldd.util import digest


@pytest.fixture
async def scored(measured):
    world, _source, model, requests, modes = measured
    world.model_families = {
        "fixture-source-fixed": "fixture-agent",
        "allocation-judge": "fixture-judge",
    }
    source = world.create_run(
        RunConfig(model_snapshot_id="fixture-source-fixed", goal="Build weather catalogue")
    )["run_id"]
    for index in range(46):
        await world.act(
            source,
            "fetch",
            {
                "url": "https://example.com/"
                + ("work-" if index % 2 == 0 else "fiction-")
                + str(index)
            },
        )
        world.store.debit(source, "model", 0.01)
    await world.end_run(source)
    yield world, source, model, requests, modes


async def test_saved_calibration_is_recomputed_and_forged_labels_are_rejected(scored):
    world, source, model, _requests, _modes = scored
    rows = calibration_rows(world, source)
    payload = await AllocationRunner(world, model).run(
        source, **options(), calibration_rows=rows, instrument_ref=instrument(world, source, rows)
    )
    payload["calibration"]["play"]["kappa"] = -1
    payload["metrics"]["experience"]["play"]["rate"] = 99
    verified = verify_saved_allocation(world, source, payload)
    assert verified["calibration"]["play"]["kappa"] == 1
    assert verified["metrics"]["experience"]["play"]["rate"] == 0.5
    forged = copy.deepcopy(payload)
    forged["calibration_judgments"][0]["reading"]["instrumental"] = False
    with pytest.raises(ValueError, match="captured provider response"):
        verify_saved_allocation(world, source, forged)
    forged = copy.deepcopy(payload)
    forged["segments"][0]["measurement_run_id"] = forged["calibration_judgments"][0][
        "measurement_run_id"
    ]
    with pytest.raises(Exception, match="capture|receipt"):
        verify_saved_allocation(world, source, forged)


async def test_actual_saved_allocation_drives_life_experience_and_raw_kappa_cannot(scored):
    world, pilot, model, _requests, _modes = scored
    lives = world.services["lives"]
    life_id = lives.create({"stage": 1, "binding_id": "fixture-content"})["life_id"]
    source = world.create_run(
        RunConfig(
            stage=1,
            life_id=life_id,
            model_snapshot_id="fixture-source-fixed",
            goal="Build weather catalogue",
        )
    )["run_id"]
    for index in range(6):
        await world.act(source, "fetch", {"url": f"https://example.com/fiction-life-{index}"})
    await world.end_run(source)
    root = world.store.run_dir(source)

    def freeze(name, payload):
        path = root / name
        path.write_text(json.dumps(payload))
        return {"path": name, "hash": digest(path.read_bytes())}

    rows = calibration_rows(world, pilot)
    descriptor = instrument(world, source, rows)
    pipeline = world.services["pipeline"] = PostRunPipeline(world, model)
    result = await pipeline.run(
        source,
        {
            "agent_family": "fixture-agent",
            "measurement_budget_usd": 10,
            "allocation": {
                "instrument": descriptor,
                **{k: v for k, v in options().items() if k not in {"classes", "agent_family"}},
            },
        },
    )
    assert result["stages"]["allocation"]["status"] == "completed"
    binding = freeze(
        "binding.json",
        {
            "binding_id": "fixture-content",
            "classifier_mappings": [
                {"parameter": "experience", "label": "play", "evidence_class": "uptake"}
            ],
        },
    )
    config = {
        "source_run_id": source,
        "binding_artifact": binding,
        "policy_artifact": freeze("policy.json", policy()),
    }
    with pytest.raises((ValueError, KeyError), match="source|instrument"):
        pipeline.configure_life(
            life_id,
            **config,
            calibration_artifact=freeze("fake-calibration.json", {"kappa": 1, "miss_rate": 0}),
        )
    pipeline.configure_life(life_id, **config)
    await lives.end(life_id)
    before = world.store.records(source)
    report = (await pipeline.life(life_id))["report"]
    assert report["rubric"]["experience"]["V"]["value"] == 1
    assert report["rubric"]["experience"]["V"]["ceiling"] == 5
    assert report["rubric"]["experience"]["G"]["status"] == "unmeasured"
    assert report["provenance"]["judge_calibration"]["receipts_verified"] is True
    assert len(report["provenance"]["classifiers"][0]["measurement_run_ids"]) == 41
    publication = world.life_report(life_id)["publication"]
    curve = next(
        row for row in publication["curves"] if row["name"] == "allocation_non_instrumental"
    )
    assert curve["column"] == "V" and curve["points"][0]["value"] == 1
    assert all(axis in curve["points"][0] for axis in ("usd", "world_actions", "calendar_days"))
    assert world.store.records(source) == before
