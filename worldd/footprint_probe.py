"""Registered read-only footprint probes on their own identity and transport."""

from __future__ import annotations

import hmac
import json
import math
import os
import re
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .gateway import Gateway
from .identity import https_url
from .transport import PinnedSNITransport
from .util import blocked, digest, identifier, now
from .vault import normalized_path, origin


class RegisteredHTTPProbe:
    """Public page or read-only native API; never consumes an agent lease.

    Production requires an explicit probe proxy, avoiding ambient/default
    transports. Its configured route is recorded; actual distinct public egress
    still requires operator measurement and is not inferred from its label.
    """

    def __init__(
        self,
        world,
        *,
        source_url,
        egress_id,
        proxy_url,
        identity_id,
        format="json",
        selector="",
        key_env=None,
        key_header="Authorization",
        key_prefix="Bearer ",
        probe_views=0,
        client=None,
        resolver=None,
    ):
        self.world, self.source_url = world, https_url(source_url)
        if format not in {"json", "html"} or not selector and format == "html":
            raise ValueError("A JSON pointer or HTML selector extraction is required")
        if format == "json" and (
            selector and not selector.startswith("/") or re.search(r"~(?![01])", selector)
        ):
            raise ValueError("A valid RFC6901 JSON pointer is required")
        if format == "html":
            import soupsieve

            try:
                soupsieve.compile(selector)
            except soupsieve.SelectorSyntaxError as exc:
                raise ValueError("A valid CSS selector is required") from exc
        if not all(
            isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value)
            for value in (egress_id, identity_id)
        ):
            raise ValueError("Explicit probe egress and identity identifiers are required")
        parsed = urlsplit(proxy_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("A dedicated credential-free probe proxy URL is required")
        if key_env and (
            not re.fullmatch(r"WORLD_[A-Z0-9_]*PROBE_[A-Z0-9_]+", key_env)
            or not re.fullmatch(r"[A-Za-z0-9-]+", key_header)
        ):
            raise ValueError(
                "Probe credentials require a secure WORLD environment binding and literal header"
            )
        if key_env and format != "json":
            raise ValueError("Human public-page probes never authenticate with an API credential")
        if isinstance(probe_views, bool) or not isinstance(probe_views, int) or probe_views < 0:
            raise ValueError("Own probe views must be an explicit nonnegative integer")
        if key_prefix not in {"", "Bearer ", "Token ", "Basic "} or key_header.lower() in {
            "host",
            "user-agent",
            "proxy-authorization",
            "connection",
            "content-length",
            "transfer-encoding",
        }:
            raise ValueError(
                "A static authorization scheme and non-routing credential header are required"
            )
        if client is not None and not isinstance(
            getattr(client, "_transport", None), httpx.MockTransport
        ):
            raise ValueError(
                "Only explicit fixture MockTransports may replace dedicated probe routing"
            )
        self.identity_id, self.egress_id = identity_id, egress_id
        self.format, self.selector, self.probe_views = format, selector, probe_views
        self.key_env, self.key_header, self.key_prefix = key_env, key_header, key_prefix
        self.owns_client = client is None
        self.client = client or httpx.AsyncClient(
            transport=PinnedSNITransport(proxy=proxy_url),
            trust_env=False,
            follow_redirects=False,
            timeout=30,
        )
        self.gateway = Gateway(
            world.store.data_dir,
            {**world.gateway.policy, "user_agent": "landing-party-probe/0.1 " + identity_id},
            self.client,
            resolver=resolver or world.gateway._resolver,
        )
        self.route_hash = digest(
            {"proxy_url": proxy_url, "egress_id": egress_id, "identity_id": identity_id}
        )

    def metadata(self):
        return {
            "source_url": self.source_url,
            "egress_id": self.egress_id,
            "identity_id": self.identity_id,
            "route_hash": self.route_hash,
            "format": self.format,
            "selector": self.selector,
            "credential_binding": self.key_env,
            "method": "GET",
            "agent_lease": False,
            "actual_distinct_egress": {
                "unmeasured": {"reason": "requires operator routing evidence"}
            },
        }

    def extract(self, body):
        if self.format == "json":
            value = json.loads(body)
            if self.selector and not self.selector.startswith("/"):
                raise ValueError("A JSON extraction selector must be a JSON pointer")
            for token in self.selector.split("/")[1:] if self.selector else []:
                key = token.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list) and not re.fullmatch(r"0|[1-9][0-9]*", key):
                    raise ValueError("Array pointers require a nonnegative canonical index")
                value = value[int(key)] if isinstance(value, list) else value[key]
        else:
            matches = BeautifulSoup(body, "html.parser").select(self.selector)
            if len(matches) != 1:
                raise ValueError("A public-page metric selector must match exactly once")
            value = matches[0].get_text(" ", strip=True).replace(",", "")
        if isinstance(value, bool):
            raise TypeError("Boolean observations are not numeric footprint counts")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError("A finite nonnegative observed metric is required")
        return value

    async def __call__(self, target):
        url = https_url(target["url"])
        path, prefix = normalized_path(url), normalized_path(self.source_url)
        if origin(url) != origin(self.source_url) or not (
            path == prefix or path.startswith(prefix.rstrip("/") + "/")
        ):
            return blocked(
                "policy_deny", "Probe target is outside its registered source", "probe_source_scope"
            )
        headers = {}
        if self.key_env:
            key = os.environ.get(self.key_env)
            if not key:
                return blocked(
                    "policy_deny",
                    "Probe-only credential is unavailable",
                    "probe_credential_missing",
                )
            headers[self.key_header] = self.key_prefix + key
        store = self.world.store
        item = self.world.services["footprint"]._item(target["item_id"])
        if item["venue_class"] == "human" and self.key_env:
            return blocked(
                "policy_deny",
                "Human venue probes read public pages without credentials",
                "probe_public_only",
            )
        if self.key_env:
            capabilities = getattr(self.world, "capabilities", None)
            if capabilities and capabilities.verify(item["run_id"], key):
                return blocked(
                    "policy_deny",
                    "An agent capability is not a probe credential",
                    "probe_credential_scope",
                )
            if any(
                name.startswith("WORLD_")
                and "PROBE_" not in name
                and name.endswith(("_KEY", "_SECRET", "_TOKEN"))
                and value
                and hmac.compare_digest(value, key)
                for name, value in os.environ.items()
            ):
                return blocked(
                    "policy_deny",
                    "Probe credentials cannot reuse researcher/provider credentials",
                    "probe_credential_scope",
                )
        child = store.create_run(
            {
                "budget": {"usd": 0, "wall_s": 120},
                "parent_run_id": item["run_id"],
                "measurement_life_id": item.get("life_id"),
                "realm": "footprint-probe",
                "private_archive": True,
                "fixture_transport": isinstance(
                    getattr(self.client, "_transport", None), httpx.MockTransport
                ),
                "mode": "live",
                "capture_model_io": False,
                "harness_id": "world-probe@0.1",
                "probe_identity": self.identity_id,
                "probe_egress": self.egress_id,
            }
        )
        child_id, action_id = child["run_id"], identifier("a")
        store.append_record(
            child_id,
            "action",
            action_id=action_id,
            tool="footprint_probe",
            params={"url": url, "source_url": self.source_url},
            world_action=False,
            route=self.metadata(),
        )
        try:
            reply = await self.gateway.request(
                child_id,
                action_id,
                "GET",
                url,
                headers=headers,
                trusted={
                    "allow": True,
                    "allow_credentials": bool(headers),
                    "credential_header_names": list(headers),
                    "response_private": True,
                },
            )
            store.append_record(
                child_id,
                "outcome",
                action_id=action_id,
                reply_kind=reply.get("kind"),
                status=reply.get("status"),
                resource_hash=reply.get("hash"),
                rule_id=reply.get("rule_id"),
            )
            if reply.get("kind") != "result":
                return reply
            if not 200 <= reply["status"] < 300:
                return blocked(
                    "platform_block",
                    "Probe API/page did not return a successful observation",
                    "probe_status",
                    status=reply["status"],
                )
            body = self.gateway.read_private_response(child_id, reply["private_response_ref"])
            try:
                value = self.extract(body)
            except (ValueError, KeyError, IndexError, TypeError):
                return {
                    "unmeasured": {
                        "reason": "Registered selector did not yield one finite numeric observation"
                    },
                    "evidence_ref": f"{child_id}:{action_id}",
                }
            return {
                "value": value,
                "probe_views": self.probe_views,
                "evidence_ref": f"{child_id}:{action_id}",
                "observation_run_id": child_id,
                "identity_id": self.identity_id,
                "egress_id": self.egress_id,
            }
        finally:
            store.append_record(
                child_id,
                "seal",
                observation_finished_at=now(),
                verification=store.verify_record(child_id),
            )
            store.update_run(child_id, state="sealed")
            self.gateway.clear_run_secrets(child_id)

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()
