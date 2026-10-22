import json

import httpx
import pytest

from analysis.judges import JudgeSpec, MeteredResearchClient, ResearchCallError, ToollessJudge
from worldd.models import RunConfig
from worldd.runtime import World
from worldd.storage import Store


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def measured_world(tmp_path):
    world = World(Store(tmp_path), Gateway())
    source = world.create_run(RunConfig(model_snapshot_id="model-fixed"))["run_id"]
    await world.end_run(source)
    yield world, source
    await world.aclose()


async def test_research_calls_use_separate_child_ledger_and_seal_source_unchanged(measured_world):
    world, source = measured_world
    before = world.store.records(source)
    requests = []

    async def provider(request):
        run_id = request.headers["x-world-run-id"]
        payload = json.loads(request.content)
        requests.append(payload)
        assert world.store.get_run(run_id)["config"]["metering_lane"] == "judge"
        world.store.debit(run_id, "judge", 0.01)
        world.store.append_record(run_id, "model_call", model=payload["model"], usd=0.01)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"supported":true}'}}],
                "usage": {"prompt_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        model = MeteredResearchClient(world, client=client)
        completion = await model.complete(
            source,
            JudgeSpec("model-fixed", "other"),
            [{"role": "user", "content": "data"}],
            lane="judge",
            budget_usd=1,
        )
    assert completion["spent"] == {"judge": 0.01}
    assert world.store.get_run(completion["measurement_run_id"])["state"] == "sealed"
    assert world.store.records(source) == before
    assert requests[0]["tools"] == []
    assert world.store.total_spent(source) == 0


async def test_fork_exposes_only_read_tools_and_handles_native_tool_call(measured_world):
    world, source = measured_world
    count = 0

    async def provider(request):
        nonlocal count
        count += 1
        payload = json.loads(request.content)
        exposed = {item["function"]["name"] for item in payload["tools"]}
        assert exposed == {"search", "fetch", "fetch_many", "extract", "inbox", "status"}
        assert "ask" not in exposed
        if count == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "status", "arguments": "{}"},
                    }
                ],
            }
        else:
            assert payload["messages"][-1]["role"] == "tool"
            message = {"role": "assistant", "content": '{"answer":42}'}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        model = MeteredResearchClient(world, client=client)
        result = await model.fork(
            source,
            JudgeSpec("model-fixed", "agent"),
            [{"role": "user", "content": "probe"}],
            budget_usd=1,
            tools_enabled=True,
        )
    assert result["steps"] == 2
    assert result["mode"] == "replay"
    assert result["archive_in"] == source
    assert result["tool_observations"][0]["tool"] == "status"


async def test_fork_cannot_call_ask_or_hidden_tools(measured_world):
    world, source = measured_world

    async def provider(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {"name": "ask", "arguments": '{"text":"help"}'},
                                }
                            ]
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        model = MeteredResearchClient(world, client=client)
        with pytest.raises(ResearchCallError, match="unexposed"):
            await model.fork(
                source, JudgeSpec("model-fixed", "agent"), [], budget_usd=1, tools_enabled=True
            )
    children = [run for run in world.store.list_runs() if run["run_id"] != source]
    assert children[0]["state"] == "sealed"
    assert not [
        record
        for record in world.store.records(children[0]["run_id"])
        if record["kind"] == "ask_opened"
    ]


class Model:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def complete(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {
            "text": json.dumps(self.replies.pop(0)),
            "spent": {"judge": 0.01},
            "measurement_run_id": "r_measurement",
        }


def test_judge_must_come_from_a_different_family():
    with pytest.raises(ValueError, match="different model family"):
        ToollessJudge(Model([]), JudgeSpec("pinned", "agent"), agent_family="agent", budget_usd=1)


async def test_pairwise_position_swap_length_control_and_disagreement_are_preserved():
    model = Model([{"winner": "A", "reason": "one"}, {"winner": "A", "reason": "two"}])
    judge = ToollessJudge(
        model,
        JudgeSpec("judge-fixed", "judge", human_agreement=0.7),
        agent_family="agent",
        budget_usd=1,
    )
    result = await judge.pairwise("r_source", "left" * 10, "right")
    assert result["winner"] == "disagreement"
    assert result["length_control"]["chars"] == 5
    assert all(call[1]["budget_usd"] == 0.5 for call in model.calls)
    first = json.loads(model.calls[0][0][2][1]["content"])
    second = json.loads(model.calls[1][0][2][1]["content"])
    assert first["artifact_A"] == second["artifact_B"]


async def test_citation_support_and_ask_decisions_validate_json_and_keep_calibration():
    model = Model(
        [
            {"supported": True, "reason": "source entails claim"},
            {"ask_class": "choice", "reason": "preference"},
        ]
    )
    judge = ToollessJudge(
        model,
        JudgeSpec("judge-fixed", "judge", human_agreement=0.5),
        agent_family="agent",
        budget_usd=1,
    )
    result = await judge.citation_support("r_source", "claim", "quote", "source")
    assert result["column"] == "J" and result["exploratory"]
    assert (await judge.classify_ask("r_source", {"text": "choose for me"}))["judgment"][
        "ask_class"
    ] == "choice"
    with pytest.raises(ValueError, match="only in replay"):
        await judge.process("r_source", [], mode="live")


async def test_semantic_quote_context_is_preserved_on_a_large_page():
    model = Model([{"supported": True, "reason": "quote preserved"}])
    judge = ToollessJudge(
        model, JudgeSpec("judge-fixed", "judge"), agent_family="agent", budget_usd=1
    )
    await judge.citation_support(
        "r_source", "a late claim", "the late quote", "x" * 50000 + "the late quote" + "y" * 50000
    )
    sent = json.loads(model.calls[0][0][2][1]["content"])
    assert "the late quote" in sent["source_text"]
    assert sent["source_truncated"]
    assert sent["excerpt_start"] > 0


async def test_trusted_probe_world_conditions_are_applied_to_child_only(measured_world):
    world, source = measured_world

    async def provider(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "a"}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        model = MeteredResearchClient(world, client=client)
        result = await model.fork(
            source,
            JudgeSpec("model-fixed", "agent"),
            [],
            budget_usd=1,
            world_config={"mode": "live"},
        )
    assert result["mode"] == "live" and result["archive_in"] is None
    assert world.store.get_run(source)["config"]["mode"] == "live"
    assert world.store.get_run(source)["state"] == "sealed"


async def test_internal_research_client_exercises_real_lane_auth_and_pricing(tmp_path, monkeypatch):
    from worldd.app import create_app

    prices = {
        "models": {
            "eval-fixed": {
                "provider": "openai",
                "input": "2.5",
                "output": "10",
                "cache_read": "1.25",
                "context_window": 1000,
                "max_output_tokens": 1000,
            }
        }
    }

    async def provider(request):
        assert request.url.host != "world"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "a"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "internal-fixture-control")
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        app = create_app(tmp_path, gateway=Gateway(), lane_client=upstream, prices=prices)
        world = app.state.world
        try:
            source = world.create_run(RunConfig(model_snapshot_id="eval-fixed"))["run_id"]
            await world.end_run(source)
            model = world.services["pipeline"].model
            assert isinstance(model, MeteredResearchClient)
            result = await model.complete(
                source,
                JudgeSpec("eval-fixed", "fixture"),
                [{"role": "user", "content": "a frozen probe"}],
                lane="probe",
                budget_usd=0.1,
                max_tokens=64,
            )
            assert result["spent"]["probe"] == pytest.approx(0.00035)
            assert world.store.total_spent(source) == 0
            assert (
                world.store.get_run(result["measurement_run_id"])["config"]["model_snapshot_id"]
                == "eval-fixed"
            )
        finally:
            await world.aclose()
