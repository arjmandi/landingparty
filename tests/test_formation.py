import json
from decimal import Decimal

import pytest

from analysis.formation import FormationRunner, canonical_claim, cross_seed_stability, prose_sample
from analysis.judges import JudgeSpec, ResearchCallError
from worldd.models import RunConfig
from worldd.runtime import World
from worldd.storage import Store


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def environment(tmp_path):
    world = World(Store(tmp_path), Gateway())
    source = world.create_run(RunConfig(model_snapshot_id="agent-pinned"))["run_id"]
    await world.end_run(source)
    yield world, source
    await world.aclose()


class RecordedModel:
    """Local judge replies with actual child records and reconciled fixture billing."""

    def __init__(self, world, responses=()):
        self.world = world
        self.responses = iter(responses)
        self.requests = []

    async def complete(self, source_run_id, spec, messages, *, lane, budget_usd, max_tokens):
        self.requests.append({"messages": messages, "lane": lane, "budget": budget_usd})
        reply = next(self.responses, {"label": "consistent", "reason": "fixture comparison"})
        child = self.world.create_measurement_run(
            source_run_id, lane, budget_usd, model_snapshot_id=spec.model
        )["run_id"]
        charge = int(Decimal(str(budget_usd)) * 100_000_000) / 1_000_000_000
        self.world.store.debit(child, "judge", charge)
        self.world.store.append_record(
            child,
            "model_call",
            model=spec.model,
            tags=["model.judge"],
            usd=charge,
            usage={"input_tokens": 10, "output_tokens": 10},
        )
        await self.world.end_run(child)
        return {
            "text": json.dumps(reply),
            "measurement_run_id": child,
            "spent": self.world.store.spend(child),
        }


def runner(world, model, *, budget=10, agreement=None):
    return FormationRunner(
        world,
        model,
        JudgeSpec("judge-pinned", "other-family", human_agreement=agreement),
        agent_family="agent-family",
        budget_usd=budget,
    )


def belief(index, *, triple=False, rank=3):
    return {
        "id": f"b_{index}",
        "claim": f"The harbour gate {index} is open.",
        "rank": rank,
        "status": "held",
        **(
            {"triple": {"subject": str(index), "predicate": "state", "object": "open"}}
            if triple
            else {}
        ),
    }


def revision_row():
    return {
        "belief_id": "b_0",
        "observation_ref": "a_real_evidence",
        "before": "The harbour is open.",
        "after": "The harbour is closed.",
        "evidence_text": "The world-recorded notice says closed.",
        "evidence_action": 10,
        "evidence_usd": 2,
        "read_action": 11,
        "revised_action": 14,
        "revised_usd": 3.5,
    }


def test_hash_sample_is_order_invariant_distinct_and_never_fills_missing_records():
    rows = [belief(index) for index in range(350)]
    first = prose_sample(rows, seed=7)
    assert first == prose_sample(list(reversed(rows)), seed=7)
    assert first["n"] == 200 and first["complete_200"]
    assert len(set(first["ids"])) == 200
    assert first["ids"] != prose_sample(rows, seed=8)["ids"]
    small = prose_sample(rows[:3])
    assert small["n"] == 3 and small["census"] and not small["complete_200"]
    with pytest.raises(ValueError, match="distinct"):
        prose_sample([rows[0], rows[0]])


def test_canonicalization_preserves_negation_and_numbers_and_spearman_handles_ties():
    assert canonical_claim("  Café  IS OPEN! ") == canonical_claim("Ｃａｆé is open")
    assert canonical_claim("not open 2") != canonical_claim("open 2")
    assert canonical_claim("open 2") != canonical_claim("open 3")
    assert canonical_claim("temperature -1") != canonical_claim("temperature 1")
    assert canonical_claim("price > 5") != canonical_claim("price < 5")
    assert canonical_claim("risk 5%") != canonical_claim("risk 5")
    a = [{"claim": claim, "rank": rank} for claim, rank in [("A!", 1), ("B", 1), ("C", 5)]]
    b = [{"claim": claim, "rank": rank} for claim, rank in [("a", 5), ("b", 5), ("c", 1), ("D", 3)]]
    metrics = cross_seed_stability([{"seed": 1, "beliefs": a}, {"seed": 2, "beliefs": b}])
    assert metrics["mean_jaccard"] == 0.75
    assert metrics["mean_rank_spearman"] == pytest.approx(-1)
    assert not metrics["headline_eligible"]
    constant = cross_seed_stability(
        [
            {"seed": 1, "beliefs": a[:2]},
            {"seed": 2, "beliefs": b[:2]},
        ]
    )
    assert constant["pairs"][0]["rank_spearman"]["status"] == "unmeasured"
    empty = cross_seed_stability([{"seed": 1, "beliefs": []}, {"seed": 2, "beliefs": []}])
    assert empty["mean_jaccard"] is None


async def test_two_hundred_prose_records_are_judged_on_metered_children_without_source_mutation(
    environment,
):
    world, source = environment
    rows = [belief(index, triple=index % 2 == 0) for index in range(200)]
    model = RecordedModel(
        world,
        [
            *[{"label": "consistent", "reason": "fixture pair"} for _ in rows],
            *[
                {
                    "label": "no_revision",
                    "revision_needed": False,
                    "reason": "fixture unchanged claim",
                }
                for _ in rows
            ],
        ],
    )
    before = world.store.records(source)
    result = await runner(world, model, budget=20, agreement=1).run(
        source,
        beliefs=rows,
        seed=9,
        revision_observations=[
            {
                "belief_id": row["id"],
                "observation_ref": f"a_fixture_{row['id']}",
                "before": row["claim"],
                "after": row["claim"],
                "context": "Fixture records show no change.",
                "evidence_action": 0,
                "evidence_usd": 0,
            }
            for row in rows
        ],
    )
    assert result["prose"]["sample"]["n"] == 200
    assert result["prose"]["coherence"]["n"] == 200
    assert result["prose"]["coherence"]["column"] == "J"  # An asserted agreement is insufficient.
    assert result["prose"]["revision"]["n"] == 200
    assert result["prose"]["status"] == "measured"
    assert result["prose"]["revision_record_coverage"] == 1
    assert len(result["measurements"]) == 400 and len(model.requests) == 400
    assert result["spent_usd"] == pytest.approx(2)
    assert all(
        request["lane"] == "judge" and request["budget"] == 0.05 for request in model.requests
    )
    assert world.store.records(source) == before and world.store.total_spent(source) == 0
    assert not result["truth_adjudicated"] and not result["scientific_gate_passed"]


@pytest.mark.parametrize("has_triples", [False, True])
async def test_revision_latency_is_hidden_below_fifty_percent_triple_coverage(
    environment, has_triples
):
    world, source = environment
    model = RecordedModel(
        world,
        [
            {"label": "consistent", "reason": "different entities"},
            {"label": "evidence_driven", "revision_needed": True, "reason": "documented notice"},
        ],
    )
    result = await runner(world, model).run(
        source,
        beliefs=[belief(0, triple=has_triples), belief(1)],
        revision_observations=[revision_row()],
    )
    latency = result["prose"]["revision_latency"]
    assert result["triple_subset"]["coverage"] == (0.5 if has_triples else 0)
    assert latency["status"] == ("measured" if has_triples else "unmeasured")
    if has_triples:
        assert latency["median_latency_actions"] == 4
        assert latency["median_latency_usd"] == 1.5
        assert latency["axes"] == ["world_actions", "usd"]
    else:
        assert "median_latency_actions" not in latency


async def test_actual_task_calibration_promotes_only_matching_labels_and_keeps_human_labels_blind(
    environment,
):
    world, source = environment
    calibration_rows = [
        {
            "task": "coherence",
            "input": {"left": "Gate open", "right": "Gate open"},
            "label": "consistent",
        },
        {
            "task": "coherence",
            "input": {"left": "Gate open", "right": "Gate closed"},
            "label": "contradiction",
        },
        {
            "task": "revision",
            "input": {"before": "Gate open", "after": "Gate closed", "evidence_text": "Closure"},
            "label": "evidence_driven",
        },
        {
            "task": "revision",
            "input": {"before": "Gate open", "after": "Gate open"},
            "label": "no_revision",
        },
        *[
            {
                "task": "dropped_goal",
                "input": {"goal": f"Goal {label}", "trajectory": "Expert-labelled trajectory"},
                "label": label,
            }
            for label in ("achieved", "believed_impossible", "no_longer_desired")
        ],
    ]
    responses = [
        {
            "label": row["label"],
            "reason": "fixture expert comparison",
            **(
                {"revision_needed": row["label"] == "evidence_driven"}
                if row["task"] == "revision"
                else {}
            ),
        }
        for row in calibration_rows
    ]
    responses += [
        {"label": "consistent", "reason": "different gates"},
        {"label": "evidence_driven", "revision_needed": True, "reason": "documented notice"},
        *[
            {"label": label, "reason": "fixture trajectory"}
            for label in ("achieved", "believed_impossible", "no_longer_desired")
        ],
    ]
    model = RecordedModel(world, responses)
    result = await runner(world, model).run(
        source,
        beliefs=[belief(0, triple=True), belief(1)],
        calibration_rows=calibration_rows,
        revision_observations=[revision_row()],
        dropped_goals=[
            {
                "id": f"goal_{index}",
                "observation_ref": "a_trajectory",
                "goal": "Pursue work",
                "trajectory": "Captured observations",
            }
            for index in range(3)
        ],
    )
    assert result["prose"]["coherence"]["column"] == "V"
    assert result["prose"]["revision"]["kappa"] == 1
    assert result["dropped_goals"]["counts"] == {
        "achieved": 1,
        "believed_impossible": 1,
        "no_longer_desired": 1,
    }
    assert result["calibration"]["dropped_goal"]["per_class"]["achieved"]["precision"] == 1
    for request in model.requests:
        payload = json.loads(request["messages"][1]["content"])
        assert "label" not in payload and "rank" not in payload and "author" not in payload


async def test_invalid_judgment_and_unmetered_callback_never_produce_metrics(environment):
    world, source = environment
    model = RecordedModel(world, [{"label": "objectively_true", "reason": "invalid task"}])
    with pytest.raises(ResearchCallError, match="invalid label"):
        await runner(world, model).run(source, beliefs=[belief(0), belief(1)])

    class Unmetered:
        async def complete(self, *args, **kwargs):
            return {"text": '{"label":"consistent","reason":"fabricated"}', "metered": True}

    with pytest.raises(ResearchCallError, match="measurement run"):
        await runner(world, Unmetered()).run(source, beliefs=[belief(0), belief(1)])


async def test_cross_family_and_coordinate_admission_happen_before_paid_calls(environment):
    world, source = environment
    model = RecordedModel(world)
    with pytest.raises(ValueError, match="different model family"):
        FormationRunner(
            world,
            model,
            JudgeSpec("agent-pinned", "agent-family"),
            agent_family="agent-family",
            budget_usd=1,
        )
    malformed = {**revision_row(), "revised_action": 1}
    with pytest.raises(ValueError, match="precede"):
        await runner(world, model).run(
            source, beliefs=[belief(0, triple=True)], revision_observations=[malformed]
        )
    assert not model.requests


async def test_uncalibrated_disagreement_and_incomplete_samples_remain_exploratory(environment):
    world, source = environment
    model = RecordedModel(
        world,
        [
            {"label": "contradiction", "reason": "fixture disagreement"},
            {"label": "consistent", "reason": "fixture disagreement"},
            {"label": "consistent", "reason": "fixture comparison"},
        ],
    )
    result = await runner(world, model).run(
        source,
        beliefs=[belief(0), belief(1)],
        calibration_rows=[
            {"task": "coherence", "input": {"left": "A", "right": "A"}, "label": "consistent"},
            {
                "task": "coherence",
                "input": {"left": "A", "right": "not A"},
                "label": "contradiction",
            },
        ],
    )
    assert result["calibration"]["coherence"]["kappa"] == -1
    assert result["prose"]["coherence"]["column"] == "J"
    assert result["prose"]["status"] == "partial"
    assert not result["prose"]["sample"]["complete_200"]


async def test_pair_and_tiny_budget_validation_precede_paid_calls(environment):
    world, source = environment
    model = RecordedModel(world)
    with pytest.raises(ValueError, match="repeat"):
        await runner(world, model).run(
            source, beliefs=[belief(0), belief(1)], coherence_pairs=[["b_0", "b_1"], ["b_1", "b_0"]]
        )
    with pytest.raises(ValueError, match="one nanodollar"):
        await runner(world, model, budget=1e-9).run(
            source, beliefs=[belief(index) for index in range(3)]
        )
    assert not model.requests


async def test_reusing_one_billed_child_for_multiple_labels_is_rejected(environment):
    world, source = environment

    class ReusedReceipt(RecordedModel):
        completion = None

        async def complete(self, *args, **kwargs):
            if self.completion is None:
                self.completion = await super().complete(*args, **kwargs)
            return self.completion

    with pytest.raises(ResearchCallError, match="new measurement run"):
        await runner(world, ReusedReceipt(world)).run(
            source, beliefs=[belief(index) for index in range(3)]
        )
