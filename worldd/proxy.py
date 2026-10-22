"""Capability-bound HTTP proxy and HTTPS CONNECT interception inside worldd.

The public proxy never chooses a run from an unauthenticated header. Each Basic
proxy capability is bound to one run, and browser capabilities to one session.
The CA and issued private keys live only in the world's private data directory.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from .util import canonical

HOP_HEADERS = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
}


class CertificateAuthority:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cert_path = self.directory / "ca.pem"
        self.key_path = self.directory / "ca-key.pem"
        if (
            self.cert_path.is_symlink()
            or self.key_path.is_symlink()
            or (self.key_path.exists() and self.key_path.stat().st_mode & 0o077)
        ):
            raise ValueError("World CA private files must not be symlinks or group/world readable")
        if self.cert_path.exists() != self.key_path.exists():
            raise ValueError("World CA certificate and private key must both exist")
        if self.cert_path.exists():
            self.certificate = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
            self.key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
            if self.key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            ) != self.certificate.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            ):
                raise ValueError("World CA key does not match its certificate")
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "Landing Party private world CA")]
            )
            clock = datetime.now(UTC)
            self.certificate = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(self.key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(clock - timedelta(minutes=5))
                .not_valid_after(clock + timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True,
                        key_encipherment=False,
                        key_cert_sign=True,
                        crl_sign=True,
                        content_commitment=False,
                        data_encipherment=False,
                        key_agreement=False,
                        encipher_only=False,
                        decipher_only=False,
                    ),
                    critical=True,
                )
                .sign(self.key, hashes.SHA256())
            )
            self._write_private(
                self.key_path,
                self.key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )
            self.cert_path.write_bytes(self.certificate.public_bytes(serialization.Encoding.PEM))
            self.cert_path.chmod(0o644)
        if self.certificate.not_valid_after_utc <= datetime.now(UTC):
            raise ValueError("World CA has expired")
        self._contexts = {}

    @staticmethod
    def _write_private(path: Path, data: bytes):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)

    def server_context(self, hostname: str) -> ssl.SSLContext:
        hostname = hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not hostname or len(hostname) > 253 or any(c in hostname for c in "/\\\x00"):
            raise ValueError("Invalid certificate hostname")
        if hostname in self._contexts:
            return self._contexts[hostname]
        key = ec.generate_private_key(ec.SECP256R1())
        clock = datetime.now(UTC)
        try:
            san = x509.IPAddress(ipaddress.ip_address(hostname))
        except ValueError:
            san = x509.DNSName(hostname)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname[:64])]))
            .issuer_name(self.certificate.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(clock - timedelta(minutes=5))
            .not_valid_after(min(clock + timedelta(days=7), self.certificate.not_valid_after_utc))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            .sign(self.key, hashes.SHA256())
        )
        tag = hashlib.sha256(hostname.encode()).hexdigest()
        cert_path, key_path = self.directory / (tag + ".pem"), self.directory / (tag + "-key.pem")
        if key_path.exists():
            key_path.unlink()  # Issued keys are a private ephemeral cache, never user configuration.
        self._write_private(
            key_path,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        cert_path.write_bytes(
            certificate.public_bytes(serialization.Encoding.PEM)
            + self.certificate.public_bytes(serialization.Encoding.PEM)
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert_path, key_path)
        context.set_alpn_protocols(["http/1.1"])
        self._contexts[hostname] = context
        return context


@dataclass
class Binding:
    run_id: str
    raw_enabled: bool
    session_id: str | None = None


class ProxyService:
    def __init__(self, world, *, ca: CertificateAuthority | None = None):
        self.world = world
        self.store = world.store
        self.ca = ca or CertificateAuthority(self.store.data_dir / "proxy-ca")
        self.server = None
        self.address = None
        self.bindings: dict[str, Binding] = {}
        self.connections: dict[str, set] = {}
        self.tasks: dict[str, set] = {}
        self.max_header_bytes = 65_536
        self.max_body_bytes = 5_000_000

    async def start(self, host="127.0.0.1", port=8080):
        if self.server is not None:
            raise ValueError("Proxy already started")
        self.server = await asyncio.start_server(
            self._connection, host, port, limit=self.max_header_bytes + 1
        )
        actual = self.server.sockets[0].getsockname()
        self.address = f"http://{host}:{actual[1]}"
        return {"server": self.address, "ca_path": str(self.ca.cert_path)}

    def bind_run(self, run_id: str, token: str | None = None, *, raw_enabled=False):
        self.store.get_run(run_id)
        token = token or secrets.token_urlsafe(32)
        if len(token) < 16:
            raise ValueError("Proxy capabilities need at least 16 characters")
        self.bindings[hashlib.sha256(token.encode()).hexdigest()] = Binding(
            run_id, bool(raw_enabled)
        )
        public_ca = self.prepare_run(run_id)
        return {
            "server": self.address,
            "username": run_id,
            "password": token,
            "ca_path": str(public_ca),
        }

    def prepare_run(self, run_id):
        """Publish only the public CA before preflight; issue no agent capability."""
        self.store.get_run(run_id)
        public_ca = self.store.run_dir(run_id) / "world" / "ca.pem"
        public_ca.parent.mkdir(parents=True, exist_ok=True)
        if public_ca.is_symlink():
            raise ValueError("The public CA destination must not be a symlink")
        public_ca.write_bytes(self.ca.cert_path.read_bytes())
        return public_ca

    def bind_browser(self, run_id: str, session_id: str, token: str | None = None):
        configuration = self.bind_run(run_id, token)
        hashed = hashlib.sha256(configuration["password"].encode()).hexdigest()
        self.bindings[hashed].session_id = session_id
        return configuration

    def _authenticate(self, header: str | None):
        try:
            kind, encoded = (header or "").split(" ", 1)
            if kind.lower() != "basic":
                return None, None
            username, token = base64.b64decode(encoded, validate=True).decode().split(":", 1)
            hashed = hashlib.sha256(token.encode()).hexdigest()
            binding = self.bindings.get(hashed)
            if binding is None or not hmac.compare_digest(username, binding.run_id):
                return None, None
            return binding, token
        except (ValueError, UnicodeError):
            return None, None

    async def _read(self, reader):
        data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        if len(data) > self.max_header_bytes:
            raise ValueError("Request headers exceed limit")
        lines = data.decode("iso-8859-1").split("\r\n")
        method, target, version = lines[0].split(" ")
        if version not in {"HTTP/1.0", "HTTP/1.1"} or not re.fullmatch("[A-Z]+", method):
            raise ValueError("Invalid request line")
        headers = {}
        if len(lines) > 105:
            raise ValueError("Too many headers")
        for line in lines[1:]:
            if not line:
                continue
            if line[0].isspace() or ":" not in line:
                raise ValueError("Invalid header")
            key, value = line.split(":", 1)
            key = key.lower()
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", key):
                raise ValueError("Invalid header name")
            if key in headers:
                raise ValueError("Duplicate request header")
            headers[key] = value.strip()
        if "transfer-encoding" in headers:
            raise ValueError("Chunked proxy requests are unsupported; send Content-Length")
        length = int(headers.get("content-length", "0"))
        if length < 0 or length > self.max_body_bytes:
            raise ValueError("Request body exceeds limit")
        body = await asyncio.wait_for(reader.readexactly(length), 30) if length else b""
        return method, target, headers, body

    async def _write(self, writer, status, headers, body, *, head_only=False):
        from http import HTTPStatus

        reason = (
            HTTPStatus(status).phrase if status in HTTPStatus._value2member_map_ else "Response"
        )
        pairs = list(headers.multi_items() if hasattr(headers, "multi_items") else headers.items())
        representation_only = head_only or status == 304
        connection_headers = {
            name.strip().lower()
            for key, value in pairs
            if key.lower() == "connection"
            for name in value.split(",")
        }
        cleaned = [
            (k, v)
            for k, v in pairs
            if k.lower()
            not in (HOP_HEADERS - {"proxy-authenticate"}) | {"content-length"} | connection_headers
            and (k.lower() != "content-encoding" or representation_only)
        ]
        length = (
            next(
                (
                    str(value)
                    for key, value in pairs
                    if key.lower() == "content-length" and str(value).isdigit()
                ),
                str(len(body)),
            )
            if representation_only
            else str(len(body))
        )
        if status != 204:
            cleaned.append(("Content-Length", length))
        cleaned.append(("Connection", "close"))
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in cleaned)
            + "\r\n"
        )
        writer.write(
            head.encode("iso-8859-1") + (b"" if representation_only or status == 204 else body)
        )
        await writer.drain()

    async def _deny(self, writer, status, rule, detail, kind="policy_deny"):
        await self._write(
            writer,
            status,
            {"Content-Type": "application/json"},
            canonical(
                {"kind": "blocked", "type": kind, "rule_id": rule, "detail": detail}
            ).encode(),
        )

    async def _connection(self, reader, writer):
        binding = None
        try:
            method, target, headers, body = await self._read(reader)
            binding, token = self._authenticate(headers.get("proxy-authorization"))
            if binding is None:
                await self._write(
                    writer,
                    407,
                    {"Proxy-Authenticate": 'Basic realm="Landing Party"'},
                    b"Proxy capability required",
                )
                return
            self.connections.setdefault(binding.run_id, set()).add(writer)
            self.tasks.setdefault(binding.run_id, set()).add(asyncio.current_task())
            if method == "CONNECT":
                parsed = urlsplit("//" + target)
                if (
                    not parsed.hostname
                    or parsed.port not in {443, 8443}
                    or parsed.username
                    or parsed.password
                ):
                    await self._deny(
                        writer, 403, "connect_target", "CONNECT target is not permitted"
                    )
                    return
                hostname, port = parsed.hostname, parsed.port
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await asyncio.wait_for(writer.start_tls(self.ca.server_context(hostname)), 15)
                method, target, headers, body = await self._read(reader)
                if not target.startswith("/") or target.startswith("//"):
                    await self._deny(
                        writer,
                        400,
                        "tunnel_target",
                        "HTTPS requests must use an origin-form target",
                    )
                    return
                authority_host = f"[{hostname}]" if ":" in hostname else hostname
                expected = authority_host if port == 443 else f"{authority_host}:{port}"
                if headers.get("host", "").lower() != expected.lower():
                    await self._deny(
                        writer, 403, "tunnel_authority", "HTTPS tunnel authority cannot change"
                    )
                    return
                target = f"https://{expected}{target}"
            parsed = urlsplit(target)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                await self._deny(writer, 400, "proxy_target", "Proxy requires an absolute HTTP URL")
                return
            await self._request(binding, token, writer, method, target, headers, body)
        except asyncio.CancelledError:
            raise
        except (
            ValueError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            ssl.SSLError,
            OSError,
        ):
            try:
                await self._deny(
                    writer, 400, "proxy_request", "Proxy request could not be completed"
                )
            except (OSError, ssl.SSLError):
                pass
        finally:
            if binding:
                self.connections.get(binding.run_id, set()).discard(writer)
                self.tasks.get(binding.run_id, set()).discard(asyncio.current_task())
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass

    async def _request(self, binding, token, writer, method, url, headers, body):
        run = self.store.get_run(binding.run_id)
        if run["state"] != "running":
            await self._deny(writer, 409, "run_state", "Run is not running")
            return
        authorization = None
        if binding.session_id is not None:
            browser = getattr(self.world, "services", {}).get("browser")
            if browser is None:
                await self._deny(
                    writer, 403, "browser_unavailable", "Browser proxy session is unavailable"
                )
                return
            authorization = await browser.authorize_network_request(
                binding.run_id, method, url, headers, body, token=token
            )
            authorization = {**authorization, "response_private": True, "browser_session": True}
            if not authorization.get("allow"):
                await self._deny(
                    writer,
                    403,
                    authorization.get("rule_id", "lease_required"),
                    "Browser request is outside its authorized action",
                )
                return
        elif not binding.raw_enabled:
            self.store.append_record(
                binding.run_id,
                "raw_attempt",
                method=method,
                request_hash="sha256:" + hashlib.sha256(url.encode()).hexdigest(),
                decision="raw_lane_disabled",
            )
            await self._deny(
                writer,
                403,
                "raw_lane_disabled",
                "Raw networking is disabled for this run",
                kind="raw_lane_disabled",
            )
            return
        if method not in {"GET", "HEAD", "OPTIONS"} and not (
            authorization and authorization.get("allow_write")
        ):
            self.store.append_record(
                binding.run_id, "raw_attempt", method=method, decision="write_method"
            )
            await self._deny(
                writer, 403, "write_method", "External writes require an approved scoped lease"
            )
            return
        connection_headers = {
            name.strip().lower() for name in headers.get("connection", "").split(",")
        }
        forwarded = {
            key: value
            for key, value in headers.items()
            if key not in HOP_HEADERS | {"host", "content-length"} | connection_headers
            and not key.startswith("x-world-")
        }
        if authorization is None:
            forwarded = {
                key: value
                for key, value in forwarded.items()
                if key not in {"accept-encoding", "user-agent", "cache-control", "pragma"}
            }
        self.store.append_record(
            binding.run_id,
            "raw_attempt",
            method=method,
            request_hash="sha256:" + hashlib.sha256(url.encode()).hexdigest(),
            decision="submitted",
            session_id=binding.session_id,
        )
        if hasattr(self.world, "submit_network"):
            reply = await self.world.submit_network(
                binding.run_id, method, url, forwarded, body, authorization=authorization
            )
        elif method == "GET" and not authorization:
            reply = await self.world.act(
                binding.run_id, "fetch", {"url": url, "headers": forwarded, "wait": True}
            )
        else:
            await self._deny(writer, 503, "network_scheduler", "Network scheduler is unavailable")
            return
        if reply.get("kind") != "result":
            await self._write(
                writer, 403, {"Content-Type": "application/json"}, canonical(reply).encode()
            )
            return
        handle = reply.get("handle", "")
        private_reference = reply.get("private_response_ref")
        if private_reference:
            payload = self.world.gateway.read_private_response(binding.run_id, private_reference)
        else:
            root = (self.store.run_dir(binding.run_id) / "world/fetched").resolve()
            path = (self.store.run_dir(binding.run_id) / handle.lstrip("/")).resolve()
            if not handle.startswith("/world/fetched/") or not path.is_relative_to(root):
                await self._deny(
                    writer, 500, "resource_scope", "Network response resource is invalid"
                )
                return
            payload = path.read_bytes()
        response_headers = httpx.Headers(
            {
                **reply.get("headers_subset", {}),
                "Content-Type": reply.get("content_type", "application/octet-stream"),
            }
        )
        if authorization and hasattr(self.world.gateway, "consume_response_headers"):
            response_headers.update(
                self.world.gateway.consume_response_headers(
                    binding.run_id, private_reference or handle
                )
            )
        for name in list(response_headers):
            if name.lower().startswith("x-world-"):
                del response_headers[name]
        if reply.get("response_block"):
            response_headers["X-World-Response-Block-Type"] = reply["response_block"]["type"]
        await self._write(
            writer, reply.get("status", 200), response_headers, payload, head_only=method == "HEAD"
        )

    async def rehearse(self, grant_id, action, *, run_id=None, archive_in=None):
        """A capability rehearsal is archive-only and can never fall through live."""
        from .util import digest, identifier

        run_id = run_id or action.get("run_id")
        archive_in = archive_in or action.get("archive_in")
        if not run_id or not archive_in:
            raise ValueError("A capability rehearsal requires run and source archive identities")
        run = self.store.get_run(run_id)
        source = self.store.get_run(archive_in)
        if run["state"] not in {"running", "preflight"} or source["state"] not in {
            "sealed",
            "ended",
            "killed",
        }:
            raise ValueError("Rehearsals need an open run and a sealed source archive")
        method, url = action.get("method", "GET").upper(), action["url"]
        body = action.get("body", b"")
        if isinstance(body, str):
            body = body.encode()
        elif not isinstance(body, bytes):
            body = canonical(body).encode()
        action_id = identifier("a")
        request_hash = digest(
            {
                "method": method,
                "url": url,
                "body_hash": digest(body),
                "headers_hash": digest(action.get("headers", {})),
            }
        )
        self.store.append_record(
            run_id,
            "rehearsal_action",
            grant_id=grant_id,
            action_id=action_id,
            request_hash=request_hash,
            archive_id=archive_in,
        )
        reply = await self.world.gateway.request(
            run_id,
            action_id,
            method,
            url,
            headers=action.get("headers"),
            body=body,
            mode="replay",
            archive_in=archive_in,
            trusted={
                "allow": True,
                "allow_write": True,
                "allow_credentials": True,
                "credential_body": True,
            },
        )
        passed = reply.get("kind") == "result"
        record = self.store.append_record(
            run_id,
            "rehearsal_outcome",
            grant_id=grant_id,
            action_id=action_id,
            request_hash=request_hash,
            response_hash=digest(reply),
            passed=passed,
            archive_id=archive_in,
        )
        return {
            "proxy_id": "world-http-proxy@0.1",
            "mode": "rehearsal",
            "grant_id": grant_id,
            "archive_id": archive_in,
            "request_hash": request_hash,
            "response_hash": digest(reply),
            "passed": passed,
            "record_ref": {"run_id": run_id, "seq": record["seq"], "hash": record["hash"]},
        }

    async def suspend_run(self, run_id):
        await self.stop_run(run_id, revoke=False)

    async def stop_run(self, run_id, *, revoke=True):
        current = asyncio.current_task()
        tasks = [task for task in self.tasks.get(run_id, set()) if task is not current]
        for writer in list(self.connections.get(run_id, set())):
            writer.close()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if hasattr(self.world.gateway, "clear_run_secrets"):
            self.world.gateway.clear_run_secrets(run_id)
        for hashed, binding in list(self.bindings.items()):
            if revoke and binding.run_id == run_id:
                del self.bindings[hashed]

    async def recover(self):
        return None

    async def aclose(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for run_id in list(self.tasks):
            await self.stop_run(run_id)
