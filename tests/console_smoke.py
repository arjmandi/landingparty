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
    standing = [
        {
            "rule_id": "sr_fixture",
            "kind": "approval",
            "platform": "fixture",
            "scope": "post",
            "reason": "not permitted",
        }
    ]
    contamination, incidents, console_cursors = [], [], []
    incoming = [
        {
            "report_id": "ir_public",
            "identity_id": "id_fixture",
            "owner": "Fixture operator",
            "report": "<script>window.publicReportXss=true</script>",
            "contact": "optional contact",
            "body_hash": "sha256:fixture",
        }
    ]
    triages = []
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
            elif path == "/v1/console/state":
                from urllib.parse import parse_qs

                console_cursors.append(int(parse_qs(parsed.query)["since"][0]))
                result = {
                    "cursor": 9,
                    "activity": [{"kind": "browser_action", "tool": "goto"}],
                    "human": {"seconds": 0},
                    "standing_rejections": standing,
                    "incidents": incidents,
                    "contamination": contamination,
                }
            elif path == "/v1/asks/standing/sr_fixture/revoke":
                standing.clear()
                result = {"revoked": True}
            elif path == "/v1/contamination":
                assert body["owner"] == "Fixture operator" and body["human_seconds"] == 6
                contamination.append(body)
                result = body
            elif path == "/v1/incidents":
                assert body["owner"] == "Fixture operator" and body["platform"] == "fixture"
                incidents.append(body)
                result = {"kind": "result", "revocation": "confirmed"}
            elif path == "/v1/public-incident-reports":
                result = {"reports": incoming}
            elif path == "/v1/public-incident-reports/ir_public/triage":
                assert (
                    body["run_id"] == "r_fixture"
                    and body["platform"] == "fixture"
                    and body["decision"] == "accept"
                )
                triages.append(body)
                incoming.clear()
                result = {"state": "accepted", "incident_ref": "fp_fixture"}
            elif path == "/v1/mandates":
                expected = next(item["body"] for item in tickets if item["kind"] == "grant_payment")
                assert body == {"run_id": "r_fixture", **expected}
                result = {"mandate_id": "mandate_fixture"}
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
        await page.get_by_role("button", name="Activity, standing orders and incidents").click()
        await page.get_by_text("Standing rejections", exact=True).wait_for()
        await page.get_by_role("button", name="Revoke standing rejection", exact=True).click()
        checks["standing_rule_revocation"] = not standing
        await page.get_by_label("Triage platform", exact=True).fill("fixture")
        await page.get_by_role(
            "button", name="Accept report and revoke affected authority", exact=True
        ).click()
        checks["public_report_review_before_revocation"] = (
            len(triages) == 1
            and not incoming
            and await page.evaluate("window.publicReportXss !== true")
        )
        await page.get_by_label("Incident platform", exact=True).fill("fixture")
        await page.get_by_label("Human owner", exact=True).fill("Fixture operator")
        await page.get_by_label("Incident detail", exact=True).fill("A fixture complaint")
        await page.get_by_role(
            "button", name="Record incident and revoke affected authority"
        ).click()
        await page.get_by_label("Ad hoc help or pasted material", exact=True).fill(
            "Unscheduled fixture hint"
        )
        await page.get_by_label("Actual additional human seconds (maximum 600)", exact=True).fill(
            "6"
        )
        await page.get_by_role("button", name="Record contamination", exact=True).click()
        await page.get_by_text("Contamination recorded; run marked tainted.", exact=True).wait_for()
        checks["incident_owner_and_contamination"] = len(incidents) == len(contamination) == 1
        await page.get_by_role("button", name="Activity, standing orders and incidents").click()
        await page.locator("#runs pre").filter(has_text="Unscheduled fixture hint").wait_for()
        checks["activity_since_visit_cursor"] = console_cursors == [0, 9]
        tickets.append(
            {
                "ticket_id": "t_payment",
                "run_id": "r_fixture",
                "kind": "grant_payment",
                "state": "open",
                "agent_text": "Read fixture article",
                "provenance": {},
                "body": {
                    "merchant": "https://merchant.fixture.test",
                    "ceiling": "1.25",
                    "purpose": "Read fixture article",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "action": {
                        "url": "https://merchant.fixture.test/article",
                        "quote_hash": "sha256:observed-fixture",
                    },
                },
            }
        )
        await page.get_by_label("Researcher control token").fill(token)
        await page.get_by_role("button", name="Connect / refresh").click()
        await page.get_by_text("grant_payment · open · t_payment", exact=True).click()
        await (
            page.locator("details")
            .filter(has_text="t_payment")
            .get_by_role("button", name="Approve exact action")
            .click()
        )
        await page.get_by_text("No open tickets.", exact=True).wait_for()
        checks["payment_approval_issues_only_exact_canonical_mandate"] = any(
            path == "/v1/mandates" for path, _ in calls
        )
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
