import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from worldd.beliefs import BeliefValidationError
from worldd.forecast import ForecastService
from worldd.storage import Store


class WorldFixture:
    def __init__(self, tmp_path):
        self.store = Store(tmp_path)
        self.events = []
        self.services = {}

    def push(self, run_id, event):
        self.events.append((run_id, event))
        if self.store.get_run(run_id)["state"] in {"sealed", "killed", "ended"}:
            return {"deferred": True, **event}
        return self.store.push_event(run_id, event)


@pytest.fixture
def forecasts(tmp_path):
    world = WorldFixture(tmp_path)
    clock = [datetime(2026, 10, 22, 12, tzinfo=UTC)]
    run_id = world.store.create_run({"budget": {"usd": 20}})["run_id"]
    service = ForecastService(world, clock=lambda: clock[0])
    yield world, service, run_id, clock
    service.database.close()
    world.store.close()


async def test_expectation_resolves_once_with_observation_provenance(forecasts):
    world, service, run_id, _ = forecasts
    opened = service.open(
        run_id, {"state": "world.harbour.status", "value": "open", "p": 0.8, "resolver": "watch"}
    )
    outcome = await service.observe(
        run_id, "world.harbour.status", "open", "o_page", resolver="watch"
    )
    assert outcome[0]["outcome"] == "held"
    assert outcome[0]["observation_ref"] == "o_page"
    assert service.get(opened["id"])["grounded"] is True
    assert len(world.events) == 1
    assert (
        await service.observe(run_id, "world.harbour.status", "open", "o_page", resolver="watch")
        == []
    )
    assert world.store.total_spent(run_id) == 0


async def test_human_expectations_do_not_enter_grounded_column(forecasts):
    _, service, run_id, _ = forecasts
    expectation = service.open(
        run_id, {"state": "ask.t_1.outcome", "value": "approved", "resolver": "human"}
    )
    await service.observe(run_id, "ask.t_1.outcome", "approved", "o_human", resolver="human")
    assert service.get(expectation["id"])["grounded"] is False


async def test_predict_captures_registered_community_median_and_resolver(forecasts):
    _, service, run_id, clock = forecasts
    service.register_question(
        "q_launch",
        {
            "state": "world.launch",
            "binary": True,
            "resolver": "watch",
            "community_median": 0.6,
            "resolve_at": (clock[0] + timedelta(days=1)).isoformat(),
        },
    )
    reply = await service.handle(
        run_id, "a_prediction", "predict", {"question_id": "q_launch", "value_or_p": 0.7}
    )
    assert reply["kind"] == "silence"
    pending = service.get(reply["expectation_id"])
    assert pending["community_median"] == 0.6 and pending["p"] == 0.7
    assert await service.observe(run_id, "world.launch", False, "o_early", resolver="watch") == []
    clock[0] += timedelta(days=1)
    assert (await service.observe(run_id, "world.launch", True, "o_launch", resolver="watch"))[0][
        "outcome"
    ] == "held"


async def test_pending_survives_seal_and_service_reconstruction(forecasts):
    world, service, run_id, clock = forecasts
    expectation = service.open(run_id, {"state": "world.balance", "cmp": "gte", "value": 10})
    world.store.append_record(run_id, "seal")
    world.store.update_run(run_id, state="sealed")
    original = (world.store.run_dir(run_id) / "record.jsonl").read_bytes()
    rebuilt = ForecastService(world, clock=lambda: clock[0])
    try:
        assert rebuilt.pending(run_id)[0]["id"] == expectation["id"]
        await rebuilt.observe(run_id, "world.balance", 15, "o_late")
        assert rebuilt.get(expectation["id"])["outcome"] == "held"
        assert (world.store.run_dir(run_id) / "record.jsonl").read_bytes() == original
        assert rebuilt.database.verify(run_id)["valid"]
    finally:
        rebuilt.database.close()


async def test_horizon_plus_grace_attempt_then_expiry(forecasts):
    _, service, run_id, clock = forecasts
    expectation = service.open(run_id, {"state": "world.never_written", "horizon": 1})
    clock[0] += timedelta(days=1, hours=23)
    assert await service.tick() == []
    clock[0] += timedelta(hours=1)
    result = await service.tick()
    assert result[0]["outcome"] == "expired"
    assert service.get(expectation["id"])["reason"] == "unresolved_after_grace"


async def test_resolver_retries_after_failure_without_inventing_measurement(forecasts):
    _, service, run_id, clock = forecasts
    calls = []

    def reader(*args):
        calls.append(args)
        if len(calls) == 1:
            raise ValueError("not yet parseable")
        return {"value": 7, "observation_ref": "o_retry"}

    service.state_reader = reader
    expectation = service.open(run_id, {"state": "world.number", "value": 7})
    assert await service.tick() == []
    assert service.get(expectation["id"])["outcome"] == "open"
    clock[0] += timedelta(hours=1)
    assert (await service.tick())[0]["outcome"] == "held"


async def test_world_event_adapter_resolves_status_and_ticket_namespaces(forecasts):
    _, service, run_id, _ = forecasts
    status = service.open(run_id, {"state": "world.last.status", "value": 200})
    await service.observe_event(
        run_id, {"action_id": "a_page", "tool": "fetch", "reply": {"status": 200}}
    )
    assert service.get(status["id"])["outcome"] == "held"
    ticket = service.open(
        run_id, {"state": "ask.t_access.outcome", "value": "approved", "resolver": "ticket"}
    )
    await service.observe_event(
        run_id, {"type": "ask_resolved", "ticket_id": "t_access", "state": "approved"}
    )
    assert service.get(ticket["id"])["outcome"] == "held"


@pytest.mark.parametrize(
    "record",
    [
        {"state": "https://example.com", "value": 1},
        {"state": "world.x", "p": float("nan")},
        {"state": "world.x", "p": 1.1},
        {"state": "world.x", "horizon": 61},
        {"state": "world.x", "recorded_at": "2026-10-01T00:00:00Z"},
        {"state": "world.x", "community_median": 0.5},
    ],
)
def test_expectation_structural_validation(forecasts, record):
    _, service, run_id, _ = forecasts
    with pytest.raises(BeliefValidationError):
        service.open(run_id, record)
    assert service.pending(run_id) == []


async def test_registered_json_resolver_uses_real_kernel_child_after_parent_seal(tmp_path):
    from worldd.app import create_app
    from worldd.gateway import Gateway
    from worldd.models import RunConfig

    async def dns(host, port):
        return ["93.184.216.34"]

    def fixture(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, json={"harbour": {"open": True}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
        gateway = Gateway(tmp_path, {"domain_interval_s": 0}, client=client, resolver=dns)
        app = create_app(tmp_path, gateway=gateway)
        world = app.state.world
        try:
            source = world.create_run(RunConfig(packs=["forecast"]))["run_id"]
            forecast = world.services["forecast"]
            expectation = forecast.open(source, {"state": "world.harbour.open", "value": True})
            forecast.bind_state(
                source,
                "world.harbour.open",
                "fetch",
                {
                    "url": "https://example.com/state.json",
                    "selector": ["harbour", "open"],
                },
            )
            await world.end_run(source)
            original = (world.store.run_dir(source) / "record.jsonl").read_bytes()
            resolved = await forecast.tick()
            assert resolved[0]["id"] == expectation["id"]
            assert resolved[0]["outcome"] == "held"
            assert (world.store.run_dir(source) / "record.jsonl").read_bytes() == original
            assert world.kernel_runs[source] != source
        finally:
            await world.aclose()


async def test_pending_forecast_resolves_from_next_same_life_session(forecasts):
    world, service, _, _ = forecasts
    old = world.store.create_run({"budget": {"usd": 1}, "life_id": "l_forecast"})["run_id"]
    current = world.store.create_run({"budget": {"usd": 1}, "life_id": "l_forecast"})["run_id"]
    expected = service.open(
        old, {"state": "driver.balance", "value": 20, "cmp": "gte", "resolver": "inbox"}
    )
    world.store.update_run(old, state="sealed")
    results = await service.observe(
        current, "driver.balance", 25, "o_later_balance", resolver="inbox"
    )
    assert results[0]["id"] == expected["id"] and results[0]["outcome"] == "held"


async def test_forecast_captures_updated_provider_median_on_each_prediction(forecasts):
    _, service, run_id, clock = forecasts
    medians = iter([0.4, 0.7])

    async def observed(run_id, question_id, question):
        return {
            "value": next(medians),
            "observed_at": clock[0].isoformat(),
            "observation_ref": "o_median",
        }

    service.median_reader = observed
    service.register_question(
        "q_live", {"state": "world.q_live", "binary": True, "resolver": "watch"}
    )
    one = await service.handle(
        run_id, "a_p1", "predict", {"question_id": "q_live", "value_or_p": 0.5}
    )
    two = await service.handle(
        run_id, "a_p2", "predict", {"question_id": "q_live", "value_or_p": 0.6}
    )
    assert service.get(one["expectation_id"])["community_median"] == 0.4
    assert service.get(two["expectation_id"])["community_median"] == 0.7
    assert service.get(two["expectation_id"])["community_median_provenance"]["captured_at_call"]


async def test_out_of_order_forecast_evidence_replays_head_without_editing_old_resolution(
    forecasts,
):
    world, service, run_id, clock = forecasts
    opened_at = clock[0]
    expectation = service.open(run_id, {"state": "world.open", "value": True})
    clock[0] += timedelta(days=2)
    await service.observe(run_id, "world.open", True, "o_newer")
    clock[0] += timedelta(days=1)
    correction = await service.observe(
        run_id, "world.open", False, "o_earlier", observed_at=opened_at + timedelta(days=1)
    )
    assert correction[0]["outcome"] == "missed"
    assert service.get(expectation["id"])["observation_ref"] == "o_earlier"
    resolutions = [
        row
        for row in service.database.journal(run_id)
        if row["kind"].startswith("expectation_resol")
    ]
    assert [row["expectation"]["outcome"] for row in resolutions] == ["held", "missed"]
    assert resolutions[-1]["kind"] == "expectation_resolution_replayed"
    assert service.get(expectation["id"])["valid_from"] == expectation["opened_at"]
    assert len(world.events) == 2


async def test_approval_replays_pending_condition_and_late_denial_replays_void(forecasts):
    _, service, run_id, clock = forecasts
    beginning = clock[0]
    expectation = service.open(
        run_id, {"state": "world.work_done", "value": True, "assumptions": ["t_permission"]}
    )
    clock[0] += timedelta(hours=1)
    assert await service.observe(run_id, "world.work_done", True, "o_done") == []
    clock[0] += timedelta(hours=1)
    approved = await service.observe(
        run_id, "ask.t_permission.outcome", "approved", "o_approved", resolver="ticket"
    )
    assert approved[0]["outcome"] == "held" and approved[0]["observation_ref"] == "o_done"
    clock[0] += timedelta(hours=1)
    await service.observe(
        run_id,
        "ask.t_permission.outcome",
        "rejected",
        "o_denied_earlier",
        resolver="ticket",
        observed_at=beginning + timedelta(minutes=30),
    )
    assert service.get(expectation["id"])["outcome"] == "void"


async def test_final_grace_fetch_keeps_actual_time_and_excludes_late_headline(forecasts):
    _, service, run_id, clock = forecasts
    expectation = service.open(run_id, {"state": "world.final", "value": True, "horizon": 1})
    clock[0] += timedelta(days=2)
    scheduled = clock[0]
    calls = []

    def reader(*args):
        calls.append(args)
        clock[0] += timedelta(milliseconds=25)
        return {"value": True, "observation_ref": "o_final"}

    service.state_reader = reader
    outcomes = await service.tick()
    assert outcomes[0]["outcome"] == "held"
    assert outcomes[0]["scheduled_grace_at"] == scheduled.isoformat(timespec="microseconds")
    assert outcomes[0]["resolution_observed_at"] == clock[0].isoformat(timespec="microseconds")
    assert outcomes[0]["late_final"] and outcomes[0]["headline_eligible"] is False
    assert outcomes[0]["late_by_seconds"] == 0.025
    assert service.get(expectation["id"])["outcome"] == "held"
    assert await service.tick() == [] and len(calls) == 1


async def test_months_late_scheduler_expires_without_fetching_a_fake_historical_result(forecasts):
    _, service, run_id, clock = forecasts
    service.open(run_id, {"state": "world.old", "value": True, "horizon": 1})
    calls = []
    service.state_reader = lambda *args: calls.append(args) or {"value": True}
    clock[0] += timedelta(days=60)
    outcomes = await service.tick()
    assert (
        outcomes[0]["outcome"] == "expired"
        and outcomes[0]["reason"] == "scheduler_late_after_grace"
    )
    assert not calls


async def test_all_declared_local_namespace_surfaces_use_scoped_production_readers(forecasts):
    world, service, _, clock = forecasts
    run_id = world.store.create_run({"budget": {"usd": 20}, "life_id": "l_namespace"})["run_id"]
    foreign = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    requested = []

    def account(alias, *, run_id):
        requested.append((alias, run_id))
        return {"state": "active"}

    world.services.update(
        vault=SimpleNamespace(get_account=account),
        lives=SimpleNamespace(
            status=lambda life_id: {"driver": {"metric": "usd_balance", "reading": 7}}
        ),
        asks=SimpleNamespace(
            get=lambda ticket: {
                "run_id": run_id if ticket == "t_own" else foreign,
                "state": "approved",
            }
        ),
        footprint=SimpleNamespace(
            query=lambda run, venue: [
                {
                    "item_id": "fp_owned",
                    "external_id": "article",
                    "readings": [
                        {"reading_id": "fr_one", "at": clock[0].isoformat(), "value": {"views": 42}}
                    ],
                }
            ]
        ),
    )
    expected = {
        "world.cap": (20, "inbox"),
        "driver.usd_balance": (7, "inbox"),
        "account.public_alias.state": ("active", "inbox"),
        "ask.t_own.outcome": ("approved", "ticket"),
        "ticket.t_own.state": ("approved", "ticket"),
        "footprint.blog.article.views": (42, "footprint_probe"),
    }
    ids = [
        service.open(run_id, {"state": state, "value": value, "resolver": resolver})["id"]
        for state, (value, resolver) in expected.items()
    ]
    await service.tick()
    assert all(service.get(key)["outcome"] == "held" for key in ids)
    assert requested == [("public_alias", run_id)]
    assert service._local_state(run_id, "ask.t_foreign.outcome") is None


@pytest.mark.parametrize(
    "state",
    [
        "world.",
        "driver.",
        "ticket.id",
        "ask.id.state",
        "account.alias.password",
        "footprint.venue.object",
        "world..x",
        "https://example.com",
    ],
)
def test_namespace_shapes_reject_nonaddressable_or_secret_fields(forecasts, state):
    _, service, run_id, _ = forecasts
    with pytest.raises(BeliefValidationError):
        service.open(run_id, {"state": state, "value": True})


async def test_continuous_predictions_and_intervals_are_distinct_from_binary_probabilities(
    forecasts,
):
    _, service, run_id, _ = forecasts
    service.register_question("q_number", {"state": "world.temperature", "binary": False})
    result = await service.handle(
        run_id,
        "a_number",
        "predict",
        {"question_id": "q_number", "value_or_p": -10, "interval": [-12, -8]},
    )
    assert result["kind"] == "silence" and service.get(result["expectation_id"])["value"] == -10
    invalid = await service.handle(
        run_id,
        "a_bad_interval",
        "predict",
        {"question_id": "q_number", "value_or_p": -10, "interval": [5, 1]},
    )
    assert invalid["kind"] == "blocked"


async def test_sync_state_hook_captures_each_write_before_observer_tasks_run(forecasts):
    import asyncio

    world, service, run_id, clock = forecasts
    reading, tasks = ["active"], []
    world.services["vault"] = SimpleNamespace(
        get_account=lambda alias, *, run_id: {"state": reading[0]}
    )
    world.track_observer = lambda owner, job: tasks.append(asyncio.create_task(job))
    expectation = service.open(
        run_id, {"state": "account.mine.state", "value": "active", "resolver": "inbox"}
    )
    assert service.notify_state_write(run_id=run_id, world_ref="account_write_1") == {
        "queued_n": 1,
        "scheduled": True,
    }
    clock[0] += timedelta(seconds=1)
    reading[0] = "revoked"
    service.notify_state_write(run_id=run_id, world_ref="account_write_2")
    await asyncio.gather(*tasks)
    assert service.get(expectation["id"])["outcome"] == "held"
    observations = [
        json.loads(row[0])
        for row in service.database.db.execute(
            "SELECT payload FROM expectation_observations ORDER BY observed_at"
        )
    ]
    assert [row["value"] for row in observations] == ["active", "revoked"]
    assert [row["world_ref"] for row in observations] == ["account_write_1", "account_write_2"]
    assert observations[0]["observed_at"] != observations[1]["observed_at"]


async def test_sync_state_hook_is_durable_scoped_and_ignores_human_resolvers(forecasts):
    world, service, _, clock = forecasts
    own = world.store.create_run({"budget": {"usd": 20}, "life_id": "l_own"})["run_id"]
    sibling = world.store.create_run({"budget": {"usd": 20}, "life_id": "l_own"})["run_id"]
    foreign = world.store.create_run({"budget": {"usd": 20}, "life_id": "l_other"})["run_id"]
    world.services["lives"] = SimpleNamespace(
        status=lambda life: {"driver": {"metric": "balance", "reading": 7}}
    )
    expected = [
        service.open(run, {"state": "driver.balance", "value": 7})["id"]
        for run in (own, sibling, foreign)
    ]
    human = service.open(own, {"state": "driver.balance", "value": 7, "resolver": "human"})
    assert service.notify_state_write(life_id="l_own", world_ref="driver_write") == {
        "queued_n": 2,
        "scheduled": False,
    }
    with pytest.raises(BeliefValidationError, match="disagree"):
        service.notify_state_write(run_id=own, life_id="l_other", world_ref="wrong_scope")
    restored = ForecastService(world, clock=lambda: clock[0])
    try:
        await restored._drain_state_writes()
        assert [restored.get(key)["outcome"] for key in expected] == ["held", "held", "open"]
        assert restored.get(human["id"])["outcome"] == "open"
        assert (
            restored.database.db.execute(
                "SELECT COUNT(*) FROM expectation_state_write_queue"
            ).fetchone()[0]
            == 0
        )
    finally:
        restored.database.close()


async def test_registered_resolver_rejects_changed_captured_bytes(forecasts):
    from worldd.util import digest

    world, service, run_id, _ = forecasts

    async def captured(owner, url):
        return {"kind": "result", "body": '{"value":false}', "hash": digest(b'{"value":true}')}

    world.fetch_kernel = captured
    service.bind_state(
        run_id, "world.report", "fetch", {"url": "https://example.com", "selector": ["value"]}
    )
    expectation = service.open(run_id, {"state": "world.report", "value": False})
    assert await service.tick() == []
    assert service.get(expectation["id"])["outcome"] == "open"


@pytest.mark.parametrize("median", [float("nan"), float("inf"), True, "0.6", 1.1])
def test_registered_binary_median_cannot_forge_invalid_calibration_inputs(forecasts, median):
    _, service, _, _ = forecasts
    with pytest.raises(BeliefValidationError, match="community median"):
        service.register_question("q_bad", {"state": "world.q", "community_median": median})


@pytest.mark.parametrize(
    "author,eligible",
    [
        ("probe", False),
        ("sponsor", False),
        ("sibling", False),
        ("unknown", False),
        ("independent_human", True),
        ("arena_participant", False),
        ("arena_participant", True),
    ],
)
async def test_local_footprint_resolution_keeps_actual_author_eligibility(
    forecasts, author, eligible
):
    world, service, run_id, clock = forecasts
    world.services["footprint"] = SimpleNamespace(
        query=lambda owner, venue: [
            {
                "item_id": "fp_owned",
                "readings": [
                    {
                        "reading_id": "fr_actual",
                        "at": clock[0].isoformat(),
                        "value": {"views": 10},
                        "author_class": author,
                        "grounded_eligible": eligible,
                    }
                ],
            }
        ]
    )
    expectation = service.open(
        run_id,
        {"state": "footprint.blog.fp_owned.views", "value": 10, "resolver": "footprint_probe"},
    )
    service.notify_state_write(run_id=run_id, world_ref="fr_actual")
    await service.tick()
    resolved = service.get(expectation["id"])
    assert resolved["outcome"] == "held"
    assert resolved["grounded"] is eligible and resolved["author_class"] == author
    assert resolved["reading_ref"] == "fr_actual"
