"""Keep researcher credentials out of native harness process environments."""

import re

WORLD_AGENT_VARIABLES = frozenset(
    {
        "WORLD_RUN_ID",
        "WORLD_RUN_TOKEN",
        "WORLD_LANE_TOKEN",
        "WORLD_BASE_URL",
        "WORLD_AGENT_URL",
        "WORLD_LANE_URL",
    }
)
# Capabilities a native harness is meant to hold; every other token-like name is dropped.
CAPABILITY_VARIABLES = frozenset({"WORLD_RUN_TOKEN", "WORLD_LANE_TOKEN"})


def agent_environment(source):
    environment = {}
    for name, value in source.items():
        if name.startswith("WORLD_") and name not in WORLD_AGENT_VARIABLES:
            continue
        if name not in CAPABILITY_VARIABLES and re.search(
            r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)(?:_|$)", name
        ):
            continue
        if name in {"ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_AUTH_TOKEN"}:
            continue
        environment[name] = value
    return environment
