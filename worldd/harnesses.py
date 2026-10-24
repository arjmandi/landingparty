"""Allowlisted native CLI discovery, explicit installation and real run lifecycle.

Subscription credentials stay in official CLI profiles. They are never API
keys, and their model usage cannot be represented as measured World spend.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from pathlib import Path
from urllib.parse import urlsplit

from .assay import AssayRun, assay_budget, player_prompt
from .harness_config import (
    BUILTINS,
    base_environment,
    binary_path,
    harness_account,
    identifier,
    launch_configuration,
    layout_context,
    model_name,
)
from .identity import VenueDB
from .mcp import manifest_tools
from .util import canonical, digest, now
from .util import identifier as new_id

MAX_LINE = 64 * 1024
MAX_LOG = 2 * 1024 * 1024
# A metered line is read whole, apart from the log's limits, so its usage is never cut off.
METER_LINE = 8 * 1024 * 1024
TERMINAL = {"completed", "failed", "stopped", "interrupted"}
# How often an ASSAY run hears the world's running spend, and how much of its
# notes is carried to the life's next session (ASSAY's own default cap).
ASSAY_SPEND_INTERVAL_S = 60
ASSAY_NOTES_CHARS = 16000
# A partial-message stream keeps only what prices a message: its start and final usage.
KEPT_STREAM_EVENTS = {"message_start", "message_delta"}
SECRET_FIELD = re.compile(
    r"""(?i)(["']?(?:access_token|refresh_token|api_key|authorization|password|secret|WORLD_RUN_TOKEN)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,}\n]+)"""
)


def _token_chunk(line: bytes) -> bool:
    """A Claude partial-message chunk other than a message's start or final usage."""
    if not line.startswith(b'{"type":"stream_event"'):
        return False
    try:
        event = json.loads(line)
    except ValueError:
        return False
    inner = event.get("event") if isinstance(event, dict) else None
    return isinstance(inner, dict) and inner.get("type") not in KEPT_STREAM_EVENTS


def _birth(pid):
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _sanitize(text, secrets=()):
    for value in secrets:
        if value and len(value) >= 4:
            text = text.replace(value, "[redacted]")
    text = SECRET_FIELD.sub(lambda match: match[1] + '"[redacted]"', text)
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._+/=-]+", "Bearer [redacted]", text)
    return re.sub(r"(?<=//)[^/@\s]+:[^/@\s]+@", "[redacted]@", text)


class HarnessService(VenueDB):
    def __init__(
        self,
        world,
        *,
        registry=None,
        auth=None,
        settings=None,
        environment=None,
        agent_url="http://127.0.0.1:7777/agent",
        lane_url="http://127.0.0.1:7777/model",
    ):
        super().__init__(world, "harnesses")
        self.registry = {item.id: item for item in (BUILTINS if registry is None else registry)}
        for name in self.registry:
            identifier(name, "harness identifier")
        self.auth = auth
        self.settings = settings
        self.environment = dict(os.environ if environment is None else environment)
        self.agent_url, self.lane_url = agent_url, lane_url
        self.root = self.store.data_dir / "native-harnesses"
        if self.root.is_symlink():
            raise ValueError("Native harness storage must not be symlinked")
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.root.chmod(0o700)
        self.tools = self.root / "tools"
        if self.tools.is_symlink():
            raise ValueError("Native tool storage must not be symlinked")
        self.tools.mkdir(mode=0o700, exist_ok=True)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS native_sessions (
                session_id TEXT PRIMARY KEY,run_id TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS native_installs (
                harness_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        """)
        self.processes, self.tasks, self.drained = {}, {}, {}
        self.install_tasks = {}
        self._launch_lock = asyncio.Lock()
        self._stop_from_world = set()
        self._secrets = {}
        # ASSAY runs the world operates for sessions whose harness plays through another.
        self.assay_runs, self._spend_reporters = {}, {}
        self._closed = False
        if self.auth is None:
            from .harness_auth import HarnessAuthService

            self.auth = HarnessAuthService(
                self.store.data_dir,
                binaries={
                    name: (
                        lambda name=name: binary_path(
                            self.registry[name], self.tools, self.environment
                        )
                    )
                    for name in self.registry
                },
                is_busy=self.is_busy,
                path=self.environment.get("PATH", os.defpath),
            )
        elif hasattr(self.auth, "binaries"):
            self.auth.binaries.update(
                {
                    name: (
                        lambda name=name: binary_path(
                            self.registry[name], self.tools, self.environment
                        )
                    )
                    for name in self.registry
                }
            )
            self.auth.path = self.environment.get("PATH", os.defpath)

    def configure_urls(self, *, agent_url, lane_url):
        if self.processes:
            raise ValueError("Routing cannot change during an active native run")
        for value in (agent_url, lane_url):
            from urllib.parse import urlsplit

            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Native routing requires a credential-free server URL")
        self.agent_url, self.lane_url = agent_url.rstrip("/"), lane_url.rstrip("/")

    def _definition(self, harness_id):
        try:
            return self.registry[harness_id]
        except KeyError:
            raise ValueError("Unknown allowlisted harness") from None

    def _save(self, value):
        self.db.execute(
            "INSERT INTO native_sessions VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET payload=excluded.payload",
            (value["session_id"], value["run_id"], canonical(value)),
        )

    def _load(self, session_id):
        identifier(session_id, "session identifier")
        row = self.db.execute(
            "SELECT payload FROM native_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not row:
            raise KeyError("Unknown native session")
        return json.loads(row[0])

    @staticmethod
    def _public(value):
        return {
            key: value.get(key)
            for key in (
                "session_id",
                "run_id",
                "harness_id",
                "account",
                "host",
                "instance_id",
                "auth_mode",
                "version",
                "model",
                "state",
                "started_at",
                "finished_at",
                "exit_code",
                "stop_reason",
                "error",
                "output_truncated",
                "output_suppressed",
                "stdout_bytes",
                "stderr_bytes",
                "billing",
                "transcript_ref",
            )
        }

    def get(self, session_id):
        return self._public(self._load(session_id))

    def list(self, run_id=None):
        rows = self.db.execute("SELECT payload FROM native_sessions ORDER BY rowid DESC LIMIT 200")
        return [
            self._public(value)
            for row in rows
            if (value := json.loads(row[0])) and (not run_id or value["run_id"] == run_id)
        ]

    def is_busy(self, harness_id, instance_id="default", host=None):
        """Whether the account this harness would sign in with already runs a session.

        A harness that plays on a host (ASSAY on Claude Code) uses the host's account.
        """
        account = host or harness_id
        return any(
            value.get("account", value["harness_id"]) == account
            and value["instance_id"] == instance_id
            and value["state"] not in TERMINAL
            for value in self.list()
        )

    @staticmethod
    def _configured_output_limit(config, model):
        declared = config.get("native_output_token_limit")
        prices = config.get("model_pricing") or {}
        maximum = prices.get("models", prices).get(model, {}).get("max_output_tokens")
        if (
            type(declared) is not int
            or type(maximum) is not int
            or not 1 <= declared <= 1_000_000
            or maximum < 1
        ):
            return None
        return min(declared, maximum)

    def output_limit(self, run_id, model):
        """A frozen output bound only for this server's actual live Codex API process."""
        row = self.db.execute(
            "SELECT payload FROM native_sessions WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        process = self.processes.get(value["session_id"])
        run = self.store.get_run(run_id)
        config = run["config"]
        bound = value.get("output_token_limit")
        if (
            value.get("harness_id") != "codex"
            or run["state"] != "running"
            or value.get("auth_mode") != "api_key"
            or value.get("state") != "running"
            or process is None
            or process.returncode is not None
            or process.pid != value.get("pid")
            or _birth(process.pid) != value.get("birth")
            or value.get("model") != config.get("model_snapshot_id")
            or model != value.get("model")
            or type(bound) is not int
            or not 1 <= bound <= 1_000_000
            or bound != self._configured_output_limit(config, model)
        ):
            return None
        try:
            return bound if os.getpgid(process.pid) == process.pid else None
        except ProcessLookupError:
            return None

    def _auth(self):
        return self.auth or getattr(self.world, "services", {}).get("harness_auth")

    async def _version(self, manifest, binary):
        home = self.root / "discovery-home"
        home.mkdir(mode=0o700, exist_ok=True)
        env = base_environment(self.environment, home)
        env.update(
            {
                name: self.environment[name]
                for name in manifest.passthrough
                if name in self.environment
            }
        )
        result = await self._bounded_command(
            [binary, *manifest.version_args], environment=env, timeout=10
        )
        text = result["output"]
        match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\b", text)
        return match[0] if result["exit_code"] == 0 and match else None

    def _api_ready(self, definition):
        """Whether the key of the provider whose models this harness uses is set."""
        if not definition.provider:
            return False
        key = f"WORLD_{definition.provider.upper()}_API_KEY"
        settings = self.settings or getattr(self.world, "services", {}).get("settings")
        if settings and hasattr(settings, "resolve"):
            try:
                return bool(settings.resolve(key))
            except (KeyError, ValueError):
                return False
        return bool(self.environment.get(key))

    async def library(self, refresh=False, instance_id="default"):
        del refresh  # Discovery is deliberately read-only; nothing is installed or logged in.
        identifier(instance_id, "instance identifier")
        items = []
        for definition in self.registry.values():
            binary = binary_path(definition, self.tools, self.environment)
            version = await self._version(definition, binary) if binary else None
            supported = bool(version) and (
                not definition.pinned_version or version == definition.pinned_version
            )
            auth = self._auth()
            connection = (
                await auth.status(definition.id, instance_id)
                if auth and binary and "subscription" in definition.auth_modes
                else {"state": "auth_required", "auth_method": "unknown"}
            )
            install = self.db.execute(
                "SELECT payload FROM native_installs WHERE harness_id=?", (definition.id,)
            ).fetchone()
            items.append(
                {
                    "id": definition.id,
                    "name": definition.name,
                    "installed": supported,
                    "version": version,
                    "version_supported": supported,
                    "auth_modes": list(definition.auth_modes),
                    "api_ready": self._api_ready(definition),
                    "subscription_auth": connection,
                    "install_status": {
                        key: value
                        for key, value in json.loads(install[0]).items()
                        if key not in {"pid", "birth"}
                    }
                    if install
                    else {"state": "not_started"},
                    "package": definition.package,
                    "pinned_version": definition.pinned_version,
                    "requirements": {
                        "node": bool(shutil.which("node", path=self.environment.get("PATH"))),
                        "npm": bool(shutil.which("npm", path=self.environment.get("PATH"))),
                    },
                    "isolation": "cooperative",
                    "subscription_model_cost": "estimated"
                    if definition.transcript
                    else "unmeasured",
                    "kind": definition.kind,
                    "provider": definition.provider,
                    "efforts": list(definition.efforts),
                    "transcript": definition.transcript,
                    "description": definition.description,
                    "hosts": list(definition.hosts),
                }
            )
        return {"items": items}

    async def _bounded_command(self, argv, *, environment, timeout, on_output=None, on_start=None):
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        if on_start:
            on_start(process)
        output = bytearray()

        async def drain():
            while chunk := await process.stdout.read(8192):
                if len(output) < 128 * 1024:
                    output.extend(chunk[: 128 * 1024 - len(output)])
                if on_output:
                    on_output(_sanitize(chunk.decode(errors="replace")))

        reader = asyncio.create_task(drain())
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except (TimeoutError, asyncio.CancelledError):
            await self._terminate(process)
            await reader
            raise
        await self._terminate(process)
        await reader
        return {
            "exit_code": process.returncode,
            "output": _sanitize(output.decode(errors="replace")),
        }

    def _install_state(self, harness_id, value):
        self.db.execute(
            "INSERT INTO native_installs VALUES (?,?) ON CONFLICT(harness_id) DO UPDATE SET payload=excluded.payload",
            (harness_id, canonical(value)),
        )

    async def install(self, harness_id):
        definition = self._definition(harness_id)
        if not definition.package or not definition.pinned_version:
            raise ValueError("This developer harness has no supported installer")
        if (
            any(
                item["harness_id"] == harness_id and item["state"] not in TERMINAL
                for item in self.list()
            )
            or harness_id in self.install_tasks
            and not self.install_tasks[harness_id].done()
        ):
            raise ValueError("Harness is busy")
        npm = shutil.which("npm", path=self.environment.get("PATH"))
        node = shutil.which("node", path=self.environment.get("PATH"))
        if not npm or not node:
            value = {
                "state": "failed",
                "error": "node_npm_required",
                "message": "Install Node.js and npm on the server, then retry this pinned installer.",
                "package": definition.package + "@" + definition.pinned_version,
            }
            self._install_state(harness_id, value)
            return value
        value = {
            "state": "installing",
            "started_at": now(),
            "package": definition.package + "@" + definition.pinned_version,
            "progress": "Installing pinned package with npm integrity verification",
        }
        self._install_state(harness_id, value)
        self.install_tasks[harness_id] = asyncio.create_task(self._install(definition, npm, value))
        return value

    async def _install(self, definition, npm, value):
        def started(process):
            value.update(pid=process.pid, birth=_birth(process.pid))
            self._install_state(definition.id, value)

        def output(text):
            value["detail"] = (value.get("detail", "") + text)[-4000:]
            self._install_state(definition.id, value)

        try:
            home = self.root / "installer-home"
            if home.is_symlink():
                raise ValueError("Installer home must not be symlinked")
            home.mkdir(mode=0o700, exist_ok=True)
            env = base_environment(self.environment, home)
            for key in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "SSL_CERT_FILE",
                "NODE_EXTRA_CA_CERTS",
            ):
                if self.environment.get(key):
                    env[key] = self.environment[key]
            npm_configs = [home / "user.npmrc", home / "global.npmrc"]
            for path in npm_configs:
                if path.is_symlink():
                    raise ValueError("Installer configuration must not be symlinked")
                path.write_text("")
                path.chmod(0o600)
            env.update(
                NPM_CONFIG_USERCONFIG=str(npm_configs[0]),
                NPM_CONFIG_GLOBALCONFIG=str(npm_configs[1]),
            )
            result = await self._bounded_command(
                [
                    npm,
                    "install",
                    "--prefix",
                    str(self.tools / definition.id),
                    "--registry",
                    "https://registry.npmjs.org",
                    "--no-audit",
                    "--no-fund",
                    "--ignore-scripts",
                    "--save-exact",
                    definition.package + "@" + definition.pinned_version,
                ],
                environment=env,
                timeout=300,
                on_start=started,
                on_output=output,
            )
            binary = binary_path(
                definition,
                self.tools,
                {
                    key: item
                    for key, item in self.environment.items()
                    if key != definition.env_binding
                },
            )
            version = (
                await self._version(definition, binary)
                if result["exit_code"] == 0 and binary
                else None
            )
            if result["exit_code"] or version != definition.pinned_version:
                value.update(
                    state="failed",
                    error="installation_failed"
                    if result["exit_code"]
                    else "installed_version_unverified",
                    progress="Pinned installation did not produce the required usable CLI",
                    detail=result["output"][-4000:],
                )
            else:
                value.update(
                    state="installed", version=version, progress="Installed CLI version verified"
                )
        except (OSError, TimeoutError, ValueError):
            value.update(
                state="failed",
                error="installer_unavailable_or_timed_out",
                progress="Installation could not complete",
            )
        except asyncio.CancelledError:
            value.update(state="interrupted", error="server_stopped")
            raise
        finally:
            value.pop("pid", None)
            value.pop("birth", None)
            value["finished_at"] = now()
            self._install_state(definition.id, value)

    async def launch(
        self, run_info, harness_id, *, auth_mode="api_key", model=None, instance_id="default"
    ):
        definition = self._definition(harness_id)
        identifier(instance_id, "instance identifier")
        model = model_name(model)
        run_id = run_info.get("run_id")
        configured = self.store.get_run(run_id)["config"].get("harness_id", "")
        # A harness that plays through another (ASSAY) launches its host's client as the
        # player: the host's connection, models, effort and account are the session's.
        hosted = bool(definition.hosts)
        if hosted:
            host_id = harness_account(configured)
            if configured.split("@", 1)[0] != harness_id or host_id not in definition.hosts:
                raise ValueError("The run does not name a host this harness plays through")
            client = self._definition(host_id)
        else:
            client = definition
        if auth_mode not in client.auth_modes:
            raise ValueError("Unsupported authentication mode")
        run = self.store.get_run(run_id)
        config = run["config"]
        if run["state"] != "running" or not self.world.capabilities.verify(
            run_id, run_info.get("run_token", "")
        ):
            raise ValueError("A running run and actual current capability are required")
        if config.get("isolation_profile") != "cooperative":
            raise ValueError("This host-native launcher requires the cooperative profile")
        if config.get("model_billing_mode", auth_mode) != auth_mode:
            raise ValueError("Authentication mode differs from the run's registered billing mode")
        if auth_mode == "subscription" and (
            config.get("research_mode") != "development"
            or config.get("series_id")
            or config.get("measurement_of")
        ):
            raise ValueError(
                "Native subscription model costs are estimated, not measured; research runs are not supported"
            )
        if auth_mode == "api_key" and (not model or model != config.get("model_snapshot_id")):
            raise ValueError("API mode requires the run's explicitly selected model snapshot")
        # A transcript-metered session prices each message from the client's own reports.
        estimated = config.get("model_metering") == "transcript"
        # The effort is the run's registered condition, never a launch-time choice.
        effort = config.get("effort")
        if effort is not None and effort not in client.efforts:
            raise ValueError("This harness does not accept the run's effort level")
        versions = {}
        for item in {definition, client}:
            item_binary = binary_path(item, self.tools, self.environment)
            item_version = await self._version(item, item_binary) if item_binary else None
            if not item_version:
                raise ValueError("The selected native CLI is not installed or usable")
            if item.pinned_version and item_version != item.pinned_version:
                raise ValueError("Install the supported pinned native CLI version before launching")
            versions[item.id] = (item_binary, item_version)
        binary, version = versions[client.id]
        profile = None
        if auth_mode == "subscription":
            auth = self._auth()
            connection = await auth.status(client.id, instance_id) if auth else {}
            if (
                connection.get("state") != "connected"
                or connection.get("auth_method") != "subscription"
            ):
                raise ValueError("Connect a legitimate native subscription account first")
            profile = auth.profile_path(client.id, instance_id)
        account = (
            harness_account(configured) if configured.split("@", 1)[0] == harness_id else harness_id
        )
        async with self._launch_lock:
            if (
                self.is_busy(account, instance_id)
                or self.db.execute(
                    "SELECT 1 FROM native_sessions WHERE run_id=?", (run_id,)
                ).fetchone()
            ):
                raise ValueError("This profile or World run already has a native session")
            session_id = new_id("hs")
            root = self.root / session_id
            root.mkdir(mode=0o700)
            goal = config.get("goal")
            goal = goal.get("text", "") if isinstance(goal, dict) else goal or ""
            # An API-key session's model calls carry a lane-only capability.
            lane_token = (
                self.world.capabilities.issue_lane(run_id)
                if auth_mode == "api_key" and hasattr(self.world.capabilities, "issue_lane")
                else None
            )
            actual_info = {
                **run_info,
                "lane_token": lane_token,
                "preamble": self.world.run_config(run_id).get(
                    "preamble", run_info.get("preamble", "")
                ),
                "goal": goal,
            }
            if not actual_info["preamble"]:
                raise ValueError("The actual World briefing is required")
            player, assay = None, None
            if hosted:
                assay, player = await self._prepare_assay(
                    root, run_id, config, actual_info, versions[definition.id][0]
                )
            argv, environment, stdin = launch_configuration(
                client,
                binary,
                root=root,
                work=self.store.run_dir(run_id) / "work",
                run_info=actual_info,
                model=model,
                auth_mode=auth_mode,
                profile=profile,
                agent_url=self.agent_url,
                lane_url=self.lane_url,
                environment=self.environment,
                stream_usage=estimated,
                effort=effort,
                player=player,
            )
            value = {
                "session_id": session_id,
                "run_id": run_id,
                "harness_id": harness_id,
                "account": account,
                "instance_id": instance_id,
                "auth_mode": auth_mode,
                "model": model,
                "effort": effort,
                "output_token_limit": self._configured_output_limit(config, model),
                "state": "starting",
                "started_at": now(),
                "version": version,
                "billing": {
                    "model": "estimated"
                    if estimated
                    else "unmeasured"
                    if auth_mode == "subscription"
                    else "world_lane",
                    "dollar_cap_scope": "model_estimate_and_world_tools"
                    if estimated
                    else "world_tools_only"
                    if auth_mode == "subscription"
                    else "model_and_world_tools",
                },
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "output_truncated": False,
                "output_suppressed": not config.get("capture_model_io", True),
                "root": str(root),
                "pid": None,
                "birth": None,
            }
            if hosted:
                value.update(
                    host=client.id,
                    host_version=version,
                    version=versions[definition.id][1],
                    assay={"run_dir": player["cwd"], "registry_hash": player["registry_hash"]},
                )
            self._save(value)
            self.drained[session_id] = asyncio.Event()
            proxy = run_info.get("proxy", {})
            self._secrets[session_id] = [
                run_info["run_token"],
                lane_token,
                proxy.get("password"),
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=player["cwd"] if player else self.store.run_dir(run_id) / "work",
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError:
                value.update(state="failed", error="process_start_failed", finished_at=now())
                self._save(value)
                self.drained[session_id].set()
                if assay:
                    await assay.stop()
                raise ValueError("The native process could not be started") from None
            if assay:
                self.assay_runs[session_id] = assay
                self._spend_reporters[session_id] = asyncio.create_task(
                    self._report_assay_spend(run_id, assay)
                )
            self.processes[session_id] = process
            value.update(state="running", pid=process.pid, birth=_birth(process.pid))
            self._save(value)
            if auth_mode == "subscription":
                self.store.update_run(run_id, tainted=True)
            self.store.append_record(
                run_id,
                "harness_started",
                session_id=session_id,
                harness_id=harness_id,
                version=value["version"],
                auth_mode=auth_mode,
                model=model,
                effort=effort,
                **(
                    {
                        "host": client.id,
                        "host_version": version,
                        "assay_registry_hash": player["registry_hash"],
                    }
                    if hosted
                    else {}
                ),
                billing=value["billing"],
                argv_hash=digest(argv),
                prompt_hash=digest(stdin),
                native_layout=layout_context(self.store.run_dir(run_id) / "work")[0],
                native_layout_context_hash=digest(
                    layout_context(self.store.run_dir(run_id) / "work")[1]
                )
                if layout_context(self.store.run_dir(run_id) / "work")[1].encode() in stdin
                else None,
            )
            self.tasks[session_id] = asyncio.create_task(
                self._run_session(session_id, process, stdin)
            )
            return self._public(value)

    async def _prepare_assay(self, root, run_id, config, info, launcher):
        """Operate an ASSAY run for this session; return it and its player's launch."""
        python = self.environment.get("ASSAY_PYTHON")
        if not python:
            raise ValueError("ASSAY's interpreter is not configured on this instance")
        assay = AssayRun(
            launcher, python, root, base_environment(self.environment, root / "operator")
        )
        objective = info["goal"] or "No objective: you decide what to pursue."
        player = await assay.prepare(
            tools=manifest_tools(self.world.manifest(run_id)),
            goal=objective,
            actions=int((config.get("harness_options") or {}).get("actions", 1000)),
            usd=assay_budget(self.store.reconcile(run_id)["cap_usd"]),
            notes=self._previous_assay_notes(config),
            world={
                "WORLD_BASE_URL": self.agent_url,
                "WORLD_RUN_ID": run_id,
                "WORLD_RUN_TOKEN": info["run_token"],
            },
            deny=[re.escape(urlsplit(self.agent_url).netloc)],
        )
        player["prompt"] = player_prompt(info["preamble"], info["goal"])
        return assay, player

    def _previous_assay_notes(self, config):
        """The notes an ASSAY player left in this life's previous session, if any."""
        life_id = config.get("life_id")
        lives = getattr(self.world, "services", {}).get("lives")
        if not life_id or lives is None:
            return ""
        for session in reversed(lives.get(life_id)["sessions"]):
            for record in reversed(self.store.records(session["run_id"])):
                if record["kind"] == "assay_journal":
                    return record.get("notes") or ""
        return ""

    async def _report_assay_spend(self, run_id, assay):
        while True:
            await asyncio.sleep(ASSAY_SPEND_INTERVAL_S)
            try:
                await assay.report_spend(self.store.total_spent(run_id))
            except (ValueError, OSError):
                continue

    async def _finish_assay(self, session_id, run_id):
        """Close the session's ASSAY run and record its journal in the world's record."""
        assay = self.assay_runs.pop(session_id, None)
        if assay is None:
            return
        reporter = self._spend_reporters.pop(session_id, None)
        if reporter:
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
        try:
            await assay.report_spend(self.store.total_spent(run_id))
            summary = await assay.finish()
        except (ValueError, OSError) as error:
            await assay.stop()
            summary = {"error": str(error)[:300]}
        summary["notes"] = assay.notes()[:ASSAY_NOTES_CHARS]
        if self.store.get_run(run_id)["state"] not in {"sealed", "ended", "killed"}:
            self.store.append_record(run_id, "assay_journal", session_id=session_id, **summary)

    async def _capture(self, session_id, stream, channel):
        value = self._load(session_id)
        root = Path(value["root"])
        pending, total, stored = bytearray(), 0, 0
        discard = False
        path = root / ("stdout.jsonl" if channel == "stdout" else "stderr.log")
        # Every native stdout line reaches the meter: it prices metered sessions and
        # records provider errors for all of them.
        meter = self.world.services.get("transcript_meter") if channel == "stdout" else None
        metered, overflowed, filtered = bytearray(), False, False
        with path.open("xb") as transcript:
            path.chmod(0o600)

            async def emit(raw):
                nonlocal stored
                current = self._load(session_id)
                if current["output_suppressed"] or stored >= MAX_LOG:
                    if stored >= MAX_LOG:
                        current["output_truncated"] = True
                        self._save(current)
                    return
                text = _sanitize(raw.decode(errors="replace"), self._secrets.get(session_id, ()))
                encoded = (text + "\n").encode()
                if stored + len(encoded) > MAX_LOG:
                    current["output_truncated"] = True
                    self._save(current)
                    return
                transcript.write(encoded)
                transcript.flush()
                stored += len(encoded)
                event = (canonical({"channel": channel, "text": text, "at": now()}) + "\n").encode()
                if current.get("event_bytes", 0) + len(event) <= MAX_LOG:
                    events_path = root / "events.jsonl"
                    with events_path.open("ab") as events:
                        events.write(event)
                    events_path.chmod(0o600)
                    current["event_bytes"] = current.get("event_bytes", 0) + len(event)
                else:
                    current["output_truncated"] = True
                self._save(current)

            while chunk := await stream.read(8192):
                total += len(chunk)
                for part in chunk.splitlines(keepends=True):
                    if meter is not None:
                        if len(metered) + len(part) > METER_LINE:
                            if not overflowed:
                                meter.overflow(value["run_id"])
                            overflowed = True
                            metered.clear()
                        elif not overflowed:
                            metered.extend(part)
                        if part.endswith(b"\n"):
                            if metered and not overflowed:
                                meter.observe(value["run_id"], metered.decode(errors="replace"))
                            metered.clear()
                            overflowed = False
                    if not discard:
                        pending.extend(part[: max(0, MAX_LINE - len(pending))])
                        if len(pending) >= MAX_LINE:
                            await emit(bytes(pending) + b" [output line truncated]")
                            pending.clear()
                            discard = True
                            current = self._load(session_id)
                            current["output_truncated"] = True
                            self._save(current)
                    if part.endswith(b"\n"):
                        if pending and channel == "stdout" and _token_chunk(pending):
                            # Partial-message chunks were metered above; storing every
                            # token delta would fill the transcript cap.
                            if not filtered:
                                filtered = True
                                current = self._load(session_id)
                                current["stdout_filtered"] = True
                                self._save(current)
                        elif pending:
                            await emit(bytes(pending).rstrip(b"\r\n"))
                        pending.clear()
                        discard = False
            if pending:
                await emit(bytes(pending))
            if meter is not None and metered and not overflowed:
                meter.observe(value["run_id"], metered.decode(errors="replace"))
        current = self._load(session_id)
        current[channel + "_bytes"] = total
        self._save(current)

    async def _run_session(self, session_id, process, stdin):
        readers = [
            asyncio.create_task(self._capture(session_id, process.stdout, "stdout")),
            asyncio.create_task(self._capture(session_id, process.stderr, "stderr")),
        ]
        monitor = asyncio.create_task(self._monitor(session_id, process))
        capture_error = None
        try:
            try:
                process.stdin.write(stdin)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
            waiter = asyncio.create_task(process.wait())
            while not waiter.done():
                done, _ = await asyncio.wait(
                    {waiter, *(reader for reader in readers if not reader.done())},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    task.result()
            await waiter
            await self._terminate(process)
            await asyncio.gather(*readers)
        except Exception as error:  # noqa: BLE001 - a capture failure must stop owned work and remain visible
            capture_error = type(error).__name__
            await self._terminate(process)
            await asyncio.gather(*readers, return_exceptions=True)
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            # The player has stopped; its ASSAY run is audited and closed before the seal.
            await self._finish_assay(session_id, self._load(session_id)["run_id"])
            self.drained[session_id].set()
        value = self._load(session_id)
        value.update(
            state="failed"
            if capture_error
            else "stopped"
            if value.get("stop_reason")
            else "completed"
            if process.returncode == 0
            else "failed",
            exit_code=process.returncode,
            finished_at=now(),
        )
        if value["state"] == "failed":
            value["error"] = "output_capture_failed" if capture_error else "native_process_failed"
        root = Path(value["root"])
        value["transcript_ref"] = "native-harnesses/" + session_id + "/stdout.jsonl"
        self._save(value)
        run = self.store.get_run(value["run_id"])
        if run["state"] not in {"sealed", "ended", "killed"}:
            self.store.append_record(
                value["run_id"],
                "harness_exit",
                session_id=session_id,
                exit_code=process.returncode,
                stdout_hash=digest((root / "stdout.jsonl").read_bytes())
                if (root / "stdout.jsonl").exists()
                else None,
                stderr_hash=digest((root / "stderr.log").read_bytes())
                if (root / "stderr.log").exists()
                else None,
                capture_truncated=value["output_truncated"],
                capture_suppressed=value["output_suppressed"],
                stdout_filtered=value.get("stdout_filtered", False),
            )
            if capture_error:
                self.store.update_run(value["run_id"], tainted=True)
            if session_id not in self._stop_from_world:
                await self.world.end_run(
                    value["run_id"],
                    reason="operator_stop" if value.get("stop_reason") else "harness_stopped",
                    truncated=process.returncode != 0,
                )
        self.processes.pop(session_id, None)
        self._secrets.pop(session_id, None)

    async def _monitor(self, session_id, process):
        paused = False
        while process.returncode is None:
            value = self._load(session_id)
            state = self.store.get_run(value["run_id"])["state"]
            if state in {"sealed", "ended", "killed"}:
                value["stop_reason"] = "world_ended"
                self._save(value)
                await self._terminate(process)
                return
            if state == "frozen" and not paused:
                self._signal(process, signal.SIGSTOP)
                paused = True
            elif state == "running" and paused:
                self._signal(process, signal.SIGCONT)
                paused = False
            await asyncio.sleep(0.25)

    @staticmethod
    def _signal(process, signum):
        if process.returncode is None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

    async def _terminate(self, process):
        self._signal(process, signal.SIGCONT)
        self._signal(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            self._signal(process, signal.SIGKILL)
            await process.wait()
        finally:
            # MCP/worker descendants share this dedicated session's process
            # group. Do not leave them alive after the CLI parent exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def stop(self, session_id):
        value = self._load(session_id)
        process = self.processes.get(session_id)
        if value["state"] in TERMINAL:
            return self._public(value)
        if not process:
            raise ValueError("Session process is not owned by this server; recovery is required")
        value.update(state="stopping", stop_reason="operator_stop")
        self._save(value)
        await self._terminate(process)
        task = self.tasks.get(session_id)
        if task and task is not asyncio.current_task():
            await task
        return self.get(session_id)

    async def stop_for_run(self, run_id):
        """World's before-seal hook. Drain only; never recursively end the run."""
        row = self.db.execute(
            "SELECT session_id FROM native_sessions WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            return
        session_id = row[0]
        process = self.processes.get(session_id)
        if not process:
            return
        self._stop_from_world.add(session_id)
        value = self._load(session_id)
        if process.returncode is None:
            value.update(state="stopping", stop_reason=value.get("stop_reason") or "world_ended")
            self._save(value)
            await self._terminate(process)
        if session_id in self.drained:
            await self.drained[session_id].wait()

    def logs(self, session_id, offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Invalid log cursor or limit")
        value = self._load(session_id)
        path = Path(value["root"]) / "events.jsonl"
        events = (
            [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        )
        return {
            "session_id": session_id,
            "events": events[offset : offset + limit],
            "cursor": min(len(events), offset + limit),
            "truncated": value["output_truncated"],
            "suppressed": value["output_suppressed"],
        }

    async def recover(self):
        for row in self.db.execute("SELECT payload FROM native_sessions").fetchall():
            value = json.loads(row[0])
            if value["state"] in TERMINAL or value["session_id"] in self.processes:
                continue
            pid, birth = value.get("pid"), value.get("birth")
            killed = await self._recover_group(pid, birth)
            if value.get("assay"):
                # Its daemon outlived the world; a run without a live daemon cannot resume.
                definition = self.registry.get(value["harness_id"])
                launcher = (
                    binary_path(definition, self.tools, self.environment) if definition else None
                )
                python = self.environment.get("ASSAY_PYTHON")
                if launcher and python:
                    root = Path(value["root"])
                    await AssayRun(
                        launcher,
                        python,
                        root,
                        base_environment(self.environment, root / "operator"),
                    ).stop()
            value.update(
                state="interrupted",
                stop_reason="server_restarted",
                finished_at=now(),
                error="process_exit_unobserved",
                recovery_stop_sent=killed,
            )
            self._save(value)
            run = self.store.get_run(value["run_id"])
            if run["state"] not in {"sealed", "ended", "killed"}:
                await self.world.end_run(value["run_id"], reason="world_fault", truncated=True)
        for row in self.db.execute("SELECT harness_id,payload FROM native_installs").fetchall():
            value = json.loads(row[1])
            if value["state"] == "installing":
                pid, birth = value.get("pid"), value.get("birth")
                await self._recover_group(pid, birth)
                value.update(state="interrupted", error="server_restarted", finished_at=now())
                self._install_state(row[0], value)
        if self.auth and hasattr(self.auth, "recover"):
            await self.auth.recover()

    async def _recover_group(self, pid, birth):
        if not pid or not birth or _birth(pid) != birth:
            return False
        try:
            if os.getpgid(pid) != pid or Path(f"/proc/{pid}").stat().st_uid != os.getuid():
                return False
            os.killpg(pid, signal.SIGCONT)
            os.killpg(pid, signal.SIGTERM)
            await asyncio.sleep(0.05)
            if _birth(pid) in {None, birth}:
                os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, FileNotFoundError):
            pass
        return True

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        for session_id in list(self.processes):
            await self.stop(session_id)
        for task in self.install_tasks.values():
            if not task.done():
                task.cancel()
        if self.install_tasks:
            await asyncio.gather(*self.install_tasks.values(), return_exceptions=True)
        if self.auth and hasattr(self.auth, "aclose"):
            await self.auth.aclose()
        await super().aclose()
