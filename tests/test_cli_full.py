"""The full researcher CLI reaches real control routes without implicit pilots."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from worldd.app import create_app
from worldd.cli import main


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "fixture-control-secret")
    app = create_app(tmp_path)
    with TestClient(
        app,
        base_url="http://127.0.0.1:7777",
        headers={"X-World-Control-Token": "fixture-control-secret"},
    ) as client:
        yield app, client


def prereg():
    return {
        "primary_metric": {"name": "completion", "threshold": 1},
        "seed_count": 1,
        "seed_count_rationale": "CLI integration fixture",
        "stopping_rule": "Fixed single episode",
        "likely_null": "No evidence of any scientific effect",
        "exploratory_metrics": [],
    }


def test_shakedown_real_app_plans_and_explicit_execute_reports_prerequisites(control, capsys):
    app, client = control
    assert main(["shakedown"], client=client) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["state"] == "planned" and plan["run_count"] == 12
    assert not plan["scientific"] and app.state.world.store.list_runs() == []
    assert (
        main(
            ["shakedown", "execute", "--study-id", plan["study_id"], "--budget", "300"],
            client=client,
        )
        == 1
    )
    result = capsys.readouterr()
    assert "prerequisites" in result.err
    assert "fixture-control-secret" not in result.err
    assert app.state.world.store.list_runs() == []


def test_sealed_orders_freezes_actual_run_tuple_and_stardate_reports_missing_reference(
    control, tmp_path, capsys
):
    _, client = control
    run = client.post(
        "/v1/run", json={"packs": ["deliver"], "budget": {"usd": 1, "wall_s": 30}}
    ).json()
    path = tmp_path / "prereg.json"
    path.write_text(json.dumps(prereg()))
    assert main(["sealed-orders", str(path), "--run", run["run_id"]], client=client) == 0
    series = json.loads(capsys.readouterr().out)
    assert series["tuple"] == run["tuple"]
    assert main(["stardate", run["run_id"], "--against", series["series_id"]], client=client) == 0
    comparison = json.loads(capsys.readouterr().out)
    assert not comparison["comparable"]
    assert comparison["reasons"] == ["Registered series has no reference runs"]


def test_debrief_posts_passive_pipeline_and_requires_positive_measurement_budget(control, capsys):
    _, client = control
    run_id = client.post("/v1/run", json={"packs": ["deliver"]}).json()["run_id"]
    assert client.post("/v1/beam-up", json={"run_id": run_id}).status_code == 200
    assert main(["debrief", run_id], client=client) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["run_id"] == run_id
    assert main(["debrief", run_id, "--options", '{"interview":true}'], client=client) == 1
    assert "explicit --budget" in capsys.readouterr().err
    assert main(["debrief", run_id, "--budget", "-1"], client=client) == 1
    assert "finite and positive" in capsys.readouterr().err


def test_orbit_calls_actual_ended_life_scheduler_without_claiming_reading(control, capsys):
    _, client = control
    life = client.post("/v1/lives", json={"stage": 1}).json()
    end = client.post(f"/v1/lives/{life['life_id']}/end", json={"reason": "operator_stop"})
    assert end.status_code == 200, end.text
    assert main(["orbit", life["life_id"]], client=client) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["ended"] and value["afterlife"]["30"]["state"] == "pending"


def test_conn_discovers_exact_takeover_ticket_then_done_and_uses_control_auth(monkeypatch, capsys):
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "fixture-token-never-print")
    calls = []

    def handler(request):
        assert request.headers["X-World-Control-Token"] == "fixture-token-never-print"
        calls.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
        )
        if request.url.path == "/v1/asks":
            return httpx.Response(
                200,
                json={
                    "tickets": [
                        {"ticket_id": "t_one", "kind": "takeover", "body": {"session_id": "s_one"}}
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "session_id": "s_one",
                "state": "agent" if request.url.path.endswith("done") else "human",
            },
        )

    with httpx.Client(
        base_url="http://127.0.0.1:7777", transport=httpx.MockTransport(handler)
    ) as client:
        assert main(["conn", "s_one"], client=client) == 0
        assert main(["conn", "s_one", "--done"], client=client) == 0
    assert calls == [
        ("GET", "/v1/asks", None),
        (
            "POST",
            "/v1/sessions/s_one/takeover",
            {"ticket_id": "t_one", "reason": "operator_takeover"},
        ),
        ("POST", "/v1/sessions/s_one/takeover-done", None),
    ]
    assert "fixture-token-never-print" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:7778", "http://127.0.0.1:8081", "https://example.org:7777"]
)
def test_cli_refuses_to_send_control_token_to_other_listener(url, monkeypatch, capsys):
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "do-not-send")

    def handler(request):
        pytest.fail("Control token reached another listener")

    with httpx.Client(base_url=url, transport=httpx.MockTransport(handler)) as client:
        assert main(["health"], client=client) == 1
    assert "trusted control listener" in capsys.readouterr().err


def test_invalid_prereg_array_and_implicit_execution_fail_before_network(tmp_path, capsys):
    path = tmp_path / "invalid.json"
    path.write_text("[]")

    def handler(request):
        pytest.fail("Invalid local input reached control API")

    with httpx.Client(
        base_url="http://127.0.0.1:7777", transport=httpx.MockTransport(handler)
    ) as client:
        assert main(["sealed-orders", str(path)], client=client) == 1
        assert main(["shakedown", "execute"], client=client) == 1
    result = capsys.readouterr()
    assert "JSON object" in result.err and "positive --budget" in result.err


def test_report_format_prints_raw_document_or_exclusively_saves_bytes(tmp_path, capsys):
    def handler(request):
        assert request.url.path == "/v1/report" and request.url.params["run_id"] == "r_report"
        form = request.url.params.get("format", "json")
        return httpx.Response(
            200,
            content=b"<p>Actual assembled report</p>"
            if form == "html"
            else b"column,value\nG,unmeasured\n",
        )

    target = tmp_path / "report.csv"
    with httpx.Client(
        base_url="http://127.0.0.1:7777", transport=httpx.MockTransport(handler)
    ) as client:
        assert main(["report", "r_report", "--format", "html"], client=client) == 0
        assert capsys.readouterr().out == "<p>Actual assembled report</p>\n"
        assert (
            main(["report", "r_report", "--format", "csv", "--output", str(target)], client=client)
            == 0
        )
        assert target.read_bytes() == b"column,value\nG,unmeasured\n"
        assert (
            main(["report", "r_report", "--format", "csv", "--output", str(target)], client=client)
            == 1
        )
        assert "already exists" in capsys.readouterr().err
