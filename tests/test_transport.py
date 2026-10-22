import asyncio

import pytest

from worldd.gateway import Gateway
from worldd.proxy import CertificateAuthority


@pytest.mark.parametrize("proxy_scheme", [None, "http", "https"])
@pytest.mark.parametrize("wrong_certificate", [False, True])
async def test_pinned_tls_verifies_original_hostname_through_real_upstream_proxy(
    tmp_path, monkeypatch, proxy_scheme, wrong_certificate
):
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(key, raising=False)
    ca = CertificateAuthority(tmp_path / "ca")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca.cert_path))
    names, tunnels, active = [], [], set()
    contexts = {
        name: ca.server_context("wrong.test" if wrong_certificate else name)
        for name in ("site-a.test", "site-b.test")
    }

    def select_hostname(connection, name, context):
        names.append(name)
        if name in contexts:
            connection.context = contexts[name]

    contexts["site-a.test"].set_servername_callback(select_hostname)

    async def origin(reader, writer):
        try:
            while True:
                request = await reader.readuntil(b"\r\n\r\n")
                body = b"User-agent: *\nAllow: /\n" if b"/robots.txt " in request else request
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: keep-alive\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(origin, "127.0.0.1", 0, ssl=contexts["site-a.test"])
    port = server.sockets[0].getsockname()[1]

    async def relay(reader, writer):
        task = asyncio.current_task()
        active.add(task)
        upstream = None
        copies = []
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            authority = header.split(b"\r\n", 1)[0].split()[1]
            tunnels.append(authority.decode())
            assert authority == f"127.0.0.1:{port}".encode()  # DNS never delegated to proxy.
            incoming, upstream = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            copies = [
                asyncio.create_task(copy(reader, upstream)),
                asyncio.create_task(copy(incoming, writer)),
            ]
            await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for copy_task in copies:
                copy_task.cancel()
            await asyncio.gather(*copies, return_exceptions=True)
            writer.close()
            if upstream:
                upstream.close()
            active.discard(task)

    proxy = None
    if proxy_scheme:
        proxy = await asyncio.start_server(
            relay,
            "127.0.0.1",
            0,
            ssl=ca.server_context("127.0.0.1") if proxy_scheme == "https" else None,
        )
        proxy_port = proxy.sockets[0].getsockname()[1]
        monkeypatch.setenv("HTTPS_PROXY", f"{proxy_scheme}://127.0.0.1:{proxy_port}")

    async def resolver(host, port):
        return ["127.0.0.1"]

    gateway = Gateway(
        tmp_path,
        {"test_allowed_hosts": ["site-a.test", "site-b.test"], "domain_interval_s": 0},
        resolver=resolver,
    )
    try:
        for index, name in enumerate(("site-a.test", "site-b.test")):
            reply = await gateway.fetch("r_1", f"a_{index}", f"https://{name}:{port}/page")
            if wrong_certificate:
                assert reply["kind"] == "blocked" and reply["rule_id"] == "robots_unavailable"
            else:
                assert reply["kind"] == "result", reply
                assert f"Host: {name}:{port}".lower() in reply["preview"].lower()
        assert names == (
            ["site-a.test", "site-b.test"]
            if wrong_certificate
            else ["site-a.test", "site-a.test", "site-b.test", "site-b.test"]
        )
        assert len(tunnels) == ((2 if wrong_certificate else 4) if proxy_scheme else 0)
    finally:
        await gateway.aclose()
        server.close()
        await server.wait_closed()
        if proxy:
            proxy.close()
            await proxy.wait_closed()
        for task in list(active):
            task.cancel()
        await asyncio.gather(*list(active), return_exceptions=True)
