import asyncio
import json
from pathlib import Path

import httpx
import pytest

from worldd.dns import SecureDNSResolver
from worldd.preflight import PreflightService
from worldd.storage import Store


class World:
    def __init__(self, path):
        self.store = Store(path)
        self.services = {}
        self.ends = []

    async def end_run(self, run_id, **kwargs):
        self.ends.append(run_id)
        self.store.update_run(run_id, state="sealed")


def test_doh_requires_verified_public_https_endpoint():
    for endpoint in [
        "http://dns.google/resolve",
        "https://127.0.0.1/dns",
        "https://localhost/dns",
        "https://user:pw@dns.google/resolve",
    ]:
        with pytest.raises(ValueError):
            SecureDNSResolver(endpoint)


async def test_doh_answers_are_typed_and_cached_without_disabling_tls():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.scheme == "https"
        querytype = request.url.params["type"]
        return httpx.Response(
            200,
            json={
                "Status": 0,
                "Answer": [
                    {
                        "type": 1 if querytype == "A" else 28,
                        "data": "93.184.216.34"
                        if querytype == "A"
                        else "2606:2800:220:1:248:1893:25c8:1946",
                        "TTL": 30,
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = SecureDNSResolver("https://dns.google/resolve", client=client, direct_first=False)
    answers = await resolver("example.com", 443)
    assert len(answers) == 2
    assert await resolver("example.com", 443) == answers
    assert len(calls) == 2
    await client.aclose()


async def test_doh_failure_remains_failure_no_unchecked_proxy_fallback():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    resolver = SecureDNSResolver("https://dns.google/resolve", client=client, direct_first=False)
    with pytest.raises(OSError, match="Verified DNS-over-HTTPS"):
        await resolver("example.com", 443)
    await client.aclose()


async def test_doh_ptr_uses_verified_resolver_and_rejects_private_address():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.scheme == "https" and request.url.params["type"] == "PTR"
        assert request.url.params["name"] == "34.216.184.93.in-addr.arpa"
        return httpx.Response(
            200, json={"Status": 0, "Answer": [{"type": 12, "data": "egress.example."}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = SecureDNSResolver("https://dns.google/resolve", client=client)
    assert await resolver.reverse("93.184.216.34") == "egress.example"
    with pytest.raises(ValueError, match="public address"):
        await resolver.reverse("127.0.0.1")
    assert len(calls) == 1
    await client.aclose()


async def test_preflight_failed_commands_alone_do_not_prove_fencing(tmp_path):
    world = World(tmp_path)
    run_id = world.store.create_run({"budget": {"usd": 1}, "isolation_profile": "fenced"})["run_id"]
    preflight = PreflightService(world)
    calls = []

    async def execute(argv):
        calls.append(argv)
        return {"returncode": 1, "stderr": "sensitive output must not be recorded"}

    result = await preflight.isolation(run_id, execute)
    assert not result["passed"]
    assert result["checks"]["curl_blocked"]
    assert "packet_drop_counters" in result["unverified"]
    assert len(calls) == 4
    assert "sensitive output" not in json.dumps(world.store.records(run_id))
    world.store.close()


async def test_preflight_freshness_and_active_price_identity(tmp_path):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    world = World(tmp_path)
    world.gateway = SimpleNamespace(signer=None)
    world.price_table_id = "prices-active"
    run_id = world.store.create_run(
        {
            "budget": {"usd": 1},
            "packs": [],
            "harness_id": "harness@1",
            "model_snapshot_id": "model@1",
            "tuple": {"extractor_id": "extractor@1"},
        }
    )["run_id"]
    directory = tmp_path / "instruments"
    directory.mkdir()
    egress = directory / "egress.json"
    versions = directory / "versions.json"
    metadata = {"egress": str(egress), "versions": str(versions)}
    egress_value = {
        "observer": "world",
        "ip": "93.184.216.34",
        "reverse_dns": "egress.example",
        "observed_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
    }
    versions_value = {
        "observer": "world",
        "harness_id": "harness@1",
        "model_snapshot_id": "model@1",
        "extractor_id": "extractor@1",
        "price_table_id": "prices-stale",
        "search_response_date": datetime.now(UTC).isoformat(),
        "browser_build": None,
        "signing_key_id": None,
    }
    egress.write_text(json.dumps(egress_value))
    versions.write_text(json.dumps(versions_value))
    service = PreflightService(world)
    stale = await service.run(run_id, metadata=metadata, run_kill=False)
    assert stale["checks"]["egress"]["status"] == "failed"
    assert stale["checks"]["versions"]["status"] == "failed"
    egress_value["observed_at"] = datetime.now(UTC).isoformat()
    versions_value["price_table_id"] = world.price_table_id
    egress.write_text(json.dumps(egress_value))
    versions.write_text(json.dumps(versions_value))
    current = await service.run(run_id, metadata=metadata, run_kill=False)
    assert current["checks"]["egress"]["status"] == "passed"
    assert current["checks"]["versions"]["status"] == "passed"
    assert not current["passed"]  # Other scientific observations are still unmeasured.
    world.store.close()


async def test_preflight_inspects_actual_namespace_environment_without_logging_values(tmp_path):
    import os
    import subprocess
    import sys
    from types import SimpleNamespace

    from worldd.proxy import CertificateAuthority

    world = World(tmp_path)
    world.gateway = SimpleNamespace(signer=None)
    world.services["proxy"] = SimpleNamespace(ca=CertificateAuthority(tmp_path / "ca"))
    run_id = world.store.create_run({"budget": {"usd": 1}, "packs": []})["run_id"]
    runtime_env = {
        "PATH": os.environ["PATH"],
        "WORLD_RUN_ID": run_id,
        "OPENAI_BASE_URL": "http://172.30.77.2:8081/openai/v1",
        "HTTP_PROXY": "http://run:private-capability@172.30.77.2:8080",
        "HTTPS_PROXY": "http://run:private-capability@172.30.77.2:8080",
        "SSL_CERT_FILE": str(world.services["proxy"].ca.cert_path),
    }

    def execute(argv):
        result = subprocess.run(
            [sys.executable, *argv[1:]],
            env=runtime_env,
            capture_output=True,
            text=True,
            check=False,
        )
        return {"returncode": result.returncode, "stdout": result.stdout}

    service = PreflightService(world)
    observed = await service.run(
        run_id, execute=execute, harness_environment={"WORLD_RUN_ID": "wrong"}, run_kill=False
    )
    assert observed["checks"]["harness_environment"]["status"] == "passed"
    runtime_env["WORLD_CONTROL_TOKEN"] = "private-control-secret"
    contaminated = await service.run(run_id, execute=execute, run_kill=False)
    assert contaminated["checks"]["harness_environment"]["status"] == "failed"
    recorded = json.dumps(world.store.records(run_id))
    assert "private-capability" not in recorded and "private-control-secret" not in recorded
    world.store.close()


async def test_preflight_requires_registered_actual_fixed_bank_profile(tmp_path):
    from worldd.app import create_app
    from worldd.gateway import Gateway
    from worldd.models import RunConfig

    async def resolver(host, port):
        return ["93.184.216.34"]

    wire_calls = []

    def observed_fixture(request):
        wire_calls.append(request.url.path)
        return httpx.Response(200, text="Observed fixture " + request.url.path)

    boundary = Gateway(
        tmp_path,
        {
            "domain_interval_s": 0,
            "domain_burst": 1000,
            "domain_max_requests": 1000,
            "global_requests_per_minute": 1000,
        },
        httpx.AsyncClient(transport=httpx.MockTransport(observed_fixture)),
        resolver=resolver,
    )
    world = create_app(tmp_path, gateway=boundary).state.world
    try:
        run_id = world.create_run(RunConfig(budget={"usd": 1}, packs=[]))["run_id"]
        canaries = world.services["canaries"]
        bank = canaries.register_block_bank(
            [
                {"url": f"https://fixture.example/{index}", "domain_class": "fixture"}
                for index in range(300)
            ]
        )
        profile = await canaries.crawl_profile(run_id, bank["bank_id"])
        assert len(wire_calls) == 301  # 300 actual resource requests plus robots.
        report = await world.services["preflight"].run(
            run_id, metadata={"block_profile": profile["artifact_path"]}, run_kill=False
        )
        assert report["checks"]["block_profile"]["status"] == "passed"
        forged = json.loads(Path(profile["artifact_path"]).read_text())
        forged["rates_by_domain_class"] = {"fixture": 0.99}
        forged_path = tmp_path / "instruments/forged-profile.json"
        forged_path.parent.mkdir()
        forged_path.write_text(json.dumps(forged))
        refused = await world.services["preflight"].run(
            run_id, metadata={"block_profile": str(forged_path)}, run_kill=False
        )
        assert refused["checks"]["block_profile"]["status"] == "failed"
    finally:
        await world.aclose()


async def test_preflight_requires_real_firewall_and_unprivileged_process_evidence(tmp_path):
    world = World(tmp_path)
    run_id = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    preflight = PreflightService(world)
    inspections = iter(
        [
            {"output_policy": "DROP", "drop_packets": 10, "harness_net_admin": False},
            {"output_policy": "DROP", "drop_packets": 13, "harness_net_admin": False},
        ]
    )
    result = await preflight.isolation(
        run_id, lambda argv: {"returncode": 1}, firewall_inspect=lambda: next(inspections)
    )
    assert result["passed"]
    assert world.store.records(run_id)[-1]["test"] == "fenced_egress"
    world.store.close()


async def test_kill_sentinel_is_read_without_following_symlinks(tmp_path):
    world = World(tmp_path)
    run_id = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    sentinel = tmp_path / "kill-switch"
    sentinel.write_text(run_id)
    preflight = PreflightService(world, sentinel_enabled=True)
    await preflight.recover()
    for _ in range(10):
        if world.ends:
            break
        await asyncio.sleep(0)
    assert world.ends == [run_id]
    assert not sentinel.exists()
    await preflight.aclose()
    world.store.close()


async def test_full_preflight_missing_evidence_is_explicit_and_hashed(tmp_path):
    from types import SimpleNamespace

    from worldd.util import digest

    world = World(tmp_path)
    world.gateway = SimpleNamespace(signer=None)
    run_id = world.store.create_run(
        {"budget": {"usd": 1}, "packs": [], "isolation_profile": "cooperative"}
    )["run_id"]
    result = await PreflightService(world).run(run_id, run_kill=False)
    assert not result["passed"]
    assert {
        "harness_environment",
        "kill_switch",
        "egress",
        "provider_canary",
        "block_profile",
        "versions",
        "evaluation_isolation",
    } <= set(result["unmeasured"])
    unsigned = {key: value for key, value in result.items() if key != "report_hash"}
    assert result["report_hash"] == digest(unsigned)
    saved = json.loads((tmp_path / "runs" / run_id / "preflight/report.json").read_text())
    assert saved == result
    assert world.store.records(run_id)[-1]["report_hash"] == result["report_hash"]
    world.store.close()


async def test_full_preflight_refuses_agent_visible_or_tampered_metadata(tmp_path):
    from types import SimpleNamespace

    world = World(tmp_path)
    world.gateway = SimpleNamespace(signer=None)
    run_id = world.store.create_run({"budget": {"usd": 1}, "packs": []})["run_id"]
    agent_path = tmp_path / "runs" / run_id / "work" / "egress.json"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_text(json.dumps({"observer": "world", "ip": "93.184.216.34"}))
    private_path = tmp_path / "instruments" / "egress.json"
    private_path.parent.mkdir()
    private_path.write_text(json.dumps({"observer": "world", "ip": "93.184.216.34"}))
    service = PreflightService(world)
    denied = await service.run(run_id, metadata={"egress": str(agent_path)}, run_kill=False)
    assert denied["checks"]["egress"]["status"] == "failed"
    alias = agent_path.with_name("aliased-private.json")
    alias.symlink_to(private_path)
    with pytest.raises(ValueError, match="world-private"):
        service._artifact(str(alias))
    corrupt = await service.run(
        run_id,
        metadata={"egress": {"path": str(private_path), "hash": "sha256:wrong"}},
        run_kill=False,
    )
    assert corrupt["checks"]["egress"]["status"] == "failed"
    world.store.close()


async def test_preflight_workspace_compares_actual_harness_bytes_to_iota_and_beliefs(tmp_path):
    import sys

    world = World(tmp_path)
    run_id = world.store.create_run(
        {"budget": {"usd": 1}, "packs": [], "iota": {"workspace": "fresh"}}
    )["run_id"]
    run = world.store.run_dir(run_id)
    (run / "work/out").mkdir(parents=True)
    (run / "world/beliefs").mkdir(parents=True)
    (run / "world/beliefs/source.json").write_text('{"source_trust":"initial"}')
    observed_beliefs = run / "world/beliefs"

    async def execute(argv):
        assert argv[3:] == ["/work", "/world/beliefs"]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *argv[1:3],
            str(run / "work"),
            str(observed_beliefs),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return {
            "returncode": process.returncode,
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
        }

    service = PreflightService(world)
    assert (await service.workspace(run_id, execute))["passed"]
    (run / "work/unexpected.txt").write_text("unregistered launcher data")
    altered = await service.workspace(run_id, execute)
    assert not altered["passed"] and not altered["checks"]["workspace_matches_iota"]
    (run / "work/unexpected.txt").unlink()
    observed_beliefs = tmp_path / "empty-beliefs"
    observed_beliefs.mkdir()
    hidden = await service.workspace(run_id, execute)
    assert not hidden["passed"] and not hidden["checks"]["harness_beliefs_match"]
    world.store.close()


async def test_preflight_workspace_import_requires_actual_sealed_snapshot_anchor(tmp_path):
    import shutil

    from worldd.workspaces import snapshot_manifest, snapshot_workspace

    world = World(tmp_path)
    source_id = world.store.create_run({"budget": {"usd": 1}, "packs": []})["run_id"]
    source = world.store.run_dir(source_id)
    (source / "work/out").mkdir(parents=True)
    (source / "work/notes").write_text("source notes")
    (source / "work/current").symlink_to("notes")
    snapshot_workspace(source / "work", source / "work-snapshot")
    manifest = snapshot_manifest(source / "work-snapshot")
    world.store.append_record(source_id, "workspace_snapshot", manifest=manifest)
    world.store.append_record(source_id, "seal")
    world.store.update_run(source_id, state="sealed")
    run_id = world.store.create_run(
        {"budget": {"usd": 1}, "packs": [], "iota": {"workspace": f"import({source_id})"}}
    )["run_id"]
    run = world.store.run_dir(run_id)
    shutil.copytree(source / "work-snapshot", run / "work", symlinks=True)
    service = PreflightService(world)

    async def execute(argv):
        return {
            "returncode": 0,
            "stdout": json.dumps({"work": service._tree_manifest(run / "work"), "beliefs": None}),
        }

    assert (await service.workspace(run_id, execute))["passed"]
    (source / "work-snapshot/notes").write_text("snapshot tampered after seal")
    with pytest.raises(ValueError, match="sealed content manifest"):
        await service.workspace(run_id, execute)
    world.store.close()


async def test_preflight_t0_uses_registered_life_artifact_and_actual_probe_children(tmp_path):
    from types import SimpleNamespace

    from worldd.storage import content_hash

    world = World(tmp_path)
    world.gateway = SimpleNamespace(signer=None)
    child = world.store.create_run(
        {"budget": {"usd": 1}, "metering_lane": "probe", "model_snapshot_id": "pinned"}
    )["run_id"]
    world.store.append_record(
        child, "model_call", model="pinned", usage={"input": 1, "output": 1}, usd=0
    )
    world.store.append_record(child, "seal")
    world.store.update_run(child, state="sealed")
    report = {
        "study": "t0",
        "life_id": "life_1",
        "completed": True,
        "scientific": True,
        "model_snapshot_id": "pinned",
        "measurement_run_ids": [child],
    }
    path = tmp_path / "lives/life_1/t0.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report))
    life = {
        "life_id": "life_1",
        "t0_fingerprint_ref": content_hash(report),
        "driver": {"reading_at_start": 1.5},
    }
    world.services["lives"] = SimpleNamespace(get=lambda life_id: life)
    run_id = world.store.create_run(
        {
            "budget": {"usd": 1},
            "packs": [],
            "life_id": "life_1",
            "model_snapshot_id": "pinned",
            "research_mode": "research",
        }
    )["run_id"]
    service = PreflightService(world)
    actual = await service.run(run_id, run_kill=False)
    assert actual["checks"]["t0_battery"]["status"] == "passed"
    assert actual["checks"]["driver_reading_at_start"]["status"] == "passed"
    forged = tmp_path / "instruments/claimed-t0.json"
    forged.parent.mkdir()
    forged.write_text(
        json.dumps(
            {
                "observer": "world",
                "completed": True,
                "forked_context": True,
                "fingerprint_ref": "invented",
            }
        )
    )
    denied = await service.run(run_id, t0_ref=str(forged), run_kill=False)
    assert denied["checks"]["t0_battery"]["status"] == "failed"
    path.write_text(json.dumps({**report, "measurement_run_ids": []}))
    changed = await service.run(run_id, run_kill=False)
    assert changed["checks"]["t0_battery"]["status"] == "failed"
    world.store.close()
