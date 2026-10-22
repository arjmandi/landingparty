"""Local OpenAI face which injects the run capability without saving it to disk."""

from __future__ import annotations

import argparse
import hmac
import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def create_proxy_app(lane_url, run_id, run_token, *, client=None):
    if not run_id or not run_token:
        raise ValueError("A run identity and scoped token are required")
    owned = client is None
    client = client or httpx.AsyncClient(timeout=180, follow_redirects=False, trust_env=False)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owned:
            await client.aclose()

    app = FastAPI(title="OpenHands run-header bridge", lifespan=lifespan)

    @app.post("/v1/{endpoint:path}")
    async def forward(endpoint: str, request: Request):
        if endpoint not in {"chat/completions", "responses"}:
            return JSONResponse({"error": "Unsupported model endpoint"}, status_code=404)
        credential = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(credential, run_token):
            return JSONResponse({"error": "Run capability required"}, status_code=401)
        upstream_request = client.build_request(
            "POST",
            lane_url.rstrip("/") + "/" + endpoint,
            content=await request.body(),
            headers={
                "content-type": "application/json",
                "X-World-Run-Id": run_id,
                "X-World-Run-Token": run_token,
            },
        )
        upstream = await client.send(upstream_request, stream=True)

        async def content():
            try:
                async for block in upstream.aiter_bytes():
                    yield block
            finally:
                await upstream.aclose()

        return StreamingResponse(
            content(),
            status_code=upstream.status_code,
            headers={
                name: upstream.headers[name]
                for name in ("content-type", "request-id", "x-request-id")
                if name in upstream.headers
            },
        )

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane-url", default="http://127.0.0.1:8081/openai/v1")
    parser.add_argument("--port", type=int, default=18082)
    args = parser.parse_args()
    run_id, token = os.getenv("WORLD_RUN_ID"), os.getenv("WORLD_RUN_TOKEN")
    if not run_id or not token:
        parser.error("WORLD_RUN_ID and WORLD_RUN_TOKEN are required")
    uvicorn.run(create_proxy_app(args.lane_url, run_id, token), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
