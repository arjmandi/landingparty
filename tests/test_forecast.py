from datetime import UTC, datetime, timedelta

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
