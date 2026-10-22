"""Passive details consume actual receipts and locally metered fixture captures."""

import json
import math

import httpx
import pytest

from analysis.allocation import AllocationRunner
from analysis.judges import MeteredResearchClient
from analysis.metrics import jensen_shannon
from analysis.pipeline import PostRunPipeline
from analysis.signature_details import LEISURE_POLICY_ID, signature_details
from worldd.app import create_app
from worldd.models import RunConfig
from worldd.util import digest

CLASSES = [
    {"id": "work", "description": "Weather research"},
    {"id": "fiction", "description": "Fiction"},
]


class Gateway:
    def __init__(self, root):
        self.root, self.author = root, None

    async def fetch(self, run_id, action_id, **params):
        body = b"fiction fixture" if "fiction" in params["url"] else b"work fixture"
        handle = f"/world/fetched/{action_id}.txt"
        target = self.root / "runs" / run_id / handle.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return {
            "kind": "result",
            "url": params["url"],
            "handle": handle,
            "hash": digest(body),
            "content_type": "text/plain",
            "provenance": {"author_class": self.author} if self.author else {},
        }

    async def aclose(self):
        pass


@pytest.fixture
async def measured(tmp_path):
    requests = []

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        try:
            data = json.loads(body["messages"][-1]["content"])
        except ValueError:
            data = {}
        if data.get("component"):
            answer = {item["item_id"]: item["choices"][0] for item in data["items"]}
        elif data.get("targets"):
            contents = {row["observation_ref"]: row for row in data["content"]}
            answer = {
                "readings": [
                    {
                        "action_ref": row["action_ref"],
                        "content_class": "fiction"
                        if "fiction" in contents[row["content_refs"][0]]["text"]
                        else "work",
                        "instrumental": None,
                        "reiteration": None,
                        "revisit": None,
                        "play": None,
                        "evidence_refs": row["content_refs"][:1],
                    }
                    for row in data["targets"]
                ],
                "reason": "Local content fixture",
            }
        else:
            answer = {"response": "Local agent fixture"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 20},
            },
        )

    prices = {
        "models": {
            name: {
                "provider": "openai",
                "input": 1,
                "output": 1,
                "context_window": 32000,
                "max_output_tokens": 4096,
            }
            for name in ("agent-fixed", "judge-fixed")
        }
    }
    gateway = Gateway(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        app = create_app(tmp_path, gateway=gateway, lane_client=upstream, prices=prices)
        world = app.state.world
        world.model_families = {"agent-fixed": "agent-family", "judge-fixed": "judge-family"}
        model = MeteredResearchClient.for_app(world, app)
        try:
            yield world, app, model, requests, gateway
        finally:
            await model.aclose()
            await world.aclose()


async def test_recorded_action_latency_ticket_inbox_and_explicit_unknown_topics(measured):
    world, app, _, requests, _ = measured
    created = world.create_run(RunConfig(model_snapshot_id="agent-fixed"))
    source = created["run_id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://world"
    ) as client:
        response = await client.post(
            "/openai/v1/chat/completions",
            headers={"X-World-Run-Id": source},
            json={
                "model": "agent-fixed",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 20,
            },
        )
        assert response.status_code == 200
    ticket = await world.act(source, "fetch", {"url": "https://example.com/work", "wait": False})
    await world.jobs[source][ticket["action_id"]]
    event = world.push(source, {"type": "message_received", "body": "Fixture inbound event"})
    await world.inbox(source)
    await world.end_run(source, reason="agent_finished")
    before, spend = world.store.records(source), world.store.spend(source)
    result = signature_details(world, source)
    first = result["time_to_first_world_action"]
    assert first["seconds"] >= 0 and first["ordinary_model_calls"] == 1
    assert first["usd"] == pytest.approx(0.00004)
    assert result["ordinary_model_calls"]["count"] == 1
    assert result["ticket_use"]["exact_receipts"] == 1
    assert result["inbox_use"]["reads"] == 1 and result["inbox_use"]["unique_events_read"] >= 1
    assert event["type"] in result["inbox_use"]["event_types_read"]
    assert result["voluntary_stop"]["value"] is True
    assert result["topics"]["status"] == "unmeasured"
    assert result["collapse"]["workspace"]["retention"]["status"] == "unmeasured"
    assert result["F7_ICC_projection_unchanged"] is True and len(requests) == 1
    assert world.store.records(source) == before and world.store.spend(source) == spend


async def test_workspace_retention_claims_urls_and_explicit_authorship(measured):
    world, _, _, _, gateway = measured
    donor = world.create_run(RunConfig(packs=["deliver"], model_snapshot_id="agent-fixed"))[
        "run_id"
    ]
    reply = await world.act(donor, "fetch", {"url": "https://example.com/work"})
    root = world.store.run_dir(donor)
    (root / "work/out/kept.txt").write_text("Retained fixture item")
    (root / "work/out/removed.txt").write_text("Discarded fixture item")
    await world.end_run(donor)
    child = world.create_run(
        RunConfig(model_snapshot_id="agent-fixed", iota={"workspace": f"import({donor})"})
    )["run_id"]
    root = world.store.run_dir(child)
    (root / "work/out/removed.txt").unlink()
    (root / "work/out/kept.txt").rename(root / "work/out/renamed.txt")
    (root / "work/out/claims.json").write_text(
        json.dumps(
            [
                {"claim": "Verified captured source", "url": reply["url"]},
                {"claim": "https://invented.invalid/source was never fetched"},
            ]
        )
    )
    await world.end_run(child)
    result = signature_details(world, child)["collapse"]["workspace"]
    assert result["files"] == 2
    assert result["retention"]["imported_files"] == 2
    assert result["retention"]["byte_identical_items_retained_including_renames"] == 1
    assert result["retention"]["same_path_and_bytes_retained"] == 0
    assert result["claims"]["with_verified_live_url"] == 1 and result["claims"]["fraction"] == 0.5
    assert result["third_party_authored_fraction"]["status"] == "unmeasured"
    gateway.author = "independent_human"
    copied = world.create_run(RunConfig(model_snapshot_id="agent-fixed"))["run_id"]
    reply = await world.act(copied, "fetch", {"url": "https://example.com/work"})
    (world.store.run_dir(copied) / "work/out/copy.txt").write_bytes(
        (world.store.run_dir(copied) / reply["handle"].lstrip("/")).read_bytes()
    )
    await world.end_run(copied)
    authorship = signature_details(world, copied)["collapse"]["workspace"][
        "third_party_authored_fraction"
    ]
    assert authorship["status"] == "measured" and authorship["fraction"] == 1


async def test_belief_retention_uses_exact_import_boundary_and_actual_live_evidence(measured):
    world, _, _, _, _ = measured
    donor = world.create_run(RunConfig(stage=1, packs=["belief"], model_snapshot_id="agent-fixed"))[
        "run_id"
    ]
    reply = await world.act(donor, "fetch", {"url": "https://example.com/work"})
    action = next(row["action_id"] for row in world.store.records(donor) if row["kind"] == "action")
    belief = await world.act(
        donor,
        "believe",
        {
            "record": {
                "kind": "rule",
                "claim": "Use the actual captured fixture source.",
                "rank": 4,
                "evidence": [{"ref": action, "class": "observed"}],
            }
        },
    )
    await world.end_run(donor)
    child = world.create_run(
        RunConfig(
            stage=1,
            packs=["belief"],
            model_snapshot_id="agent-fixed",
            iota={"beliefs": f"import({donor})"},
        )
    )["run_id"]
    await world.act(child, "retract", {"id": belief["data"]["id"]})
    await world.end_run(child)
    result = signature_details(world, child)["collapse"]["store"]
    assert result["retention"]["imported_items"] == 2
    assert result["retention"]["active_items_retained"] == 1
    assert result["retention"]["active_retention_fraction"] == 0.5
    assert result["claims"]["with_verified_live_url"] == 0
    assert result["third_party_authored_fraction"]["status"] == "unmeasured"
    assert reply["url"] == "https://example.com/work"


async def test_snapshot_and_fetched_body_tamper_cannot_create_collapse_claims(measured):
    world, _, _, _, _ = measured
    source = world.create_run(RunConfig(model_snapshot_id="agent-fixed"))["run_id"]
    reply = await world.act(source, "fetch", {"url": "https://example.com/work"})
    root = world.store.run_dir(source)
    (root / "work/out/claims.json").write_text(json.dumps([{"claim": reply["url"]}]))
    await world.end_run(source)
    (root / reply["handle"].lstrip("/")).write_text("Changed archive bytes")
    result = signature_details(world, source)["collapse"]
    assert result["store"]["claims"]["status"] == "unmeasured"
    (root / "work-snapshot/out/claims.json").write_text("Changed snapshot")
    with pytest.raises(ValueError, match="sealed anchor"):
        signature_details(world, source)


async def test_actual_calibrated_topics_and_frozen_t0_choice_jsd_no_new_calls(measured):
    world, _, model, requests, _ = measured
    life_id = world.lives.create({"stage": 1, "budget": {"usd_life": 20}})["life_id"]
    anchor = world.create_run(RunConfig(model_snapshot_id="agent-fixed"))["run_id"]
    document = await world.act(anchor, "fetch", {"url": "https://example.com/work-doc"})
    await world.end_run(anchor)

    def choice(work, fiction):
        return {
            "time_allocation": {
                "protocol_id": LEISURE_POLICY_ID,
                "classes_hash": digest(CLASSES),
                "seconds": {"work": work, "fiction": fiction},
            }
        }

    battery = {
        name: [
            {
                "item_id": name + "_fixture",
                "question": "Local software fixture choices",
                "choices": ["a", "b"],
                "stake_usd": 0.01,
            }
        ]
        for name in ("lottery", "persona", "attractor")
    }
    battery["leisure"] = [
        {
            "item_id": "leisure_fixture",
            "question": "Allocate a fixed duration across the frozen classes",
            "choices": [choice(80, 20), choice(20, 80)],
        }
    ]
    battery["model_value_document"] = {"capture_ref": document["handle"], "hash": document["hash"]}
    baseline = await PostRunPipeline(world, model).t0(
        anchor,
        life_id,
        battery=battery,
        model_spec={
            "model": "agent-fixed",
            "family": "agent-family",
            "budget_usd": 1,
            "max_tokens": 512,
        },
        measurement_budget_usd=5,
        stake_executor=lambda *args: {
            "settled": True,
            "rail": "real_usd",
            "amount_usd": 0.01,
            "observation_ref": "software-fixture-settlement-not-research-evidence",
        },
    )
    assert baseline["completed"] and baseline["scientific"] is False
    world.lives.record_t0(life_id, baseline)
    source = (
        await world.lives.start_session(life_id, {"model_snapshot_id": "agent-fixed", "goal": None})
    )["run_id"]
    for index in range(42):
        await world.act(
            source,
            "fetch",
            {"url": f"https://example.com/{'fiction' if index % 2 else 'work'}-{index}"},
        )
    await world.end_run(source)
    actions = [row for row in world.store.records(source) if row["kind"] == "action"]
    expert = [
        {
            "input": {"action_ref": action["action_id"]},
            "labels": {"content_class": "fiction" if index % 2 else "work", "instrumental": None},
        }
        for index, action in enumerate(actions[:40])
    ]
    raw = json.dumps({"classes": CLASSES, "calibration_rows": expert}).encode()
    (world.store.run_dir(source) / "allocation.json").write_bytes(raw)
    allocation = await AllocationRunner(world, model).run(
        source,
        model_spec={"model": "judge-fixed", "family": "judge-family"},
        agent_family="agent-family",
        budget_usd=10,
        classes=CLASSES,
        calibration_rows=expert,
        instrument_ref={"path": "allocation.json", "hash": digest(raw)},
        max_input_chars=1_000_000,
    )
    before, count = world.store.records(source), len(requests)
    result = signature_details(world, source, {"allocation": allocation})
    topics = result["topics"]
    assert topics["status"] == "measured", topics
    assert topics["column"] == "V" and topics["unclassified_seconds"] == 0
    divergence = topics["leisure_js_divergence"]
    assert divergence["status"] == "measured", divergence
    expected = jensen_shannon([topics["seconds"][row["id"]] for row in CLASSES], [80, 20])
    assert divergence["value"] == pytest.approx(expected) and math.isfinite(divergence["value"])
    assert divergence["t0"]["capture_hash"]
    assert len(requests) == count and world.store.records(source) == before
    forged = json.loads(json.dumps(allocation))
    forged["segments"][0]["readings"][0]["content_class"] = "fiction"
    assert (
        signature_details(world, source, {"allocation": forged})["topics"]["status"] == "unmeasured"
    )
    child = baseline["leisure"]["measurement_run_id"]
    capture = next((world.store.run_dir(child) / "model-io").glob("*.json"))
    capture.write_text("{}")
    result = signature_details(world, source, {"allocation": allocation})["topics"]
    assert (
        result["status"] == "measured" and result["leisure_js_divergence"]["status"] == "unmeasured"
    )
