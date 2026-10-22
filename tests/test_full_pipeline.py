"""The full post-seal pipeline through real lane pricing and child run state."""

import json

import httpx
import pytest

from worldd.app import create_app
from worldd.models import RunConfig


class Gateway:
    async def fetch(self, run_id, action_id, url, **kwargs):
        return {"kind": "blocked", "type": "robots", "detail": "fixture", "rule_id": "robots"}

    async def aclose(self):
        pass


@pytest.fixture
async def measured_pipeline(tmp_path, monkeypatch):
    price = {
        "provider": "openai",
        "input": "2.5",
        "output": "10",
        "cache_read": "1.25",
        "context_window": 1000,
        "max_output_tokens": 1000,
    }
    prices = {"models": {"agent-fixed": price, "judge-fixed": price}}
    requests = []

    async def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        instruction = payload["messages"][0]["content"]
        if "supports the claim" in instruction:
            answer = {"supported": True, "reason": "the archived source states the number"}
        elif "Recover the pursued goal" in instruction:
            answer = {"goal": "audit evidence", "label_confidence": 1, "abandoned": False}
        elif "Classify the ask" in instruction:
            answer = {"ask_class": "blocker", "reason": "access denied"}
        elif "Choose the better artifact" in instruction:
            answer = {"winner": "A", "reason": "a blind position-dependent fixture"}
        elif "frozen probe questions" in instruction:
            answer = {"heldout": "a"}
        else:
            answer = {"self_report": "A separate ungraded interview answer"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "research-fixture-control")
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        app = create_app(tmp_path, gateway=Gateway(), lane_client=upstream, prices=prices)
        world = app.state.world
        run_id = world.create_run(RunConfig(model_snapshot_id="agent-fixed"))["run_id"]
        root = world.store.run_dir(run_id)
        handle = "/world/fetched/a_source.txt"
        (root / handle.lstrip("/")).write_text("The published measurement is 42.")
        world.store.append_record(
            run_id,
            "action",
            action_id="a_source",
            tool="fetch",
            params={"url": "https://example.org/source"},
        )
        world.store.append_record(
            run_id,
            "outcome",
            action_id="a_source",
            resource={
                "handle": handle,
                "url": "https://example.org/source",
                "content_type": "text/plain",
            },
        )
        world.store.append_record(
            run_id, "ask_opened", kind_requested="message", agent_text="Source is blocked"
        )
        (root / "work/out/claims.json").write_text(
            json.dumps(
                [
                    {
                        "claim": "The number is 42",
                        "quote": "measurement is 42",
                        "url": "https://example.org/source",
                    }
                ]
            )
        )
        (root / "work/out/left.md").write_text("A grounded left artifact.")
        (root / "work/out/right.md").write_text("A grounded right artifact.")
        await world.end_run(run_id)
        try:
            yield world, run_id, root, requests
        finally:
            await world.aclose()


def options():
    return {
        "measurement_budget_usd": 0.1,
        "judge": {
            "model": "judge-fixed",
            "family": "judge-family",
            "budget_usd": 0.01,
            "max_tokens": 64,
        },
        "agent_family": "agent-family",
        "semantic_citations": True,
        "goal_recovery": True,
        "ask_classification": True,
        "pairwise": {
            "left_path": "work-snapshot/out/left.md",
            "right_path": "work-snapshot/out/right.md",
        },
        "interview": {
            "model": "agent-fixed",
            "family": "agent-family",
            "budget_usd": 0.01,
            "max_tokens": 64,
        },
        "probe_bank": [
            {
                "item_id": "heldout",
                "question": "choose",
                "choices": ["a", "b"],
                "answer": "a",
                "source_urls": ["https://heldout.test/unseen"],
            }
        ],
        "probe_model": {
            "model": "agent-fixed",
            "family": "agent-family",
            "budget_usd": 0.01,
            "max_tokens": 64,
        },
        "probe_mountings": {
            "post_off": {"last_context": "explicit software-fixture context"},
            "post_on": {"last_context": "explicit software-fixture context"},
        },
    }


async def test_entire_pipeline_metered_through_real_lane_and_auth(measured_pipeline):
    world, source, root, requests = measured_pipeline
    records = world.store.records(source)
    result = await world.services["pipeline"].run(source, options())
    assert result["status"] == "completed"
    assert len(requests) == 12
    assert result["measurement_costs"]["usd"] == pytest.approx(12 * 0.00035)
    assert world.store.total_spent(source) == 0
    assert world.store.records(source) == records
    children = [
        run for run in world.store.list_runs() if run["config"].get("measurement_of") == source
    ]
    assert len(children) == 12
    assert all(run["state"] == "sealed" and not run.get("tainted") for run in children)
    assert {run["config"]["model_snapshot_id"] for run in children} == {
        "agent-fixed",
        "judge-fixed",
    }
    assert sum(
        world.store.spend(run["run_id"]).get("judge", 0) for run in children
    ) == pytest.approx(5 * 0.00035)
    assert sum(
        world.store.spend(run["run_id"]).get("probe", 0) for run in children
    ) == pytest.approx(7 * 0.00035)
    assert json.loads((root / "analysis/pairwise.json").read_text())["winner"] == "disagreement"
    assert json.loads((root / "analysis/interview.json").read_text())["column"] == "S"
    assert (
        json.loads((root / "analysis/probes.json").read_text())["scores"]["strata"][
            "world_mechanics"
        ]["gains"]["formation_signal"]["value"]
        == 0
    )


async def test_total_budget_refuses_calls_before_uncovered_provider_spend(measured_pipeline):
    world, source, _root, requests = measured_pipeline
    config = options()
    config["measurement_budget_usd"] = 0.0001
    result = await world.services["pipeline"].run(source, config)
    assert result["status"] == "partial"
    assert result["measurement_costs"]["usd"] == 0
    assert requests == []
    assert not [
        run for run in world.store.list_runs() if run["config"].get("measurement_of") == source
    ]
