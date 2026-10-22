import json
from types import SimpleNamespace

import httpx
import pytest

from worldd.footprint import FootprintService
from worldd.footprint_probe import RegisteredHTTPProbe
from worldd.gateway import Gateway
from worldd.storage import Store


async def test_registered_probe_is_read_only_separate_private_and_survives_sealed_parent(
    tmp_path, monkeypatch
):
    store, requests = Store(tmp_path), []
    run = store.create_run({"budget": {"usd": 1, "wall_s": 120}})
    run_id = run["run_id"]

    async def dns(host, port):
        assert host == "native.example"
        return ["93.184.216.34"]

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert "landing-party-probe/0.1 observer-one" in request.headers["user-agent"]
        assert run_id not in request.headers["user-agent"]
        if request.url.path == "/robots.txt":
            assert "authorization" not in request.headers
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        assert request.headers["authorization"] == "Bearer private-probe-read-key"
        return httpx.Response(200, json={"followers": 12, "private": "not-an-agent-observation"})

    monkeypatch.setenv("WORLD_NATIVE_PROBE_KEY", "private-probe-read-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        primary = Gateway(tmp_path, {"domain_interval_s": 0}, resolver=dns)
        world = SimpleNamespace(store=store, gateway=primary, services={})
        footprint = FootprintService(world)
        world.services["footprint"] = footprint
        adapter = RegisteredHTTPProbe(
            world,
            source_url="https://native.example/accounts/party",
            egress_id="observer-egress",
            identity_id="observer-one",
            proxy_url="http://probe-route.example:8080",
            selector="/followers",
            key_env="WORLD_NATIVE_PROBE_KEY",
            probe_views=1,
            client=client,
        )
        footprint.probe_adapters["native"] = adapter
        item = footprint.record_item(run_id, None, kind="follower", platform="native")
        footprint.schedule_probe(
            item["item_id"], platform="native", url="https://native.example/accounts/party"
        )
        store.append_record(run_id, "seal")
        store.update_run(run_id, state="sealed")
        original = store.records(run_id)
        reading = (await footprint.probe_due())[0]
        assert (
            reading["value"] == 11
            and reading["author_class"] == "probe"
            and not reading["grounded_eligible"]
        )
        assert store.records(run_id) == original
        child_id = reading["evidence_ref"].split(":")[0]
        child = store.get_run(child_id)
        assert child["state"] == "sealed" and child["config"]["private_archive"]
        assert store.verify_record(child_id)["valid"]
        assert list((store.run_dir(child_id) / "private-responses").glob("*"))
        assert not list((store.run_dir(run_id) / "world" / "fetched").glob("*"))
        record = (store.run_dir(child_id) / "record.jsonl").read_text()
        assert "private-probe-read-key" not in record and "not-an-agent-observation" not in record
        denied = await adapter(
            {
                "item_id": item["item_id"],
                "url": "https://native.example/accounts/party/%2e%2e/other",
            }
        )
        assert denied["rule_id"] == "probe_source_scope"
        assert len(requests) == 2
        await footprint.aclose()
        await primary.aclose()
    store.close()


async def test_probe_missing_metric_is_unmeasured_and_registration_is_durable(tmp_path):
    store = Store(tmp_path)
    gateway = Gateway(tmp_path)
    world = SimpleNamespace(store=store, gateway=gateway, services={})
    service = FootprintService(world)
    metadata = service.register_probe_adapter(
        "native",
        source_url="https://native.example/api",
        egress_id="different",
        identity_id="observer",
        proxy_url="http://probe.example:8080",
        selector="/missing",
    )
    assert metadata["agent_lease"] is False and "unmeasured" in metadata["actual_distinct_egress"]
    with pytest.raises(KeyError):
        service.probe_adapters["native"].extract(json.dumps({"other": 1}).encode())
    await service.aclose()
    restored = FootprintService(world)
    assert restored.probe_adapters["native"].metadata() == metadata
    with pytest.raises(ValueError, match="credential"):
        RegisteredHTTPProbe(
            world,
            source_url="https://native.example/api",
            egress_id="different",
            identity_id="observer",
            proxy_url="http://probe.example:8080",
            key_env="WORLD_RUN_TOKEN",
        )
    await restored.aclose()
    await gateway.aclose()
    store.close()
