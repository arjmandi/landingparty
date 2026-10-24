"""ASSAY as a native harness: the world operates the run, Claude Code plays it."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from worldd.assay import BENIGN, registry
from worldd.harness_config import BUILTINS, harness_account
from worldd.harnesses import HarnessService
from worldd.mcp import manifest_tools
from worldd.models import manifest
from worldd.security import Capabilities
from worldd.storage import Store

SUBSET = {
    "type",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
}


def tools():
    return manifest_tools(
        manifest(
            ["deliver", "belief", "footprint", "schedule", "bounty", "market", "objective"],
            market_settlement="balance",
        )
    )


def within_subset(schema):
    assert set(schema) <= SUBSET, schema
    for item in (schema.get("properties") or {}).values():
        within_subset(item)
    if "items" in schema:
        within_subset(schema["items"])


def test_the_registry_offers_every_world_tool_in_assays_schema_subset():
    document, mapping = registry(tools(), goal="No objective", actions=50, usd=21.0)
    actions = {entry["name"]: entry for entry in document["actions"]}
    assert set(mapping) == set(actions)
    assert {spec["tool"] for spec in mapping.values()} == {tool["name"] for tool in tools()}
    for entry in document["actions"]:
        assert len(entry["name"]) <= 32 and entry["name"] == entry["name"].upper()
        for schema in entry["params"].values():
            within_subset(schema)
    # Every required scalar: flat. Optional or nested: one arguments object. None: none.
    assert mapping["WAIT"]["shape"] == "flat" and actions["WAIT"]["params"]["seconds"]
    assert mapping["STATUS"]["shape"] == "none" and actions["STATUS"]["params"] == {}
    fetch = actions["FETCH"]["params"]["arguments"]
    assert mapping["FETCH"]["shape"] == "object" and fetch["required"] == ["url"]
    # A free-form object travels as JSON text the adapter parses back.
    assert mapping["BELIEVE"]["json_fields"] == ["record"]
    assert "Pass record as JSON text." in actions["BELIEVE"]["description"]
    # Outward actions need ASSAY's declared worst case; the world's own records do not.
    assert actions["DELIVER"]["destructive"] and actions["CLAIM_BOUNTY"]["destructive"]
    assert all(
        "destructive" not in actions[name.upper()] for name in BENIGN if name.upper() in actions
    )
    assert document["secrets"] == ["WORLD_RUN_TOKEN"]
    assert document["budget"] == {"actions": 50, "usd": 21.0}
    assert "gate" not in document  # The prediction gate stays at its required default.


def test_a_hosted_harness_uses_its_hosts_account():
    assert harness_account("assay@1.5.1+claude-code@2.1.308/subscription") == "claude-code"
    assert harness_account("claude-code@2.1.308/api_key") == "claude-code"
    assay = next(item for item in BUILTINS if item.id == "assay")
    assert (assay.kind, assay.hosts, assay.auth_modes) == ("native", ("claude-code",), ())


FAKE_ASSAY = r"""
import json, os, sys
from pathlib import Path
log = Path(__file__).parent.parent / "assay.log"
args = sys.argv[1:]
if args == ["version"]:
    print("assay 1.5.1 | journal spec assay-journal-v1 | python 3.12"); raise SystemExit(0)
run = Path(args[1]); words = args[2:]
with log.open("a") as out:
    out.write(json.dumps({"words": words, "token": os.environ.get("WORLD_RUN_TOKEN"),
                          "tools": os.environ.get("WORLD_ASSAY_TOOLS"),
                          "assay": os.environ.get("ASSAY")}) + "\n")
if words[0] == "start":
    (run / ".assay").mkdir(parents=True)
    (run / ".assay" / "NOTES.md").write_text("# Notes\n")
elif words[:2] == ["hooks", "install"] and "--check" not in words:
    (run / ".claude").mkdir()
    (run / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "pre_tool_use.py"}]}]}}))
elif words[0] == "audit":
    events = [{"id": 1, "predict": "loads", "predict_ok": True}, {"id": 2, "predict": "x"}]
    (run / ".assay" / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    notes = "# Notes\nThe link is dead.\n".encode()
    if (run.parent / "bad-notes").exists():
        notes += b"\xff\xfe not utf-8\n"
    (run / ".assay" / "NOTES.md").write_bytes(notes)
    print(json.dumps({"verdict": "CLEAN"}))
"""

FAKE_CLAUDE = r"""
import json, os, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("2.1.308 (Claude Code)"); raise SystemExit(0)
prompt = sys.stdin.read()
(Path(__file__).parent.parent / "player.json").write_text(json.dumps({
    "argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt,
    "env": {k: v for k, v in os.environ.items() if k.startswith(("WORLD_", "ASSAY", "ANTHROPIC"))}}))
print(json.dumps({"type": "result", "subtype": "success"}), flush=True)
"""


@pytest.fixture
async def assay_world(tmp_path):
    scripts = tmp_path / "bin"
    scripts.mkdir()
    for name, body in (("assay", FAKE_ASSAY), ("claude", FAKE_CLAUDE)):
        path = scripts / name
        path.write_text(f"#!{sys.executable}\n{body}")
        path.chmod(0o755)
    store = Store(tmp_path / "data")
    capabilities = Capabilities(store.data_dir)
    world = SimpleNamespace(store=store, capabilities=capabilities, services={})
    world.run_config = lambda run_id: store.get_run(run_id)["config"]
    world.manifest = lambda run_id: {"tools": tools()}
    service = HarnessService(
        world,
        registry=[item for item in BUILTINS if item.id in {"claude-code", "assay"}],
        environment={
            "PATH": os.environ["PATH"],
            "WORLD_CLAUDE_CLI": str(scripts / "claude"),
            "WORLD_ASSAY_CLI": str(scripts / "assay"),
            "ASSAY_PYTHON": sys.executable,
        },
    )
    world.services["harnesses"] = service

    async def end_run(run_id, **kwargs):
        await service.stop_for_run(run_id)
        if store.get_run(run_id)["state"] != "sealed":
            store.append_record(run_id, "seal", **kwargs)
            store.update_run(run_id, state="sealed")

    world.end_run = end_run

    def create(**changes):
        config = {
            "budget": {"usd": 20, "wall_s": 60},
            "isolation_profile": "cooperative",
            "research_mode": "development",
            "capture_model_io": True,
            "model_snapshot_id": "fixture-model",
            "model_billing_mode": "api_key",
            "harness_id": "assay@1.5.1+claude-code@2.1.308/api_key",
            "preamble": "Actual World briefing",
            **changes,
        }
        info = store.create_run(config)
        (store.run_dir(info["run_id"]) / "work").mkdir()
        return {
            **info,
            "preamble": "Actual World briefing",
            "run_token": capabilities.issue(info["run_id"]),
        }

    yield service, world, create, tmp_path
    await service.aclose()
    capabilities.close()
    store.close()


async def test_the_world_operates_assay_and_claude_code_plays_through_it(assay_world):
    service, world, create, tmp_path = assay_world
    info = create()
    session = await service.launch(info, "assay", model="fixture-model")
    await asyncio.wait_for(service.tasks[session["session_id"]], 10)
    calls = [json.loads(line) for line in (tmp_path / "assay.log").read_text().splitlines()]
    commands = [
        call["words"][0]
        if call["words"][0] != "hooks"
        else " ".join(word for word in call["words"][:3] if not word.startswith("/"))
        for call in calls
    ]
    assert commands[0] == "start"
    assert commands[1].startswith("hooks install") and "--check" in calls[2]["words"]
    assert commands[-3:] == ["spend", "audit", "stop"]
    start = calls[0]
    # Only the daemon holds the run's capability.
    assert start["token"] == info["run_token"] and start["tools"]
    assert "--adapter" in start["words"] and start["words"][
        start["words"].index("--adapter") + 1
    ].endswith("adapters/assay/adapter.py:factory")
    assert "--deny" in calls[1]["words"] and calls[1]["assay"]
    player = json.loads((tmp_path / "player.json").read_text())
    root = Path(service._load(session["session_id"])["root"])
    assert player["cwd"] == str((root / "assay-run").resolve()) or player["cwd"] == str(
        root / "assay-run"
    )
    assert "WORLD_RUN_TOKEN" not in player["env"]
    assert player["env"]["ASSAY"].endswith("/assay") and player["env"]["ASSAY_STOP_GATE"]
    assert player["env"]["ANTHROPIC_AUTH_TOKEN"] == player["env"]["WORLD_LANE_TOKEN"]
    assert "How you act here: through ASSAY." in player["prompt"]
    assert "Actual World briefing" in player["prompt"]
    mcp = json.loads((root / "world.mcp.json").read_text())
    settings = json.loads((root / "world.settings.json").read_text())
    assert mcp == {"mcpServers": {}}
    assert "Bash" in settings["permissions"]["allow"]
    assert "mcp__world__*" not in settings["permissions"]["allow"]
    assert settings["hooks"]["PreToolUse"]
    assert "stop_gate.py" in settings["hooks"]["Stop"][-1]["hooks"][0]["command"]
    records = {record["kind"]: record for record in world.store.records(info["run_id"])}
    started = records["harness_started"]
    assert (started["harness_id"], started["host"], started["version"]) == (
        "assay",
        "claude-code",
        "1.5.1",
    )
    assert started["assay_registry_hash"]
    journal = records["assay_journal"]
    assert (journal["events"], journal["predictions"], journal["predictions_correct"]) == (2, 2, 1)
    assert journal["audit"] == {"verdict": "CLEAN"} and "The link is dead." in journal["notes"]
    assert service.get(session["session_id"])["account"] == "claude-code"


async def test_assay_and_claude_code_share_one_account(assay_world):
    service, _world, create, _tmp_path = assay_world
    info = create()
    session = await service.launch(info, "assay", model="fixture-model")
    assert service.is_busy("claude-code")
    await asyncio.wait_for(service.tasks[session["session_id"]], 10)
    assert not service.is_busy("claude-code")


async def test_a_run_must_name_a_host_the_harness_plays_through(assay_world):
    service, _world, create, _tmp_path = assay_world
    info = create(harness_id="assay@1.5.1+codex@0.163.0/api_key")
    with pytest.raises(ValueError, match="host"):
        await service.launch(info, "assay", model="fixture-model")


async def test_unreadable_notes_or_a_failed_close_never_block_the_seal(assay_world, monkeypatch):
    service, world, create, _tmp_path = assay_world
    info = create()
    session = await service.launch(info, "assay", model="fixture-model")
    root = Path(service._load(session["session_id"])["root"])
    (root / "bad-notes").write_text("")
    await asyncio.wait_for(service.tasks[session["session_id"]], 10)
    journal = next(r for r in world.store.records(info["run_id"]) if r["kind"] == "assay_journal")
    assert "The link is dead." in journal["notes"] and "\ufffd" in journal["notes"]
    assert world.store.get_run(info["run_id"])["state"] == "sealed"

    async def broken(self):
        raise RuntimeError("the journal could not be read")

    monkeypatch.setattr("worldd.assay.AssayRun.finish", broken)
    info = create()
    session = await service.launch(info, "assay", model="fixture-model")
    await asyncio.wait_for(service.tasks[session["session_id"]], 10)
    assert service.drained[session["session_id"]].is_set()
    assert world.store.get_run(info["run_id"])["state"] == "sealed"
    journal = next(r for r in world.store.records(info["run_id"]) if r["kind"] == "assay_journal")
    assert journal["error"] == "RuntimeError: the journal could not be read"
    assert not service.is_busy("claude-code")
