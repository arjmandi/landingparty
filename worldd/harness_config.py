"""Code-owned native harness definitions and credential-free launch configuration."""

from __future__ import annotations

import importlib
import json
import os
import platform
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from adapters.environment import agent_environment


@dataclass(frozen=True)
class HarnessManifest:
    id: str
    name: str
    executable: str
    package: str | None = None
    pinned_version: str | None = None
    env_binding: str | None = None
    version_args: tuple[str, ...] = ("--version",)
    launcher: Callable | None = None
    auth_modes: tuple[str, ...] = ("api_key", "subscription")


BUILTINS = (
    HarnessManifest(
        "claude-code",
        "Claude Code",
        "claude",
        "@anthropic-ai/claude-code",
        "2.1.308",
        "WORLD_CLAUDE_CLI",
    ),
    HarnessManifest("codex", "Codex", "codex", "@openai/codex", "0.163.0", "WORLD_CODEX_CLI"),
)


def identifier(value, name="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError(f"Invalid {name}")
    return value


def model_name(value):
    if value is not None and (
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value)
    ):
        raise ValueError("Invalid model name")
    return value


def binary_path(manifest, tools, environment):
    configured = environment.get(manifest.env_binding) if manifest.env_binding else None
    if configured:
        path = Path(configured).expanduser()
        return (
            str(path.resolve())
            if path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
            else None
        )
    native = _claude_native_binary(manifest, tools)
    if native:
        return native
    private = tools / manifest.id / "node_modules" / ".bin" / manifest.executable
    if private.is_file() and os.access(private, os.X_OK):
        return str(private)
    return shutil.which(manifest.executable, path=environment.get("PATH"))


def _claude_native_binary(manifest, tools):
    """Use the pinned optional package without executing npm lifecycle scripts."""
    if manifest.id != "claude-code" or manifest.package != "@anthropic-ai/claude-code":
        return None
    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    arch = {"x86_64": "x64", "AMD64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine()
    )
    if not system or not arch:
        return None
    suffix = "-musl" if system == "linux" and platform.libc_ver()[0] == "musl" else ""
    if system == "linux" and not suffix:
        loader_arch = "x86_64" if arch == "x64" else "aarch64"
        if Path(f"/lib/ld-musl-{loader_arch}.so.1").exists():
            suffix = "-musl"
    name = f"@anthropic-ai/claude-code-{system}-{arch}{suffix}"
    prefix = (tools / manifest.id).resolve()
    package = prefix / "node_modules" / name
    wrapper = prefix / "node_modules" / manifest.package / "package.json"
    binary = package / "claude"
    try:
        for path in (wrapper, package / "package.json", binary):
            path.resolve().relative_to(prefix)
        parent = json.loads(wrapper.read_text())
        metadata = json.loads((package / "package.json").read_text())
        if (
            parent.get("name") == manifest.package
            and parent.get("version") == manifest.pinned_version
            and parent.get("optionalDependencies", {}).get(name) == manifest.pinned_version
            and metadata.get("name") == name
            and metadata.get("version") == manifest.pinned_version
            and binary.is_file()
            and os.access(binary, os.X_OK)
        ):
            return str(binary.resolve())
    except (OSError, ValueError, TypeError):
        pass
    return None


def base_environment(source, home):
    filtered = agent_environment(source)
    allowed = {"PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR"}
    environment = {key: value for key, value in filtered.items() if key in allowed}
    environment.setdefault("PATH", os.defpath)
    environment.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"),
        XDG_CACHE_HOME=str(home / ".cache"),
        TERM="dumb",
        NO_COLOR="1",
        DO_NOT_TRACK="1",
        DISABLE_TELEMETRY="1",
        DISABLE_ERROR_REPORTING="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )
    return environment


def _write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def layout_context(work):
    layout = {
        "profile": "cooperative",
        "workspace": str(work),
        "away_kit": str(work.parent / "world"),
        "workspace_handle": "/work",
        "world_handle": "/world",
    }
    text = (
        "Native harness layout: your local workspace is the process's current working directory, "
        + layout["workspace"]
        + ". The World away kit is at "
        + layout["away_kit"]
        + ". The virtual /work and /world paths in the mission briefing name those locations. "
        + "Use World MCP tools for handle-based reads such as /world/fetched/ resources. This run uses the cooperative profile."
    )
    return layout, text


def launch_configuration(
    manifest,
    binary,
    *,
    root,
    work,
    run_info,
    model,
    auth_mode,
    profile,
    agent_url,
    lane_url,
    environment,
    stream_usage=False,
):
    """Return argv/env/stdin; only the constructor's trusted registry can add launchers.

    ``stream_usage`` asks the client for each message's final usage as it streams,
    which a transcript-metered session needs to price every message exactly.
    """
    home = root / "home"
    home.mkdir(mode=0o700)
    env = base_environment(environment, home)
    env.update(
        WORLD_RUN_ID=run_info["run_id"],
        WORLD_RUN_TOKEN=run_info["run_token"],
        WORLD_BASE_URL=agent_url,
    )
    proxy = run_info.get("proxy", {})
    if proxy.get("server") and proxy.get("ca_path"):
        address = urlsplit(proxy["server"])
        auth = ""
        if proxy.get("username") and proxy.get("password"):
            auth = quote(proxy["username"], safe="") + ":" + quote(proxy["password"], safe="") + "@"
        gateway = urlunsplit((address.scheme, auth + address.netloc, address.path, "", ""))
        env.update(
            HTTP_PROXY=gateway,
            HTTPS_PROXY=gateway,
            SSL_CERT_FILE=proxy["ca_path"],
            REQUESTS_CA_BUNDLE=proxy["ca_path"],
            NODE_EXTRA_CA_CERTS=proxy["ca_path"],
        )
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    if auth_mode == "subscription":
        env["NO_PROXY"] += ",api.anthropic.com,api.openai.com,chatgpt.com,auth.openai.com"
        # Official native provider TLS uses the system trust store, not a world-only CA.
        env.pop("SSL_CERT_FILE", None)
        env.pop("REQUESTS_CA_BUNDLE", None)
    prompt = run_info["preamble"]
    if run_info.get("goal"):
        prompt += "\n\n" + run_info["goal"]
    prompt += "\n\n" + layout_context(work)[1]
    if manifest.launcher:
        return manifest.launcher(
            root=root,
            work=work,
            run_info=run_info,
            model=model,
            auth_mode=auth_mode,
            profile=profile,
            environment=env,
            binary=binary,
            agent_url=agent_url,
            lane_url=lane_url,
        )
    if manifest.id == "claude-code":
        configure = importlib.import_module("adapters.claude-code.configure")
        runner = importlib.import_module("adapters.claude-code.run")
        binding = configure.binding(
            run_info["run_id"],
            world_url=agent_url,
            lane_url=lane_url.rstrip("/") + "/anthropic",
            python=sys.executable,
        )
        if auth_mode == "subscription":
            binding["world.settings.json"]["env"] = {
                key: value
                for key, value in binding["world.settings.json"]["env"].items()
                if key.startswith("WORLD_")
            }
            env["CLAUDE_CONFIG_DIR"] = str(profile)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        configure.write_binding(root, binding)
        argv = runner.command(root, "")
        argv[0] = binary
        # Read the prompt from stdin; never expose the mission in a process listing.
        del argv[2]
        if auth_mode == "api_key":
            env = runner.runtime_environment(root, env)
        argv += ["--setting-sources", "", "--no-session-persistence"]
        if stream_usage:
            argv.append("--include-partial-messages")
        if model:
            argv += ["--model", model]
        return argv, env, prompt.encode()
    if manifest.id != "codex":
        raise ValueError("Developer harness requires a code-owned launcher")
    config = {
        "web_search": "disabled",
        "check_for_update_on_startup": False,
        "features.api_key_model_discovery": False,
        "features.apps": False,
        "features.plugins": False,
        "features.remote_plugin": False,
        "features.skill_search": False,
        "features.skill_mcp_dependency_install": False,
        "features.browser_use": False,
        "features.browser_use_external": False,
        "features.in_app_browser": False,
        "features.computer_use": False,
        "analytics.enabled": False,
        "feedback.enabled": False,
        "otel.exporter": "none",
        "otel.trace_exporter": "none",
        "otel.log_user_prompt": False,
        "mcp_servers.world.command": sys.executable,
        "mcp_servers.world.args": ["-m", "worldd.mcp"],
        "mcp_servers.world.env": {"WORLD_RUN_ID": run_info["run_id"], "WORLD_BASE_URL": agent_url},
        "mcp_servers.world.env_vars": ["WORLD_RUN_TOKEN"],
        "shell_environment_policy.inherit": "none",
    }
    if auth_mode == "api_key":
        codex_home = home / ".codex"
        codex_home.mkdir(mode=0o700)
        env["CODEX_HOME"] = str(codex_home)
        config.update(
            {
                "model_provider": "landingparty",
                "model_providers.landingparty.name": "LandingParty",
                "model_providers.landingparty.base_url": lane_url.rstrip("/") + "/openai/v1",
                "model_providers.landingparty.env_key": "WORLD_RUN_TOKEN",
                "model_providers.landingparty.wire_api": "responses",
                "model_providers.landingparty.env_http_headers": {
                    "X-World-Run-Token": "WORLD_RUN_TOKEN",
                    "X-World-Run-Id": "WORLD_RUN_ID",
                },
            }
        )
    else:
        env["CODEX_HOME"] = str(profile)

    # JSON scalar/array syntax is also TOML syntax for these fixed values.
    def toml(value):
        if isinstance(value, dict):
            return (
                "{ "
                + ", ".join(json.dumps(key) + " = " + toml(item) for key, item in value.items())
                + " }"
            )
        return json.dumps(value)

    argv = [
        binary,
        "--no-daemon",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(work),
    ]
    for key, value in config.items():
        argv += ["-c", key + "=" + toml(value)]
    if model:
        argv += ["--model", model]
    argv.append("-")
    return argv, env, prompt.encode()
