"""Thin authenticated shell face for the same free belief/expectation tools."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def parser():
    command = argparse.ArgumentParser(description="Read or append Landing Party beliefs")
    command.add_argument("--url", default=os.environ.get("WORLD_BASE_URL", "http://127.0.0.1:7777"))
    command.add_argument("--run-id", default=os.environ.get("WORLD_RUN_ID"))
    sub = command.add_subparsers(dest="command", required=True)
    all_ = sub.add_parser("all", help="Read the current fold")
    all_.add_argument("--as-of")
    about = sub.add_parser("about", help="Read beliefs about an entity")
    about.add_argument("entity")
    about.add_argument("--as-of")
    for name in ("why", "history"):
        item = sub.add_parser(name)
        item.add_argument("id")
    changed = sub.add_parser("changed")
    changed.add_argument("--since", required=True)
    changed.add_argument("--until")
    review = sub.add_parser("review")
    review.add_argument("kind", nargs="?", choices=("queue", "conflicts", "stale"), default="queue")
    for name in ("believe", "expect"):
        item = sub.add_parser(name)
        item.add_argument("record", help="JSON object, @path.json, or - for stdin")
    revise = sub.add_parser("revise")
    revise.add_argument("id")
    revise.add_argument("delta", help="JSON object, @path.json, or - for stdin")
    retract = sub.add_parser("retract")
    retract.add_argument("id")
    link = sub.add_parser("link")
    link.add_argument("from_id")
    link.add_argument("to")
    link.add_argument(
        "relation",
        choices=("supports", "contradicts", "derived_from", "supersedes", "operationalised_by"),
    )
    return command


def _object(value):
    if value == "-":
        result = json.load(sys.stdin)
    elif value.startswith("@"):
        result = json.loads(Path(value[1:]).read_text())
    else:
        result = json.loads(value)
    if not isinstance(result, dict):
        raise TypeError("record/delta must be a JSON object")
    return result


def request(args):
    name = args.command
    if name in {"all", "about", "why", "history", "changed", "review"}:
        params = {"query": name}
        for key in ("entity", "id", "as_of", "since", "until", "kind"):
            value = getattr(args, key, None)
            if value is not None:
                params[key] = value
        return {"run_id": args.run_id, "tool": "beliefs", "params": params}
    if name in {"believe", "expect"}:
        params = {"record": _object(args.record)}
    elif name == "revise":
        params = {"id": args.id, "delta": _object(args.delta)}
    elif name == "retract":
        params = {"id": args.id}
    else:
        params = {"from": args.from_id, "to": args.to, "relation": args.relation}
    return {"run_id": args.run_id, "tool": name, "params": params}


def run(argv=None, *, client=None):
    args = parser().parse_args(argv)
    token = os.environ.get("WORLD_RUN_TOKEN")
    if not args.run_id:
        print("beliefctl: set WORLD_RUN_ID or supply --run-id", file=sys.stderr)
        return 2
    if not token:
        print("beliefctl: WORLD_RUN_TOKEN is required", file=sys.stderr)
        return 2
    target = urlsplit(args.url)
    if (
        target.scheme not in {"http", "https"}
        or not target.hostname
        or target.username
        or target.password
        or target.query
        or target.fragment
    ):
        print("beliefctl: invalid world base URL", file=sys.stderr)
        return 2
    owned = client is None
    client = client or httpx.Client(timeout=65, follow_redirects=False, trust_env=False)
    try:
        body = request(args)
        response = client.post(
            args.url.rstrip("/") + "/v1/act",
            json=body,
            headers={"Authorization": "Bearer " + token},
        )
        if response.status_code != 200:
            print(f"beliefctl: world returned HTTP {response.status_code}", file=sys.stderr)
            return 2
        result = response.json()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("kind") == "blocked" else 0
    except (httpx.HTTPError, ValueError, TypeError, OSError):
        print("beliefctl: world request or JSON input failed", file=sys.stderr)
        return 2
    finally:
        if owned:
            client.close()


def main():
    raise SystemExit(run())


if __name__ == "__main__":
    main()
