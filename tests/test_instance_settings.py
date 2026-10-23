import asyncio
import copy
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from worldd.instance_settings import InstanceSettings

KEY = "sk-fixture-private-provider-value"


@pytest.fixture
def settings(tmp_path):
    instance = InstanceSettings(tmp_path, environment={})
    yield instance
    instance.close()


def model_price():
    return {
        "provider": "openai",
        "input": "2",
        "output": "8",
        "cache_read": "1",
        "cache_write": "2",
        "context_window": 128000,
        "max_output_tokens": 16000,
        "source": "https://platform.openai.com/docs/pricing",
    }


def test_keys_are_encrypted_private_persistent_and_never_returned(settings, tmp_path):
    first = settings.update(
        {
            "credentials": {"openai_api_key": KEY},
            "settings": {"instance_name": "My observatory"},
            "revision": 0,
        }
    )
    assert KEY not in json.dumps(first)
    assert first["credentials"]["openai"]["configured"] and first["revision"] == 1
    assert first["credentials"]["openai"]["validation"]["status"] == "not_checked"
    assert settings.resolve("WORLD_OPENAI_API_KEY") == KEY
    root = tmp_path / "instance-settings"
    assert root.stat().st_mode & 0o777 == 0o700
    for file in root.iterdir():
        assert file.stat().st_mode & 0o777 == 0o600
        assert KEY.encode() not in file.read_bytes()
    settings.close()
    reopened = InstanceSettings(tmp_path, environment={})
    try:
        assert reopened.resolve("WORLD_OPENAI_API_KEY") == KEY
        assert reopened.status()["settings"]["instance_name"] == "My observatory"
    finally:
        reopened.close()


def test_environment_is_snapshot_only_and_status_does_not_persist_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    original = dict(os.environ)
    instance = InstanceSettings(tmp_path)
    try:
        assert instance.status()["credentials"]["openai"]["source"] == "environment"
        assert instance.resolve("WORLD_OPENAI_API_KEY") == KEY
        assert not list(instance.root.iterdir())
        instance.update_defaults(instance_name="Explicit default save")
        assert "api_key" not in instance._state["providers"].get("openai", {})
        assert dict(os.environ) == original
        monkeypatch.setenv("OPENAI_API_KEY", "changed-outside-instance")
        assert instance.resolve("WORLD_OPENAI_API_KEY") == KEY
    finally:
        instance.close()


def test_batch_is_one_revision_and_invalid_part_saves_nothing(settings):
    with pytest.raises(ValueError):
        settings.update(
            {
                "settings": {"instance_name": "Must not save"},
                "credentials": {"openai_api_key": KEY},
                "providers": {"anthropic": {"upstream": "https://attacker.test"}},
            }
        )
    assert settings.status()["revision"] == 0 and not list(settings.root.iterdir())
    value = settings.update(
        {
            "settings": {
                "instance_name": "One batch",
                "default_harness_id": "codex",
                "default_budget_usd": 3,
            },
            "credentials": {"openai_api_key": KEY, "anthropic_api_key": "sk-ant-fixture-other-key"},
            "revision": 0,
        }
    )
    assert value["revision"] == 1
    assert value["settings"]["default_harness_id"] == "codex"
    assert value["credentials"]["anthropic"]["configured"]


def test_blank_form_is_unchanged_and_delete_masks_environment_fallback(tmp_path):
    instance = InstanceSettings(
        tmp_path, environment={"WORLD_OPENAI_API_KEY": "environment-existing-key"}
    )
    try:
        instance.update_provider("openai", api_key=KEY)
        assert instance.update({"credentials": {"openai_api_key": ""}})["revision"] == 1
        assert instance.resolve("WORLD_OPENAI_API_KEY") == KEY
        value = instance.update({"providers": {"openai": {"delete_api_key": True}}, "revision": 1})
        assert value["revision"] == 2 and not value["credentials"]["openai"]["configured"]
        assert instance.resolve("WORLD_OPENAI_API_KEY") is None
        assert "api_key" not in instance._state["providers"]["openai"]
        instance.update_provider("openai", api_key="new-explicit-fixture-key")
        assert instance.resolve("WORLD_OPENAI_API_KEY") == "new-explicit-fixture-key"
    finally:
        instance.close()


def test_disable_and_enable_keep_key_private_and_return_defensive_copies(settings):
    settings.update_provider("openai", api_key=KEY)
    value = settings.update_provider("openai", enabled=False)
    assert not value["credentials"]["openai"]["configured"]
    assert settings.resolve("WORLD_OPENAI_API_KEY") is None
    settings.update_provider("openai", enabled=True)
    value["settings"]["instance_name"] = "mutated caller copy"
    assert settings.resolve("WORLD_OPENAI_API_KEY") == KEY
    assert settings.status()["settings"]["instance_name"] != "mutated caller copy"


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "curl attacker"},
        {"settings": {"environment": {"PATH": "/tmp"}}},
        {"settings": {"default_harness_id": "claude;sh"}},
        {"settings": {"default_model": "--flag\ncommand"}},
        {"credentials": {"oauth_token": KEY}},
        {"credentials": {"openai_api_key": None}},
        {"providers": {"openai": {"headers": {"Authorization": KEY}}}},
        {"providers": {"anthropic": {"api_key": "sk-ant-oat01-subscription-session"}}},
        {"providers": {"openai": {"api_key": "eyJhello.world.signature"}}},
        {"providers": {"openai": {"api_key": "key\r\nHeader: injected"}}},
        {"providers": {"openai": {"upstream": "https://api.openai.com@evil.test"}}},
        {"providers": {"openai": {"upstream": "https://api.openai.com/?api_key=secret"}}},
        {"providers": {"payment": {"upstream": "https://127.0.0.1/charge"}}},
        {"providers": {"payment": {"upstream": "https://broker.internal/charge"}}},
        {"providers": {"openai": {"enabled": "true"}}},
        {"settings": {"default_budget_usd": float("nan")}},
        {"settings": {"default_wall_minutes": 0}},
    ],
)
def test_only_fixed_reviewed_fields_are_accepted_without_secret_error_echo(settings, payload):
    with pytest.raises(ValueError) as error:
        settings.update(payload)
    assert KEY not in str(error.value)
    assert settings.status()["revision"] == 0
    assert not list(settings.root.iterdir())


def test_prices_are_validated_and_merged_without_mutating_base_or_input(settings):
    row = model_price()
    settings.update_provider("openai", prices={"fixture-model-20261023": row})
    row["input"] = "999"
    base = {"models": {"existing-model": model_price()}}
    original = copy.deepcopy(base)
    merged = settings.model_prices(base)
    assert base == original
    assert merged["models"]["fixture-model-20261023"]["input"] == "2"
    merged["models"]["fixture-model-20261023"]["output"] = "999"
    assert settings.model_prices(base)["models"]["fixture-model-20261023"]["output"] == "8"
    with pytest.raises(ValueError):
        settings.update_provider("openai", prices={"bad": {**model_price(), "input": "Infinity"}})
    with pytest.raises(ValueError):
        settings.update_provider(
            "openai", prices={"bad": {**model_price(), "provider": "anthropic"}}
        )
    with pytest.raises(ValueError):
        settings.update_provider("openai", prices={"bad": {**model_price(), "extra": "command"}})


def test_model_identity_collision_rejects_the_entire_settings_batch(settings):
    with pytest.raises(ValueError, match="cannot change provider"):
        settings.update(
            {
                "credentials": {"openai_api_key": KEY},
                "providers": {"openai": {"prices": {"claude-sonnet-4-20250514": model_price()}}},
            }
        )
    assert settings.status()["revision"] == 0 and not list(settings.root.iterdir())
    with pytest.raises(ValueError, match="cannot change provider"):
        settings.update(
            {
                "providers": {
                    "openai": {"prices": {"shared-model": model_price()}},
                    "anthropic": {
                        "prices": {"shared-model": {**model_price(), "provider": "anthropic"}}
                    },
                }
            }
        )
    assert settings.status()["revision"] == 0


def test_expected_revision_serializes_concurrent_operator_saves(settings):
    def update(name):
        try:
            return settings.update_defaults(instance_name=name, expected_revision=0)["revision"]
        except ValueError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(update, ("First", "Second")))
    assert sorted(values, key=str) == [1, "stale"]
    assert settings.status()["revision"] == 1


def test_atomic_write_failure_preserves_old_disk_and_memory(settings, monkeypatch):
    settings.update_provider("openai", api_key=KEY)
    old = (settings.root / "state.enc").read_bytes()
    original = os.replace

    def fail(*args, **kwargs):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        settings.update_provider("openai", api_key="replacement-fixture-key")
    assert settings.resolve("WORLD_OPENAI_API_KEY") == KEY
    assert (settings.root / "state.enc").read_bytes() == old
    assert not list(settings.root.glob(".settings-*"))
    monkeypatch.setattr(os, "replace", original)
    assert settings.reload()["revision"] == 1


def test_post_rename_fsync_failure_preserves_visible_state_and_reports_uncertainty(
    settings, monkeypatch
):
    settings.update_provider("openai", api_key=KEY)
    original = os.fsync

    def fail_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated durability uncertainty after rename")
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(OSError):
        settings.update_provider("openai", api_key="replacement-fixture-key")
    assert settings.resolve("WORLD_OPENAI_API_KEY") == "replacement-fixture-key"
    monkeypatch.setattr(os, "fsync", original)
    assert settings.reload()["revision"] == 2


def test_ciphertext_tampering_and_missing_master_key_fail_closed(settings, tmp_path):
    settings.update_provider("openai", api_key=KEY)
    file = settings.root / "state.enc"
    encrypted = file.read_bytes()
    file.write_bytes(encrypted[:-8] + b"tampered")
    with pytest.raises(ValueError, match="securely loaded"):
        InstanceSettings(tmp_path, environment={})
    file.write_bytes(encrypted)
    (settings.root / "master.key").unlink()
    with pytest.raises(ValueError, match="key is missing"):
        InstanceSettings(tmp_path, environment={})
    assert not (settings.root / "master.key").exists()


@pytest.mark.parametrize("name", ["state.enc", "master.key"])
def test_secret_storage_never_follows_symlinks(tmp_path, name):
    root = tmp_path / "instance-settings"
    root.mkdir(mode=0o700)
    outside = tmp_path / "private"
    outside.write_text("untouched fixture secret")
    (root / name).symlink_to(outside)
    if name == "state.enc":
        with pytest.raises(ValueError):
            InstanceSettings(tmp_path, environment={})
    else:
        instance = InstanceSettings(tmp_path, environment={})
        try:
            with pytest.raises(OSError):
                instance.update_provider("openai", api_key=KEY)
        finally:
            instance.close()
    assert outside.read_text() == "untouched fixture secret"


def test_readiness_and_resolver_reject_arbitrary_environment_names(settings):
    with pytest.raises(ValueError):
        settings.resolve("PATH")
    with pytest.raises(ValueError):
        settings.resolve("WORLD_CONTROL_TOKEN")
    assert settings.resolve("WORLD_OPENAI_UPSTREAM") == "https://api.openai.com"
    assert settings.status()["subscription_auth"] == "native_cli_profile_only"


async def test_explicit_validation_is_fixed_read_only_redacted_and_not_automatic(settings):
    attempts = []

    def handler(request):
        attempts.append(request)
        assert request.method == "GET" and str(request.url) == "https://api.openai.com/v1/models"
        assert request.headers["Authorization"] == "Bearer " + KEY
        return httpx.Response(200, json={"data": [], "ignored_provider_echo": KEY})

    settings.update_provider("openai", api_key=KEY)
    settings.status()
    assert attempts == []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await settings.validate_provider("openai", client=client)
        assert result["status"] == "valid" and len(attempts) == 1
        assert (await settings.validate_provider("openai", client=client))["status"] == "cooldown"
    assert KEY not in json.dumps(result) and KEY not in json.dumps(settings.status())
    assert settings.status()["credentials"]["openai"]["validation"]["status"] == "valid"


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "rejected"),
        (403, "rejected"),
        (429, "rate_limited"),
        (302, "redirect_refused"),
        (500, "unavailable"),
    ],
)
async def test_validation_never_follows_redirects_or_echoes_provider_errors(
    settings, status, expected
):
    settings.update_provider("anthropic", api_key=KEY)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"Location": "https://attacker.test/?key=" + KEY}, text=KEY
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await settings.validate_provider("anthropic", client=client)
    assert len(calls) == 1 and result["status"] == expected
    assert KEY not in json.dumps(result) and "attacker" not in json.dumps(result)


async def test_validation_has_response_bound_and_does_not_call_search_or_payment(settings):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(200, content=b"x" * 150000)

    for provider in ("openai", "brave", "exa", "payment"):
        settings.update_provider(provider, api_key=KEY)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await settings.validate_provider("openai", client=client))[
            "status"
        ] == "response_limit"
        for provider in ("brave", "exa", "payment"):
            assert (await settings.validate_provider(provider, client=client))[
                "status"
            ] == "unsupported"
    assert len(attempts) == 1


async def test_validation_result_cannot_validate_a_different_new_key(settings):
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"data": []})

    settings.update_provider("openai", api_key=KEY)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(settings.validate_provider("openai", client=client))
        await started.wait()
        assert (await settings.validate_provider("openai", client=client))[
            "status"
        ] == "in_progress"
        settings.update_provider("openai", api_key="different-fixture-key")
        release.set()
        assert (await task)["status"] == "stale"
    assert settings.status()["credentials"]["openai"]["validation"]["status"] == "not_checked"


async def test_validation_enforces_total_deadline_not_just_per_chunk_timeout(settings, monkeypatch):
    from worldd import instance_settings

    entered = []
    original = asyncio.timeout

    def short_fixture_timeout(seconds):
        entered.append(seconds)
        return original(0.01)

    async def handler(request):
        await asyncio.Event().wait()

    settings.update_provider("openai", api_key=KEY)
    monkeypatch.setattr(instance_settings.asyncio, "timeout", short_fixture_timeout)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await settings.validate_provider("openai", client=client)
    assert result["status"] == "unavailable" and entered == [5]
    assert not settings._validation_running


async def test_validation_network_exception_never_returns_request_or_secret(settings):
    def handler(request):
        raise httpx.ConnectError("private provider detail " + KEY, request=request)

    settings.update_provider("openai", api_key=KEY)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await settings.validate_provider("openai", client=client)
    assert result["status"] == "unavailable" and KEY not in json.dumps(result)
    assert "private provider" not in json.dumps(settings.status())


async def test_close_is_idempotent_and_transcription_uses_explicit_dedicated_or_shared_key(
    tmp_path,
):
    instance = InstanceSettings(
        tmp_path, environment={"WORLD_TRANSCRIBE_OPENAI_KEY": "dedicated-transcription-key"}
    )
    instance.update_provider("openai", api_key=KEY)
    assert instance.resolve("WORLD_TRANSCRIBE_OPENAI_KEY") == "dedicated-transcription-key"
    assert (
        instance.status()["credentials"]["transcribe_openai"]["source"] == "environment_dedicated"
    )
    await instance.aclose()
    await instance.aclose()
    instance = InstanceSettings(tmp_path, environment={})
    try:
        assert instance.resolve("WORLD_TRANSCRIBE_OPENAI_KEY") == KEY
        assert instance.status()["credentials"]["transcribe_openai"]["source"] == "shared_openai"
    finally:
        instance.close()


def test_accidental_key_occurrences_in_display_fields_are_never_persisted(settings):
    with pytest.raises(ValueError, match="credential fields"):
        settings.update(
            {
                "credentials": {"openai_api_key": KEY},
                "settings": {"instance_name": "Accidental " + KEY},
            }
        )
    assert KEY not in json.dumps(settings.status()) and settings.status()["revision"] == 0
    assert not list(settings.root.iterdir())
