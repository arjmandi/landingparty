"""Start one browser origin, or opt in to the original separate listeners."""

import argparse
import asyncio
import ipaddress
import os

import uvicorn
from fastapi import FastAPI

from .app import create_app
from .security import create_agent_app
from .studio_gateway import create_studio_gateway


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description="Landing Party world process")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument(
        "--legacy-listeners", action="store_true", help="Use the original separate HTTP listeners."
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Enable the optional /public boundary on the studio origin.",
    )
    parser.add_argument(
        "--no-proxy", action="store_true", help="Disable the internal recorded TCP proxy."
    )
    parser.add_argument(
        "--proxy-host",
        default=None,
        help="Internal recorded proxy bind (studio default:127.0.0.1); never publish this port as the browser frontend.",
    )
    parser.add_argument(
        "--agent-port",
        type=int,
        default=None,
        help="Legacy agent listener; specifying this opts into legacy listeners. 0 disables it.",
    )
    parser.add_argument(
        "--proxy-port",
        type=int,
        default=None,
        help="Recorded proxy TCP port; specifying this preserves legacy listener behavior. 0 disables it.",
    )
    parser.add_argument(
        "--public-port",
        type=int,
        default=None,
        help="Legacy public listener; specifying this opts into legacy listeners. 0 disables it.",
    )
    parser.add_argument(
        "--lane-port",
        type=int,
        default=None,
        help="Legacy model listener; specifying this opts into legacy listeners. 0 disables it.",
    )
    args = parser.parse_args(argv)
    if args.no_proxy and args.proxy_port is not None:
        parser.error("--no-proxy and --proxy-port cannot be combined")
    old_flags = any(
        getattr(args, name) is not None
        for name in ("agent_port", "proxy_port", "public_port", "lane_port")
    )
    args.legacy_listeners = args.legacy_listeners or old_flags
    defaults = {
        "agent_port": 7778 if args.legacy_listeners else 0,
        "lane_port": 8081 if args.legacy_listeners else 0,
        "proxy_port": 8080,
        "public_port": 0,
    }
    for name, value in defaults.items():
        if getattr(args, name) is None:
            setattr(args, name, value)
    if args.no_proxy:
        args.proxy_port = 0
    if args.proxy_host is None:
        args.proxy_host = args.host if args.legacy_listeners else "127.0.0.1"
    if args.public and args.legacy_listeners:
        parser.error("--public is a studio mount; use --public-port with legacy listeners")
    ports = [
        port
        for port in [args.port, args.lane_port, args.agent_port, args.proxy_port, args.public_port]
        if port
    ]
    if any(not 1 <= port <= 65535 for port in ports) or args.port == 0:
        parser.error("HTTP port must be 1..65535; optional listener ports may also be 0")
    if len(ports) != len(set(ports)):
        parser.error("listener ports must differ (0 disables an optional listener)")
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host.lower() == "localhost"
    if not loopback and not os.environ.get("WORLD_CONTROL_TOKEN"):
        parser.error(
            "Binding beyond loopback requires WORLD_CONTROL_TOKEN for the instance operator"
        )
    return args


def main():
    args = _arguments()
    control = create_app(args.data_dir)
    _configure_studio(control, args)
    app = control if args.legacy_listeners else create_studio_gateway(control, public=args.public)
    if not args.lane_port and not args.agent_port and not args.proxy_port and not args.public_port:
        uvicorn.run(app, host=args.host, port=args.port)
        return

    async def serve():
        if args.proxy_port:
            proxy = control.state.world.services.get("proxy")
            if proxy is None:
                raise RuntimeError("Proxy service dependencies are unavailable")
            # The studio's extra TCP service is internal, not a second browser front end.
            await proxy.start(args.proxy_host, args.proxy_port)
        listeners = [(app, args.port)]
        if args.lane_port:
            lane = FastAPI(title="Landing Party model lane")
            lane.state.require_run_token = True
            lane.include_router(control.state.lane_router)
            listeners.append((lane, args.lane_port))
        if args.agent_port:
            listeners.append((create_agent_app(control.state.world), args.agent_port))
        if args.public_port:
            from .public_identity import create_public_app

            listeners.append((create_public_app(control.state.world), args.public_port))
        servers = [
            uvicorn.Server(uvicorn.Config(listener, host=args.host, port=port))
            for listener, port in listeners
        ]
        tasks = [asyncio.create_task(server.serve()) for server in servers]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for server in servers:
                server.should_exit = True
            await asyncio.gather(*tasks)

    asyncio.run(serve())


def _configure_studio(control, args):
    studio = getattr(control.state, "studio", None)
    if studio is None:
        return
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(args.host, args.host)
    address = f"[{host}]" if ":" in host else host
    origin = f"http://{address}:{args.port}"
    if args.legacy_listeners:
        studio.configure_urls(
            origin,
            agent_url=f"http://{address}:{args.agent_port}" if args.agent_port else origin,
            lane_url=f"http://{address}:{args.lane_port}" if args.lane_port else origin,
        )
    else:
        studio.configure_urls(origin)


if __name__ == "__main__":
    main()
