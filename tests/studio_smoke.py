"""Actual Chromium exercise of the operator app, using offline API fixtures only.

Run in the hardened world container with only this pilot and the three UI assets
mounted read-only. No provider credentials, model calls, or Internet are used.
"""

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from worldd.browser import PlaywrightBackend


async def main():
    assets = Path(os.getenv("WORLD_STUDIO_ASSETS", "/opt/landingparty/worldd"))
    output = Path(os.getenv("WORLD_STUDIO_OUTPUT", "/tmp/studio-output"))
    output.mkdir(parents=True, exist_ok=True)
    token, secret, code = "operator-fixture-token", "sk-private-fixture-key", "private-login-code"
    malicious = '<img src=x onerror="window.studioXss=true">'
    calls, errors, checks = [], [], {}
    authenticated, connected, installed = False, False, False
    fail_save = False
    settings = {
        "revision": 1,
        "setup_complete": False,
        "settings": {
            "instance_name": "Landing Party",
            "default_harness_id": "claude-code",
            "default_model": "claude-fixture-priced",
            "default_budget_usd": 20,
            "default_wall_minutes": 120,
        },
        "credentials": {"anthropic": {"configured": False}, "openai": {"configured": False}},
    }
    tickets = [
        {
            "ticket_id": "t_fixture",
            "run_id": "r_fixture",
            "kind": "approval",
            "state": "open",
            "agent_text": malicious,
            "platform": "fixture",
            "body": {
                "scope": "post",
                "request": {
                    "operation": "post",
                    "text": malicious,
                    "arguments": {"nested": "exact-review-fixture"},
                },
            },
            "action_hash": "sha256:fixture",
            "opened_at": "2026-10-23T10:00:00Z",
        }
    ]
    runs = []
    connection = {"state": "disconnected", "requires_code": True}
    connect_attempts, check_attempts = 0, 0
    started = []

    def model_rows(estimated):
        return [
            {
                "id": "claude-fixture-priced",
                "label": "Priced fixture",
                "group": "current",
                "input_usd": "3",
                "output_usd": "15",
                "priced": True,
                "served": None,
                "selectable": True,
                "efforts": ["low", "medium", "high"],
                "default_effort": "medium",
            },
            {
                "id": "claude-unpriced-fixture",
                "label": "claude-unpriced-fixture",
                "group": "earlier",
                "input_usd": None,
                "output_usd": None,
                "priced": False,
                "served": None,
                "selectable": False,
                "efforts": [],
                "default_effort": None,
            },
        ]

    def catalog():
        key_ready = installed and settings["credentials"]["anthropic"]["configured"]
        return {
            "harnesses": [
                {
                    "id": "claude-code",
                    "name": "Claude Code",
                    "description": "Native coding agent.",
                    "kind": "native",
                    "provider": "anthropic",
                    "pinned_version": "fixture-1",
                    "transcript": "claude-stream-json",
                    "efforts": ["low", "medium", "high", "xhigh", "max"],
                    "installed": installed,
                    "version": "fixture-1" if installed else None,
                    "install_status": {"state": "installed" if installed else "not_started"},
                    "requirements": {"node": True, "npm": True},
                    "connection": connection,
                    "connections": [
                        {
                            "id": "api_key",
                            "label": "Anthropic API key",
                            "configured": settings["credentials"]["anthropic"]["configured"],
                            "ready": key_ready,
                            "reason": None
                            if key_ready
                            else "Add your Anthropic API key on this harness.",
                            "billing": {"kind": "metered", "label": "Metered API"},
                            "models": model_rows(False),
                        },
                        {
                            "id": "subscription",
                            "label": "Subscription account",
                            "configured": connected,
                            "ready": installed and connected,
                            "reason": None if connected else "Connect your subscription account.",
                            "billing": {
                                "kind": "estimated",
                                "label": "Provider subscription · model dollars estimated",
                            },
                            "models": model_rows(True),
                        },
                    ],
                }
            ]
        }

    def detail():
        run = runs[0]
        return {
            "run": run,
            "status": {
                "state": run["state"],
                "in_flight": 1 if run["state"] == "running" else 0,
                "wall_left_s": 600,
                "pending_asks": sum(t["state"] == "open" for t in tickets),
            },
            "billing": run["billing"],
            "limits": {"budget_usd": 20},
            "costs": {"usd_total": None, "model_usd": None, "world_tools_usd": 0.025},
            "timeline": [
                {"kind": "beam_down", "title": "Run started", "at": "2026-10-23T10:00:00Z"},
                {"kind": "ask_open", "title": malicious, "detail": malicious},
            ],
        }

    backend = PlaywrightBackend(state_dir=Path("/tmp/studio-browser"))
    try:
        handle = await backend.open({})
        page = handle["page"]
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def route_api(route):
            nonlocal authenticated, installed, connected, connection, fail_save
            nonlocal connect_attempts, check_attempts
            request = route.request
            url = urlsplit(request.url)
            assert url.hostname == "studio.fixture.test", "Unexpected network destination"
            path = url.path
            if path == "/":
                await route.fulfill(
                    body=(assets / "studio.html").read_text(), content_type="text/html"
                )
                return
            if path.startswith("/assets/"):
                name = path.removeprefix("/assets/")
                assert name in {"studio.css", "studio.js"}
                await route.fulfill(
                    body=(assets / name).read_text(),
                    content_type="text/css" if name.endswith("css") else "application/javascript",
                )
                return
            body = request.post_data_json if request.method == "POST" else None
            assert not any(
                key in request.headers for key in ("x-world-control-token", "authorization")
            )
            calls.append((request.method, path, body))
            result = {}
            if path == "/api/session":
                if request.method == "POST":
                    assert body == {"token": token}
                    authenticated = True
                elif request.method == "DELETE":
                    authenticated = False
                result = {"authenticated": authenticated, "requires_token": True}
            elif not authenticated:
                await route.fulfill(status=401, json={"detail": "Operator session required"})
                return
            elif path == "/api/settings":
                if request.method == "POST":
                    assert body["revision"] == settings["revision"]
                    if body.get("credentials"):
                        assert body["credentials"] == {"anthropic_api_key": secret}
                    if fail_save:
                        fail_save = False
                        await route.fulfill(status=400, json={"detail": "Do not display " + secret})
                        return
                    settings["settings"].update(body.get("settings", {}))
                    if body.get("credentials"):
                        settings["credentials"]["anthropic"]["configured"] = True
                    settings["revision"] += 1
                    settings["setup_complete"] = True
                result = settings
            elif path == "/api/harnesses":
                result = catalog()
            elif path.endswith("/install"):
                assert body == {} and not installed
                installed = True
                result = {"state": "installed"}
            elif path.endswith("/connect"):
                assert installed
                connect_attempts += 1
                connection = {"state": "connecting", "requires_code": True}
                result = connection
            elif path.endswith("/cancel-connect"):
                connection = {"state": "disconnected", "requires_code": True}
                result = connection
            elif path.endswith("/connection/code"):
                assert body == {"code": code}
                assert connection.get("auth_url"), "A code was submitted before a provider link"
                connected = True
                connection = {"state": "connected"}
                settings["setup_complete"] = True
                result = connection
            elif path.endswith("/connection"):
                check_attempts += 1
                if check_attempts == 1:
                    connection = {
                        "state": "failed",
                        "reason": "login_timeout",
                        "requires_code": True,
                    }
                else:
                    connection = {
                        "state": "connecting",
                        "requires_code": True,
                        "auth_url": "https://claude.com/cai/oauth/authorize?client_id=fixture&code=true",
                        "message": "Complete provider sign-in.",
                    }
                result = connection
            elif path == "/api/runs":
                if request.method == "POST":
                    assert body["auth_mode"] == "subscription"
                    assert body["model"] == "claude-fixture-priced" and body["effort"] == "high"
                    assert set(body["packs"]) == {"deliver", "belief", "footprint"}
                    assert body["goal"] == malicious
                    assert body["reply_policy"] == "blockers_only"
                    started.append(body)
                    runs.append(
                        {
                            "run_id": "r_fixture",
                            "goal": body["goal"],
                            "state": "running",
                            "harness_id": "claude-code",
                            "model": body["model"],
                            "budget_usd": 20,
                            "pending_asks": 1,
                            "spent_usd": None,
                            "created_at": "2026-10-23T10:00:00Z",
                            "billing": {
                                "mode": "subscription",
                                "model_cost_measured": False,
                                "model_cost_usd": None,
                                "cap_scope": "world_tools",
                                "cap_spent_usd": 0.025,
                            },
                        }
                    )
                    result = detail()
                else:
                    result = {"runs": runs}
            elif path == "/api/lives":
                result = {"lives": []}
            elif path == "/api/runs/r_fixture":
                result = detail()
            elif path == "/api/runs/r_fixture/asks":
                result = {"tickets": tickets}
            elif path == "/api/runs/r_fixture/beliefs":
                query = parse_qs(url.query)
                result = {
                    "available": True,
                    "beliefs": [
                        {
                            "id": "b_fixture",
                            "claim": malicious,
                            "kind": "fact",
                            "status": "active",
                            "rank": 3,
                            "n_support": 1,
                            "evidence": [{"ref": "world:fixture"}],
                            "version_id": "v1",
                        }
                    ],
                }
                if query.get("query") == ["why"]:
                    assert query["id"] == ["b_fixture"]
            elif path == "/api/runs/r_fixture/footprint":
                result = {
                    "items": [
                        {
                            "kind": "account",
                            "state": "active",
                            "title": malicious,
                            "platform": "fixture",
                            "url": "javascript:window.studioXss=true",
                            "reversible": False,
                        }
                    ]
                }
            elif path == "/api/runs/r_fixture/stop":
                runs[0]["state"] = "sealed"
                result = detail()
            elif path.endswith("/touch"):
                assert path == "/v1/asks/t_fixture/touch" and isinstance(body["active"], bool)
            elif path == "/v1/asks/t_fixture/answer":
                assert body == {"decision": "approve", "ask_class": "approval", "reason": None}
                tickets[0]["state"] = "approved"
                runs[0]["pending_asks"] = 0
                result = tickets[0]
            else:
                raise AssertionError("Unexpected route: " + request.method + " " + path)
            await route.fulfill(json=result)

        await page.route("**/*", route_api)
        await page.set_viewport_size({"width": 1440, "height": 1000})
        await page.goto("http://studio.fixture.test/")
        await page.locator("#login-dialog[open]").wait_for()
        assert not any(path == "/api/runs" for _, path, _ in calls)
        await page.get_by_label("Operator token", exact=True).fill(token)
        await page.get_by_role("button", name="Open workspace").click()
        await page.get_by_role("heading", name="A world your agent can live in.").wait_for()
        checks["session_gate_and_token_cleared"] = (
            await page.locator("#operator-token").input_value() == ""
        )
        checks["no_implicit_install_or_run"] = not any(
            path.endswith("/install") or (method == "POST" and path == "/api/runs")
            for method, path, _ in calls
        )
        await page.locator("[data-page=harnesses]").click()
        await page.get_by_role("button", name="Install client", exact=True).click()
        await page.get_by_text("Installed · fixture-1", exact=True).wait_for()
        checks["explicit_native_install"] = installed
        checks["stale_code_flag_does_not_start_signin"] = (
            await page.get_by_label("Provider confirmation code").count() == 0
            and connect_attempts == 0
        )
        await page.get_by_role("button", name="Connect subscription", exact=True).click()
        await page.get_by_text("Waiting for the provider sign-in link…", exact=True).wait_for()
        checks["missing_link_waits_without_code_prompt"] = (
            await page.get_by_label("Provider confirmation code").count() == 0
            and await page.get_by_role("link", name="Open provider sign-in").count() == 0
            and (
                await page.locator(".auth-details")
                .locator("..")
                .locator(".status-pill.connecting")
                .inner_text()
            ).casefold()
            == "connecting"
        )
        await page.screenshot(path=str(output / "studio-signin-waiting.png"), full_page=True)
        await page.get_by_role("button", name="Check connection", exact=True).click()
        await page.get_by_role("button", name="Try sign-in again", exact=True).wait_for()
        checks["failed_signin_has_reason_and_explicit_retry"] = (
            "Sign-in timed out" in await page.locator("#page-content").inner_text()
            and await page.get_by_label("Provider confirmation code").count() == 0
            and connect_attempts == 1
        )
        await page.get_by_role("button", name="Try sign-in again", exact=True).click()
        await page.get_by_role("button", name="Cancel sign-in", exact=True).click()
        await page.get_by_role("button", name="Connect subscription", exact=True).wait_for()
        checks["cancel_returns_to_disconnected_without_code"] = (
            await page.get_by_label("Provider confirmation code").count() == 0
            and connect_attempts == 2
        )
        await page.get_by_role("button", name="Connect subscription", exact=True).click()
        await page.get_by_role("button", name="Restart sign-in", exact=True).click()
        await page.get_by_text("Waiting for the provider sign-in link…", exact=True).wait_for()
        checks["restart_is_explicit_cancel_then_connect"] = connect_attempts == 4
        await page.get_by_role("button", name="Check connection", exact=True).click()
        login = page.get_by_role("link", name="Open provider sign-in")
        await login.wait_for()
        checks["provider_link_precedes_confirmation_code"] = (
            await login.get_attribute("href")
        ).startswith("https://claude.com/cai/oauth/authorize?") and await page.locator(
            ".auth-details"
        ).evaluate(
            "box=>Array.from(box.querySelectorAll('a,input')).map(item=>item.tagName).join(',')==='A,INPUT'"
        )
        await page.screenshot(path=str(output / "studio-signin-code.png"), full_page=True)
        await page.get_by_label("Provider confirmation code").fill(code)
        await page.wait_for_timeout(5200)
        checks["auth_poll_preserves_code_being_entered"] = (
            await page.get_by_label("Provider confirmation code").input_value() == code
        )
        await page.get_by_role("button", name="Submit confirmation code").click()
        await page.get_by_role("button", name="Disconnect subscription", exact=True).wait_for()
        checks["subscription_code_clear"] = await page.locator("input[type=password]").evaluate_all(
            "(items)=>items.every(item=>!item.value)"
        )
        checks["subscription_setup_without_key"] = not settings["credentials"]["anthropic"][
            "configured"
        ]
        await page.get_by_role("button", name="Beam down with this connection").click()
        await page.locator("#new-run-dialog[open]").wait_for()
        order = await page.evaluate(
            "['#run-harness','#run-mode','#run-model','#run-effort','#shape-life','#run-budget']"
            ".map(id=>document.querySelector(id)).every((item,index,all)=>index===0||"
            "Boolean(all[index-1].compareDocumentPosition(item)&Node.DOCUMENT_POSITION_FOLLOWING))"
        )
        checks["dialog_order_harness_connection_model_effort_then_rest"] = order
        checks["subscription_life_is_estimated_on_priced_models"] = (
            await page.locator("#shape-life").is_checked()
            and not await page.locator("#shape-life").is_disabled()
            and "estimated" in await page.locator("#shape-note").inner_text()
            and await page.locator("#run-model option").evaluate_all(
                "(items)=>items.map(item=>[item.value,item.disabled])"
            )
            == [["claude-fixture-priced", False], ["claude-unpriced-fixture", True]]
            and await page.locator("#run-model").input_value() == "claude-fixture-priced"
            and await page.locator("#run-model optgroup").evaluate_all(
                "(items)=>items.map(item=>item.label)"
            )
            == ["Current", "Earlier"]
        )
        checks["effort_offers_the_models_levels"] = await page.locator(
            "#run-effort option"
        ).evaluate_all("(items)=>items.map(item=>item.value)") == ["", "low", "medium", "high"]
        await page.locator("#run-effort").select_option("high")
        await page.locator("#shape-run").check()
        checks["subscription_single_run_still_available"] = (
            await page.locator("#shape-run").is_checked()
            and not await page.locator("#launch-button").is_disabled()
        )
        await page.get_by_text("Add an objective").click()
        await page.get_by_role("textbox", name="Objective").fill(malicious)
        await page.locator("#launch-button").click()
        await page.get_by_text("Model dollars are unavailable.", exact=False).wait_for()
        checks["launch_sends_exact_model_and_effort"] = (
            started[-1]["model"] == "claude-fixture-priced" and started[-1]["effort"] == "high"
        )
        checks["subscription_real_world_cost_unmeasured_model"] = (
            "$0.025" in await page.locator(".metric-value").first.inner_text()
            or "$0.03" in await page.locator(".metric-value").first.inner_text()
        )
        checks["untrusted_run_and_timeline_are_text"] = await page.locator(
            "img"
        ).count() == 0 and not await page.evaluate("Boolean(window.studioXss)")
        await page.locator("[data-page=beliefs]").click()
        await page.get_by_label("Search beliefs").fill("does-not-match")
        await page.get_by_role("heading", name="No matching beliefs").wait_for()
        await page.get_by_label("Search beliefs").fill("")
        await page.get_by_role("button", name="Why this belief?", exact=True).click()
        await page.get_by_text("world:fixture", exact=True).wait_for()
        checks["belief_queries_are_passive"] = not any(
            path.startswith("/v1/act") for _, path, _ in calls
        )
        await page.locator("[data-page=footprint]").click()
        await page.get_by_role("heading", name=malicious, exact=True).wait_for()
        checks["unsafe_footprint_link_rejected"] = (
            await page.locator('a[href^="javascript:"]').count() == 0
        )
        await page.locator("[data-page=asks]").click()
        await page.get_by_role("button", name="Review ask").click()
        await page.locator("#ask-dialog[open]").wait_for()
        await page.get_by_text("Exact action reference", exact=True).click()
        checks["nested_action_arguments_reviewable_as_text"] = (
            "exact-review-fixture" in await page.locator(".exact-action").inner_text()
        )
        await page.get_by_role("button", name="Approve exact action", exact=True).click()
        await page.locator("#ask-dialog").wait_for(state="hidden")
        checks["official_hash_bound_ask_response"] = tickets[0]["state"] == "approved"
        checks["active_human_ticket_released"] = [
            body["active"] for _, path, body in calls if path.endswith("/touch")
        ][-1] is False
        await page.locator("[data-page=harnesses]").click()
        key = page.locator("#key-claude-code")
        await key.fill(secret)
        await page.wait_for_timeout(5200)
        checks["key_draft_survives_harness_polls"] = await key.input_value() == secret
        fail_save = True
        await page.get_by_role("button", name="Save key", exact=True).click()
        await page.get_by_text("could not be saved", exact=False).wait_for()
        checks["secret_cleared_on_failure_never_echoed"] = (
            await page.locator("#key-claude-code").input_value() == ""
            and secret not in await page.locator("body").inner_text()
        )
        await page.locator("#key-claude-code").fill(secret)
        await page.get_by_role("button", name="Save key", exact=True).click()
        await page.get_by_text("API key saved.", exact=False).wait_for()
        checks["write_only_key_on_its_harness"] = (
            await page.locator("#key-claude-code").input_value() == ""
            and secret not in await page.locator("body").inner_text()
            and settings["credentials"]["anthropic"]["configured"]
        )
        await page.locator("[data-page=settings]").click()
        await page.locator("#setting-default_model").wait_for()
        checks["settings_hold_no_model_keys"] = await page.get_by_label(
            "Anthropic API key", exact=True
        ).count() == 0 and await page.locator("#setting-default_model option").evaluate_all(
            "(items)=>items.map(item=>item.value)"
        ) == ["claude-fixture-priced"]
        await page.locator("#new-run-top").click()
        await page.locator("#run-mode").select_option("api_key")
        checks["api_model_exact_priced"] = (
            await page.locator("#run-model").input_value() == "claude-fixture-priced"
            and await page.locator("#run-model option[value=claude-unpriced-fixture]").is_disabled()
            and "$3 in · $15 out per MTok" in await page.locator("#model-note").inner_text()
            and not await page.locator("#launch-button").is_disabled()
        )
        await page.get_by_role("button", name="Close beam-down", exact=True).click()
        await page.locator("[data-page=runs]").click()
        await page.get_by_role("button", name="Beam up", exact=True).click()
        checks["stop_requires_explicit_confirmation"] = runs[0]["state"] == "running"
        await page.get_by_role("button", name="Beam up and seal", exact=True).click()
        await page.get_by_text("Sealed", exact=True).first.wait_for()
        checks["stop_and_actual_state"] = runs[0]["state"] == "sealed"
        checks["no_browser_storage_or_secret_headers"] = await page.evaluate(
            "localStorage.length===0 && sessionStorage.length===0"
        )
        checks["passive_product_asks_only"] = not any(
            method == "GET" and path == "/v1/asks" for method, path, _ in calls
        )
        await page.screenshot(path=str(output / "studio-desktop.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_timeout(100)
        checks["responsive_no_horizontal_overflow"] = await page.evaluate(
            "document.documentElement.scrollWidth<=innerWidth"
        )
        await page.screenshot(path=str(output / "studio-mobile.png"), full_page=True)
        checks["no_script_errors"] = not errors
        assert all(checks.values()), {"checks": checks, "errors": errors}
        print(
            json.dumps(
                {"checks": checks, "requests": len(calls), "provider_calls": 0, "errors": errors}
            )
        )
    finally:
        await backend.aclose()


if __name__ == "__main__":
    asyncio.run(main())
