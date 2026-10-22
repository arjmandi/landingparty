"""The real metered NER runner reaches passive reports and portable figures."""

# ruff: noqa: F811 -- pytest fixture registration imports intentionally match parameters

from analysis.pipeline import PostRunPipeline
from analysis.publication import render_publication
from tests.test_entities import config, instrument, measured  # noqa: F401


async def test_entity_pipeline_preserves_exploratory_column_and_exports_actual_curves(measured):
    world, source, model, requests, _modes = measured
    before, spend = world.store.records(source), world.store.spend(source)
    result = await PostRunPipeline(world, model).run(
        source,
        {
            "measurement_budget_usd": 10,
            "agent_family": "source-family",
            "entities": config(instrument(world, source)),
        },
    )
    assert result["stages"]["entities"]["status"] == "completed"
    assert len(requests) == 42
    report = world.report(source)
    reading = report["measurement_readings"]["entities"]
    assert reading["column"] == "J" and reading["headline"] is False
    curve = next(row for row in report["curves"] if row["name"] == "discovered_entities")
    assert curve["column"] == "J" and curve["points"][-1]["value"] == 3
    assert curve["points"][-1]["world_actions"] == 42
    assert "distinct labelled entities" in render_publication(report, "html")
    assert world.store.records(source) == before and world.store.spend(source) == spend
