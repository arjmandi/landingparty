"""Entity labels use actual billed ASGI calls; the upstream labels are fixtures."""

import json

import httpx
import pytest

from analysis.entities import EntityInstrument, entity_source_items, tokenize
from analysis.judges import MeteredResearchClient, ResearchCallError
from worldd.app import create_app
from worldd.models import RunConfig
from worldd.util import digest

CLASSES = [
    {"id": "PERSON", "description": "An explicitly named person"},
    {"id": "ORG", "description": "An explicitly named organization"},
]
ALIASES = [{"type": "ORG", "canonical": "Acme Labs", "forms": ["Acme Labs", "ACME"]}]


class Gateway:
    def __init__(self, root):
        self.root = root

    async def fetch(self, run_id, action_id, **params):
        index = int(params["url"].rsplit("/", 1)[-1])
        text = (
            f"Alice {index} visited Acme Labs." if index % 2 == 0 else f"Bob {index} visited ACME."
        )
        raw = (f"<p>{text}</p><script>Hallucinated Phantom</script>").encode()
        handle = f"/world/fetched/{action_id}.html"
        path = self.root / "runs" / run_id / handle.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {
            "kind": "result",
            "url": params["url"],
            "handle": handle,
            "hash": digest(raw),
            "bytes": len(raw),
            "content_type": "text/html",
        }

    async def aclose(self):
        pass


def spans(text):
    result = []
    for name, kind in (
        ("Alice", "PERSON"),
        ("Bob", "PERSON"),
        ("Acme Labs", "ORG"),
        ("ACME", "ORG"),
    ):
        if name in text:
            start = text.index(name)
            result.append({"start": start, "end": start + len(name), "text": name, "type": kind})
    return sorted(result, key=lambda row: row["start"])


def bio(text):
    labels = ["O"] * len(tokenize(text))
    for span in spans(text):
        indices = [
            index
            for index, token in enumerate(tokenize(text))
            if span["start"] <= token["start"] < span["end"]
        ]
        for position, index in enumerate(indices):
            labels[index] = ("B-" if position == 0 else "I-") + span["type"]
    return labels


@pytest.fixture
async def measured(tmp_path):
    requests, modes = [], {"mode": "correct"}

    def provider(request):
        payload = json.loads(request.content)
        assert payload["tools"] == []
        data = json.loads(payload["messages"][-1]["content"])
        assert "calibration_rows" not in data and "labels" not in data
        requests.append(data)
        readings = []
        for item in data["items"]:
            assert "Phantom" not in item["text"]
            labels = spans(item["text"])
            if modes["mode"] == "hallucinate":
                labels[0]["text"] = "Phantom"
            elif modes["mode"] == "partial":
                labels[0].update(text=item["text"][:2], end=2)
            elif modes["mode"] == "miss":
                labels = []
            elif modes["mode"] == "abstain":
                labels = None
            readings.append(
                {
                    "observation_ref": "forged"
                    if modes["mode"] == "unknown_ref"
                    else item["observation_ref"],
                    "spans": labels,
                }
            )
        return httpx.Response(
            200,
            json={
                "model": "entity-judge-fixed",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"readings": readings, "reason": "Local software fixture labels"}
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 20},
            },
        )

    prices = {
        "models": {
            "entity-judge-fixed": {
                "provider": "openai",
                "input": 1,
                "output": 1,
                "context_window": 32000,
                "max_output_tokens": 4096,
            }
        }
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        app = create_app(tmp_path, gateway=Gateway(tmp_path), lane_client=client, prices=prices)
        world = app.state.world
        world.model_families = {
            "entity-source-fixed": "source-family",
            "entity-judge-fixed": "judge-family",
        }
        source = world.create_run(RunConfig(model_snapshot_id="entity-source-fixed"))["run_id"]
        for index in range(42):
            result = await world.act(source, "fetch", {"url": f"https://example.com/{index}"})
            assert result["kind"] == "result", result
            world.store.debit(source, "model", 0.01)
        await world.end_run(source)
        model = MeteredResearchClient.for_app(world, app)
        yield world, source, model, requests, modes
        await model.aclose()
        await world.aclose()


def instrument(world, source, *, count=0, mutate=None):
    items = entity_source_items(world, source)["items"]
    rows = [
        {
            "observation_ref": item["observation_ref"],
            "text_hash": item["text_hash"],
            "labels": bio(item["text"]),
        }
        for item in items[:count]
    ]
    payload = {
        "instrument_id": "entities-local-fixture@1",
        "classes": CLASSES,
        "aliases": ALIASES,
        "calibration_rows": rows,
    }
    if mutate:
        mutate(payload)
    raw = json.dumps(payload).encode()
    target = world.store.run_dir(source) / "entity-instrument.json"
    target.write_bytes(raw)
    return {"path": target.name, "hash": digest(raw)}


def config(reference):
    return {
        "instrument": reference,
        "model_spec": {
            "model": "entity-judge-fixed",
            "family": "judge-family",
            "human_agreement": 1,
        },
        "agent_family": "source-family",
        "budget_usd": 10,
    }


async def test_actual_span_calls_aliases_first_touch_and_immutable_parent(measured):
    world, source, model, requests, _ = measured
    reference = instrument(world, source)
    records, spend = world.store.records(source), world.store.spend(source)
    result = await EntityInstrument(world, model).run(source, config(reference))
    assert len(requests) == 42 and result["spent_usd"] == pytest.approx(42 * 0.00004)
    assert result["column"] == "J" and result["calibration"]["kappa"] is None
    assert result["G"]["status"] == "unmeasured" and result["scientific_gate_evidence"] is False
    entities = {row["name"]: row for row in result["entities"]}
    assert set(entities) == {"Alice", "Bob", "Acme Labs"}
    assert entities["Alice"]["usd"] == 0 and entities["Alice"]["world_actions"] == 1
    assert entities["Bob"]["usd"] == 0.01 and entities["Bob"]["world_actions"] == 2
    assert result["series"][-1]["distinct_entities"] == 3
    assert result["input_truncated"] is False and result["coverage"] == 1
    for row in result["readings"]:
        assert row["text"] is None and row["source_hash"] and row["text_hash"]
        receipt = row["receipt"]
        assert world.store.get_run(receipt["measurement_run_id"])["state"] == "sealed"
        assert (
            receipt["capture_hash"]
            and world.store.reconcile(receipt["measurement_run_id"])["valid"]
        )
    assert world.store.records(source) == records and world.store.spend(source) == spend


async def test_forty_fresh_distinct_actual_bio_items_promote_only_calibrated_v(measured):
    world, source, model, requests, _ = measured
    result = await EntityInstrument(world, model).run(
        source, config(instrument(world, source, count=40))
    )
    assert len(requests) == 82
    assert len({row["measurement_run_id"] for row in result["receipts"]}) == 82
    score = result["calibration"]
    assert score["column"] == result["column"] == "V" and score["kappa"] == 1
    assert score["precision"] == score["recall"] == 1 and score["miss_rate"] == 0
    assert score["paired_items"] == 40 and score["paired_tokens"] > 40
    assert result["G"]["status"] == "unmeasured"


async def test_fewer_items_claimed_agreement_and_missed_entities_cannot_promote_v(measured):
    world, source, model, _, modes = measured
    result = await EntityInstrument(world, model).run(
        source, config(instrument(world, source, count=3))
    )
    assert result["calibration"]["kappa"] == 1 and result["column"] == "J"
    modes["mode"] = "miss"
    result = await EntityInstrument(world, model).run(
        source, config(instrument(world, source, count=40))
    )
    assert result["calibration"]["kappa"] == 0 and result["calibration"]["miss_rate"] == 1
    assert result["column"] == "J" and result["entities"] == []


@pytest.mark.parametrize(
    "mode,match",
    [
        ("hallucinate", "literal"),
        ("partial", "complete source tokens"),
        ("unknown_ref", "actual supplied observation refs"),
    ],
)
async def test_hallucinated_unknown_and_partial_spans_are_rejected(measured, mode, match):
    world, source, model, requests, modes = measured
    modes["mode"] = mode
    with pytest.raises(ResearchCallError, match=match):
        await EntityInstrument(world, model).run(source, config(instrument(world, source)))
    assert len(requests) == 1 and world.store.verify_record(source)["valid"]


async def test_abstentions_remain_unmeasured_and_misses_are_not_zeroed(measured):
    world, source, model, _, modes = measured
    modes["mode"] = "abstain"
    result = await EntityInstrument(world, model).run(
        source, config(instrument(world, source, count=40))
    )
    assert result["status"] == "unmeasured" and result["coverage"] == 0
    assert all(row["distinct_entities"] is None for row in result["series"])
    score = result["calibration"]
    assert score["kappa"] is None and score["paired_items"] == 0 and score["abstentions"] == 40
    assert score["miss_rate"] == 1 and score["column"] == "J"


async def test_frozen_expert_hash_duplicates_and_source_tamper_fail_before_calls(measured):
    world, source, model, requests, _ = measured

    def duplicate(payload):
        payload["calibration_rows"].append(payload["calibration_rows"][0])

    with pytest.raises(ValueError, match="distinct actual text"):
        await EntityInstrument(world, model).run(
            source, config(instrument(world, source, count=1, mutate=duplicate))
        )
    reference = instrument(world, source)
    reference["hash"] = digest(b"forged")
    with pytest.raises(ValueError, match="hash mismatch"):
        await EntityInstrument(world, model).run(source, config(reference))
    reference = instrument(world, source)
    first = entity_source_items(world, source)["items"][0]
    (world.store.run_dir(source) / first["handle"].lstrip("/")).write_text("changed after seal")
    with pytest.raises(ValueError, match="resource hash mismatch"):
        await EntityInstrument(world, model).run(source, config(reference))
    assert not requests


async def test_all_input_caps_and_actual_family_checks_precede_purchases(measured):
    world, source, model, requests, _ = measured
    options = config(instrument(world, source))
    with pytest.raises(ValueError, match="input cap"):
        await EntityInstrument(world, model).run(source, {**options, "max_input_chars": 1000})
    with pytest.raises(ValueError, match="actual pinned model"):
        await EntityInstrument(world, model).run(
            source, {**options, "agent_family": "invented-family"}
        )
    world.model_families["entity-judge-fixed"] = "source-family"
    with pytest.raises(ValueError, match="different model family"):
        await EntityInstrument(world, model).run(
            source,
            {**options, "model_spec": {"model": "entity-judge-fixed", "family": "source-family"}},
        )
    assert not requests


async def test_caller_changed_labels_and_capture_tampering_cannot_reuse_billing(measured):
    world, source, model, _, _ = measured

    class Modified:
        async def complete(self, *args, **kwargs):
            result = await model.complete(*args, **kwargs)
            value = json.loads(result["text"])
            value["readings"][0]["spans"] = []
            return {**result, "text": json.dumps(value)}

    with pytest.raises(ResearchCallError, match="actual captured response"):
        await EntityInstrument(world, Modified()).run(source, config(instrument(world, source)))

    class Tampered:
        async def complete(self, *args, **kwargs):
            result = await model.complete(*args, **kwargs)
            child = result["measurement_run_id"]
            path = next((world.store.run_dir(child) / "model-io").glob("*.json"))
            path.write_text("{}")
            return result

    with pytest.raises(ResearchCallError, match="capture hash"):
        await EntityInstrument(world, Tampered()).run(source, config(instrument(world, source)))


async def test_async_batch_events_supply_actual_source_windows(measured):
    world, _, model, requests, _ = measured
    source = world.create_run(RunConfig(model_snapshot_id="entity-source-fixed"))["run_id"]
    ticket = await world.act(
        source, "fetch_many", {"urls": ["https://example.com/100", "https://example.com/101"]}
    )
    await world.jobs[source][ticket["action_id"]]
    await world.end_run(source)
    items = entity_source_items(world, source)["items"]
    assert len(items) == 2 and all(item["action_ref"] == ticket["action_id"] for item in items)
    assert all(item["world_actions"] == 1 and item["usd"] == 0 for item in items)
    result = await EntityInstrument(world, model).run(source, config(instrument(world, source)))
    assert len(requests) == 2 and len(result["entities"]) == 3
    assert all(
        row["through_seq"] <= world.store.records(source)[-1]["seq"] for row in result["readings"]
    )


async def test_invalid_bio_and_insufficient_real_budget_fail_without_provider_spend(measured):
    world, source, model, requests, _ = measured

    def invalid(payload):
        payload["calibration_rows"][0]["labels"][0] = "I-PERSON"

    with pytest.raises(ValueError, match="continuation"):
        await EntityInstrument(world, model).run(
            source, config(instrument(world, source, count=1, mutate=invalid))
        )
    options = config(instrument(world, source))
    before = world.store.records(source)
    with pytest.raises(ResearchCallError, match="HTTP 402"):
        await EntityInstrument(world, model).run(source, {**options, "budget_usd": 0.000001})
    assert not requests and world.store.records(source) == before
    assert (
        sum(
            world.store.total_spent(row["run_id"])
            for row in world.store.list_runs()
            if row["config"].get("measurement_of") == source
        )
        == 0
    )


async def test_capture_disabled_sources_fail_before_calls(measured):
    world, _, model, requests, _ = measured
    source = world.create_run(
        RunConfig(model_snapshot_id="entity-source-fixed", capture_model_io=False)
    )["run_id"]
    assert (await world.act(source, "fetch", {"url": "https://example.com/100"}))[
        "kind"
    ] == "result"
    await world.end_run(source)
    with pytest.raises(ValueError, match="capture_model_io before any purchase"):
        await EntityInstrument(world, model).run(source, config(instrument(world, source)))
    assert not requests
