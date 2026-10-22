"""One-shot, signed host observations for a Docker-free world container.

The host owns Docker access. Only the protected researcher API issues challenges;
the host reconstructs its reviewed local plan rather than running received code.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .isolation import DockerHarness
from .preflight_probes import reviewed_plan
from .util import canonical, digest, identifier, now


class HostObserverBridge:
    def __init__(self, service):
        self.service, self.world, self.store = service, service.world, service.store
        self.pending, self.accepted = {}, {}

    def challenge(
        self, run_id, observer_public_key, container_id, firewall_id, *, evaluation_paths=()
    ):
        run = self.store.get_run(run_id)
        if run["state"] not in {"preflight", "running"}:
            raise ValueError("Host observation requires an open preflight/run")
        try:
            key_bytes = base64.b64decode(observer_public_key, validate=True)
            Ed25519PublicKey.from_public_bytes(key_bytes)
        except (ValueError, TypeError) as exc:
            raise ValueError("A raw Ed25519 public observer key is required") from exc
        for value in (container_id, firewall_id):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 128
                or not all(char.isalnum() or char in "_.-" for char in value)
            ):
                raise ValueError("Literal container/fence identities are required")
        proxy = self.world.services.get("proxy")
        if proxy is not None:
            proxy.prepare_run(run_id)
        plan = reviewed_plan(
            evaluation_paths, harness_id=run["config"].get("harness_id", "manual@0.1")
        )
        value = {
            "challenge_id": identifier("hobs"),
            "nonce": secrets.token_urlsafe(32),
            "run_id": run_id,
            "config_hash": digest(run["config"]),
            "observer_public_key": observer_public_key,
            "observer_key_id": digest(key_bytes),
            "container_id": container_id,
            "firewall_id": firewall_id,
            "issued_at": now(),
            "valid_until": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
            "plan": plan,
            "plan_hash": digest(plan),
        }
        value["marker"] = {
            "challenge_id": value["challenge_id"],
            "nonce_hash": digest(value["nonce"]),
            "config_hash": value["config_hash"],
            "world_id": getattr(self.world, "world_id", None),
        }
        root = self.store.run_dir(run_id) / "world"
        if root.is_symlink():
            raise ValueError("World observation marker must remain in this run")
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ".preflight-observer.json"
        if marker.is_symlink():
            raise ValueError("World observation marker must be a regular file")
        marker.write_text(canonical(value["marker"]))
        marker.chmod(0o644)
        self.pending[value["challenge_id"]] = value
        self.store.append_record(
            run_id,
            "control_plane_audit",
            test="host_observer_challenge",
            challenge_id=value["challenge_id"],
            observer_key_id=value["observer_key_id"],
            plan_hash=value["plan_hash"],
            config_hash=value["config_hash"],
        )
        return value

    async def submit(
        self,
        run_id,
        challenge_id,
        receipt,
        signature,
        *,
        metadata=None,
        sources=None,
        t0_ref=None,
        run_kill=True,
    ):
        challenge = self.pending.get(challenge_id)
        if challenge is None or challenge["run_id"] != run_id:
            raise ValueError("Observer challenge is unknown or already consumed")
        if datetime.now(UTC) >= datetime.fromisoformat(challenge["valid_until"]):
            self.pending.pop(challenge_id, None)
            raise ValueError("Observer challenge expired; collect a new observation")
        try:
            key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(challenge["observer_public_key"], validate=True)
            )
            key.verify(base64.b64decode(signature, validate=True), canonical(receipt).encode())
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise ValueError("Host observer signature is invalid") from exc
        bound = (
            "challenge_id",
            "nonce",
            "run_id",
            "config_hash",
            "container_id",
            "firewall_id",
            "plan_hash",
        )
        if not isinstance(receipt, dict) or any(
            receipt.get(name) != challenge[name] for name in bound
        ):
            raise ValueError("Observer receipt differs from its exact run/namespace challenge")
        run = self.store.get_run(run_id)
        if (
            run["state"] not in {"preflight", "running"}
            or digest(run["config"]) != challenge["config_hash"]
        ):
            raise ValueError("Observer configuration is stale")
        observed = datetime.fromisoformat(receipt["observed_at"])
        if not datetime.fromisoformat(challenge["issued_at"]) <= observed <= datetime.now(UTC):
            raise ValueError("Observer timestamp differs from its challenge interval")
        plan = challenge["plan"]
        outputs = receipt.get("outputs", {})
        if (
            set(outputs) != set(plan["commands"])
            or not receipt.get("structure", {}).get("verified")
            or receipt["structure"].get("run_id") != run_id
        ):
            raise ValueError("A complete measured probe plan and structural binding are required")
        for result in outputs.values():
            if (
                not isinstance(result, dict)
                or type(result.get("returncode")) is not int
                or not isinstance(result.get("stdout", ""), str)
                or len(result.get("stdout", "")) > 2_000_000
            ):
                raise ValueError("Invalid or oversized namespace observation")
        try:
            observed_marker = json.loads(outputs["environment"]["stdout"]).get(
                "WORLD_OBSERVER_MARKER"
            )
        except (ValueError, TypeError) as exc:
            raise ValueError("The actual mounted world marker was not observed") from exc
        if observed_marker != challenge["marker"]:
            raise ValueError("The harness world mount belongs to another storage/run/challenge")
        before, after = receipt.get("firewall_before"), receipt.get("firewall_after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise TypeError("Actual before/after firewall observations are required")
        self.pending.pop(challenge_id)  # single use, including a failed preflight result
        receipt_hash = digest(receipt)
        binding = {
            "receipt_hash": receipt_hash,
            "observer_key_id": challenge["observer_key_id"],
            "challenge_id": challenge_id,
            "config_hash": challenge["config_hash"],
            "container_id": receipt["structure"]["container_id"],
            "firewall_id": receipt["structure"]["firewall_id"],
            "valid_until": challenge["valid_until"],
            "plan_hash": challenge["plan_hash"],
        }
        directory = self.store.run_dir(run_id) / "preflight"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / "host-observer.json"
        target.write_text(
            canonical({"receipt": receipt, "signature": signature, "challenge": challenge})
        )
        target.chmod(0o600)
        self.accepted[run_id] = {"binding": binding, "receipt": receipt}
        self.store.append_record(
            run_id, "control_plane_audit", test="host_observer_receipt", **binding
        )
        command_results = {digest(argv): outputs[name] for name, argv in plan["commands"].items()}
        inspection_index, invoked = 0, False

        async def execute(argv):
            key_hash = digest(argv)
            if key_hash not in command_results:
                raise ValueError("Command is outside the signed reviewed observation plan")
            return command_results[key_hash]

        async def firewall_inspect():
            nonlocal inspection_index
            if inspection_index >= 2:
                raise ValueError("The one-shot firewall observation was already consumed")
            value = (before, after)[inspection_index]
            inspection_index += 1
            return value

        async def validate():
            nonlocal invoked
            if invoked or datetime.now(UTC) >= datetime.fromisoformat(binding["valid_until"]):
                raise ValueError("A fresh one-shot host observation is required")
            invoked = True

        self.service.register_harness(
            run_id,
            execute=execute,
            firewall_inspect=firewall_inspect,
            evaluation_paths=plan["evaluation_paths"],
        )
        self.service.harnesses[run_id].update(
            validate=validate, host_observer=binding, harness_observation=outputs["harness"]
        )
        if sources is not None:
            generated = await self.service.observe_sources(run_id, **sources)
            metadata = {**generated["metadata"], **(metadata or {})}
        return await self.service.run(run_id, metadata=metadata, t0_ref=t0_ref, run_kill=run_kill)


def verify_activation(world, run_id, binding):
    """Verify the persisted receipt and chained anchor before issuing an agent capability."""
    run = world.store.get_run(run_id)
    if binding.get("config_hash") != digest(run["config"]) or datetime.now(
        UTC
    ) >= datetime.fromisoformat(binding["valid_until"]):
        raise ValueError("The host observation expired or belongs to another configuration")
    path = world.store.run_dir(run_id) / "preflight/host-observer.json"
    if path.is_symlink():
        raise ValueError("Host observation artifacts must remain private regular files")
    value = json.loads(path.read_text())
    receipt, challenge = value["receipt"], value["challenge"]
    current_marker = world.store.run_dir(run_id) / "world/.preflight-observer.json"
    if current_marker.is_symlink() or json.loads(current_marker.read_text()) != challenge.get(
        "marker"
    ):
        raise ValueError("The host observation was superseded by a newer storage challenge")
    if (
        digest(receipt) != binding.get("receipt_hash")
        or challenge["challenge_id"] != binding.get("challenge_id")
        or receipt["run_id"] != run_id
    ):
        raise ValueError("The host observation differs from its report")
    try:
        key_bytes = base64.b64decode(challenge["observer_public_key"], validate=True)
        if digest(key_bytes) != binding.get("observer_key_id"):
            raise ValueError("Observer key differs from the chained attester identity")
        key = Ed25519PublicKey.from_public_bytes(key_bytes)
        key.verify(base64.b64decode(value["signature"], validate=True), canonical(receipt).encode())
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise ValueError("The persisted host observation signature is invalid") from exc
    anchors = [
        record
        for record in world.store.records(run_id)
        if record.get("kind") == "control_plane_audit"
        and record.get("test") == "host_observer_receipt"
        and record.get("receipt_hash") == binding["receipt_hash"]
    ]
    if (
        len(anchors) != 1
        or any(anchors[0].get(name) != value for name, value in binding.items())
        or not world.store.verify_record(run_id)["valid"]
    ):
        raise ValueError("The host observation lacks its exact chained receipt anchor")


async def collect(challenge, *, key, container_id, firewall_id, data_dir=None, runner=None):
    """Trusted host client: execute only a locally reconstructed reviewed plan."""
    plan = reviewed_plan(
        challenge["plan"]["evaluation_paths"], harness_id=challenge["plan"]["harness_id"]
    )
    if (
        plan != challenge["plan"]
        or digest(plan) != challenge["plan_hash"]
        or container_id != challenge["container_id"]
        or firewall_id != challenge["firewall_id"]
    ):
        raise ValueError("Server plan/namespace differs from the reviewed local observer")
    harness = await DockerHarness.bind(
        challenge["run_id"], container_id, firewall_id, data_dir=data_dir, runner=runner
    )
    structure = await harness.validate()
    before = await harness.firewall_inspect()
    outputs = {}
    for name, argv in plan["commands"].items():
        result = await harness.execute(argv)
        outputs[name] = {"returncode": result["returncode"], "stdout": result.get("stdout", "")}
    after = await harness.firewall_inspect()
    if await harness.validate() != structure:
        raise ValueError("Harness structure changed while observations ran")
    receipt = {
        name: challenge[name]
        for name in (
            "challenge_id",
            "nonce",
            "run_id",
            "config_hash",
            "container_id",
            "firewall_id",
            "plan_hash",
        )
    }
    receipt.update(
        observed_at=now(),
        structure=structure,
        outputs=outputs,
        firewall_before=before,
        firewall_after=after,
    )
    signature = base64.b64encode(key.sign(canonical(receipt).encode())).decode()
    return {
        "run_id": challenge["run_id"],
        "challenge_id": challenge["challenge_id"],
        "receipt": receipt,
        "signature": signature,
    }


async def observe_control(
    world_url,
    run_id,
    container_id,
    firewall_id,
    *,
    token,
    data_dir=None,
    metadata=None,
    sources=None,
    client=None,
):
    parsed = urlsplit(world_url)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not (
            parsed.scheme == "https"
            or parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        )
    ):
        raise ValueError("Use verified HTTPS or local loopback control without URL credentials")
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    own_client = client is None
    client = client or httpx.AsyncClient(
        base_url=world_url, trust_env=False, timeout=120, headers={"X-World-Control-Token": token}
    )
    try:
        response = await client.post(
            "/v1/preflight/observer/challenge",
            json={
                "run_id": run_id,
                "observer_public_key": public,
                "container_id": container_id,
                "firewall_id": firewall_id,
            },
        )
        response.raise_for_status()
        submission = await collect(
            response.json(),
            key=key,
            container_id=container_id,
            firewall_id=firewall_id,
            data_dir=data_dir,
        )
        response = await client.post(
            "/v1/preflight/observer/submit",
            json={**submission, "metadata": metadata, "sources": sources},
        )
        response.raise_for_status()
        return response.json()
    finally:
        if own_client:
            await client.aclose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("container_id")
    parser.add_argument("firewall_id")
    parser.add_argument("--world", default="http://127.0.0.1:7777")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--sources", type=Path, help="Trusted JSON source references; never credentials"
    )
    parser.add_argument(
        "--metadata", type=Path, help="Additional world-private artifact references"
    )
    args = parser.parse_args(argv)
    token = os.environ.get("WORLD_CONTROL_TOKEN")
    if not token:
        parser.error("WORLD_CONTROL_TOKEN must be supplied in trusted environment settings")
    report = asyncio.run(
        observe_control(
            args.world,
            args.run_id,
            args.container_id,
            args.firewall_id,
            token=token,
            data_dir=args.data_dir,
            sources=json.loads(args.sources.read_text()) if args.sources else {},
            metadata=json.loads(args.metadata.read_text()) if args.metadata else None,
        )
    )
    print(
        canonical(
            {
                "run_id": args.run_id,
                "passed": report["passed"],
                "report_hash": report["report_hash"],
                "failures": report["failures"],
                "unmeasured": report["unmeasured"],
            }
        )
    )


if __name__ == "__main__":
    main()
