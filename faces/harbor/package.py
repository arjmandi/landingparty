"""Build a Harbor task package while preserving the external fenced boundary."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def build_task(
    output: str | Path,
    *,
    instruction: str,
    run_id: str,
    world_url="http://world:7778",
    lane_url="http://world:8081/openai/v1",
    gateway_url="http://world:8080",
    image="landing-party-agent:0.1",
    fenced_compose="deploy/compose.fenced.yml",
    agent_capability=None,
):
    if not instruction or not re.fullmatch(r"r_[A-Za-z0-9_-]+", run_id):
        raise ValueError("A real instruction and run identifier are required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", image):
        raise ValueError("A literal container image reference is required")
    if any(
        "\n" in item or "\r" in item for item in (run_id, world_url, lane_url, gateway_url, image)
    ):
        raise ValueError("Task routing and image fields cannot contain line breaks")
    for url in (world_url, lane_url, gateway_url):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Task endpoints require credential-free HTTP(S) URLs")
    if agent_capability is not None:
        raise ValueError(
            "Do not bake runtime capabilities into a task image; inject them at launch"
        )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "environment").mkdir()
    (output / "tests").mkdir()
    (output / "instruction.md").write_text(instruction + "\n")
    (output / "task.toml").write_text(
        'version = "1.0"\n\n[metadata]\nname = "landing-party"\n\n[agent]\ntimeout_sec = 7200\n\n[verifier]\ntimeout_sec = 60\n\n[environment]\nbuild_timeout_sec = 600\ncpus = 1\nmemory_mb = 2048\nstorage_mb = 10240\n'
    )
    routing = {
        "WORLD_RUN_ID": run_id,
        "WORLD_BASE_URL": world_url,
        "OPENAI_BASE_URL": lane_url,
        "HTTP_PROXY": gateway_url,
        "HTTPS_PROXY": gateway_url,
        "SSL_CERT_FILE": "/world/ca.pem",
    }
    dockerfile = ["FROM " + image]
    dockerfile.extend("ENV " + key + "=" + json.dumps(value) for key, value in routing.items())
    dockerfile += ["WORKDIR /work", "USER agent"]
    (output / "environment" / "Dockerfile").write_text("\n".join(dockerfile) + "\n")
    # A no-goal life must not receive an invented numeric grade to satisfy a runner convention.
    (output / "tests" / "test.sh").write_text(
        '#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\npython - <<\'PY\'\nimport json\nfrom pathlib import Path\nPath("/logs/verifier/reward.json").write_text(json.dumps({"reward": None, "reason": "Open-ended live world; inspect the sealed mission report."}))\nPY\n'
    )
    (output / "tests" / "test.sh").chmod(0o755)
    manifest = {
        "run_id": run_id,
        "fenced_compose": fenced_compose,
        "image": image,
        "routing": routing,
        "reward": None,
        "enforcement": "Requires the external fenced network namespace and successful live preflight.",
        "runtime_capabilities": "Inject only the run-scoped agent capability; researcher token is forbidden.",
    }
    (output / "landing-party.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return output
