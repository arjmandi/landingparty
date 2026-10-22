"""Calendar rubric jobs survive restart and never interrupt active sessions."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from worldd.lives import LifeService
from worldd.storage import Store, content_hash


@pytest.fixture
async def checkpoints(tmp_path):
    store = Store(tmp_path)
    clock = SimpleNamespace(now=datetime.now(UTC))
    world = SimpleNamespace(store=store, services={}, jobs={})
    lives = LifeService(world, clock=lambda: clock.now)
    emitted = []

    class Pipeline:
        def has_life_config(self, life_id):
            return True

        async def life(self, life_id, *, checkpoint):
            assert lives.get(life_id)["active_run_id"] is None
            emitted.append(checkpoint)
            report = {
                "life_id": life_id,
                "checkpoint": checkpoint,
                "status": "unmeasured",
                "reason": "No independent fixture observations",
            }
            name = f"lives/{life_id}/rubric/{checkpoint}/fixture.json"
            path = store.data_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")))
            return {"report": report, "artifact_ref": name, "artifact_hash": content_hash(report)}

    world.services.update(lives=lives, pipeline=Pipeline())
    yield world, lives, clock, emitted
    await lives.aclose()
    store.close()


async def test_due_checkpoint_defers_until_session_seals_and_remains_idempotent(checkpoints):
    world, lives, clock, emitted = checkpoints
    life_id = lives.create({"budget": {"usd_life": 20, "wall_life_days": 40}})["life_id"]
    jobs = lives.schedule_rubric(life_id)
    assert set(jobs) == {"d7", "d14", "d28", "end", "after30", "after90"}
    run = world.store.create_run(lives.prepare_session(life_id, {}))["run_id"]
    lives.attach_session(life_id, run)
    clock.now += timedelta(days=7)
    await lives.tick(life_id)
    assert emitted == [] and lives.get(life_id)["rubric_jobs"]["d7"]["state"] == "deferred"
    world.store.append_record(run, "seal")
    world.store.update_run(run, state="sealed")
    lives.session_ended(run)
    await lives.tasks[life_id + ":rubric"]
    await lives.tick(life_id)
    assert emitted == ["d7"]
    assert lives.get(life_id)["rubric_jobs"]["d7"]["state"] == "complete"
    clock.now += timedelta(days=7)
    await lives.tick(life_id)
    assert emitted == ["d7", "d14"]


async def test_restart_recovers_due_readings_and_end_preserves_missing_future_days(checkpoints):
    world, lives, clock, emitted = checkpoints
    life_id = lives.create({"budget": {"usd_life": 20, "wall_life_days": 40}})["life_id"]
    lives.schedule_rubric(life_id)
    await lives.aclose()
    restored = LifeService(world, clock=lambda: clock.now)
    world.services["lives"] = restored
    clock.now += timedelta(days=14)
    try:
        await restored.recover()
        assert emitted == ["d7", "d14"]
        await restored.end(life_id)
        jobs = restored.get(life_id)["rubric_jobs"]
        assert jobs["end"]["state"] == "complete"
        assert jobs["d28"]["state"] == "unmeasured"
        assert jobs["after30"]["due_at"]
        await restored.tick(life_id)
        assert emitted.count("end") == 1
    finally:
        await restored.aclose()


async def test_afterlife_failure_has_durable_retry_and_does_not_claim_completed(checkpoints):
    world, lives, clock, _ = checkpoints
    attempts = []

    class Footprint:
        async def read_life(self, life_id, *, checkpoint):
            attempts.append(checkpoint)
            if len(attempts) == 1:
                raise OSError("fixture unavailable")
            return {"readings": []}

    world.services["footprint"] = Footprint()
    life_id = lives.create({"afterlife_days": [30]})["life_id"]
    await lives.end(life_id)
    clock.now += timedelta(days=30)
    await lives.tick(life_id)
    item = lives.get(life_id)["afterlife"]["30"]
    assert item["state"] == "failed" and item["retry_at"]
    await lives.tick(life_id)
    assert attempts == [30]
    clock.now += timedelta(hours=1)
    await lives.tick(life_id)
    assert attempts == [30, 30]
    assert lives.get(life_id)["rubric_jobs"]["after30"]["state"] == "complete"
