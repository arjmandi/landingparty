#!/usr/bin/env python3
"""Run Claude Code with an existing world binding, without shell interpolation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def command(binding_dir: Path, briefing: str, goal: str = "") -> list[str]:
    return [
        "claude",
        "-p",
        "\n\n".join(part for part in (briefing, goal) if part),
        "--tools",
        "Bash,Read,Write,Edit,Glob,Grep",
        "--strict-mcp-config",
        "--mcp-config",
        str(binding_dir / "world.mcp.json"),
        "--settings",
        str(binding_dir / "world.settings.json"),
        "--permission-mode",
        "dontAsk",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def runtime_environment(binding_dir: Path, source=None):
    environment = dict(os.environ if source is None else source)
    settings = json.loads((binding_dir / "world.settings.json").read_text())
    run_id = settings["env"]["WORLD_RUN_ID"]
    token = environment.get("WORLD_RUN_TOKEN")
    headers = f"X-World-Run-Id: {run_id}"
    if token:
        headers += "\nX-World-Run-Token: " + token
        environment["ANTHROPIC_AUTH_TOKEN"] = token
    else:
        environment.pop("ANTHROPIC_AUTH_TOKEN", None)
    environment["ANTHROPIC_CUSTOM_HEADERS"] = headers
    for name in (
        "WORLD_CONTROL_TOKEN",
        "WORLD_OPENAI_API_KEY",
        "WORLD_ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        environment.pop(name, None)
    return environment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binding_dir", type=Path)
    parser.add_argument("--briefing", type=Path, required=True)
    parser.add_argument("--goal", type=Path)
    parser.add_argument("--transcript", type=Path, required=True)
    args = parser.parse_args(argv)
    for required in ("world.mcp.json", "world.settings.json"):
        if not (args.binding_dir / required).is_file():
            parser.error(f"missing binding file: {args.binding_dir / required}")
    argv = command(
        args.binding_dir, args.briefing.read_text(), args.goal.read_text() if args.goal else ""
    )
    with args.transcript.open("xb") as transcript:
        return subprocess.run(
            argv, stdout=transcript, env=runtime_environment(args.binding_dir), check=False
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
