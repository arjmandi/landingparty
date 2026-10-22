"""A resumed session never becomes another independent life repeat."""

# ruff: noqa: F811 -- pytest fixture imports intentionally match parameters

from datetime import timedelta

from analysis.cohort import StudyReport
from tests.test_study_lives import life_cohort, planned, until  # noqa: F401


async def test_whole_life_report_retains_sessions_without_counting_them_as_repeats(life_cohort):
    app, client, clock, _seen = life_cohort
    world = app.state.world
    plan, life_ids = await planned(client)
    response = await client.post(
        f"/v1/studies/{plan['study_id']}/execute", json={"execute": True, "budget_usd": 4}
    )
    assert response.status_code == 200, response.text
    lives = world.services["lives"]
    await until(
        lambda: all(
            lives.get(life)["state"] == "sleeping" and lives.get(life)["active_run_id"] is None
            for life in life_ids
        )
    )
    interim = StudyReport(world).build(plan["study_id"])
    assert interim["unit"] == "life" and interim["n_started"] == 2
    assert interim["sealed"] is False
    assert interim["n_started_sessions"] == 2
    assert all(row["primary"]["status"] == "unmeasured" for row in interim["life_rows"])
    clock[0] += timedelta(days=7)
    for life_id in life_ids:
        await lives.tick(life_id)
    await until(lambda: world.services["studies"].get(plan["study_id"])["state"] == "completed")
    report = StudyReport(world).build(plan["study_id"])
    assert report["n_started"] == 2 and report["n_started_sessions"] == 4
    assert report["sealed"] is True
    assert {row["life_id"] for row in report["life_rows"]} == set(life_ids)
    assert all(len(row["run_ids"]) == 2 for row in report["life_rows"])
    assert all(len(row["session_attrition"]) == 2 for row in report["life_rows"])
    assert all(group["n_started"] == 1 for group in report["cells"].values())
    assert len(report["attrition_rows"]) == 4
    service = world.services["studies"]
    uncertain = service.get(plan["study_id"])
    uncertain["life_analysis"] = {
        uncertain["life_runners"][0]["runner_id"]: {
            "state": "uncertain",
            "reason": "fixture interrupted measurement",
        }
    }
    service._save(uncertain)
    partial = StudyReport(world).build(plan["study_id"])
    assert partial["completeness"]["verdict"] == "partial"
    assert partial["requested_measurements"][0]["state"] == "uncertain"
    assert partial["n_started"] == 2
