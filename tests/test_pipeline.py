import hashlib
import json

import pytest

from analysis.pipeline import PostRunPipeline
from worldd.models import RunConfig
from worldd.runtime import World
from worldd.storage import Store


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def source(tmp_path):
    world = World(Store(tmp_path), Gateway())
    run_id = world.create_run(RunConfig(packs=["deliver"], model_snapshot_id="model-fixed"))[
        "run_id"
    ]
    root = world.store.run_dir(run_id)
    handle = "/world/fetched/a_captured.html"
    (root / handle.lstrip("/")).write_text("<h1>The measured value is 42.</h1>")
    world.store.append_record(
        run_id,
        "action",
        action_id="a_captured",
        tool="fetch",
        params={"url": "https://example.org/source"},
    )
    world.store.append_record(
        run_id,
        "outcome",
        action_id="a_captured",
        resource={
            "handle": handle,
            "url": "https://example.org/source",
            "final_url": "https://example.org/source",
            "content_type": "text/html",
        },
    )
    claims = [
        {
            "claim": "The value is 42",
            "url": "https://example.org/source",
            "quote": "measured value is 42",
        }
    ]
    (root / "work/out/claims.json").write_text(json.dumps(claims))
    (root / "work/out/faq.md").write_text("A factual FAQ.")
    await world.end_run(run_id)
    yield world, run_id, root
    await world.aclose()


class Model:
    def __init__(self):
        self.calls = []

    async def complete(self, source_run_id, spec, messages, **kwargs):
        self.calls.append((messages, kwargs))
        system = messages[0]["content"]
        value = (
            {"supported": True, "reason": "entailed"}
            if "supports the claim" in system
            else {"response": "post-run recall"}
        )
        return {
            "text": json.dumps(value),
            "spent": {kwargs["lane"]: 0.01},
            "measurement_run_id": f"r_m{len(self.calls)}",
        }

    async def fork(self, source_run_id, spec, messages, **kwargs):
        self.calls.append((messages, kwargs))
        data = json.loads(messages[1]["content"])
        items = data.get("questions", data.get("items", []))
        answers = {item["item_id"]: "a" for item in items}
        return {
            "text": json.dumps(answers),
            "spent": {"probe": 0.01},
            "measurement_run_id": f"r_m{len(self.calls)}",
            "mode": "replay",
            "archive_in": source_run_id,
        }


async def test_offline_pipeline_quotes_and_report_do_not_touch_terminal_record(source):
    world, run_id, root = source
    before = world.store.records(run_id)
    manifest = await PostRunPipeline(world).run(run_id)
    assert manifest["status"] == "completed"
    quote = json.loads((root / "analysis/citation-quotes.json").read_text())
    assert quote["quote_match_rate"] == 1
    assert quote["claim_support"]["status"] == "unmeasured"
    assert world.store.records(run_id) == before
    assert (root / manifest["report_path"]).is_file()
    assert (root / "analysis/trajectory.json").is_file()


async def test_paid_pipeline_requires_explicit_total_budget_and_keeps_judge_column(source):
    world, run_id, root = source
    model = Model()
    options = {
        "judge": {"model": "judge-fixed", "family": "other", "budget_usd": 0.2},
        "agent_family": "agent",
        "semantic_citations": True,
    }
    with pytest.raises(ValueError, match="total measurement budget"):
        await PostRunPipeline(world, model).run(run_id, options)
    options["measurement_budget_usd"] = 0.2
    result = await PostRunPipeline(world, model).run(run_id, options)
    assert result["measurement_costs"]["usd"] == 0.01
    support = json.loads((root / "analysis/citation-support.json").read_text())
    assert support["column"] == "J"
    assert support["items"][0]["judgment"]["supported"]


async def test_two_stage_interview_free_recall_never_receives_record(source):
    world, run_id, root = source
    model = Model()
    result = await PostRunPipeline(world, model).run(
        run_id,
        {
            "interview": {"model": "model-fixed", "family": "agent", "budget_usd": 0.4},
            "measurement_budget_usd": 0.4,
        },
    )
    assert result["status"] == "completed"
    first, second = model.calls
    assert "workspace" in json.loads(first[0][1]["content"])
    assert "days" not in json.loads(first[0][1]["content"])
    assert "days" in json.loads(second[0][1]["content"])
    assert first[1]["lane"] == second[1]["lane"] == "probe"
    interview = json.loads((root / "analysis/interview.json").read_text())
    assert not interview["consistency_scored"]


async def test_probe_contexts_exclude_sources_and_refuse_transcript_in_post_mem(source):
    world, run_id, root = source
    options = {
        "probe_bank": [
            {
                "item_id": "exposed",
                "question": "which?",
                "answer": "a",
                "source_urls": ["https://example.org/source"],
            }
        ],
        "probe_model": {"model": "model-fixed", "family": "agent", "budget_usd": 0.1},
        "measurement_budget_usd": 1,
    }
    result = await PostRunPipeline(world, Model()).run(run_id, options)
    assert result["status"] == "completed"
    probes = json.loads((root / "analysis/probes.json").read_text())
    assert probes["scores"]["excluded_item_ids"] == ["exposed"]
    options["probe_mountings"] = {"post_mem": {"transcript": "must not be supplied"}}
    result = await PostRunPipeline(world, Model()).run(run_id, options)
    assert result["status"] == "partial"
    assert result["stages"]["probes"]["error"] == "ValueError"


async def test_t0_never_calls_fictional_stakes_a_completed_battery(source):
    world, run_id, _root = source
    battery = {
        name: [{"item_id": name, "question": "choose", "choices": ["a", "b"], "stake_usd": 0.1}]
        for name in ("lottery", "leisure", "persona", "attractor")
    }
    result = await PostRunPipeline(world, Model()).t0(
        run_id,
        "l_fixture",
        battery=battery,
        model_spec={"model": "model-fixed", "family": "agent", "budget_usd": 0.1},
        measurement_budget_usd=1,
    )
    assert not result["completed"]
    assert result["lottery"]["status"] == "unmeasured"
    assert result["model_value_document"]["status"] == "unmeasured"
    assert len(result["measurement_run_ids"]) == 4


async def test_t0_requires_exact_real_usd_receipts_for_frozen_choices(source):
    world, run_id, root = source
    battery = {
        name: [{"item_id": name, "question": "choose", "choices": ["a", "b"], "stake_usd": 0.1}]
        for name in ("lottery", "leisure", "persona", "attractor")
    }
    captured = root / "world/fetched/a_captured.html"
    battery["model_value_document"] = {
        "capture_ref": "/world/fetched/a_captured.html",
        "hash": "sha256:" + hashlib.sha256(captured.read_bytes()).hexdigest(),
    }

    def executor(_life_id, _item, _answer):
        return {"settled": True, "rail": "paper", "amount_usd": 0.1, "observation_ref": "paper"}

    result = await PostRunPipeline(world, Model()).t0(
        run_id,
        "l_fixture",
        battery=battery,
        model_spec={"model": "model-fixed", "family": "agent", "budget_usd": 0.1},
        measurement_budget_usd=1,
        stake_executor=executor,
    )
    assert not result["completed"]


async def test_pipeline_rejects_preseal_and_escaping_artifacts(source):
    world, run_id, _root = source
    with pytest.raises(ValueError, match="stay inside"):
        await PostRunPipeline(world).run(run_id, {"claims_path": "../../../../etc/passwd"})
    running = world.create_run(RunConfig())["run_id"]
    with pytest.raises(ValueError, match="sealed"):
        await PostRunPipeline(world).run(running)


async def test_instruments_require_pinned_artifacts_and_resolved_evidence(source):
    world, run_id, root = source
    before = world.store.records(run_id)
    path = root / "instruments.json"
    rows = [
        {"usd": 1, "world_actions": 10, "score": 0.2, "observation_ref": "a_captured"},
        {"usd": 2, "world_actions": 20, "score": 0.8, "observation_ref": "record:1"},
    ]
    path.write_text(json.dumps({"rows": rows}))
    descriptor = {
        "path": "instruments.json",
        "hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    options = {"instrument_artifacts": {"cost_performance": descriptor}}
    manifest = await PostRunPipeline(world).run(run_id, options)
    assert manifest["status"] == "completed"
    result = json.loads((root / "analysis/cost-performance.json").read_text())
    assert result["measurement"]["usd"]["slope_log_cost"] > 0
    assert result["provenance"]["reference_integrity_verified"]
    assert result["provenance"]["scientific_gate_passed"] is False
    assert world.store.records(run_id) == before

    rows[0]["observation_ref"] = "unobserved-fiction"
    path.write_text(json.dumps({"rows": rows}))
    descriptor["hash"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = await PostRunPipeline(world).run(run_id, options)
    assert manifest["status"] == "partial"
    assert manifest["stages"]["cost-performance"]["error"] == "ValueError"

    rows[0]["observation_ref"] = "a_captured"
    path.write_text(json.dumps({"rows": rows}))
    manifest = await PostRunPipeline(world).run(run_id, options)
    assert manifest["status"] == "partial"  # mismatched file hash is never silently accepted


async def test_probe_mounting_uses_actual_context_and_explicit_provenance(source):
    world, run_id, root = source
    model = Model()
    options = {
        "probe_bank": [{"item_id": "i", "question": "which", "answer": "a"}],
        "probe_model": {"model": "model-fixed", "family": "agent", "budget_usd": 0.1},
        "measurement_budget_usd": 1,
    }
    await PostRunPipeline(world, model).run(run_id, options)
    result = json.loads((root / "analysis/probes.json").read_text())
    assert result["contexts"]["post_off"]["status"] == "unmeasured"
    assert result["contexts"]["post_on"]["status"] == "unmeasured"
    assert len(model.calls) == 3
    assert result["mountings"]["pre_off"]["source"] == "source_artifacts"
    options["probe_cells"] = ["post_off"]
    options["probe_mountings"] = {"post_off": {"last_context": {"messages": []}}}
    await PostRunPipeline(world, model).run(run_id, options)
    result = json.loads((root / "analysis/probes.json").read_text())
    assert result["mountings"]["post_off"]["source"] == "researcher_supplied"


async def test_observer_pipeline_measures_pinned_stream_without_model_calls(source):
    world, run_id, root = source

    def artifact(name, value):
        path = root / name
        path.write_bytes(value)
        return {"path": name, "hash": "sha256:" + hashlib.sha256(value).hexdigest()}

    stream = [
        {**artifact(f"observer-stream-{index}.txt", text), "observation_ref": "a_captured"}
        for index, text in enumerate((b"alpha text", b"beta text", b"gamma text"))
    ]
    background = artifact("observer-background.txt", b"background text")
    instrument = {
        "stream": stream,
        "observer_specs": [
            {"order": order, "alpha": 0.5, "background": background} for order in (1, 2)
        ],
        "coordinates": [[0, 2], [0, 3], [1, 2], [1, 3]],
        "exploratory": True,
    }
    descriptor = artifact("observer-config.json", json.dumps(instrument).encode())
    result = await PostRunPipeline(world).run(run_id, {"observer_run": {"instrument": descriptor}})
    assert result["status"] == "completed" and result["measurement_costs"]["usd"] == 0
    report = json.loads((root / "analysis/observer-run.json").read_text())
    assert len(report["rows"]) == 8
    assert report["metrics"]["status"] == "measured"
    assert report["provenance"]["exploratory"]
    assert all(
        not descriptor["path"].startswith("/")
        for descriptor in report["reference_artifacts"].values()
    )
