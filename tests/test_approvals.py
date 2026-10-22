import asyncio
from datetime import timedelta

import httpx
import pytest
from capability_helpers import CapabilityWorld

from worldd.approvals import HTTPPaymentAdapter, PaymentMandateLedger


class PaymentAdapter:
    test_only = True

    def __init__(self, *, uncertain=False, decline=False):
        self.calls, self.uncertain, self.decline = [], uncertain, decline

    async def pay(self, **request):
        self.calls.append(request)
        if self.uncertain:
            raise OSError("payment outcome unknown")
        if self.decline:
            return {"paid": False, "declined": True}
        return {
            "paid": True,
            "receipt_ref": "receipt_fixture",
            "amount": request["amount"],
            "merchant": request["merchant"],
            "currency": "USD",
            "access_headers": {"X-Receipt": "private-access-proof"},
        }


@pytest.fixture
def world(tmp_path):
    result = CapabilityWorld(tmp_path)
    yield result
    result.close()


async def mandate(world, ledger, run_id):
    args = {
        "merchant": "https://merchant.test",
        "ceiling": "2.50",
        "purpose": "Read one article",
        "expires_at": (world.clock() + timedelta(hours=1)).isoformat(),
        "action": {"url": "https://merchant.test/article", "quote_hash": "sha256:fixture"},
    }
    ticket = await ledger.request(run_id, "a_request", **args)
    assert ticket["kind"] == "ticket"
    world.services["asks"].answer(ticket["ticket_id"], "approve", ask_class="approval")
    return ledger.create(run_id, **args), args


async def test_mandate_requires_exact_signed_operator_approval(world):
    run_id = world.run(stage=3)
    ledger = PaymentMandateLedger(world, clock=world.clock)
    args = {
        "merchant": "https://merchant.test",
        "ceiling": "2.50",
        "purpose": "article",
        "expires_at": (world.clock() + timedelta(hours=1)).isoformat(),
        "action": {"url": "https://merchant.test/article"},
    }
    with pytest.raises(ValueError, match="signed approval"):
        ledger.create(run_id, **args)
    pending = await ledger.request(run_id, "a_request", **args)
    world.services["asks"].answer(pending["ticket_id"], "approve")
    with pytest.raises(ValueError, match="signed approval"):
        ledger.create(run_id, **{**args, "ceiling": "20"})


async def test_paid_receipt_is_single_use_and_debited_exactly(world):
    run_id = world.run(stage=3)
    adapter = PaymentAdapter()
    ledger = PaymentMandateLedger(world, adapter=adapter, clock=world.clock)
    approved, args = await mandate(world, ledger, run_id)
    result = await ledger.pay(run_id, "a_pay", approved["mandate_id"], args["action"], "2")
    assert result["paid"] and result["receipt_ref"] == "receipt_fixture"
    assert world.store.spend(run_id)["paid_content"] == 2
    assert world.store.reserved(run_id) == 0
    assert world.store.reconcile(run_id)["valid"]
    assert "private-access-proof" not in str(world.store.records(run_id))
    with pytest.raises(ValueError, match="used"):
        await ledger.pay(run_id, "a_reuse", approved["mandate_id"], args["action"], "2")
    assert len(adapter.calls) == 1


async def test_payment_stop_drains_before_seal_and_retains_unknown_spend_hold(world):
    class SlowAdapter(PaymentAdapter):
        async def pay(self, **request):
            self.started.set()
            await asyncio.Event().wait()

    run_id = world.run(stage=3)
    adapter = SlowAdapter()
    adapter.started = asyncio.Event()
    ledger = PaymentMandateLedger(world, adapter=adapter, clock=world.clock)
    approved, args = await mandate(world, ledger, run_id)
    task = asyncio.create_task(
        ledger.pay(run_id, "a_pay", approved["mandate_id"], args["action"], "2")
    )
    await adapter.started.wait()
    world.store.update_run(run_id, state="stopping")
    await asyncio.wait_for(ledger.stop_run(run_id), 2)
    assert task.cancelled()
    assert ledger.list_for_run(run_id)[0]["state"] == "uncertain"
    assert world.store.reserved(run_id) == 2
    assert world.store.get_run(run_id)["tainted"]
    world.store.append_record(run_id, "seal")
    world.store.update_run(run_id, state="sealed")
    assert world.store.records(run_id)[-1]["kind"] == "seal"


async def test_agent_payment_ask_binds_actual_quote_and_rejects_invented_amount(world):
    run_id = world.run(stage=3)
    payments = PaymentMandateLedger(world, clock=world.clock)
    world.services["payments"] = payments
    world.store.append_record(
        run_id,
        "action",
        action_id="a_fetch",
        tool="fetch",
        params={"url": "https://merchant.test/article"},
    )
    asks = world.services["asks"]
    params = {"kind": "grant_payment", "text": "Read the article", "action_ref": "a_fetch"}
    assert (await asks.handle(run_id, "a_ask", "ask", params))["rule_id"] == "payment_quote"
    world.store.append_record(
        run_id,
        "outcome",
        action_id="a_fetch",
        resource=None,
        reply={
            "kind": "blocked",
            "type": "paywall_402",
            "amount": "1.25",
            "merchant": "https://merchant.test",
            "quote_hash": "sha256:observed-quote",
        },
    )
    pending = await asks.handle(run_id, "a_ask", "ask", params)
    body = asks.get(pending["ticket_id"])["body"]
    assert body["ceiling"] == "1.25" and body["action"]["quote_hash"] == "sha256:observed-quote"
    asks.answer(pending["ticket_id"], "approve")
    world.clock.advance(5)
    result = await asks.handle(run_id, "a_retry", "ask", params)
    assert result["mandate"]["ceiling"] == "1.25"


async def test_402_handler_does_not_invent_or_pay_an_unapproved_quote(world):
    run_id = world.run(stage=3)
    adapter = PaymentAdapter()
    ledger = PaymentMandateLedger(world, adapter=adapter, clock=world.clock)
    quote = {
        "run_id": run_id,
        "url": "https://merchant.test/article",
        "amount": "2",
        "quote_hash": "sha256:fixture",
    }
    assert (await ledger.handle_402(quote))["type"] == "paywall_402"
    assert not adapter.calls
    await mandate(world, ledger, run_id)
    assert (await ledger.handle_402(quote))["paid"]
    assert len(adapter.calls) == 1


async def test_merchant_action_ceiling_and_expiry_cannot_be_swapped(world):
    run_id = world.run(stage=3)
    adapter = PaymentAdapter()
    ledger = PaymentMandateLedger(world, adapter=adapter, clock=world.clock)
    approved, args = await mandate(world, ledger, run_id)
    for action, amount in [
        ({"url": "https://different.test/article", "quote_hash": "sha256:fixture"}, "2"),
        ({**args["action"], "quote_hash": "changed"}, "2"),
        (args["action"], "3"),
    ]:
        with pytest.raises(ValueError):
            await ledger.pay(run_id, "a_swap", approved["mandate_id"], action, amount)
    world.clock.advance(3601)
    with pytest.raises(ValueError, match="expired"):
        await ledger.pay(run_id, "a_expired", approved["mandate_id"], args["action"], "2")
    assert not adapter.calls


async def test_uncertain_payment_keeps_hold_and_prevents_duplicate_payment(world):
    run_id = world.run(stage=3)
    adapter = PaymentAdapter(uncertain=True)
    ledger = PaymentMandateLedger(world, adapter=adapter, clock=world.clock)
    approved, args = await mandate(world, ledger, run_id)
    reply = await ledger.pay(run_id, "a_pay", approved["mandate_id"], args["action"], "2")
    assert reply["rule_id"] == "payment_uncertain"
    assert world.store.get_run(run_id)["tainted"]
    assert world.store.get_run(run_id)["state"] == "frozen"
    assert world.store.reserved(run_id) == 2 and world.store.total_spent(run_id) == 0
    assert ledger.list_for_run(run_id)[0]["state"] == "uncertain"


async def test_payment_decline_releases_hold_and_never_reports_paid(world):
    run_id = world.run(stage=3)
    ledger = PaymentMandateLedger(world, adapter=PaymentAdapter(decline=True), clock=world.clock)
    approved, args = await mandate(world, ledger, run_id)
    reply = await ledger.pay(run_id, "a_pay", approved["mandate_id"], args["action"], "2")
    assert reply["rule_id"] == "payment_declined"
    assert world.store.reserved(run_id) == 0 and world.store.total_spent(run_id) == 0


async def test_development_run_cannot_use_live_payment_adapter(world):
    run_id = world.run(stage=3, research_mode="development")
    adapter = PaymentAdapter()
    adapter.test_only = False
    ledger = PaymentMandateLedger(world, adapter=adapter, clock=world.clock)
    approved, args = await mandate(world, ledger, run_id)
    reply = await ledger.pay(run_id, "a_pay", approved["mandate_id"], args["action"], "2")
    assert reply["rule_id"] == "development_boundary" and not adapter.calls


async def test_payment_cancel_retains_unknown_spend_for_reconciliation(world):
    run_id = world.run(stage=3)
    started = asyncio.Event()

    class SlowAdapter:
        test_only = True

        async def pay(self, **request):
            started.set()
            await asyncio.Event().wait()

    ledger = PaymentMandateLedger(world, adapter=SlowAdapter(), clock=world.clock)
    approved, args = await mandate(world, ledger, run_id)
    pending = asyncio.create_task(
        ledger.pay(run_id, "a_pay", approved["mandate_id"], args["action"], "2")
    )
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert world.store.reserved(run_id) == 2
    assert ledger.list_for_run(run_id)[0]["state"] == "uncertain"


async def test_https_broker_adapter_sends_idempotent_scoped_request_without_logging_key(
    monkeypatch,
):
    monkeypatch.setenv("WORLD_PAYMENT_API_KEY", "fixture-broker-secret")

    def handler(request):
        assert str(request.url) == "https://broker.test/pay"
        assert request.headers["authorization"] == "Bearer fixture-broker-secret"
        assert request.headers["idempotency-key"] == "mandate_fixture"
        return httpx.Response(
            200,
            json={
                "paid": True,
                "receipt_ref": "actual_fixture_receipt",
                "amount": "2",
                "merchant": "https://merchant.test",
                "currency": "USD",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        class RecordedBoundary:
            async def submit_payment(self, run_id, action_id, request, **bindings):
                assert run_id == "r_fixture" and action_id == "a_payment"
                assert bindings == {
                    "mandate_id": "mandate_fixture",
                    "reservation_id": "hold_fixture",
                }
                return await client.send(request, follow_redirects=False)

        adapter = HTTPPaymentAdapter("https://broker.test/pay", client=client)
        receipt = await adapter.pay(
            merchant="https://merchant.test",
            amount="2",
            purpose="article",
            action={"url": "https://merchant.test/article"},
            payment_id="mandate_fixture",
            context={
                "world": RecordedBoundary(),
                "run_id": "r_fixture",
                "action_id": "a_payment",
                "mandate_id": "mandate_fixture",
                "reservation_id": "hold_fixture",
            },
        )
        assert receipt["receipt_ref"] == "actual_fixture_receipt"
    with pytest.raises(ValueError, match="HTTPS"):
        HTTPPaymentAdapter("http://insecure.test/pay")


def test_payment_broker_configuration_is_world_environment_only(world, monkeypatch):
    monkeypatch.setenv("WORLD_PAYMENT_API_URL", "https://broker.test/pay")
    ledger = PaymentMandateLedger(world)
    assert isinstance(ledger.adapter, HTTPPaymentAdapter)
    assert ledger.adapter.endpoint == "https://broker.test/pay"


@pytest.mark.parametrize("submitted", [False, True])
async def test_payment_boundary_denial_distinguishes_known_not_sent_from_uncertain(
    world, monkeypatch, submitted
):
    monkeypatch.setenv("WORLD_PAYMENT_API_KEY", "private-broker-key")
    adapter = HTTPPaymentAdapter("https://broker.test/pay")
    adapter.test_only = True
    ledger = PaymentMandateLedger(world, adapter=adapter, clock=world.clock)
    world.services["payments"] = ledger

    async def boundary(*args, **kwargs):
        return {
            "kind": "blocked",
            "type": "policy_deny",
            "rule_id": "fixture_denial",
            "egress_submitted": submitted,
        }

    world.submit_payment = boundary
    run_id = world.run(stage=3)
    approved, args = await mandate(world, ledger, run_id)
    try:
        result = await ledger.pay(run_id, "a_pay", approved["mandate_id"], args["action"], "2")
        assert world.store.total_spent(run_id) == 0
        if submitted:
            assert result["rule_id"] == "payment_uncertain"
            assert world.store.reserved(run_id) == 2
            assert world.store.get_run(run_id)["tainted"]
        else:
            assert result["rule_id"] == "fixture_denial"
            assert world.store.reserved(run_id) == 0
            assert not world.store.get_run(run_id).get("tainted", False)
            assert ledger.list_for_run(run_id)[0]["state"] == "not_submitted"
    finally:
        await ledger.aclose()
