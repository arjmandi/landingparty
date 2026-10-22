import csv
import io
import json

import pytest

from analysis.pipeline import PostRunPipeline
from analysis.publication import (
    attach_measurements,
    export_publication,
    load_measurements,
    render_publication,
)
from analysis.report import build_report
from worldd.models import RunConfig
from worldd.runtime import World
from worldd.storage import Store


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def sealed(tmp_path):
    world = World(Store(tmp_path), Gateway())
    world.price_table_id = "price-fixture@1"
    run_id = world.create_run(RunConfig())["run_id"]
    await world.end_run(run_id)
    try:
        yield world, run_id
    finally:
        await world.aclose()


def report(world, run_id):
    return build_report(world.store.get_run(run_id), world.store.records(run_id))


async def test_passive_report_loads_only_actual_hash_bound_measurements(sealed):
    world, run_id = sealed
    before = world.store.records(run_id)
    await PostRunPipeline(world).run(run_id)
    measured = load_measurements(world.store, run_id)
    assert measured["_integrity"]["valid"]
    assert measured["integrity"]["record"]["valid"]
    path = world.store.run_dir(run_id) / "analysis/integrity.json"
    path.write_text('{"invented":true}')
    measured = load_measurements(world.store, run_id)
    assert "integrity" not in measured
    assert measured["_integrity"]["invalid_stages"] == ["integrity"]
    assert world.store.records(run_id) == before


async def test_publication_keeps_horizon_metric_columns_and_portable_identity(sealed):
    world, run_id = sealed
    data = report(world, run_id)
    attach_measurements(
        data,
        {
            "horizon-failures": {
                "metrics": {
                    "thread_abandonment": {"column": "V", "measured_segments": 4, "rate": 0.25},
                    "commitment_contradiction": {
                        "column": "J",
                        "measured_segments": 4,
                        "rate": 0.5,
                    },
                }
            }
        },
    )
    assert data["columns"]["V"]["readings"]["horizon_thread_abandonment"]["rate"] == 0.25
    assert data["columns"]["J"]["readings"]["horizon_commitment_contradiction"]["rate"] == 0.5
    rows = list(csv.DictReader(io.StringIO(render_publication(data, "csv"))))
    assert all(row["run_or_life_id"] == run_id for row in rows)
    assert all(row["policy_id"] == data["identifiers"]["policy_id"] for row in rows)
    assert all(row["price_table_id"] == data["identifiers"]["price_table_id"] for row in rows)
    assert all(row["preamble_hash"] == data["identifiers"]["preamble_hash"] for row in rows)
    assert "0.25" in render_publication(data, "markdown")
    assert "seed_scope_verbatim" in render_publication(data, "markdown")


async def test_actual_two_axis_exports_and_omission_without_coordinates(sealed, tmp_path):
    world, run_id = sealed
    data = report(world, run_id)
    data["curves"] = [
        {
            "name": "measured",
            "column": "V",
            "unit": "score",
            "points": [
                {"usd": 0.1, "world_actions": 2, "value": 0.3, "observation_ref": "record:1"},
                {"usd": 0.3, "world_actions": 5, "value": 0.8, "observation_ref": "record:2"},
            ],
        },
        {"name": "no_action_axis", "column": "V", "points": [{"usd": 1, "value": 0.5}]},
    ]
    out = tmp_path / "publication"
    manifest = export_publication(data, out)
    assert manifest["figures"] == ["figure-1.svg"]
    assert manifest["omitted_figures"] == [
        {"curve": "no_action_axis", "reason": "actual coordinates missing for world_actions"}
    ]
    assert (out / "figure-1.png").read_bytes().startswith(b"\x89PNG")
    page = (out / "report.html").read_text()
    assert "<svg" in page and "<img src=" not in page
    assert run_id in page and data["identifiers"]["price_table_id"] in page
    assert "Attrition" in page and "World actions" in page
    assert json.loads((out / "exports.json").read_text()) == manifest


async def test_no_reading_creates_no_figures_and_preserves_explicit_missing(sealed, tmp_path):
    world, run_id = sealed
    data = report(world, run_id)
    manifest = export_publication(data, tmp_path / "empty")
    assert manifest["figures"] == []
    assert "unmeasured" in render_publication(data, "csv")
    assert "<svg" not in render_publication(data, "html")


async def test_malformed_manifest_stays_a_missing_measurement_not_a_broken_report(sealed):
    world, run_id = sealed
    await PostRunPipeline(world).run(run_id)
    path = world.store.run_dir(run_id) / "analysis/manifest.json"
    path.write_text("{broken")
    measurements = load_measurements(world.store, run_id)
    assert not measurements["_integrity"]["valid"]
    data = build_report(
        world.store.get_run(run_id), world.store.records(run_id), measurements=measurements
    )
    assert not data["measurement_integrity"]["valid"] and data["sealed"]


async def test_export_rejects_symlink_parent_before_creating_output(sealed, tmp_path):
    world, run_id = sealed
    target = tmp_path / "actual"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        export_publication(report(world, run_id), alias / "should-not-exist")
    assert not (target / "should-not-exist").exists()
