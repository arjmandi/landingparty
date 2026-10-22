"""Durable scheduled judging with real ASGI metering and local fixture responses."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from analysis.judges import MeteredResearchClient
from worldd.app import create_app
from worldd.judge_canaries import JudgeCanaryService
from worldd.models import RunConfig
from worldd.util import digest


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def measured(tmp_path, monkeypatch):
    clock, requests, mode = [datetime.now(UTC)], [], {"position_bias": False, "failure": False}
    monkeypatch.setattr("worldd.storage.utc_now", lambda: clock[0].isoformat())

    def provider(request):
        body = json.loads(request.content)
        data = json.loads(body["messages"][-1]["content"])
        requests.append((body, data))
        if mode["failure"]:
            return httpx.Response(500, json={"error": "Local fixture failure"})
        winner = "A" if mode["position_bias"] or "accurate" in data["artifact_A"] else "B"
        return httpx.Response(
            200,
            json={
                "model": "canary-judge",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"winner": winner, "reason": "Local fixture comparison"}
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )

    prices = {
        "models": {
            "canary-judge": {
                "provider": "openai",
                "input": 1,
                "output": 1,
                "context_window": 2048,
                "max_output_tokens": 1000,
            }
        }
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        app = create_app(tmp_path, gateway=Gateway(), lane_client=client, prices=prices)
        world = app.state.world
        source = world.create_run(RunConfig(packs=["deliver"], model_snapshot_id="agent-pinned"))[
            "run_id"
        ]
        root = world.store.run_dir(source)
        (root / "work/out/left.txt").write_text(
            "An accurate explanation supported by the observed facts."
        )
        (root / "work/out/right.txt").write_text(
            "An unsupported explanation with no evidence at all."
        )
        await world.end_run(source)
        protocol = {
            "protocol_id": "fixture-pair@1",
            "model_spec": {"model": "canary-judge", "family": "other-family", "human_agreement": 1},
            "agent_family": "agent-family",
            "criterion": "Factual support",
            "expected_winner": "left",
            "max_tokens": 100,
            **{
                side + "_artifact": {
                    "path": f"work-snapshot/out/{side}.txt",
                    "hash": digest((root / f"work-snapshot/out/{side}.txt").read_bytes()),
                }
                for side in ("left", "right")
            },
        }
        raw = json.dumps(protocol).encode()
        (root / "judge-pair.json").write_bytes(raw)
        instrument = {"path": "judge-pair.json", "hash": digest(raw)}
        original = world.services.get("judge_canaries")
        if original:
            await original.aclose()
        model = MeteredResearchClient.for_app(world, app)
        service = JudgeCanaryService(world, model=model, clock=lambda: clock[0], poll_seconds=60)
        service._owns_model = True
        world.services["judge_canaries"] = service
        yield world, service, source, clock, requests, mode, instrument
        await world.aclose()


def config(instrument, **changes):
    return {
        "instrument": instrument,
        "interval_s": 60,
        "per_check_budget_usd": 0.02,
        "lifetime_budget_usd": 0.06,
        "max_checks": 3,
        "enabled": True,
        **changes,
    }


async def test_explicit_authorization_due_time_singleflight_actual_swaps_and_parent_immutability(
    measured,
):
    world, service, source, clock, requests, _, instrument = measured
    source_records, source_spend = world.store.records(source), world.store.spend(source)
    schedule = service.register(source, config(instrument, enabled=False))
    assert requests == [] and await service.run_due() == []
    service.set_enabled(schedule["schedule_id"], True)
    batches = await asyncio.gather(service.run_due(), service.run_due())
    reports = [report for batch in batches for report in batch]
    assert len(reports) == 1 and len(requests) == 2
    report = reports[0]
    assert report["winner"] == "left" and not report["position_disagreement"]
    assert report["drift_from_previous"] is None and report["drift_from_expected"] is False
    assert report["column"] == "J" and report["calibration_inferred"] is False
    assert report["spent_nano"] > 0 and service.get(schedule["schedule_id"])["held_usd"] == 0
    a, b = requests[0][1], requests[1][1]
    assert a["artifact_A"] == b["artifact_B"] and a["artifact_B"] == b["artifact_A"]
    assert len(a["artifact_A"]) == len(a["artifact_B"])
    assert set(a) == {"artifact_A", "artifact_B", "criterion"}
    assert not requests[0][0].get("tools") and report["style_control"]["hash"]
    assert service.reports_for_run(source)[0]["observed_at"] == clock[0].isoformat(
        timespec="microseconds"
    )
    assert await service.run_due() == []
    calls_before_report = len(requests)
    published = world.report(source)
    assert published["judge_canary_checks"] == service.reports_for_run(source)
    assert (
        published["spec_sheet"]["measurements"]["judge_canary_checks"]
        == published["judge_canary_checks"]
    )
    assert len(requests) == calls_before_report
    assert (
        world.store.records(source) == source_records and world.store.spend(source) == source_spend
    )
    assert {row["measurement"]["measurement_run_id"] for row in report["judgments"]}.isdisjoint(
        {source}
    )


async def test_actual_drift_position_disagreement_timestamps_and_count_cap(measured):
    _, service, source, clock, requests, mode, instrument = measured
    schedule = service.register(source, config(instrument, max_checks=2))
    first = (await service.run_due())[0]
    clock[0] += timedelta(seconds=60)
    mode["position_bias"] = True
    second = (await service.run_due())[0]
    assert second["observed_at"] != first["observed_at"]
    assert (
        second["position_disagreement"]
        and second["drift_from_expected"]
        and second["drift_from_previous"]
    )
    assert service.get(schedule["schedule_id"])["last_check_at"] == second["observed_at"]
    clock[0] += timedelta(seconds=60)
    assert await service.run_due() == [] and len(requests) == 4
    assert service.get(schedule["schedule_id"])["stop_reason"] == "authorized_limit_reached"


async def test_lifetime_cap_reserves_provider_maximum_and_duplicate_registration_cannot_reset_it(
    measured,
):
    _, service, source, clock, requests, _, instrument = measured
    body = config(instrument, lifetime_budget_usd=0.02, max_checks=100)
    schedule = service.register(source, body)
    await service.run_due()
    same = service.register(source, body)
    assert same["schedule_id"] == schedule["schedule_id"] and same["attempts"] == 1
    clock[0] += timedelta(seconds=60)
    assert await service.run_due() == [] and len(requests) == 2
    assert service.get(schedule["schedule_id"])["spent_usd"] <= 0.02


async def test_restart_keeps_ambiguous_ceiling_and_never_resamples_claimed_check(measured):
    world, service, source, clock, requests, _, instrument = measured
    schedule = service.register(source, config(instrument, lifetime_budget_usd=0.04))
    claim = service._claim(schedule["schedule_id"])
    assert claim and service.get(schedule["schedule_id"])["held_usd"] == 0.02
    model = service.model
    service._owns_model = False
    await service.aclose()
    rebuilt = JudgeCanaryService(world, model=model, clock=lambda: clock[0], poll_seconds=60)
    rebuilt._owns_model = True
    world.services["judge_canaries"] = rebuilt
    await rebuilt.recover()
    assert await rebuilt.run_due() == [] and not requests
    current = rebuilt.get(schedule["schedule_id"])
    assert (
        current["held_usd"] == 0.02 and current["attempts"] == 1 and current["active_check"] is None
    )
    clock[0] += timedelta(seconds=60)
    assert len(await rebuilt.run_due()) == 1 and len(requests) == 2
    clock[0] += timedelta(seconds=60)
    assert await rebuilt.run_due() == [] and len(requests) == 2
    assert (
        rebuilt.db.execute(
            "SELECT status FROM judge_canary_checks WHERE check_id=?", (claim["check_id"],)
        ).fetchone()[0]
        == "interrupted"
    )


async def test_failure_preserves_authorization_and_cannot_fabricate_a_check_date(measured):
    _, service, source, clock, requests, mode, instrument = measured
    schedule = service.register(source, config(instrument, lifetime_budget_usd=0.02))
    mode["failure"] = True
    report = (await service.run_due())[0]
    assert report["status"] == "unmeasured"
    current = service.get(schedule["schedule_id"])
    assert current["held_usd"] == 0.02 and current["last_check_at"] is None
    clock[0] += timedelta(seconds=60)
    assert await service.run_due() == [] and len(requests) == 2


async def test_pair_and_protocol_tampering_disable_checks_before_purchase(measured):
    world, service, source, _, requests, _, instrument = measured
    schedule = service.register(source, config(instrument))
    path = world.store.run_dir(source) / "work-snapshot/out/left.txt"
    path.chmod(0o600)
    path.write_text("Changed after sealed snapshot")
    assert await service.run_due() == [] and not requests
    current = service.get(schedule["schedule_id"])
    assert not current["enabled"] and current["attempts"] == 0 and current["held_usd"] == 0


async def test_invalid_authorization_cross_family_and_shutdown_cannot_buy_calls(measured):
    world, service, source, _, requests, _, instrument = measured
    for changes in (
        {"per_check_budget_usd": 0},
        {"lifetime_budget_usd": 0.001},
        {"max_checks": True},
        {"interval_s": 0},
        {"enabled": "yes"},
    ):
        with pytest.raises((ValueError, TypeError)):
            service.register(source, config(instrument, **changes))
    service.register(source, config(instrument))
    world.closing = True
    assert await service.run_due() == []
    world.closing = False
    raw = json.loads((world.store.run_dir(source) / instrument["path"]).read_bytes())
    raw["agent_family"] = "other-family"
    body = json.dumps(raw).encode()
    (world.store.run_dir(source) / "same-family.json").write_bytes(body)
    with pytest.raises(ValueError, match="different model family"):
        service.register(source, config({"path": "same-family.json", "hash": digest(body)}))
    assert not requests


async def test_owned_model_closed_and_unconfigured_model_never_claims_authorization(measured):
    _, service, source, _, requests, _, instrument = measured
    service.register(source, config(instrument))
    model, service.model = service.model, None
    assert await service.run_due() == [] and not requests
    assert service.list(source)[0]["attempts"] == 0
    service.model = model
    await service.aclose()
    assert model.client.is_closed


async def test_latest_failed_attempt_is_not_replaced_by_an_old_successful_check(measured):
    _, service, source, clock, _, mode, instrument = measured
    service.register(source, config(instrument))
    first = (await service.run_due())[0]
    clock[0] += timedelta(seconds=60)
    mode["failure"] = True
    await service.run_due()
    latest = service.reports_for_run(source)[0]
    assert latest["status"] == "unmeasured" and latest["critique_artifact"]
    assert latest["last_successful_check_at"] == first["observed_at"]
    assert latest["last_attempt_at"] == clock[0].isoformat(timespec="microseconds")


async def test_saved_critique_tampering_withdraws_measured_status(measured):
    world, service, source, _, _, _, instrument = measured
    service.register(source, config(instrument))
    result = (await service.run_due())[0]
    path = world.store.run_dir(source) / result["artifact"]["path"]
    path.chmod(0o600)
    path.write_text("changed critique")
    assert service.reports_for_run(source)[0]["status"] == "unmeasured"
