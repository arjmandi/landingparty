"""Historical goal scores use bytes captured at actual committed debits."""

import json

import pytest

from analysis.pipeline import PostRunPipeline
from analysis.publication import load_measurements
from worldd.app import create_app
from worldd.models import RunConfig
from worldd.util import digest


class Gateway:
    async def aclose(self):
        pass


@pytest.mark.parametrize("amounts", [(0.1, 0.15, 0.25, 0.5), (0.55, 0.45)])
async def test_actual_checkpoints_score_historical_bytes_and_publish_both_axes(tmp_path, amounts):
    world = create_app(tmp_path, gateway=Gateway()).state.world
    run_id = world.create_run(RunConfig(budget={"usd": 1}))["run_id"]
    root = world.store.run_dir(run_id)
    try:
        for index, amount in enumerate(amounts):
            (root / "work/out/answer.json").write_text(
                json.dumps({"answer": 0 if index == 0 else 42})
            )
            await world.act(run_id, "status", {})
            world.store.debit(run_id, "model", amount)
        await world.end_run(run_id)
        protocol = {
            "protocol_id": "fixture-historical-core@1",
            "core_threshold": 1,
            "checklist": [
                {
                    "item_id": "answer",
                    "check": {
                        "kind": "json_equals",
                        "path": "work-snapshot/out/answer.json",
                        "pointer": "/answer",
                        "expected": 42,
                    },
                }
            ],
            "curve_score_path": ["checklist", "pass_fraction"],
        }
        path = root / "grader.json"
        path.write_text(json.dumps(protocol))
        before = world.store.records(run_id)
        result = await PostRunPipeline(world).run(
            run_id, {"grading_protocol": {"path": path.name, "hash": digest(path.read_bytes())}}
        )
        assert result["stages"]["grading-checkpoints"]["status"] == "completed"
        measurements = load_measurements(world.store, run_id)
        checkpoints = measurements["grading-checkpoints"]
        assert [row["status"] for row in checkpoints["rows"]] == ["measured"] * 4
        assert checkpoints["rows"][0]["score"] == 0
        assert checkpoints["rows"][-1]["score"] == 1
        assert measurements["grading"]["checklist"]["pass_fraction"] == 1
        assert len(checkpoints["measurement"]["checkpoints"]) == len(amounts)
        assert "auc_log_cost" in checkpoints["measurement"]["usd"]
        assert "auc_log_cost" in checkpoints["measurement"]["world_actions"]
        report = world.report(run_id)
        assert any(curve["name"] == "historical_goal_score" for curve in report["curves"])
        exports = root / "analysis/publication"
        figures = [path.read_text() for path in exports.glob("figure-*.svg")]
        assert any(
            "historical goal score" in figure
            and "Source dollars (USD)" in figure
            and "World actions" in figure
            for figure in figures
        )
        assert world.store.records(run_id) == before
    finally:
        await world.aclose()
