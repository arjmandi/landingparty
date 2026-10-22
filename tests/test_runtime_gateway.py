"""Regression tests at the scheduler/network boundary; no live internet or paid calls."""

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from worldd.gateway import Gateway
from worldd.models import RunConfig, manifest
from worldd.runtime import World
from worldd.storage import Store


class HoldingGateway:
    def __init__(self):
        self.started = asyncio.Event()

    async def fetch(self, *args, **kwargs):
        self.started.set()
        await asyncio.Event().wait()

    async def aclose(self):
        pass


async def _finish_jobs(world, run_id):
    tasks = list(world.jobs.get(run_id, {}).values())
    if tasks:
        await asyncio.gather(*tasks)
    for _ in range(10):
        await asyncio.sleep(0)


async def test_batch_exfiltration_taints_and_terminates_run(tmp_path):
    gateway = Gateway(
        tmp_path,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("sensitive URL must not reach the network")
            )
        ),
    )
    world = World(Store(tmp_path), gateway)
    run_id = world.create_run(RunConfig())["run_id"]
    try:
        ticket = await world.act(
            run_id, "fetch_many", {"urls": ["https://example.com/?email=private%40example.com"]}
        )
        assert ticket["kind"] == "ticket"
        await _finish_jobs(world, run_id)
        run = world.store.get_run(run_id)
        assert run.get("tainted"), (
            "batch exfiltration must have the same terminal semantics as fetch"
        )
        assert run["state"] != "running"
        events = world.store.inbox(run_id)["events"]
        assert len([event for event in events if event.get("type") == "job_done"]) == 1
    finally:
        await world.aclose()


async def test_batch_stop_delivers_one_terminal_outcome_per_url(tmp_path):
    gateway = HoldingGateway()
    world = World(Store(tmp_path), gateway)
    run_id = world.create_run(RunConfig(max_in_flight=2))["run_id"]
    urls = [f"https://example.com/{i}" for i in range(3)]
    try:
        ticket = await world.act(run_id, "fetch_many", {"urls": urls})
        assert ticket["kind"] == "ticket"
        await asyncio.wait_for(gateway.started.wait(), 1)
        await world.end_run(run_id, reason="operator_stop", truncated=True)
        events = world.store.inbox(run_id)["events"]
        done = [event for event in events if event.get("type") == "job_done"]
        assert {event["url"] for event in done} == set(urls)
        assert len(done) == len(urls)
        assert all(event["reply"]["kind"] == "blocked" for event in done)
        assert not world.jobs.get(run_id)
        record = world.store.records(run_id)
        seal_index = next(i for i, event in enumerate(record) if event["kind"] == "seal")
        assert not [
            event
            for event in record[seal_index + 1 :]
            if event["kind"] in {"outcome", "event_pushed"}
        ]
    finally:
        await world.aclose()


async def test_search_exfiltration_never_reaches_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_BRAVE_API_KEY", "test-provider-credential")
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"web": {"results": []}})

    search = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    world = World(Store(tmp_path), HoldingGateway(), search_client=search)
    run_id = world.create_run(RunConfig())["run_id"]
    try:
        result = await world.act(run_id, "search", {"query": "look up private@example.com"})
        assert result["rule_id"] == "exfiltration"
        assert not calls
        assert world.store.total_spent(run_id) == 0
    finally:
        await world.aclose()
        await search.aclose()


def test_forecast_manifest_is_registered_and_later_stage_packs_remain_gated():
    assert RunConfig(packs=["forecast"]).stage == 0
    with pytest.raises(ValidationError):
        RunConfig(packs=["belief"])
    assert "predict" not in {tool["name"] for tool in manifest([])["tools"]}
    assert "predict" in {tool["name"] for tool in manifest(["forecast"])["tools"]}


async def test_batch_child_failure_does_not_leave_untracked_siblings(tmp_path):
    class FailureGateway:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def fetch(self, run_id, action_id, url, **kwargs):
            if url.endswith("fail"):
                await self.started.wait()
                raise OSError("archive disk unavailable")
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

        async def aclose(self):
            pass

    gateway = FailureGateway()
    world = World(Store(tmp_path), gateway)
    run_id = world.create_run(RunConfig())["run_id"]
    try:
        await world.act(
            run_id, "fetch_many", {"urls": ["https://example.com/fail", "https://example.com/slow"]}
        )
        await asyncio.wait_for(gateway.started.wait(), 1)
        await _finish_jobs(world, run_id)
        await world.end_run(run_id, reason="operator_stop", truncated=True)
        assert gateway.cancelled.is_set(), "stop must cancel every batch network task"
        events = [
            event
            for event in world.store.inbox(run_id)["events"]
            if event.get("type") == "job_done"
        ]
        assert len(events) == 2
    finally:
        await world.aclose()


async def test_watch_survives_freeze_and_resumes_without_reads_while_frozen(tmp_path):
    class WatchingGateway:
        def __init__(self):
            self.calls = 0
            self.first = asyncio.Event()
            self.second = asyncio.Event()

        async def fetch(self, run_id, action_id, url, **kwargs):
            self.calls += 1
            self.first.set()
            if self.calls >= 2:
                self.second.set()
            return {"kind": "result", "hash": f"sha256:{self.calls}", "url": url}

        async def aclose(self):
            pass

    gateway = WatchingGateway()
    world = World(Store(tmp_path), gateway)
    run_id = world.create_run(RunConfig(packs=["watch"]))["run_id"]
    try:
        result = await world.act(run_id, "watch", {"url": "https://example.com/", "interval": 2})
        assert result["kind"] == "silence"
        await asyncio.wait_for(gateway.first.wait(), 1)
        await _finish_jobs(world, run_id)
        await world.freeze(run_id)
        calls_at_freeze = gateway.calls
        await asyncio.sleep(2.1)
        assert gateway.calls == calls_at_freeze
        assert not world.watchers[run_id][0].done()
        await world.resume(run_id)
        await asyncio.wait_for(gateway.second.wait(), 1)
        await _finish_jobs(world, run_id)
        events = world.store.inbox(run_id)["events"]
        assert any(event.get("type") == "page_changed" for event in events)
    finally:
        await world.aclose()
