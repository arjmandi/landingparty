"""Read-only network boundary, private WARC/HAR archive, and offline replay.

Policy is operator configuration, never accepted from an agent action.  The direct
fetch path pins a validated address while preserving the HTTP host and TLS SNI.
An upstream proxy remains responsible for respecting that pinned tunnel address.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import mimetypes
import re
import socket
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import httpx
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from .scanning import CLASSIFIER_ID, classify_injection, contains_sensitive_data
from .transport import pinned_client

DEFAULT_POLICY = {
    "user_agent": "landing-party/0.1",
    "provider_hosts": ["api.openai.com", "api.anthropic.com", "api.search.brave.com"],
    "denied_hosts": [],
    "researcher_hosts": [],
    "domain_interval_s": 2.0,
    "domain_burst": 5,
    "domain_max_requests": 150,
    "global_requests_per_minute": 30,
    "max_bytes": 5_000_000,
    "timeout_s": 30,
    "max_redirects": 5,
    "robots_cache_s": 3600,
    "cooldown_s": 900,
}
SAFE_HEADERS = {"accept", "accept-language", "if-modified-since", "if-none-match", "range"}
PRIVATE_NAMES = {"localhost", "metadata.google.internal", "metadata", "instance-data"}
LOGIN_PATH = re.compile(
    r"/(?:login|log-in|signin|sign-in|oauth|authorize|auth)(?:/|$)", re.IGNORECASE
)
SECRET_PATTERN = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[A-Z0-9]{16}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
    re.IGNORECASE,
)
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def blocked(block_type: str, detail: str, rule_id: str, **extra) -> dict:
    return {"kind": "blocked", "type": block_type, "detail": detail, "rule_id": rule_id, **extra}


def canonical_url(url: str) -> str:
    """A scheme-preserving SURT-style archive key; fragments are not requests."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    labels = ",".join(reversed(host.split(".")))
    port = parsed.port
    if port and (parsed.scheme, port) not in {("https", 443), ("http", 80)}:
        labels += f":{port}"
    return f"{parsed.scheme.lower()}://({labels},){parsed.path or '/'}" + (
        "?" + parsed.query if parsed.query else ""
    )


async def resolve_host(host: str, port: int) -> list[str]:
    answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(answer[4][0] for answer in answers))


def _robots_octets(value: str) -> str:
    """RFC 9309 octet comparison: decode unreserved, preserve reserved escapes."""
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"

    def normalize(match):
        character = chr(int(match[0][1:], 16))
        return character if character in unreserved else match[0].upper()

    value = re.sub(r"%[0-9a-fA-F]{2}", normalize, value)
    return quote(value, safe="/?:@!$&'()*+,;=-._~%")


class RobotsRules:
    """The RFC 9309 group selection, wildcard, and longest-rule matching rules."""

    def __init__(self, text: str):
        self.groups = []
        agents, rules = [], []
        seen_rule = False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if seen_rule:
                    self.groups.append((agents, rules))
                    agents, rules, seen_rule = [], [], False
                agents.append(value.lower())
            elif key in {"allow", "disallow"} and agents:
                seen_rule = True
                if value:
                    rules.append((key == "allow", _robots_octets(value)))
        if agents:
            self.groups.append((agents, rules))

    def can_fetch(self, user_agent: str, url: str) -> bool:
        product = user_agent.split("/", 1)[0].lower()
        specificity = -1
        selected = []
        for agents, rules in self.groups:
            matches = [len(agent) for agent in agents if agent != "*" and agent in product]
            match = max(matches, default=0 if "*" in agents else -1)
            if match > specificity:
                specificity, selected = match, list(rules)
            elif match >= 0 and match == specificity:
                selected.extend(rules)
        parsed = urlsplit(url)
        path = _robots_octets((parsed.path or "/") + ("?" + parsed.query if parsed.query else ""))
        longest, allow = -1, True
        for allowed, pattern in selected:
            terminal = pattern.endswith("$")
            raw = pattern[:-1] if terminal else pattern
            expression = "^" + ".*".join(re.escape(part) for part in raw.split("*"))
            if terminal:
                expression += "$"
            if re.search(expression, path):
                length = len(raw.replace("*", "").encode())
                if length > longest or (length == longest and allowed):
                    longest, allow = length, allowed
        return allow


class Gateway:
    def __init__(
        self,
        data_dir: Path,
        policy: dict | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        resolver: Callable[[str, int], Awaitable[list[str]]] | None = None,
        signer=None,
    ):
        self.data_dir = Path(data_dir)
        categories_path = Path(__file__).parents[1] / "policy/categories.json"
        categories = (
            json.loads(categories_path.read_text())["categories"]
            if categories_path.exists()
            else {}
        )
        self.policy = {**DEFAULT_POLICY, "category_hosts": categories, **(policy or {})}
        self.classifier_id = CLASSIFIER_ID
        self._client = client or pinned_client(timeout=self.policy["timeout_s"])
        self._owns_client = client is None
        self._resolver = resolver or resolve_host
        self.signer = signer
        self._private_response_headers = {}
        self._private_response_paths = {}
        self.payment_handler = None
        self._locks = defaultdict(asyncio.Lock)
        self._rate_lock = asyncio.Lock()
        self._domain_tokens: dict[tuple[str, str], tuple[float, float]] = {}
        self._counts = defaultdict(int)
        self._global_times = defaultdict(deque)
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._failures = defaultdict(int)
        self._robots: dict[tuple[str, str], tuple[float, object]] = {}
        self._robots_locks = defaultdict(asyncio.Lock)

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()

    def _run_dir(self, run_id: str) -> Path:
        if not ID_PATTERN.fullmatch(run_id):
            raise ValueError("Invalid run id")
        path = self.data_dir / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def _syntax_policy(
        self, url: str, headers: dict | None = None, *, trusted: dict | None = None
    ) -> dict | None:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower().rstrip(".")
            port = parsed.port
        except (ValueError, UnicodeError):
            return blocked("policy_deny", "Malformed URL", "invalid_url")
        if len(url) > 16384 or any(ord(c) < 32 for c in url):
            return blocked("policy_deny", "Malformed URL", "invalid_url")
        if parsed.scheme not in {"http", "https"} or not host:
            return blocked(
                "policy_deny", "Only public HTTP and HTTPS URLs are supported", "url_scheme"
            )
        if parsed.username is not None or parsed.password is not None:
            return blocked("policy_deny", "Credentials in URLs are not allowed", "url_credentials")
        if port not in {None, 80, 443} and host not in self.policy.get("test_allowed_hosts", []):
            return blocked(
                "policy_deny", "Nonstandard destination ports are denied", "destination_port"
            )
        if host in self.policy["provider_hosts"] and not (
            trusted
            and trusted.get("allow")
            and trusted.get("provider_operation") in {"perception", "payment", "search"}
            and trusted.get("allowed_provider_host") == host
            and trusted.get("reservation_id")
        ):
            return blocked(
                "policy_deny", "Provider traffic must use the metered lane", "provider_bypass"
            )
        denied = self.policy["denied_hosts"] + self.policy["researcher_hosts"]
        if any(host == entry or host.endswith("." + entry) for entry in denied):
            return blocked(
                "policy_deny", "Destination denied by the operator policy", "denied_host"
            )
        if host.endswith((".onion", ".localhost")) or host in PRIVATE_NAMES:
            return blocked("policy_deny", "Private destinations are denied", "private_destination")
        for category, hosts in self.policy.get("category_hosts", {}).items():
            if any(host == entry or host.endswith("." + entry) for entry in hosts) and not (
                trusted and category in trusted.get("allowed_categories", [])
            ):
                return blocked(
                    "policy_deny",
                    "Destination belongs to a denied category",
                    "category_" + category,
                )
        if LOGIN_PATH.search(unquote(parsed.path)) and not (trusted and trusted.get("allow_login")):
            return blocked("policy_deny", "Login and OAuth require a vault lease", "login_path")
        if contains_sensitive_data(url):
            return blocked(
                "policy_deny", "Sensitive data detected in the outbound URL", "exfiltration"
            )
        if headers:
            for key, value in headers.items():
                if contains_sensitive_data(str(value)) and not (
                    trusted
                    and trusted.get("allow_credentials")
                    and key.lower()
                    in (
                        {"authorization", "cookie"}
                        | {
                            str(name).lower()
                            for name in trusted.get(
                                "credential_header_names", trusted.get("credential_headers", [])
                            )
                        }
                    )
                ):
                    return blocked(
                        "policy_deny", "Sensitive data detected in outbound headers", "exfiltration"
                    )
                browser_public_header = (
                    trusted
                    and trusted.get("allow")
                    and key.lower()
                    not in {"authorization", "cookie", "proxy-authorization", "host"}
                    and not key.lower().startswith("x-world-")
                )
                if key.lower() not in SAFE_HEADERS and not (
                    browser_public_header or (trusted and trusted.get("allow_credentials"))
                ):
                    return blocked(
                        "policy_deny",
                        "Only public content-negotiation headers are allowed",
                        "unsafe_header",
                    )
                if (
                    "\r" in str(value)
                    or "\n" in str(value)
                    or "\r" in str(key)
                    or "\n" in str(key)
                    or (
                        SECRET_PATTERN.search(str(value))
                        and not (
                            trusted
                            and trusted.get("allow_credentials")
                            and key.lower()
                            in (
                                {"authorization", "cookie"}
                                | {
                                    str(name).lower()
                                    for name in trusted.get(
                                        "credential_header_names",
                                        trusted.get("credential_headers", []),
                                    )
                                }
                            )
                        )
                    )
                ):
                    return blocked(
                        "policy_deny", "Sensitive or invalid outbound header", "exfiltration"
                    )
        return None

    async def _target(self, url: str) -> tuple[str | None, dict | None]:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        try:
            literal = ipaddress.ip_address(host)
            addresses = [str(literal)]
        except ValueError:
            try:
                addresses = await asyncio.wait_for(
                    self._resolver(host, parsed.port or (443 if parsed.scheme == "https" else 80)),
                    timeout=self.policy["timeout_s"],
                )
            except (TimeoutError, OSError):
                return None, blocked(
                    "dns", "The destination could not be resolved", "dns_resolution"
                )
        if not addresses:
            return None, blocked("dns", "The destination has no addresses", "dns_resolution")
        # All answers must be public, not just the first selected address.
        for address in addresses:
            try:
                public = ipaddress.ip_address(address).is_global
            except ValueError:
                public = False
            if not public and host not in self.policy.get("test_allowed_hosts", []):
                return None, blocked(
                    "policy_deny",
                    "Private or reserved network addresses are denied",
                    "private_destination",
                )
        return addresses[0], None

    async def _rate(self, run_id: str, host: str) -> dict | None:
        """A per-run, per-domain token bucket, plus a per-run global window."""
        async with self._rate_lock:
            clock = time.monotonic()
            key = (run_id, host)
            cooldown = self._cooldowns.get(key, 0)
            if cooldown > clock:
                return blocked(
                    "rate_limit",
                    "Destination is cooling down",
                    "domain_cooldown",
                    retry_after=round(cooldown - clock, 3),
                )
            if self._counts[key] >= self.policy["domain_max_requests"]:
                return blocked(
                    "rate_limit", "Per-domain episode limit reached", "domain_episode_limit"
                )
            window = self._global_times[run_id]
            while window and window[0] <= clock - 60:
                window.popleft()
            if len(window) >= self.policy["global_requests_per_minute"]:
                return blocked(
                    "rate_limit",
                    "Global request rate reached",
                    "global_rate",
                    retry_after=max(0.001, 60 - (clock - window[0])),
                )
            interval = self.policy["domain_interval_s"]
            burst = self.policy["domain_burst"]
            tokens, last = self._domain_tokens.get(key, (float(burst), clock))
            tokens = (
                min(float(burst), tokens + (clock - last) / interval)
                if interval > 0
                else float(burst)
            )
            if tokens < 1:
                self._domain_tokens[key] = (tokens, clock)
                return blocked(
                    "rate_limit",
                    "Per-domain request rate reached",
                    "domain_rate",
                    retry_after=round((1 - tokens) * interval, 3),
                )
            self._domain_tokens[key] = (tokens - 1, clock)
            self._counts[key] += 1
            window.append(clock)
        return None

    @staticmethod
    def _retry_after(headers: dict) -> float | None:
        value = headers.get("retry-after", "")
        if value.isdigit():
            return float(value)
        if value:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                return max(0.0, (date - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        return None

    def _observe_status(self, run_id: str, host: str, status: int, headers: dict):
        key = (run_id, host)
        if status in {403, 429, 503}:
            self._failures[key] += 1
            retry = self._retry_after(headers)
            if retry is not None:
                self._cooldowns[key] = max(self._cooldowns.get(key, 0), time.monotonic() + retry)
            if self._failures[key] >= 3:
                self._cooldowns[key] = max(
                    self._cooldowns.get(key, 0), time.monotonic() + self.policy["cooldown_s"]
                )
        else:
            self._failures[key] = 0

    async def _request(
        self,
        url: str,
        address: str,
        run_id: str,
        headers: dict | None = None,
        *,
        method: str = "GET",
        body: bytes = b"",
        signed: bool = False,
        user_agent_run_id: str | None = None,
    ) -> tuple[int, dict, bytes]:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        address_host = f"[{address}]" if ":" in address else address
        authority = address_host + (f":{parsed.port}" if parsed.port else "")
        pinned_url = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
        host_header = host + (f":{parsed.port}" if parsed.port else "")
        outbound = {
            "Accept": "*/*",
            **(headers or {}),
            "Host": host_header,
            "User-Agent": f"{self.policy['user_agent']} (run {user_agent_run_id or run_id})",
        }
        if signed:
            if self.signer is None:
                raise ValueError("Signed identity requires a configured world signing key")
            outbound.update(self.signer.sign(method, url))
        request = self._client.build_request(method, pinned_url, headers=outbound, content=body)
        request.extensions["sni_hostname"] = host
        async with asyncio.timeout(self.policy["timeout_s"]):
            response = await self._client.send(request, stream=True, follow_redirects=False)
            try:
                if int(response.headers.get("content-length", "0")) > self.policy["max_bytes"]:
                    raise ResponseTooLarge
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.policy["max_bytes"]:
                        raise ResponseTooLarge
                    chunks.append(chunk)
                return response.status_code, dict(response.headers), b"".join(chunks)
            finally:
                await response.aclose()

    async def _robots_allowed(self, run_id: str, url: str, *, signed=False) -> dict | None:
        parsed = urlsplit(url)
        async with self._robots_locks[(run_id, f"{parsed.scheme}://{parsed.netloc}")]:
            return await self._robots_allowed_locked(run_id, url, signed=signed)

    async def _robots_allowed_locked(self, run_id: str, url: str, *, signed=False) -> dict | None:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        key = (run_id, origin)
        cached = self._robots.get(key)
        if cached and cached[0] > time.monotonic():
            parser = cached[1]
        else:
            robot_url = origin + "/robots.txt"
            status = 0
            body = b""
            # Robot redirects get the same public target validation as content.
            for _ in range(self.policy["max_redirects"] + 1):
                denial = self._syntax_policy(robot_url)
                if denial:
                    return blocked(
                        "robots", "Robots policy redirect is not permitted", "robots_redirect"
                    )
                address, denial = await self._target(robot_url)
                if denial:
                    return blocked("robots", "Robots policy is unavailable", "robots_unavailable")
                denial = await self._rate(run_id, urlsplit(robot_url).hostname or "")
                if denial:
                    return denial
                try:
                    status, response_headers, body = await self._request(
                        robot_url, address, run_id, signed=signed
                    )
                    await self._archive_transaction(
                        run_id, robot_url, status, response_headers, body
                    )
                    self._observe_status(
                        run_id, urlsplit(robot_url).hostname or "", status, response_headers
                    )
                except (TimeoutError, httpx.HTTPError, OSError, ValueError, ResponseTooLarge):
                    return blocked("robots", "Robots policy is unavailable", "robots_unavailable")
                if status in {301, 302, 303, 307, 308} and response_headers.get("location"):
                    robot_url = urljoin(robot_url, response_headers["location"])
                    continue
                break
            else:
                return blocked("robots", "Too many robots redirects", "robots_redirect")
            if status == 429 or status >= 500:
                return blocked(
                    "robots", "Robots policy is temporarily unavailable", "robots_unavailable"
                )
            if 200 <= status < 300:
                parser = RobotsRules(body.decode("utf-8", errors="replace"))
            else:
                # RFC 9309: 4xx robots responses mean unavailable, access allowed.
                parser = RobotsRules("")
            self._robots[key] = (time.monotonic() + self.policy["robots_cache_s"], parser)
        if not parser.can_fetch(self.policy["user_agent"], url):
            return blocked(
                "robots", "The site's robots policy disallows this resource", "robots_disallow"
            )
        return None

    async def _archive_transaction(
        self, run_id: str, url: str, status: int, headers: dict, body: bytes, *, method: str = "GET"
    ):
        root = self._run_dir(run_id)
        async with self._locks[run_id]:
            # Decoded bytes are archived, so transport encodings must not be retained.
            safe_headers = [
                (k, v)
                for k, v in headers.items()
                if k.lower()
                not in {"set-cookie", "content-encoding", "transfer-encoding", "content-length"}
            ]
            safe_headers.append(("Content-Length", str(len(body))))
            with (root / "archive.warc.gz").open("ab") as stream:
                writer = WARCWriter(stream, gzip=True)
                record = writer.create_warc_record(
                    url,
                    "response",
                    payload=BytesIO(body),
                    http_headers=StatusAndHeaders(
                        f"{status} {httpx.codes.get_reason_phrase(status)}",
                        safe_headers,
                        protocol="HTTP/1.1",
                    ),
                )
                writer.write_record(record)
            har_path = root / "archive.har"
            if har_path.exists():
                har = json.loads(har_path.read_text())
            else:
                har = {
                    "log": {
                        "version": "1.2",
                        "creator": {"name": "landing-party", "version": "0.1"},
                        "entries": [],
                    }
                }
            har["log"]["entries"].append(
                {
                    "startedDateTime": now(),
                    "time": 0,
                    "request": {
                        "method": method,
                        "url": url,
                        "headers": [],
                        "httpVersion": "HTTP/1.1",
                    },
                    "response": {
                        "status": status,
                        "httpVersion": "HTTP/1.1",
                        "headers": [{"name": k, "value": v} for k, v in safe_headers],
                        "content": {
                            "size": len(body),
                            "mimeType": headers.get("content-type", "application/octet-stream"),
                        },
                    },
                    "cache": {},
                    "timings": {"send": 0, "wait": 0, "receive": 0},
                }
            )
            temporary = root / "archive.har.tmp"
            temporary.write_text(json.dumps(har))
            temporary.replace(har_path)

    async def _index(
        self,
        run_id: str,
        url: str,
        reply: dict,
        body_path: str | None = None,
        *,
        method="GET",
        request_body=b"",
    ):
        root = self._run_dir(run_id)
        async with self._locks[run_id]:
            path = root / "archive-index.json"
            data = json.loads(path.read_text()) if path.exists() else {}
            data[self._archive_key(url, method, request_body)] = {
                "reply": reply,
                "body_path": body_path,
            }
            temporary = root / "archive-index.json.tmp"
            temporary.write_text(json.dumps(data, ensure_ascii=False))
            temporary.replace(path)

    async def _replay(
        self,
        run_id: str,
        action_id: str,
        url: str,
        archive_in: str | None,
        inline: bool,
        max_inline_chars: int,
        *,
        method="GET",
        request_body=b"",
        response_private=False,
    ) -> dict:
        if not archive_in or not ID_PATTERN.fullmatch(archive_in):
            return blocked("replay_miss", "No valid source archive was selected", "replay_archive")
        source = self.data_dir / "runs" / archive_in
        try:
            index = json.loads((source / "archive-index.json").read_text())
            entry = index[self._archive_key(url, method, request_body)]
        except (OSError, ValueError, KeyError):
            return blocked(
                "replay_miss", "This URL is absent from the selected archive", "replay_lookup"
            )
        reply = dict(entry["reply"])
        if reply["kind"] == "blocked":
            return reply
        relative = Path(entry.get("body_path") or "")
        source_private = relative.parts[:1] == ("private-responses",)
        if source_private and not response_private:
            return blocked(
                "replay_miss",
                "Private browser/provider captures are not public fetch resources",
                "replay_private_scope",
            )
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or (relative.parts[:2] != ("world", "fetched") and not source_private)
        ):
            return blocked("replay_miss", "Invalid archived resource path", "replay_integrity")
        path = (source / relative).resolve()
        if not path.is_relative_to(source.resolve()):
            return blocked("replay_miss", "Archived resource escapes its run", "replay_integrity")
        try:
            body = path.read_bytes()
        except OSError:
            return blocked("replay_miss", "Archived body is unavailable", "replay_integrity")
        if digest(body) != reply.get("hash"):
            return blocked(
                "replay_miss", "Archived body failed its content hash", "replay_integrity"
            )
        filename = action_id + relative.suffix
        destination = (
            self._run_dir(run_id)
            / ("private-responses" if response_private else "world/fetched")
            / filename
        )
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.write_bytes(body)
        if response_private:
            reply.pop("handle", None)
            reply.pop("preview", None)
            reference = "n_" + action_id
            reply["private_response_ref"] = reference
            self._private_response_paths[(run_id, reference)] = destination
        else:
            reply["handle"] = "/world/fetched/" + filename
        reply["url"] = url
        reply["provenance"] = {
            **reply.get("provenance", {}),
            "capture_mode": "replay",
            "archive_id": archive_in,
        }
        reply.pop("inline", None)
        reply.pop("inline_encoding", None)
        if not response_private:
            self._attach_inline(reply, body, inline, max_inline_chars)
        await self._index(
            run_id,
            url,
            reply,
            str(destination.relative_to(self._run_dir(run_id))),
            method=method,
            request_body=request_body,
        )
        return reply

    @staticmethod
    def _attach_inline(reply: dict, body: bytes, inline: bool, max_chars: int):
        if not inline:
            return
        content_type = reply.get("content_type", "")
        if content_type.startswith("text/") or any(
            x in content_type for x in ("json", "xml", "javascript")
        ):
            reply["inline"] = body.decode("utf-8", errors="replace")[:max_chars]
            reply["inline_encoding"] = "utf-8"
        else:
            reply["inline"] = base64.b64encode(body).decode("ascii")[:max_chars]
            reply["inline_encoding"] = "base64"
        reply["inline_truncated"] = (
            len(body.decode("utf-8", errors="replace")) > max_chars
            if reply["inline_encoding"] == "utf-8"
            else len(base64.b64encode(body)) > max_chars
        )

    async def fetch(
        self,
        run_id: str,
        action_id: str,
        url: str,
        *,
        headers: dict | None = None,
        inline: bool = False,
        max_inline_chars: int = 8000,
        mode: str = "live",
        archive_in: str | None = None,
        _method: str = "GET",
        _body: bytes = b"",
        _trusted: dict | None = None,
        _signed: bool = False,
    ) -> dict:
        method, request_body = _method.upper(), _body
        if method not in {"GET", "HEAD", "OPTIONS"} and not (
            _trusted and _trusted.get("allow") and _trusted.get("allow_write")
        ):
            return blocked(
                "policy_deny", "External writes require an approved scoped lease", "write_method"
            )
        if len(request_body) > self.policy["max_bytes"]:
            return blocked("policy_deny", "Outbound request exceeds size limit", "request_size")
        if contains_sensitive_data(request_body) and not (
            _trusted and _trusted.get("credential_body")
        ):
            return blocked(
                "policy_deny", "Sensitive data detected in outbound body", "exfiltration"
            )
        if _signed and self.signer is None:
            return blocked(
                "policy_deny",
                "Signed identity requires a configured world signing key",
                "signing_key_missing",
            )
        if not ID_PATTERN.fullmatch(action_id):
            raise ValueError("Invalid action id")
        self._run_dir(run_id)
        denial = self._syntax_policy(url, headers, trusted=_trusted)
        if denial:
            return denial
        max_inline_chars = min(100_000, max(0, int(max_inline_chars)))
        if mode == "replay":
            return await self._replay(
                run_id,
                action_id,
                url,
                archive_in,
                inline,
                max_inline_chars,
                method=method,
                request_body=request_body,
                response_private=bool(_trusted and _trusted.get("response_private")),
            )
        if mode != "live":
            return blocked("policy_deny", "Unsupported capture mode", "capture_mode")
        original_url = url
        redirects = []
        for step in range(self.policy["max_redirects"] + 1):
            denial = self._syntax_policy(url, headers, trusted=_trusted)
            if denial:
                return denial
            address, denial = await self._target(url)
            if denial:
                return denial
            provider_api = bool(
                _trusted
                and _trusted.get("provider_operation") in {"perception", "payment", "search"}
                and _trusted.get("allowed_provider_host") == urlsplit(url).hostname
                and _trusted.get("reservation_id")
            )
            if urlsplit(url).path != "/robots.txt" and not provider_api:
                denial = await self._robots_allowed(run_id, url, signed=_signed)
                if denial:
                    return denial
            host = urlsplit(url).hostname or ""
            denial = await self._rate(run_id, host)
            if denial:
                return denial
            try:
                status, response_headers, body = await self._request(
                    url,
                    address,
                    run_id,
                    headers,
                    method=method,
                    body=request_body,
                    signed=_signed,
                    user_agent_run_id=(_trusted or {}).get("user_agent_run_id"),
                )
            except ResponseTooLarge:
                return blocked(
                    "policy_deny", "The response exceeds the configured size limit", "response_size"
                )
            except (TimeoutError, httpx.TimeoutException):
                return blocked(
                    "timeout", "The request exceeded the configured timeout", "request_timeout"
                )
            except (httpx.ConnectError, httpx.ProxyError) as exc:
                block_type = (
                    "tls"
                    if "ssl" in str(exc).lower() or "certificate" in str(exc).lower()
                    else "dns"
                )
                return blocked(
                    block_type, "A verified connection could not be established", "connection_error"
                )
            except (httpx.HTTPError, OSError, ValueError):
                return blocked("timeout", "The request could not be completed", "request_error")
            await self._archive_transaction(
                run_id, url, status, response_headers, body, method=method
            )
            self._observe_status(run_id, host, status, response_headers)
            if status == 402 and not provider_api:
                quote = self.payment_quote(url, response_headers, body)
                paid = None
                if (
                    mode == "live"
                    and method in {"GET", "HEAD"}
                    and self.payment_handler
                    and quote.get("amount") is not None
                ):
                    paid = await self.payment_handler(
                        {
                            "run_id": run_id,
                            "action_id": action_id,
                            "url": url,
                            "body_hash": digest(body),
                            **quote,
                        }
                    )
                if paid and paid.get("kind") == "result" and paid.get("paid") is True:
                    from decimal import Decimal, InvalidOperation

                    try:
                        valid_receipt = (
                            paid.get("receipt_ref")
                            and paid.get("merchant") == quote["merchant"]
                            and paid.get("currency") == "USD"
                            and Decimal(str(paid.get("amount"))) == Decimal(quote["amount"])
                        )
                    except (InvalidOperation, ValueError, TypeError):
                        valid_receipt = False
                    access = paid.get("access_headers", {})
                    if (
                        not valid_receipt
                        or not isinstance(access, dict)
                        or not access
                        or any(
                            not isinstance(k, str)
                            or not isinstance(v, str)
                            or "\r" in k + v
                            or "\n" in k + v
                            for k, v in access.items()
                        )
                    ):
                        return blocked(
                            "policy_deny",
                            "The payment receipt or access proof is invalid",
                            "payment_receipt",
                        )
                    if any(
                        key.lower()
                        in {"host", "proxy-authorization", "content-length", "transfer-encoding"}
                        for key in access
                    ):
                        return blocked(
                            "policy_deny",
                            "The payment proof contains an unsafe header",
                            "payment_receipt",
                        )
                    headers = {**(headers or {}), **access}
                    _trusted = {
                        **(_trusted or {}),
                        "allow": True,
                        "allow_credentials": True,
                        "credential_header_names": list(access),
                    }
                    try:
                        status, response_headers, body = await self._request(
                            url,
                            address,
                            run_id,
                            headers,
                            method=method,
                            body=request_body,
                            signed=_signed,
                            user_agent_run_id=_trusted.get("user_agent_run_id"),
                        )
                    except (TimeoutError, httpx.HTTPError, OSError, ValueError, ResponseTooLarge):
                        return blocked(
                            "timeout",
                            "Paid access could not be retrieved",
                            "payment_access_failed",
                            receipt_ref=paid["receipt_ref"],
                        )
                    if any(len(proof) >= 8 and proof.encode() in body for proof in access.values()):
                        return blocked(
                            "policy_deny",
                            "The site reflected a private payment proof",
                            "credential_reflection",
                        )
                    await self._archive_transaction(
                        run_id, url, status, response_headers, body, method=method
                    )
                    if status == 402:
                        return blocked(
                            "paywall_402",
                            "The merchant did not accept the paid access proof",
                            "payment_access_failed",
                            receipt_ref=paid["receipt_ref"],
                        )
                else:
                    reply = (
                        paid
                        if paid and paid.get("kind") == "blocked"
                        else blocked(
                            "paywall_402",
                            "The site requires an approved payment mandate",
                            "payment_required",
                        )
                    )
                    reply = {
                        **reply,
                        **{key: value for key, value in quote.items() if value is not None},
                    }
                    await self._index(
                        run_id, original_url, reply, method=_method, request_body=_body
                    )
                    return reply
            if (
                status in {301, 302, 303, 307, 308}
                and response_headers.get("location")
                and not _trusted
            ):
                redirects.append(
                    {"url": url, "status": status, "location": response_headers["location"]}
                )
                next_url = urljoin(url, response_headers["location"])
                if _trusted and urlsplit(next_url).netloc != urlsplit(url).netloc:
                    return blocked(
                        "policy_deny",
                        "Lease-bound redirects cannot change origin",
                        "lease_redirect_scope",
                    )
                if status == 303 or (status in {301, 302} and method == "POST"):
                    method, request_body = "GET", b""
                url = next_url
                if step == self.policy["max_redirects"]:
                    return blocked(
                        "policy_deny", "The redirect limit was reached", "redirect_limit"
                    )
                continue
            classification = (
                None
                if provider_api and _trusted.get("response_private")
                else self._classify(status, response_headers, body)
            )
            if classification:
                await self._index(
                    run_id, original_url, classification, method=_method, request_body=_body
                )
                return classification
            content_type = (
                response_headers.get("content-type", "application/octet-stream")
                .split(";", 1)[0]
                .strip()
            )
            extension = (
                {
                    "text/html": ".html",
                    "text/plain": ".txt",
                    "application/json": ".json",
                    "application/pdf": ".pdf",
                }.get(content_type)
                or mimetypes.guess_extension(content_type)
                or ".bin"
            )
            filename = action_id + extension
            private = bool(_trusted and _trusted.get("response_private"))
            relative = (
                Path("private-responses") / filename
                if private
                else Path("world") / "fetched" / filename
            )
            destination = self._run_dir(run_id) / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_bytes(body)
            classification = classify_injection(body)
            reply = {
                "kind": "result",
                "method": method,
                "signature_key_id": self.signer.key_id if _signed else None,
                "handle": "/world/fetched/" + filename,
                "url": original_url,
                "final_url": url,
                "status": status,
                "content_type": content_type,
                "bytes": len(body),
                "hash": digest(body),
                "headers_subset": {
                    k: v
                    for k, v in response_headers.items()
                    if k in {"content-type", "last-modified", "etag", "date", "content-length"}
                },
                "preview": body.decode("utf-8", errors="replace")[:2000]
                if content_type.startswith("text/") or "json" in content_type
                else "",
                "redirects": redirects,
                "warc_ref": f"runs/{run_id}/archive.warc.gz",
                "policy": {"decision": "allow", "rule_id": "allow_public_get"},
                "provenance": {
                    "origin": host,
                    "fetch_ts": now(),
                    "capture_mode": "live",
                    "trust_class": "unknown",
                    "injection_score": classification["injection_score"],
                    "classifier_id": classification["classifier_id"],
                    "injection_signals": classification["signals"],
                    "classifier_calibrated": False,
                },
            }
            if private:
                reply.pop("handle", None)
                reply.pop("preview", None)
                reference = "n_" + action_id
                reply["private_response_ref"] = reference
                self._private_response_paths[(run_id, reference)] = destination
            else:
                self._attach_inline(reply, body, inline, max_inline_chars)
            if private or (_trusted and _trusted.get("allow_credentials")):
                self._private_response_headers[
                    (run_id, reply.get("private_response_ref") or reply["handle"])
                ] = response_headers
            await self._index(
                run_id, original_url, reply, str(relative), method=_method, request_body=_body
            )
            return reply
        return blocked("policy_deny", "The redirect limit was reached", "redirect_limit")

    @staticmethod
    def payment_quote(url, headers, body):
        """Parse a finite USD quote, bound to the authenticated merchant origin."""
        from decimal import Decimal, InvalidOperation

        parsed = urlsplit(url)
        merchant = f"{parsed.scheme}://{parsed.netloc.lower()}"
        try:
            data = json.loads(body) if len(body) <= 64_000 else {}
            if not isinstance(data, dict):
                data = {}
        except (ValueError, UnicodeError):
            data = {}
        amount = headers.get("x-payment-amount") or data.get("amount") or data.get("price")
        if isinstance(amount, dict):
            amount = amount.get("amount")
        currency = headers.get("x-payment-currency") or data.get("currency", "USD")
        claimed = headers.get("x-payment-merchant") or data.get("merchant", merchant)
        try:
            value = Decimal(str(amount))
            if (
                not value.is_finite()
                or value <= 0
                or currency != "USD"
                or claimed.rstrip("/") != merchant
                or parsed.scheme != "https"
            ):
                amount = None
            else:
                amount = str(value)
        except (InvalidOperation, ValueError, TypeError):
            amount = None
        quote = {
            "url": url,
            "merchant": merchant,
            "amount": amount,
            "currency": currency,
            "quote_id": headers.get("x-payment-quote-id") or data.get("quote_id"),
            "quote_body_hash": digest(body),
        }
        quote["quote_hash"] = digest(
            json.dumps(quote, sort_keys=True, separators=(",", ":")).encode()
        )
        return {key: value for key, value in quote.items() if key != "url"}

    @staticmethod
    def _archive_key(url, method="GET", request_body=b""):
        base = canonical_url(url)
        return (
            base
            if method.upper() == "GET" and not request_body
            else f"{method.upper()}:{digest(request_body)} {base}"
        )

    async def request(
        self,
        run_id,
        action_id,
        method,
        url,
        *,
        headers=None,
        body=b"",
        trusted=None,
        mode="live",
        archive_in=None,
        signed=False,
    ):
        """Trusted server interface; never exposed as arbitrary agent tool parameters."""
        return await self.fetch(
            run_id,
            action_id,
            url,
            headers=headers,
            mode=mode,
            archive_in=archive_in,
            _method=method,
            _body=body,
            _trusted=trusted,
            _signed=signed,
        )

    def read_private_response(self, run_id, reference):
        """Internal only: no agent endpoint exposes private provider/browser bodies."""
        path = self._private_response_paths.get((run_id, reference))
        if path is None:
            raise KeyError("Private response reference is unavailable")
        root = (self._run_dir(run_id) / "private-responses").resolve()
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise ValueError("Private response escaped its run")
        return path.read_bytes()

    def clear_run_secrets(self, run_id):
        for mapping in (self._private_response_headers, self._private_response_paths):
            for key in list(mapping):
                if key[0] == run_id:
                    del mapping[key]

    def consume_response_headers(self, run_id, handle):
        """Single-use world-browser channel. Cookies never enter record/archive replies."""
        return self._private_response_headers.pop((run_id, handle), {})

    def _classify(self, status: int, headers: dict, body: bytes) -> dict | None:
        text = body[:64000].decode("utf-8", errors="replace").lower()
        if status == 402:
            return blocked("paywall_402", "The site requires payment", "payment_required")
        if status == 401:
            return blocked(
                "login_wall", "The site requires authentication", "authentication_required"
            )
        if status == 429:
            retry = self._retry_after(headers)
            extra = {"retry_after": retry} if retry is not None else {}
            return blocked(
                "rate_limit", "The destination returned a rate limit", "upstream_rate", **extra
            )
        if status in {403, 503}:
            if any(
                token in text
                for token in ("captcha", "cf-chl-", "challenge-platform", "verify you are human")
            ):
                return blocked("captcha", "The site requires a human challenge", "human_challenge")
            return blocked(
                "platform_block",
                f"The destination refused the request (HTTP {status})",
                "platform_refusal",
            )
        return None


class ResponseTooLarge(Exception):
    """Raised before an oversized body can be retained."""
