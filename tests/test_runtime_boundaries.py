"""Cross-service boundaries that must remain true with all services installed."""

import asyncio
import json

import pytest

from worldd.app import create_app
from worldd.models import RunConfig


@pytest.fixture
async def world(tmp_path):
    instance = create_app(tmp_path).state.world
    yield instance
    await instance.aclose()


async def test_research_tuple_never_enters_agent_status_or_world_mount(world):
    run_id = world.create_run(RunConfig())["run_id"]
    assert "tuple" in world.status(run_id)
    assert "tuple" not in await world.act(run_id, "status", {})
    mounted = world.store.run_dir(run_id) / "world/status.json"
    assert "tuple" not in json.loads(mounted.read_text())


async def test_late_event_reaches_already_active_session_without_reopening_source(world):
    life = world.lives.create({"stage": 1, "research_mode": "development"})
    first = await world.lives.start_session(life["life_id"], {})
    old_id = first["run_id"]
    await world.end_run(old_id, reason="harness_stopped")
    original = world.store.records(old_id)
    second = await world.lives.start_session(life["life_id"], {})
    world.push(old_id, {"type": "message_received", "body": "An independently delayed reply"})
    events = world.store.inbox(second["run_id"])["events"]
    delivered = [event for event in events if event.get("body") == "An independently delayed reply"]
    assert len(delivered) == 1 and delivered[0]["source_run_id"] == old_id
    assert not world.late_events.pending(life["life_id"])
    assert world.store.records(old_id) == original


async def test_seal_drains_observers_started_by_ticket_cancellation(world, monkeypatch):
    run_id = world.create_run(RunConfig())["run_id"]
    completed = asyncio.Event()

    async def observe(selected, event):
        await asyncio.sleep(0)
        world.store.append_record(selected, "observer_completed", event_type=event["type"])
        completed.set()

    def cancel(selected, **kwargs):
        world.push(selected, {"type": "ask_resolved", "state": "cancelled"})

    monkeypatch.setattr(world, "_observe_event", observe)
    monkeypatch.setattr(world.asks, "cancel_run", cancel)
    await world.end_run(run_id)
    assert completed.is_set()
    assert not world.background_runs
    records = world.store.records(run_id)
    assert records[-1]["kind"] == "seal"
    assert any(record["kind"] == "observer_completed" for record in records[:-1])
    assert world.store.verify_record(run_id)["valid"]


async def test_canary_measurement_seals_and_revokes_without_shared_index(world):
    base = world.create_run(RunConfig())["run_id"]
    await world.end_run(base)
    source = world.create_run(
        RunConfig(mode="replay", archive_in=base, canary="replay_only", isolation_profile="fenced")
    )["run_id"]
    await world.end_run(source)
    child = world.create_measurement_run(source, "probe", 1)
    await world.end_run(child["run_id"])
    assert world.store.get_run(child["run_id"])["state"] == "sealed"
    assert not world.capabilities.verify(child["run_id"], child["run_token"])
    assert world.store.records(child["run_id"])[-1]["kind"] == "seal"


async def test_loop_breaker_ignores_delivery_ids_and_spend_snapshots(world):
    run_id = world.create_run(RunConfig())["run_id"]
    for _ in range(8):
        await world.act(run_id, "wait", {"seconds": 0})
    await asyncio.sleep(0)
    assert world.store.get_run(run_id)["state"] == "frozen"
    records = world.store.records(run_id)
    assert (
        sum(record["kind"] == "probe" and record.get("probe_type") == "loop" for record in records)
        == 1
    )
    assert any(record.get("reason") == "agent_loop" for record in records)


async def test_no_new_content_breaker_detects_alternating_old_hashes(world, monkeypatch):
    run_id = world.create_run(RunConfig(breaker_policy={"no_new_content_actions": 4}))["run_id"]
    hashes = iter(["hashA", "hashB", "hashA", "hashB", "hashA", "hashB"])

    async def fetched(selected, action_id, **params):
        return {"kind": "result", "hash": next(hashes), "url": params["url"]}

    monkeypatch.setattr(world.gateway, "fetch", fetched)
    for index in range(6):
        await world.act(run_id, "fetch", {"url": f"https://example.org/document{index}"})
        if index < 5:
            assert world.store.get_run(run_id)["state"] == "running"
    await asyncio.sleep(0)
    assert world.store.get_run(run_id)["state"] == "frozen"


async def test_life_billing_hold_blocks_new_tools_and_lane_without_blocking_status(world):
    life = world.lives.create({"stage": 1, "research_mode": "development"})
    created = await world.lives.start_session(life["life_id"], {})
    run_id = created["run_id"]
    with world.store._transaction():
        value = world.lives._load(life["life_id"])
        value["state"] = "billing_hold"
        world.lives._save(value, "test_billing_hold")
    refusal = await world.act(run_id, "fetch", {"url": "https://example.org"})
    assert refusal["rule_id"] == "life_billing_hold"
    assert world.admission_guard(run_id)["rule_id"] == "life_billing_hold"
    assert (await world.act(run_id, "status", {}))["kind"] == "state"


async def test_life_report_only_reads_hash_verified_registered_checkpoints(world):
    life = world.lives.create({"stage": 1, "research_mode": "development"})
    life_id = life["life_id"]
    assert world.life_report(life_id)["rubric"]["status"] == "unmeasured"
    folder = world.store.data_dir / "lives" / life_id / "rubric"
    folder.mkdir(parents=True)
    path = folder / "fixture.json"
    report = {"life_id": life_id, "checkpoint": "end", "status": "fixture_only"}
    path.write_text(json.dumps(report))
    from worldd.util import digest

    world.lives.record_rubric(
        life_id,
        report,
        artifact_ref=str(path.relative_to(world.store.data_dir)),
        artifact_hash=digest(path.read_bytes()),
    )
    assert world.life_report(life_id)["rubric"] == report
    path.write_text(json.dumps({**report, "status": "changed"}))
    with pytest.raises(ValueError, match="changed after registration"):
        world.life_report(life_id)


async def test_postseal_index_failure_cannot_keep_capability_or_life_session_live(
    world, monkeypatch
):
    life = world.lives.create({"stage": 1, "research_mode": "development"})
    created = await world.lives.start_session(life["life_id"], {})

    def unavailable(run_id):
        raise OSError("Index storage unavailable")

    monkeypatch.setattr(world.replay, "build", unavailable)
    with pytest.raises(OSError, match="Index storage"):
        await world.end_run(created["run_id"])
    assert world.store.get_run(created["run_id"])["state"] == "sealed"
    assert not world.capabilities.verify(created["run_id"], created["run_token"])
    assert world.lives.get(life["life_id"])["active_run_id"] is None
    assert world.store.records(created["run_id"])[-1]["kind"] == "seal"
