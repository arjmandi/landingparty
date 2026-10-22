from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from worldd.beliefs import BeliefIntegrityError, BeliefStore, BeliefValidationError

T0 = datetime(2026, 10, 22, 12, tzinfo=UTC)


@pytest.fixture
def world(tmp_path):
    now = [T0]
    observations = {
        "a_observed": {"run_id": "r_test", "recorded_at": (T0 - timedelta(hours=1)).isoformat()},
        "a_late": {"run_id": "r_test", "recorded_at": (T0 + timedelta(days=2)).isoformat()},
        "a_foreign": {"run_id": "r_other", "recorded_at": T0.isoformat()},
        "a_same_series": {
            "run_id": "r_other",
            "series_id": "s_shared",
            "recorded_at": T0.isoformat(),
        },
    }
    store = BeliefStore(
        tmp_path,
        "r_test",
        observations.get,
        series_id="s_shared",
        clock=lambda: now[0],
    )
    return store, now, observations


def belief(**changes):
    return {
        "kind": "fact",
        "claim": "The harbour is open.",
        "rank": 4,
        "triple": {"subject": "harbour", "predicate": "state", "object": "open"},
        "evidence": [{"ref": "a_observed", "class": "observed"}],
        **changes,
    }


def test_bitemporal_versions_preserve_historical_fold_and_replay(world):
    store, now, observations = world
    first = store.believe(belief(valid_from=(T0 - timedelta(days=2)).isoformat()))
    now[0] += timedelta(days=1)
    revised = store.revise(
        first["id"],
        {
            "claim": "The harbour is closed.",
            "triple": {"subject": "harbour", "predicate": "state", "object": "closed"},
            "revision": {"kind": "update", "conflict_class": "earlier_observation"},
        },
    )
    assert revised["id"] == first["id"]
    assert revised["version_id"] != first["version_id"]
    assert store.fold(as_of=T0)[0]["claim"] == "The harbour is open."
    assert store.fold()[0]["claim"] == "The harbour is closed."
    assert store.fold(valid_at=T0 - timedelta(days=3)) == []
    history = store.history(first["id"])
    assert history[0]["status"] == "superseded"
    assert history[0]["superseded_at"] == revised["recorded_at"]
    assert store.changed(T0) == [revised]
    rebuilt = BeliefStore(store.data_dir, "r_test", observations.get)
    assert rebuilt.fold() == store.fold()
    assert rebuilt.verify() == store.verify()


def test_unresolved_future_and_foreign_evidence_do_not_append(world):
    store, _, _ = world
    for ref in ["a_missing", "a_late", "a_foreign"]:
        with pytest.raises(BeliefValidationError):
            store.believe(belief(evidence=[{"ref": ref, "class": "observed"}]))
    assert store.verify()["count"] == 0
    same_series = store.believe(belief(evidence=[{"ref": "a_same_series", "class": "human"}]))
    assert same_series["id"].startswith("b_")


def test_source_claimed_timestamp_cannot_override_world_observed_time(world):
    store, _, observations = world
    observations["a_late"]["last_modified"] = (T0 - timedelta(days=100)).isoformat()
    with pytest.raises(BeliefValidationError, match="postdates"):
        store.believe(belief(evidence=[{"ref": "a_late", "class": "fetched_content"}]))


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "version_id",
        "recorded_at",
        "superseded_at",
        "n_support",
        "n_contra",
        "last_support_at",
        "flags",
        "op",
        "hash",
        "prev_hash",
        "seq",
    ],
)
def test_system_fields_cannot_be_supplied(world, field):
    store, _, _ = world
    with pytest.raises(BeliefValidationError, match="world-owned"):
        store.believe(belief(**{field: "injected"}))
    first = store.believe(belief())
    with pytest.raises(BeliefValidationError, match="world-owned"):
        store.revise(first["id"], {field: "injected"})
    assert store.verify()["count"] == 1


def test_evidence_or_existing_link_is_required_and_flags_are_not_rejections(world):
    store, _, _ = world
    with pytest.raises(BeliefValidationError, match="needs evidence"):
        store.believe(belief(evidence=[]))
    first = store.believe(belief(evidence=[{"ref": "a_observed", "class": "self_inference"}]))
    assert first["rank"] == 4
    assert "flags" not in first
    assert store._records()[0]["belief"]["flags"] == ["high_rank_self_inference"]
    second = store.believe(
        {
            "kind": "rule",
            "claim": "Recheck before departure.",
            "rank": 3,
            "links": [{"to": first["id"], "relation": "derived_from"}],
        }
    )
    assert store.why(second["id"])["links"][0]["to"] == first["id"]


def test_conflicts_are_private_flags_with_no_rank_change_or_automatic_retraction(world):
    store, _, _ = world
    first = store.believe(belief())
    second = store.believe(
        belief(
            claim="The harbour is closed.",
            triple={"subject": "harbour", "predicate": "state", "object": "closed"},
        )
    )
    assert len(store.about("harbour")) == 2
    assert {record["rank"] for record in store.fold()} == {4}
    conflicts = store.review("conflicts")
    assert [record["id"] for record in conflicts] == [second["id"]]
    assert "flags" not in conflicts[0]
    assert store.history(first["id"])[0]["status"] == "held"


def test_nonoverlapping_intervals_do_not_conflict(world):
    store, _, _ = world
    store.believe(belief(valid_to=T0.isoformat(), valid_from=(T0 - timedelta(days=2)).isoformat()))
    store.believe(
        belief(
            claim="The harbour is closed.",
            triple={"subject": "harbour", "predicate": "state", "object": "closed"},
        )
    )
    assert store.review("conflicts") == []


def test_retraction_and_links_preserve_unrelated_beliefs(world):
    store, now, _ = world
    first = store.believe(belief())
    second = store.believe(
        {
            "kind": "strategy",
            "claim": "Use the east gate.",
            "rank": 2,
            "evidence": [{"ref": "a_observed", "class": "observed"}],
        }
    )
    now[0] += timedelta(seconds=1)
    linked = store.link(second["id"], first["id"], "operationalised_by")
    with pytest.raises(BeliefValidationError, match="already exists"):
        store.link(second["id"], first["id"], "operationalised_by")
    store.retract(first["id"])
    assert store.history(first["id"])[-1]["status"] == "retracted"
    assert store.about("harbour") == []
    assert next(item for item in store.fold() if item["id"] == second["id"]) == linked
    with pytest.raises(BeliefValidationError, match="retracted"):
        store.revise(first["id"], {"rank": 5})


def test_rendered_views_are_private_flag_free_deterministic_and_bounded(world):
    store, _, _ = world
    for index in range(20):
        store.believe(
            {
                "kind": "rule",
                "claim": f"Rule {index}: " + "界" * 90,
                "rank": 5,
                "evidence": [{"ref": "a_observed", "class": "self_inference"}],
            }
        )
    paths = store.render()
    before = {name: Path(path).read_bytes() for name, path in paths.items()}
    assert len(before["core.md"]) <= 2000
    for name, path in paths.items():
        assert "high_rank_self_inference" not in before[name].decode()
        assert '"flags"' not in before[name].decode()
        assert Path(path).stat().st_mode & 0o222 == 0
    store.render()
    assert before == {name: Path(path).read_bytes() for name, path in paths.items()}
    assert len(store.fold()) == 20


def test_hash_chain_detects_edits_and_prevents_further_writes(world):
    store, _, _ = world
    store.believe(belief())
    event = json.loads(store.log_path.read_text())
    event["belief"]["rank"] = 0
    store.log_path.write_text(json.dumps(event) + "\n")
    assert store.verify()["valid"] is False
    with pytest.raises(BeliefIntegrityError, match="hash mismatch"):
        store.fold()
    with pytest.raises(BeliefIntegrityError):
        store.believe(belief())


def test_concurrent_instances_share_a_single_append_chain(world):
    store, now, observations = world

    def append(index):
        other = BeliefStore(store.data_dir, "r_test", observations.get, clock=lambda: now[0])
        return other.believe(belief(claim=f"Observed harbour report {index}."))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(append, range(24)))
    assert len({item["id"] for item in results}) == 24
    assert store.verify()["count"] == 24
    assert len(store.fold()) == 24


def test_conditional_and_stale_review_do_not_resolve_or_change_rank(world):
    store, now, _ = world
    record = store.believe(belief(assumptions=["t_access"], change_class="fast"))
    assert record["status"] == "conditional"
    assert store.review("queue")[0]["id"] == record["id"]
    now[0] += timedelta(days=2)
    assert store.review("stale")[0]["id"] == record["id"]
    assert store.fold()[0]["rank"] == 4
    assert store.fold()[0]["status"] == "conditional"


@pytest.mark.parametrize(
    "changes",
    [
        {"triple": None},
        {"rank": True},
        {"rank": 6},
        {"claim": "x" * 301},
        {"assumptions": ["a"] * 5},
        {"valid_from": "2026-10-22"},
        {"author": "reflector", "status": "held"},
        {"evidence": [{"ref": "a_observed", "class": "self_inference", "injection_score": 2}]},
    ],
)
def test_structural_validation(world, changes):
    store, _, _ = world
    with pytest.raises(BeliefValidationError):
        store.believe(belief(**changes))
    assert store.verify()["count"] == 0


def test_default_evidence_resolver_reads_only_this_run_world_record(tmp_path):
    store = BeliefStore(tmp_path, "r_test", clock=lambda: T0)
    (tmp_path / "runs" / "r_test" / "record.jsonl").write_text(
        json.dumps(
            {
                "run_id": "r_test",
                "ts": (T0 - timedelta(seconds=1)).isoformat(),
                "action_id": "a_observed",
                "kind": "outcome",
            }
        )
        + "\n"
    )
    assert store.believe(belief())["rank"] == 4


def test_rank_change_does_not_refresh_old_evidence(world):
    store, now, _ = world
    first = store.believe(belief(change_class="fast"))
    now[0] += timedelta(days=3)
    revised = store.revise(first["id"], {"rank": 5})
    assert revised["last_support_at"] == first["last_support_at"]
    assert store.review("stale")[0]["id"] == first["id"]


def test_rendered_ledger_retains_all_versions(world):
    store, _, _ = world
    first = store.believe(belief())
    store.revise(first["id"], {"rank": 3})
    store.retract(first["id"])
    views = store.render()
    ledger = [json.loads(line) for line in Path(views["ledger.jsonl"]).read_text().splitlines()]
    assert [record["op"] for record in ledger] == ["assert", "revise", "retract"]


def test_invalid_run_path_rejected(tmp_path):
    with pytest.raises(BeliefValidationError):
        BeliefStore(tmp_path, "../escape")


def test_world_authored_injection_score_overrides_agent_supplied_score(world):
    store, _, observations = world
    observations["a_observed"]["resource"] = {"provenance": {"injection_score": 0.9}}
    record = store.believe(
        belief(
            evidence=[
                {
                    "ref": "a_observed",
                    "class": "fetched_content",
                    "injection_score": 0.01,
                }
            ]
        )
    )
    assert record["evidence"][0]["injection_score"] == 0.9


def test_registered_no_flag_and_no_slot_arms_preserve_agent_choices(world):
    store, now, observations = world
    alternative = BeliefStore(
        store.data_dir,
        "r_no_flags",
        lambda ref: {
            **observations[ref],
            "run_id": "r_no_flags",
        },
        clock=lambda: now[0],
        enable_flags=False,
        require_fact_triples=False,
    )
    record = alternative.believe(
        belief(
            triple=None,
            rank=5,
            evidence=[
                {
                    "ref": "a_observed",
                    "class": "self_inference",
                }
            ],
        )
    )
    assert record["rank"] == 5 and record["triple"] is None
    assert alternative._records()[0]["belief"]["flags"] == []
