"""R1 execution contracts using recorded fixtures; no actual money is moved."""

import copy
import json

import pytest

from analysis.judges import ResearchCallError
from analysis.r1 import MOUNTINGS, R1Readiness, R1Runner, _shuffle
from worldd.kernel import BeliefService
from worldd.models import RunConfig
from worldd.runtime import World
from worldd.storage import Store
from worldd.util import digest, identifier


class Gateway:
    async def aclose(self):
        pass


def capture(world, run_id, model, messages, text):
    call_id = identifier("m")
    body = json.dumps(
        {
            "request": {"model": model, "messages": messages, "tools": []},
            "response": json.dumps({"choices": [{"message": {"content": text}}]}),
        }
    ).encode()
    relative = f"model-io/{call_id}.json"
    target = world.store.run_dir(run_id) / relative
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(body)
    world.store.append_record(
        run_id, "model_capture", call_id=call_id, capture_ref=relative, capture_hash=digest(body)
    )


@pytest.fixture
async def environment(tmp_path):
    world = World(Store(tmp_path), Gateway())
    beliefs = BeliefService(world)
    world.services["beliefs"] = beliefs
    settings = RunConfig(
        stage=1, packs=["belief"], model_snapshot_id="model-a", capture_model_io=True
    ).model_dump(mode="json")
    settings["tuple"] = {}
    first = world.store.create_run(settings)["run_id"]
    world.store.append_record(first, "outcome", action_id="a_known")
    for index, rank in enumerate((3, 5)):
        reply = await beliefs.handle(
            first,
            f"a_b{index}",
            "believe",
            {
                "record": {
                    "kind": "rule",
                    "claim": f"Prefer recorded strategy {index}.",
                    "rank": rank,
                    "evidence": [{"ref": "a_known", "class": "observed"}],
                }
            },
        )
        assert reply["kind"] == "result", reply
    capture(
        world,
        first,
        "model-a",
        [{"role": "user", "content": "earlier world context"}],
        "earlier choice",
    )
    await world.end_run(first)
    settings.update(
        continuity_source_run_id=first,
        continuity="fresh_context_workspace",
        iota={"workspace": "fresh", "beliefs": f"import({first})"},
    )
    second = world.store.create_run(settings)["run_id"]
    beliefs.import_continuity(second, beliefs.export_continuity(first))
    capture(
        world,
        second,
        "model-a",
        [{"role": "user", "content": "last captured world context"}],
        "last choice",
    )
    await world.end_run(second)
    bank = [
        {
            "item_id": f"heldout_{index}",
            "question": f"Fresh decision number {index}?",
            "choices": ["left", "right"],
            "stake_usd": 0.001,
        }
        for index in range(40)
    ]
    prereg = {
        "experiment_id": "R1",
        "primary_metric": {"name": "formation", "threshold": 0.15},
        "models": ["model-a", "model-b"],
        "seeds": list(range(5)),
        "reflection_policies": ["none", "self", "companion"],
        "primary_reflection_policy": "none",
        "forced_choices": 40,
        "bank_hash": digest(bank),
        "stopping_rule": "all registered cells",
        "likely_null": "no lasting preferences",
        "seed_count_rationale": "engineering fixture, never scientific",
    }
    cell = {
        "model_snapshot_id": "model-a",
        "seed": 0,
        "reflection_policy": "none",
        "session_run_ids": [first, second],
        "model_spec": {"model": "model-a", "family": "family-a"},
    }
    yield world, cell, prereg, bank
    await world.aclose()


class RecordedModel:
    def __init__(self, world):
        self.world, self.calls = world, []

    async def fork(self, source_run_id, spec, messages, **kwargs):
        self.calls.append({"source": source_run_id, "messages": messages, **kwargs})
        items = json.loads(messages[-1]["content"])["items"]
        answers = {item["item_id"]: item["choices"][index % 2] for index, item in enumerate(items)}
        text = json.dumps(answers)
        child = self.world.create_measurement_run(
            source_run_id, "probe", kwargs["budget_usd"], model_snapshot_id=spec.model
        )["run_id"]
        self.world.store.debit(child, "probe", 0.001)
        self.world.store.append_record(
            child,
            "model_call",
            model=spec.model,
            usd=0.001,
            usage={"input_tokens": 20, "output_tokens": 20},
        )
        capture(self.world, child, spec.model, messages, text)
        await self.world.end_run(child)
        return {"text": text, "measurement_run_id": child, "spent": self.world.store.spend(child)}


class RecordedStakes:
    def __init__(self, world, *, wrong_amount=False, reused_debit=False):
        self.world, self.children, self.calls = world, {}, []
        self.wrong_amount, self.reused_debit = wrong_amount, reused_debit

    async def __call__(
        self,
        *,
        source_run_id,
        measurement_run_id,
        item,
        choice,
        condition,
        prereg_hash,
        idempotency_key,
    ):
        key = (source_run_id, condition)
        if key not in self.children:
            settings = RunConfig(
                budget={"usd": 1},
                mode="replay",
                archive_in=source_run_id,
                measurement_of=source_run_id,
                metering_lane="kernel",
            ).model_dump(mode="json")
            settings["world_accounting"] = True
            settings["tuple"] = {}
            self.children[key] = self.world.store.create_run(settings)["run_id"]
        child = self.children[key]
        self.calls.append((condition, item["item_id"], choice))
        cost = item.get("stake", {}).get("cost_usd", item.get("stake_usd"))
        self.world.store.debit(child, "kernel", 0.002 if self.wrong_amount else cost)
        debit = [row for row in self.world.store.records(child) if row["kind"] == "ledger_debit"]
        debit_seq = debit[0 if self.reused_debit else -1]["seq"]
        reference, transaction = identifier("stake"), identifier("fixture_transaction")
        assert idempotency_key.startswith("sha256:")
        proof = json.dumps(
            {"fixture": True, "no_real_funds": True, "transaction_id": transaction}
        ).encode()
        relative = f"receipts/{item['item_id']}.json"
        target = self.world.store.run_dir(child) / relative
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(proof)
        self.world.store.append_record(
            child,
            "r1_stake_settled",
            event_id=reference,
            observer="world",
            executor_id="fixture-no-real-funds",
            prereg_hash=prereg_hash,
            item_id=item["item_id"],
            choice=choice,
            condition=condition,
            stake=item.get("stake")
            or {
                "rail": "real_usd",
                "amount": item["stake_usd"],
                "unit": "USD",
                "cost_usd": item["stake_usd"],
            },
            transaction_id=transaction,
            settlement_status="settled",
            debit_seq=debit_seq,
            provider_receipt_ref=relative,
            provider_receipt_hash=digest(proof),
        )
        if item["item_id"] == "heldout_39":
            await self.world.end_run(child)
        return {"receipt_run_id": child, "observation_ref": reference}


@pytest.mark.parametrize(
    "rail,cost", [("real_usd", 0.001), ("real_resource", 0.001), ("real_resource", 0)]
)
async def test_real_execution_uses_six_fresh_forks_and_distinct_recorded_stakes_without_source_mutation(
    environment,
    rail,
    cost,
):
    world, cell, prereg, bank = environment
    if rail == "real_resource":
        for item in bank:
            item["stake"] = {
                "rail": "real_resource",
                "amount": 2,
                "unit": "fixture_cpu_seconds",
                "cost_usd": cost,
            }
        prereg["bank_hash"] = digest(bank)
    model, stakes = RecordedModel(world), RecordedStakes(world)
    before = {key: world.store.records(key) for key in cell["session_run_ids"]}
    report = await R1Runner(world, model, stake_settler=stakes).run(
        prereg, [cell], bank, budget_usd=1, bootstrap=100
    )
    assert len(model.calls) == 6 and len(stakes.calls) == 240
    assert all(not row["tools_enabled"] and row["max_steps"] == 1 for row in model.calls)
    assert model.calls[0]["source"] == cell["session_run_ids"][0]
    assert all(row["source"] == cell["session_run_ids"][1] for row in model.calls[1:])
    mounts = report["execution"][0]["mountings"]
    assert mounts["nothing"] == mounts["retest"] == {}
    assert "last_context" in mounts["full_context"] and "last_context" not in mounts["store"]
    assert mounts["shuffle_receipt"]["changed"]
    assert set(report["execution"][0]["measurements"]) == {"reference", *MOUNTINGS}
    assert all(world.store.records(key) == records for key, records in before.items())
    assert report["scientific"] is False and report["completed"] is False
    assert report["stake_budget_usd"] == 240 * cost and report["model_spent_usd"] == 0.006
    assert (
        await R1Runner(world, model, stake_settler=stakes).run(
            prereg, [cell], bank, budget_usd=1, bootstrap=100
        )
        == report
    )
    assert len(model.calls) == 6 and len(stakes.calls) == 240
    assert R1Readiness(world).verify(report) == [
        "R1 execution lacks complete scientific provenance"
    ]
    changed = {**report, "scientific": True}
    assert "differs" in R1Readiness(world).verify(changed)[0]


@pytest.mark.parametrize("wrong_amount,reused_debit", [(True, False), (False, True)])
async def test_real_stake_receipts_require_actual_amount_and_unique_debits(
    environment, wrong_amount, reused_debit
):
    world, cell, prereg, bank = environment
    with pytest.raises(ResearchCallError, match="ledger debit|reuse"):
        await R1Runner(
            world,
            RecordedModel(world),
            stake_settler=RecordedStakes(
                world, wrong_amount=wrong_amount, reused_debit=reused_debit
            ),
        ).run(prereg, [cell], bank, budget_usd=1, bootstrap=100)


async def test_callback_boolean_cannot_replace_world_owned_real_stake_receipts(environment):
    world, cell, prereg, bank = environment
    with pytest.raises(ResearchCallError, match="actual receipt run"):
        await R1Runner(
            world,
            RecordedModel(world),
            stake_settler=lambda **kwargs: {"settled": True, "rail": "real_usd"},
        ).run(prereg, [cell], bank, budget_usd=1, bootstrap=100)


async def test_all_preregistration_and_scientific_preconditions_precede_paid_calls(environment):
    world, cell, prereg, bank = environment
    model = RecordedModel(world)
    runner = R1Runner(world, model, stake_settler=RecordedStakes(world))
    with pytest.raises(ValueError, match="scientific preconditions"):
        await runner.run(prereg, [cell], bank, budget_usd=1, bootstrap=100, scientific=True)
    bad = copy.deepcopy(bank)
    bad[0]["question"] = "Changed after registration"
    with pytest.raises(ValueError, match="registered hash"):
        await runner.run(prereg, [cell], bad, budget_usd=1)
    with pytest.raises(ValueError, match="model.*pinned"):
        await runner.run(
            prereg,
            [{**cell, "model_spec": {"model": "other", "family": "other"}}],
            bank,
            budget_usd=1,
        )
    assert model.calls == []


def test_shuffle_preserves_exact_supplied_token_count_and_rank_multiset():
    mounting = {
        "beliefs": [
            {"id": "a", "rank": 3, "links": []},
            {"id": "b", "rank": 5, "links": [{"to": "a", "relation": "supports"}]},
        ],
        "skills": [],
    }
    shuffled, receipt = _shuffle(mounting, seed=2, counter=lambda value: len(value.encode()))
    assert shuffled != mounting and receipt["exact_tokens"]
    assert len(json.dumps(shuffled, sort_keys=True)) == len(json.dumps(mounting, sort_keys=True))
    assert sorted(row["rank"] for row in shuffled["beliefs"]) == [3, 5]


def test_shuffle_moves_link_counters_and_propagation_without_changing_direct_evidence():
    mounting = {
        "beliefs": [
            {
                "id": "a",
                "status": "held",
                "rank": 3,
                "n_contra": 7,
                "links": [],
                "kernel": {
                    "evidence": [{"ref": "x"}],
                    "classifications": [{"observation_ref": "x"}],
                    "support_lost": [],
                    "flags": [],
                },
            },
            {
                "id": "b",
                "status": "held",
                "rank": 3,
                "n_contra": 0,
                "links": [{"to": "a", "relation": "derived_from"}],
                "kernel": {
                    "support_lost": [{"observation_ref": "x", "from": "a", "hop": 1}],
                    "flags": ["support_lost"],
                },
            },
            {
                "id": "c",
                "status": "held",
                "rank": 3,
                "n_contra": 1,
                "links": [{"to": "b", "relation": "contradicts"}],
                "kernel": {"support_lost": [], "flags": []},
            },
        ],
        "skills": [],
    }
    before = copy.deepcopy(mounting)
    shuffled, receipt = _shuffle(mounting, seed=2, counter=lambda value: len(value.encode()))
    assert receipt["changed"] and receipt["exact_tokens"]
    assert mounting == before
    assert [row["links"] for row in shuffled["beliefs"]] != [
        row["links"] for row in mounting["beliefs"]
    ]
    assert len(json.dumps(shuffled, sort_keys=True)) == len(json.dumps(mounting, sort_keys=True))
    for old, new in zip(mounting["beliefs"], shuffled["beliefs"], strict=True):
        link_count = sum(edge["relation"] == "contradicts" for edge in new["links"])
        assert new["n_contra"] - link_count == (7 if new["id"] == "a" else 0)
        assert new["kernel"].get("evidence") == old["kernel"].get("evidence")
        assert new["kernel"].get("classifications") == old["kernel"].get("classifications")
        for lost in new["kernel"]["support_lost"]:
            assert {"to": lost["from"], "relation": "derived_from"} in new["links"]
            assert lost["observation_ref"] == "x" and lost["hop"] == 1
