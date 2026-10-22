"""OpenHands configuration pointing its LLM and tools at the same world core."""

import re


def openhands_binding(
    run_id,
    *,
    model,
    world_url="http://127.0.0.1:7778",
    lane_url="http://127.0.0.1:8081/openai/v1",
    gateway_url=None,
    ca_path=None,
    python="python",
    bridge_url="http://127.0.0.1:18082/v1",
):
    if not re.fullmatch(r"r_[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid run identifier")
    if bool(gateway_url) != bool(ca_path):
        raise ValueError("Proxy URL and world CA path must be supplied together")
    env = {
        "WORLD_RUN_ID": run_id,
        "WORLD_BASE_URL": world_url,
        "OPENAI_BASE_URL": bridge_url,
        "LLM_BASE_URL": bridge_url,
        "LLM_MODEL": model,
    }
    if gateway_url:
        env.update(HTTP_PROXY=gateway_url, HTTPS_PROXY=gateway_url, SSL_CERT_FILE=ca_path)
    return {
        "llm": {"model": model, "base_url": bridge_url, "drop_params": False},
        "mcp": {
            "stdio_servers": [
                {
                    "name": "world",
                    "command": python,
                    "args": ["-m", "worldd.mcp"],
                    "env": {"WORLD_RUN_ID": run_id, "WORLD_BASE_URL": world_url},
                }
            ]
        },
        "environment": env,
        "header_proxy": {
            "command": python,
            "args": ["-m", "adapters.openhands.proxy", "--lane-url", lane_url],
            "credential_source": "WORLD_RUN_TOKEN environment at runtime only",
        },
        "isolation": "cooperative; shell bypass requires the external fenced deployment for enforcement",
    }


def runtime_environment(binding, source):
    environment = {**source, **binding["environment"]}
    token = source.get("WORLD_RUN_TOKEN")
    if not token:
        raise ValueError("The OpenHands frontend requires its run-scoped WORLD_RUN_TOKEN")
    environment["LLM_API_KEY"] = token
    environment["OPENAI_API_KEY"] = token
    for name in (
        "WORLD_CONTROL_TOKEN",
        "WORLD_OPENAI_API_KEY",
        "WORLD_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        environment.pop(name, None)
    return environment
