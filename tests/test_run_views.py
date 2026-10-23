"""Operator reads must neither drive agents nor expire or initialize their state."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from worldd.app import create_app
from worldd.models import RunConfig
from worldd.run_views import RunViews


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def viewed(tmp_path):
    world = create_app(tmp_path, gateway=Gateway()).state.world
    try:
        yield world, RunViews(world)
    finally:
        await world.aclose()


def files(root):
    return {
        str(path.relative_to(root)): (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
            path.stat().st_mode,
        )
        for path in root.rglob("*")
        if not path.name.endswith("-shm")
    }


def stored_run(world, **changes):
    config = RunConfig().model_dump()
    changes["budget"] = {**config["budget"], **changes.get("budget", {})}
    return world.store.create_run({**config, "tuple": {}, **changes})["run_id"]


async def test_actual_live_beliefs_asks_footprint_and_ledger_reads_leave_all_state_unchanged(
    viewed, monkeypatch
):
    world, views = viewed
    source = world.create_run(
        RunConfig(
            stage=1,
            packs=["belief"],
            model_snapshot_id="fixture-model",
            reply_policy="blockers_only",
        )
    )["run_id"]
    world.store.append_record(source, "outcome", action_id="a_page")
    response = await world.act(
        source,
        "believe",
        {
            "record": {
                "kind": "fact",
                "claim": "Harbour is open",
                "rank": 4,
                "triple": {"subject": "harbour", "predicate": "status", "object": "open"},
                "evidence": [{"ref": "a_page", "class": "observed"}],
            }
        },
    )
    assert response["kind"] == "result", response
    belief_id = response["data"]["id"]
    service = world.services["beliefs"]
    await service.observe(
        source,
        {
            "observation_ref": "world_changed",
            "triple": {"subject": "harbour", "predicate": "status", "object": "closed"},
            "observed_at": datetime.now(UTC).isoformat(),
        },
    )
    service._stores.clear()  # A UI read must not initialize the normal cache or touch directories.
    asks = world.services["asks"]
    ticket_id = asks.raise_ticket(
        source,
        "a_pending",
        "approval",
        pending_action={"url": "https://example.org/post", "api_key": "private-value"},
    )["ticket_id"]
    asks.clock = lambda: datetime.now(UTC) + timedelta(days=30)
    footprint = world.services["footprint"]
    item = footprint.record_item(
        source,
        "a_post",
        kind="post",
        platform="fixture",
        metadata={"url": "https://example.org/work"},
    )
    footprint.add_reading(item["item_id"], 3, author_class="independent_human")
    world.store.debit(source, "fetch", 0.2)
    before, cursors = files(world.store.data_dir), dict(world.cursors)
    original_cache = dict(service._stores)
    total_changes = world.store.db.total_changes
    # SQLite reader locks update its shared-memory bytes; durable DB/WAL,
    # logs, files, permissions and rows must remain unchanged.
    extension_changes = (
        service.database.db.total_changes,
        asks.database.db.total_changes,
        footprint.db.total_changes,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A view tried to execute a mutating agent/service path")

    with monkeypatch.context() as guard:
        guard.setattr(world, "act", forbidden)
        guard.setattr(world, "status", forbidden)
        guard.setattr(asks, "list_tickets", forbidden)
        guard.setattr(asks, "expire", forbidden)
        guard.setattr(service, "query", forbidden)
        assert views.list_runs()["runs"][0]["pending_asks"] == 1
        detail = views.get(source)
        assert detail["costs"]["usd_total"] == 0.2
        assert detail["status"]["state"] == "running"
        assert views.asks(source)["tickets"][0]["ticket_id"] == ticket_id
        assert views.asks(source)["tickets"][0]["state"] == "open"
        assert views.footprint(source)["items"][0]["readings"][0]["value"] == 3
        assert views.ledger(source)["costs"]["recorded_usd"] == 0.2
        assert views.events(source)["next_seq"] > 0
        beliefs = views.beliefs(source)["beliefs"]
        recorded = next(row for row in beliefs if row["id"] == belief_id)
        assert recorded["kernel"]["classifications"][0]["kind"] in {"revision", "update"}
        assert views.beliefs(source, query="about", entity="harbour")["beliefs"]
        assert (
            views.beliefs(source, query="history", id=belief_id)["beliefs"][0]["claim"]
            == "Harbour is open"
        )
        assert views.beliefs(source, query="why", id=belief_id)["beliefs"][0]["evidence"]
        assert views.beliefs(source, query="why", id="unobserved-id")["beliefs"] == []
    assert world.store.db.total_changes == total_changes
    assert (
        service.database.db.total_changes,
        asks.database.db.total_changes,
        footprint.db.total_changes,
    ) == extension_changes
    assert world.cursors == cursors and service._stores == original_cache
    assert files(world.store.data_dir) == before


async def test_missing_beliefs_are_explicit_and_do_not_create_any_files(viewed):
    world, views = viewed
    source = world.create_run(RunConfig())["run_id"]
    before = files(world.store.data_dir)
    result = views.beliefs(source)
    assert result["beliefs"] == [] and result["available"] is False and result["reason"]
    assert not (world.store.run_dir(source) / "beliefs").exists()
    assert files(world.store.data_dir) == before
    with pytest.raises(ValueError, match="unsupported"):
        views.beliefs(source, query="suggest_slots")


async def test_secret_material_and_raw_model_captures_never_enter_projection(viewed):
    world, views = viewed
    source = stored_run(
        world,
        goal="Visible goal",
        model_snapshot_id="fixture",
        budget={"usd": 5},
        name="Operator test",
        provider_api_key="provider-secret",
        native_auth={"access_token": "native-secret"},
    )
    world.store.append_record(
        source,
        "model_capture",
        request={"authorization": "provider-secret"},
        response="raw model body",
        capture_ref="native-secret",
    )
    world.store.append_record(
        source,
        "model_call",
        model="fixture",
        usage={"input": 10, "output": 5, "debug": "provider-secret"},
    )
    world.store.append_record(
        source,
        "action",
        action_id="a_url",
        tool="fetch",
        params={
            "url": "https://user:password@example.org/page?api_key=provider-secret&visible=yes",
            "env": {"AUTH": "native-secret"},
        },
    )
    world.services["asks"].raise_ticket(
        source,
        "a_approval",
        "approval",
        pending_action={
            "url": "https://user:password@example.org/page?token=native-secret",
            "api_key": "provider-secret",
        },
    )
    result = json.dumps([views.get(source), views.asks(source), views.events(source)])
    assert all(
        secret not in result
        for secret in (
            "provider-secret",
            "native-secret",
            "raw model body",
            "user:password",
            "resume_",
        )
    )
    assert "visible=yes" in result and '"input": 10' in result
    assert views.get(source)["run"]["goal"] == "Visible goal"
    assert views.get(source)["run"]["name"] == "Operator test"


async def test_subscription_has_world_tool_cap_and_unknown_model_dollars(viewed):
    world, views = viewed
    source = stored_run(
        world,
        goal="Native subscription",
        budget={"usd": 5, "wall_s": 300},
        model_billing_mode="subscription",
        model_snapshot_id="native-model",
    )
    world.store.debit(source, "fetch", 0.25)
    world.store.append_record(
        source, "model_call", usage={"input_tokens": 123, "output_tokens": 45}
    )
    detail = views.get(source)
    assert detail["billing"]["mode"] == "subscription"
    assert detail["billing"]["model_cost_measured"] is False
    assert detail["costs"]["usd_total"] is detail["costs"]["model_usd"] is None
    assert detail["costs"]["world_tools_usd"] == 0.25 and detail["costs"]["tokens"]["input"] == 123
    assert (
        detail["limits"]["cap_scope"] == "world_tools" and detail["limits"]["remaining_usd"] == 4.75
    )
    assert views.list_runs()["runs"][0]["spent_usd"] is None
    assert views.get(source)["run"]["name"] == "Native subscription"


async def test_events_are_bounded_cursor_ordered_and_internal_runs_are_not_listed(viewed):
    world, views = viewed
    source = world.create_run(RunConfig())["run_id"]
    for index in range(8):
        world.store.append_record(source, "fixture_observation", index=index)
    child = stored_run(world, budget={"usd": 1}, measurement_of=source)
    first = views.events(source, limit=3)
    second = views.events(source, since=first["next_seq"], limit=3)
    assert first["has_more"] and len(first["events"]) == 3
    assert second["events"][0]["seq"] == first["next_seq"] + 1
    assert child not in {run["run_id"] for run in views.list_runs()["runs"]}
    with pytest.raises(ValueError):
        views.events(source, limit=501)
    with pytest.raises(ValueError):
        views.events(source, since=-1)
    with pytest.raises(KeyError):
        views.get("r_nonexistent")
