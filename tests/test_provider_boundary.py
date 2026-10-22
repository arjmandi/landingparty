import io
import json
import wave
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from worldd.gateway import Gateway
from worldd.perception import PerceptionService, default_pricing_identity
from worldd.provider_boundary import submit_payment, submit_provider, submit_search, submit_venue
from worldd.storage import Store


async def public_dns(host, port):
    return ["93.184.216.34"]


async def test_paid_vendor_uses_gateway_and_private_child_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_TRANSCRIBE_OPENAI_KEY", "provider-fixture-secret")
    requests = []

    def vendor(request):
        requests.append(request)
        assert request.headers["host"] == "api.openai.com"
        assert request.headers["authorization"] == "Bearer provider-fixture-secret"
        assert request.url.path == "/v1/audio/transcriptions"
        return httpx.Response(
            200,
            json={
                "text": "A genuine transcript.",
                "segments": [],
                "internal_token": "provider-internal-token",
            },
        )

    store = Store(tmp_path)
    run_id = store.create_run(
        {
            "stage": 2,
            "perception_pricing": default_pricing_identity(),
            "research_mode": "research",
            "budget": {"usd": 1, "wall_s": 300},
            "mode": "live",
        }
    )["run_id"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(vendor)) as client:
        gateway = Gateway(tmp_path, {"domain_interval_s": 0}, client, resolver=public_dns)
        world = SimpleNamespace(store=store, gateway=gateway, services={}, semaphores={})

        async def send(*args, **kwargs):
            return await submit_provider(world, *args, **kwargs)

        world.submit_provider = send
        service = PerceptionService(world)
        world.services["perception"] = service
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * 8000)
        fetched = store.run_dir(run_id) / "world" / "fetched"
        fetched.mkdir(parents=True)
        (fetched / "audio.wav").write_bytes(output.getvalue())
        params = {"handle": "/world/fetched/audio.wav", "provider": "openai"}
        quoted = await service.handle(run_id, "a_quote", "transcribe", params)
        result = await service.handle(
            run_id,
            "a_transcribe",
            "transcribe",
            {**params, "quote_id": quoted["quotes"][0]["quote_id"]},
        )
        assert result["text"] == "A genuine transcript."
        assert "internal_token" not in result
        assert len(requests) == 1
        assert store.spend(run_id)["perception"] == float(result["usd"])
        children = [
            run for run in store.list_runs() if run["config"].get("parent_run_id") == run_id
        ]
        assert len(children) == 1 and children[0]["state"] == "sealed"
        assert children[0]["config"]["fixture_transport"] is True
        child_path = store.run_dir(children[0]["run_id"])
        assert (child_path / "archive.warc.gz").is_file()
        assert any(path.is_file() for path in (child_path / "private-responses").rglob("*"))
        for path in (store.run_dir(run_id) / "world").rglob("*.json"):
            assert "provider-internal-token" not in path.read_text()
        assert "provider-fixture-secret" not in (store.run_dir(run_id) / "record.jsonl").read_text()
        assert store.verify_record(run_id)["valid"]
        await service.aclose()
        await gateway.aclose()
    store.close()


async def test_provider_cannot_use_unreserved_or_wrong_vendor_destination(tmp_path):
    store = Store(tmp_path)
    run_id = store.create_run(
        {"research_mode": "research", "budget": {"usd": 1, "wall_s": 300}, "mode": "live"}
    )["run_id"]
    world = SimpleNamespace(store=store, services={})
    response = await submit_provider(
        world,
        run_id,
        "a_1",
        "openai",
        httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions"),
        reservation_id="nonexistent",
    )
    assert response["rule_id"] == "provider_reservation"
    venue = await submit_venue(
        world,
        run_id,
        "a_2",
        httpx.Request("POST", "https://native.example/post", json={}),
        lease={},
    )
    assert venue["rule_id"] == "venue_lease"
    assert len(store.list_runs()) == 1
    store.close()


async def test_development_cannot_enable_provider_egress_with_forged_receipt(tmp_path):
    store = Store(tmp_path)
    run_id = store.create_run(
        {"research_mode": "development", "budget": {"usd": 1, "wall_s": 300}}
    )["run_id"]
    world = SimpleNamespace(store=store, services={})
    response = await submit_provider(
        world,
        run_id,
        "a_1",
        "openai",
        httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions"),
        reservation_id="forged",
    )
    assert response["rule_id"] == "development_write"
    assert len(store.list_runs()) == 1
    store.close()


@pytest.mark.parametrize("status", [200, 429])
async def test_search_read_boundary_works_in_development_and_preserves_status(tmp_path, status):
    requests = []

    def vendor(request):
        requests.append(request)
        assert request.headers["host"] == "api.search.brave.com"
        assert request.headers["x-subscription-token"] == "fixture-search-key"
        return httpx.Response(status, json={"web": {"results": []}})

    store = Store(tmp_path)
    run_id = store.create_run(
        {"research_mode": "development", "mode": "live", "budget": {"usd": 1, "wall_s": 300}}
    )["run_id"]
    store.append_record(
        run_id, "action", tool="search", action_id="a_search", params={"query": "source", "k": 3}
    )
    reservation = store.reserve(run_id, "0.005")
    async with httpx.AsyncClient(transport=httpx.MockTransport(vendor)) as client:
        gateway = Gateway(tmp_path, {"domain_interval_s": 0}, client, resolver=public_dns)
        world = SimpleNamespace(store=store, services={}, gateway=gateway, search_client=client)
        request = httpx.Request(
            "GET",
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": "source", "count": 3},
            headers={"X-Subscription-Token": "fixture-search-key"},
        )
        result = await submit_search(world, run_id, "a_search", request, reservation_id=reservation)
        assert isinstance(result, httpx.Response) and result.status_code == status
        again = await submit_search(world, run_id, "a_search", request, reservation_id=reservation)
        assert again["rule_id"] == "search_reservation_used"
        assert len(requests) == 1
        assert store.total_spent(run_id) == 0  # Caller settles the actual submitted receipt.
        assert store.reserved(run_id) == 0.005
        await gateway.aclose()
    store.close()


async def test_payment_uses_actual_approval_reservation_and_private_broker_boundary(
    tmp_path, monkeypatch
):
    from worldd.approvals import HTTPPaymentAdapter, PaymentMandateLedger
    from worldd.asks import AskService

    monkeypatch.setenv("WORLD_PAYMENT_API_KEY", "private-broker-fixture-key")
    calls = []

    def broker(request):
        calls.append(request)
        assert request.headers["host"] == "broker.example"
        assert request.headers["authorization"] == "Bearer private-broker-fixture-key"
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "paid": True,
                "receipt_ref": "fixture:paid",
                "amount": payload["amount"],
                "merchant": payload["merchant"],
                "currency": "USD",
            },
        )

    store = Store(tmp_path)
    run_id = store.create_run(
        {
            "stage": 3,
            "research_mode": "research",
            "mode": "live",
            "reply_policy": "blockers_only",
            "budget": {"usd": 1, "wall_s": 300},
        }
    )["run_id"]
    world = SimpleNamespace(store=store, services={})
    world.push = lambda run, event: store.push_event(run, event)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(broker)) as client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: pytest.fail("payment direct HTTP bypass"))
        ) as blocked_client,
    ):
        gateway = Gateway(tmp_path, {"domain_interval_s": 0}, client, resolver=public_dns)
        world.gateway = gateway

        async def submit(*args, **kwargs):
            return await submit_payment(world, *args, **kwargs)

        world.submit_payment = submit
        asks = AskService(world)
        adapter = HTTPPaymentAdapter("https://broker.example/pay", client=blocked_client)
        ledger = PaymentMandateLedger(world, adapter=adapter)
        world.services.update(asks=asks, payments=ledger)
        action = {"url": "https://merchant.example/article", "quote_hash": "sha256:fixture"}
        request = {
            "merchant": "https://merchant.example",
            "ceiling": "0.25",
            "purpose": "Read one article",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            "action": action,
        }
        ticket = await ledger.request(run_id, "a_request", **request)
        asks.answer(ticket["ticket_id"], "approve", ask_class="approval")
        granted = await ledger.request(run_id, "a_grant", **request)
        result = await ledger.pay(
            run_id, "a_payment", granted["mandate"]["mandate_id"], action, "0.25"
        )
        assert result["paid"] is True
        assert len(calls) == 1 and store.spend(run_id)["paid_content"] == 0.25
        assert store.reserved(run_id) == 0 and store.reconcile(run_id)["valid"]
        assert any(run["config"].get("realm") == "payment" for run in store.list_runs())
        assert (
            "private-broker-fixture-key" not in (store.run_dir(run_id) / "record.jsonl").read_text()
        )
        await adapter.aclose()
        await gateway.aclose()
    store.close()


async def test_real_world_single_slot_402_payment_is_reentrant_without_broker_bypass(
    tmp_path, monkeypatch
):
    import asyncio

    from worldd.approvals import HTTPPaymentAdapter, PaymentMandateLedger
    from worldd.asks import AskService
    from worldd.models import RunConfig
    from worldd.runtime import World

    monkeypatch.setenv("WORLD_PAYMENT_API_KEY", "private-single-slot-fixture-key")
    proof, broker_calls = "private-single-slot-receipt-proof", []

    def upstream(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.headers["host"] == "broker.example":
            broker_calls.append(request)
            payment = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "paid": True,
                    "receipt_ref": "broker:receipt",
                    "merchant": payment["merchant"],
                    "amount": payment["amount"],
                    "currency": "USD",
                    "access_headers": {"X-Payment-Proof": proof},
                },
            )
        if request.headers.get("x-payment-proof") == proof:
            return httpx.Response(
                200, text="Purchased article.", headers={"content-type": "text/plain"}
            )
        return httpx.Response(
            402,
            json={
                "amount": "0.25",
                "currency": "USD",
                "merchant": "https://merchant.example",
                "quote_id": "article1",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        store = Store(tmp_path)
        gateway = Gateway(tmp_path, {"domain_interval_s": 0}, client, resolver=public_dns)
        world = World(store, gateway)
        # Direct store fixture: exercises engineering internals without registering research gates.
        run_id = store.create_run(
            RunConfig(
                stage=3,
                research_mode="research",
                max_in_flight=1,
                reply_policy="blockers_only",
                budget={"usd": 1, "wall_s": 300},
            ).model_dump()
        )["run_id"]
        world.semaphores[run_id] = asyncio.Semaphore(1)
        asks = AskService(world)
        adapter = HTTPPaymentAdapter("https://broker.example/pay")
        payments = PaymentMandateLedger(world, adapter=adapter)
        world.services.update(
            asks=asks,
            payments=payments,
            registry=SimpleNamespace(require_stage=lambda *args, **kwargs: None),
        )
        gateway.payment_handler = payments.handle_402
        try:
            initial = await asyncio.wait_for(
                world._fetch(run_id, "a_first", url="https://merchant.example/article"), timeout=2
            )
            assert initial["type"] == "paywall_402" and not broker_calls
            action = {
                "url": "https://merchant.example/article",
                "quote_hash": initial["quote_hash"],
            }
            fields = {
                "merchant": "https://merchant.example",
                "ceiling": "0.25",
                "purpose": "Read article",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                "action": action,
            }
            ticket = await payments.request(run_id, "a_ask", **fields)
            asks.answer(ticket["ticket_id"], "approve", ask_class="approval")
            await payments.request(run_id, "a_mandate", **fields)
            result = await asyncio.wait_for(
                world._fetch(run_id, "a_paid", url="https://merchant.example/article"), timeout=2
            )
            assert result["kind"] == "result" and result["preview"] == "Purchased article."
            assert len(broker_calls) == 1 and store.spend(run_id)["paid_content"] == 0.25
            assert world.semaphores[run_id]._value == 1
            assert any(run["config"].get("realm") == "payment" for run in store.list_runs())
        finally:
            await adapter.aclose()
            store.update_run(run_id, state="sealed")
            await world.aclose()


async def test_private_services_refuse_admission_hold_before_any_egress(tmp_path):
    from worldd.util import blocked

    store = Store(tmp_path)
    run_id = store.create_run(
        {"research_mode": "research", "mode": "live", "budget": {"usd": 1, "wall_s": 300}}
    )["run_id"]
    world = SimpleNamespace(
        store=store,
        admission_guard=lambda run: blocked(
            "budget_exhausted", "Human billing unresolved", "life_billing_hold"
        ),
    )
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    results = [
        await submit_provider(
            world, run_id, "a_provider", "openai", request, reservation_id="missing"
        ),
        await submit_venue(world, run_id, "a_venue", request, lease={}),
        await submit_payment(
            world, run_id, "a_payment", request, mandate_id="missing", reservation_id="missing"
        ),
        await submit_search(
            world,
            run_id,
            "a_search",
            httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search"),
            reservation_id="missing",
        ),
    ]
    assert all(
        result["rule_id"] == "life_billing_hold" and result["egress_submitted"] is False
        for result in results
    )
    assert len(store.list_runs()) == 1
    store.close()


async def test_private_boundary_rechecks_admission_after_waiting_for_network_slot(tmp_path):
    import asyncio

    from worldd.provider_boundary import _private_exchange
    from worldd.util import blocked

    store = Store(tmp_path)
    run_id = store.create_run({"research_mode": "research", "budget": {"usd": 1, "wall_s": 300}})[
        "run_id"
    ]
    semaphore = asyncio.Semaphore(0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: pytest.fail("revoked request reached network")
        )
    ) as client:
        gateway = Gateway(tmp_path, {}, client, resolver=public_dns)
        world = SimpleNamespace(
            store=store,
            gateway=gateway,
            semaphores={run_id: semaphore},
            admission_guard=lambda run: None,
        )
        operation = asyncio.create_task(
            _private_exchange(
                world,
                run_id,
                "a_pending",
                httpx.Request("GET", "https://fixture.example/private"),
                {},
                realm="test",
            )
        )
        await asyncio.sleep(0)
        world.admission_guard = lambda run: blocked(
            "policy_deny", "Research authorization revoked", "stage_gate"
        )
        semaphore.release()
        result = await operation
        assert result["rule_id"] == "stage_gate" and result["egress_submitted"] is False
        child = next(run for run in store.list_runs() if run["run_id"] != run_id)
        assert child["state"] == "sealed" and store.verify_record(child["run_id"])["valid"]
        await gateway.aclose()
    store.close()
