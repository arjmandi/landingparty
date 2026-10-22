"""Harbor SDK verifier that records a sealed mission and deliberately returns no score."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
from pathlib import Path

import httpx
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier

from worldd.isolation import DockerHarness


class FencedExistingEnvironment(BaseEnvironment):
    """Bind Harbor to one validated, already-created per-run fenced container.

    Harbor cannot build an unfenced replacement or execute as root. Transfers
    run inside the same unprivileged namespace and never mount the Docker socket.
    """

    def __init__(self, *, run_id, container_id, firewall_id, data_dir=None, runner=None, **kwargs):
        self.run_id, self.container_id, self.firewall_id = run_id, container_id, firewall_id
        self.data_dir, self.runner, self.harness = data_dir, runner, None
        kwargs.pop("enable_environment_dir_upload", None)
        super().__init__(enable_environment_dir_upload=False, **kwargs)

    @staticmethod
    def type():
        return "landing-party-fenced"

    @property
    def capabilities(self):
        return EnvironmentCapabilities(mounted=True)

    def _validate_definition(self):
        if not self.environment_dir.is_dir():
            raise ValueError("A Harbor task environment directory is required")

    async def start(self, force_build=False):
        if force_build:
            raise ValueError("The registered fenced container must be created before this trial")
        self.harness = await DockerHarness.bind(
            self.run_id,
            self.container_id,
            self.firewall_id,
            data_dir=self.data_dir,
            runner=self.runner,
            extra_mount_roots={
                "/logs/agent": {"source": str(self.trial_paths.agent_dir), "readonly": False},
                "/logs/verifier": {"source": str(self.trial_paths.verifier_dir), "readonly": True},
            },
        )
        inspected = await self.harness._json("inspect", self.harness.agent_id)
        if not any(
            mount.get("Destination") == "/logs/agent" and mount.get("RW")
            for mount in inspected.get("Mounts", [])
        ):
            raise ValueError("Harbor requires its exact writable trial agent-log mount")
        inspection = await self.harness.firewall_inspect()
        if (
            inspection["output_policy"] != "DROP"
            or not inspection["rules_reviewed"]
            or inspection["harness_net_admin"]
        ):
            raise ValueError(
                "The existing namespace does not enforce the registered world-only fence"
            )

    async def stop(self, delete=False):
        if self.harness:
            await self.harness.stop(delete=delete)

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        if self.harness is None:
            raise RuntimeError("Start and validate the fenced environment first")
        if user is not None and str(user) != self.harness.user:
            raise ValueError("Harbor cannot change the registered unprivileged container user")
        argv = ["env"]
        for key, value in {**self._persistent_env, **(env or {})}.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in {
                "WORLD_CONTROL_TOKEN",
                "WORLD_OPENAI_API_KEY",
                "WORLD_ANTHROPIC_API_KEY",
            }:
                raise ValueError("Only agent environment values may be injected")
            argv.append(key + "=" + str(value))
        script = command if cwd is None else "cd -- " + shlex.quote(cwd) + " && " + command
        argv.extend(["/bin/sh", "-lc", script])
        result = await asyncio.wait_for(self.harness.execute(argv), timeout=timeout_sec or 120)
        return ExecResult(
            stdout=result["stdout"], stderr=result["stderr"], return_code=result["returncode"]
        )

    async def _python(self, source, *args):
        result = await self.exec(shlex.join(["python3", "-c", source, *map(str, args)]))
        if result.return_code:
            raise ValueError("Fenced file transfer failed: " + (result.stderr or "")[-1000:])
        return result.stdout or ""

    # Resolve inside the container: symlinks cannot move a transfer outside agent-visible roots.
    _PATH = """import pathlib,sys
p=pathlib.Path(sys.argv[1]).resolve()
roots=[pathlib.Path(s) for s in ('/work','/tmp','/logs/agent','/world')]
if not any(p==r or r in p.parents for r in roots): raise ValueError('path outside agent surface')
"""

    async def upload_file(self, source_path, target_path):
        source = Path(source_path)
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 64 * 1024**2:
            raise ValueError("Transfer requires a regular file of at most 64 MiB")
        await self._python(
            self._PATH
            + "\nif p==pathlib.Path('/world') or pathlib.Path('/world') in p.parents: raise ValueError('world readonly')\np.parent.mkdir(parents=True,exist_ok=True)\np.write_bytes(b'')",
            target_path,
        )
        content = await asyncio.to_thread(source.read_bytes)
        for offset in range(0, len(content), 32768):
            await self._python(
                self._PATH
                + "\nimport base64\nwith p.open('ab') as f: f.write(base64.b64decode(sys.argv[2],validate=True))",
                target_path,
                base64.b64encode(content[offset : offset + 32768]).decode(),
            )

    async def upload_dir(self, source_dir, target_dir):
        source = Path(source_dir)
        if source.is_symlink() or not source.is_dir():
            raise ValueError("A regular source directory is required")
        await self._python(
            self._PATH
            + "\nif p==pathlib.Path('/world') or pathlib.Path('/world') in p.parents: raise ValueError('world readonly')\np.mkdir(parents=True,exist_ok=True)",
            target_dir,
        )
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError("Directory transfers cannot contain symlinks")
            if path.is_file():
                await self.upload_file(path, str(Path(target_dir) / path.relative_to(source)))

    async def download_file(self, source_path, target_path):
        content = await self._python(
            self._PATH
            + "\nimport base64\nif not p.is_file() or p.stat().st_size>67108864: raise ValueError('invalid size/type')\nprint(base64.b64encode(p.read_bytes()).decode())",
            source_path,
        )
        target = Path(target_path)
        if target.is_symlink():
            raise ValueError("A download target cannot be a symlink")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(content.strip(), validate=True))

    async def download_dir(self, source_dir, target_dir):
        names = json.loads(
            await self._python(
                self._PATH
                + "\nimport json\nprint(json.dumps([str(x.relative_to(p)) for x in p.rglob('*') if x.is_file() and not x.is_symlink()]))",
                source_dir,
            )
        )
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid remote relative path")
            if not (target / relative).resolve().is_relative_to(target.resolve()):
                raise ValueError("A download target cannot escape through a host symlink")
            await self.download_file(str(Path(source_dir) / relative), target / relative)


class WorldNoScoreVerifier(BaseVerifier):
    def __init__(
        self,
        *,
        run_id,
        world_url="http://127.0.0.1:7777",
        control_token=None,
        client=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.run_id, self.world_url = run_id, world_url
        self.token = control_token or os.environ.get("WORLD_CONTROL_TOKEN")
        self.client = client

    async def verify(self):
        client = self.client or httpx.AsyncClient(
            base_url=self.world_url,
            trust_env=False,
            timeout=90,
            headers={"X-World-Control-Token": self.token} if self.token else {},
        )
        try:
            response = await client.get("/v1/status", params={"run_id": self.run_id})
            response.raise_for_status()
            if response.json()["state"] in {"running", "frozen", "stopping"}:
                response = await client.post(
                    "/v1/beam-up", json={"run_id": self.run_id, "reason": "harness_stopped"}
                )
                response.raise_for_status()
            response = await client.get("/v1/report", params={"run_id": self.run_id})
            response.raise_for_status()
            directory = Path(self.trial_paths.verifier_dir)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "landing-party-report.json"
            path.write_text(
                json.dumps(
                    {"run_id": self.run_id, "reward": None, "report": response.json()}, indent=2
                )
                + "\n"
            )
            path.chmod(0o600)
            return VerifierResult(rewards=None)
        finally:
            if self.client is None:
                await client.aclose()
