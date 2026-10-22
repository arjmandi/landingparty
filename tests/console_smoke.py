"""Offline browser exercise for the researcher bridge (run in the world image).

Uses Playwright's network interceptor as a local control-plane fixture. No
provider, account, or payment request leaves the container.
"""

import asyncio
import base64
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from worldd.browser import PlaywrightBackend


async def main():
    html_path = Path(os.getenv("WORLD_CONSOLE_HTML", "/opt/landingparty/worldd/console.html"))
    token = "fixture-control-capability"
    tickets = [
        {
            "ticket_id": "t_approval",
            "run_id": "r_fixture",
            "kind": "approval",
            "state": "open",
            "body": {"body": "<script>window.xss=true</script>"},
            "agent_text": "Fixture approval",
            "provenance": {},
        },
        {
            "ticket_id": "t_deposit",
            "run_id": "r_fixture",
            "kind": "grant_credential",
            "state": "open",
            "body": {"alias": "fixture"},
            "agent_text": "Fixture credential",
            "provenance": {},
        },
    ]
    calls, errors, active = [], [], set()
    backend = PlaywrightBackend(state_dir=Path("/tmp/console-browser"))
    checks = {}
    try:
        handle = await backend.open({})
        page = handle["page"]
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def control(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.hostname != "bridge.fixture.test":
                await route.abort()
                raise AssertionError("Unexpected network destination")
            path = parsed.path
            if path == "/":
                await route.fulfill(body=html_path.read_text(), content_type="text/html")
                return
            assert request.headers.get("x-world-control-token") == token
            body = request.post_data_json if request.method == "POST" else None
            calls.append((path, body))
            result = {}
            if path == "/v1/bridge":
                result = {
                    "runs": [
                        {
                            "run_id": "r_fixture",
                            "state": "running",
                            "reply_policy": "blockers_only",
                            "messages": [{"agent_text": '<img src=x onerror="window.xss=true">'}],
                        }
                    ]
                }
            elif path == "/v1/asks":
                result = {"tickets": tickets}
            elif path == "/v1/sessions":
                result = {"sessions": []}
            elif path.endswith("/touch"):
                ticket_id = path.split("/")[-2]
                if body["active"]:
                    active.add(ticket_id)
                else:
                    active.discard(ticket_id)
                assert len(active) <= 1, "The console charged two tickets simultaneously"
            elif path.endswith("/answer"):
                ticket = next(t for t in tickets if t["ticket_id"] == path.split("/")[-2])
                assert body["decision"] in {"approve", "reject", "deposit"}
                ticket["state"] = "accepted" if body["decision"] == "deposit" else "approved"
                result = ticket
            elif path == "/v1/vault/accounts/fixture" and request.method == "GET":
                assert parsed.query == "run_id=r_fixture"
                result = {"alias": "fixture", "state": "ready"}
            elif path == "/v1/vault/accounts/fixture/credential":
                assert body == {"credential": {"api_key": "private-fixture-secret"}}
                result = {"alias": "fixture", "state": "ready"}
            elif path.endswith("/deposit-complete"):
                assert body == {"accepted": True}
                ticket = next(t for t in tickets if t["ticket_id"] == path.split("/")[-2])
                ticket["state"] = "deposited"
                result = ticket
            else:
                raise AssertionError("Unexpected control route: " + path)
            await route.fulfill(json=result)

        await page.route("**/*", control)
        await page.goto("http://bridge.fixture.test/")
        await page.get_by_label("Researcher control token").fill(token)
        await page.get_by_role("button", name="Connect / refresh").click()
        await page.get_by_text("approval · open · t_approval", exact=True).wait_for()
        checks["csp_script_executes"] = True
        checks["control_token_cleared"] = await page.locator("#credential").input_value() == ""
        checks["untrusted_markup_is_text"] = (
            await page.locator("img").count() == 0
            and not await page.evaluate("Boolean(window.xss)")
            and "<img src=x" in await page.locator("#runs").inner_text()
        )
        await page.get_by_text("approval · open · t_approval", exact=True).click()
        await page.get_by_text("grant_credential · open · t_deposit", exact=True).click()
        deposit = page.locator("details").filter(has_text="t_deposit")
        await deposit.get_by_role("button", name="Accept secure deposit").click()
        await page.get_by_text("grant_credential · accepted · t_deposit", exact=True).click()
        deposit = page.locator("details").filter(has_text="t_deposit")
        checks["bound_account_alias_readonly"] = await deposit.get_by_label(
            "Account alias"
        ).evaluate("element => element.readOnly && element.value === 'fixture'")
        secret = deposit.get_by_label("Secure credential JSON", exact=True)
        await secret.fill('{"api_key":"DO_NOT_ECHO_INVALID')
        await deposit.get_by_role("button", name="Complete secure deposit").click()
        await page.wait_for_function("document.querySelector('#error').textContent.length > 0")
        checks["invalid_secret_not_echoed"] = (
            await page.locator("#error").inner_text() == "Enter a valid JSON object."
            and await secret.input_value() == ""
        )
        await secret.fill(json.dumps({"api_key": "private-fixture-secret"}))
        await deposit.get_by_role("button", name="Complete secure deposit").click()
        await page.get_by_text("grant_credential · accepted · t_deposit", exact=True).wait_for(
            state="detached"
        )
        paths = [path for path, _ in calls]
        checks["deposit_state_then_secure_write_then_complete"] = (
            paths.index("/v1/asks/t_deposit/answer")
            < paths.index("/v1/vault/accounts/fixture/credential")
            < paths.index("/v1/asks/t_deposit/deposit-complete")
        )
        await page.get_by_text("approval · open · t_approval", exact=True).click()
        await (
            page.locator("details")
            .filter(has_text="t_approval")
            .get_by_role("button", name="Approve exact action")
            .click()
        )
        await page.get_by_text("No open tickets.", exact=True).wait_for()
        answer = next(body for path, body in calls if path == "/v1/asks/t_approval/answer")
        checks["approval_matches_api_enum"] = answer["decision"] == "approve"
        checks["one_active_ticket"] = len(active) == 0
        visible = await page.locator("body").inner_text()
        checks["no_secret_or_token_in_visible_output"] = all(
            value not in visible
            for value in (token, "private-fixture-secret", "DO_NOT_ECHO_INVALID")
        )
        checks["no_script_errors"] = not errors
        screenshot = await page.screenshot()
        checks["actual_screenshot"] = base64.b64encode(screenshot).decode().startswith("iVBOR")
        await backend.close(handle)
    finally:
        await backend.aclose()
    print(
        json.dumps(
            {
                "kind": "console_software_verification",
                "scientific_gate_evidence": False,
                "chromium_sandbox": True,
                "network": "none",
                "checks": checks,
                "passed": all(checks.values()),
            }
        )
    )
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
