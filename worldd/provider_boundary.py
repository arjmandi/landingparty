"""Trusted vendor/venue egress with private child archives and verified receipts.

These functions are server internals, never agent-callable raw HTTP tools.
They run the same gateway policy as ordinary fetches, while keeping raw API
responses out of the agent's mount and its episode export.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from decimal import Decimal, InvalidOperation
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from urllib.parse import urlsplit

import httpx

from .approvals import action_hash
from .search import PROVIDERS, provider_request, search_price
from .util import blocked, digest, identifier


def _mail_body_authorized(world, run_id, request, approved):
    """Mask only exact approved envelope addresses; scan every content field."""
    from .scanning import contains_sensitive_data

    recipient, sender = approved.get("recipient"), approved.get("sender")
    if not recipient or not sender:
        return False
    payload = json.loads(request.content)
    sanitized = json.loads(request.content)
    scan_text = request.content.decode("utf-8", errors="replace")
    if "raw" in payload:
        message = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(payload["raw"])
        )
        if {address for _, address in getaddresses(message.get_all("to", []))} != {recipient} or {
            address for _, address in getaddresses(message.get_all("from", []))
        } != {sender}:
            return False
        message.replace_header("To", "[approved recipient]")
        message.replace_header("From", "[approved sender]")
        text_parts = [
            part.get_content() for part in message.walk() if part.get_content_maintype() == "text"
        ]
        scan_text += "\n" + "\n".join(str(part) for part in text_parts)
        sanitized["raw"] = {"headers": dict(message.items()), "content": text_parts}
    elif "personalizations" in payload:
        for personalization in sanitized["personalizations"]:
            for field in ("to", "cc", "bcc"):
                for address in personalization.get(field, []):
                    if address.get("email") != recipient:
                        return False
                    address["email"] = "[approved recipient]"
        if sanitized.get("from", {}).get("email") != sender:
            return False
        sanitized["from"]["email"] = "[approved sender]"
    elif sanitized.get("to") == recipient:
        sanitized["to"] = "[approved recipient]"
    else:
        return False
    for name, secret in os.environ.items():
        if (
            name.startswith("WORLD_")
            and name.endswith(("_KEY", "_TOKEN", "_SECRET"))
            and len(secret) >= 8
            and secret in scan_text
        ):
            return False
    for header in ("authorization", "x-api-key"):
        credential = request.headers.get(header, "").split(" ", 1)[-1]
        if len(credential) >= 8 and credential in scan_text:
            return False
    capabilities = getattr(world, "capabilities", None)
    if capabilities and any(
        capabilities.verify(run_id, candidate)
        for candidate in re.findall(r"[A-Za-z0-9_-]{40,}", scan_text)
    ):
        return False
    return not contains_sensitive_data(json.dumps(sanitized, ensure_ascii=False))


VENDOR_ENDPOINTS = {
    "openai": ("api.openai.com", {("POST", "/v1/audio/transcriptions")}),
    "deepgram": ("api.deepgram.com", {("POST", "/v1/listen")}),
    "assemblyai": ("api.assemblyai.com", {("POST", "/v2/upload"), ("POST", "/v2/transcript")}),
    "mistral-ocr": ("api.mistral.ai", {("POST", "/v1/ocr")}),
}


def _admission(world, run_id):
    if world.store.get_run(run_id)["state"] != "running":
        return blocked(
            "policy_deny",
            "Run cannot submit external network work",
            "run_state",
            egress_submitted=False,
        )
    guard = getattr(world, "admission_guard", None)
    denial = guard(run_id) if guard else None
    return {**denial, "egress_submitted": False} if denial else None


def _running(world, run_id):
    run = world.store.get_run(run_id)
    if run["state"] != "running":
        return blocked("policy_deny", "Run cannot submit external network work", "run_state")
    if run["config"].get("research_mode", "development") != "research":
        return blocked(
            "policy_deny", "Development mode cannot submit production writes", "development_write"
        )
    if run["config"].get("mode", "live") != "live":
        return blocked(
            "policy_deny", "A replay cannot initiate venue/vendor traffic", "replay_write"
        )
    return _admission(world, run_id)


async def _private_exchange(
    world, run_id, action_id, request, authorization, *, realm, gateway=None
):
    gateway = gateway or world.gateway
    await request.aread()
    child = world.store.create_run(
        {
            "budget": {"usd": 0, "wall_s": 120},
            "parent_run_id": run_id,
            "realm": realm,
            "mode": "live",
            "capture_model_io": False,
            "private_archive": True,
            "harness_id": "world-internal@0.1",
            "fixture_transport": isinstance(
                getattr(getattr(gateway, "_client", None), "_transport", None), httpx.MockTransport
            ),
        }
    )
    child_id, network_id = child["run_id"], identifier("a")
    request_hash = digest(
        {"method": request.method, "url": str(request.url), "body_hash": digest(request.content)}
    )
    world.store.append_record(
        run_id,
        "network_boundary",
        action_id=action_id,
        observation_run_id=child_id,
        realm=realm,
        request_hash=request_hash,
    )
    world.store.append_record(
        child_id,
        "action",
        action_id=network_id,
        tool="private_api",
        params_hash=request_hash,
        world_action=False,
        parent_action_id=action_id,
    )
    semaphore = getattr(world, "semaphores", {}).get(run_id) or asyncio.Semaphore(32)
    slot = world.network_slot(run_id) if hasattr(world, "network_slot") else semaphore
    try:
        async with slot:
            denial = _admission(world, run_id)
            if denial:
                return denial
            reply = await gateway.request(
                child_id,
                network_id,
                request.method,
                str(request.url),
                headers={
                    key: value
                    for key, value in request.headers.items()
                    if key.lower()
                    not in {
                        "host",
                        "content-length",
                        "connection",
                        "transfer-encoding",
                        "accept-encoding",
                        "user-agent",
                        "proxy-authorization",
                    }
                },
                body=request.content,
                trusted={**authorization, "response_private": True, "user_agent_run_id": run_id},
                signed=world.store.get_run(run_id)["config"].get("browser_identity") == "signed",
            )
        world.store.append_record(
            child_id,
            "outcome",
            action_id=network_id,
            reply_kind=reply.get("kind"),
            status=reply.get("status"),
            resource_hash=reply.get("hash"),
            rule_id=reply.get("rule_id"),
            response_headers_subset=reply.get("headers_subset", {}),
            response_headers_hash=digest(reply.get("headers_subset", {})),
        )
        if reply.get("kind") != "result":
            return {**reply, "egress_submitted": reply.get("type") in {"timeout", "platform_block"}}
        reference = reply.get("private_response_ref")
        if not reference or not hasattr(gateway, "read_private_response"):
            return blocked(
                "policy_deny",
                "Gateway did not return a private API receipt",
                "private_response",
                egress_submitted=True,
            )
        raw = gateway.read_private_response(child_id, reference)
        headers = gateway.consume_response_headers(child_id, reference)
        if reply.get("content_type") and "content-type" not in headers:
            headers["content-type"] = reply["content_type"]
        return httpx.Response(reply["status"], content=raw, headers=headers, request=request)
    finally:
        world.store.append_record(child_id, "beam_up", terminated="internal_complete")
        world.store.append_record(child_id, "seal", parent_run_id=run_id, private_archive=True)
        world.store.update_run(child_id, state="sealed")


async def submit_venue(world, run_id, action_id, request: httpx.Request, *, lease: dict):
    denial = _running(world, run_id)
    if denial:
        return denial
    vault = getattr(world, "services", {}).get("vault")
    if vault is None:
        return blocked("policy_deny", "A consumed vault lease is required", "venue_lease")
    approved = request.extensions.get("landing_party_approved_request")
    if not isinstance(approved, dict) or action_hash(approved) != lease.get("request_hash"):
        return blocked(
            "policy_deny",
            "The network request lacks its exact approved action",
            "venue_action_hash",
        )
    await request.aread()
    if request.method != approved.get("method") or str(request.url) != approved.get("url"):
        return blocked("policy_deny", "Approved method or destination changed", "venue_action_hash")
    try:
        body = json.loads(request.content) if request.content else {}
    except ValueError:
        return blocked(
            "policy_deny", "Approved venue body is not the expected JSON", "venue_action_hash"
        )
    if body != approved.get("json"):
        return blocked("policy_deny", "Approved request body changed", "venue_action_hash")
    with vault.database.lock:
        row = vault.database.db.execute(
            "SELECT * FROM leases WHERE lease_id=?", (lease.get("lease_id"),)
        ).fetchone()
    if (
        not row
        or row["state"] != "used"
        or row["run_id"] != run_id
        or row["action_id"] != action_id
        or row["request_hash"] != lease.get("request_hash")
    ):
        return blocked("policy_deny", "Lease was not consumed for this exact action", "venue_lease")
    with world.store._transaction():
        world.store.db.execute(
            "CREATE TABLE IF NOT EXISTS venue_submissions (lease_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL)"
        )
        submitted = world.store.db.execute(
            "INSERT OR IGNORE INTO venue_submissions VALUES (?,?)",
            (lease["lease_id"], lease["request_hash"]),
        ).rowcount
    if not submitted:
        return blocked(
            "policy_deny",
            "This one-action lease already submitted its network request",
            "venue_lease_used",
        )
    authorized_mail = approved.get("operation") == "send" and approved.get("scope") == "message"
    if authorized_mail and not _mail_body_authorized(world, run_id, request, approved):
        return blocked(
            "policy_deny", "Mail content contains unapproved private data", "exfiltration"
        )
    return await _private_exchange(
        world,
        run_id,
        action_id,
        request,
        {
            "allow": True,
            "allow_write": True,
            "allow_credentials": True,
            "credential_body": authorized_mail,
            "credential_header_names": ["authorization", "x-api-key"],
            "lease_id": lease["lease_id"],
        },
        realm="venue",
    )


async def submit_provider(
    world, run_id, action_id, provider, request: httpx.Request, *, reservation_id
):
    denial = _running(world, run_id)
    if denial:
        return denial
    perception = getattr(world, "services", {}).get("perception")
    row = world.store.db.execute(
        "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
    ).fetchone()
    quote = (
        perception.db.execute(
            "SELECT * FROM perception_quotes WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if perception
        else None
    )
    if (
        not row
        or row["run_id"] != run_id
        or row["state"] != "held"
        or not quote
        or quote["run_id"] != run_id
        or quote["state"] != "processing"
    ):
        return blocked(
            "policy_deny",
            "A held reservation bound to this perception quote is required",
            "provider_reservation",
        )
    payload = json.loads(quote["payload"])
    if row["amount_nano"] != Decimal(payload["usd"]) * 1_000_000_000:
        return blocked(
            "policy_deny",
            "Reservation differs from the exact accepted quote",
            "provider_reservation_amount",
        )
    if payload.get("provider") != provider:
        return blocked("policy_deny", "Provider differs from the accepted quote", "provider_quote")
    target = VENDOR_ENDPOINTS.get(provider)
    parsed = urlsplit(str(request.url))
    path_allowed = target and (request.method, parsed.path) in target[1]
    if (
        provider == "assemblyai"
        and request.method == "GET"
        and re.fullmatch(r"/v2/transcript/[A-Za-z0-9_-]+", parsed.path)
    ):
        path_allowed = True
    if (
        not target
        or parsed.scheme != "https"
        or parsed.hostname != target[0]
        or parsed.port not in {None, 443}
        or not path_allowed
    ):
        return blocked(
            "policy_deny",
            "Destination is not a registered endpoint for this provider",
            "provider_destination",
        )
    if provider == "mistral-ocr":
        from .perception import MISTRAL_OCR_MODEL

        try:
            await request.aread()
            submitted = json.loads(request.content)
            document = submitted["document"]
            prefix = "data:application/pdf;base64,"
            encoded = document["document_url"]
            if not isinstance(encoded, str) or not encoded.startswith(prefix):
                raise ValueError("OCR input must be the quoted PDF bytes")
            source = base64.b64decode(encoded[len(prefix) :], validate=True)
            expected = {
                "model": MISTRAL_OCR_MODEL,
                "document": {"type": "document_url", "document_url": encoded},
                "include_image_base64": False,
            }
            if payload["options"].get("pages"):
                expected["pages"] = [page - 1 for page in payload["options"]["pages"]]
            if (
                payload["operation"] != "parse_document"
                or digest(source) != payload["source_hash"]
                or submitted != expected
            ):
                raise ValueError("OCR request differs from quote")
        except (KeyError, TypeError, ValueError):
            return blocked(
                "policy_deny",
                "OCR body/model/pages differ from the accepted quote",
                "provider_quote_body",
            )
    return await _private_exchange(
        world,
        run_id,
        action_id,
        request,
        {
            "allow": True,
            "allow_write": True,
            "allow_credentials": True,
            "credential_body": True,
            "credential_header_names": ["authorization", "x-api-key"],
            "provider_operation": "perception",
            "allowed_provider_host": target[0],
            "reservation_id": reservation_id,
        },
        realm="perception",
    )


async def submit_search(world, run_id, action_id, request: httpx.Request, *, reservation_id):
    """A paid read works in development, but never escapes its reserved API route."""
    run = world.store.get_run(run_id)
    if run["state"] != "running" or run["config"].get("mode", "live") != "live":
        return blocked("policy_deny", "Search API requires an active live run", "search_state")
    target = request.url
    provider = run["config"].get("search_provider", "brave")
    spec = PROVIDERS[provider]
    endpoint = httpx.URL(spec["url"])
    if (
        request.method != spec["method"]
        or target.scheme != "https"
        or target.host != endpoint.host
        or target.path != endpoint.path
        or target.port not in {None, 443}
        or target.username
        or target.password
    ):
        return blocked(
            "policy_deny",
            "Search destination is not the registered provider endpoint",
            "search_destination",
        )
    denial = _admission(world, run_id)
    if denial:
        return denial
    reservation = world.store.db.execute(
        "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
    ).fetchone()
    if (
        not reservation
        or reservation["run_id"] != run_id
        or reservation["state"] != "held"
        or reservation["amount_nano"] != Decimal(search_price(provider)) * 1_000_000_000
    ):
        return blocked(
            "policy_deny",
            "The search call lacks its exact held price reservation",
            "search_reservation",
        )
    action = next(
        (
            record
            for record in reversed(world.store.records(run_id))
            if record["kind"] == "action"
            and record.get("action_id") == action_id
            and record.get("tool") == "search"
        ),
        None,
    )
    params = action.get("params", {}) if action else {}
    expected = (
        provider_request(provider, "boundary-validation", params, submitted_at=action["ts"])
        if action
        else None
    )
    if expected is None or request.url != expected.url or request.content != expected.content:
        return blocked(
            "policy_deny",
            "Search parameters changed after recorded submission",
            "search_action_hash",
        )
    with world.store._transaction():
        world.store.db.execute(
            "CREATE TABLE IF NOT EXISTS search_submissions (reservation_id TEXT PRIMARY KEY, action_id TEXT NOT NULL)"
        )
        submitted = world.store.db.execute(
            "INSERT OR IGNORE INTO search_submissions VALUES (?,?)", (reservation_id, action_id)
        ).rowcount
    if not submitted:
        return blocked(
            "policy_deny",
            "This search reservation already submitted a provider call",
            "search_reservation_used",
        )
    gateway = world.gateway
    search_client = getattr(world, "search_client", None)
    if search_client is not None and isinstance(
        getattr(search_client, "_transport", None), httpx.MockTransport
    ):
        # Explicit injected fixture transport cannot perform real network I/O.
        # Still execute real gateway URL policy, DNS checks, WARC, and rate limits.
        cached = getattr(world, "_search_fixture_gateway", None)
        if cached is None or cached._client is not search_client:
            from .gateway import Gateway

            async def fixture_dns(host, port):
                return ["93.184.216.34"]

            cached = Gateway(
                world.store.data_dir,
                getattr(gateway, "policy", None),
                search_client,
                resolver=getattr(gateway, "_resolver", fixture_dns),
                signer=getattr(gateway, "signer", None),
            )
            for name in (
                "_rate_lock",
                "_domain_tokens",
                "_counts",
                "_global_times",
                "_cooldowns",
                "_failures",
            ):
                if hasattr(gateway, name):
                    setattr(cached, name, getattr(gateway, name))
            world._search_fixture_gateway = cached
        gateway = cached
    return await _private_exchange(
        world,
        run_id,
        action_id,
        request,
        {
            "allow": True,
            "allow_credentials": True,
            "allow_write": provider == "exa",  # POST carries a paid read, never a venue write.
            "credential_header_names": [spec["header"]],
            "provider_operation": "search",
            "allowed_provider_host": target.host,
            "reservation_id": reservation_id,
        },
        realm="search",
        gateway=gateway,
    )


async def submit_payment(
    world, run_id, action_id, request: httpx.Request, *, mandate_id, reservation_id
):
    denial = _running(world, run_id)
    if denial:
        return {**denial, "egress_submitted": False}
    payments = getattr(world, "services", {}).get("payments")
    if payments is None or payments.adapter is None:
        return blocked(
            "policy_deny",
            "A configured signed payment broker is required",
            "payment_adapter",
            egress_submitted=False,
        )
    with payments.database.lock:
        mandate = payments.database.db.execute(
            "SELECT * FROM payment_mandates WHERE mandate_id=?", (mandate_id,)
        ).fetchone()
    reservation = world.store.db.execute(
        "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
    ).fetchone()
    if (
        not mandate
        or mandate["run_id"] != run_id
        or mandate["state"] != "in_flight"
        or mandate["reservation_id"] != reservation_id
        or not reservation
        or reservation["state"] != "held"
        or reservation["run_id"] != run_id
    ):
        return blocked(
            "policy_deny",
            "The payment lacks its pending mandate and held reservation",
            "payment_reservation",
            egress_submitted=False,
        )
    import hmac

    payload = json.loads(mandate["payload"])
    if not hmac.compare_digest(mandate["signature"], payments.database.sign(payload)):
        return blocked(
            "policy_deny",
            "The payment mandate signature is invalid",
            "payment_signature",
            egress_submitted=False,
        )
    await request.aread()
    try:
        body = json.loads(request.content)
        value = Decimal(str(body["amount"]))
    except (ValueError, KeyError, TypeError, InvalidOperation):
        return blocked(
            "policy_deny",
            "Payment broker request is malformed",
            "payment_action_hash",
            egress_submitted=False,
        )
    if (
        request.method != "POST"
        or str(request.url) != payments.adapter.endpoint
        or body.get("payment_id") != mandate_id
        or body.get("merchant") != payload["merchant"]
        or body.get("purpose") != payload["purpose"]
        or body.get("currency") != "USD"
        or action_hash(body.get("action")) != payload["action_hash"]
        or not value.is_finite()
        or value <= 0
        or value > Decimal(payload["ceiling"])
        or value * 1_000_000_000 != reservation["amount_nano"]
    ):
        return blocked(
            "policy_deny",
            "Broker request differs from its exact approved mandate",
            "payment_action_hash",
            egress_submitted=False,
        )
    with world.store._transaction():
        world.store.db.execute(
            "CREATE TABLE IF NOT EXISTS payment_submissions (mandate_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL)"
        )
        submitted = world.store.db.execute(
            "INSERT OR IGNORE INTO payment_submissions VALUES (?,?)", (mandate_id, digest(body))
        ).rowcount
    if not submitted:
        return blocked(
            "policy_deny",
            "The single-use mandate already submitted a payment",
            "payment_mandate_used",
            egress_submitted=True,
        )
    return await _private_exchange(
        world,
        run_id,
        action_id,
        request,
        {
            "allow": True,
            "allow_write": True,
            "allow_credentials": True,
            "credential_header_names": ["authorization"],
            "provider_operation": "payment",
            "allowed_provider_host": request.url.host,
            "reservation_id": reservation_id,
            "mandate_id": mandate_id,
            "allowed_categories": ["payment_processors"],
        },
        realm="payment",
    )
