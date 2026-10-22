import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from worldd.kernel import BeliefService
from worldd.storage import Store


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 22, 12, tzinfo=UTC)]
    monkeypatch.setattr("worldd.storage.utc_now", lambda: clock[0].isoformat())
    monkeypatch.setattr("worldd.kernel.now", lambda: clock[0].isoformat())
    store = Store(tmp_path)
    world = SimpleNamespace(store=store, services={})
    world.push = lambda run_id, event: store.push_event(run_id, event)
    run_id = store.create_run({"budget": {"usd": 1}, "reflection_policy": "none"})["run_id"]
    service = BeliefService(world, clock=lambda: clock[0])
    store.append_record(run_id, "outcome", action_id="a_source")
    belief = service.beliefs(run_id).believe(
        {
            "kind": "fact",
            "claim": "Harbour open",
            "triple": {"subject": "harbour", "predicate": "status", "object": "open"},
            "rank": 4,
            "change_class": "fast",
            "evidence": [{"ref": "a_source", "class": "observed"}],
        }
    )
    yield world, service, run_id, belief, clock
    service.database.close()
    store.close()


def observation(ref, value, observed_at, **extra):
    return {
        "observation_ref": ref,
        "triple": {"subject": "harbour", "predicate": "status", "object": value},
        "observed_at": observed_at.isoformat(),
        **extra,
    }


async def test_changes_open_world_intervals_and_reattach_after_original_closes(kernel):
    _, service, run_id, original, clock = kernel
    first_at = clock[0] + timedelta(days=1)
    clock[0] = first_at
    await service.observe(
        run_id,
        observation(
            "o_closed",
            "closed",
            first_at,
            **{
                "class": "fetched_content",
                "provenance": {"origin": "harbour.example"},
                "last_modified": "1900-01-01T00:00:00Z",
            },
        ),
    )
    clock[0] += timedelta(hours=1)
    await service.observe(run_id, observation("o_still_closed", "closed", clock[0]))
    clock[0] += timedelta(hours=1)
    await service.observe(run_id, observation("o_reopened", "open", clock[0]))
    folded = service.fold(run_id)[0]
    intervals = folded["kernel"]["observed_intervals"]
    assert len(intervals) == 2
    assert intervals[0]["value"] == "closed" and intervals[0]["valid_from"] == first_at.isoformat(
        timespec="microseconds"
    )
    assert (
        intervals[0]["valid_to"]
        == intervals[1]["valid_from"]
        == clock[0].isoformat(timespec="microseconds")
    )
    assert intervals[1]["value"] == "open" and intervals[1]["valid_to"] is None
    assert [row["observation_ref"] for row in intervals[0]["observations"]] == [
        "o_closed",
        "o_still_closed",
    ]
    assert intervals[0]["observations"][0]["provenance"] == {"origin": "harbour.example"}
    assert intervals[0]["observations"][0]["class"] == "fetched_content"
    assert all(row["mechanical"] and not row["agent_endorsed"] for row in intervals)
    assert {key: folded[key] for key in ("id", "claim", "triple", "rank", "status")} == {
        key: original[key] for key in ("id", "claim", "triple", "rank", "status")
    }
    assert folded["valid_to"] == first_at.isoformat(timespec="microseconds")
    assert service.fold(run_id, valid_at=clock[0]) == []
    assert len(service.beliefs(run_id).history(original["id"])) == 1


async def test_out_of_order_actual_values_reconstruct_exclusive_intervals_and_as_of(kernel):
    _, service, run_id, _, clock = kernel
    initial = clock[0]
    clock[0] = initial + timedelta(days=3)
    await service.observe(run_id, observation("o_3", "frozen", clock[0]))
    before_late = copy.deepcopy(service.fold(run_id))
    cutoff = clock[0]
    clock[0] += timedelta(days=1)
    await service.observe(run_id, observation("o_1", "closed", initial + timedelta(days=1)))
    clock[0] += timedelta(days=1)
    await service.observe(run_id, observation("o_2", "open", initial + timedelta(days=2)))
    assert service.fold(run_id, as_of=cutoff) == before_late
    rows = service.fold(run_id)[0]["kernel"]["observed_intervals"]
    assert [row["value"] for row in rows] == ["closed", "open", "frozen"]
    assert (
        rows[0]["valid_to"] == rows[1]["valid_from"]
        and rows[1]["valid_to"] == rows[2]["valid_from"]
    )
    assert rows[-1]["valid_to"] is None
    observations = service._observations(run_id)
    assert service.fold(run_id, _observation_rows=list(reversed(observations))) == service.fold(
        run_id
    )


async def test_missing_value_never_fabricates_replacement_and_static_is_revision(kernel):
    _, service, run_id, belief, clock = kernel
    clock[0] += timedelta(days=1)
    await service.observe(
        run_id, {"observation_ref": "o_unknown", "belief_id": belief["id"], "supports": False}
    )
    assert service.fold(run_id)[0]["kernel"]["observed_intervals"] == []
    service.beliefs(run_id).revise(belief["id"], {"change_class": "static"})
    clock[0] += timedelta(hours=1)
    await service.observe(
        run_id, observation("o_static", "closed", clock[0], belief_id=belief["id"])
    )
    folded = service.fold(run_id)[0]
    assert folded["kernel"]["classifications"][-1]["kind"] == "revision"
    assert folded["kernel"]["observed_intervals"] == []


async def test_equal_time_changes_keep_evidence_and_continuity_is_exact(kernel):
    world, service, source, _, clock = kernel
    clock[0] += timedelta(days=1)
    await service.observe(source, observation("o_z", "frozen", clock[0]))
    await service.observe(source, observation("o_a", "closed", clock[0]))
    before = service.fold(source)
    rows = before[0]["kernel"]["observed_intervals"]
    assert [row["observation_ref"] for row in rows] == ["o_a", "o_z"]
    assert rows[0]["valid_from"] == rows[0]["valid_to"] == rows[1]["valid_from"]
    target = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    service.import_continuity(target, service.export_continuity(source))
    assert service.fold(target) == before
    pattern_target = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    service.import_bundle(pattern_target, service.export_bundle(source))
    assert service.fold(pattern_target)[0]["kernel"] == before[0]["kernel"]


async def test_explicit_value_observation_is_metadata_without_invented_triple(kernel):
    _, service, run_id, belief, clock = kernel
    clock[0] += timedelta(hours=1)
    await service.observe(
        run_id,
        {
            "observation_ref": "o_value",
            "belief_id": belief["id"],
            "value": {"status": "offline"},
            "supports": False,
        },
    )
    record = service.fold(run_id)[0]
    interval = record["kernel"]["observed_intervals"][0]
    assert interval["value"] == {"status": "offline"} and "triple" not in interval
    assert record["triple"] == belief["triple"] and record["rank"] == belief["rank"]
