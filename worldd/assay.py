"""ASSAY as a native harness: the world is its operator, another harness its player.

ASSAY never calls a model. A player, a native client such as Claude Code on its own
connection, acts on the world only through `"$ASSAY" act NAME ... --predict`, and
ASSAY grades each prediction against the world's reply in its own journal. The
world does what ASSAY's `/assay:attach` skill and session runner do for a person:
it writes the registry of world tools, starts ASSAY's daemon with the world's
adapter, installs ASSAY's hooks and Stop gate for the player, carries the notes
from one session to the next, reports its spend, and audits and stops the run.
One ASSAY run lives for one world session: live world actions cannot be replayed.

The player never holds the run's capability: only the daemon does, so the world
sees the player's actions only as ASSAY forwards them. The isolation stays
cooperative: the player runs as the same OS user as the daemon.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import shlex
from pathlib import Path

from .mcp import READ_TOOLS
from .util import digest, now

ADAPTER = Path(__file__).parents[1] / "adapters" / "assay" / "adapter.py"


def _adapter():
    """The adapter module ASSAY's daemon imports, loaded here for its shared rules."""
    spec = importlib.util.spec_from_file_location("landingparty_assay_adapter", ADAPTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assay_budget = _adapter().assay_budget
WORLD_ID = "landing-party"
# The world's own records and waiting change nothing outside it; any other action
# needs ASSAY's declared worst case and recovery before it runs.
BENIGN = READ_TOOLS | {"believe", "revise", "retract", "link", "expect", "sleep"}
SCALARS = {"string", "integer", "number", "boolean"}
# A free-form object travels as JSON text the adapter parses back.
JSON_TEXT = {"type": "string", "maxLength": 20000}
COMMAND_TIMEOUT_S = 120
PLAYER_ALLOW = ["Bash", "Read(./**)", "Write(./**)", "Edit(./**)", "Glob", "Grep"]
GATE_STATE_VERSION = 1


def _value(schema):
    """One world parameter in ASSAY's schema subset, or None when only JSON text can carry it."""
    if not isinstance(schema, dict):
        return None
    if "anyOf" in schema:
        present = [
            option
            for option in schema["anyOf"]
            if isinstance(option, dict) and option.get("type") != "null"
        ]
        if len(present) != 1:
            return None
        schema = {**{k: v for k, v in schema.items() if k != "anyOf"}, **present[0]}
    kind = schema.get("type")
    if kind in SCALARS:
        kept = ("enum", "minimum", "maximum", "minLength", "maxLength")
        return {"type": kind, **{key: schema[key] for key in kept if key in schema}}
    if kind == "array":
        item = _value(schema.get("items"))
        if item is None:
            return None
        sizes = {key: schema[key] for key in ("minItems", "maxItems") if key in schema}
        return {"type": "array", "items": item, **sizes}
    if (
        kind == "object"
        and isinstance(schema.get("properties"), dict)
        and schema.get("additionalProperties") is False
    ):
        fields = {name: _value(item) for name, item in schema["properties"].items()}
        if any(item is None for item in fields.values()):
            return None
        return {
            "type": "object",
            "properties": fields,
            "required": [name for name in schema.get("required", []) if name in fields],
            "additionalProperties": False,
        }
    return None


def registry(tools, *, goal, actions, usd):
    """ASSAY's registry of the run's world tools, and the adapter's map of each action.

    An action takes its parameters flat when all are required scalars, as one
    `arguments` object otherwise, and none when the tool takes none.
    """
    entries, mapping = [], {}
    for tool in tools:
        name = re.sub(r"[^A-Z0-9_]", "_", tool["name"].upper())[:32]
        schema = tool.get("inputSchema") or {}
        fields = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        values = {field: _value(item) for field, item in fields.items()}
        text = sorted(field for field, item in values.items() if item is None)
        values = {field: item or dict(JSON_TEXT) for field, item in values.items()}
        if not values:
            params, shape = {}, "none"
        elif required >= set(values) and all(item["type"] in SCALARS for item in values.values()):
            params, shape = values, "flat"
        else:
            params = {
                "arguments": {
                    "type": "object",
                    "properties": values,
                    "required": sorted(required & set(values)),
                    "additionalProperties": False,
                }
            }
            shape = "object"
        entry = {"name": name, "params": params}
        description = (tool.get("description") or "")[:1800]
        if text:
            description += f" Pass {', '.join(text)} as JSON text."
        if description:
            entry["description"] = description.strip()
        if tool["name"] not in BENIGN:
            entry["destructive"] = True
        entries.append(entry)
        mapping[name] = {"tool": tool["name"], "shape": shape, "json_fields": text}
    return {
        "actions": entries,
        "budget": {"actions": actions, "usd": usd},
        "goal": {"text": goal},
        "secrets": ["WORLD_RUN_TOKEN"],
        "mode_note": "Landing Party: each action is one world tool; the world answers as it "
        "would any harness, and keeps no prediction.",
    }, mapping


def player_prompt(preamble, goal):
    """The player's briefing: the world's own, then how this world is reached through ASSAY."""
    parts = [preamble.strip()]
    if goal:
        parts.append(goal.strip())
    parts.append(
        "How you act here: through ASSAY.\n"
        '- You reach the world only through ASSAY\'s command line, `"$ASSAY"`, run from this '
        "directory. Each world tool is an ASSAY action; you have no other route to the world.\n"
        '- First read CONSTITUTION.md here, run `"$ASSAY" status`, and read `.assay/NOTES.md`; '
        "it carries your notes from earlier sessions.\n"
        '- Act with `"$ASSAY" act NAME key=value --predict "what you expect"`, or '
        "`--params '{\"arguments\": {...}}'` for an action that takes an arguments object. "
        "Every action needs a prediction; ASSAY grades it against the world's reply.\n"
        "- Read nothing outside this directory except the constitution, edit only "
        "`.assay/NOTES.md`, and never run `assay start` or `assay stop`. Before you stop early, "
        "add a line `STOP: <reason>` to your notes."
    )
    return "\n\n".join(parts) + "\n"


class AssayRun:
    """One ASSAY run for one world session, operated by the world."""

    def __init__(self, launcher, python, root, environment):
        self.launcher, self.python = str(launcher), str(python)
        self.root = Path(root)
        self.run_dir = self.root / "assay-run"
        self.environment = dict(environment)
        self.owner = self.root / "owner.token"
        self.policy = self.root / "hooks.json"
        self.gate = self.root / "gate.json"
        self.tools = self.root / "tools.json"
        self.registry = self.root / "registry.json"
        self.anchors = self.root / "anchors"
        self.tmp = self.root / "tmp"
        self.checkout = Path(self.launcher).resolve().parents[1]

    def shared_environment(self):
        """What operator, daemon and player share: one interpreter, temp dir and anchors."""
        return {
            "ASSAY_PYTHON": self.python,
            "ASSAY_ANCHOR_DIR": str(self.anchors),
            "TMPDIR": str(self.tmp),
        }

    async def _assay(self, *words, extra=None):
        environment = {**self.environment, **self.shared_environment(), **(extra or {})}
        process = await asyncio.create_subprocess_exec(
            self.launcher,
            "--run-dir",
            str(self.run_dir),
            *words,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), COMMAND_TIMEOUT_S)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError(f"`assay {words[0]}` did not finish") from None
        return process.returncode, output.decode(errors="replace")

    @staticmethod
    def _said(output):
        lines = [line for line in output.splitlines() if line.strip()]
        errors = [line for line in lines if line.startswith("ERROR")]
        return (errors or lines[-1:] or ["no output"])[0][:500]

    async def prepare(self, *, tools, goal, actions, usd, notes, world, deny=()):
        """Start the run and return what the player's launch needs."""
        for path in (self.anchors, self.tmp):
            path.mkdir(mode=0o700)
        self.run_dir.mkdir(mode=0o700)
        document, mapping = registry(tools, goal=goal, actions=actions, usd=usd)
        self.registry.write_text(json.dumps(document, indent=2))
        self.tools.write_text(json.dumps(mapping))
        # The run's capability is in the daemon's environment only.
        daemon = {**world, "WORLD_ASSAY_TOOLS": str(self.tools)}
        code, output = await self._assay(
            "start",
            WORLD_ID,
            "--adapter",
            f"{ADAPTER}:factory",
            "--registry",
            str(self.registry),
            "--owner-token-file",
            str(self.owner),
            extra=daemon,
        )
        if code:
            raise ValueError("ASSAY could not start: " + self._said(output))
        refused = ["--deny", r"(^|\s)spend(\s|$)"]  # the player cannot report spend
        for pattern in deny:
            refused += ["--deny", pattern]
        for words in (
            (
                "hooks",
                "install",
                "--policy",
                str(self.policy),
                "--owner-token-file",
                str(self.owner),
                *refused,
            ),
            ("hooks", "install", "--check"),
        ):
            code, output = await self._assay(*words, extra={"ASSAY": self.launcher})
            if code:
                await self.stop()
                raise ValueError("ASSAY's hooks could not be installed: " + self._said(output))
        installed = json.loads((self.run_dir / ".claude" / "settings.json").read_text())
        hooks = dict(installed.get("hooks", {}))
        gate = shlex.join(
            [
                self.python,
                str(self.checkout / "hooks" / "stop_gate.py"),
                "--run-dir",
                str(self.run_dir),
            ]
        )
        hooks["Stop"] = [
            *hooks.get("Stop", []),
            {"hooks": [{"type": "command", "command": gate, "timeout": 60}]},
        ]
        constitution = self.checkout / "CONSTITUTION.md"
        if constitution.is_file():
            (self.run_dir / "CONSTITUTION.md").write_text(constitution.read_text())
        notes_path = self.run_dir / ".assay" / "NOTES.md"
        if notes and notes.strip():
            current = notes_path.read_text() if notes_path.exists() else ""
            notes_path.write_text(
                "## Carried from your previous session\n\n" + notes.strip() + "\n\n" + current
            )
        activity = self.run_dir / ".assay" / "activity.jsonl"
        declared = notes_path.read_text() if notes_path.exists() else ""
        self.gate.write_text(
            json.dumps(
                {
                    "version": GATE_STATE_VERSION,
                    "launch": {
                        "session": 1,
                        "at": now(),
                        "activity": len(activity.read_text().splitlines())
                        if activity.exists()
                        else 0,
                        "declarations": sum(
                            1
                            for line in declared.splitlines()
                            if line.strip().startswith("STOP:") and line.strip()[5:].strip()
                        ),
                        "paid": 0,
                    },
                    "blocks": 0,
                    "paid_at_last_block": None,
                    "decisions": [],
                }
            )
        )
        return {
            "cwd": str(self.run_dir),
            "hooks": hooks,
            "allow": list(PLAYER_ALLOW),
            "env": {
                **self.shared_environment(),
                "ASSAY": self.launcher,
                "ASSAY_STOP_GATE": str(self.gate),
            },
            "registry_hash": digest(document),
        }

    async def report_spend(self, usd):
        """The world's running total for the session, so ASSAY's ledger sees the same dollars."""
        await self._assay(
            "spend", "report", "--usd", f"{float(usd):.6f}", "--tokens", "0", "--id", "world"
        )

    def notes(self):
        path = self.run_dir / ".assay" / "NOTES.md"
        return path.read_text() if path.exists() else ""

    async def stop(self):
        try:
            await self._assay("stop")
        except ValueError:
            pass

    async def finish(self):
        """Audit and stop the run; summarize its journal for the world's record."""
        try:
            code, output = await self._assay("audit", "--json")
            try:
                audit = json.loads(output)
            except ValueError:
                audit = {"exit_code": code, "said": self._said(output)}
        finally:
            await self.stop()
        journal = self.run_dir / ".assay" / "events.jsonl"
        events = []
        if journal.exists():
            for line in journal.read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        predicted = [event for event in events if event.get("predict")]
        return {
            "journal": str(journal),
            "journal_hash": digest(journal.read_bytes()) if journal.exists() else None,
            "events": len(events),
            "predictions": len(predicted),
            "predictions_correct": sum(event.get("predict_ok") is True for event in predicted),
            "audit": audit,
        }
