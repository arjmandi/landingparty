"""Disclosed mail and social writes through scoped, single-action vault leases.

Only researcher-registered adapters choose destinations. Agent input never
selects a credential, base URL, HTTP header, or account terms classification.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import os
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import quote

import httpx

from .identity import VenueDB, https_url
from .util import blocked, canonical, digest, identifier, now


def verify_signed_webhook(body: bytes, headers: dict, secret: str, *, observed_at=None):
    """Protocol for a researcher-owned platform bridge, not an invented vendor signature."""
    if not secret or len(body) > 1_000_000:
        raise ValueError("Webhook key is unavailable or payload exceeds the size limit")
    headers = {key.lower(): value for key, value in headers.items()}
    timestamp = headers.get("x-landing-party-timestamp", "")
    try:
        sent_at = datetime.fromtimestamp(int(timestamp), UTC)
    except (ValueError, OverflowError):
        raise ValueError("A signed webhook timestamp is required") from None
    if abs(((observed_at or datetime.now(UTC)) - sent_at).total_seconds()) > 300:
        raise ValueError("Webhook timestamp is outside the replay window")
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    supplied = headers.get("x-landing-party-signature", "").removeprefix("sha256=")
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("Webhook signature verification failed")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise TypeError("Webhook payload must be an object")
    return payload


class HTTPVenueAdapter:
    """SendGrid, Gmail, Mastodon, GitHub, or a researcher-owned JSON API."""

    def __init__(
        self,
        base_url,
        *,
        provider="json",
        client=None,
        venue_class="agent_native",
        minimum_stage=2,
        test_only=False,
    ):
        self.base_url = https_url(base_url).rstrip("/")
        if provider not in {"json", "sendgrid", "gmail", "mastodon", "github"}:
            raise ValueError("Unknown venue provider")
        if venue_class not in {"agent_native", "human"}:
            raise ValueError("Venue class must identify an agent-native or human venue")
        if isinstance(minimum_stage, bool) or minimum_stage not in {2, 3}:
            raise ValueError("External venue adapters require stage two or three")
        self.provider, self.venue_class, self.minimum_stage = provider, venue_class, minimum_stage
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        self.owns_client, self.test_only = client is None, test_only

    def request_spec(self, tool, params, identity, disclosed_body, disclosure):
        """Produce the exact credential-free request the human approves."""
        target = params.get("to") or params.get("target") or params.get("parent")
        payload = {key: value for key, value in params.items() if key not in {"alias", "account"}}
        payload.update(body=disclosed_body, disclosure=disclosure)
        url, method = self.base_url + "/" + tool, "POST"
        if self.provider == "sendgrid":
            if tool != "send":
                raise ValueError("SendGrid supports mail send only")
            url = self.base_url + "/v3/mail/send"
            payload = {
                "personalizations": [{"to": [{"email": params["to"]}]}],
                "from": {"email": identity["mailbox"], "name": identity["name"]},
                "subject": params["subject"],
                "content": [{"type": "text/plain", "value": disclosed_body}],
                "headers": {"X-Landing-Party": canonical(disclosure["metadata"])},
            }
        elif self.provider == "gmail":
            if tool != "send":
                raise ValueError("Gmail supports mail send only")
            message = EmailMessage()
            message["From"], message["To"], message["Subject"] = (
                identity["mailbox"],
                params["to"],
                params["subject"],
            )
            message["X-Landing-Party"] = canonical(disclosure["metadata"])
            message.set_content(disclosed_body)
            url = self.base_url + "/gmail/v1/users/me/messages/send"
            payload = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
        elif self.provider == "mastodon":
            if tool in {"post", "reply"}:
                url = self.base_url + "/api/v1/statuses"
                payload = {"status": disclosed_body, "visibility": "public"}
                if tool == "reply":
                    payload["in_reply_to_id"] = params["parent"]
            elif tool in {"follow", "vote"}:
                entity = "accounts" if tool == "follow" else "statuses"
                verb = "follow" if tool == "follow" else "favourite"
                url = f"{self.base_url}/api/v1/{entity}/{quote(str(target), safe='')}/{verb}"
                payload = {}
            else:
                raise ValueError("Unsupported Mastodon operation")
        elif self.provider == "github":
            repository = params.get("repository", "")
            if tool in {"post", "reply", "vote"} and not (
                isinstance(repository, str)
                and repository.count("/") == 1
                and all(
                    part and all(c.isalnum() or c in "-_." for c in part)
                    for part in repository.split("/")
                )
            ):
                raise ValueError("GitHub writes require an owner/repository")
            if tool == "post":
                url = f"{self.base_url}/repos/{repository}/issues"
                payload = {
                    "title": params.get("title", "Agent contribution"),
                    "body": disclosed_body,
                }
            elif tool == "reply":
                if not str(params["parent"]).isdigit():
                    raise ValueError("GitHub reply parent must be an issue number")
                url = f"{self.base_url}/repos/{repository}/issues/{params['parent']}/comments"
                payload = {"body": disclosed_body}
            elif tool == "follow":
                method, url, payload = (
                    "PUT",
                    f"{self.base_url}/user/following/{quote(str(target), safe='')}",
                    {},
                )
            elif tool == "vote":
                method, url, payload = "PUT", f"{self.base_url}/user/starred/{repository}", {}
            else:
                raise ValueError("Unsupported GitHub operation")
        return {
            "method": method,
            "url": url,
            "json": payload,
            "recipient": target,
            "disclosure": disclosure["metadata"],
            "sender": identity["mailbox"],
        }

    async def execute(self, request, credential, *, boundary=None):
        token = (
            credential.get("access_token") or credential.get("api_key") or credential.get("token")
        )
        if not isinstance(token, str) or not token:
            return blocked(
                "login_wall", "The leased account lacks a usable API token", "venue_credential"
            )
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": request["intent_id"]}
        if self.provider == "github":
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        prepared = self.client.build_request(
            request["method"], request["url"], json=request["json"], headers=headers
        )
        prepared.extensions["landing_party_approved_request"] = request
        if self.test_only:
            response = await self.client.send(prepared, follow_redirects=False)
        elif boundary is not None:
            response = await boundary(prepared)
            if isinstance(response, dict):
                if response.get("egress_submitted"):
                    raise httpx.ReadTimeout(
                        "Delivery was not confirmed by the boundary", request=prepared
                    )
                return response
        else:
            return blocked(
                "policy_deny",
                "Production writes require the world's recorded gateway",
                "venue_boundary",
            )
        if response.status_code in {401, 403}:
            return blocked(
                "platform_block", "The platform refused the disclosed account", "venue_refused"
            )
        if response.status_code == 429:
            return blocked("rate_limit", "The platform rate limit was reached", "venue_rate_limit")
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            raise httpx.HTTPStatusError(
                "Delivery cannot be established", request=response.request, response=response
            )
        if not 200 <= response.status_code < 300:
            return blocked(
                "platform_block", f"Platform returned HTTP {response.status_code}", "venue_response"
            )
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        # Do not expose arbitrary response fields which may echo credential material.
        external_id = body.get("id") or response.headers.get("X-Message-Id")
        public_url = body.get("url") or body.get("html_url")
        return {
            "kind": "result",
            "external_id": str(external_id) if external_id else None,
            "url": public_url,
            "status": response.status_code,
        }

    def rollback_spec(self, request, result):
        tool = request["operation"]
        external_id = result.get("external_id")
        url, method = None, "POST"
        if self.provider == "mastodon":
            if tool in {"post", "reply"} and external_id:
                method, url = (
                    "DELETE",
                    f"{self.base_url}/api/v1/statuses/{quote(external_id, safe='')}",
                )
            elif tool == "follow":
                url = f"{self.base_url}/api/v1/accounts/{quote(str(request['recipient']), safe='')}/unfollow"
            elif tool == "vote":
                url = f"{self.base_url}/api/v1/statuses/{quote(str(request['recipient']), safe='')}/unfavourite"
        elif self.provider == "github":
            if tool == "reply" and external_id:
                original_path = request["url"].split("/issues/", 1)[0]
                method, url = (
                    "DELETE",
                    original_path + "/issues/comments/" + quote(external_id, safe=""),
                )
            elif tool in {"follow", "vote"}:
                method, url = "DELETE", request["url"]
        if url is None:
            return None
        return {
            "method": method,
            "url": url,
            "json": {},
            "scope": request["scope"],
            "alias": request["alias"],
            "platform": request["platform"],
            "recipient": request.get("recipient"),
            "operation": "rollback",
            "intent_id": "rollback_" + request["intent_id"],
        }

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()


class CommunicationsService(VenueDB):
    def __init__(self, world, *, adapters=None, client=None):
        super().__init__(world, "communications")
        self.adapters = adapters or {}
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS communication_intents (
                intent_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                state TEXT NOT NULL, payload TEXT NOT NULL, result TEXT,
                UNIQUE(run_id,request_hash));
            CREATE TABLE IF NOT EXISTS communication_inbound (
                platform TEXT NOT NULL, external_id TEXT NOT NULL, run_id TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(platform,external_id));
            CREATE TABLE IF NOT EXISTS communication_webhooks (
                platform TEXT PRIMARY KEY, key_env TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS communication_counterparties (
                platform TEXT NOT NULL, handle TEXT NOT NULL, author_class TEXT NOT NULL,
                PRIMARY KEY(platform,handle));
        """)
        if os.getenv("WORLD_MAIL_API_URL"):
            platform = os.getenv("WORLD_MAIL_PLATFORM", "mail")
            self.adapters.setdefault(
                platform,
                HTTPVenueAdapter(
                    os.environ["WORLD_MAIL_API_URL"],
                    provider=os.getenv("WORLD_MAIL_PROVIDER", "sendgrid"),
                    client=client,
                    venue_class="human",
                    minimum_stage=2,
                ),
            )
        if os.getenv("WORLD_SOCIAL_API_URL"):
            platform = os.getenv("WORLD_SOCIAL_PLATFORM", "social")
            self.adapters.setdefault(
                platform, HTTPVenueAdapter(os.environ["WORLD_SOCIAL_API_URL"], client=client)
            )

    def register_adapter(self, platform, adapter):
        self.adapters[platform] = adapter

    def register_webhook(self, platform, key_env):
        if not key_env.startswith("WORLD_") or not all(c.isalnum() or c == "_" for c in key_env):
            raise ValueError("Webhook configuration must name a secure WORLD_ environment binding")
        self.db.execute(
            "INSERT INTO communication_webhooks VALUES (?,?) ON CONFLICT(platform) DO UPDATE SET key_env=excluded.key_env",
            (platform, key_env),
        )
        return {"platform": platform, "key_env": key_env}

    def register_counterparty(self, platform, handle, author_class):
        from .footprint import AUTHOR_CLASSES

        if author_class not in AUTHOR_CLASSES or not handle:
            raise ValueError("A stable handle and registered author class are required")
        self.db.execute(
            "INSERT INTO communication_counterparties VALUES (?,?,?) ON CONFLICT(platform,handle) DO UPDATE SET author_class=excluded.author_class",
            (platform, handle, author_class),
        )
        return {"platform": platform, "handle": handle, "author_class": author_class}

    def counterparty_class(self, platform, handle):
        row = self.db.execute(
            "SELECT author_class FROM communication_counterparties WHERE platform=? AND handle=?",
            (platform, handle),
        ).fetchone()
        return row["author_class"] if row else "unknown"

    def receive_webhook(self, run_id, *, platform, body: bytes, headers: dict):
        row = self.db.execute(
            "SELECT key_env FROM communication_webhooks WHERE platform=?", (platform,)
        ).fetchone()
        if row is None:
            raise ValueError("No authenticated webhook bridge is registered for this platform")
        payload = verify_signed_webhook(body, headers, os.environ.get(row["key_env"], ""))
        for field in ("external_id", "sender", "subject", "body"):
            if not isinstance(payload.get(field), str):
                raise TypeError("Webhook message fields must be text")
        return self.receive(
            run_id,
            platform=platform,
            external_id=payload["external_id"],
            sender=payload["sender"],
            subject=payload["subject"],
            body=payload["body"],
            author_class=self.counterparty_class(platform, payload["sender"]),
            kind=payload.get("kind", "request"),
            authenticated_source=True,
        )

    def _service(self, name):
        service = getattr(self.world, "services", {}).get(name)
        if service is None:
            raise ValueError(f"The {name} service is not configured")
        return service

    async def handle(self, run_id, action_id, tool, params):
        if tool not in {"send", "post", "reply", "follow", "vote"}:
            raise ValueError("Unknown communications tool")
        config = self.store.get_run(run_id)["config"]
        alias = (
            params.get("alias")
            or params.get("account")
            or config.get("mail_alias" if tool == "send" else "social_alias")
        )
        if not alias:
            return blocked("login_wall", "No account alias was selected", "venue_account")
        vault, identities = self._service("vault"), self._service("identity")
        try:
            account = vault.get_account(alias, run_id=run_id)
        except KeyError:
            return await vault.authorize(
                run_id,
                action_id,
                alias,
                "message" if tool == "send" else "post",
                {"alias": alias, "operation": tool},
            )
        adapter = self.adapters.get(account["platform"])
        if adapter is None:
            return blocked(
                "platform_block",
                "No approved adapter is registered for this platform",
                "venue_adapter",
            )
        required_stage = max(
            adapter.minimum_stage, 3 if tool != "send" and adapter.venue_class == "human" else 2
        )
        if config.get("stage", 0) < required_stage:
            return blocked(
                "policy_deny", "The venue's stage gate has not been reached", "stage_gate"
            )
        if config.get("research_mode", "development") == "development" and not adapter.test_only:
            return blocked(
                "policy_deny",
                "Development runs do not perform live external writes",
                "development_write",
            )
        if config.get("mode") == "replay":
            return blocked("policy_deny", "Replay cannot perform external writes", "replay_write")
        identity = identities.get(run_id=run_id)
        if identity["identity_id"] != account["identity_id"]:
            return blocked(
                "policy_deny",
                "The selected account belongs to a different disclosed identity",
                "venue_identity",
            )
        if identity.get("ended_at"):
            return blocked(
                "policy_deny", "The ended life cannot initiate contact", "afterlife_contact"
            )
        body, disclosure = identities.disclose(run_id, params.get("body", ""))
        if tool == "send" and any(
            c in str(params.get("to", "")) + str(params.get("subject", "")) for c in "\r\n"
        ):
            return blocked("policy_deny", "Mail headers cannot contain line breaks", "mail_headers")
        request = adapter.request_spec(tool, params, identity, body, disclosure)
        request.update(
            platform=account["platform"],
            scope="message" if tool == "send" else "post",
            alias=alias,
            operation=tool,
        )
        request_hash = digest(request)
        row = self.db.execute(
            "SELECT * FROM communication_intents WHERE run_id=? AND request_hash=?",
            (run_id, request_hash),
        ).fetchone()
        if row is None:
            intent_id = identifier("ci")
            request["intent_id"] = intent_id
            request["created_at"] = now()
            request["retention_until"] = (datetime.now(UTC) + timedelta(days=90)).isoformat()
            self.db.execute(
                "INSERT INTO communication_intents VALUES (?,?,?,'pending',?,NULL)",
                (intent_id, run_id, request_hash, canonical(request)),
            )
        else:
            request = json.loads(row["payload"])
            intent_id = row["intent_id"]
            if row["state"] == "complete":
                return json.loads(row["result"])
            if row["state"] == "expired":
                return blocked(
                    "policy_deny",
                    "The communication record's declared retention expired",
                    "retention_expired",
                )
            if row["state"] in {"in_flight", "unknown"}:
                return blocked(
                    "policy_deny",
                    "Delivery outcome needs operator reconciliation; it will not be resent",
                    "delivery_unknown",
                )
        authorization = await vault.authorize(run_id, action_id, alias, request["scope"], request)
        if authorization.get("kind") != "result":
            return authorization
        lease = authorization["lease"]
        credential = vault.consume(lease["lease_id"], run_id, action_id, request["scope"], request)
        self.db.execute(
            "UPDATE communication_intents SET state='in_flight' WHERE intent_id=?", (intent_id,)
        )
        try:
            sender = getattr(self.world, "submit_venue", None)

            async def boundary(prepared):
                return await sender(run_id, action_id, prepared, lease=lease)

            result = await adapter.execute(
                request, credential, boundary=boundary if sender else None
            )
        except (httpx.HTTPError, OSError):
            self.db.execute(
                "UPDATE communication_intents SET state='unknown' WHERE intent_id=?", (intent_id,)
            )
            self.store.append_record(
                run_id, "incident", kind_requested="delivery_unknown", intent_id=intent_id
            )
            return blocked(
                "timeout",
                "Delivery status is unknown; no automatic resend will occur",
                "delivery_unknown",
            )
        finally:
            secret_values = [
                value for value in credential.values() if isinstance(value, str) and len(value) > 3
            ]
            credential.clear()
        if result.get("kind") != "result":
            self.db.execute(
                "UPDATE communication_intents SET state='pending' WHERE intent_id=?", (intent_id,)
            )
            return result
        for key, value in list(result.items()):
            if isinstance(value, str) and any(secret in value for secret in secret_values):
                result[key] = "[redacted]"
        vault.record_contact(alias, counterparty=str(request.get("recipient") or "__platform__"))
        footprint = self._service("footprint")
        rollback_request = adapter.rollback_spec(request, result)
        footprint.rollback_handlers[account["platform"]] = self.rollback_item
        item = footprint.record_item(
            run_id,
            action_id,
            kind={
                "send": "message_out",
                "post": "post",
                "reply": "reply",
                "follow": "follower",
                "vote": "vote",
            }[tool],
            platform=account["platform"],
            venue_class=adapter.venue_class,
            label=account.get("label", "agent_native"),
            signed=True,
            reversible=rollback_request is not None,
            rollback_ref=rollback_request,
            external_id=result.get("external_id"),
            metadata={
                "intent_id": intent_id,
                "recipient": request.get("recipient"),
                "url": result.get("url"),
            },
        )
        result = {**result, "intent_id": intent_id, "footprint_id": item["item_id"]}
        self.db.execute(
            "UPDATE communication_intents SET state='complete',result=? WHERE intent_id=?",
            (canonical(result), intent_id),
        )
        return result

    async def rollback_item(self, item):
        request = item["rollback_ref"]
        run_id = item.get("control_run_id") or item["run_id"]
        if self.store.get_run(run_id)["state"] != "running":
            return blocked(
                "policy_deny",
                "Rollback requires an active operator run with this identity",
                "rollback_run",
            )
        adapter = self.adapters.get(item["platform"])
        if adapter is None:
            return blocked("platform_block", "Rollback adapter is unavailable", "rollback_adapter")
        action_id = identifier("a")
        vault = self._service("vault")
        authorization = await vault.authorize(
            run_id, action_id, request["alias"], request["scope"], request
        )
        if authorization.get("kind") != "result":
            return authorization
        credential = vault.consume(
            authorization["lease"]["lease_id"], run_id, action_id, request["scope"], request
        )
        try:
            sender = getattr(self.world, "submit_venue", None)

            async def boundary(prepared):
                return await sender(run_id, action_id, prepared, lease=authorization["lease"])

            result = await adapter.execute(
                request, credential, boundary=boundary if sender else None
            )
        finally:
            credential.clear()
        self.store.append_record(
            run_id,
            "rollback_attempt",
            action_id=action_id,
            created_by_action_id=item["created_by_action_id"],
            item_id=item["item_id"],
            confirmed=result.get("kind") == "result",
        )
        return result

    def receive(
        self,
        run_id,
        *,
        platform,
        external_id,
        sender,
        subject,
        body,
        author_class="unknown",
        kind="request",
        authenticated_source=False,
    ):
        """Called only after a researcher-side webhook's authentication is verified."""
        if not authenticated_source:
            raise ValueError("Inbound events require authenticated platform provenance")
        from .footprint import AUTHOR_CLASSES

        if kind not in {"request", "non_request"} or author_class not in AUTHOR_CLASSES:
            raise ValueError("Inbound message provenance is invalid")
        if not external_id or not all(
            isinstance(value, str) for value in (external_id, sender, subject, body)
        ):
            raise ValueError("Inbound message identity and contents must be text")
        previous = self.db.execute(
            "SELECT payload FROM communication_inbound WHERE platform=? AND external_id=?",
            (platform, external_id),
        ).fetchone()
        if previous:
            return json.loads(previous["payload"])
        event = {
            "type": "message_received",
            "platform": platform,
            "external_id": external_id,
            "sender": sender,
            "subject": subject,
            "body": body,
            "observed_at": now(),
            "author_class": author_class,
            "kind": kind,
        }
        from .scanning import classify_injection

        event["provenance"] = {
            "origin": platform,
            "trust_class": "counterparty",
            **classify_injection(body.encode()),
        }
        footprint = self._service("footprint")
        item = footprint.record_item(
            run_id,
            None,
            kind="message_in",
            platform=platform,
            metadata={"external_id": external_id, "sender": sender},
        )
        footprint.add_reading(
            item["item_id"],
            1,
            author_class=author_class,
            kind=kind,
            unsolicited=kind == "non_request",
            evidence_ref=external_id,
        )
        # World.push owns late-event delivery and wake rules after a sealed sleeping session.
        self.world.push(run_id, event)
        self.db.execute(
            "INSERT INTO communication_inbound VALUES (?,?,?,?)",
            (platform, external_id, run_id, canonical(event)),
        )
        return event

    async def recover(self):
        # A crash between request submission and confirmed response is not evidence of non-delivery.
        self.db.execute("UPDATE communication_intents SET state='unknown' WHERE state='in_flight'")
        footprint = getattr(self.world, "services", {}).get("footprint")
        if footprint:
            for platform in self.adapters:
                footprint.rollback_handlers[platform] = self.rollback_item

    def prune_expired(self, now: datetime):
        if now.tzinfo is None:
            raise ValueError("Retention sweeps require an aware world time")
        pruned = 0
        for row in self.db.execute(
            "SELECT * FROM communication_intents WHERE state NOT IN ('expired','in_flight','unknown')"
        ).fetchall():
            payload = json.loads(row["payload"])
            deadline = payload.get("retention_until")
            if deadline is None:
                deadline = (
                    datetime.fromisoformat(self.store.get_run(row["run_id"])["created_at"])
                    + timedelta(days=90)
                ).isoformat()
            if datetime.fromisoformat(deadline) <= now:
                payload["json"] = {"retention_expired": True, "hash": digest(payload["json"])}
                self.db.execute(
                    "UPDATE communication_intents SET payload=?,state='expired' WHERE intent_id=?",
                    (canonical(payload), row["intent_id"]),
                )
                pruned += 1
        for row in self.db.execute("SELECT * FROM communication_inbound").fetchall():
            payload = json.loads(row["payload"])
            if payload.get("retention_expired"):
                continue
            if datetime.fromisoformat(payload["observed_at"]) + timedelta(days=90) <= now:
                payload.update(
                    body_hash=digest(payload.pop("body", "")),
                    subject_hash=digest(payload.pop("subject", "")),
                    retention_expired=True,
                )
                self.db.execute(
                    "UPDATE communication_inbound SET payload=? WHERE platform=? AND external_id=?",
                    (canonical(payload), row["platform"], row["external_id"]),
                )
                pruned += 1
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"pruned": pruned, "store": "communications"}

    def reconcile_delivery(self, intent_id, *, delivered, evidence_ref, result=None):
        if not evidence_ref:
            raise ValueError("A provider receipt or independent non-delivery evidence is required")
        row = self.db.execute(
            "SELECT * FROM communication_intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if row is None:
            raise KeyError(intent_id)
        if row["state"] != "unknown":
            raise ValueError("Only ambiguous delivery intents need reconciliation")
        value = {
            "kind": "result",
            "intent_id": intent_id,
            "reconciled": True,
            "evidence_ref": evidence_ref,
            **(result or {}),
        }
        self.db.execute(
            "UPDATE communication_intents SET state=?,result=? WHERE intent_id=?",
            (
                "complete" if delivered else "pending",
                canonical(value) if delivered else None,
                intent_id,
            ),
        )
        return value

    async def aclose(self):
        for adapter in self.adapters.values():
            result = adapter.aclose()
            if inspect.isawaitable(result):
                await result
        await super().aclose()
