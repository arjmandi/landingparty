import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from worldd.extraction import EXTRACTOR_ID
from worldd.gateway import Gateway
from worldd.lane import load_prices, normalize_usage, price_table_id, usage_cost
from worldd.preflight import PreflightService
from worldd.signing import WorldSigner
from worldd.storage import Store
from worldd.util import digest


def setup(tmp_path, *, browse=False):
    store = Store(tmp_path)
    world = SimpleNamespace(
        store=store,
        services={},
        gateway=SimpleNamespace(signer=WorldSigner(tmp_path, "https://identity.example/keys")),
        price_table_id=price_table_id(),
    )
    run_id = store.create_run(
        {
            "budget": {"usd": 1},
            "packs": ["browse"] if browse else [],
            "harness_id": "manual@0.1",
            "model_snapshot_id": "gpt-4o-2024-08-06",
            "browser_identity": "signed",
            "tuple": {"extractor_id": EXTRACTOR_ID},
        }
    )["run_id"]
    service = PreflightService(world)

    async def execute(argv):
        assert argv[-1] == "manual@0.1"
        return {
            "returncode": 0,
            "stdout": '{"declared_id":"manual@0.1","observed_version":"0.1.0"}',
        }

    service.register_harness(run_id, execute=execute)
    return world, service, run_id


async def test_source_observer_runs_actual_configured_binary_and_binds_signing_key(tmp_path):
    world, service, run_id = setup(tmp_path, browse=True)
    binary = tmp_path / "browser-fixture"
    binary.write_text(
        "#!"
        + sys.executable
        + "\nimport os\nassert '--version' in __import__('sys').argv\nassert 'WORLD_CONTROL_TOKEN' not in os.environ\nprint('Chromium fixture-build')\n"
    )
    binary.chmod(0o700)
    world.services["browser"] = SimpleNamespace(
        _injected_backend=False,
        backend=SimpleNamespace(
            executable_path=str(binary),
            child_environment=lambda: {"PATH": os.environ.get("PATH", "")},
        ),
    )
    result = await service.observe_sources(run_id)
    versions = json.loads(Path(result["metadata"]["versions"]["path"]).read_text())
    assert versions["browser_build"] == "Chromium fixture-build"
    assert versions["browser_observation"]["executable_hash"] == digest(binary.read_bytes())
    assert versions["signing_key_id"] == world.gateway.signer.key_id
    assert versions["signing_directory_hash"] == digest(world.gateway.signer.directory())
    assert service._source_observer.registered(run_id, "versions", versions)
    assert versions["status"] == "partial" and versions["search_response_date"] is None
    assert not result["metadata"]["versions"]["path"].endswith("/versions.json")
    world.store.close()


async def test_provider_artifact_derives_actual_capture_usage_without_inventing_billed_usd(
    tmp_path,
):
    world, service, run_id = setup(tmp_path)
    model = "gpt-4o-2024-08-06"
    source = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    usage = {"prompt_tokens": 100, "completion_tokens": 10}
    normalized = normalize_usage("openai", usage)
    amount = usage_cost(normalized, load_prices()["models"][model])
    assert world.store.debit(source, "model", amount)
    world.store.append_record(
        source,
        "model_call",
        call_id="call-1",
        provider="openai",
        model=model,
        usage=normalized,
        usd=float(amount),
        price_table_id=world.price_table_id,
        fixture_transport=True,
        upstream_origin="https://api.openai.com",
        transport_type="MockTransport",
    )
    response = json.dumps({"model": model, "usage": usage})
    captured = json.dumps(
        {"request": {"model": model, "stream": False, "tools": []}, "response": response}
    ).encode()
    path = world.store.run_dir(source) / "model-io/call-1.json"
    path.parent.mkdir()
    path.write_bytes(captured)
    world.store.append_record(
        source,
        "model_capture",
        call_id="call-1",
        capture_ref="model-io/call-1.json",
        capture_hash=digest(captured),
        response_hash=digest(response.encode()),
    )
    world.store.append_record(source, "seal")
    world.store.update_run(source, state="sealed")
    result = await service.observe_sources(
        run_id, provider_run_id=source, provider_call_id="call-1"
    )
    value = json.loads(Path(result["metadata"]["provider_canary"]["path"]).read_text())
    assert value["status"] == "measured" and value["usage"] == normalized
    assert value["provider_usage_price_usd"] == str(amount)
    assert value["provider_reported_usd"] is None and value["fixture_transport"] is True
    assert value["provider_response_hash"] == digest(response.encode())
    path.write_text("capture changed after seal")
    invalid = await service.observe_sources(
        run_id, provider_run_id=source, provider_call_id="call-1"
    )
    broken = json.loads(Path(invalid["metadata"]["provider_canary"]["path"]).read_text())
    assert broken["status"] == "unmeasured"
    world.store.close()


async def test_missing_browser_binary_and_blocked_egress_stay_unmeasured(tmp_path):
    world, service, run_id = setup(tmp_path, browse=True)
    world.services["browser"] = SimpleNamespace(
        _injected_backend=False,
        backend=SimpleNamespace(executable_path="/nonexistent/chromium", child_environment=dict),
    )

    async def fetch_kernel(run_id, url, **kwargs):
        return {"kind": "blocked", "rule_id": "robots_unavailable"}

    world.fetch_kernel = fetch_kernel
    result = await service.observe_sources(run_id, egress_url="https://api.ipify.org?format=json")
    versions = json.loads(Path(result["metadata"]["versions"]["path"]).read_text())
    egress = json.loads(Path(result["metadata"]["egress"]["path"]).read_text())
    assert versions["browser_build"] is None and versions["status"] == "partial"
    assert egress["status"] == "unmeasured" and egress["rule_id"] == "robots_unavailable"
    assert egress["ip"] is None and egress["reverse_dns"] is None
    world.store.close()


async def test_search_date_requires_actual_private_body_and_sealed_header_anchor(tmp_path):
    world, service, run_id = setup(tmp_path)
    parent = world.store.create_run({"budget": {"usd": 1}})["run_id"]
    child = world.store.create_run({"budget": {"usd": 1}, "fixture_transport": True})["run_id"]

    async def resolver(host, port):
        return ["93.184.216.34"]

    remote = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"web": {"results": []}},
                headers={"Date": "Thu, 22 Oct 2026 12:00:00 GMT"},
            )
        )
    )
    gateway = Gateway(tmp_path, client=remote, resolver=resolver)
    reply = await gateway.request(
        child,
        "a_private",
        "GET",
        "https://api.search.brave.com/res/v1/web/search",
        trusted={
            "allow": True,
            "response_private": True,
            "provider_operation": "search",
            "allowed_provider_host": "api.search.brave.com",
            "reservation_id": "fixture-hold",
        },
    )
    world.store.append_record(
        child,
        "outcome",
        action_id="a_private",
        status=200,
        resource_hash=reply["hash"],
        response_headers_subset=reply["headers_subset"],
        response_headers_hash=digest(reply["headers_subset"]),
    )
    world.store.append_record(child, "seal")
    world.store.update_run(child, state="sealed")
    world.store.append_record(parent, "action", tool="search", action_id="a_search")
    world.store.append_record(
        parent, "network_boundary", realm="search", action_id="a_search", observation_run_id=child
    )
    world.store.append_record(parent, "seal")
    world.store.update_run(parent, state="sealed")
    await service.observe_sources(run_id)
    result = service._source_observer.search_date(parent, "a_search", "brave")
    assert result["date"] == "2026-10-22T12:00:00+00:00" and result["fixture_transport"] is True
    index = world.store.run_dir(child) / "archive-index.json"
    payload = json.loads(index.read_text())
    next(iter(payload.values()))["reply"]["headers_subset"]["date"] = (
        "Thu, 22 Oct 2026 13:00:00 GMT"
    )
    index.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="sealed receipt anchor"):
        service._source_observer.search_date(parent, "a_search", "brave")
    await remote.aclose()
    world.store.close()
