"""Researcher CLI for the HTTP control plane."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


def _json_input(value: str) -> Any:
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text())
    return json.loads(value)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Landing Party researcher control plane")
    root.add_argument("--url", default=os.environ.get("WORLD_BASE_URL", "http://127.0.0.1:7777"))
    sub = root.add_subparsers(dest="verb", required=True)
    energize = sub.add_parser("energize", help="Start a run from its JSON configuration")
    energize.add_argument("config", type=Path)
    holodeck = sub.add_parser("holodeck", help="Start a replay run against an existing archive")
    holodeck.add_argument("archive_id")
    holodeck.add_argument("config", type=Path)
    sub.add_parser("runs", help="List all started runs")
    sub.add_parser("health", help="Check the control plane")
    for verb, help_text in [
        ("status", "Read spend and run state"),
        ("beam-up", "End a run and seal its record"),
        ("red-alert", "Freeze a run without sealing it"),
        ("resume", "Resume a frozen run"),
        ("emergency-beam-out", "Stop a run and cancel outstanding work"),
        ("report", "Read the passive mission report"),
    ]:
        command = sub.add_parser(verb, help=help_text)
        command.add_argument("run_id")
        if verb == "emergency-beam-out":
            command.add_argument("--dry-run", action="store_true")
    act = sub.add_parser("act", help="Submit one world tool call")
    act.add_argument("run_id")
    act.add_argument("tool")
    act.add_argument("--params", default="{}", help="JSON object or @path.json")
    inbox = sub.add_parser("inbox", help="Read events after a cursor")
    inbox.add_argument("run_id")
    inbox.add_argument("--since", type=int, default=0)
    inbox.add_argument("--limit", type=int, default=50)
    inbox.add_argument("--wait-seconds", type=float, default=0)
    export = sub.add_parser("export", help="Download a sealed episode archive")
    export.add_argument("run_id")
    export.add_argument("--output", type=Path, required=True)
    bridge = sub.add_parser("bridge", help="Read agent messages on the ask console")
    bridge.add_argument("--run-id")
    message = sub.add_parser("message", help="Send an operator message if the run permits it")
    message.add_argument("run_id")
    message.add_argument("text")
    conn = sub.add_parser("conn", help="Hold a browser session for a pending takeover ticket")
    conn.add_argument("session_id")
    conn.add_argument("--ticket")
    conn.add_argument("--done", action="store_true")
    conn.add_argument("--reason", default="operator_takeover")
    orders = sub.add_parser(
        "sealed-orders", help="Register a frozen pre-registration and version tuple"
    )
    orders.add_argument("prereg", type=Path)
    identity = orders.add_mutually_exclusive_group()
    identity.add_argument("--tuple", dest="tuple_path", type=Path)
    identity.add_argument("--run", dest="run_id")
    orbit = sub.add_parser("orbit", help="Run due afterlife readings for an ended life")
    orbit.add_argument("life_id")
    debrief = sub.add_parser("debrief", help="Run the sealed episode's requested post-run analysis")
    debrief.add_argument("run_id")
    debrief.add_argument("--options", default="{}", help="JSON object or @path.json")
    debrief.add_argument(
        "--budget", type=float, help="Explicit total dollar cap for requested model measurements"
    )
    stardate = sub.add_parser(
        "stardate", help="Print a run's vector and optional registered comparability"
    )
    stardate.add_argument("run_id", nargs="?")
    stardate.add_argument("--against", dest="series_id")
    pilot = sub.add_parser(
        "shakedown", help="Plan or explicitly execute the registered twelve-run pilot"
    )
    pilot.add_argument("mode", nargs="?", choices=["plan", "execute"], default="plan")
    pilot.add_argument("--spec", type=Path)
    pilot.add_argument("--study-id")
    pilot.add_argument("--execute", action="store_true")
    pilot.add_argument("--budget", type=float)
    pilot.add_argument("--wait", action="store_true")
    pilot.add_argument(
        "--options", default="{}", help="Registered execution options as JSON or @path"
    )
    return root


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None) -> int:
    args = parser().parse_args(argv)
    owned = client is None
    raw_client = client or httpx.Client(base_url=args.url, timeout=180, trust_env=False)

    def request(method, path, **kwargs):
        token = os.environ.get("WORLD_CONTROL_TOKEN")
        if token:
            address = urlsplit(str(raw_client.base_url))
            if address.port != 7777 or address.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("Researcher commands require the trusted control listener")
            kwargs["headers"] = {**kwargs.pop("headers", {}), "X-World-Control-Token": token}
        return getattr(raw_client, method)(path, **kwargs)

    def current_run():
        if getattr(args, "run_id", None):
            return args.run_id
        response = request("get", "/v1/runs")
        response.raise_for_status()
        runs = [
            run
            for run in response.json().get("runs", [])
            if run["state"] in {"running", "frozen", "preflight"}
        ]
        if not runs:
            raise ValueError("Select a run ID; no current run supplies a version tuple")
        return runs[-1]["run_id"]

    def tuple_for(run_id):
        response = request("get", "/v1/status", params={"run_id": run_id})
        response.raise_for_status()
        value = response.json().get("tuple", response.json().get("version_tuple"))
        if not isinstance(value, dict) or not value:
            raise ValueError("the server did not return the run's version tuple")
        return value

    try:
        if args.verb in {"energize", "holodeck"}:
            config = json.loads(args.config.read_text())
            if not isinstance(config, dict):
                raise ValueError("run configuration must be a JSON object")
            if args.verb == "holodeck":
                config.update(mode="replay", archive_in=args.archive_id)
            response = request("post", "/v1/run", json=config)
        elif args.verb == "sealed-orders":
            prereg = json.loads(args.prereg.read_text())
            if not isinstance(prereg, dict):
                raise ValueError("Pre-registration must be a JSON object")
            version = (
                json.loads(args.tuple_path.read_text())
                if args.tuple_path
                else (prereg.get("version_tuple") or tuple_for(current_run()))
            )
            if not isinstance(prereg, dict) or not isinstance(version, dict):
                raise ValueError("Pre-registration and tuple must be JSON objects")
            response = request("post", "/v1/series", json={"prereg": prereg, "tuple": version})
        elif args.verb == "conn":
            if args.done:
                response = request("post", f"/v1/sessions/{args.session_id}/takeover-done")
            else:
                ticket = args.ticket
                if not ticket:
                    pending = request("get", "/v1/asks", params={"state": "open"})
                    pending.raise_for_status()
                    matches = []
                    for item in pending.json().get("tickets", []):
                        body = item.get("body", {})
                        if item.get("kind") == "takeover" and args.session_id in {
                            body.get("session_id"),
                            body.get("session"),
                            body.get("request", {}).get("session_id"),
                        }:
                            matches.append(item["ticket_id"])
                    if len(matches) != 1:
                        raise ValueError(
                            "Select the session's pending takeover ticket with --ticket"
                        )
                    ticket = matches[0]
                response = request(
                    "post",
                    f"/v1/sessions/{args.session_id}/takeover",
                    json={"ticket_id": ticket, "reason": args.reason},
                )
        elif args.verb == "orbit":
            response = request("post", f"/v1/lives/{args.life_id}/orbit")
        elif args.verb == "debrief":
            options = _json_input(args.options)
            if not isinstance(options, dict):
                raise ValueError("Analysis options must be a JSON object")
            if args.budget is not None:
                if not math.isfinite(args.budget) or args.budget <= 0:
                    raise ValueError("Measurement budget must be finite and positive")
                options["measurement_budget_usd"] = args.budget
            if any(
                options.get(key) for key in ("judge", "interview", "probe_bank")
            ) and not options.get("measurement_budget_usd"):
                raise ValueError("Requested model measurements require an explicit --budget")
            response = request(
                "post", "/v1/debrief", json={"run_id": args.run_id, "options": options}
            )
        elif args.verb == "stardate":
            run_id = current_run()
            version = tuple_for(run_id)
            if not args.series_id:
                _print(version)
                return 0
            registered = request("get", f"/v1/series/{args.series_id}")
            registered.raise_for_status()
            series = registered.json()
            runs = series.get("runs", [])
            if not runs:
                _print(
                    {
                        "tuple": version,
                        "against": args.series_id,
                        "comparable": False,
                        "reasons": ["Registered series has no reference runs"],
                    }
                )
                return 0
            reference = next(
                (
                    run
                    for run in reversed(runs)
                    if (run.get("run_id") if isinstance(run, dict) else run) != run_id
                ),
                runs[-1],
            )
            right = reference["run_id"] if isinstance(reference, dict) else reference
            response = request(
                "post",
                "/v1/compare",
                json={"left": run_id, "right": right, "series_id": args.series_id},
            )
        elif args.verb == "shakedown":
            execute = args.execute or args.mode == "execute"
            if execute and (
                args.budget is None or not math.isfinite(args.budget) or args.budget <= 0
            ):
                raise ValueError("Pilot execution requires an explicit positive --budget")
            if args.study_id:
                response = request("get", f"/v1/studies/{args.study_id}")
            else:
                spec = json.loads(args.spec.read_text()) if args.spec else None
                response = request(
                    "post", "/v1/studies/plan", json={"kind": "shakedown", "spec": spec}
                )
            response.raise_for_status()
            if execute:
                study_id = response.json()["study_id"]
                options = _json_input(args.options)
                if not isinstance(options, dict):
                    raise ValueError("Study execution options must be a JSON object")
                options["wait"] = args.wait
                response = request(
                    "post",
                    f"/v1/studies/{study_id}/execute",
                    json={"execute": True, "budget_usd": args.budget, "options": options},
                    timeout=None if args.wait else 180,
                )
        elif args.verb in {"beam-up", "red-alert", "resume", "emergency-beam-out"}:
            path = {
                "beam-up": "beam-up",
                "red-alert": "freeze",
                "resume": "resume",
                "emergency-beam-out": "kill",
            }[args.verb]
            body: dict[str, Any] = {"run_id": args.run_id}
            if args.verb == "emergency-beam-out":
                body["dry_run"] = args.dry_run
            response = request("post", f"/v1/{path}", json=body)
        elif args.verb == "act":
            params = _json_input(args.params)
            if not isinstance(params, dict):
                raise ValueError("tool parameters must be a JSON object")
            response = request(
                "post", "/v1/act", json={"run_id": args.run_id, "tool": args.tool, "params": params}
            )
        elif args.verb == "message":
            response = request(
                "post", "/v1/bridge/message", json={"run_id": args.run_id, "text": args.text}
            )
        else:
            path = {
                "runs": "/v1/runs",
                "health": "/healthz",
                "status": "/v1/status",
                "inbox": "/v1/inbox",
                "report": "/v1/report",
                "export": "/v1/export",
                "bridge": "/v1/bridge",
            }[args.verb]
            query = {}
            if getattr(args, "run_id", None):
                query["run_id"] = args.run_id
            if args.verb == "inbox":
                query.update(since=args.since, limit=args.limit, wait_seconds=args.wait_seconds)
            if args.verb == "export" and args.output.exists():
                raise ValueError(f"output already exists: {args.output}")
            response = request("get", path, params=query)
        response.raise_for_status()
        if args.verb == "export":
            # Exclusive create preserves existing local work, including a concurrent writer.
            with args.output.open("xb") as stream:
                stream.write(response.content)
            print(str(args.output))
        else:
            data = response.json()
            _print(data)
        return 0
    except (OSError, ValueError, httpx.HTTPError) as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            print(f"lp: HTTP {exc.response.status_code}: {exc.response.text}", file=sys.stderr)
        else:
            print(f"lp: {exc}", file=sys.stderr)
        return 1
    finally:
        if owned:
            raw_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
