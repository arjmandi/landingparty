"""Encrypted, operator-owned instance configuration, separate from agent accounts.

The HTTP layer must expose only status/update/delete/explicit validation. ``resolve``
and ``model_prices`` are trusted server adapters; neither belongs on an agent API.
Native subscription credentials remain in the native CLI's private profile.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .harness_config import BUILTINS
from .storage import canonical_json
from .workspaces import DIRECTORY_FLAGS, FILE_FLAGS, _directory

_UNSET = object()
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_MAX_STATE = 512 * 1024
_MODEL_FIELDS = {
    "provider",
    "input",
    "output",
    "cache_read",
    "cache_write",
    "cache_write_1h",
    "context_window",
    "max_output_tokens",
    "source",
    "long_context",
}
_TIER_RATES = ("input", "output", "cache_read", "cache_write", "cache_write_1h")
DEFAULT_SETTINGS = {
    "instance_name": "Landing Party",
    "default_harness_id": "claude-code",
    "default_model": "claude-sonnet-5-5",
    "default_budget_usd": 20,
    "default_wall_minutes": 120,
}


def _provider(name, host, *, kind, unit=None, validation=None):
    upper = name.upper()
    return {
        "id": name,
        "key_binding": f"WORLD_{upper}_API_KEY",
        "key_alias": f"{upper}_API_KEY",
        "upstream": "https://" + host if host else None,
        "upstream_binding": f"WORLD_{upper}_UPSTREAM"
        if kind == "model"
        else "WORLD_PAYMENT_API_URL"
        if name == "payment"
        else None,
        "kind": kind,
        "unit": unit,
        "validation": validation,
    }


PROVIDERS = {
    "openai": _provider("openai", "api.openai.com", kind="model", validation="/v1/models"),
    "anthropic": _provider("anthropic", "api.anthropic.com", kind="model", validation="/v1/models"),
    "brave": _provider("brave", "api.search.brave.com", kind="search", unit="usd_per_call"),
    "exa": _provider("exa", "api.exa.ai", kind="search", unit="usd_per_call"),
    "deepgram": _provider("deepgram", "api.deepgram.com", kind="perception", unit="usd_per_minute"),
    "assemblyai": _provider(
        "assemblyai", "api.assemblyai.com", kind="perception", unit="usd_per_minute"
    ),
    "mistral": _provider(
        "mistral", "api.mistral.ai", kind="perception", unit="usd_per_page", validation="/v1/models"
    ),
    "payment": _provider("payment", None, kind="payment"),
}
_UNIT_BINDINGS = {
    "deepgram": "WORLD_DEEPGRAM_USD_PER_MINUTE",
    "assemblyai": "WORLD_ASSEMBLYAI_USD_PER_MINUTE",
    "mistral": "WORLD_MISTRAL_OCR_USD_PER_PAGE",
}
_BINDINGS = {
    binding: (provider, kind)
    for provider, spec in PROVIDERS.items()
    for binding, kind in ((spec["key_binding"], "key"), (spec["upstream_binding"], "upstream"))
    if binding
}
_BINDINGS.update({binding: (provider, "unit") for provider, binding in _UNIT_BINDINGS.items()})
_BINDINGS["WORLD_TRANSCRIBE_OPENAI_KEY"] = ("openai", "transcribe_key")


def _now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _decimal(value, *, maximum=1_000_000):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("A finite nonnegative declared price is required")  # noqa: TRY004
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("A finite nonnegative declared price is required") from None
    if not result.is_finite() or result < 0 or result > maximum:
        raise ValueError("Declared price is outside the supported range")
    return str(result)


def _upstream(provider, value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Provider upstream must be a reviewed HTTPS URL")
    try:
        parsed = urlsplit(value)
        valid = parsed.port in (None, 443)
    except ValueError:
        valid = False
        parsed = None
    if (
        not valid
        or not parsed
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(character.isspace() or ord(character) < 32 for character in value)
        or "\\" in value
    ):
        raise ValueError("Provider upstream must be a credential-free reviewed HTTPS URL")
    if provider != "payment":
        expected = PROVIDERS[provider]["upstream"]
        if value.rstrip("/") != expected:
            raise ValueError("Only the reviewed official provider upstream is supported")
        return expected
    # Payment brokers are operator-selected, but the runtime still verifies its
    # exact registered endpoint, mandate and gateway destination policy.
    host = parsed.hostname.lower()
    if (
        "." not in host
        or host.endswith((".local", ".localhost", ".internal"))
        or host.replace(".", "").isdigit()
        or ":" in host
        or not re.fullmatch(r"[a-z0-9.-]+", host)
    ):
        raise ValueError("Payment broker must use a public HTTPS hostname")
    return value.rstrip("/")


def _secret(value):
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 8192
        or value != value.strip()
        or not value.isascii()
        or any(
            character.isspace() or ord(character) < 33 or ord(character) == 127
            for character in value
        )
        or value.lower().startswith(("bearer ", "sk-ant-oat", "sess-"))
        or (value.count(".") == 2 and value.startswith("eyJ"))
    ):
        raise ValueError("Enter an API key only; native subscription login uses its CLI profile")
    return value


def _long_context(tier, row):
    """A long-prompt tier: a threshold inside the window and every base rate repriced."""
    threshold = tier.get("above_prompt_tokens") if isinstance(tier, dict) else None
    if (
        not isinstance(tier, dict)
        or set(tier) - {"above_prompt_tokens", *_TIER_RATES}
        or isinstance(threshold, bool)
        or not isinstance(threshold, int)
        or not 1 <= threshold < row["context_window"]
        or {name for name in _TIER_RATES if name in row} - set(tier)
    ):
        raise ValueError("A long-context tier needs a threshold and a rate for every base rate")
    return {
        "above_prompt_tokens": threshold,
        **{name: _decimal(tier[name]) for name in _TIER_RATES if name in tier},
    }


def _prices(provider, prices):
    if not isinstance(prices, dict) or len(prices) > 128:
        raise ValueError("Provider prices must be a bounded reviewed object")
    spec = PROVIDERS[provider]
    if spec["kind"] != "model":
        if not spec["unit"] or set(prices) != {spec["unit"]}:
            raise ValueError("Only the provider's declared unit price is supported")
        return {spec["unit"]: _decimal(prices[spec["unit"]])}
    result = {}
    for model, row in prices.items():
        if (
            not isinstance(model, str)
            or not _MODEL.fullmatch(model)
            or not isinstance(row, dict)
            or set(row) - _MODEL_FIELDS
            or row.get("provider", provider) != provider
            or not {
                "input",
                "output",
                "cache_read",
                "cache_write",
                "context_window",
                "max_output_tokens",
                "source",
            }
            <= set(row)
        ):
            raise ValueError(
                "Each model needs an exact identifier and complete declared price fields"
            )
        normalized = {"provider": provider}
        for name in ("input", "output", "cache_read", "cache_write", "cache_write_1h"):
            if name in row:
                normalized[name] = _decimal(row[name])
        for name in ("context_window", "max_output_tokens"):
            value = row[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 10_000_000
            ):
                raise ValueError(
                    "Model context and output limits must be bounded positive integers"
                )
            normalized[name] = value
        if normalized["max_output_tokens"] > normalized["context_window"]:
            raise ValueError("Model output limit cannot exceed its context window")
        if "long_context" in row:
            normalized["long_context"] = _long_context(row["long_context"], normalized)
        source = row["source"]
        try:
            parsed = urlsplit(source) if isinstance(source, str) else None
        except ValueError:
            parsed = None
        if (
            not parsed
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or len(source) > 2048
            or any(character.isspace() or ord(character) < 32 for character in source)
        ):
            raise ValueError("Declared prices require a credential-free HTTPS source reference")
        normalized["source"] = source
        result[model] = normalized
    return result


class InstanceSettings:
    """One trusted daemon's encrypted configuration; no global environment edits."""

    def __init__(self, data_dir, *, environment=None):
        self._lock = threading.RLock()
        self.root = Path(os.path.abspath(data_dir)) / "instance-settings"
        # What each provider's key actually serves, from its last validation. Plain
        # metadata kept outside the encrypted state, so an older build still loads it.
        self.model_lists = Path(os.path.abspath(data_dir)) / "model-lists"
        with _directory(self.root.parent) as parent:
            try:
                os.mkdir(self.root.name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            self._descriptor = os.open(self.root.name, DIRECTORY_FLAGS, dir_fd=parent)
        info = os.fstat(self._descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            os.close(self._descriptor)
            raise ValueError("Instance settings directory requires private owner-only permissions")
        allowed = set(_BINDINGS) | {spec["key_alias"] for spec in PROVIDERS.values()}
        source = os.environ if environment is None else environment
        self._environment = {key: source[key] for key in allowed if key in source}
        self._fernet = None
        self._validation_running = set()
        self._last_validation = {}
        self._state = {"schema_version": 1, "revision": 0, "settings": {}, "providers": {}}
        try:
            self.reload()
        except BaseException:
            self.close()
            raise

    def _read(self, name, maximum):
        try:
            descriptor = os.open(name, FILE_FLAGS, dir_fd=self._descriptor)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > maximum
            ):
                raise ValueError("Instance settings files require private regular storage")
            chunks, size = [], 0
            while block := os.read(descriptor, min(65536, maximum + 1 - size)):
                chunks.append(block)
                size += len(block)
                if size > maximum:
                    raise ValueError("Instance settings file exceeds its size bound")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _cipher(self):
        if self._fernet is None:
            material = self._read("master.key", 32)
            if material is None:
                material = secrets.token_bytes(32)
                self._atomic("master.key", material, replace=False)
            if len(material) != 32:
                raise ValueError("Invalid instance settings key")
            # Same authenticated encryption as the vault, with a separate key
            # and namespace; no delegation profile can unlock these values.
            self._fernet = Fernet(base64.urlsafe_b64encode(material))
        return self._fernet

    def _atomic(self, name, body, *, replace=True):
        temporary = ".settings-" + secrets.token_hex(12)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self._descriptor,
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            if replace:
                # Existing links are rejected, even though replacing a link
                # itself would not follow it.
                try:
                    current = os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if current and (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
                    raise ValueError("Instance settings cannot replace linked/special storage")
                os.replace(
                    temporary, name, src_dir_fd=self._descriptor, dst_dir_fd=self._descriptor
                )
            else:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=self._descriptor,
                    dst_dir_fd=self._descriptor,
                    follow_symlinks=False,
                )
                os.unlink(temporary, dir_fd=self._descriptor)
            os.fsync(self._descriptor)
        finally:
            try:
                os.unlink(temporary, dir_fd=self._descriptor)
            except FileNotFoundError:
                pass

    def _commit(self, value):
        value["revision"] = self._state["revision"] + 1
        self._validate_state(value)
        public = copy.deepcopy(value)
        keys = []
        for fields in public["providers"].values():
            if secret := fields.pop("api_key", None):
                keys.append(secret)
        keys.extend(
            fields["api_key"]
            for fields in self._state["providers"].values()
            if fields.get("api_key")
        )
        keys.extend(
            secret
            for name, secret in self._environment.items()
            if name.endswith("API_KEY") or name == "WORLD_TRANSCRIBE_OPENAI_KEY"
        )

        def contains_secret(item):
            if isinstance(item, str):
                return any(isinstance(key, str) and len(key) >= 8 and key in item for key in keys)
            if isinstance(item, dict):
                return any(
                    contains_secret(key) or contains_secret(value) for key, value in item.items()
                )
            if isinstance(item, list):
                return any(contains_secret(value) for value in item)
            return False

        if contains_secret(public):
            raise ValueError("API keys may only appear in credential fields")
        raw = canonical_json(value).encode()
        if len(raw) > _MAX_STATE // 2:
            raise ValueError("Instance configuration exceeds its bounded storage")
        encrypted = self._cipher().encrypt(raw)
        try:
            self._atomic("state.enc", encrypted)
        except OSError:
            # A directory fsync can fail after the atomic rename. Keep memory
            # consistent with the bytes actually visible, while still reporting
            # that durable persistence could not be confirmed.
            if self._read("state.enc", _MAX_STATE) == encrypted:
                self._state = value
            raise
        self._state = value

    def reload(self):
        """Explicit disk reload; never imports environment keys into persisted state."""
        with self._lock:
            try:
                raw = self._read("state.enc", _MAX_STATE)
                if raw is None:
                    if self._state["revision"]:
                        raise ValueError("Existing instance configuration is missing")
                    return self.status()
                # Never generate a replacement key for existing encrypted state.
                if self._read("master.key", 32) is None:
                    raise ValueError("Instance settings key is missing")
                state = json.loads(self._cipher().decrypt(raw))
                self._validate_state(state)
            except (InvalidToken, UnicodeError, json.JSONDecodeError, OSError):
                raise ValueError("Instance settings could not be securely loaded") from None
            if state["revision"] < self._state["revision"]:
                raise ValueError("Instance settings revision moved backwards")
            self._state = state
            return self.status()

    def _validate_state(self, value):
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "revision", "settings", "providers"}
            or value["schema_version"] != 1
            or isinstance(value["revision"], bool)
            or not isinstance(value["revision"], int)
            or value["revision"] < 1
            or not isinstance(value["providers"], dict)
            or set(value["providers"]) - set(PROVIDERS)
        ):
            raise ValueError("Invalid encrypted instance settings schema")
        self._defaults(value["settings"])
        declared = json.loads((Path(__file__).parents[1] / "policy/prices.json").read_text())
        owners = {name: row["provider"] for name, row in declared.get("models", {}).items()}
        for provider, fields in value["providers"].items():
            if not isinstance(fields, dict) or set(fields) - {
                "api_key",
                "key_deleted",
                "enabled",
                "upstream",
                "prices",
                "reviewed_at",
                "validation",
            }:
                raise ValueError("Invalid encrypted provider settings schema")
            if "api_key" in fields:
                _secret(fields["api_key"])
            if "upstream" in fields:
                _upstream(provider, fields["upstream"])
            if "prices" in fields:
                normalized_prices = _prices(provider, fields["prices"])
                if PROVIDERS[provider]["kind"] == "model":
                    for name in normalized_prices:
                        if name in owners and owners[name] != provider:
                            raise ValueError("A priced model identifier cannot change provider")
                        owners[name] = provider
            if any(
                not isinstance(fields[key], bool)
                for key in ("key_deleted", "enabled")
                if key in fields
            ):
                raise ValueError("Invalid encrypted provider settings flags")
            if "validation" in fields:
                check = fields["validation"]
                if (
                    not isinstance(check, dict)
                    or set(check) - {"provider", "checked_at", "status", "http_status"}
                    or check.get("provider") != provider
                    or check.get("status")
                    not in {
                        "valid",
                        "unavailable",
                        "rejected",
                        "unexpected_response",
                        "response_limit",
                        "rate_limited",
                        "redirect_refused",
                    }
                    or not isinstance(check.get("checked_at"), str)
                    or (
                        "http_status" in check
                        and (
                            isinstance(check["http_status"], bool)
                            or not isinstance(check["http_status"], int)
                            or not 100 <= check["http_status"] <= 599
                        )
                    )
                ):
                    raise ValueError("Invalid private provider validation receipt")

    def _expected(self, revision):
        if revision is not None and (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision != self._state["revision"]
        ):
            raise ValueError("Settings changed; reload before saving")

    @staticmethod
    def _defaults(values):
        if not isinstance(values, dict) or set(values) - set(DEFAULT_SETTINGS):
            raise ValueError("Only reviewed instance default fields can be changed")
        result = dict(values)
        if "instance_name" in values:
            value = values["instance_name"]
            if (
                not isinstance(value, str)
                or not 1 <= len(value.strip()) <= 80
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Instance name must be 1 to 80 display characters")
            result["instance_name"] = value.strip()
        if "default_harness_id" in values and (
            not isinstance(values["default_harness_id"], str)
            or values["default_harness_id"] not in {*(m.id for m in BUILTINS), "manual"}
        ):
            raise ValueError("Select a supported native harness")
        if "default_model" in values and (
            not isinstance(values["default_model"], str)
            or not _MODEL.fullmatch(values["default_model"])
        ):
            raise ValueError("Select a bounded model identifier")
        for key, maximum in (("default_budget_usd", 100_000), ("default_wall_minutes", 43_200)):
            if key in values:
                value = Decimal(_decimal(values[key], maximum=maximum))
                if value <= 0:
                    raise ValueError("Default run limits must be positive")
                result[key] = float(value)
        return result

    def update_defaults(
        self,
        *,
        instance_name=_UNSET,
        default_harness_id=_UNSET,
        default_model=_UNSET,
        default_budget_usd=_UNSET,
        default_wall_minutes=_UNSET,
        expected_revision=None,
    ):
        values = {
            key: value
            for key, value in locals().items()
            if key in DEFAULT_SETTINGS and value is not _UNSET
        }
        normalized = self._defaults(values)
        with self._lock:
            self._expected(expected_revision)
            if normalized:
                state = copy.deepcopy(self._state)
                state["settings"].update(normalized)
                self._commit(state)
            return self.status()

    def update_provider(
        self,
        provider,
        *,
        api_key=_UNSET,
        upstream=_UNSET,
        prices=_UNSET,
        enabled=_UNSET,
        expected_revision=None,
    ):
        updates = self._provider_updates(
            provider, api_key=api_key, upstream=upstream, prices=prices, enabled=enabled
        )
        with self._lock:
            self._expected(expected_revision)
            if updates:
                state = copy.deepcopy(self._state)
                self._apply_provider(state, provider, updates)
                self._commit(state)
            return self.status()

    @staticmethod
    def _provider_updates(
        provider, *, api_key=_UNSET, upstream=_UNSET, prices=_UNSET, enabled=_UNSET
    ):
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise ValueError("Unknown reviewed provider")
        updates = {}
        if api_key is not _UNSET and api_key != "":
            updates["key_deleted"] = api_key is None
            if api_key is not None:
                updates["api_key"] = _secret(api_key)
        if upstream is not _UNSET:
            updates["upstream"] = _upstream(provider, upstream)
        if prices is not _UNSET:
            updates["prices"] = _prices(provider, prices)
            updates["reviewed_at"] = _now()
        if enabled is not _UNSET:
            if not isinstance(enabled, bool):
                raise ValueError("Provider enabled must be a boolean")
            updates["enabled"] = enabled
        return updates

    @staticmethod
    def _apply_provider(state, provider, updates):
        fields = state["providers"].setdefault(provider, {})
        fields.update(updates)
        if updates.get("key_deleted"):
            fields.pop("api_key", None)
        if {"api_key", "key_deleted", "upstream", "enabled"}.intersection(updates):
            fields.pop("validation", None)

    def update(self, payload: dict):
        """Validate and atomically save one canonical operator form submission.

        ``credentials`` accepts fixed ``openai_api_key``-style names. Blank
        password inputs mean unchanged. Explicit provider.delete_api_key=true
        or the dedicated delete method is required to clear a credential.
        """
        if not isinstance(payload, dict) or set(payload) - {
            "settings",
            "credentials",
            "providers",
            "revision",
        }:
            raise ValueError("Only reviewed instance settings fields are accepted")
        defaults = self._defaults(payload.get("settings", {}))
        credentials = payload.get("credentials", {})
        providers = payload.get("providers", {})
        names = {name + "_api_key": name for name in PROVIDERS}
        if (
            not isinstance(credentials, dict)
            or set(credentials) - set(names)
            or not isinstance(providers, dict)
            or set(providers) - set(PROVIDERS)
        ):
            raise ValueError("Only fixed provider credential fields are accepted")
        updates = {}
        for name, value in credentials.items():
            if value is None:
                raise ValueError("Use the explicit delete action to clear a credential")
            if value != "":
                updates[names[name]] = self._provider_updates(names[name], api_key=value)
        for provider, fields in providers.items():
            if not isinstance(fields, dict) or set(fields) - {
                "api_key",
                "upstream",
                "prices",
                "enabled",
                "delete_api_key",
            }:
                raise ValueError("Only reviewed provider configuration fields are accepted")
            fields = copy.deepcopy(fields)
            delete = fields.pop("delete_api_key", False)
            if not isinstance(delete, bool) or (delete and fields.get("api_key")):
                raise ValueError("Credential save and delete are separate explicit actions")
            if "api_key" in fields and fields["api_key"] is None:
                raise ValueError("Use the explicit delete action to clear a credential")
            if delete:
                fields["api_key"] = None
            if provider in updates and "api_key" in fields and fields["api_key"] != "":
                raise ValueError("Supply each provider credential only once")
            changes = self._provider_updates(provider, **fields)
            if changes:
                updates.setdefault(provider, {}).update(changes)
        with self._lock:
            self._expected(payload.get("revision"))
            if defaults or updates:
                state = copy.deepcopy(self._state)
                state["settings"].update(defaults)
                for provider, changes in updates.items():
                    self._apply_provider(state, provider, changes)
                self._commit(state)
            return self.status()

    def delete_api_key(self, provider, *, expected_revision=None):
        return self.update_provider(provider, api_key=None, expected_revision=expected_revision)

    def resolve(self, name, default=None):
        """Internal fixed-binding resolver. Never expose this method over HTTP."""
        if name not in _BINDINGS:
            raise ValueError("Unsupported instance configuration binding")
        provider, kind = _BINDINGS[name]
        spec = PROVIDERS[provider]
        with self._lock:
            fields = self._state["providers"].get(provider, {})
            if kind == "transcribe_key":
                return self._environment.get(name) or self.resolve("WORLD_OPENAI_API_KEY", default)
            if kind == "key":
                if not fields.get("enabled", True) or fields.get("key_deleted"):
                    return None
                return (
                    fields.get("api_key")
                    or self._environment.get(name)
                    or self._environment.get(spec["key_alias"])
                    or default
                )
            if kind == "upstream":
                return (
                    fields.get("upstream")
                    or self._environment.get(name)
                    or spec["upstream"]
                    or default
                )
            return (
                fields.get("prices", {}).get(spec["unit"]) or self._environment.get(name) or default
            )

    def secret_values(self):
        """Internal scanner adapter; never return this through an operator/agent API."""
        with self._lock:
            values = [
                fields["api_key"]
                for fields in self._state["providers"].values()
                if fields.get("api_key")
            ]
            values.extend(
                value
                for spec in PROVIDERS.values()
                for name in (spec["key_binding"], spec["key_alias"])
                if (value := self._environment.get(name))
            )
            if value := self._environment.get("WORLD_TRANSCRIBE_OPENAI_KEY"):
                values.append(value)
            return tuple(sorted({value for value in values if isinstance(value, str)}))

    def model_prices(self, default=None):
        """Validated table for NEW run pinning; existing runs retain their table."""
        if default is None:
            default = json.loads((Path(__file__).parents[1] / "policy/prices.json").read_text())
        result = copy.deepcopy(default)
        models = result.get("models", result)
        with self._lock:
            for provider, fields in self._state["providers"].items():
                if PROVIDERS[provider]["kind"] == "model":
                    for model, row in fields.get("prices", {}).items():
                        if model in models and models[model].get("provider") != provider:
                            raise ValueError("A priced model identifier cannot change provider")
                        # Field by field: an override never silently drops a declared tier.
                        models[model] = {**models.get(model, {}), **copy.deepcopy(row)}
        return result

    def status(self):
        """Read-only, secret-free snapshot; configured never implies validated."""
        with self._lock:
            credentials, providers = {}, []
            for provider, spec in PROVIDERS.items():
                fields = self._state["providers"].get(provider, {})
                configured = bool(self.resolve(spec["key_binding"]))
                source = (
                    "disabled"
                    if not fields.get("enabled", True) or fields.get("key_deleted")
                    else "saved"
                    if fields.get("api_key")
                    else "environment"
                    if configured
                    else "missing"
                )
                check = copy.deepcopy(fields.get("validation", {"status": "not_checked"}))
                credentials[provider] = {
                    "configured": configured,
                    "source": source,
                    "enabled": fields.get("enabled", True),
                    "validation": check,
                }
                upstream = (
                    self.resolve(spec["upstream_binding"])
                    if spec["upstream_binding"]
                    else spec["upstream"]
                )
                # Existing host environment may contain custom URLs. Do not echo
                # credential-bearing/query-bearing values into the operator UI.
                try:
                    upstream = _upstream(provider, upstream) if upstream else None
                except ValueError:
                    upstream = None
                providers.append(
                    {
                        "id": provider,
                        "kind": spec["kind"],
                        **credentials[provider],
                        "upstream": upstream,
                        "prices": copy.deepcopy(fields.get("prices", {})),
                        "prices_reviewed_at": fields.get("reviewed_at"),
                        "validation_supported": bool(spec["validation"]),
                    }
                )
            transcription = bool(self.resolve("WORLD_TRANSCRIBE_OPENAI_KEY"))
            credentials["transcribe_openai"] = {
                "configured": transcription,
                "source": "environment_dedicated"
                if self._environment.get("WORLD_TRANSCRIBE_OPENAI_KEY")
                else "shared_openai"
                if transcription
                else "missing",
                "enabled": True,
                "validation": {"status": "not_checked"},
            }
            result = {
                "settings": {**DEFAULT_SETTINGS, **copy.deepcopy(self._state["settings"])},
                "credentials": credentials,
                "providers": providers,
                "revision": self._state["revision"],
                "setup_complete": any(
                    credentials[name]["configured"] for name in ("openai", "anthropic")
                ),
                "subscription_auth": "native_cli_profile_only",
            }
            secrets_ = [
                fields["api_key"]
                for fields in self._state["providers"].values()
                if fields.get("api_key")
            ]
            secrets_.extend(
                value
                for spec in PROVIDERS.values()
                for binding in (spec["key_binding"], spec["key_alias"])
                if isinstance(value := self._environment.get(binding), str) and value
            )
            if self._environment.get("WORLD_TRANSCRIBE_OPENAI_KEY"):
                secrets_.append(self._environment["WORLD_TRANSCRIBE_OPENAI_KEY"])

            def redact(value):
                if isinstance(value, str):
                    for secret in secrets_:
                        value = value.replace(secret, "[redacted]")
                    return value
                if isinstance(value, dict):
                    return {redact(key): redact(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [redact(item) for item in value]
                return value

            return redact(result)

    async def validate_provider(self, provider, *, client=None):
        """Explicit, bounded, non-generating metadata GET; no search/payment probe."""
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise ValueError("Unknown reviewed provider")
        spec = PROVIDERS[provider]
        with self._lock:
            secret = self.resolve(spec["key_binding"])
            revision = self._state["revision"]
            if not secret:
                return {"provider": provider, "status": "not_configured", "revision": revision}
            if not spec["validation"]:
                return {"provider": provider, "status": "unsupported", "revision": revision}
            upstream = (
                self.resolve(spec["upstream_binding"])
                if spec["upstream_binding"]
                else spec["upstream"]
            )
            if upstream != spec["upstream"]:
                return {
                    "provider": provider,
                    "status": "unsupported_upstream",
                    "revision": revision,
                }
            if provider in self._validation_running or len(self._validation_running) >= 2:
                return {"provider": provider, "status": "in_progress", "revision": revision}
            instant = time.monotonic()
            if instant - self._last_validation.get(provider, -10) < 5:
                return {"provider": provider, "status": "cooldown", "revision": revision}
            self._last_validation[provider] = instant
            self._validation_running.add(provider)
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if provider == "anthropic":
            headers.update({"x-api-key": secret, "anthropic-version": "2023-06-01"})
        else:
            headers["Authorization"] = "Bearer " + secret
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=5, follow_redirects=False)
        result = {"provider": provider, "checked_at": _now(), "status": "unavailable"}
        served = None
        try:
            async with (
                asyncio.timeout(5),
                client.stream(
                    "GET",
                    # Anthropic pages its catalogue (20 by default); one page holds it all.
                    spec["upstream"]
                    + spec["validation"]
                    + ("?limit=1000" if provider == "anthropic" else ""),
                    headers=headers,
                    timeout=5,
                    follow_redirects=False,
                ) as response,
            ):
                code = response.status_code
                result["http_status"] = code
                if code == 200:
                    encoded = response.headers.get("content-encoding", "identity").lower()
                    raw = bytearray()
                    if encoded in {"", "identity"}:
                        async for block in response.aiter_bytes(chunk_size=4096):
                            raw.extend(block)
                            if len(raw) > 131072:
                                break
                    if encoded not in {"", "identity"}:
                        result["status"] = "unexpected_response"
                    elif len(raw) <= 131072:
                        try:
                            payload = json.loads(raw)
                        except (ValueError, UnicodeError, RecursionError):
                            payload = None
                        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                            result["status"] = "valid"
                            served = sorted(
                                {
                                    item["id"]
                                    for item in payload["data"]
                                    if isinstance(item, dict)
                                    and isinstance(item.get("id"), str)
                                    and _MODEL.fullmatch(item["id"])
                                }
                            )
                        else:
                            result["status"] = "unexpected_response"
                    else:
                        # A model catalogue may legitimately exceed this bound.
                        # Status alone never substitutes for a parsed auth check.
                        result["status"] = "response_limit"
                elif code in {401, 403}:
                    result["status"] = "rejected"
                elif code == 429:
                    result["status"] = "rate_limited"
                elif 300 <= code < 400:
                    result["status"] = "redirect_refused"
        except (httpx.HTTPError, TimeoutError):
            pass  # Never return exception text, URL, request headers or provider body.
        finally:
            with self._lock:
                self._validation_running.discard(provider)
            if owns_client:
                await client.aclose()
        with self._lock:
            current = self.resolve(spec["key_binding"]) or ""
            if revision != self._state["revision"] or not hmac.compare_digest(current, secret):
                return {
                    "provider": provider,
                    "status": "stale",
                    "revision": self._state["revision"],
                }
            state = copy.deepcopy(self._state)
            state["providers"].setdefault(provider, {})["validation"] = result
            self._commit(state)
            if served is not None and spec["kind"] == "model":
                self._save_served(provider, served, result["checked_at"])
            return {**result, "revision": self._state["revision"]}

    def _save_served(self, provider, models, checked_at):
        self.model_lists.mkdir(mode=0o700, exist_ok=True)
        target = self.model_lists / f"{provider}.json"
        staging = target.with_suffix(".tmp")
        staging.write_text(canonical_json({"checked_at": checked_at, "models": models}))
        staging.replace(target)

    def served_models(self, provider):
        """The model IDs the provider's key served at its last validation, or None."""
        if provider not in PROVIDERS:
            return None
        try:
            value = json.loads((self.model_lists / f"{provider}.json").read_text())
        except (OSError, ValueError):
            return None
        models = value.get("models") if isinstance(value, dict) else None
        if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
            return None
        return models

    def close(self):
        with self._lock:
            if self._descriptor is not None:
                os.close(self._descriptor)
                self._descriptor = None

    async def aclose(self):
        self.close()
