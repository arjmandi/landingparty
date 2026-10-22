import base64
import html
import json
import re
from datetime import datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from worldd.app import create_app
from worldd.models import RunConfig
from worldd.public_identity import create_public_app
from worldd.security import create_agent_app
from worldd.util import canonical


@pytest.fixture
async def published(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_CONTROL_TOKEN", "fixture-researcher-authority")
    app = create_app(tmp_path)
    world = app.state.world
    lives = world.services["lives"]
    life_id = lives.create({"budget": {"usd_life": 10, "wall_life_days": 28}})["life_id"]
    run = world.create_run(RunConfig(life_id=life_id, budget={"usd": 1, "wall_s": 300}))
    identity = world.services["identity"].create(
        principal="Responsible researcher",
        name="Disclosed <agent>",
        mailbox="agent@example.org",
        homepage="https://identity.example/party",
        life_id=life_id,
    )
    revocations = []
    world.services["footprint"].revoker = lambda *args: revocations.append(args) or True
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(create_public_app(world), client=("198.51.100.10", 1234)),
            base_url="https://identity.example",
        ) as public,
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://control") as control,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(create_agent_app(world)), base_url="http://agent"
        ) as agent,
    ):
        yield world, run["run_id"], identity["identity_id"], public, control, agent, revocations
    await world.aclose()


async def test_public_disclosure_is_signed_escaped_and_remains_after_life_ends(published):
    world, run_id, identity_id, public, _control, _agent, _revoked = published
    response = await public.get("/party")
    assert response.status_code == 200
    assert "Disclosed &lt;agent&gt;" in response.text and "Responsible researcher" in response.text
    marker = json.loads(html.unescape(re.search(r'data-landing-party="([^"]+)"', response.text)[1]))
    Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(marker["verification_key"])).verify(
        base64.urlsafe_b64decode(marker["signature"]), canonical(marker["metadata"]).encode()
    )
    assert marker["metadata"]["run_id"] == run_id and marker["metadata"]["agent"] is True
    assert "default-src 'none'" in response.headers["content-security-policy"]
    world.services["identity"].mark_ended(world.store.get_run(run_id)["config"]["life_id"])
    assert "life has ended" in (await public.get(f"/identities/{identity_id}")).text
    assert (await public.get("/party/incident")).status_code == 200
    assert (await public.get("https://unregistered.example/party")).status_code == 404
    assert (await public.get("/unknown")).status_code == 404


async def test_unverified_report_needs_authorized_identity_bound_human_triage(published):
    world, run_id, _identity_id, public, control, agent, revoked = published
    report_text = "Private complaint <script>alert(1)</script>"
    received = await public.post(
        "/party/incident", json={"report": report_text, "contact": "reporter@example.org"}
    )
    assert received.status_code == 202 and set(received.json()) == {"receipt_id", "received"}
    assert not revoked and not world.services["footprint"].query(run_id, include_incidents=True)
    report_id = received.json()["receipt_id"]
    queue = "/v1/public-incident-reports"
    review = f"{queue}/{report_id}/triage"
    assert (await public.get(queue)).status_code == 404
    assert (await agent.get(queue)).status_code == 404
    assert (await control.get(queue)).status_code == 401
    headers = {"X-World-Control-Token": "fixture-researcher-authority"}
    pending = (await control.get(queue, params={"run_id": run_id}, headers=headers)).json()[
        "reports"
    ]
    assert len(pending) == 1 and pending[0]["report"] == report_text and not pending[0]["verified"]
    other = world.create_run(RunConfig(budget={"usd": 1, "wall_s": 300}))["run_id"]
    world.services["identity"].create(
        principal="Other owner",
        name="Other",
        mailbox="other@example.org",
        homepage="https://identity.example/other",
        run_id=other,
    )
    assert (await control.get(queue, params={"run_id": other}, headers=headers)).json()[
        "reports"
    ] == []
    with pytest.raises(ValueError, match="actual source run"):
        await world.services["incident_reports"].triage(
            report_id, decision="accept", run_id=other, platform="native"
        )
    assert not revoked
    accepted = await control.post(
        review,
        headers=headers,
        json={
            "decision": "accept",
            "run_id": run_id,
            "platform": "native",
            "owner": "Human operator",
        },
    )
    assert accepted.status_code == 200 and accepted.json()["revocation_confirmed"]
    assert revoked == [(run_id, "native", None)]
    assert (await control.post(review, headers=headers, json={"decision": "accept"})).json()[
        "state"
    ] == "accepted"
    assert len(revoked) == 1
    item = world.services["footprint"].query(run_id, include_incidents=True)[0]
    assert item["kind"] == "incident" and item["metadata"]["owner"] == "Human operator"
    assert report_id in item["metadata"]["detail"]
    assert report_text not in json.dumps(item) and "reporter@example.org" not in json.dumps(item)
    assert world.store.verify_record(run_id)["valid"]


async def test_intake_limits_fields_actual_sender_rate_and_private_body_retention(published):
    world, run_id, identity_id, public, _control, _agent, revoked = published
    endpoint = f"/identities/{identity_id}/incident"
    assert (
        await public.post(endpoint, json={"report": "x", "platform": "arbitrary"})
    ).status_code == 400
    assert (
        await public.post(
            endpoint, content=b"x" * 16385, headers={"content-type": "application/json"}
        )
    ).status_code == 413
    assert (
        await public.post(
            endpoint,
            content="report=a&report=b",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    ).status_code == 400
    assert (await public.post(endpoint, content="unsupported")).status_code == 415
    assert (
        await public.post("/identities/unknown/incident", json={"report": "x"})
    ).status_code == 404
    for index in range(5):
        reply = await public.post(
            endpoint,
            json={"report": f"private report {index}"},
            headers={"X-Forwarded-For": f"203.0.113.{index}"},
        )
        assert reply.status_code == 202
    assert (await public.post(endpoint, json={"report": "sixth"})).status_code == 429
    assert not revoked
    service = world.services["incident_reports"]
    rows = service.reports(run_id=run_id)
    assert len(rows) == 5
    assert (
        service.prune_expired(datetime.fromisoformat(rows[0]["created_at"]) + timedelta(days=91))[
            "pruned"
        ]
        == 5
    )
    assert all(
        "report" not in row and row["body_expired"] and row["body_hash"]
        for row in service.reports()
    )
    result = await service.triage(rows[0]["report_id"], decision="dismiss")
    assert result["state"] == "dismissed" and not revoked
