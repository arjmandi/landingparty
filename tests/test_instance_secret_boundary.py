"""Keys saved through the product API must be guarded without process globals."""

import asyncio
import base64
import json
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from worldd.app import create_app
from worldd.gateway import Gateway
from worldd.instance_settings import InstanceSettings
from worldd.models import RunConfig
from worldd.provider_boundary import _mail_body_authorized
from worldd.scanning import contains_configured_secret, contains_sensitive_data

KEY = "opaqueBraveFixtureCredentialForThisInstance"


async def public_dns(host, port):
    return ["93.184.216.34"]


@pytest.fixture
async def worlds(tmp_path):
    items, calls = [], []

    def upstream(request):
        calls.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text="Owned fixture", headers={"content-type": "text/plain"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        for name in ("one", "two"):
            folder = tmp_path / name
            boundary = Gateway(folder, {"domain_interval_s": 0}, client, resolver=public_dns)
            world = create_app(folder, gateway=boundary).state.world
            items.append(world)
        items[0].instance_settings.update_provider("brave", api_key=KEY)
        yield items, calls
        for world in items:
            await world.aclose()


@pytest.mark.parametrize(
    "encode",
    [
        lambda value: value,
        quote,
        lambda value: value.encode().hex(),
        lambda value: value.encode().hex().upper(),
        lambda value: base64.b64encode(value.encode()).decode(),
    ],
)
def test_extra_secret_scanning_is_explicit_and_not_global(encode):
    encoded = encode(KEY)
    assert contains_sensitive_data(encoded, extra_secrets=(KEY,))
    assert contains_configured_secret(encoded, extra_secrets=(KEY,))
    assert not contains_sensitive_data(encoded)


async def test_saved_key_url_is_denied_before_dns_or_wire_and_not_logged(worlds):
    (world, other), calls = worlds
    info = world.create_run(RunConfig())
    url = "https://public.example/?q=" + KEY
    reply = await world.act(info["run_id"], "fetch", {"url": url})
    assert reply["rule_id"] == "exfiltration" and calls == []
    assert KEY not in (world.store.run_dir(info["run_id"]) / "record.jsonl").read_text()
    # Another world in the same process has its own saved keys. No global
    # registration changes its policy or environment.
    other_run = other.create_run(RunConfig())["run_id"]
    assert (await other.act(other_run, "fetch", {"url": url}))["kind"] == "result"
    assert calls


async def test_saved_key_search_and_batch_cannot_reenter_public_action_or_inbox_records(worlds):
    (world, _other), calls = worlds
    first = world.create_run(RunConfig())["run_id"]
    result = await world.act(first, "search", {"query": "find " + KEY})
    assert result["rule_id"] == "exfiltration" and calls == []
    second = world.create_run(RunConfig())["run_id"]
    batch = await world.act(second, "fetch_many", {"urls": ["https://public.example/?q=" + KEY]})
    assert batch["kind"] == "ticket"
    tasks = list(world.jobs[second].values())
    await asyncio.gather(*tasks, return_exceptions=True)
    assert calls == []
    events = world.store.inbox(second)["events"]
    assert any(event.get("type") == "job_done" for event in events)
    assert KEY not in json.dumps(events)
    assert KEY not in (world.store.run_dir(second) / "record.jsonl").read_text()


async def test_gateway_body_and_headers_follow_dynamic_instance_settings(worlds):
    (world, _other), calls = worlds
    boundary = world.gateway
    assert (
        boundary._syntax_policy("https://public.example/", {"Accept": KEY})["rule_id"]
        == "exfiltration"
    )
    result = await boundary.request(
        "r_body",
        "a_body",
        "POST",
        "https://public.example/",
        body=KEY.encode(),
        trusted={"allow": True, "allow_write": True},
    )
    assert result["rule_id"] == "exfiltration" and calls == []
    world.instance_settings.update_provider("exa", api_key="secondOpaqueInstanceFixtureKey")
    assert (
        boundary._syntax_policy("https://public.example/?q=secondOpaqueInstanceFixtureKey")[
            "rule_id"
        ]
        == "exfiltration"
    )
    world.instance_settings.update_provider("exa", enabled=False)
    assert (
        boundary._syntax_policy("https://public.example/?q=secondOpaqueInstanceFixtureKey")[
            "rule_id"
        ]
        == "exfiltration"
    )


def test_approved_mail_envelope_does_not_exempt_saved_opaque_or_encoded_provider_keys(tmp_path):
    settings = InstanceSettings(tmp_path, environment={})
    try:
        settings.update_provider("brave", api_key=KEY)
        world = SimpleNamespace(instance_settings=settings)
        approved = {"recipient": "recipient@example.org", "sender": "sender@example.org"}

        def request(text):
            return httpx.Request(
                "POST",
                "https://mail.example/send",
                json={"to": approved["recipient"], "body": text},
            )

        assert _mail_body_authorized(
            world, "r_mail", request("Approved harmless message"), approved
        )
        for value in (KEY, base64.b64encode(KEY.encode()).decode(), KEY.encode().hex()):
            assert not _mail_body_authorized(world, "r_mail", request(value), approved)
    finally:
        settings.close()
