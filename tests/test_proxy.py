import asyncio
import json
import ssl
from pathlib import Path

import httpx
import pytest
from cryptography import x509

from worldd.gateway import Gateway
from worldd.proxy import CertificateAuthority, ProxyService
from worldd.signing import WorldSigner
from worldd.storage import Store
from worldd.util import identifier


class ProxyWorld:
    def __init__(self, root, handler):
        self.store = Store(root)
        self.run_id = self.store.create_run({"budget": {"usd": 1}, "mode": "live"})["run_id"]
        self.services = {}

        async def resolver(host, port):
            return ["93.184.216.34"]

        self.gateway = Gateway(
            root,
            {"domain_interval_s": 0, "domain_burst": 100, "global_requests_per_minute": 1000},
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=resolver,
        )
        self.submitted = []

    async def submit_network(self, run_id, method, url, headers, body, authorization=None):
        self.submitted.append((run_id, method, url, headers, body, authorization))
        action_id = identifier("a")
        return await self.gateway.request(
            run_id, action_id, method, url, headers=headers, body=body, trusted=authorization
        )


def handler(request):
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    return httpx.Response(200, text="World boundary works", headers={"content-type": "text/plain"})


async def proxy_fixture(tmp_path, handler=handler, *, raw=True):
    world = ProxyWorld(tmp_path, handler)
    proxy = ProxyService(world)
    await proxy.start(port=0)
    config = proxy.bind_run(world.run_id, raw_enabled=raw)
    return world, proxy, config


def client_for(config):
    trust = ssl.create_default_context(cafile=config["ca_path"])
    return httpx.AsyncClient(
        proxy=httpx.Proxy(config["server"], auth=(config["username"], config["password"])),
        verify=trust,
        trust_env=False,
        timeout=3,
    )


async def test_real_http_and_https_proxy_ca_trust(tmp_path):
    world, proxy, config = await proxy_fixture(tmp_path)
    try:
        async with client_for(config) as client:
            plain = await client.get("http://example.com/evidence")
            secure = await client.get("https://example.com/evidence")
        assert plain.status_code == secure.status_code == 200
        assert plain.text == secure.text == "World boundary works"
        assert len(world.submitted) == 2
        assert all("proxy-authorization" not in call[3] for call in world.submitted)
        assert Path(config["ca_path"]).read_bytes() == proxy.ca.cert_path.read_bytes()
        records = world.store.records(world.run_id)
        assert sum(record["kind"] == "raw_attempt" for record in records) == 2
    finally:
        await proxy.aclose()
        world.store.close()


async def test_https_without_world_ca_rejects_interception(tmp_path):
    world, proxy, config = await proxy_fixture(tmp_path)
    try:
        async with httpx.AsyncClient(
            proxy=httpx.Proxy(config["server"], auth=(config["username"], config["password"])),
            trust_env=False,
            timeout=3,
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://example.com/evidence")
        assert not world.submitted
    finally:
        await proxy.aclose()
        world.store.close()


async def test_no_proxy_capability_rejected(tmp_path):
    world, proxy, config = await proxy_fixture(tmp_path)
    try:
        async with httpx.AsyncClient(proxy=config["server"], trust_env=False) as client:
            response = await client.get("http://example.com/")
        assert response.status_code == 407
        assert response.headers["proxy-authenticate"].startswith("Basic")
        assert not world.submitted
    finally:
        await proxy.aclose()
        world.store.close()


async def test_raw_disabled_and_write_method_denied(tmp_path):
    world, proxy, config = await proxy_fixture(tmp_path, raw=False)
    try:
        async with client_for(config) as client:
            response = await client.get("http://example.com/")
        assert response.json()["type"] == "raw_lane_disabled"
        enabled = proxy.bind_run(world.run_id, raw_enabled=True)
        async with client_for(enabled) as client:
            response = await client.post("https://example.com/post", json={"message": "no"})
        assert response.json()["rule_id"] == "write_method"
        assert not world.submitted
    finally:
        await proxy.aclose()
        world.store.close()


async def test_tunnel_cannot_change_authority(tmp_path):
    world, proxy, config = await proxy_fixture(tmp_path)
    try:
        async with client_for(config) as client:
            response = await client.get("https://example.com/", headers={"host": "169.254.169.254"})
        assert response.status_code == 403
        assert response.json()["rule_id"] == "tunnel_authority"
        assert not world.submitted
    finally:
        await proxy.aclose()
        world.store.close()


async def test_proxy_kill_cancels_socket_and_revokes_capability(tmp_path):
    started = asyncio.Event()

    async def delayed(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        started.set()
        await asyncio.Event().wait()

    world, proxy, config = await proxy_fixture(tmp_path, delayed)
    try:
        async with client_for(config) as client:
            request = asyncio.create_task(client.get("https://example.com/slow"))
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(proxy.stop_run(world.run_id), 1)
            with pytest.raises(httpx.HTTPError):
                await request
        assert not proxy.connections[world.run_id]
        assert not proxy.tasks[world.run_id]
        assert not proxy.bindings
    finally:
        await proxy.aclose()
        world.store.close()


async def test_browser_proxy_checks_scoped_authorization_and_private_cookie_channel(tmp_path):
    outgoing = []

    def upstream(request):
        outgoing.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text="accepted", headers={"set-cookie": "session=world-private; Secure; HttpOnly"}
        )

    world, proxy, _ = await proxy_fixture(tmp_path, upstream)

    class Browser:
        async def authorize_network_request(self, run_id, method, url, headers, body, token=None):
            assert token is not None
            return {
                "allow": url.endswith("/approved"),
                "allow_write": True,
                "allow_credentials": True,
                "rule_id": "lease_scope",
            }

    world.services["browser"] = Browser()
    config = proxy.bind_browser(world.run_id, "s_1")
    try:
        async with client_for(config) as client:
            denied = await client.post("https://example.com/other", content=b"hello")
            accepted = await client.post(
                "https://example.com/approved",
                headers={"authorization": "Bearer vault-managed-value"},
                content=b"hello",
            )
        assert denied.status_code == 403 and denied.json()["rule_id"] == "lease_scope"
        assert accepted.status_code == 200
        assert accepted.headers["set-cookie"].startswith("session=world-private")
        assert outgoing[-1].method == "POST"
        assert outgoing[-1].headers["authorization"] == "Bearer vault-managed-value"
        archives = (tmp_path / "runs" / world.run_id / "archive.har").read_text()
        assert "world-private" not in archives and "vault-managed-value" not in archives
    finally:
        await proxy.aclose()
        world.store.close()


async def test_browser_proxy_preserves_challenges_and_repeated_cookies_with_trusted_marker(
    tmp_path,
):
    def upstream(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            403,
            text='<html>captcha<input type="password"></html>',
            headers=[
                ("content-type", "text/html"),
                ("set-cookie", "first=private-one; Secure"),
                ("set-cookie", "second=private-two; Secure"),
                ("x-world-response-block-type", "spoofed-upstream-marker"),
                ("connection", "x-remove-me"),
                ("x-remove-me", "hop-only"),
            ],
        )

    world, proxy, _ = await proxy_fixture(tmp_path, upstream)

    class Browser:
        async def authorize_network_request(self, *args, **kwargs):
            return {"allow": True}

    world.services["browser"] = Browser()
    binding = proxy.bind_browser(world.run_id, "s_1")
    try:
        async with client_for(binding) as client:
            response = await client.get("https://example.com/challenge")
        assert response.status_code == 403 and "captcha" in response.text
        assert response.headers["x-world-response-block-type"] == "captcha"
        assert response.headers.get_list("set-cookie") == [
            "first=private-one; Secure",
            "second=private-two; Secure",
        ]
        assert "x-remove-me" not in response.headers
        assert not list((tmp_path / "runs" / world.run_id / "world/fetched").glob("*"))
        assert "private-one" not in (tmp_path / "runs" / world.run_id / "archive.har").read_text()
    finally:
        await proxy.aclose()
        world.store.close()


def test_ca_persistence_and_private_key_permissions(tmp_path):
    first = CertificateAuthority(tmp_path / "ca")
    second = CertificateAuthority(tmp_path / "ca")
    assert first.cert_path.read_bytes() == second.cert_path.read_bytes()
    assert first.key_path.stat().st_mode & 0o777 == 0o600
    context = first.server_context("example.com")
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    certs = list((tmp_path / "ca").glob("*.pem"))
    assert len(certs) == 4
    certificate = x509.load_pem_x509_certificate(first.cert_path.read_bytes())
    assert certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_http_message_signature_is_deterministic_valid_and_expires(tmp_path):
    signer = WorldSigner(
        tmp_path, "https://research.example/.well-known/http-message-signatures-directory"
    )
    headers = signer.sign("GET", "https://example.com/evidence", timestamp=1000)
    assert signer.verify("GET", "https://example.com/evidence", headers, timestamp=1020)
    assert not signer.verify("POST", "https://example.com/evidence", headers, timestamp=1020)
    assert not signer.verify("GET", "https://example.com/evidence", headers, timestamp=1061)
    assert WorldSigner(tmp_path, signer.identity_url).key_id == signer.key_id
    serialized = json.dumps(signer.directory())
    assert "PRIVATE" not in serialized and "Ed25519" in serialized
    assert (tmp_path / "keys/http-signing.pem").stat().st_mode & 0o777 == 0o600


async def test_capability_rehearsal_is_archive_only_with_auditable_receipt(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request)
        return handler(request)

    world, proxy, _ = await proxy_fixture(tmp_path, upstream)
    source = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    await world.gateway.fetch(source, "a_source", "https://example.com/evidence")
    world.store.update_run(source, state="sealed")
    before = len(calls)
    receipt = await proxy.rehearse(
        "g_1", {"url": "https://example.com/evidence"}, run_id=world.run_id, archive_in=source
    )
    assert receipt["passed"] and receipt["mode"] == "rehearsal"
    assert len(calls) == before
    reference = receipt["record_ref"]
    record = next(
        item for item in world.store.records(world.run_id) if item["seq"] == reference["seq"]
    )
    assert record["hash"] == reference["hash"] and record["kind"] == "rehearsal_outcome"
    missing = await proxy.rehearse(
        "g_2", {"url": "https://example.com/missing"}, run_id=world.run_id, archive_in=source
    )
    assert not missing["passed"] and len(calls) == before
    await proxy.aclose()
    world.store.close()
