"""A subscription life's balance, estimated from the native client's own transcript."""

import asyncio
import json
from decimal import Decimal

import pytest

from worldd.app import create_app
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
