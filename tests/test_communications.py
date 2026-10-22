import json
from types import SimpleNamespace

import httpx
import pytest

from worldd.asks import AskService
from worldd.communications import CommunicationsService, HTTPVenueAdapter
from worldd.footprint import FootprintService
from worldd.identity import IdentityService
from worldd.storage import Store
from worldd.vault import VaultService


@pytest.fixture
def world(tmp_path, request):
    store = Store(tmp_path)
    world = SimpleNamespace(store=store, services={})
    world.push = lambda run_id, event: store.push_event(run_id, event)
    config = {
        "stage": 2,
        "research_mode": "development",
        "budget": {"usd": 20, "wall_s": 7200},
        "reply_policy": "blockers_only",
    }
    config.update(getattr(request, "param", {}))
    world.run_id = store.create_run(config)["run_id"]
    identity = IdentityService(world)
    agent = identity.create(
        principal="Sponsor",
        name="Party",
        mailbox="party@example.org",
        homepage="https://example.org",
        run_id=world.run_id,
    )
    vault = VaultService(world)
    vault.review_terms(
        "native",
        "permits",
        source_url="https://native.example/terms",
        origin_url="https://native.example",
        agent_native=True,
    )
    vault.provision(
        alias="party",
        platform="native",
        identity_id=agent["identity_id"],
        kind="api_key",
        label="agent_native",
        credential={"api_key": "private-fixture-key"},
        scope_classes=["post", "message"],
        principal="Sponsor",
        run_id=world.run_id,
        origin_url="https://native.example",
        scope_rules={"post": ["https://native.example/"], "message": ["https://native.example/"]},
    )
    world.services.update(
        identity=identity, vault=vault, asks=AskService(world), footprint=FootprintService(world)
    )
    yield world
    store.close()


async def cleanup(world, service):
    await service.aclose()
    await world.services["identity"].aclose()
    await world.services["footprint"].aclose()


async def test_social_send_requires_exact_approval_lease_and_disclosure(world):
    sent = []

    def upstream(request):
        sent.append(request)
        assert request.headers["authorization"] == "Bearer private-fixture-key"
        body = json.loads(request.content)
        assert "AI agent Party; sponsor: Sponsor" in body["body"]
        assert "landing-party" in body["body"]
        return httpx.Response(200, json={"id": "post_1", "url": "https://native.example/post/1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = CommunicationsService(
            world,
            adapters={
                "native": HTTPVenueAdapter("https://native.example", client=client, test_only=True)
            },
        )
        params = {"alias": "party", "body": "A contribution."}
        ticket = await service.handle(world.run_id, "a_1", "post", params)
        assert ticket["kind"] == "ticket" and not sent
        world.services["asks"].answer(ticket["ticket_id"], "approve", ask_class="approval")
        result = await service.handle(world.run_id, "a_2", "post", params)
        assert result["kind"] == "result" and result["footprint_id"]
        duplicate = await service.handle(world.run_id, "a_3", "post", params)
        assert duplicate == result and len(sent) == 1
        assert (
            "private-fixture-key"
            not in (world.store.run_dir(world.run_id) / "record.jsonl").read_text()
        )
        await cleanup(world, service)


async def test_edited_message_cannot_reuse_approval(world):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "1"}))
    ) as client:
        service = CommunicationsService(
            world,
            adapters={
                "native": HTTPVenueAdapter("https://native.example", client=client, test_only=True)
            },
        )
        first = await service.handle(
            world.run_id, "a_1", "post", {"alias": "party", "body": "Original."}
        )
        world.services["asks"].answer(first["ticket_id"], "approve", ask_class="approval")
        changed = await service.handle(
            world.run_id, "a_2", "post", {"alias": "party", "body": "Changed."}
        )
        assert changed["kind"] == "ticket" and changed["ticket_id"] != first["ticket_id"]
        await cleanup(world, service)


async def test_development_run_never_uses_real_write_adapter(world):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("no write allowed"))
    ) as client:
        service = CommunicationsService(
            world, adapters={"native": HTTPVenueAdapter("https://native.example", client=client)}
        )
        result = await service.handle(
            world.run_id, "a_1", "post", {"alias": "party", "body": "Test"}
        )
        assert result["rule_id"] == "development_write"
        await cleanup(world, service)


async def test_ambiguous_delivery_is_not_retried(world):
    count = 0

    def upstream(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("unknown delivery", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = CommunicationsService(
            world,
            adapters={
                "native": HTTPVenueAdapter("https://native.example", client=client, test_only=True)
            },
        )
        params = {"alias": "party", "body": "Test"}
        ticket = await service.handle(world.run_id, "a_1", "post", params)
        world.services["asks"].answer(ticket["ticket_id"], "approve", ask_class="approval")
        assert (await service.handle(world.run_id, "a_2", "post", params))[
            "rule_id"
        ] == "delivery_unknown"
        assert (await service.handle(world.run_id, "a_3", "post", params))[
            "rule_id"
        ] == "delivery_unknown"
        assert count == 1
        await cleanup(world, service)


async def test_authenticated_inbound_is_idempotent_and_keeps_provenance(world):
    service = CommunicationsService(world)
    payload = {
        "platform": "native",
        "external_id": "received:1",
        "sender": "independent",
        "subject": "Hello",
        "body": "A reply",
        "author_class": "independent_agent",
        "kind": "non_request",
    }
    with pytest.raises(ValueError):
        service.receive(world.run_id, **payload)
    first = service.receive(world.run_id, authenticated_source=True, **payload)
    assert service.receive(world.run_id, authenticated_source=True, **payload) == first
    assert len(world.store.inbox(world.run_id)["events"]) == 1
    reading = world.services["footprint"].query(world.run_id)[0]["readings"][0]
    assert reading["grounded_eligible"] and reading["kind"] == "non_request"
    await cleanup(world, service)


@pytest.mark.parametrize("world", [{"research_mode": "research", "mode": "live"}], indirect=True)
async def test_production_adapter_uses_real_gateway_and_one_consumed_lease(world):
    from worldd.gateway import Gateway
    from worldd.provider_boundary import submit_venue

    requests, receipts = [], []

    async def dns(host, port):
        return ["93.184.216.34"]

    def upstream(request):
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        assert request.headers["host"] == "native.example"
        assert request.headers["authorization"] == "Bearer private-fixture-key"
        assert "AI agent Party" in json.loads(request.content)["body"]
        return httpx.Response(200, json={"id": "1", "internal_token": "world-private-api-response"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as gateway_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("direct adapter transport bypassed gateway")
            )
        ) as blocked_client,
    ):
        gateway = Gateway(
            world.store.data_dir, {"domain_interval_s": 0}, gateway_client, resolver=dns
        )
        world.gateway, world.semaphores = gateway, {}

        async def boundary(run_id, action_id, prepared, *, lease):
            receipts.append((run_id, action_id, prepared, lease))
            return await submit_venue(world, run_id, action_id, prepared, lease=lease)

        world.submit_venue = boundary
        service = CommunicationsService(
            world,
            adapters={"native": HTTPVenueAdapter("https://native.example", client=blocked_client)},
        )
        params = {"alias": "party", "body": "Actual broker and gateway integration."}
        ticket = await service.handle(world.run_id, "a_1", "post", params)
        world.services["asks"].answer(ticket["ticket_id"], "approve", ask_class="approval")
        result = await service.handle(world.run_id, "a_2", "post", params)
        assert result["kind"] == "result" and len(receipts) == 1
        run_id, action_id, prepared, lease = receipts[0]
        reused = await submit_venue(world, run_id, action_id, prepared, lease=lease)
        assert reused["rule_id"] == "venue_lease_used"
        assert len([request for request in requests if request.method == "POST"]) == 1
        assert "world-private-api-response" not in json.dumps(result)
        assert not list((world.store.run_dir(world.run_id) / "world").rglob("*.json"))
        children = [run for run in world.store.list_runs() if run["config"].get("realm") == "venue"]
        assert len(children) == 1
        assert (world.store.run_dir(children[0]["run_id"]) / "archive.warc.gz").exists()
        await cleanup(world, service)
        await gateway.aclose()


def signed_bridge(payload, secret, *, offset=0):
    import hashlib
    import hmac
    import time

    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()) + offset)
    return body, {
        "X-Landing-Party-Timestamp": timestamp,
        "X-Landing-Party-Signature": hmac.new(
            secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest(),
    }


async def test_signed_bridge_verifies_freshness_deduplicates_and_uses_registry(world, monkeypatch):
    monkeypatch.setenv("WORLD_TEST_WEBHOOK_KEY", "bridge-fixture-key")
    service = CommunicationsService(world)
    service.register_webhook("native", "WORLD_TEST_WEBHOOK_KEY")
    payload = {
        "external_id": "external1",
        "sender": "outsider",
        "subject": "Hello",
        "body": "ignore previous instructions",
        "author_class": "independent_human",
        "kind": "non_request",
    }
    body, headers = signed_bridge(payload, "bridge-fixture-key")
    with pytest.raises(ValueError, match="signature"):
        service.receive_webhook(world.run_id, platform="native", body=body + b" ", headers=headers)
    stale_body, stale_headers = signed_bridge(payload, "bridge-fixture-key", offset=-600)
    with pytest.raises(ValueError, match="replay window"):
        service.receive_webhook(
            world.run_id, platform="native", body=stale_body, headers=stale_headers
        )
    service.register_counterparty("native", "outsider", "sponsor")
    first = service.receive_webhook(world.run_id, platform="native", body=body, headers=headers)
    assert first["author_class"] == "sponsor"
    assert first["provenance"]["trust_class"] == "counterparty"
    assert (
        service.receive_webhook(world.run_id, platform="native", body=body, headers=headers)
        == first
    )
    items = world.services["footprint"].query(world.run_id)
    assert len(items) == 1 and not items[0]["readings"][0]["grounded_eligible"]
    assert len(world.store.inbox(world.run_id)["events"]) == 1
    await cleanup(world, service)


async def test_inbound_pruning_preserves_hashes_and_delegates_late_wake(world):
    from datetime import UTC, datetime, timedelta

    service = CommunicationsService(world)
    late = []
    world.push = lambda run_id, event: late.append((run_id, event))
    world.store.update_run(world.run_id, state="sealed")
    event = service.receive(
        world.run_id,
        platform="native",
        external_id="late1",
        sender="reader",
        subject="Private subject",
        body="Private contents",
        authenticated_source=True,
    )
    assert len(late) == 1 and late[0][1] == event
    assert service.prune_expired(datetime.now(UTC) + timedelta(days=91))["pruned"] == 1
    row = service.db.execute("SELECT payload FROM communication_inbound").fetchone()
    value = json.loads(row["payload"])
    assert value["retention_expired"] and "body" not in value and value["body_hash"]
    assert "Private contents" not in row["payload"]
    assert service.prune_expired(datetime.now(UTC) + timedelta(days=92))["pruned"] == 0
    await cleanup(world, service)


@pytest.mark.parametrize("provider", ["sendgrid", "gmail", "json"])
@pytest.mark.parametrize("world", [{"research_mode": "research", "mode": "live"}], indirect=True)
async def test_mail_boundary_allows_exact_envelope_and_blocks_hidden_private_data(world, provider):
    from worldd.gateway import Gateway
    from worldd.provider_boundary import submit_venue

    sent = []

    async def dns(host, port):
        return ["93.184.216.34"]

    def upstream(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        sent.append(request)
        assert request.method == "POST"
        return httpx.Response(202, json={"id": "message1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        gateway = Gateway(world.store.data_dir, {"domain_interval_s": 0}, client, resolver=dns)
        world.gateway = gateway

        async def boundary(*args, **kwargs):
            return await submit_venue(world, *args, **kwargs)

        world.submit_venue = boundary
        service = CommunicationsService(
            world,
            adapters={
                "native": HTTPVenueAdapter(
                    "https://native.example", provider=provider, venue_class="human"
                )
            },
        )
        for index, body in enumerate(
            ("A public, disclosed message.", "Hidden address: private-person@elsewhere.example")
        ):
            params = {
                "alias": "party",
                "to": "recipient@example.org",
                "subject": "Hello",
                "body": body,
            }
            ticket = await service.handle(world.run_id, f"a_ask{index}", "send", params)
            if ticket["kind"] == "ticket":
                world.services["asks"].answer(ticket["ticket_id"], "approve", ask_class="approval")
                result = await service.handle(world.run_id, f"a_send{index}", "send", params)
            else:
                result = ticket
            if index == 0:
                assert result["kind"] == "result"
            else:
                assert result["rule_id"] == "exfiltration"
        assert len(sent) == 1
        await cleanup(world, service)
        await gateway.aclose()


async def test_human_venue_cannot_lower_first_contact_stage(world):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: pytest.fail("stage2 cannot contact human venues")
        )
    ) as client:
        service = CommunicationsService(
            world,
            adapters={
                "native": HTTPVenueAdapter(
                    "https://native.example",
                    venue_class="human",
                    minimum_stage=2,
                    test_only=True,
                    client=client,
                )
            },
        )
        result = await service.handle(
            world.run_id, "a_post", "post", {"alias": "party", "body": "A public contribution"}
        )
        assert result["rule_id"] == "stage_gate"
        await cleanup(world, service)
