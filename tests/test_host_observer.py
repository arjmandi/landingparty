import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from worldd.host_observer import collect, verify_activation
from worldd.preflight import PreflightService
from worldd.proxy import CertificateAuthority
from worldd.storage import Store
from worldd.util import canonical, digest, now


def setup(tmp_path):
    store = Store(tmp_path)
    world = SimpleNamespace(store=store, services={}, gateway=SimpleNamespace(signer=None))
    ca = CertificateAuthority(tmp_path / "ca")
    world.services["proxy"] = SimpleNamespace(ca=ca, prepare_run=lambda run_id: None)
    run_id = store.create_run(
        {
            "budget": {"usd": 1},
            "packs": [],
            "harness_id": "manual@0.1",
            "isolation_profile": "fenced",
        }
    )["run_id"]
    (store.run_dir(run_id) / "work/out").mkdir(parents=True)
    service = PreflightService(world)
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    challenge = service.host_challenge(run_id, public, "agent", "fence")
    return world, service, run_id, key, challenge


def observed_receipt(world, service, run_id, challenge):
    from cryptography.hazmat.primitives import hashes

    outputs = {name: {"returncode": 1, "stdout": ""} for name in challenge["plan"]["commands"]}
    env = {
        "WORLD_RUN_ID": run_id,
        "OPENAI_BASE_URL": "http://172.30.77.2:8081/openai/v1",
        "HTTP_PROXY": "http://172.30.77.2:8080",
        "HTTPS_PROXY": "http://172.30.77.2:8080",
        "SSL_CERT_FILE": "/world/ca.pem",
        "WORLD_CA_SHA256": world.services["proxy"]
        .ca.certificate.fingerprint(hashes.SHA256())
        .hex(),
        "WORLD_PRIVATE_ENV_PRESENT": False,
        "WORLD_OBSERVER_MARKER": challenge["marker"],
    }
    outputs["environment"] = {"returncode": 0, "stdout": json.dumps(env)}
    outputs["surfaces"] = {
        "returncode": 0,
        "stdout": json.dumps(
            {"work": service._tree_manifest(world.store.run_dir(run_id) / "work"), "beliefs": None}
        ),
    }
    outputs["evaluation"] = {"returncode": 0, "stdout": ""}
    outputs["harness"] = {
        "returncode": 0,
        "stdout": '{"declared_id":"manual@0.1","observed_version":"0.1.0"}',
    }
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
        structure={
            "verified": True,
            "run_id": run_id,
            "container_id": "agent-full",
            "firewall_id": "fence-full",
        },
        outputs=outputs,
        firewall_before={
            "output_policy": "DROP",
            "drop_packets": 0,
            "harness_net_admin": False,
            "rules_reviewed": True,
        },
        firewall_after={
            "output_policy": "DROP",
            "drop_packets": 2,
            "harness_net_admin": False,
            "rules_reviewed": True,
        },
    )
    return receipt


def sign(key, receipt):
    return base64.b64encode(key.sign(canonical(receipt).encode())).decode()


async def test_signed_namespace_observations_drive_same_preflight_and_are_one_shot(tmp_path):
    world, service, run_id, key, challenge = setup(tmp_path)
    receipt = observed_receipt(world, service, run_id, challenge)
    report = await service.host_submit(
        run_id, challenge["challenge_id"], receipt, sign(key, receipt), run_kill=False
    )
    assert report["checks"]["harness_environment"]["status"] == "passed"
    assert report["checks"]["fenced_egress"]["status"] == "passed"
    assert report["checks"]["initial_surfaces"]["status"] == "passed"
    assert report["checks"]["evaluation_isolation"]["status"] == "passed"
    assert not report["passed"] and "provider_canary" in report["unmeasured"]
    assert report["host_observer"]["receipt_hash"] == digest(receipt)
    verify_activation(world, run_id, report["host_observer"])
    with pytest.raises(ValueError, match="consumed"):
        await service.host_submit(
            run_id, challenge["challenge_id"], receipt, sign(key, receipt), run_kill=False
        )
    world.store.close()


@pytest.mark.parametrize("mutation", ["nonce", "plan_hash", "run_id", "signature"])
async def test_signed_receipt_cannot_change_challenge_scope(tmp_path, mutation):
    world, service, run_id, key, challenge = setup(tmp_path)
    receipt = observed_receipt(world, service, run_id, challenge)
    signature = sign(key, receipt)
    if mutation == "signature":
        signature = sign(Ed25519PrivateKey.generate(), receipt)
    else:
        receipt[mutation] = "different"
        signature = sign(key, receipt)
    with pytest.raises(ValueError, match="signature|exact run/namespace"):
        await service.host_submit(
            run_id, challenge["challenge_id"], receipt, signature, run_kill=False
        )
    assert not any(
        record.get("test") == "host_observer_receipt" for record in world.store.records(run_id)
    )
    world.store.close()


async def test_host_never_executes_a_plan_changed_by_the_server(tmp_path):
    world, _, _, key, challenge = setup(tmp_path)
    challenge["plan"]["commands"]["evaluation"] = [
        "python3",
        "-c",
        "raise SystemExit('not reviewed')",
    ]
    challenge["plan_hash"] = digest(challenge["plan"])
    with pytest.raises(ValueError, match="reviewed local"):
        await collect(challenge, key=key, container_id="agent", firewall_id="fence")
    world.store.close()


async def test_activation_rechecks_signature_artifact_anchor_and_expiry(tmp_path):
    world, service, run_id, key, challenge = setup(tmp_path)
    receipt = observed_receipt(world, service, run_id, challenge)
    report = await service.host_submit(
        run_id, challenge["challenge_id"], receipt, sign(key, receipt), run_kill=False
    )
    expired = {
        **report["host_observer"],
        "valid_until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    }
    with pytest.raises(ValueError, match="expired"):
        verify_activation(world, run_id, expired)
    path = world.store.run_dir(run_id) / "preflight/host-observer.json"
    value = json.loads(path.read_text())
    replacement = Ed25519PrivateKey.generate()
    original = json.loads(path.read_text())
    value["challenge"]["observer_public_key"] = base64.b64encode(
        replacement.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    value["signature"] = sign(replacement, value["receipt"])
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="signature"):
        verify_activation(world, run_id, report["host_observer"])
    value = original
    value["receipt"]["outputs"]["environment"]["returncode"] = 1
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="differs"):
        verify_activation(world, run_id, report["host_observer"])
    world.store.close()


async def test_foreign_world_volume_marker_is_rejected_even_with_valid_host_signature(tmp_path):
    world, service, run_id, key, challenge = setup(tmp_path)
    receipt = observed_receipt(world, service, run_id, challenge)
    environment = json.loads(receipt["outputs"]["environment"]["stdout"])
    environment["WORLD_OBSERVER_MARKER"] = {"challenge_id": "another-volume"}
    receipt["outputs"]["environment"]["stdout"] = json.dumps(environment)
    with pytest.raises(ValueError, match="another storage/run/challenge"):
        await service.host_submit(
            run_id, challenge["challenge_id"], receipt, sign(key, receipt), run_kill=False
        )
    world.store.close()
