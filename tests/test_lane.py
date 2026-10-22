from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from worldd.lane import StreamUsage, UsageError, create_lane_router, normalize_usage, usage_cost
from worldd.storage import Store

PRICES = {
    "models": {
        "fixture-openai": {
            "provider": "openai",
            "input": "2.5",
            "output": "10",
            "cache_read": "1.25",
            "context_window": 1000,
            "max_output_tokens": 1000,
        },
        "fixture-anthropic": {
            "provider": "anthropic",
            "input": "3",
            "output": "15",
            "cache_read": "0.3",
            "cache_write": "3.75",
            "cache_write_1h": "6",
            "context_window": 1000,
            "max_output_tokens": 1000,
        },
    }
}


def lane(store, handler):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = FastAPI()
    app.include_router(create_lane_router(store, client=upstream, prices=PRICES))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://world"
    ), upstream


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path)
    yield result
    result.close()


def run(store, budget="20", **config):
    return store.create_run({"budget": {"usd": budget, "wall_s": 7200}, **config})["run_id"]


def test_openai_cached_tokens_are_disjoint():
    usage = normalize_usage(
        "openai",
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 800},
        },
    )
    assert usage["input"] == 200
    assert usage_cost(usage, PRICES["models"]["fixture-openai"]) == Decimal("0.0025")


def test_anthropic_cache_durations_are_distinct():
    usage = normalize_usage(
        "anthropic",
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 100},
        },
    )
    assert usage_cost(usage, PRICES["models"]["fixture-anthropic"]) == Decimal("0.00210")


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": -1, "completion_tokens": 1},
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 2}},
    ],
)
def test_invalid_provider_usage_is_never_guessed(usage):
    with pytest.raises(UsageError):
        normalize_usage("openai", usage)


async def test_chat_forwarded_unchanged_and_metered(store, monkeypatch):
    monkeypatch.setenv("WORLD_OPENAI_API_KEY", "world-secret")
    monkeypatch.setenv("WORLD_OPENAI_UPSTREAM", "https://fixture.provider")
    run_id = run(store, capture_model_io=True)
    payload = {
        "model": "fixture-openai",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hello"}],
    }

    def upstream(request):
        assert str(request.url) == "https://fixture.provider/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer world-secret"
        assert "x-world-run-id" not in request.headers
        assert json.loads(request.content) == payload
        return httpx.Response(
            200,
            json={
                "model": "fixture-openai",
                "choices": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    client, remote = lane(store, upstream)
    async with client, remote:
        response = await client.post(
            "/openai/v1/chat/completions", headers={"x-world-run-id": run_id}, json=payload
        )
    assert response.status_code == 200
    assert store.spend(run_id)["model"] == 0.00035
    assert store.reserved(run_id) == 0
    calls = [item for item in store.records(run_id) if item["kind"] == "model_call"]
    assert calls[0]["fixture_transport"] is True
    assert calls[0]["transport_type"] == "MockTransport"
    assert calls[0]["upstream_origin"] == "https://fixture.provider"
    assert len(calls) == 1 and calls[0]["usage"]["input"] == 100
    assert store.reconcile(run_id)["valid"]
    capture = next((store.run_dir(run_id) / "model-io").glob("*.json")).read_text()
    assert "world-secret" not in capture
    assert "world-secret" not in (store.run_dir(run_id) / "record.jsonl").read_text()
    anchor = next(item for item in store.records(run_id) if item["kind"] == "model_capture")
    assert anchor["call_id"] == calls[0]["call_id"]
    assert (
        anchor["capture_hash"]
        == "sha256:"
        + hashlib.sha256((store.run_dir(run_id) / anchor["capture_ref"]).read_bytes()).hexdigest()
    )


async def test_responses_route_and_query_run_id(store):
    run_id = run(store)
    client, remote = lane(
        store,
        lambda request: httpx.Response(
            200,
            json={
                "model": "fixture-openai",
                "output": [],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 40},
                },
            },
        ),
    )
    async with client, remote:
        response = await client.post(
            f"/openai/v1/responses?run_id={run_id}",
            json={"model": "fixture-openai", "max_output_tokens": 100, "input": "hello"},
        )
    assert response.status_code == 200
    assert store.spend(run_id)["model"] == 0.0003


async def test_anthropic_messages_and_run_scoped_route(store):
    run_id = run(store)

    def handler(request):
        assert request.headers["x-api-key"] == "harness-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(
            200,
            json={
                "model": "fixture-anthropic",
                "content": [],
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        )

    client, remote = lane(store, handler)
    async with client, remote:
        response = await client.post(
            f"/lane/{run_id}/anthropic/v1/messages",
            headers={"x-api-key": "harness-key"},
            json={"model": "fixture-anthropic", "max_tokens": 100, "messages": []},
        )
    assert response.status_code == 200
    assert store.spend(run_id)["model"] == 0.00045


async def test_anthropic_bearer_auth_preserved_without_recording_it(store):
    run_id = run(store, capture_model_io=True)

    def handler(request):
        assert request.headers["authorization"] == "Bearer oauth-secret"
        return httpx.Response(
            200,
            json={
                "model": "fixture-anthropic",
                "content": [],
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        )

    client, remote = lane(store, handler)
    async with client, remote:
        response = await client.post(
            f"/lane/{run_id}/anthropic/v1/messages",
            headers={"authorization": "Bearer oauth-secret"},
            json={"model": "fixture-anthropic", "max_tokens": 100, "messages": []},
        )
    assert response.status_code == 200
    assert "oauth-secret" not in (store.run_dir(run_id) / "record.jsonl").read_text()
    assert (
        "oauth-secret" not in next((store.run_dir(run_id) / "model-io").glob("*.json")).read_text()
    )


async def test_web_tools_unpriced_models_and_missing_limits_do_not_call_provider(store):
    run_id = run(store)
    calls = []

    def handler(request):
        calls.append(request)
        raise AssertionError("Denied request reached provider")

    client, remote = lane(store, handler)
    async with client, remote:
        for payload, status in [
            (
                {
                    "model": "fixture-openai",
                    "max_tokens": 100,
                    "tools": [{"type": "web_search_preview"}],
                },
                403,
            ),
            ({"model": "unpriced", "max_tokens": 100}, 400),
            ({"model": "fixture-openai"}, 400),
            ({"model": "fixture-openai", "max_tokens": 100, "stream": True}, 400),
            ({"model": "fixture-openai", "max_tokens": 100, "service_tier": "priority"}, 400),
        ]:
            response = await client.post(
                "/openai/v1/chat/completions", headers={"x-world-run-id": run_id}, json=payload
            )
            assert response.status_code == status
    assert not calls
    assert store.total_spent(run_id) == 0
    assert store.reserved(run_id) == 0
    assert any(item["kind"] == "control_plane_audit" for item in store.records(run_id))


async def test_budget_reservation_refuses_call_before_network(store):
    run_id = run(store, "0.001")
    client, remote = lane(store, lambda request: pytest.fail("Over-budget call reached provider"))
    async with client, remote:
        response = await client.post(
            f"/lane/{run_id}/openai/v1/chat/completions",
            json={"model": "fixture-openai", "max_tokens": 100},
        )
    assert response.status_code == 402
    assert store.total_spent(run_id) == 0
    assert store.reserved(run_id) == 0


async def test_missing_usage_freezes_and_preserves_hold(store):
    run_id = run(store)
    client, remote = lane(store, lambda request: httpx.Response(200, json={"choices": []}))
    async with client, remote:
        response = await client.post(
            f"/lane/{run_id}/openai/v1/chat/completions",
            json={"model": "fixture-openai", "max_tokens": 100},
        )
    assert response.status_code == 502
    assert store.get_run(run_id)["tainted"]
    assert store.get_run(run_id)["state"] == "frozen"
    assert store.total_spent(run_id) == 0
    assert store.reserved(run_id) == 0.0035
    assert (
        next(item for item in store.records(run_id) if item["kind"] == "model_call")["usd"] is None
    )


async def test_provider_error_releases_hold(store):
    run_id = run(store)
    client, remote = lane(
        store, lambda request: httpx.Response(429, json={"error": {"message": "rate limited"}})
    )
    async with client, remote:
        response = await client.post(
            f"/lane/{run_id}/openai/v1/chat/completions",
            json={"model": "fixture-openai", "max_tokens": 100},
        )
    assert response.status_code == 429
    assert store.total_spent(run_id) == 0 and store.reserved(run_id) == 0


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data, size=7):
        self.data, self.size = data, size

    async def __aiter__(self):
        for start in range(0, len(self.data), self.size):
            yield self.data[start : start + self.size]


def sse(*events):
    return "".join(
        "data: " + (event if isinstance(event, str) else json.dumps(event)) + "\n\n"
        for event in events
    ).encode()


async def test_streaming_chat_unchanged_and_metered_after_done(store):
    run_id = run(store)
    payload = sse(
        {"choices": [{"delta": {"content": "héllo"}}]},
        {"model": "fixture-openai", "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
        "[DONE]",
    )
    client, remote = lane(
        store,
        lambda request: httpx.Response(
            200, stream=Chunks(payload), headers={"content-type": "text/event-stream"}
        ),
    )
    async with client, remote:
        response = await client.post(
            f"/lane/{run_id}/openai/v1/chat/completions",
            json={
                "model": "fixture-openai",
                "max_tokens": 100,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
    assert response.content == payload
    assert store.spend(run_id)["model"] == 0.00035 and store.reserved(run_id) == 0


async def test_anthropic_stream_accumulates_start_and_delta_usage(store):
    run_id = run(store)
    payload = sse(
        {
            "type": "message_start",
            "message": {
                "model": "fixture-anthropic",
                "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 200},
            },
        },
        {"type": "message_delta", "usage": {"output_tokens": 50}},
        {"type": "message_stop"},
    )
    client, remote = lane(store, lambda request: httpx.Response(200, stream=Chunks(payload)))
    async with client, remote:
        response = await client.post(
            f"/lane/{run_id}/anthropic/v1/messages",
            json={"model": "fixture-anthropic", "max_tokens": 100, "stream": True},
        )
    assert response.content == payload
    assert store.spend(run_id)["model"] == 0.00111


async def test_responses_stream_uses_final_response_usage(store):
    run_id = run(store)
    payload = sse(
        {
            "type": "response.completed",
            "response": {
                "model": "fixture-openai",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        }
    )
    client, remote = lane(store, lambda request: httpx.Response(200, stream=Chunks(payload)))
    async with client, remote:
        response = await client.post(
            f"/lane/{run_id}/openai/v1/responses",
            json={"model": "fixture-openai", "max_output_tokens": 100, "stream": True},
        )
    assert response.content == payload
    assert store.spend(run_id)["model"] == 0.00035


async def test_stream_without_usage_is_tainted_and_never_reported_free(store):
    run_id = run(store)
    client, remote = lane(store, lambda request: httpx.Response(200, stream=Chunks(sse("[DONE]"))))
    async with client, remote:
        response = await client.post(
            f"/lane/{run_id}/openai/v1/chat/completions",
            json={
                "model": "fixture-openai",
                "max_tokens": 100,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
    assert b"unmetered_response" in response.content
    assert store.get_run(run_id)["tainted"]
    assert store.reserved(run_id) > 0


async def test_concurrent_requests_cannot_reserve_same_dollars(store):
    run_id = run(store, "0.005")
    provider_started = asyncio.Event()
    complete = asyncio.Event()
    called = 0

    async def handler(request):
        nonlocal called
        called += 1
        provider_started.set()
        await complete.wait()
        return httpx.Response(200, json={"usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    client, remote = lane(store, handler)
    async with client, remote:
        first = asyncio.create_task(
            client.post(
                f"/lane/{run_id}/openai/v1/chat/completions",
                json={"model": "fixture-openai", "max_tokens": 100},
            )
        )
        await provider_started.wait()
        second = await client.post(
            f"/lane/{run_id}/openai/v1/chat/completions",
            json={"model": "fixture-openai", "max_tokens": 100},
        )
        assert second.status_code == 402
        complete.set()
        assert (await first).status_code == 200
    assert called == 1
    assert store.total_spent(run_id) == 0.00035


async def test_run_stop_interrupts_in_flight_provider_call(store):
    run_id = run(store)
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.Event().wait()

    client, remote = lane(store, handler)
    async with client, remote:
        pending = asyncio.create_task(
            client.post(
                f"/lane/{run_id}/openai/v1/chat/completions",
                json={"model": "fixture-openai", "max_tokens": 100},
            )
        )
        await started.wait()
        store.update_run(run_id, state="killed")
        response = await asyncio.wait_for(pending, timeout=1)
    assert response.status_code == 502
    assert store.get_run(run_id)["state"] == "killed"
    assert store.get_run(run_id)["tainted"] and store.reserved(run_id) > 0


def test_sse_parser_handles_unicode_fragmentation():
    parser = StreamUsage("openai")
    wire = 'data: {"text":"héllo"}\r\n\r\ndata: [DONE]\r\n\r\n'.encode()
    for byte in wire:
        parser.feed(bytes([byte]))
    parser.feed(b"", final=True)
    assert parser.complete


@pytest.mark.parametrize("streaming", [False, True])
async def test_control_plane_kill_drains_lane_before_seal(tmp_path, streaming):
    from worldd.app import create_app

    started = asyncio.Event()
    provider_closed = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse({"choices": [{"delta": {"content": "hello"}}]})
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                provider_closed.set()

        async def aclose(self):
            provider_closed.set()

    async def handler(request):
        if streaming:
            return httpx.Response(200, stream=SlowStream())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            provider_closed.set()

    remote = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(tmp_path, lane_client=remote, prices=PRICES)
    async with (
        remote,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://world"
        ) as client,
    ):
        run_id = (await client.post("/v1/run", json={})).json()["run_id"]
        payload = {"model": "fixture-openai", "max_tokens": 100}
        if streaming:
            payload.update(stream=True, stream_options={"include_usage": True})
        pending = asyncio.create_task(
            client.post(f"/lane/{run_id}/openai/v1/chat/completions", json=payload)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        result = await asyncio.wait_for(client.post("/v1/kill", json={"run_id": run_id}), timeout=2)
        assert result.status_code == 200
        response = await asyncio.wait_for(pending, timeout=1)
        assert response.status_code == (200 if streaming else 502)
        await asyncio.wait_for(provider_closed.wait(), timeout=0.5)
        store = app.state.store
        records = store.records(run_id)
        assert records[-1]["kind"] == "seal"
        assert len([item for item in records if item["kind"] == "model_call"]) == 1
        assert store.get_run(run_id)["tainted"]
        assert store.reserved(run_id) > 0
        assert app.state.lane_router.lane_manager.active_count(run_id) == 0
        assert store.verify_record(run_id)["valid"]
        before = len(records)
        await asyncio.sleep(0.15)
        assert len(store.records(run_id)) == before
    await app.state.world.aclose()


async def test_stop_barrier_finalizes_a_backpressured_stream(store):
    run_id = run(store, capture_model_io=True)
    remote = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=Chunks(sse({"choices": [{"delta": {"content": "hello"}}]}))
            )
        )
    )
    router = create_lane_router(store, client=remote, prices=PRICES)
    app = FastAPI()
    app.include_router(router)
    paused = asyncio.Event()
    resume = asyncio.Event()
    request_received = False
    body = json.dumps(
        {
            "model": "fixture-openai",
            "max_tokens": 100,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ).encode()

    async def receive():
        nonlocal request_received
        if not request_received:
            request_received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body") and not paused.is_set():
            paused.set()
            await resume.wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/lane/{run_id}/openai/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "server": ("world", 80),
        "client": ("test", 123),
        "root_path": "",
    }
    async with remote:
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(paused.wait(), timeout=1)
        store.update_run(run_id, state="stopping")
        result = await router.lane_manager.stop_run(run_id, timeout=0.2)
        assert result["audited"]
        assert store.get_run(run_id)["tainted"]
        anchor = next(row for row in store.records(run_id) if row["kind"] == "model_capture")
        capture = store.run_dir(run_id) / anchor["capture_ref"]
        before = capture.read_bytes()
        assert anchor["capture_hash"] == "sha256:" + hashlib.sha256(before).hexdigest()
        store.append_record(run_id, "seal")
        store.update_run(run_id, state="sealed")
        count = len(store.records(run_id))
        resume.set()
        await asyncio.wait_for(task, timeout=1)
        assert len(store.records(run_id)) == count
        assert store.records(run_id)[-1]["kind"] == "seal"
        assert capture.read_bytes() == before
