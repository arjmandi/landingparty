import json
from types import SimpleNamespace

import pytest

from worldd.registry import VERSION_FIELDS, GateRequired, RegistryService
from worldd.storage import Store


@pytest.fixture
def registry(tmp_path):
    store = Store(tmp_path / "data")
    service = RegistryService(SimpleNamespace(store=store))
    yield service
    store.close()


def version(**changes):
    return {**{field: "pinned@1" for field in VERSION_FIELDS}, "archive_id": None, **changes}


def prereg():
    return {
        "primary_metric": {"name": "formation", "threshold": 0.15},
        "seed_count": 5,
        "seed_count_rationale": "Pilot variance estimate",
        "stopping_rule": "all registered cells",
        "likely_null": "no formation",
        "exploratory_metrics": [],
    }


@pytest.fixture
def verified_pilot_contract(monkeypatch):
    """Isolate generic authority logic; no empirical fixture passes production verification."""
    from analysis.programme import PilotReadiness

    monkeypatch.setattr(PilotReadiness, "verify", lambda self, report: [])


def research_runs(registry, count=12, *, mode="research"):
    registration = registry.register_series(prereg(), version())
    ids = []
    for number in range(count):
        run = registry.store.create_run(
            {
                "tuple": version(),
                "budget": {"usd": 1},
                "mode": "live",
                "goal": "goal14",
                "family": "F2",
                "research_mode": mode,
                "series_id": registration["series_id"],
                "block_profile": {"reference": 0.1},
                "seed": number,
            }
        )
        run_id = run["run_id"]
        registry.record_run(registration["series_id"], run_id, cell="A" if number % 2 == 0 else "B")
        registry.store.update_run(run_id, state="sealed", tainted=False)
        registry.store.append_record(run_id, "seal")
        (registry.store.run_dir(run_id) / "report.json").write_text(json.dumps({"run_id": run_id}))
        ids.append(run_id)
    return registration, ids


def pilot_report(registration, ids):
    return {
        "study": "shakedown",
        "scientific": True,
        "completed": True,
        "run_ids": ids,
        "prereg_hash": registration["prereg_hash"],
        "metrics": {
            "metering_matches_provider": True,
            "completeness_verdicts": True,
            "replay_fidelity": True,
            "block_profile": True,
            "hash_chain_and_seal": True,
            "attrition": True,
            "breaker_calibrated": True,
            "probe_bank_items": 60,
        },
    }


def test_registration_is_hashed_immutable_and_requires_complete_tuple(registry):
    first = registry.register_series(prereg(), version())
    assert registry.register_series(prereg(), version()) == first
    assert registry.get_series(first["series_id"])["prereg"] == prereg()
    with pytest.raises(ValueError, match="Incomplete version"):
        registry.register_series(prereg(), {"world_id": "only-one"})
    bad = prereg()
    bad.pop("stopping_rule")
    with pytest.raises(ValueError, match="pre-registration"):
        registry.register_series(bad, version())


def test_series_refuses_changed_price_policy_or_model(registry):
    series = registry.register_series(prereg(), version())
    for field in ("price_table_id", "policy_id", "model_snapshot_id"):
        run = registry.store.create_run(
            {"tuple": version(**{field: "changed"}), "budget": {"usd": 1}}
        )
        with pytest.raises(ValueError, match=field):
            registry.record_run(series["series_id"], run["run_id"])


def test_development_mode_never_claims_research_gate_passage(registry):
    status = registry.require_stage(3, research_mode="development")
    assert status["development_only"]
    assert not status["research_authorized"]
    assert not status["external_writes_allowed"]
    assert status["missing_gates"] == ["shakedown", "R1", "R5", "L4", "IRB"]
    with pytest.raises(GateRequired):
        registry.require_stage(1)


def test_development_run_fixtures_cannot_unlock_research(registry):
    series, ids = research_runs(registry, mode="development")
    gate = registry.register_gate("shakedown", pilot_report(series, ids))
    assert not gate["eligible"]
    assert any("not a research run" in error for error in gate["errors"])
    with pytest.raises(GateRequired):
        registry.require_stage(1)


def test_test_provenance_cannot_be_converted_to_scientific_evidence(registry):
    series, ids = research_runs(registry)
    gate = registry.register_gate("shakedown", pilot_report(series, ids), provenance="test")
    assert not gate["eligible"]
    assert any("test evidence" in error for error in gate["errors"])


def test_registered_research_evidence_unlocks_only_its_stage(registry, verified_pilot_contract):
    series, ids = research_runs(registry)
    gate = registry.register_gate("shakedown", pilot_report(series, ids))
    assert gate["eligible"], gate["errors"]
    assert registry.require_stage(1)["research_authorized"]
    with pytest.raises(GateRequired, match="R1"):
        registry.require_stage(2)


def test_gate_rechecks_taint_and_report_integrity(registry, verified_pilot_contract):
    series, ids = research_runs(registry)
    gate = registry.register_gate("shakedown", pilot_report(series, ids))
    assert gate["eligible"]
    (registry.store.run_dir(ids[0]) / "report.json").write_text('{"changed":true}')
    with pytest.raises(GateRequired):
        registry.require_stage(1)


def test_missing_actual_runs_artifacts_and_duplicate_ids_reject_gate(registry):
    series = registry.register_series(prereg(), version())
    report = pilot_report(series, ["r_missing"] * 12)
    gate = registry.register_gate("shakedown", report)
    assert not gate["eligible"]
    assert any("duplicate" in error for error in gate["errors"])
    assert any("unknown evidence run" in error for error in gate["errors"])


def test_live_comparability_needs_interleaving_and_block_profiles(registry):
    series, ids = research_runs(registry, count=4)
    assert registry.comparability(ids[0], ids[1], series_id=series["series_id"])["comparable"]
    assert not registry.comparability(ids[0], ids[1])["comparable"]
    changed = registry.store.get_run(ids[1])
    changed["config"]["block_profile"] = {"reference": 0.16}
    verdict = registry.comparability(
        registry.store.get_run(ids[0]), changed, series_id=series["series_id"]
    )
    assert not verdict["comparable"]
    assert any("five percentage" in reason for reason in verdict["reasons"])


@pytest.mark.parametrize(
    "cells,comparable",
    [
        (["A", "B", "C", "A", "B", "C"], True),
        (["A", "A", "B", "B", "A", "B"], False),
        (["A", "B", "C", "D", "A", "B", "A", "B"], True),
    ],
)
def test_execution_interleaving_handles_three_arms_and_rejects_batched_prefix(
    registry, cells, comparable
):
    series, ids = research_runs(registry, count=len(cells))
    with registry.store._transaction():
        for run_id, cell in zip(ids, cells, strict=True):
            registry.store.db.execute(
                "UPDATE series_runs SET cell=? WHERE series_id=? AND run_id=?",
                (cell, series["series_id"], run_id),
            )
    assert (
        registry.comparability(ids[0], ids[1], series_id=series["series_id"])["comparable"]
        is comparable
    )


def test_empty_block_rates_and_different_iso_week_are_unmeasured_comparability(registry):
    series, ids = research_runs(registry, count=4)
    left, right = registry.store.get_run(ids[0]), registry.store.get_run(ids[1])
    left["config"]["block_profile"] = {}
    right["config"]["block_profile"] = {}
    assert not registry.comparability(left, right, series_id=series["series_id"])["comparable"]
    left["config"]["block_profile"] = right["config"]["block_profile"] = {"reference": 0.1}
    right["created_at"] = "2020-01-01T00:00:00+00:00"
    result = registry.comparability(left, right, series_id=series["series_id"])
    assert not result["comparable"] and any("ISO weeks" in reason for reason in result["reasons"])


def test_negative_or_unscored_r1_cannot_unlock_outpost(registry):
    report = {
        "study": "R1",
        "completed": True,
        "scientific": True,
        "positive": False,
        "prereg_hash": "sha256:missing",
        "run_ids": [],
        "by_model": {},
    }
    gate = registry.register_gate("R1", report)
    assert not gate["eligible"]
    assert any("positive formation" in error for error in gate["errors"])


def test_r1_positive_arrays_without_actual_execution_receipt_cannot_authorize(registry):
    report = {
        "study": "R1",
        "completed": True,
        "scientific": True,
        "positive": True,
        "prereg_hash": "sha256:missing",
        "run_ids": [],
        "by_model": {},
    }
    gate = registry.register_gate("R1", report)
    assert not gate["eligible"]
    assert any("execution_receipt" in error for error in gate["errors"])


def test_r1_execution_provenance_is_rechecked_for_existing_authority(registry, monkeypatch):
    from analysis.r1 import R1Readiness

    invalid = []
    monkeypatch.setattr(R1Readiness, "verify", lambda self, report: invalid)
    gate = {"gate_id": "gate_unit_authority", "name": "R1", "report": {}, "artifact_hashes": {}}
    assert registry._gate_still_valid(gate)
    invalid.append("actual settlement evidence changed")
    assert not registry._gate_still_valid(gate)


def test_irb_requires_protocol_consent_and_research_provenance(registry):
    missing = registry.register_gate(
        "IRB", {"study": "IRB", "completed": True, "scientific": True, "decision": "approved"}
    )
    assert not missing["eligible"]
    accepted = registry.register_gate(
        "IRB",
        {
            "study": "IRB",
            "completed": True,
            "scientific": True,
            "decision": "approved",
            "protocol_ref": "approved-protocol.pdf",
            "consent_ref": "consent.pdf",
        },
    )
    assert accepted["eligible"]


def test_revocation_withdraws_existing_authority_and_survives_restart(
    registry, verified_pilot_contract
):
    registration, ids = research_runs(registry)
    gate = registry.register_gate("shakedown", pilot_report(registration, ids))
    assert registry.require_stage(1)["research_authorized"]
    withdrawn = registry.revoke_gate(gate["gate_id"], reason="Provider metering audit withdrawn")
    assert withdrawn["revoked"] and registry.gates()[0]["revocation"] == withdrawn
    assert (
        registry.revoke_gate(gate["gate_id"], reason="Retry keeps first immutable reason")
        == withdrawn
    )
    assert (
        registry.register_gate("shakedown", pilot_report(registration, ids))["gate_id"]
        == gate["gate_id"]
    )
    with pytest.raises(GateRequired):
        registry.require_stage(1)
    replacement = RegistryService(registry.world)
    with pytest.raises(GateRequired):
        replacement.require_stage(1)


def test_caller_metric_booleans_never_substitute_for_actual_pilot_assessment(registry):
    registration, ids = research_runs(registry)
    report = pilot_report(registration, ids)
    gate = registry.register_gate("shakedown", report)
    assert not gate["eligible"] and any("readiness evidence" in error for error in gate["errors"])
    with pytest.raises(GateRequired):
        registry.require_stage(1)


def test_version_registry_is_content_addressed(registry):
    first = registry.register_version("policy", {"robots": True})
    assert registry.register_version("policy", {"robots": True}) == first
    second = registry.register_version("policy", {"robots": False})
    assert first["version_id"] != second["version_id"]
