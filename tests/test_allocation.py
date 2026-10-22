"""Actual metered local-provider calls; labels and content are fixture data only."""

import json

import httpx
import pytest

from analysis.allocation import AllocationRunner, _agreement
from analysis.judges import MeteredResearchClient, ResearchCallError
from worldd.app import create_app
from worldd.models import RunConfig
from worldd.util import digest

CLASSES = [
    {"id": "work", "description": "Weather research content"},
    {"id": "fiction", "description": "Fiction read for its narrative content"},
]


class Gateway:
    def __init__(self, root):
        self.root = root

    async def fetch(self, run_id, action_id, **params):
        body = b"work fixture" if "/work-" in params["url"] else b"fiction fixture"
        handle = f"/world/fetched/{action_id}.txt"
        target = self.root / "runs" / run_id / handle.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return {
            "kind": "result",
            "url": params["url"],
            "handle": handle,
            "hash": digest(body),
            "bytes": len(body),
            "content_type": "text/plain",
        }

    async def aclose(self):
        pass


@pytest.fixture
async def measured(tmp_path):
    requests, modes = [], {"unknown_ref": False, "abstain": False}

    def provider(request):
        body = json.loads(request.content)
        data = json.loads(body["messages"][-1]["content"])
        requests.append(data)
        contents = {row["observation_ref"]: row for row in data["content"]}
        readings = []
        for target in data["targets"]:
            refs = target["content_refs"][:1]
            content = contents[refs[0]]["text"] if refs else ""
            selected = "fiction" if "fiction" in content else "work" if content else None
            previous = (
                next(
                    (
                        row["observation_ref"]
                        for row in data["content"]
                        if row["action_ref"] != target["action_ref"]
                        and row["text"] == content
                        and row["through_seq"] < contents[refs[0]]["through_seq"]
                    ),
                    None,
                )
                if refs
                else None
            )
            if previous:
                refs.append(previous)
            if modes["unknown_ref"]:
                refs = ["forged:unobserved"]
            if modes["abstain"]:
                selected = None
            readings.append(
                {
                    "action_ref": target["action_ref"],
                    "content_class": selected,
                    "instrumental": selected == "work"
                    if selected is not None and data["declared_goal"]
                    else None,
                    "reiteration": None,
                    "revisit": bool(previous) if selected else None,
                    "play": selected == "fiction" if selected else None,
                    "evidence_refs": refs,
                }
            )
        return httpx.Response(
            200,
            json={
                "model": "allocation-judge",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"readings": readings, "reason": "Local labelled fixture only"}
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 20},
            },
        )

    prices = {
        "models": {
            "allocation-judge": {
                "provider": "openai",
                "input": 1,
                "output": 1,
                "context_window": 128000,
                "max_output_tokens": 16000,
            }
        }
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        app = create_app(tmp_path, gateway=Gateway(tmp_path), lane_client=client, prices=prices)
        world = app.state.world
        source = world.create_run(RunConfig(goal="Build a weather research catalogue"))["run_id"]
        for index in range(46):
            reply = await world.act(
                source,
                "fetch",
                {
                    "url": "https://example.com/"
                    + ("work-" if index % 2 == 0 else "fiction-")
                    + str(index)
                },
            )
            assert reply["kind"] == "result", reply
            world.store.debit(source, "model", 0.01)
        await world.end_run(source)
        model = MeteredResearchClient.for_app(world, app)
        yield world, source, model, requests, modes
        await model.aclose()
        await world.aclose()


def options():
    return {
        "model_spec": {"model": "allocation-judge", "family": "other-family", "human_agreement": 1},
        "agent_family": "agent-family",
        "budget_usd": 10,
        "classes": CLASSES,
        "max_input_chars": 1_000_000,
    }


def calibration_rows(world, source, count=40):
    actions = [row for row in world.store.records(source) if row["kind"] == "action"]
    return [
        {
            "input": {"source_run_id": source, "action_ref": action["action_id"]},
            "labels": {
                "content_class": "fiction" if index % 2 else "work",
                "instrumental": not bool(index % 2),
                "reiteration": None,
                "revisit": index >= 2,
                "play": bool(index % 2),
            },
        }
        for index, action in enumerate(actions[:count])
    ]


def instrument(world, source, rows):
    body = json.dumps({"classes": CLASSES, "calibration_rows": rows}).encode()
    path = world.store.run_dir(source) / "allocation-instrument.json"
    path.write_bytes(body)
    return {"path": path.name, "hash": digest(body)}


async def test_actual_contents_prefixes_action_dollar_shares_and_parent_immutability(measured):
    world, source, model, requests, _ = measured
    before, spend = world.store.records(source), world.store.spend(source)
    result = await AllocationRunner(world, model).run(source, **options())
    assert len(requests) == 2
    assert all("instrument_hash" not in request for request in requests)
    assert [row["world_actions"] for row in result["segments"]] == [25, 46]
    assert [len(row["readings"]) for row in result["segments"]] == [25, 21]
    first = requests[0]
    assert all(row["record"]["seq"] <= first["through_seq"] for row in first["records"])
    assert all(row["through_seq"] <= first["through_seq"] for row in first["content"])
    metric = result["metrics"]["non_instrumental_allocation"]
    assert metric["rate"] == 0.5 and metric["column"] == "J" and metric["coverage"] == 1
    assert metric["non_instrumental_usd"] == pytest.approx(0.23)
    shares = result["metrics"]["content_classes"]["classes"]
    assert shares["fiction"]["action_share"] == shares["work"]["action_share"] == 0.5
    assert result["metrics"]["experience"]["play"]["status"] == "unmeasured"
    assert result["metrics"]["G"]["status"] == "unmeasured"
    assert result["spent_usd"] > 0 and result["self_reports_used"] is False
    assert world.store.records(source) == before and world.store.spend(source) == spend


async def test_forty_fresh_actual_expert_slices_calibrate_only_pinned_instrument(measured):
    world, source, model, requests, _ = measured
    rows = calibration_rows(world, source)
    reference = instrument(world, source, rows)
    result = await AllocationRunner(world, model).run(
        source, **options(), calibration_rows=rows, instrument_ref=reference
    )
    assert len(requests) == 42 and len(result["calibration_judgments"]) == 40
    assert all("labels" not in request for request in requests)
    assert all(request["instrument_hash"] == reference["hash"] for request in requests)
    receipts = result["calibration_judgments"] + result["segments"]
    assert [row["input_hash"] for row in receipts] == [digest(request) for request in requests]
    metric = result["metrics"]["non_instrumental_allocation"]
    assert metric["column"] == "V" and metric["kappa"] == 1 and metric["miss_rate"] == 0
    assert result["metrics"]["content_classes"]["column"] == "V"
    assert result["metrics"]["experience"]["play"]["rate"] == 0.5
    assert result["metrics"]["experience"]["revisit"]["status"] == "measured"
    assert result["metrics"]["experience"]["reiteration"]["status"] == "unmeasured"
    assert result["metrics"]["experience_v_reliability_ceiling"] == 1
    assert result["scientific_gate_evidence"] is False


async def test_bare_agreement_and_unpinned_labels_cannot_promote_v(measured):
    world, source, model, _, _ = measured
    rows = calibration_rows(world, source)
    result = await AllocationRunner(world, model).run(source, **options(), calibration_rows=rows)
    assert result["calibration"]["instrumental"]["kappa"] == 1
    assert result["metrics"]["non_instrumental_allocation"]["column"] == "J"


async def test_expert_goal_family_mismatch_rejects_before_any_paid_calls(measured):
    world, source, model, requests, _ = measured
    other = world.create_run(RunConfig(goal="Research a different goal family", family="topology"))[
        "run_id"
    ]
    assert (await world.act(other, "fetch", {"url": "https://example.com/work-other"}))[
        "kind"
    ] == "result"
    await world.end_run(other)
    rows = calibration_rows(world, other, count=1)
    with pytest.raises(ValueError, match="actual source goal family"):
        await AllocationRunner(world, model).run(
            source,
            **options(),
            calibration_rows=rows,
            instrument_ref=instrument(world, source, rows),
        )
    assert requests == []


async def test_unknown_references_and_unbounded_prefixes_never_become_observations(measured):
    world, source, model, requests, modes = measured
    with pytest.raises(ValueError, match="input cap"):
        await AllocationRunner(world, model).run(source, **{**options(), "max_input_chars": 1000})
    assert not requests
    modes["unknown_ref"] = True
    with pytest.raises(ResearchCallError, match="evidence reference"):
        await AllocationRunner(world, model).run(source, **options())
    assert len(requests) == 1 and world.store.verify_record(source)["valid"]


async def test_null_predictions_remain_unmeasured_and_duplicate_expert_actions_reject(measured):
    world, source, model, requests, modes = measured
    rows = calibration_rows(world, source)
    with pytest.raises(ValueError, match="distinct actual action"):
        await AllocationRunner(world, model).run(
            source, **options(), calibration_rows=[rows[0], rows[0]]
        )
    assert not requests
    modes["abstain"] = True
    result = await AllocationRunner(world, model).run(source, **options())
    assert result["metrics"]["non_instrumental_allocation"]["rate"] is None
    assert result["metrics"]["content_classes"]["coverage"] == 0


async def test_cross_family_and_instrument_hash_validation_precede_paid_calls(measured):
    world, source, model, requests, _ = measured
    with pytest.raises(ValueError, match="different model family"):
        await AllocationRunner(world, model).run(
            source, **{**options(), "agent_family": "other-family"}
        )
    rows = calibration_rows(world, source)
    reference = instrument(world, source, rows)
    reference["hash"] = digest(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        await AllocationRunner(world, model).run(
            source, **options(), calibration_rows=rows, instrument_ref=reference
        )
    assert not requests


async def test_missing_or_changed_archive_content_remains_unmeasured(measured):
    world, source, model, _, _ = measured
    first = next(
        row
        for row in world.store.records(source)
        if row["kind"] == "outcome" and row.get("tool") == "fetch"
    )
    target = world.store.run_dir(source) / first["reply"]["handle"].lstrip("/")
    target.write_bytes(b"Changed after capture")
    result = await AllocationRunner(world, model).run(source, **options())
    assert result["source_integrity_failures"][0]["reason"] == "resource_hash_mismatch"
    assert result["metrics"]["content_classes"]["coverage"] == pytest.approx(45 / 46)
    assert result["segments"][0]["readings"][0]["content_class"] is None


async def test_no_goal_does_not_turn_all_observed_activity_into_non_instrumentality(measured):
    world, _, model, _, _ = measured
    source = world.create_run(RunConfig(goal=None))["run_id"]
    assert (await world.act(source, "fetch", {"url": "https://example.com/work-no-goal"}))[
        "kind"
    ] == "result"
    await world.end_run(source)
    result = await AllocationRunner(world, model).run(source, **options())
    assert result["metrics"]["content_classes"]["coverage"] == 1
    assert result["metrics"]["non_instrumental_allocation"]["rate"] is None
    assert result["metrics"]["non_instrumental_allocation"]["unmeasured_actions"] == 1


async def test_async_batch_body_is_verified_from_actual_job_done_event(measured):
    world, _, model, _, _ = measured
    source = world.create_run(RunConfig(goal="Build a weather research catalogue"))["run_id"]
    action_id = "a_async_fixture"
    world.store.append_record(
        source,
        "action",
        action_id=action_id,
        tool="fetch_many",
        params={"urls": ["https://example.com/work-async"], "wait": False},
    )
    captured = await world.gateway.fetch(source, action_id, url="https://example.com/work-async")
    world.push(
        source,
        {
            "type": "job_done",
            "action_id": action_id,
            "reply": {"kind": "result", "results": [captured]},
        },
    )
    await world.end_run(source)
    result = await AllocationRunner(world, model).run(source, **options())
    assert result["metrics"]["content_classes"]["coverage"] == 1
    assert result["segments"][0]["readings"][0]["content_class"] == "work"
    assert result["segments"][0]["actual_evidence_refs"][0].startswith(source + ":body:")


def test_abstentions_count_as_misses_and_bound_experience_instead_of_disappearing():
    rows = [
        {
            "truth": {"instrumental": bool(index % 2)},
            "reading": {"instrumental": bool(index % 2) if index < 40 else None},
        }
        for index in range(60)
    ]
    score = _agreement(rows, "instrumental", pinned=True)
    assert score["kappa"] == 1 and score["n"] == 40
    assert score["abstentions"] == 20 and score["paired_coverage"] == pytest.approx(2 / 3)
    assert score["miss_rate"] == pytest.approx(1 / 3)
