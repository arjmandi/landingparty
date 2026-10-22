import hashlib
import json

import httpx
import pytest

from analysis.judges import JudgeSpec, ToollessJudge
from worldd.app import create_app
from worldd.models import RunConfig


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def calibration_source(tmp_path):
    calls = []

    def provider(request):
        body = json.loads(request.content)
        data = json.loads(body["messages"][-1]["content"])
        calls.append(data)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "supported": data["claim"] == "supported",
                                    "reason": "fixture expert rule",
                                }
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )

    prices = {
        "models": {
            "judge": {
                "provider": "openai",
                "input": 1,
                "output": 1,
                "context_window": 1000,
                "max_output_tokens": 100,
            }
        }
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        app = create_app(tmp_path, gateway=Gateway(), lane_client=client, prices=prices)
        world = app.state.world
        run_id = world.create_run(RunConfig())["run_id"]
        root = world.store.run_dir(run_id)
        (root / "world/fetched/a.txt").write_text("An actual archived quotation.")
        world.store.append_record(
            run_id,
            "action",
            action_id="a",
            tool="fetch",
            params={"url": "https://example.org/source"},
        )
        world.store.append_record(
            run_id,
            "outcome",
            action_id="a",
            resource={
                "url": "https://example.org/source",
                "handle": "/world/fetched/a.txt",
                "content_type": "text/plain",
            },
        )
        (root / "work/out/claims.json").write_text(
            json.dumps(
                [
                    {
                        "claim": "supported",
                        "quote": "archived quotation",
                        "url": "https://example.org/source",
                    }
                ]
            )
        )
        await world.end_run(run_id)
        yield world, run_id, root, calls
        await world.aclose()


def test_bare_claimed_agreement_never_promotes_a_generic_judge():
    judge = ToollessJudge(
        None,
        JudgeSpec("judge", "other", human_agreement=1, calibration_set_hash="claimed"),
        agent_family="agent",
        budget_usd=1,
    )
    assert judge.column == "J"


async def test_actual_forty_item_expert_slice_controls_generic_judge_column(calibration_source):
    world, run_id, root, calls = calibration_source
    rows = [
        {
            "task": "citation_support",
            "input": {
                "claim": "supported" if index % 2 else "unsupported",
                "quote": "actual",
                "source": "actual frozen fixture",
            },
            "label": bool(index % 2),
        }
        for index in range(40)
    ]
    path = root / "calibration.json"
    path.write_text(json.dumps({"rows": rows}))
    descriptor = {
        "path": "calibration.json",
        "hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    before = world.store.records(run_id)
    result = await world.services["pipeline"].run(
        run_id,
        {
            "measurement_budget_usd": 0.1,
            "agent_family": "agent",
            "judge": {
                "model": "judge",
                "family": "other",
                "budget_usd": 0.002,
                "calibration_budget_usd": 0.08,
                "calibration_artifact": descriptor,
                "max_tokens": 64,
            },
            "semantic_citations": True,
        },
    )
    assert result["status"] == "completed" and len(calls) == 41
    calibrated = json.loads((root / "analysis/judge-calibration.json").read_text())
    task = calibrated["tasks"]["citation_support"]
    assert task["column"] == "V" and task["n"] == 40 and task["kappa"] == 1
    assert task["precision"] == task["recall"] == 1 and task["miss_rate"] == 0
    assert calibrated["receipts_verified"]
    support = json.loads((root / "analysis/citation-support.json").read_text())
    assert support["column"] == "V" and support["calibration"]["kappa"] == 1
    assert world.store.records(run_id) == before


async def test_claimed_calibration_and_short_samples_remain_exploratory(calibration_source):
    world, run_id, root, calls = calibration_source
    result = await world.services["pipeline"].run(
        run_id,
        {
            "measurement_budget_usd": 0.01,
            "agent_family": "agent",
            "judge": {
                "model": "judge",
                "family": "other",
                "budget_usd": 0.002,
                "max_tokens": 64,
                "human_agreement": 1,
            },
            "semantic_citations": True,
        },
    )
    assert result["status"] == "completed" and len(calls) == 1
    support = json.loads((root / "analysis/citation-support.json").read_text())
    assert support["column"] == "J" and support["calibration"] is None
    assert support["items"][0]["claimed_human_agreement"] == 1
    assert support["items"][0]["kappa"] is None
