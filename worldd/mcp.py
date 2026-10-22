"""A small MCP stdio face; all world behavior remains in the HTTP core.

MCP stdio uses one UTF-8 JSON-RPC message per line. Diagnostics go to stderr;
stdout is reserved exclusively for protocol messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx

from worldd import __version__

SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
READ_TOOLS = frozenset(
    {
        "search",
        "fetch",
        "fetch_many",
        "extract",
        "status",
        "inbox",
        "wait",
        "beliefs",
        "footprint",
        "identity",
        "market_status",
        "bounties",
    }
)


def manifest_tools(manifest: Any) -> list[dict[str, Any]]:
    """Normalize a public manifest, keeping its actual registered schemas."""
    if isinstance(manifest, dict):
        manifest = manifest.get("manifest", manifest)
    if isinstance(manifest, dict):
        manifest = manifest.get("tools", [])
    if not isinstance(manifest, list):
        raise TypeError("world manifest must contain a tools list")
    tools = []
    for item in manifest:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise TypeError("world manifest contains an invalid tool")
        tool = {
            "name": item["name"],
            "description": item.get("description", ""),
            "inputSchema": item.get(
                "inputSchema", item.get("parameters", {"type": "object", "properties": {}})
            ),
        }
        if isinstance(item.get("annotations"), dict):
            tool["annotations"] = dict(item["annotations"])
        elif item["name"] in READ_TOOLS:
            tool["annotations"] = {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": item["name"] in {"search", "fetch", "fetch_many"},
            }
        tools.append(tool)
    return tools


class MCPBridge:
    def __init__(self, client: httpx.AsyncClient, run_id: str, token: str | None = None):
        self.client = client
        self.run_id = run_id
        self.headers = {"Authorization": "Bearer " + token} if token else {}
        self.initialized = False

    async def _get(self, path: str, **params: Any) -> Any:
        response = await self.client.get(
            path, params={"run_id": self.run_id, **params}, headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    async def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid Request")
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            return self._error(request_id, -32600, "Invalid Request")
        if "id" not in message:
            if method == "notifications/initialized":
                self.initialized = True
            return None
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                protocol = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
                result = {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "world", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": manifest_tools(await self._get("/v1/manifest"))}
            elif method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments", {})
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    return self._error(
                        request_id, -32602, "Expected tool name and arguments object"
                    )
                response = await self.client.post(
                    "/v1/act",
                    json={"run_id": self.run_id, "tool": name, "params": arguments},
                    headers=self.headers,
                )
                response.raise_for_status()
                result = {
                    "content": [{"type": "text", "text": json.dumps(response.json())}],
                    "isError": False,
                }
            elif method == "resources/list":
                result = {
                    "resources": [
                        {
                            "uri": f"world://inbox/{self.run_id}",
                            "name": "inbox",
                            "description": "Recorded inbox events; reading does not consume events.",
                            "mimeType": "application/json",
                        }
                    ]
                }
            elif method == "resources/read":
                uri = params.get("uri")
                if uri != f"world://inbox/{self.run_id}":
                    return self._error(request_id, -32602, "Unknown resource URI")
                data = await self._get("/v1/inbox", since=0, limit=50)
                result = {
                    "contents": [
                        {"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}
                    ]
                }
            else:
                return self._error(request_id, -32601, f"Method not found: {method}")
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            detail = f"World request failed: {type(exc).__name__}"
            if isinstance(exc, httpx.HTTPStatusError):
                detail += f" (HTTP {exc.response.status_code})"
            if method == "tools/call":
                result = {"content": [{"type": "text", "text": detail}], "isError": True}
            else:
                return self._error(request_id, -32603, detail)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


async def serve(url: str, run_id: str, token: str | None = None) -> None:
    async with httpx.AsyncClient(base_url=url, timeout=65, trust_env=False) as client:
        bridge = MCPBridge(client, run_id, token)
        pending: set[asyncio.Task] = set()

        async def dispatch(line: str) -> None:
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                reply = MCPBridge._error(None, -32700, "Parse error")
            else:
                reply = await bridge.handle(request)
            if reply is not None:
                # No await between write and flush: parallel calls cannot interleave JSON lines.
                sys.stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
                sys.stdout.flush()

        while line := await asyncio.to_thread(sys.stdin.readline):
            if not line.strip():
                continue
            task = asyncio.create_task(dispatch(line))
            pending.add(task)
            task.add_done_callback(pending.discard)
        if pending:
            await asyncio.gather(*pending)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Landing Party MCP stdio bridge")
    parser.add_argument("--url", default=os.environ.get("WORLD_BASE_URL", "http://127.0.0.1:7778"))
    parser.add_argument("--run-id", default=os.environ.get("WORLD_RUN_ID"))
    args = parser.parse_args(argv)
    if not args.run_id:
        parser.error("--run-id or WORLD_RUN_ID is required")
    try:
        asyncio.run(serve(args.url, args.run_id, os.environ.get("WORLD_RUN_TOKEN")))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
