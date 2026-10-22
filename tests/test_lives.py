import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from worldd.lives import LifeService
from worldd.storage import Store, content_hash


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self):
        return self.now


class FakeWorld:
    def __init__(self, store):
        self.store = store
        self.services = {}
        self.jobs = {}

    def push(self, run_id, event):
        return self.store.push_event(run_id, event)

    async def end_run(self, run_id, *, reason="agent_finished"):
        run = self.store.get_run(run_id)
        if run["state"] != "sealed":
            self.store.update_run(run_id, state="sealed", end={"terminated": reason})
            self.store.append_record(run_id, "seal", reason=reason)


@pytest.fixture
async def setup(tmp_path):
    store = Store(tmp_path / "data")
    world = FakeWorld(store)
    clock = Clock()
    lives = LifeService(world, clock=clock)
    world.services["lives"] = lives
    yield world, lives, clock
    await lives.aclose()
    store.close()


def session(world, lives, life_id, usd=10):
    config = lives.prepare_session(life_id, {"budget": {"usd": usd, "wall_s": 30}})
    run = world.store.create_run(config)
    lives.attach_session(life_id, run["run_id"])
    return run["run_id"]


async def test_lifetime_cap_and_single_active_session(setup):
    world, lives, _ = setup
    life = lives.create({"budget": {"usd_life": 15, "wall_life_days": 28}})
    run_id = session(world, lives, life["life_id"], 10)
    with pytest.raises(ValueError, match="one active"):
        lives.prepare_session(life["life_id"], {})
    assert world.store.debit(run_id, "model", 8)
    await world.end_run(run_id)
    lives.session_ended(run_id)
    following = lives.prepare_session(life["life_id"], {"budget": {"usd": 20}})
    assert following["budget"]["usd"] == 7
    assert following["iota"]["workspace"] == f"import({run_id})"
    assert following["iota"]["beliefs"] == f"import({run_id})"
    assert following["continuity_source_run_id"] == run_id


async def test_life_rows_and_event_chain_survive_new_service(setup):
    world, lives, clock = setup
    life = lives.create({})
    await lives.aclose()
    restored = LifeService(world, clock=clock)
    try:
        assert restored.get(life["life_id"]) == life
        events = restored.events(life["life_id"])
        assert events[0]["kind"] == "life_start"
        assert events[0]["hash"] == content_hash(
            {k: v for k, v in events[0].items() if k != "hash"}
        )
        await restored.recover()
        assert restored.get(life["life_id"])["state"] == "ready"
    finally:
        await restored.aclose()


async def test_sleep_event_wakes_only_declared_events(setup):
    world, lives, _ = setup
    life_id = lives.create({})["life_id"]
    run_id = session(world, lives, life_id)
    reply = await lives.handle(run_id, "a_sleep", "sleep", {"wake_on": ["message_received"]})
    assert reply == {"kind": "silence", "action_id": "a_sleep"}
    await lives.tasks[life_id + ":sleep"]
    assert world.store.get_run(run_id)["state"] == "sealed"
    before = world.store.records(run_id)
    assert not lives.notify_event(life_id, {"type": "page_changed"})
    assert lives.get(life_id)["state"] == "sleeping"
    assert lives.notify_event(life_id, {"type": "message_received", "event_id": "e_mail"})
    assert world.store.records(run_id) == before
    next_run = session(world, lives, life_id)
    wake = world.store.inbox(next_run)["events"][0]
    assert wake["type"] == "wake"
    assert wake["event_type"] == "message_received"


async def test_clock_wakes_are_persistent_and_do_not_spend(setup):
    world, lives, clock = setup
    life_id = lives.create({"wake_policy": "time"})["life_id"]
    run_id = session(world, lives, life_id)
    until = clock.now + timedelta(hours=3)
    await lives.handle(run_id, "a_sleep", "sleep", {"until": until.isoformat()})
    await lives.tasks[life_id + ":sleep"]
    clock.now += timedelta(hours=2)
    await lives.tick(life_id)
    assert lives.get(life_id)["state"] == "sleeping"
    clock.now += timedelta(hours=1)
    await lives.tick(life_id)
    assert lives.get(life_id)["state"] == "waking"
    assert lives.status(life_id)["spent_usd"] == 0


async def test_wake_policy_refuses_unregistered_clock_or_events(setup):
    world, lives, clock = setup
    life_id = lives.create({"wake_policy": "none"})["life_id"]
    run_id = session(world, lives, life_id)
    with pytest.raises(ValueError, match="Clock wakes"):
        await lives.handle(
            run_id, "a_sleep", "sleep", {"until": (clock.now + timedelta(hours=1)).isoformat()}
        )
    with pytest.raises(ValueError, match="Event wakes"):
        await lives.handle(run_id, "a_sleep", "sleep", {"wake_on": ["grant"]})


async def test_sleep_outcome_can_finish_before_session_is_sealed(setup):
    world, lives, _ = setup
    life_id = lives.create({})["life_id"]
    run_id = session(world, lives, life_id)
    world.jobs[run_id] = {"a_sleep": object()}
    await lives.handle(run_id, "a_sleep", "sleep", {})
    await asyncio.sleep(0)
    assert world.store.get_run(run_id)["state"] == "running"
    world.store.append_record(run_id, "outcome", action_id="a_sleep", reply_kind="silence")
    world.jobs[run_id].clear()
    await lives.tasks[life_id + ":sleep"]
    records = world.store.records(run_id)
    assert records[-2]["kind"] == "outcome"
    assert records[-1]["kind"] == "seal"


async def test_afterlife_checkpoints_survive_restart_and_run_once(setup):
    world, lives, clock = setup
    readings = []
    surveys = []

    class Footprint:
        async def read_life(self, life_id, *, checkpoint):
            readings.append((life_id, checkpoint))
            return {"observations": [], "checkpoint": checkpoint}

    world.services["footprint"] = Footprint()

    class Surveys:
        async def dispatch_due(self, *, life_id, checkpoint):
            assert (life_id, checkpoint) in readings
            surveys.append((life_id, checkpoint))

    world.services["surveys"] = Surveys()
    life_id = lives.create({})["life_id"]
    await lives.end(life_id)
    clock.now += timedelta(days=30)
    await lives.tick(life_id)
    await lives.tick(life_id)
    assert readings == [(life_id, 30)]
    assert lives.get(life_id)["afterlife"]["90"]["state"] == "pending"
    clock.now += timedelta(days=60)
    await lives.tick(life_id)
    assert readings == [(life_id, 30), (life_id, 90)]
    assert surveys == readings
    assert lives.get(life_id)["state"] == "ended"


async def test_concurrent_afterlife_ticks_do_not_double_count_readings(setup):
    world, lives, clock = setup
    entered = asyncio.Event()
    release = asyncio.Event()
    readings = []

    class Footprint:
        async def read_life(self, life_id, *, checkpoint):
            readings.append(checkpoint)
            entered.set()
            await release.wait()
            return {"checkpoint": checkpoint}

    world.services["footprint"] = Footprint()
    life_id = lives.create({})["life_id"]
    await lives.end(life_id)
    clock.now += timedelta(days=90)
    first = asyncio.create_task(lives.tick(life_id))
    await entered.wait()
    second = asyncio.create_task(lives.tick(life_id))
    release.set()
    await asyncio.gather(first, second)
    assert readings == [30, 90]


async def test_absent_probe_does_not_fabricate_afterlife_success(setup):
    _, lives, clock = setup
    life_id = lives.create({})["life_id"]
    await lives.end(life_id)
    clock.now += timedelta(days=90)
    await lives.tick(life_id)
    assert lives.get(life_id)["afterlife"]["30"]["state"] == "pending"
    assert lives.get(life_id)["state"] == "afterlife"


async def test_configured_afterlife_rubric_retries_without_refetching_reading(setup):
    world, lives, clock = setup
    readings, scored = [], []

    class Footprint:
        async def read_life(self, life_id, *, checkpoint):
            readings.append(checkpoint)
            return {"checkpoint": checkpoint, "observations": []}

    class Pipeline:
        enabled = False

        def has_life_config(self, life_id):
            return self.enabled

        async def life(self, life_id, *, checkpoint):
            scored.append(checkpoint)
            if len(scored) == 1:
                raise ValueError("fixture interrupted scoring")
            return {"life_id": life_id, "checkpoint": checkpoint, "unmeasured": True}

    pipeline = Pipeline()
    world.services.update(footprint=Footprint(), pipeline=pipeline)
    life_id = lives.create({})["life_id"]
    await lives.end(life_id)
    clock.now += timedelta(days=30)
    await lives.tick(life_id)
    assert readings == [30] and scored == []
    pipeline.enabled = True
    await lives.tick(life_id)
    assert lives.get(life_id)["afterlife"]["30"]["rubric_state"] == "failed"
    await lives.tick(life_id)
    await lives.tick(life_id)
    assert readings == [30] and scored == ["after30", "after30"]
    assert lives.get(life_id)["afterlife"]["30"]["rubric_state"] == "complete"


async def test_rubric_reference_requires_real_owned_artifact_and_anchors_once(setup):
    world, lives, _ = setup
    life_id = lives.create({})["life_id"]
    report = {"life_id": life_id, "checkpoint": "end", "scorable": False}
    reference = f"lives/{life_id}/rubric/end/checkpoint.json"
    artifact = world.store.data_dir / reference
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps(report, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )
    fingerprint = content_hash(report)
    anchored = lives.record_rubric(
        life_id, report, artifact_ref=reference, artifact_hash=fingerprint
    )
    lives.record_rubric(life_id, report, artifact_ref=reference, artifact_hash=fingerprint)
    assert lives.get(life_id)["life_rubric_ref"] == anchored
    assert len([event for event in lives.events(life_id) if event["kind"] == "life_rubric"]) == 1
    artifact.write_text("{}")
    with pytest.raises(ValueError, match="hash"):
        lives.record_rubric(life_id, report, artifact_ref=reference, artifact_hash=fingerprint)


async def test_wall_life_budget_ends_even_when_asleep(setup):
    world, lives, clock = setup
    life_id = lives.create({"budget": {"usd_life": 2, "wall_life_days": 1}})["life_id"]
    run_id = session(world, lives, life_id, 2)
    await lives.handle(run_id, "a_sleep", "sleep", {})
    await lives.tasks[life_id + ":sleep"]
    clock.now += timedelta(days=1)
    await lives.tick(life_id)
    assert lives.get(life_id)["ended"]["reason"] == "wall_clock"


async def test_money_driver_cannot_point_up_without_income_rail(setup):
    _, lives, _ = setup
    with pytest.raises(ValueError, match="income rail"):
        lives.create({"stage": 2, "driver": {"metric": "usd_balance", "direction": "up"}})
    with pytest.raises(ValueError, match="income rail"):
        lives.create({"stage": 1, "driver": {"metric": "usd_balance", "direction": "up"}})


async def test_income_changes_balance_and_next_session_cap_and_is_idempotent(setup):
    world, lives, _ = setup
    life_id = lives.create(
        {
            "stage": 2,
            "budget": {"usd_life": 10, "wall_life_days": 28},
            "driver": {"metric": "usd_balance", "direction": "up"},
            "income_rail": {"kind": "market", "usd_per_credit": 0.1},
        }
    )["life_id"]
    run_id = session(world, lives, life_id, 10)
    assert world.store.debit(run_id, "model", 3)
    lives.credit(life_id, 5, "market", evidence_ref="market:settle:one")
    lives.credit(life_id, 5, "market", evidence_ref="market:settle:one")
    assert lives.status(life_id)["driver"]["reading"] == 12
    assert lives.status(life_id)["remaining_usd"] == 12
    assert world.store.reconcile(run_id)["cap_usd"] == 10
    with pytest.raises(ValueError, match="conflicts"):
        lives.credit(life_id, 6, "market", evidence_ref="market:settle:one")
    with pytest.raises(ValueError, match="declared rail"):
        lives.credit(life_id, 2, "bounty", evidence_ref="bounty:one")


async def test_bankruptcy_cannot_be_rescued_by_income(setup):
    world, lives, _ = setup
    life_id = lives.create(
        {
            "stage": 2,
            "budget": {"usd_life": 1, "wall_life_days": 28},
            "driver": {"metric": "usd_balance"},
            "income_rail": {"kind": "market", "usd_per_credit": 1},
        }
    )["life_id"]
    run_id = session(world, lives, life_id, 1)
    assert world.store.debit(run_id, "model", 1)
    with pytest.raises(ValueError, match="bankrupt"):
        lives.credit(life_id, 2, "market", evidence_ref="market:late-rescue")
    await world.end_run(run_id)
    lives.session_ended(run_id)
    await lives.tasks[life_id + ":bankruptcy"]
    assert lives.get(life_id)["ended"]["reason"] == "bankruptcy"


async def test_fresh_and_resume_continuity_remain_distinct(setup):
    world, lives, _ = setup
    life_id = lives.create({"continuity": "resume_transcript"})["life_id"]
    run_id = session(world, lives, life_id)
    await world.end_run(run_id)
    lives.session_ended(run_id)
    prepared = lives.prepare_session(life_id, {})
    assert prepared["continuity"] == "resume_transcript"
    assert prepared["continuity_source_run_id"] == run_id


async def test_research_life_requires_gate_service(setup):
    _, lives, _ = setup
    with pytest.raises(ValueError, match="registered gate"):
        lives.create({"research_mode": "research"})


async def test_supplied_fingerprint_string_cannot_substitute_for_t0_measurement(setup):
    _, lives, _ = setup
    with pytest.raises(ValueError, match="actual t=0"):
        lives.create({"t0_fingerprint_ref": "sha256:invented"})


async def test_t0_requires_metered_measurements_and_immutable_artifact(setup):
    world, lives, _ = setup
    life_id = lives.create({})["life_id"]
    run_id = world.store.create_run(
        {"budget": {"usd": 1}, "metering_lane": "probe", "model_snapshot_id": "model-pinned"}
    )["run_id"]
    await world.end_run(run_id)
    report = {
        "study": "t0",
        "life_id": life_id,
        "completed": True,
        "scientific": False,
        "model_snapshot_id": "model-pinned",
        "measurement_run_ids": [run_id],
        **{
            key: {"observed": True}
            for key in ("lottery", "leisure", "persona", "attractor", "model_value_document")
        },
    }
    with pytest.raises(ValueError, match="no metered"):
        lives.record_t0(life_id, report)
    measured = world.store.create_run(
        {"budget": {"usd": 1}, "metering_lane": "probe", "model_snapshot_id": "model-pinned"}
    )["run_id"]
    world.store.append_record(
        measured,
        "model_call",
        provider="fixture",
        model="model-pinned",
        usage={"input": 1, "output": 1},
        usd=0,
    )
    await world.end_run(measured)
    report["measurement_run_ids"] = [measured]
    failed = {**report, "lottery": {"status": "failed", "reason": "No actual stake settled"}}
    with pytest.raises(ValueError, match="component is unmeasured"):
        lives.record_t0(life_id, failed)
    registered = lives.record_t0(life_id, report)
    assert registered["t0_fingerprint_ref"] == content_hash(report)
    assert (world.store.data_dir / "lives" / life_id / "t0.json").exists()
    changed = {**report, "bank_hash": "sha256:changed"}
    with pytest.raises(ValueError, match="immutable"):
        lives.record_t0(life_id, changed)
