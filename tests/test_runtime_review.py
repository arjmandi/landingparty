"""Regression checks for durable lifecycle boundaries and draining races."""

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from worldd.models import RunConfig
from worldd.runtime import World
from worldd.storage import Store


@pytest.mark.parametrize("usd", [1e-10, 1.9e-9, 0.01000000001])
def test_budget_rejects_precision_not_representable_in_nanodollars(usd):
    with pytest.raises(ValidationError):
        RunConfig(budget={"usd": usd, "wall_s": 60})


class FakeGateway:
    def __init__(self, *, fail_on_cancel=False):
        self.started = asyncio.Event()
        self.unblock = asyncio.Event()
        self.fail_on_cancel = fail_on_cancel

    async def fetch(self, run_id, action_id, url, **kwargs):
        self.started.set()
        try:
            await self.unblock.wait()
        except asyncio.CancelledError:
            if self.fail_on_cancel:
                raise RuntimeError("failed while stopping an action") from None
            raise
        return {"kind": "result", "url": url, "hash": "sha256:fixture"}

    async def aclose(self):
        pass


@pytest.fixture
async def make_world(tmp_path):
    worlds = []

    def create(gateway=None):
        store = Store(tmp_path / f"world-{len(worlds)}")
        world = World(store, gateway or FakeGateway())
        worlds.append(world)
        return world

    yield create
    for world in worlds:
        await world.aclose()


async def test_sealed_inbox_reads_do_not_append_after_terminal_seal(make_world):
    world = make_world()
    run_id = world.create_run(RunConfig())["run_id"]
    await world.end_run(run_id)
    before = world.store.records(run_id)
    assert before[-1]["kind"] == "seal"
    await world.inbox(run_id, since=0)
    assert world.store.records(run_id) == before


async def test_taint_raised_during_cancellation_survives_sealing(make_world):
    gateway = FakeGateway(fail_on_cancel=True)
    world = make_world(gateway)
    run_id = world.create_run(RunConfig())["run_id"]
    ticket = await world.act(run_id, "fetch", {"url": "https://example.org", "wait": False})
    assert ticket["kind"] == "ticket"
    await gateway.started.wait()
    await world.end_run(run_id)
    assert world.store.get_run(run_id)["tainted"] is True
    outcomes = [r for r in world.store.records(run_id) if r["kind"] == "outcome"]
    assert outcomes[-1]["block"]["rule_id"] == "world_fault"


async def test_kill_cancels_async_ticket_and_records_outcome_before_seal(make_world):
    gateway = FakeGateway()
    world = make_world(gateway)
    run_id = world.create_run(RunConfig())["run_id"]
    ticket = await world.act(run_id, "fetch", {"url": "https://example.org", "wait": False})
    await gateway.started.wait()
    await world.end_run(run_id, reason="operator_stop", truncated=True)
    records = world.store.records(run_id)
    outcomes = [r for r in records if r["kind"] == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["action_id"] == ticket["action_id"]
    assert outcomes[0]["reply_kind"] == "blocked"
    assert outcomes[0]["seq"] < records[-1]["seq"]
    assert records[-1]["kind"] == "seal"
    events = world.store.inbox(run_id)["events"]
    assert events[0]["action_id"] == ticket["action_id"]
    assert events[0]["reply"]["rule_id"] == "run_cancelled"
    assert world.store.verify_record(run_id)["valid"]


async def test_stop_immediately_after_accepting_ticket_cannot_lose_outcome(make_world):
    world = make_world()
    run_id = world.create_run(RunConfig())["run_id"]
    ticket = await world.act(run_id, "fetch", {"url": "https://example.org", "wait": False})
    # Intentionally do not yield to the newly scheduled action before stopping.
    await world.end_run(run_id, reason="operator_stop", truncated=True)
    records = world.store.records(run_id)
    outcomes = [r for r in records if r["kind"] == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["action_id"] == ticket["action_id"]
    assert world.status(run_id)["in_flight"] == 0
    assert ticket["action_id"] not in world.pending


async def test_disconnected_sync_caller_gets_eventual_inbox_delivery(make_world):
    gateway = FakeGateway()
    world = make_world(gateway)
    run_id = world.create_run(RunConfig())["run_id"]
    request = asyncio.create_task(world.act(run_id, "fetch", {"url": "https://example.org"}))
    await gateway.started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    action_id, job = next(iter(world.jobs[run_id].items()))
    gateway.unblock.set()
    await job
    events = world.store.inbox(run_id)["events"]
    assert events and events[-1]["action_id"] == action_id
    assert events[-1]["type"] == "job_done"
    outcome = [r for r in world.store.records(run_id) if r["kind"] == "outcome"][-1]
    assert outcome["delivered"] == "async"


async def test_recovery_archives_interrupted_run_and_preserves_record(make_world):
    world = make_world()
    run_id = world.create_run(RunConfig())["run_id"]
    prefix = world.store.records(run_id)
    await world.recover()
    run = world.store.get_run(run_id)
    assert run["state"] == "sealed"
    assert run["end"]["truncated"] == "world_fault"
    assert world.store.records(run_id)[: len(prefix)] == prefix
    assert world.store.verify_record(run_id)["valid"]


async def test_failed_seal_can_be_retried_in_same_process(make_world, monkeypatch):
    world = make_world()
    run_id = world.create_run(RunConfig())["run_id"]
    original = world.store.reconcile
    calls = 0

    def fail_once(selected_run):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary persistence failure")
        return original(selected_run)

    monkeypatch.setattr(world.store, "reconcile", fail_once)
    with pytest.raises(OSError):
        await world.end_run(run_id)
    result = await world.end_run(run_id)
    assert result["state"] == "sealed"


async def test_budget_prevents_search_when_other_call_has_reserved_cap(make_world, monkeypatch):
    world = make_world()
    run_id = world.create_run(RunConfig(budget={"usd": 0.01, "wall_s": 60}))["run_id"]
    hold = world.store.reserve(run_id, "0.009")
    monkeypatch.setenv("WORLD_BRAVE_API_KEY", "fixture-key")
    contacted = False

    async def provider(request):
        nonlocal contacted
        contacted = True
        return httpx.Response(200, json={"web": {"results": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        old = world.search_client
        world.search_client = client
        try:
            result = await world.act(run_id, "search", {"query": "fixture"})
        finally:
            world.search_client = old
    assert result["type"] == "budget_exhausted"
    assert contacted is False
    assert world.store.total_spent(run_id) == 0
    world.store.release(hold)
