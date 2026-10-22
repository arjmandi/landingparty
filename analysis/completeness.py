"""Post-run transcript parsing and conservative cooperative-boundary audits."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path

_NETWORK = re.compile(
    r"\b(curl|wget|httpx|requests|urlopen|urllib|socket|nc|netcat|ssh|scp|ftp|telnet)\b"
    r"|\bgit\s+(?:clone|fetch|pull|push|ls-remote)\b"
    r"|\b(?:pip|npm|pnpm|yarn|uv)\s+(?:install|add|sync|pip)\b"
    r"|/dev/(?:tcp|udp)/",
    re.IGNORECASE,
)
_BYPASS = re.compile(
    r"--noproxy|trust_env\s*=\s*False|ProxyHandler\s*\(\s*\{\s*\}\s*\)"
    r"|(?:unset|env\s+-u)\s+(?:HTTPS?_PROXY|ALL_PROXY)"
    r"|(?:HTTPS?_PROXY|ALL_PROXY)\s*=\s*['\"]?\s*(?:['\"]|\s|$)",
    re.IGNORECASE,
)


def parse_transcript(source: str | Path | Iterable[dict]) -> dict:
    """Parse Claude stream JSONL or normalized model/tool events.

    Repeated chunks from one assistant message count once by message id. Shell
    command bodies are deliberately omitted from this audit's output: commands
    can contain credentials. Candidates contain only hashes and classifications.
    """
    if isinstance(source, Path):
        source = source.read_text(encoding="utf-8")
    malformed = 0
    if isinstance(source, str):
        events = []
        for line in source.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError("event must be an object")
                events.append(value)
            except (json.JSONDecodeError, TypeError):
                malformed += 1
    else:
        events = list(source)
        if not all(isinstance(event, dict) for event in events):
            raise TypeError("transcript events must be objects")
    message_ids = set()
    unidentified = 0
    shell_hashes = set()
    candidates = []
    compactions = 0

    def inspect_tool(tool):
        if tool.get("name") not in {"Bash", "bash", "shell", "exec_command"}:
            return
        arguments = tool.get("input", tool.get("arguments", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"command": arguments}
        command = (
            arguments.get("command", arguments.get("cmd")) if isinstance(arguments, dict) else None
        )
        if not isinstance(command, str):
            return
        digest = "sha256:" + hashlib.sha256(command.encode()).hexdigest()
        if digest in shell_hashes:
            return
        shell_hashes.add(digest)
        if _NETWORK.search(command) or _BYPASS.search(command):
            candidates.append(
                {
                    "command_hash": digest,
                    "network_capable": bool(_NETWORK.search(command)),
                    "explicit_proxy_bypass": bool(_BYPASS.search(command)),
                }
            )

    for event in events:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") in {"compact_boundary", "context_compacted"}:
            compactions += 1
        if kind in {"assistant", "model_call"}:
            message = event.get("message", event)
            message_id = message.get("id", event.get("request_id"))
            if message_id:
                message_ids.add(str(message_id))
            else:
                unidentified += 1
            for content in message.get("content", []):
                if isinstance(content, dict) and content.get("type") == "tool_use":
                    inspect_tool(content)
        elif kind == "stream_event":
            streamed = event.get("event", {})
            if streamed.get("type") == "message_start":
                message_id = streamed.get("message", {}).get("id")
                if message_id:
                    message_ids.add(str(message_id))
                else:
                    unidentified += 1
            # Partial JSON argument deltas cannot safely establish a complete
            # shell audit; the final assistant message is needed for that.
        elif kind == "tool_call":
            inspect_tool(event)
    return {
        "events": len(events),
        "malformed_lines": malformed,
        "model_calls_transcript": len(message_ids),
        "unidentified_model_messages": unidentified,
        "shell_commands": len(shell_hashes),
        "shell_bypass_candidates": len(candidates),
        "shell_candidates": candidates,
        "compaction_count": compactions,
    }


def audit_transcript(
    records: list[dict], source: str | Path | Iterable[dict], *, shell_audited: bool = False
) -> dict:
    """A complete verdict requires a complete transcript and explicit shell audit."""
    result = parse_transcript(source)
    result["model_calls_lane"] = sum(record.get("kind") == "model_call" for record in records)
    result["shell_audited"] = bool(shell_audited)
    reasons = []
    if result["malformed_lines"]:
        reasons.append("malformed transcript lines")
    if result["unidentified_model_messages"]:
        reasons.append("model messages lack stable ids")
    if result["model_calls_lane"] != result["model_calls_transcript"]:
        reasons.append("transcript and lane call counts differ")
    if result["shell_bypass_candidates"]:
        reasons.append("network-capable shell commands need routing verification")
    if any(record.get("kind") == "completeness_gap" for record in records):
        reasons.append("the episode records a completeness gap")
    if not shell_audited:
        reasons.append("shell audit was not attested")
    if not result["model_calls_transcript"]:
        reasons.append("no model calls were established by the transcript")
    result.update(verdict="partial" if reasons else "complete", reasons=reasons)
    return result
