import csv
import hashlib
import io
import json

import pytest

from analysis.cohort import StudyReport, primary_reading, reality_strata
from analysis.experiments import validate_prereg
from analysis.publication import render_publication
from tests.test_studies import study_app  # noqa: F401 - shared actual ASGI fixture
from worldd.models import RunConfig
from worldd.runtime import World
from worldd.storage import Store
from worldd.studies import StudyService


class Gateway:
    async def aclose(self):
        pass


def test_primary_only_uses_registered_actual_grounded_or_verifiable_path():
    report = {"columns": {"V": {"readings": {"core": {"passed": False}}}, "J": {"rating": 1}}}
    value = primary_reading(
        report, {"source_path": ["columns", "V", "readings", "core", "passed"], "threshold": 1}
    )
    assert value["value"] == 0 and value["passed"] is False
    assert (
        primary_reading(report, {"source_path": ["columns", "J", "rating"], "threshold": 1})[
            "status"
        ]
        == "unmeasured"
    )
    assert (
        primary_reading(report, {"name": "some scalar", "threshold": 1})["status"] == "unmeasured"
    )


def test_actual_reality_strata_keep_missing_and_the_upper_endpoint_separate():
    rows = [
        {"run_id": str(i), "reality_belief": rating, "primary": {"value": 0.5}}
        for i, rating in enumerate((0, 25, 50, 75, 100, {"status": "unmeasured"}))
    ]
    result = reality_strata(rows, comparable=True)
    assert result["0–25"]["n_started"] == result["25–50"]["n_started"] == 1
    assert result["75–100"]["n_started"] == 2
    assert result["unmeasured"]["n_started"] == 1
    assert reality_strata(rows)["75–100"]["primary_distribution"]["status"] == "unmeasured"


async def test_study_report_retains_every_started_row_and_blocks_unverified_pooling(tmp_path):
    world = World(Store(tmp_path), Gateway())
    service = world.services["studies"] = StudyService(world)
    ids, attrition = [], []
    for seed in range(5):
        run_id = world.create_run(RunConfig(goal="fixture task", seed=seed))["run_id"]
        world.store.debit(run_id, "model", 0.1)
        if seed != 4:
            await world.end_run(run_id)
        ids.append(run_id)
        attrition.append({"run_id": run_id, "cell": "test", "seed": seed})
    prereg = {
        "experiment_id": "fixture-cohort",
        "primary_metric": {
            "name": "actual spend fixture",
            "source_path": ["columns", "G", "spend_usd"],
            "threshold": 0.1,
        },
        "seed_count": 5,
        "seed_count_rationale": "software fixture",
        "stopping_rule": "retain all started",
        "likely_null": "no effect",
    }
    study_id = "study_fixture_cohort"
    service._save(
        {
            "study_id": study_id,
            "kind": "experiment",
            "state": "failed",
            "prereg": prereg,
            "prereg_hash": validate_prereg(prereg)["prereg_hash"],
            "run_ids": ids,
            "result": {"attrition": attrition},
        }
    )
    before = {run_id: world.store.records(run_id) for run_id in ids}
    try:
        result = StudyReport(world).build(study_id)
        group = result["cells"]["test"]
        assert result["n_started"] == len(group["rows"]) == len(result["attrition_rows"]) == 5
        assert group["rows"][-1]["primary"]["status"] == "unmeasured"
        assert all(row["status"] == "unmeasured" for row in group["pass_k"].values())
        assert group["primary_distribution"]["status"] == "unmeasured"
        assert group["n_unique_seeds"] == 5 and result["costs"]["usd_total"] == pytest.approx(0.5)
        assert not result["scientific_gate_passed"]
        output = list(csv.DictReader(io.StringIO(render_publication(result, "csv"))))
        assert output and all(row["run_or_life_id"] == study_id for row in output)
        assert study_id in render_publication(result, "markdown")
        assert before == {run_id: world.store.records(run_id) for run_id in ids}
    finally:
        await world.aclose()


async def test_carryover_consumes_actual_donor_dag_receipts_and_measured_artifacts(study_app):  # noqa: F811 - injected shared fixture
    app, _client, _provider_calls = study_app
    world = app.state.world
    world.gateway.policy.update(
        global_requests_per_minute=10000, domain_max_requests=10000, domain_burst=10000
    )
    anchor = world.create_run(RunConfig())["run_id"]
    await world.end_run(anchor)
    canaries = world.services["canaries"]
    bank = canaries.register_block_bank(
        [{"url": f"https://example.org/profile/{i}", "domain_class": "fixture"} for i in range(300)]
    )
    profile = await canaries.crawl_profile(anchor, bank["bank_id"], concurrency=16)
    assert profile["status"] == "measured" and profile["coverage"] == 1, profile[
        "unresolved_by_rule"
    ]
    protocol = {
        "protocol_id": "actual-fixture-core",
        "core_threshold": 1,
        "checklist": [
            {
                "item_id": "answer",
                "check": {
                    "kind": "json_equals",
                    "path": "work-snapshot/out/answer.json",
                    "expected": 42,
                },
            }
        ],
    }
    raw = json.dumps(protocol).encode()
    descriptor = {"path": "grader.json", "hash": "sha256:" + hashlib.sha256(raw).hexdigest()}

    async def harness(created, cell):
        root = world.store.run_dir(created["run_id"])
        (root / "grader.json").write_bytes(raw)
        (root / "work/out/answer.json").write_text("42" if cell["cell_id"] == "imported" else "0")
        (root / "work/donor.txt").write_text("actual carried workspace")
        return {"run_id": created["run_id"]}

    service = world.services["studies"] = StudyService(world, harnesses={"B1": harness})
    base = {
        "baseline": "B1",
        "seeds": list(range(5)),
        "model": "fixture-model",
        "max_steps": 1,
        "max_tokens": 20,
        "config": {
            "stage": 1,
            "family": "F5",
            "goal": "fixture task",
            "learning_mechanism": "workspace",
            "block_profile_id": profile["block_profile_id"],
            "budget": {"usd": 1, "wall_s": 30},
        },
        "analysis_options": {"grading_protocol": descriptor},
    }
    cells = [
        {**base, "cell_id": "donor", "episode": 1},
        {
            **base,
            "cell_id": "imported",
            "episode": 2,
            "depends_on": {
                "workspace": {"source_cell": "donor"},
                "archive": {"source_cell": "donor"},
            },
        },
        {
            **base,
            "cell_id": "stripped",
            "episode": 2,
            "depends_on": {"archive": {"source_cell": "donor"}},
        },
    ]
    prereg = {
        "experiment_id": "actual-carryover-fixture",
        "primary_metric": {
            "name": "actual core",
            "source_path": ["columns", "V", "readings", "grading", "checklist", "pass_fraction"],
            "threshold": 1,
        },
        "seed_count": 5,
        "seed_count_rationale": "engineering fixture",
        "stopping_rule": "all started rows",
        "likely_null": "no transfer",
        "carryover_pairs": [
            {"name": "workspace", "imported_cell": "imported", "stripped_cell": "stripped"}
        ],
    }
    try:
        preview = service.plan({"prereg": prereg, "cells": cells})
        prereg["dependency_protocol_hash"] = preview["dependency_protocol"]["hash"]
        planned = service.plan({"prereg": prereg, "cells": cells})
        executed = await service.execute(
            planned["study_id"], execute=True, budget_usd=15, options={"wait": True}
        )
        assert executed["state"] == "completed", {
            "stopped": (executed.get("result") or {}).get("stopped"),
            "rows": [
                {key: row.get(key) for key in ("cell", "seed", "status", "reason")}
                for row in (executed.get("result") or {}).get("attrition", [])
            ],
        }
        report = StudyReport(world).build(planned["study_id"])
        contrast = report["carryover_gain"]["contrasts"][0]
        assert len(contrast["rows"]) == 5
        assert all(
            row["status"] == "measured" and row["dependency_receipts"] for row in contrast["rows"]
        ), contrast
        assert contrast["by_learning_mechanism"]["workspace"]["gain"] == 1
        assert (
            not contrast["pooled_across_learning_mechanisms"]
            and not report["scientific_gate_passed"]
        )
    finally:
        await service.aclose()
