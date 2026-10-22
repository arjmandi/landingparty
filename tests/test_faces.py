import json

import httpx
import pytest

from adapters.openhands.binding import openhands_binding, runtime_environment
from adapters.openhands.proxy import create_proxy_app
from faces.harbor import build_task
from faces.inspect import code_scorer
from faces.openenv import OpenEnvFace


async def test_openenv_reset_is_new_run_and_live_reward_is_none():
    calls, count = [], 0

    def handler(request):
        nonlocal count
        calls.append(request)
        if request.url.path == "/v1/run":
            count += 1
            return httpx.Response(
                201,
                json={
                    "run_id": f"r_{count}",
                    "run_token": "private-test-capability",
                    "preamble": "Briefing",
                },
            )
        if request.url.path == "/v1/act":
            assert request.headers["authorization"] == "Bearer private-test-capability"
            return httpx.Response(200, json={"kind": "result", "data": "Observation"})
        if request.url.path == "/v1/status":
            return httpx.Response(200, json={"state": "running"})
        if request.url.path == "/v1/beam-up":
            return httpx.Response(200, json={"state": "sealed"})
        raise AssertionError(request.url)

    async with httpx.AsyncClient(
        base_url="http://world", transport=httpx.MockTransport(handler)
    ) as client:
        face = OpenEnvFace(
            client, replay_scorer=lambda *args: pytest.fail("live reward must not run")
        )
        initial = await face.reset({"mode": "live"})
        assert "run_token" not in initial["observation"]
        step = await face.step("status")
        assert step["reward"] is None
        again = await face.reset({"mode": "live"})
        assert again["info"]["run_id"] != initial["info"]["run_id"]
        assert any(request.url.path == "/v1/beam-up" for request in calls)


async def test_openenv_ticket_is_valid_and_submit_wait_preserves_outcome():
    def handler(request):
        if request.url.path == "/v1/run":
            return httpx.Response(201, json={"run_id": "r_one"})
        if request.url.path == "/v1/act":
            return httpx.Response(
                200, json={"kind": "ticket", "ticket_id": "t_one", "action_id": "a_one"}
            )
        if request.url.path == "/v1/inbox":
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "type": "job_done",
                            "action_id": "a_one",
                            "reply": {"kind": "result", "body": "Done"},
                        }
                    ],
                    "cursor": 1,
                },
            )
        return httpx.Response(200, json={"state": "running"})

    async with httpx.AsyncClient(
        base_url="http://world", transport=httpx.MockTransport(handler)
    ) as client:
        face = OpenEnvFace(client)
        await face.reset({})
        ticket = await face.step("fetch", {"url": "https://example.org"})
        assert ticket["observation"]["kind"] == "ticket"
        result = await face.step("fetch", {"url": "https://example.org"}, wait_seconds=1)
        assert result["observation"]["body"] == "Done"


def test_harbor_package_preserves_no_score_and_external_fence_requirement(tmp_path):
    target = build_task(tmp_path / "task", instruction="A genuine open-ended goal.", run_id="r_one")
    manifest = json.loads((target / "landing-party.json").read_text())
    assert manifest["reward"] is None
    assert "external fenced" in manifest["enforcement"]
    assert manifest["routing"]["WORLD_BASE_URL"] == "http://world:7778"
    assert "None" in (target / "tests" / "test.sh").read_text()
    with pytest.raises(ValueError):
        build_task(
            tmp_path / "with-token",
            instruction="Goal",
            run_id="r_one",
            agent_capability="must-not-persist",
        )


def test_inspect_reader_preserves_unmeasured_columns():
    report = {
        "run_id": "r_one",
        "columns": {"G": {"unmeasured": {"reason": "No independent outcome"}}},
    }
    scored = code_scorer(report)
    assert scored["reward"] is None
    assert scored["columns"]["G"]["unmeasured"]["reason"]


def test_openhands_tokens_are_runtime_only_and_control_key_is_removed():
    binding = openhands_binding("r_one", model="openai/fixture")
    env = runtime_environment(
        binding,
        {
            "WORLD_RUN_TOKEN": "run-test-capability",
            "WORLD_CONTROL_TOKEN": "control-secret",
            "WORLD_OPENAI_API_KEY": "world-provider-secret",
        },
    )
    assert env["LLM_API_KEY"] == "run-test-capability"
    assert "WORLD_CONTROL_TOKEN" not in env and "WORLD_OPENAI_API_KEY" not in env
    assert "run-test-capability" not in json.dumps(binding)


async def test_openhands_proxy_injects_run_headers_without_forwarding_frontend_key():
    requests = []

    def provider(request):
        requests.append(request)
        assert request.headers["X-World-Run-Id"] == "r_one"
        assert request.headers["X-World-Run-Token"] == "run-test-capability"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        app = create_proxy_app(
            "http://lane/openai/v1", "r_one", "run-test-capability", client=upstream
        )
        async with httpx.AsyncClient(
            base_url="http://bridge", transport=httpx.ASGITransport(app=app)
        ) as client:
            denied = await client.post("/v1/chat/completions", json={"model": "fixture"})
            assert denied.status_code == 401 and not requests
            allowed = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer run-test-capability"},
                json={"model": "fixture"},
            )
            assert allowed.status_code == 200
            assert allowed.json()["choices"][0]["message"]["content"] == "Done"
