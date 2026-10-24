"""A subscription life's balance, estimated from the native client's own transcript."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from worldd.app import create_app
from worldd.lane import load_prices
from worldd.models import RunConfig
from worldd.transcript_meter import TranscriptMeter

PRICE = {
    "provider": "anthropic",
    "input": "3",
    "output": "15",
    "cache_read": "0.3",
    "cache_write": "3.75",
    "cache_write_1h": "6",
    "context_window": 200000,
    "max_output_tokens": 8192,
    "source": "https://fixture.example/prices",
}


@pytest.fixture
async def world(tmp_path):
    app = create_app(tmp_path, prices={"models": {"claude-fixture": PRICE}})
    yield app.state.world
    await app.state.world.aclose()


def life(world, usd=1):
    return world.services["lives"].create(
        {
            "stage": 2,
            "budget": {"usd_life": usd, "wall_life_days": None},
            "driver": {"metric": "usd_balance"},
            "income_channels": ["recorded"],
            "on_exhaustion": "strand",
            "on_harness_exit": "rest",
            "model_metering": "transcript",
        }
    )["life_id"]


async def session(world, life_id):
    created = await world.services["lives"].start_session(
        life_id,
        {
            "model_billing_mode": "subscription",
            "model_snapshot_id": "claude-fixture",
            "budget": {"usd": 10000, "wall_s": 600},
        },
    )
    return created["run_id"]


def assistant(message_id, *, input_tokens=1000, output_tokens=100, model="claude-fixture"):
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": message_id,
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        }
    )


def cost(input_tokens=1000, output_tokens=100):
    return (Decimal(input_tokens) * 3 + Decimal(output_tokens) * 15) / Decimal(1_000_000)


def estimated(world, run_id):
    return Decimal(str(world.store.spend(run_id).get("model.estimated", 0)))


def kinds(world, run_id):
    return [record["kind"] for record in world.store.records(run_id)]


async def settle(world):
    await asyncio.gather(*world.services["transcript_meter"].tasks)


def test_estimates_are_only_for_development_subscription_lives():
    with pytest.raises(ValueError, match="subscription"):
        RunConfig(model_billing_mode="subscription", life_id="life_fixture")
    RunConfig(model_billing_mode="subscription", life_id="life_x", model_metering="transcript")
    with pytest.raises(ValueError, match="Transcript estimates"):
        RunConfig(model_metering="transcript")


async def test_a_default_run_keeps_its_tuple_and_ignores_the_meter(world):
    first = world.create_run(RunConfig())
    second = world.create_run(RunConfig())
    assert first["tuple"]["runtime_protocol_id"] == second["tuple"]["runtime_protocol_id"]
    world.services["transcript_meter"].observe(first["run_id"], assistant("msg_1"))
    assert estimated(world, first["run_id"]) == 0


async def test_repeated_chunks_of_one_message_are_charged_once(world):
    run_id = await session(world, life(world))
    meter = world.services["transcript_meter"]
    meter.observe(run_id, assistant("msg_1", output_tokens=40))
    meter.observe(run_id, assistant("msg_1", output_tokens=100))
    meter.observe(run_id, assistant("msg_1", output_tokens=100))
    assert estimated(world, run_id) == cost()
    assert kinds(world, run_id).count("model_estimate") == 2
    config = world.run_config(run_id)
    assert "estimated from your session's own usage reports" in config["preamble"]
    assert (
        config["tuple"]["runtime_protocol_id"]
        != world.create_run(RunConfig())["tuple"]["runtime_protocol_id"]
    )


async def test_the_result_totals_charge_only_what_messages_missed(world):
    run_id = await session(world, life(world))
    meter = world.services["transcript_meter"]
    meter.observe(run_id, assistant("msg_1"))
    result = {
        "type": "result",
        "session_id": "s_1",
        "modelUsage": {"claude-fixture": {"inputTokens": 3000, "outputTokens": 300}},
    }
    meter.observe(run_id, json.dumps(result))
    meter.observe(run_id, json.dumps(result))
    assert estimated(world, run_id) == cost(3000, 300)


async def test_the_lookahead_strands_the_life_and_a_deposit_wakes_it(world):
    lives, bank = world.services["lives"], world.services["bank"]
    life_id = life(world, usd="0.02")
    run_id = await session(world, life_id)
    meter = world.services["transcript_meter"]
    for index in range(3):
        meter.observe(run_id, assistant(f"msg_{index}"))
    await settle(world)
    assert world.store.get_run(run_id)["end"]["terminated"] == "budget_exhausted"
    refused = [r for r in world.store.records(run_id) if r["kind"] == "model_refused"]
    assert refused and refused[0]["metering"] == "transcript"
    assert lives.get(life_id)["state"] == "stranded"
    bank.record(life_id, usd=1, source="Client paid", evidence="Line 7", request_id="request-01")
    assert lives.get(life_id)["state"] == "waking"


async def test_overshoot_never_breaks_the_cap_and_is_recorded(world):
    run_id = await session(world, life(world, usd="0.002"))
    world.services["transcript_meter"].observe(run_id, assistant("msg_1"))
    await settle(world)
    assert estimated(world, run_id) == Decimal("0.002")
    overshoot = [r for r in world.store.records(run_id) if r["kind"] == "estimate_overshoot"]
    assert Decimal(overshoot[0]["usd"]) == cost() - Decimal("0.002")
    assert world.store.reconcile(run_id)["valid"]


@pytest.mark.parametrize(
    "line",
    [
        assistant("msg_1", model="claude-unpriced"),
        json.dumps({"type": "assistant", "message": {"id": "msg_1", "model": "claude-fixture"}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "model": "claude-fixture",
                    "usage": {"input_tokens": 10},
                },
            }
        ),
    ],
)
async def test_unpriced_or_unreadable_usage_fails_closed(world, line):
    run_id = await session(world, life(world))
    world.services["transcript_meter"].observe(run_id, line)
    await settle(world)
    run = world.store.get_run(run_id)
    assert "estimate_unavailable" in kinds(world, run_id)
    assert run["tainted"] and run["state"] == "sealed"
    assert run["end"]["truncated"] == "estimate_unavailable"


async def test_a_restarted_meter_rebuilds_from_the_record_and_never_charges_twice(world):
    run_id = await session(world, life(world))
    world.services["transcript_meter"].observe(run_id, assistant("msg_1"))
    fresh = TranscriptMeter(world)
    fresh.observe(run_id, assistant("msg_1"))
    fresh.observe(run_id, assistant("msg_2"))
    assert estimated(world, run_id) == cost() * 2


async def test_capture_reads_whole_stdout_lines_for_the_meter(world, tmp_path):
    run_id = await session(world, life(world))
    harnesses = world.services["harnesses"]
    root = tmp_path / "native-session"
    root.mkdir()
    harnesses._save(
        {
            "session_id": "hs_fixture",
            "run_id": run_id,
            "root": str(root),
            "output_suppressed": False,
            "output_truncated": False,
        }
    )
    reader = asyncio.StreamReader()
    padding = "x" * (70 * 1024)  # longer than a logged line, still read whole
    long_line = json.loads(assistant("msg_long"))
    long_line["message"]["content"] = [{"type": "text", "text": padding}]
    reader.feed_data((json.dumps(long_line) + "\n" + assistant("msg_2") + "\n").encode())
    reader.feed_eof()
    await harnesses._capture("hs_fixture", reader, "stdout")
    assert estimated(world, run_id) == cost() * 2


async def test_usage_observed_while_frozen_is_charged_not_dropped(world):
    run_id = await session(world, life(world))
    world.store.update_run(run_id, state="frozen")
    world.services["transcript_meter"].observe(run_id, assistant("msg_1"))
    assert estimated(world, run_id) == cost()
    world.store.update_run(run_id, state="running")
    world.services["transcript_meter"].observe(run_id, assistant("msg_1"))
    assert estimated(world, run_id) == cost()


async def test_usage_drained_after_a_lookahead_stop_still_counts(world):
    run_id = await session(world, life(world, usd="0.02"))
    meter = world.services["transcript_meter"]
    for index in range(3):
        meter.observe(run_id, assistant(f"msg_{index}"))
    assert "model_refused" in kinds(world, run_id)
    # The session drains after the stop: its buffered usage and final totals arrive.
    world.store.update_run(run_id, state="stopping")
    meter.observe(run_id, assistant("msg_3"))
    meter.observe(
        run_id,
        json.dumps(
            {
                "type": "result",
                "modelUsage": {"claude-fixture": {"inputTokens": 5000, "outputTokens": 500}},
            }
        ),
    )
    assert estimated(world, run_id) == Decimal("0.02")
    overshoot = sum(
        Decimal(r["usd"]) for r in world.store.records(run_id) if r["kind"] == "estimate_overshoot"
    )
    assert estimated(world, run_id) + overshoot == cost(5000, 500)
    assert kinds(world, run_id).count("model_refused") == 1
    await settle(world)


async def test_incurred_charges_stop_only_at_the_seal(world):
    run_id = await session(world, life(world, usd=1))
    store = world.store
    assert store.charge_incurred(run_id, "model.estimated", "0.4") == Decimal("0.4")
    store.update_run(run_id, state="stopping")
    assert store.charge_incurred(run_id, "model.estimated", "0.9") == Decimal("0.6")
    store.update_run(run_id, state="sealed")
    assert store.charge_incurred(run_id, "model.estimated", "0.1") == 0
    assert estimated(world, run_id) == 1


FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"


@pytest.fixture
async def priced(tmp_path):
    """A world on the reviewed price table, plus the fixture model for synthetic lines."""
    prices = load_prices()
    prices["models"]["claude-fixture"] = PRICE
    app = create_app(tmp_path, prices=prices)
    yield app.state.world
    await app.state.world.aclose()


async def session_on(world, life_id, model, mode="subscription"):
    created = await world.services["lives"].start_session(
        life_id,
        {
            "model_billing_mode": mode,
            "model_snapshot_id": model,
            "budget": {"usd": 10000, "wall_s": 600},
        },
    )
    return created["run_id"]


def replay(world, run_id, name):
    meter = world.services["transcript_meter"]
    for line in (FIXTURES / name).read_text().splitlines():
        meter.observe(run_id, line)
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


async def test_partial_messages_price_a_real_session_exactly_as_its_client(priced):
    run_id = await session_on(priced, life(priced, usd=1), "claude-sonnet-5-5")
    events = replay(priced, run_id, "claude-2.1.308-partial.jsonl")
    result = next(event for event in events if event["type"] == "result")
    # The early assistant events said 16 output tokens; the final delta says 74.
    assert estimated(priced, run_id) == Decimal(str(result["total_cost_usd"]))
    estimates = [r for r in priced.store.records(run_id) if r["kind"] == "model_estimate"]
    assert not [r for r in estimates if r.get("true_up")]
    assert {r["usage"]["cache_write_1h"] for r in estimates} >= {2237, 45795}
    assert "estimate_unavailable" not in kinds(priced, run_id)


async def test_a_model_the_plan_does_not_serve_is_a_model_error_not_an_estimate(priced):
    run_id = await session_on(priced, life(priced), "claude-sonnet-5-5")
    replay(priced, run_id, "claude-2.1.308-model-404.jsonl")
    await settle(priced)
    errors = [r for r in priced.store.records(run_id) if r["kind"] == "model_error"]
    assert len(errors) == 1
    assert errors[0]["status"] == 404 and errors[0]["error_class"] == "model_unavailable"
    assert errors[0]["error"] == "model_not_found"
    assert "claude-sonnet-4-20250514" in errors[0]["message"]
    run = priced.store.get_run(run_id)
    assert "estimate_unavailable" not in kinds(priced, run_id)
    assert run["end"]["terminated"] == "model_error" and not run["end"].get("truncated")
    assert estimated(priced, run_id) == 0


async def test_a_lane_metered_session_records_its_model_error_too(priced):
    lives = priced.services["lives"]
    life_id = lives.create(
        {
            "stage": 2,
            "budget": {"usd_life": 1, "wall_life_days": None},
            "driver": {"metric": "usd_balance"},
            "income_channels": ["recorded"],
            "on_exhaustion": "strand",
            "on_harness_exit": "rest",
        }
    )["life_id"]
    run_id = await session_on(priced, life_id, "claude-sonnet-5-5", mode="api_key")
    replay(priced, run_id, "claude-2.1.308-model-404.jsonl")
    await settle(priced)
    assert [r["kind"] for r in priced.store.records(run_id) if r["kind"] == "model_error"]
    assert estimated(priced, run_id) == 0


async def test_a_plan_limit_carries_its_reset_time(priced):
    run_id = await session_on(priced, life(priced), "claude-fixture")
    meter = priced.services["transcript_meter"]
    meter.observe(
        run_id,
        json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": 1792842600},
            }
        ),
    )
    meter.observe(
        run_id,
        json.dumps(
            {
                "type": "assistant",
                "is_api_error_message": True,
                "api_error_status": 429,
                "error": "rate_limit",
                "message": {
                    "id": "synthetic",
                    "model": "<synthetic>",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "content": [{"type": "text", "text": "Usage limit reached"}],
                },
            }
        ),
    )
    await settle(priced)
    error = next(r for r in priced.store.records(run_id) if r["kind"] == "model_error")
    assert error["error_class"] == "plan_limit"
    assert error["resets_at"].startswith("2026-10-24")


async def test_a_synthetic_message_with_usage_still_fails_closed(priced):
    run_id = await session_on(priced, life(priced), "claude-fixture")
    priced.services["transcript_meter"].observe(run_id, assistant("synthetic", model="<synthetic>"))
    await settle(priced)
    assert "estimate_unavailable" in kinds(priced, run_id)


def haiku_message(message_id, prompt, output=100):
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": message_id,
                "model": "claude-haiku-5-5",
                "usage": {
                    "input_tokens": prompt,
                    "output_tokens": output,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        }
    )


async def test_a_tiered_model_is_priced_by_each_calls_own_prompt(priced):
    run_id = await session_on(priced, life(priced, usd=5), "claude-haiku-5-5")
    meter = priced.services["transcript_meter"]
    meter.observe(run_id, haiku_message("msg_short", 90_000))
    short = Decimal(90_000) * Decimal("0.10") / 10**6 + Decimal(100) * Decimal("0.50") / 10**6
    assert estimated(priced, run_id) == short
    meter.observe(run_id, haiku_message("msg_long", 150_000))
    long = Decimal(150_000) * Decimal("0.50") / 10**6 + Decimal(100) * Decimal("2.50") / 10**6
    assert estimated(priced, run_id) == short + long


async def test_the_true_up_keeps_the_tier_its_messages_paid(priced):
    run_id = await session_on(priced, life(priced, usd=5), "claude-haiku-5-5")
    meter = priced.services["transcript_meter"]
    meter.observe(run_id, haiku_message("msg_1", 60_000))
    meter.observe(run_id, haiku_message("msg_2", 60_000))
    # 30K missed tokens on a 150K session total stay at the short-prompt rate.
    meter.observe(
        run_id,
        json.dumps(
            {
                "type": "result",
                "modelUsage": {"claude-haiku-5-5": {"inputTokens": 150_000, "outputTokens": 200}},
            }
        ),
    )
    expected = Decimal(150_000) * Decimal("0.10") / 10**6 + Decimal(200) * Decimal("0.50") / 10**6
    assert estimated(priced, run_id) == expected


async def test_the_true_up_prices_one_hour_cache_writes_at_their_rate(priced):
    run_id = await session_on(priced, life(priced, usd=5), "claude-fixture")
    meter = priced.services["transcript_meter"]
    meter.observe(
        run_id,
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "model": "claude-fixture",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 10,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 1000,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 0,
                            "ephemeral_1h_input_tokens": 1000,
                        },
                    },
                },
            }
        ),
    )
    meter.observe(
        run_id,
        json.dumps(
            {
                "type": "result",
                "modelUsage": {
                    "claude-fixture": {
                        "inputTokens": 10,
                        "outputTokens": 10,
                        "cacheCreationInputTokens": 3000,
                    }
                },
            }
        ),
    )
    one_hour = Decimal(PRICE["cache_write_1h"])
    expected = (Decimal(10) * 3 + Decimal(10) * 15 + Decimal(3000) * one_hour) / Decimal(1_000_000)
    assert estimated(priced, run_id) == expected


async def test_capture_keeps_message_usage_and_drops_token_chunks(priced, tmp_path):
    run_id = await session_on(priced, life(priced, usd=1), "claude-sonnet-5-5")
    harnesses = priced.services["harnesses"]
    root = tmp_path / "native-partial"
    root.mkdir()
    harnesses._save(
        {
            "session_id": "hs_partial",
            "run_id": run_id,
            "root": str(root),
            "output_suppressed": False,
            "output_truncated": False,
        }
    )
    lines = (FIXTURES / "claude-2.1.308-partial.jsonl").read_text()
    reader = asyncio.StreamReader()
    reader.feed_data(lines.encode())
    reader.feed_eof()
    await harnesses._capture("hs_partial", reader, "stdout")
    stored = [json.loads(line) for line in (root / "stdout.jsonl").read_text().splitlines()]
    kept = {e["event"]["type"] for e in stored if e["type"] == "stream_event"}
    assert kept == {"message_start", "message_delta"}
    assert any(e["type"] == "assistant" for e in stored)
    assert harnesses._load("hs_partial")["stdout_filtered"] is True
    result = next(e for e in stored if e["type"] == "result")
    assert estimated(priced, run_id) == Decimal(str(result["total_cost_usd"]))
