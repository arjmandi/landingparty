"""Explicit, TLS-verified DNS-over-HTTPS fallback for proxy-only environments.

This resolver never changes egress policy or accepts an agent-selected DNS server.
Its answers still undergo Gateway public-address validation and connection pinning.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from urllib.parse import urlsplit

import httpx

from .gateway import resolve_host


class SecureDNSResolver:
    def __init__(
        self, endpoint: str, *, client: httpx.AsyncClient | None = None, direct_first=True
    ):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise ValueError("DNS fallback must be an operator-configured HTTPS endpoint")
        try:
            literal = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            literal = None
        if (
            (literal is not None and not literal.is_global)
            or parsed.hostname in {"localhost", "metadata.google.internal"}
            or parsed.hostname.endswith((".localhost", ".onion"))
        ):
            raise ValueError("DNS fallback endpoint must not be private")
        self.endpoint = endpoint
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self.owns_client = client is None
        self.direct_first = direct_first
        self.cache = {}

    async def __call__(self, host: str, port: int) -> list[str]:
        if self.direct_first:
            try:
                return await asyncio.wait_for(resolve_host(host, port), timeout=2)
            except (OSError, TimeoutError):
                pass
        key = host.encode("idna").decode("ascii").lower()
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            return list(cached[1])
        answers, ttls = [], []
        for query_type, expected in [("A", 1), ("AAAA", 28)]:
            try:
                response = await self.client.get(
                    self.endpoint,
                    params={"name": key, "type": query_type, "cd": "false"},
                    headers={"Accept": "application/dns-json"},
                )
                response.raise_for_status()
                data = response.json()
                if data.get("Status") != 0 or data.get("TC"):
                    raise OSError("DNS fallback returned an incomplete or failed answer")
                for record in data.get("Answer", []):
                    if record.get("type") != expected:
                        continue
                    address = ipaddress.ip_address(record["data"])
                    if (expected == 1 and address.version != 4) or (
                        expected == 28 and address.version != 6
                    ):
                        raise OSError("DNS answer family does not match its question")
                    answers.append(str(address))
                    ttls.append(max(0, min(3600, int(record.get("TTL", 0)))))
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise OSError("Verified DNS-over-HTTPS resolution failed") from exc
        if not answers:
            raise OSError("DNS-over-HTTPS returned no addresses")
        unique = list(dict.fromkeys(answers))
        self.cache[key] = (time.monotonic() + min(ttls, default=0), unique)
        return unique

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()
