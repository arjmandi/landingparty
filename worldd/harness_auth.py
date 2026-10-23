"""Managed native subscription login; credentials remain in private CLI profiles.

Only explicit ``start`` invokes an authentication flow. Readiness comes from the
official CLI's local status command, never from a browser callback or log text.
The public projection omits account identities and all native status JSON.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import pty
import re
import shutil
import signal
import sqlite3
import sys
import termios
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .storage import canonical_json, utc_now

PROVIDERS = {"claude-code": "anthropic", "codex": "openai"}
_INSTANCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
_URL = re.compile(r"https?://[^\s<>\"'\x1b]+")
_DEVICE = re.compile(r"\b[A-Z0-9]{3,6}-[A-Z0-9]{3,6}\b")
_SENSITIVE = re.compile(
    r"(?i)\btoken\b|access[_ -]?token|refresh[_ -]?token|id[_ -]?token|api[_ -]?key|authorization|bearer|password|set-cookie|cookie\s*[:=]"
)
_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{12,}(?:\.[A-Za-z0-9_-]+){1,2})\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_MAX_OUTPUT = 16_384


def _process_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _owner_alive(row):
    return bool(row.get("owner_pid") and _process_start(row["owner_pid"]) == row.get("owner_start"))


def _safe_url(harness_id, candidate):
    candidate = candidate.rstrip(".,;)")
    try:
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.fragment
        ):
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        if harness_id == "codex":
            if parsed.hostname != "auth.openai.com" or parsed.path not in {
                "/codex/device",
                "/device",
                "/activate",
            }:
                return None
            if set(query) - {"user_code"}:
                return None
            if any(not _DEVICE.fullmatch(value) for value in query.get("user_code", [])):
                return None
        else:
            if (
                parsed.hostname not in {"claude.ai", "platform.claude.com", "console.anthropic.com"}
                or parsed.path != "/oauth/authorize"
            ):
                return None
            allowed = {
                "client_id",
                "response_type",
                "redirect_uri",
                "scope",
                "code_challenge",
                "code_challenge_method",
                "state",
                "code",
                "organization",
            }
            if set(query) - allowed or any(
                value not in {"true", "1"} for value in query.get("code", [])
            ):
                return None
            for redirect in query.get("redirect_uri", []):
                target = urlsplit(redirect)
                remote = (
                    target.scheme == "https"
                    and target.hostname in {"platform.claude.com", "console.anthropic.com"}
                    and target.path == "/oauth/code/callback"
                    and target.port in {None, 443}
                )
                local = (
                    target.scheme == "http"
                    and target.hostname in {"localhost", "127.0.0.1", "::1"}
                    and target.path in {"/callback", "/oauth/callback", "/oauth/code/callback"}
                )
                if (
                    target.username
                    or target.password
                    or target.fragment
                    or target.query
                    or not (remote or local)
                ):
                    return None
        return candidate if len(candidate) <= 8192 else None
    except (ValueError, TypeError):
        return None


def _redact(line, secrets=()):
    line = _ANSI.sub("", line)
    line = "".join(character for character in line if character >= " " or character == "\t")
    for value in secrets:
        if value:
            line = line.replace(value, "[redacted]")
    if _SENSITIVE.search(line):
        return "[sensitive authentication output omitted]"
    line = _URL.sub("[authentication URL]", line)
    line = _SECRET.sub("[redacted]", line)
    line = _EMAIL.sub("[account identity omitted]", line)
    return line.replace("<", "‹").replace(">", "›")[:1000]


class HarnessAuthService:
    def __init__(
        self,
        world_or_data_dir,
        *,
        binaries=None,
        login_timeout_s=600,
        status_timeout_s=8,
        busy=None,
        is_busy=None,
        path=None,
    ):
        self.data_dir = Path(
            getattr(getattr(world_or_data_dir, "store", None), "data_dir", world_or_data_dir)
        ).resolve()
        self.root = self.data_dir / "harness-auth"
        if self.root.is_symlink():
            raise ValueError("Native authentication profiles must not be symlinked")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        if (self.root / "auth.sqlite3").is_symlink():
            raise ValueError("Native authentication metadata must not be symlinked")
        self.path = path if path is not None else os.environ.get("PATH", os.defpath)
        self._db_lock = threading.RLock()
        self.db = sqlite3.connect(
            self.root / "auth.sqlite3", isolation_level=None, check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS profiles (harness_id TEXT,instance_id TEXT,payload TEXT NOT NULL,PRIMARY KEY(harness_id,instance_id))"
        )
        (self.root / "auth.sqlite3").chmod(0o600)
        claude = shutil.which("claude")
        fallback = Path("/workspace/.cache/landing-party-tools/node_modules/.bin/claude")
        self.binaries = {
            "claude-code": claude or (str(fallback) if fallback.is_file() else None),
            "codex": shutil.which("codex"),
            **(binaries or {}),
        }
        self.login_timeout_s, self.status_timeout_s = (
            float(login_timeout_s),
            float(status_timeout_s),
        )
        if not 0 < self.login_timeout_s <= 1800 or not 0 < self.status_timeout_s <= 30:
            raise ValueError("Native authentication timeouts are bounded")
        self.busy = is_busy or busy
        self._locks, self._workers, self._processes, self._inputs, self._secrets = (
            {},
            {},
            {},
            {},
            {},
        )
        self._closed = False
        self._browser = self.root / "open-browser"
        if self._browser.is_symlink():
            raise ValueError("The managed browser helper must not be a symlink")
        self._browser.write_text(
            f"#!{sys.executable}\nimport sys\nfor value in sys.argv[1:]:\n if value.startswith('https://'): print(value, flush=True)\n"
        )
        self._browser.chmod(0o700)
        self._terminal = self.root / "native-terminal.py"
        if self._terminal.is_symlink():
            raise ValueError("The managed terminal helper must not be a symlink")
        self._terminal.write_text(
            "import fcntl,os,sys,termios\n"
            "fcntl.ioctl(0,termios.TIOCSCTTY,0)\n"
            "os.execv(sys.argv[1],sys.argv[1:])\n"
        )
        self._terminal.chmod(0o600)

    def _key(self, harness_id, instance_id):
        if (
            harness_id not in PROVIDERS
            or not isinstance(instance_id, str)
            or not _INSTANCE.fullmatch(instance_id)
        ):
            raise ValueError("Choose a supported native harness and a valid instance identifier")
        return harness_id, instance_id

    def _profile_base(self, harness_id, instance_id):
        self._key(harness_id, instance_id)
        target = self.root / harness_id / instance_id
        for directory in (
            target.parent,
            target,
            target / "config",
            target / "home",
            target / "tmp",
        ):
            if directory.resolve() != directory:
                raise ValueError("Native profiles must remain in their private owned directory")
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        return target

    def profile_path(self, harness_id, instance_id="default"):
        """Private launcher-only path; never include this in the public UI response."""
        return self._profile_base(harness_id, instance_id) / "config"

    def profile_env(self, harness_id, instance_id="default"):
        profile = self._profile_base(harness_id, instance_id)
        return {
            "CLAUDE_CONFIG_DIR" if harness_id == "claude-code" else "CODEX_HOME": str(
                profile / "config"
            )
        }

    def _environment(self, key):
        profile = self._profile_base(*key)
        return {
            "PATH": self.path,
            "HOME": str(profile / "home"),
            "XDG_CONFIG_HOME": str(profile / "home" / ".config"),
            "XDG_CACHE_HOME": str(profile / "home" / ".cache"),
            "TMPDIR": str(profile / "tmp"),
            "TERM": "xterm-256color",
            "NO_COLOR": "1",
            "LANG": "C.UTF-8",
            "BROWSER": str(self._browser),
            **self.profile_env(*key),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }

    def _binary(self, harness_id):
        value = self.binaries.get(harness_id)
        value = value() if callable(value) else value
        if value is None:
            return None
        path = Path(value)
        return (
            str(path.resolve())
            if path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
            else None
        )

    def _load(self, key):
        with self._db_lock:
            row = self.db.execute(
                "SELECT payload FROM profiles WHERE harness_id=? AND instance_id=?", key
            ).fetchone()
        return (
            json.loads(row[0])
            if row
            else {
                "harness_id": key[0],
                "instance_id": key[1],
                "state": "auth_required",
                "auth_method": "unknown",
                "log": [],
            }
        )

    def _save(self, row):
        row["updated_at"] = utc_now()
        with self._db_lock:
            self.db.execute(
                "INSERT INTO profiles VALUES(?,?,?) ON CONFLICT(harness_id,instance_id) DO UPDATE SET payload=excluded.payload",
                (row["harness_id"], row["instance_id"], canonical_json(row)),
            )
        return row

    def _public(self, row):
        result = {
            key: row[key]
            for key in (
                "harness_id",
                "instance_id",
                "state",
                "auth_method",
                "operation_id",
                "started_at",
                "updated_at",
                "reason",
                "log",
            )
            if key in row
        }
        result.update(
            provider=PROVIDERS[row["harness_id"]],
            available=self._binary(row["harness_id"]) is not None,
        )
        if row["state"] == "connecting":
            result.update(
                {
                    key: row[key]
                    for key in ("auth_url", "device_code", "input_required")
                    if row.get(key)
                }
            )
        result["requires_code"] = result["awaiting_code"] = row[
            "state"
        ] == "connecting" and row.get("input_required", False)
        return result

    async def _guard(self, key):
        if self._closed:
            raise ValueError("Native authentication service is closed")
        if self.busy:
            value = self.busy(*key)
            if hasattr(value, "__await__"):
                value = await value
            if value:
                raise ValueError(
                    "Stop the active native harness before changing its account connection"
                )

    async def _terminate(self, process):
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 1)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        # Children inherited this dedicated group even if its leader exited.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    async def _command(self, key, arguments):
        binary = self._binary(key[0])
        if binary is None:
            return None, "", ""
        process = await asyncio.create_subprocess_exec(
            binary,
            *arguments,
            cwd=self._profile_base(*key),
            env=self._environment(key),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def read(stream):
            chunks, size = [], 0
            while data := await stream.read(4096):
                if size < _MAX_OUTPUT:
                    chunks.append(data[: _MAX_OUTPUT - size])
                size += len(data)
            if size > _MAX_OUTPUT:
                raise ValueError("Native authentication status exceeded its output limit")
            return b"".join(chunks).decode("utf-8", "replace")

        outputs = [
            asyncio.create_task(read(process.stdout)),
            asyncio.create_task(read(process.stderr)),
        ]
        try:
            await asyncio.wait_for(process.wait(), self.status_timeout_s)
            stdout, stderr = await asyncio.gather(*outputs)
            return process.returncode, stdout, stderr
        finally:
            await self._terminate(process)
            for task in outputs:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*outputs, return_exceptions=True)

    async def _probe(self, key):
        arguments = ["auth", "status", "--json"] if key[0] == "claude-code" else ["login", "status"]
        try:
            code, stdout, stderr = await self._command(key, arguments)
            if code is None:
                return False, "unknown", "native_cli_missing"
            if key[0] == "claude-code":
                payload = json.loads(stdout)
                if not isinstance(payload, dict):
                    return False, "unknown", "native_status_unavailable"
                method = str(payload.get("authMethod", "")).lower()
                if (
                    code == 0
                    and payload.get("loggedIn") is True
                    and (
                        payload.get("apiProvider") is None
                        or payload.get("apiProvider") == "firstParty"
                    )
                    and method in {"claude.ai", "claudeai", "claude_ai", "oauth"}
                ):
                    return True, "subscription", None
                if payload.get("loggedIn") is True and (
                    "api" in method or payload.get("apiKeySource")
                ):
                    return False, "api_key", "subscription_login_required"
            else:
                text = _ANSI.sub("", stdout + "\n" + stderr)
                if code == 0 and re.search(r"(?im)^Logged in using ChatGPT\s*$", text):
                    return True, "subscription", None
                if re.search(r"(?im)^Logged in using (?:an? )?API key\b", text):
                    return False, "api_key", "subscription_login_required"
            return False, "unknown", "subscription_login_required"
        except (OSError, ValueError, TimeoutError):
            return False, "unknown", "native_status_unavailable"

    async def status(self, harness_id, instance_id="default"):
        key = self._key(harness_id, instance_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            row = self._load(key)
            if row["state"] == "connecting":
                if row.get("deadline", 0) <= time.time() or not _owner_alive(row):
                    await self._cancel(
                        key,
                        state="failed",
                        reason="login_timeout" if _owner_alive(row) else "login_interrupted",
                    )
                    self._wipe(key)
                    row = self._load(key)
                return self._public(row)
            connected, method, reason = await self._probe(key)
            # Another server worker may have claimed the flow while this local
            # status subprocess ran. Its durable claim takes precedence.
            current = self._load(key)
            if current != row:
                return self._public(current)
            row["auth_method"] = method
            if connected:
                row.update(state="connected", reason=None)
            elif row["state"] not in {"failed", "canceled"}:
                row.update(state="auth_required", reason=reason)
            return self._public(self._save(row))

    async def start(self, harness_id, instance_id="default"):
        key = self._key(harness_id, instance_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            await self._guard(key)
            row = self._load(key)
            if row["state"] == "connecting" and row.get("deadline", 0) > time.time():
                return self._public(row)
            connected, method, _reason = await self._probe(key)
            if connected:
                row.update(state="connected", auth_method=method, reason=None)
                return self._public(self._save(row))
            binary = self._binary(harness_id)
            if binary is None:
                row.update(state="failed", reason="native_cli_missing")
                return self._public(self._save(row))
            if row["state"] == "connecting":
                await self._cancel(key, state="failed", reason="login_timeout")
            operation = uuid.uuid4().hex
            row = {
                "harness_id": harness_id,
                "instance_id": instance_id,
                "state": "connecting",
                "auth_method": "unknown",
                "operation_id": operation,
                "started_at": utc_now(),
                "deadline": time.time() + self.login_timeout_s,
                "owner_pid": os.getpid(),
                "owner_start": _process_start(os.getpid()),
                "log": ["Starting the official subscription sign-in flow."],
            }
            with self._db_lock:
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    current = self._load(key)
                    if (
                        current["state"] == "connecting"
                        and current.get("deadline", 0) > time.time()
                    ):
                        self.db.execute("COMMIT")
                        return self._public(current)
                    self._save(row)
                    self.db.execute("COMMIT")
                except BaseException:
                    self.db.execute("ROLLBACK")
                    raise
            self._workers[key] = asyncio.create_task(self._login(key, operation))
            return self._public(row)

    def _output(self, key, operation, line, *, partial=False):
        row = self._load(key)
        if row.get("operation_id") != operation or row["state"] != "connecting":
            return
        plain = _ANSI.sub("", line)
        prompt = key[0] == "claude-code" and re.search(
            r"(?i)(paste|enter).{0,40}(code|verification)", plain
        )
        if partial:
            if prompt and not row.get("input_required"):
                row["input_required"] = True
                self._save(row)
            return
        for candidate in _URL.findall(plain):
            approved = _safe_url(key[0], candidate)
            if approved:
                row["auth_url"] = approved
        if key[0] == "codex" and row.get("auth_url"):
            match = _DEVICE.search(plain)
            if match:
                row["device_code"] = match[0]
        if prompt:
            row["input_required"] = True
        cleaned = _redact(plain, self._secrets.get(key, ()))
        if cleaned.strip():
            row["log"] = [*row.get("log", []), cleaned][-40:]
            while sum(map(len, row["log"])) > 8000:
                row["log"].pop(0)
        self._save(row)

    async def _read_pty(self, descriptor):
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def ready():
            if future.done():
                return
            try:
                data = os.read(descriptor, 4096)
            except BlockingIOError:
                return
            except OSError as error:
                if error.errno not in {errno.EIO, errno.EBADF}:
                    future.set_exception(error)
                    return
                data = b""
            if not future.done():
                future.set_result(data)

        loop.add_reader(descriptor, ready)
        try:
            return await future
        finally:
            loop.remove_reader(descriptor)

    async def _consume(self, key, operation, descriptor):
        pending = ""
        while data := await self._read_pty(descriptor):
            pending += data.decode("utf-8", "replace").replace("\r", "\n")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                self._output(key, operation, line)
            if len(pending) > _MAX_OUTPUT:
                self._output(key, operation, "[overlong authentication output omitted]")
                pending = ""
            elif pending:
                # Interactive prompts need not end in a newline.
                self._output(key, operation, pending, partial=True)
        if pending:
            self._output(key, operation, pending)

    async def _login(self, key, operation):
        row = self._load(key)
        if row.get("operation_id") != operation or row["state"] != "connecting":
            return
        binary = self._binary(key[0])
        if binary is None:
            row.update(state="failed", reason="native_cli_missing")
            self._save(row)
            return
        master, slave = pty.openpty()
        settings = termios.tcgetattr(slave)
        settings[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, settings)
        os.set_blocking(master, False)
        process, reader = None, None
        self._inputs[key] = master
        self._secrets[key] = []
        try:
            arguments = (
                ["auth", "login", "--claudeai"]
                if key[0] == "claude-code"
                else ["login", "--device-auth"]
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(self._terminal),
                binary,
                *arguments,
                cwd=self._profile_base(*key),
                env=self._environment(key),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
            os.close(slave)
            slave = None
            self._processes[key] = process
            row = self._load(key)
            if row.get("operation_id") != operation or row["state"] != "connecting":
                return
            row.update(pid=process.pid, process_start=_process_start(process.pid))
            self._save(row)
            reader = asyncio.create_task(self._consume(key, operation, master))
            await asyncio.wait_for(process.wait(), self.login_timeout_s)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(reader), 0.5)
            connected, method, reason = await self._probe(key)
            row = self._load(key)
            if row.get("operation_id") == operation and row["state"] == "connecting":
                row.update(
                    state="connected" if connected else "failed",
                    auth_method=method,
                    reason=None if connected else reason or "native_login_failed",
                )
                for field in ("auth_url", "device_code", "input_required"):
                    row.pop(field, None)
                self._save(row)
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, TimeoutError) as error:
            row = self._load(key)
            if row.get("operation_id") == operation and row["state"] == "connecting":
                row.update(
                    state="failed",
                    reason="login_timeout"
                    if isinstance(error, TimeoutError)
                    else "native_login_failed",
                )
                self._save(row)
        finally:
            if process:
                await self._terminate(process)
            if reader:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if slave is not None:
                os.close(slave)
            os.close(master)
            self._inputs.pop(key, None)
            self._processes.pop(key, None)
            self._secrets.pop(key, None)
            row = self._load(key)
            if row.get("operation_id") == operation and row["state"] == "failed":
                self._wipe(key)

    async def submit(self, harness_id, instance_id="default", *, code):
        key = self._key(harness_id, instance_id)
        if (
            not isinstance(code, str)
            or not 1 <= len(code) <= 2048
            or any(ord(character) < 33 or ord(character) > 126 for character in code)
        ):
            raise ValueError("Enter the provider's single-line verification code")
        async with self._locks.setdefault(key, asyncio.Lock()):
            row = self._load(key)
            if (
                key[0] != "claude-code"
                or row["state"] != "connecting"
                or not row.get("input_required")
                or key not in self._inputs
            ):
                raise ValueError(
                    "This official login flow is not awaiting a pasted verification code"
                )
            self._secrets.setdefault(key, []).append(code)
            settings = termios.tcgetattr(self._inputs[key])
            settings[3] &= ~termios.ECHO
            termios.tcsetattr(self._inputs[key], termios.TCSANOW, settings)
            os.write(self._inputs[key], (code + "\n").encode())
            row["input_required"] = False
            return self._public(self._save(row))

    async def submit_code(self, harness_id, code, instance_id="default"):
        return await self.submit(harness_id, instance_id, code=code)

    async def _cancel(self, key, *, state="canceled", reason="operator_canceled"):
        row = self._load(key)
        row.update(state=state, auth_method="unknown", reason=reason)
        for field in ("auth_url", "device_code", "input_required"):
            row.pop(field, None)
        self._save(row)
        task = self._workers.get(key)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif row.get("pid") and row.get("process_start") == _process_start(row["pid"]):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                if os.getpgid(row["pid"]) == row["pid"]:
                    os.killpg(row["pid"], signal.SIGKILL)
        return row

    def _wipe(self, key):
        profile = self._profile_base(*key)
        for path in (profile / "config", profile / "home", profile / "tmp"):
            if path.is_symlink():
                raise ValueError("Refusing to remove an unowned authentication path")
            shutil.rmtree(path)
        self._profile_base(*key)

    async def cancel(self, harness_id, instance_id="default"):
        key = self._key(harness_id, instance_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            if self._load(key)["state"] != "connecting":
                return self._public(self._load(key))
            row = await self._cancel(key)
            self._wipe(key)
            return self._public(row)

    async def disconnect(self, harness_id, instance_id="default"):
        key = self._key(harness_id, instance_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            await self._guard(key)
            await self._cancel(key, state="auth_required", reason="disconnected")
            arguments = ["auth", "logout"] if key[0] == "claude-code" else ["logout"]
            with contextlib.suppress(OSError, ValueError, TimeoutError):
                await self._command(key, arguments)
            self._wipe(key)
            return self._public(self._load(key))

    async def recover(self):
        with self._db_lock:
            rows = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM profiles")]
        for row in rows:
            if row["state"] == "connecting" and (
                not _owner_alive(row) or row.get("deadline", 0) <= time.time()
            ):
                key = self._key(row["harness_id"], row["instance_id"])
                async with self._locks.setdefault(key, asyncio.Lock()):
                    await self._cancel(key, state="failed", reason="login_interrupted")
                    self._wipe(key)

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        for key, task in list(self._workers.items()):
            if not task.done():
                async with self._locks.setdefault(key, asyncio.Lock()):
                    await self._cancel(key, state="canceled", reason="server_stopped")
                    self._wipe(key)
        self.db.close()
