"""Explicit official Claude login-startup check; never completes authorization.

Not collected by pytest. Invoke with the actual supported native CLI path. Only
URL shapes and state metadata leave the fresh, automatically removed profile.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from worldd.harness_auth import HarnessAuthService


def url_shape(value):
    if not value:
        return None
    parsed = urlsplit(value)
    query = parse_qs(parsed.query, keep_blank_values=True)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "path": parsed.path,
        "query_keys": sorted(query),
        "redirects": [
            {"scheme": target.scheme, "host": target.hostname, "path": target.path}
            for target in map(urlsplit, query.get("redirect_uri", []))
        ],
    }


async def check(options):
    result = {
        "fixture_only": False,
        "account_authorized": False,
        "credentials_supplied": False,
        "scientific_gate_evidence": False,
        "harness": "claude-code",
        "passed": False,
    }
    with tempfile.TemporaryDirectory(prefix="landingparty-native-auth-") as directory:
        service = HarnessAuthService(
            directory,
            binaries={"claude-code": str(options.cli.resolve())},
            login_timeout_s=options.timeout,
        )
        try:
            await service.start("claude-code")
            deadline = time.monotonic() + options.timeout
            while time.monotonic() < deadline:
                status = await service.status("claude-code")
                result.update(
                    state=status["state"],
                    requires_code=bool(status.get("requires_code")),
                    authorization=url_shape(status.get("auth_url")),
                )
                if status["state"] != "connecting" or (
                    status.get("auth_url") and status.get("requires_code")
                ):
                    break
                await asyncio.sleep(0.2)
            result["passed"] = (
                result.get("state") == "connecting"
                and result.get("requires_code", False)
                and bool(result.get("authorization"))
            )
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            # Exception messages and native output may contain OAuth parameters.
            result["error_type"] = type(error).__name__
        finally:
            try:
                canceled = await service.cancel("claude-code")
                result["final_state"] = canceled["state"]
            finally:
                await service.aclose()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    if not 1 <= options.timeout <= 120:
        parser.error("timeout must be between 1 and 120 seconds")
    if (
        not options.cli.is_absolute()
        or not options.cli.is_file()
        or not os.access(options.cli, os.X_OK)
    ):
        parser.error("cli must be an absolute executable path")
    result = asyncio.run(check(options))
    serialized = json.dumps(result, indent=2) + "\n"
    if options.output:
        options.output.write_text(serialized)
        options.output.chmod(0o600)
    print(serialized, end="")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
