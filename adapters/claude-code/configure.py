#!/usr/bin/env python3
"""Generate a Claude Code cooperative binding without copying credentials."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def binding(
    run_id: str,
    *,
    world_url: str = "http://127.0.0.1:7778",
    lane_url: str = "http://127.0.0.1:8081/anthropic",
    gateway_url: str | None = None,
    ca_path: str | None = None,
    python: str | None = None,
) -> dict[str, dict[str, Any]]:
    if not re.fullmatch(r"r_[A-Za-z0-9_-]+", run_id):
        raise ValueError("run_id must be a run identifier, not an arbitrary header value")
    if bool(gateway_url) != bool(ca_path):
        raise ValueError("gateway URL and its CA path must be supplied together")
    env = {
        "WORLD_RUN_ID": run_id,
        "WORLD_BASE_URL": world_url,
        "ANTHROPIC_BASE_URL": lane_url.rstrip("/"),
    }
    if gateway_url and ca_path:
        env.update(HTTPS_PROXY=gateway_url, HTTP_PROXY=gateway_url, SSL_CERT_FILE=ca_path)
        # These values are routing configuration; credential environment values are never copied.
    return {
        "world.mcp.json": {
            "mcpServers": {
                "world": {
                    "command": python or sys.executable,
                    "args": ["-m", "worldd.mcp"],
                    "env": {"WORLD_RUN_ID": run_id, "WORLD_BASE_URL": world_url},
                }
            }
        },
        "world.settings.json": {
            "permissions": {
                "deny": ["WebSearch", "WebFetch"],
                "allow": ["mcp__world__*", "Bash", "Read", "Write", "Edit", "Glob", "Grep"],
            },
            "env": env,
        },
    }


def write_binding(directory: Path, files: dict[str, dict[str, Any]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / name for name in files]
    if any(path.exists() for path in paths):
        raise FileExistsError("binding files already exist; use a fresh output directory")
    created = []
    try:
        for name, content in files.items():
            path = directory / name
            with path.open("x") as stream:
                created.append(path)
                stream.write(json.dumps(content, indent=2) + "\n")
    except Exception:
        # Only files exclusively created by this call may be removed.
        for path in created:
            path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--world-url", default="http://127.0.0.1:7778")
    parser.add_argument("--lane-url", default="http://127.0.0.1:8081/anthropic")
    parser.add_argument("--gateway-url")
    parser.add_argument("--ca-path")
    args = parser.parse_args(argv)
    write_binding(
        args.output,
        binding(
            args.run_id,
            world_url=args.world_url,
            lane_url=args.lane_url,
            gateway_url=args.gateway_url,
            ca_path=args.ca_path,
        ),
    )
    print(f"Wrote {args.output / 'world.mcp.json'} and {args.output / 'world.settings.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
