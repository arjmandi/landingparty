"""Trusted host adapter for an already-created fenced harness namespace.

Only the researcher can register containers. No shell strings or Docker socket
are exposed to the agent; execution uses the bound container's unprivileged user.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path


class DockerHarness:
    @classmethod
    async def bind(
        cls,
        run_id,
        container_id,
        firewall_id,
        *,
        data_dir=None,
        extra_mount_roots=None,
        runner=None,
    ):
        instance = cls()
        if any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
            for value in (run_id, container_id, firewall_id)
        ):
            raise ValueError("Literal run and container identities are required")
        instance.run_id, instance.container_id, instance.firewall_id = (
            run_id,
            container_id,
            firewall_id,
        )
        instance.data_dir = Path(data_dir).resolve() if data_dir else None
        instance.extra_mount_roots = extra_mount_roots or {}
        instance.runner = runner or instance._run
        await instance.validate()
        return instance

    @staticmethod
    async def _run(argv):
        env = {
            key: os.environ[key]
            for key in ("PATH", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT", "HOME")
            if key in os.environ
        }
        process = await asyncio.create_subprocess_exec(
            "docker", *argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        return {
            "returncode": process.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }

    async def _json(self, *argv):
        result = await self.runner(list(argv))
        if result.get("returncode") != 0:
            raise ValueError("Trusted Docker inspection failed")
        value = json.loads(result["stdout"])
        return value[0]

    async def validate(self):
        agent = await self._json("inspect", self.container_id)
        fence = await self._json("inspect", self.firewall_id)
        for container, role in ((agent, "agent"), (fence, "fence")):
            labels = container.get("Config", {}).get("Labels") or {}
            if (
                labels.get("landing-party.run_id") != self.run_id
                or labels.get("landing-party.role") != role
                or not container.get("State", {}).get("Running")
            ):
                raise ValueError("Harness containers must be running and bound to this run/role")
            host = container["HostConfig"]
            if (
                host.get("Privileged")
                or "ALL" not in {value.removeprefix("CAP_") for value in host.get("CapDrop") or []}
                or not any(
                    option.startswith("no-new-privileges")
                    for option in host.get("SecurityOpt") or []
                )
            ):
                raise ValueError(
                    "Harness containers must drop capabilities and retain no-new-privileges"
                )
        host = agent["HostConfig"]
        user = agent["Config"].get("User", "")
        if (
            not user
            or user.split(":")[0] in {"0", "root"}
            or host.get("CapAdd")
            or not host.get("ReadonlyRootfs")
        ):
            raise ValueError("Agent must be nonroot with readonly rootfs and no added capabilities")
        if host.get("NetworkMode") != "container:" + fence["Id"]:
            raise ValueError("Agent must share only the bound firewall network namespace")
        if {value.removeprefix("CAP_") for value in fence["HostConfig"].get("CapAdd") or []} != {
            "NET_ADMIN"
        }:
            raise ValueError("Firewall must hold only NET_ADMIN")
        networks = fence.get("NetworkSettings", {}).get("Networks") or {}
        if len(networks) != 1:
            raise ValueError("Firewall must have exactly one internal network")
        network = await self._json("network", "inspect", next(iter(networks)))
        if network.get("Internal") is not True:
            raise ValueError("Harness network must be internal")
        configured_mounts = {mount.get("Target"): mount for mount in host.get("Mounts") or []}
        mounted = set()
        run_mounts = {}
        for mount in agent.get("Mounts", []):
            destination = mount["Destination"]
            if mount.get("Type") == "tmpfs" and destination in {"/tmp", "/home/agent"}:
                continue
            if destination in {"/work", "/world"}:
                part = destination[1:]
                options = configured_mounts.get(destination, {}).get("VolumeOptions") or {}
                subpath = options.get("Subpath", options.get("SubPath"))
                exact_bind = (
                    self.data_dir
                    and mount.get("Type") == "bind"
                    and Path(mount["Source"]).resolve()
                    == self.data_dir / "runs" / self.run_id / part
                )
                scoped_volume = (
                    mount.get("Type") == "volume" and subpath == f"runs/{self.run_id}/{part}"
                )
                if not (exact_bind or scoped_volume) or bool(mount.get("RW")) != (
                    destination == "/work"
                ):
                    raise ValueError("Run mounts must expose only this run's scoped work/world")
                mounted.add(destination)
                run_mounts[destination] = mount
            elif (
                destination in {"/logs/agent", "/logs/verifier"}
                and destination in self.extra_mount_roots
            ):
                expected = self.extra_mount_roots[destination]
                if destination == "/logs/verifier" and not expected.get("readonly"):
                    raise ValueError("Verifier logs must be read-only to the agent")
                if Path(mount["Source"]).resolve() != Path(expected["source"]).resolve() or bool(
                    mount.get("RW")
                ) == bool(expected["readonly"]):
                    raise ValueError("Extra mount differs from its trusted declaration")
            else:
                raise ValueError("Unexpected harness mount; private data must remain outside")
        if mounted != {"/work", "/world"}:
            raise ValueError("Both scoped work and world mounts are required")
        if run_mounts["/work"]["Type"] != run_mounts["/world"]["Type"]:
            raise ValueError("Work and world must use the same reviewed storage scope")
        if run_mounts["/work"]["Type"] == "volume" and (
            not run_mounts["/work"].get("Name")
            or run_mounts["/work"].get("Name") != run_mounts["/world"].get("Name")
        ):
            raise ValueError("Work and world subpaths must belong to the same named volume")
        self.user = user
        self.agent_id, self.fence_id = agent["Id"], fence["Id"]
        result = await self.runner(["exec", "--user", user, self.agent_id, "id", "-u"])
        if (
            result.get("returncode")
            or not result.get("stdout", "").strip().isdigit()
            or int(result["stdout"]) == 0
        ):
            raise ValueError("Actual harness process user must be unprivileged")
        return {
            "container_id": self.agent_id,
            "firewall_id": self.fence_id,
            "run_id": self.run_id,
            "verified": True,
            "agent_image_id": agent.get("Image"),
            "firewall_image_id": fence.get("Image"),
            "network_id": network.get("Id"),
        }

    async def execute(self, argv):
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or "\x00" in item for item in argv)
        ):
            raise ValueError("Execution requires a literal argument vector")
        return await self.runner(["exec", "--user", self.user, self.agent_id, *argv])

    async def firewall_inspect(self):
        table = await self.runner(["exec", self.fence_id, "iptables", "-nvx", "-L", "OUTPUT"])
        rules = await self.runner(["exec", self.fence_id, "iptables", "-S", "OUTPUT"])
        ipv6 = await self.runner(["exec", self.fence_id, "ip6tables", "-S", "OUTPUT"])
        capabilities = await self.execute(
            [
                "python3",
                "-c",
                "from pathlib import Path; print(next(x.split()[1] for x in Path('/proc/self/status').read_text().splitlines() if x.startswith('CapEff:')))",
            ]
        )
        if any(result.get("returncode") for result in (table, rules, ipv6, capabilities)):
            raise ValueError("Actual firewall/capability evidence is unavailable")
        match = re.search(r"policy (\w+) (\d+) packets", table["stdout"])
        expected = {
            "-P OUTPUT DROP",
            "-A OUTPUT -o lo -j ACCEPT",
            "-A OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
            "-A OUTPUT -d 172.30.77.2/32 -p tcp -m multiport --dports 7778,8080,8081 -j ACCEPT",
        }
        observed = set(
            rules["stdout"]
            .replace("ESTABLISHED,RELATED", "RELATED,ESTABLISHED")
            .strip()
            .splitlines()
        )
        reviewed = observed == expected and set(ipv6["stdout"].strip().splitlines()) == {
            "-P OUTPUT DROP",
            "-A OUTPUT -o lo -j ACCEPT",
        }
        return {
            "output_policy": match[1] if match else None,
            "drop_packets": int(match[2]) if match else 0,
            "harness_net_admin": bool(int(capabilities["stdout"].strip(), 16) & (1 << 12)),
            "rules_reviewed": reviewed,
        }

    async def stop(self, delete=False):
        """Stop/remove only the bound agent, never world or firewall containers."""
        return await self.runner(
            ["rm", "-f", self.agent_id] if delete else ["stop", "--time", "2", self.agent_id]
        )
