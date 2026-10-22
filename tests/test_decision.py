import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from analysis.decision import DecisionDivergenceRunner, _observations, _response_action
from analysis.instruments import load_instrument
from analysis.judges import ResearchCallError
from worldd.footprint import FootprintService
from worldd.storage import Store


def artifact(world, run_id, name, value):
    path = world.store.run_dir(run_id) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value).encode()
    path.write_bytes(body)
    return {"path": name, "hash": "sha256:" + hashlib.sha256(body).hexdigest()}


@pytest.fixture
def source(tmp_path):
    store = Store(tmp_path)
    world = SimpleNamespace(store=store, services={})
    run_id = store.create_run(
        {
            "budget": {"usd": 1, "wall_s": 300},
            "capture_model_io": True,
            "model_snapshot_id": "fixed-snapshot",
            "goal": "The fixed goal.",
            "fixture_transport": True,
        }
    )["run_id"]
    directory = store.run_dir(run_id) / "model-io"
    directory.mkdir()
    for index in range(200):
        call_id = f"m_{index:03d}"
        capture = {
            "request": {
                "model": "fixed-snapshot",
                "messages": [
                    {"role": "system", "content": "HIDDEN_SYSTEM"},
                    {"role": "user", "content": "HIDDEN_EARLIER_CONVERSATION"},
                    {"role": "assistant", "content": "HIDDEN_REASONING"},
                    *[
                        {
                            "role": "tool",
                            "tool_call_id": f"old_{observation}",
                            "content": f"OBSERVATION_{observation:02d}",
                        }
                        for observation in range(30)
                    ],
                ],
            },
            "response": json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [{"function": {"name": "fetch", "arguments": "{}"}}]
                            }
                        }
                    ]
                }
            ),
        }
        (directory / (call_id + ".json")).write_text(json.dumps(capture))
        store.append_record(
            run_id,
            "model_call",
            call_id=call_id,
            model="fixed-snapshot",
            usage={"input": 1, "output": 1},
        )
        store.append_record(
            run_id,
            "action",
            action_id=f"a_{index:03d}",
            tool="fetch",
            params={"url": "https://example.org"},
        )
    bank = artifact(
        world,
        run_id,
        "prereg/classes.json",
        {
            "bank_id": "frozen-tools@1",
            "classes": [
                {
                    "class_id": "retrieve",
                    "description": "Read another source.",
                    "tool_names": ["fetch", "search"],
                },
                {
                    "class_id": "respond",
                    "description": "Write a terminal response.",
                    "assistant_text": True,
                },
            ],
        },
    )
    protocol = {
        "protocol_id": "frozen-decision@1",
        "registered_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "model_snapshot_id": "fixed-snapshot",
        "class_bank_hash": bank["hash"],
        "decision_count": 200,
        "samples_per_context": 20,
        "contexts": [0, 5, 20],
        "call_budget_usd": 0.001,
        "max_tokens": 128,
        "seed": 7,
        "vindication_policy": {"kind": "independent-footprint-threshold@1", "threshold": 1},
    }
    registration = artifact(world, run_id, "prereg/decision.json", protocol)
    store.append_record(run_id, "seal")
    store.update_run(run_id, state="sealed")
    yield world, run_id, bank, registration, protocol
    store.close()


class CopyModel:
    def __init__(self, *, invalid=False):
        self.calls, self.invalid = 0, invalid

    async def complete(self, source_run_id, spec, messages, **kwargs):
        self.calls += 1
        assert spec.model == "fixed-snapshot" and kwargs["lane"] == "probe"
        assert kwargs["budget_usd"] == 0.001 and kwargs["max_tokens"] == 128
        data = json.loads(messages[1]["content"])
        assert set(data) == {"goal", "observations"}
        assert data["goal"] == "The fixed goal."
        k = len(data["observations"])
        assert k in {0, 5, 20}
        assert [observation["content"] for observation in data["observations"]] == [
            f"OBSERVATION_{index:02d}" for index in range(30 - k, 30)
        ]
        assert "HIDDEN" not in json.dumps(messages)
        label = "invalid" if self.invalid else "respond" if self.calls % 4 == 0 else "retrieve"
        return {
            "text": json.dumps({"action_class": label}),
            "measurement_run_id": f"r_fixture_{self.calls}",
            "spent": {"probe": 0.000001},
        }


def options(bank, registration):
    return {
        "model_spec": {"model": "fixed-snapshot", "family": "agent"},
        "class_bank": bank,
        "registration": registration,
        "measurement_budget_usd": 12,
        "seed": 7,
    }


async def test_full_registered_12000_draws_hide_context_and_resume_without_resampling(source):
    world, run_id, bank, registration, _ = source
    before = world.store.records(run_id)
    model = CopyModel()
    runner = DecisionDivergenceRunner(world, model)
    result = await runner.run(run_id, **options(bank, registration))
    assert model.calls == 12000 and len(result["rows"]) == 600
    assert result["result"]["status"] == "measured"
    assert all(curve["mean"] == 0.25 for curve in result["result"]["curves"].values())
    assert all(
        not curve["vindicated"] and not curve["unvindicated"]
        for curve in result["result"]["curves"].values()
    )
    assert result["provenance"]["spent_usd"] == pytest.approx(0.012)
    assert result["provenance"]["scientific_gate_passed"] is False
    assert all(len(row["sampling_seeds"]) == 20 for row in result["rows"])
    instrument = load_instrument(world.store, run_id, result["artifact"])
    assert len(instrument["rows"]) == 600
    repeated = await runner.run(run_id, **options(bank, registration))
    assert repeated["result"] == result["result"] and model.calls == 12000
    assert world.store.records(run_id) == before


async def test_invalid_draw_is_not_retried_to_improve_its_class(source):
    world, run_id, bank, registration, _ = source
    model = CopyModel(invalid=True)
    runner = DecisionDivergenceRunner(world, model)
    with pytest.raises(ResearchCallError, match="valid class"):
        await runner.run(run_id, **options(bank, registration))
    model.invalid = False
    with pytest.raises(ResearchCallError, match="operator reconciliation"):
        await runner.run(run_id, **options(bank, registration))
    assert model.calls == 1


async def test_prereg_budget_and_snapshot_checks_happen_before_any_call(source):
    world, run_id, bank, registration, _ = source
    model = CopyModel()
    runner = DecisionDivergenceRunner(world, model)
    with pytest.raises(ValueError, match="12,000"):
        await runner.run(run_id, **{**options(bank, registration), "measurement_budget_usd": 1})
    with pytest.raises(ValueError, match="snapshot"):
        await runner.run(
            run_id,
            **{
                **options(bank, registration),
                "model_spec": {"model": "different-model", "family": "agent"},
            },
        )
    assert model.calls == 0


async def test_sponsor_receipt_cannot_create_vindication(source):
    world, run_id, bank, registration, _ = source
    footprint = FootprintService(world)
    world.services["footprint"] = footprint
    item = footprint.record_item(run_id, "a_000", kind="post", platform="native")
    reading = footprint.add_reading(
        item["item_id"], 20, author_class="sponsor", evidence_ref="sponsor:reaction"
    )
    receipts = artifact(world, run_id, "prereg/receipts.json", {"m_000": reading["reading_id"]})
    model = CopyModel()
    with pytest.raises(ValueError, match="independent numeric"):
        await DecisionDivergenceRunner(world, model).run(
            run_id, grounded_receipts=receipts, **options(bank, registration)
        )
    assert model.calls == 0
    await footprint.aclose()


def test_anthropic_and_responses_captures_preserve_only_tool_observations():
    assert _observations(
        {
            "messages": [
                {"role": "assistant", "content": "hidden"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hidden"},
                        {"type": "tool_result", "content": "visible"},
                    ],
                },
            ]
        }
    ) == [{"index": "1.1", "content": "visible"}]
    assert _observations({"input": [{"type": "function_call_output", "output": "visible"}]}) == [
        {"index": "0", "content": "visible"}
    ]
    assert _response_action(
        'event: content_block_start\ndata: {"type":"content_block_start","content_block":{"type":"tool_use","name":"fetch"}}\n\n'
    )[0] == ["fetch"]
    assert _response_action({"output": [{"type": "function_call", "name": "search"}]})[0] == [
        "search"
    ]


async def test_insufficient_captures_remain_unmeasured_without_calls(source):
    world, run_id, bank, registration, _ = source
    (world.store.run_dir(run_id) / "model-io/m_199.json").unlink()
    model = CopyModel()
    result = await DecisionDivergenceRunner(world, model).run(run_id, **options(bank, registration))
    assert result["status"] == "unmeasured" and result["eligible_decisions"] == 199
    assert model.calls == 0


async def test_grounded_vindication_requires_action_link_and_actual_independent_value(source):
    world, run_id, _, _, protocol = source
    footprint = FootprintService(world)
    world.services["footprint"] = footprint
    runner = DecisionDivergenceRunner(world, CopyModel())
    item = footprint.record_item(run_id, "a_000", kind="post", platform="native")
    positive = footprint.add_reading(
        item["item_id"], 2, author_class="independent_human", evidence_ref="platform:positive"
    )
    negative = footprint.add_reading(
        item["item_id"], 0, author_class="independent_agent", evidence_ref="platform:negative"
    )
    decision = {"decision_id": "m_000", "action_ids": ["a_000"]}
    assert (
        runner._vindication(run_id, decision, {"m_000": positive["reading_id"]}, protocol)[
            "vindicated"
        ]
        is True
    )
    assert (
        runner._vindication(run_id, decision, {"m_000": negative["reading_id"]}, protocol)[
            "vindicated"
        ]
        is False
    )
    with pytest.raises(ValueError, match="linked"):
        runner._vindication(
            run_id,
            {**decision, "action_ids": ["a_other"]},
            {"m_000": positive["reading_id"]},
            protocol,
        )
    await footprint.aclose()


def test_streamed_openai_function_names_are_reassembled_before_classifying():
    stream = "\n".join(
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"name": fragment}}]},
                    }
                ]
            }
        )
        for fragment in ("fe", "tch")
    )
    assert _response_action(stream)[0] == ["fetch"]


async def test_reused_measurement_receipt_cannot_count_as_independent_draws(source):
    world, run_id, bank, registration, _ = source

    class ReusedReceipt(CopyModel):
        async def complete(self, *args, **kwargs):
            result = await super().complete(*args, **kwargs)
            result["measurement_run_id"] = "r_reused"
            return result

    model = ReusedReceipt()
    with pytest.raises(ResearchCallError, match="metered receipt"):
        await DecisionDivergenceRunner(world, model).run(run_id, **options(bank, registration))
    assert model.calls == 2
