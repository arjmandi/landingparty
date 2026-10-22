"""Start the control plane and, optionally, a second model-lane listener."""

import argparse
import asyncio

import uvicorn
from fastapi import FastAPI

from .app import create_app
from .security import create_agent_app


def main():
    parser = argparse.ArgumentParser(description="Landing Party world process")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument(
        "--agent-port",
        type=int,
        default=7778,
        help="Agent-only authenticated tool listener; 0 disables it.",
    )
    parser.add_argument(
        "--proxy-port", type=int, default=8080, help="Recorded HTTP/HTTPS proxy; 0 disables it."
    )
    parser.add_argument(
        "--lane-port",
        type=int,
        default=8081,
        help="Separate model listener; 0 disables it. Lane is also on control port.",
    )
    args = parser.parse_args()
    ports = [port for port in [args.port, args.lane_port, args.agent_port, args.proxy_port] if port]
    if len(ports) != len(set(ports)):
        parser.error("listener ports must differ (0 disables an optional listener)")
    app = create_app(args.data_dir)
    if not args.lane_port and not args.agent_port and not args.proxy_port:
        uvicorn.run(app, host=args.host, port=args.port)
        return

    async def serve():
        if args.proxy_port:
            proxy = app.state.world.services.get("proxy")
            if proxy is None:
                raise RuntimeError("Proxy service dependencies are unavailable")
            await proxy.start(args.host, args.proxy_port)
        listeners = [(app, args.port)]
        if args.lane_port:
            lane = FastAPI(title="Landing Party model lane")
            lane.state.require_run_token = True
            lane.include_router(app.state.lane_router)
            listeners.append((lane, args.lane_port))
        if args.agent_port:
            listeners.append((create_agent_app(app.state.world), args.agent_port))
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


if __name__ == "__main__":
    main()
