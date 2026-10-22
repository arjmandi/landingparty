"""Pinned-IP TLS with hostname verification, including upstream CONNECT proxies.

httpcore's CONNECT transport currently ignores the sni_hostname extension. Wrap
its network stream rather than change the pinned CONNECT destination. The frozen
HTTPX/httpcore versions and real socket regressions cover this small adapter.
"""

from __future__ import annotations

from contextvars import ContextVar

import httpcore
import httpx
from httpx._utils import get_environment_proxies

_target = ContextVar("gateway_tls_target", default=None)


class _Stream(httpcore.AsyncNetworkStream):
    def __init__(self, stream, *, proxy_tls_hostname=None):
        self.stream = stream
        self.proxy_tls_hostname = proxy_tls_hostname

    async def read(self, max_bytes, timeout=None):
        return await self.stream.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        await self.stream.write(buffer, timeout)

    async def aclose(self):
        await self.stream.aclose()

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        target = _target.get()
        if self.proxy_tls_hostname:
            server_hostname = self.proxy_tls_hostname
        elif target and server_hostname == target[0]:
            server_hostname = target[1]
        upgraded = await self.stream.start_tls(ssl_context, server_hostname, timeout)
        return _Stream(upgraded)


class _Backend(httpcore.AsyncNetworkBackend):
    def __init__(self, backend, *, proxy_tls_hostname=None):
        self.backend = backend
        self.proxy_tls_hostname = proxy_tls_hostname

    async def connect_tcp(self, *args, **kwargs):
        stream = await self.backend.connect_tcp(*args, **kwargs)
        return _Stream(stream, proxy_tls_hostname=self.proxy_tls_hostname)

    async def connect_unix_socket(self, *args, **kwargs):
        stream = await self.backend.connect_unix_socket(*args, **kwargs)
        return _Stream(stream, proxy_tls_hostname=self.proxy_tls_hostname)

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


class PinnedSNITransport(httpx.AsyncHTTPTransport):
    def __init__(self, *, proxy=None, **kwargs):
        # Pinned URLs share an IP origin across hostnames. Prevent idle TLS reuse
        # across them, including an upstream proxy's CONNECT pool.
        kwargs["limits"] = httpx.Limits(max_connections=100, max_keepalive_connections=0)
        kwargs["http2"] = False
        super().__init__(proxy=proxy, **kwargs)
        proxy_url = (
            proxy.url if isinstance(proxy, httpx.Proxy) else httpx.URL(proxy) if proxy else None
        )
        self._pool._network_backend = _Backend(
            self._pool._network_backend,
            proxy_tls_hostname=proxy_url.host
            if proxy_url and proxy_url.scheme == "https"
            else None,
        )

    async def handle_async_request(self, request):
        hostname = request.extensions.get("sni_hostname")
        token = _target.set((request.url.host, hostname) if hostname else None)
        try:
            return await super().handle_async_request(request)
        finally:
            _target.reset(token)


def pinned_client(*, timeout):
    mounts = {
        pattern: PinnedSNITransport(proxy=proxy) if proxy else None
        for pattern, proxy in get_environment_proxies().items()
    }
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        transport=PinnedSNITransport(),
        mounts=mounts,
        trust_env=True,
    )
