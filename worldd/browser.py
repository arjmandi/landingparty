"""Leased world-held browser contexts with explicit takeover and time metering."""

from __future__ import annotations

import asyncio
import base64
import inspect
import os
import secrets
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit


def _browser_errors():
    try:
        from playwright.async_api import Error as PlaywrightError
    except ImportError:
        return (ValueError, OSError, RuntimeError, ImportError)
    return (PlaywrightError, ValueError, OSError, RuntimeError, ImportError)


from .approvals import action_hash, timestamp
from .vault import origin


def blocked(kind, detail, rule_id):
    return {"kind": "blocked", "type": kind, "detail": detail, "rule_id": rule_id}


def credential_secrets(value, sensitive=False):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from credential_secrets(
                item,
                sensitive
                or key.lower()
                in {
                    "password",
                    "api_key",
                    "access_token",
                    "refresh_token",
                    "token",
                    "cookies",
                    "authorization",
                    "secret",
                    "client_secret",
                },
            )
    elif isinstance(value, list):
        for item in value:
            yield from credential_secrets(item, sensitive)
    elif sensitive and isinstance(value, str) and value:
        yield value


class PlaywrightBackend:
    """Real Chromium adapter. HTTPS certificate validation always stays enabled.

    The world CA must be installed in the browser OS user's trust store by the
    environment's setup/preflight. Missing trust produces a typed TLS failure;
    this adapter never uses ignore_https_errors or certificate-ignore flags.
    Contexts persist in memory and their saved state is encrypted by the vault.
    """

    def __init__(self, *, headless=True, executable_path=None, sandbox=True, state_dir=None):
        self.headless = headless
        self.executable_path = executable_path or os.getenv("WORLD_CHROMIUM_EXECUTABLE")
        self.sandbox = sandbox
        self.state_dir = Path(state_dir or tempfile.mkdtemp(prefix="landing-party-browser-"))
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self._playwright = None
        self._browsers = []

    def child_environment(self):
        allowed = {"PATH", "LANG", "LC_ALL", "TZ", "DISPLAY", "WAYLAND_DISPLAY"}
        result = {key: value for key, value in os.environ.items() if key in allowed}
        # These are the dedicated browser child's own directories, rather than
        # the world process's home or any ambient model-provider environment.
        result["HOME"] = str(self.state_dir)
        for key, directory in (
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_CACHE_HOME", "cache"),
            ("XDG_DATA_HOME", "share"),
        ):
            path = self.state_dir / directory
            path.mkdir(exist_ok=True, mode=0o700)
            result[key] = str(path)
        return result

    async def trust_ca(self, ca_path):
        """Add the world CA to this browser user's NSS database, preserving TLS."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        certificate = x509.load_pem_x509_certificate(Path(ca_path).read_bytes())
        if not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError("Browser trust input must be a certificate authority")
        trust = self.state_dir / ".pki/nssdb"
        trust.mkdir(parents=True, exist_ok=True, mode=0o700)

        async def certutil(*args):
            child = await asyncio.create_subprocess_exec(
                "certutil",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.child_environment(),
            )
            output, _ = await child.communicate()
            return child.returncode, output

        if not (trust / "cert9.db").exists():
            code, _ = await certutil("-N", "--empty-password", "-d", "sql:" + str(trust))
            if code:
                raise ValueError("Could not initialize the private browser certificate database")
        code, existing = await certutil(
            "-L", "-d", "sql:" + str(trust), "-n", "LandingPartyWorld", "-a"
        )
        if not code:
            old = x509.load_pem_x509_certificate(existing)
            if old.fingerprint(hashes.SHA256()) == certificate.fingerprint(hashes.SHA256()):
                return
            await certutil("-D", "-d", "sql:" + str(trust), "-n", "LandingPartyWorld")
        code, _ = await certutil(
            "-A",
            "-d",
            "sql:" + str(trust),
            "-n",
            "LandingPartyWorld",
            "-t",
            "C,,",
            "-i",
            str(ca_path),
        )
        if code:
            raise ValueError(
                "Could not trust the world CA in the private browser certificate database"
            )

    async def open(self, credential, *, proxy=None, user_agent=None):
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if proxy and proxy.get("ca_path"):
            await self.trust_ca(proxy["ca_path"])
        launch = {
            "headless": self.headless,
            "chromium_sandbox": self.sandbox,
            "env": self.child_environment(),
        }
        if proxy:
            launch["proxy"] = {
                key: proxy[key] for key in ("server", "username", "password") if key in proxy
            }
            launch["proxy"]["bypass"] = "<-loopback>"
        if self.executable_path:
            launch["executable_path"] = self.executable_path
        browser = await self._playwright.chromium.launch(**launch)
        self._browsers.append(browser)
        options = {"user_agent": user_agent or "landing-party/0.1"}
        if proxy:
            options["proxy"] = {
                key: proxy[key] for key in ("server", "username", "password") if key in proxy
            }
            options["proxy"]["bypass"] = "<-loopback>"
        if credential.get("storage_state"):
            options["storage_state"] = credential["storage_state"]
        elif credential.get("cookies"):
            options["storage_state"] = {"cookies": credential["cookies"], "origins": []}
        context = await browser.new_context(**options)
        context.set_default_timeout(30000)
        if credential.get("_world_origin"):

            async def fill_headers(route):
                request = route.request
                headers = dict(request.headers)
                if origin(request.url) == credential["_world_origin"]:
                    token = (
                        credential.get("access_token")
                        or credential.get("api_key")
                        or credential.get("token")
                    )
                    if token:
                        header_name = credential.get("header_name", "Authorization")
                        if header_name.lower() not in {"authorization", "x-api-key"}:
                            await route.abort("blockedbyclient")
                            return
                        prefix = credential.get(
                            "header_prefix",
                            "Bearer " if header_name.lower() == "authorization" else "",
                        )
                        headers[header_name] = prefix + token
                await route.continue_(headers=headers)

            await context.route("**/*", fill_headers)
        page = await context.new_page()
        # No tracing, keyboard listeners, or HAR capture in the browser process:
        # the gateway is authoritative and human takeover keystrokes stay private.
        return {"browser": browser, "context": context, "page": page, "credential": credential}

    async def goto(self, handle, url):
        response = await handle["page"].goto(url, wait_until="domcontentloaded", timeout=30000)
        return {"url": handle["page"].url, "status": response.status if response else None}

    async def current_url(self, handle):
        return handle["page"].url

    async def snapshot(self, handle, view):
        page, result = handle["page"], {"url": handle["page"].url}
        if view in {"tree", "both"}:
            result["tree"] = await page.locator("body").aria_snapshot()
        if view in {"screenshot", "both"}:
            hidden = [page.locator("input[type=password]")]
            values = set(credential_secrets(handle.get("credential", {})))
            for secret in values:
                hidden.append(page.get_by_text(secret, exact=False))
            for control in await page.locator("input,textarea").all():
                value = await control.input_value()
                if any(secret in value for secret in values):
                    hidden.append(control)
            result["screenshot"] = await page.screenshot(mask=hidden)
        result["site_tools"] = await page.locator(
            "link[rel='mcp-server'],link[rel='mcp']"
        ).evaluate_all("nodes => nodes.map(n => ({name:n.title||'site',url:n.href}))")
        return result

    async def action(self, handle, tool, params, *, human=False):
        page = handle["page"]
        selector = params.get("selector")
        if tool == "click":
            await page.locator(selector).click()
        elif tool == "type":
            target = page.locator(selector)
            if not human and await target.get_attribute("type") == "password":
                raise ValueError("Password fields are filled only by the vault or human takeover")
            await target.fill(params.get("text", params.get("value", "")))
        elif tool == "press":
            if selector:
                await page.locator(selector).press(params["key"])
            else:
                await page.keyboard.press(params["key"])
        elif tool == "scroll":
            await page.mouse.wheel(float(params.get("x", 0)), float(params.get("y", 600)))
        elif tool == "select":
            await page.locator(selector).select_option(params.get("value"))
        elif tool == "upload":
            await page.locator(selector).set_input_files(params["path"])
        elif tool == "back":
            await page.go_back(wait_until="domcontentloaded")
        else:
            raise ValueError("Unknown browser action")
        return {"url": page.url}

    async def login(self, handle, login):
        page = handle["page"]
        await page.goto(login["url"], wait_until="domcontentloaded")
        if login.get("username_selector"):
            await page.locator(login["username_selector"]).fill(login.get("username", ""))
        if login.get("password_selector"):
            await page.locator(login["password_selector"]).fill(login.get("password", ""))
        if login.get("submit_selector"):
            await page.locator(login["submit_selector"]).click()
        return {"url": page.url}

    async def needs_human(self, handle):
        page = handle["page"]
        content = (await page.locator("body").inner_text())[:20000].lower()
        if any(
            marker in content
            for marker in (
                "verify you are human",
                "captcha",
                "verification code",
                "two-factor authentication",
                "multi-factor authentication",
            )
        ):
            return "challenge"
        parsed = urlsplit(page.url)
        if any(
            part in parsed.path.lower()
            for part in ("/login", "/signin", "/sign-in", "/oauth/authorize")
        ):
            return "login"
        return None

    async def close(self, handle):
        state = await handle["context"].storage_state()
        await handle["context"].close()
        await handle["browser"].close()
        if handle["browser"] in self._browsers:
            self._browsers.remove(handle["browser"])
        return state

    async def aclose(self):
        for browser in list(self._browsers):
            await browser.close()
        self._browsers.clear()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None


class BrowserService:
    def __init__(self, world, *, backend=None, clock=None, idle_seconds=300):
        self.world, self.store = world, world.store
        self.backend = backend or PlaywrightBackend(
            state_dir=self.store.data_dir / "vault/browser-home"
        )
        self._injected_backend = backend is not None
        self.clock = clock or (lambda: datetime.now(UTC))
        self.idle_seconds = idle_seconds
        self.sessions = {}
        self.account_sessions = {}
        self._task = None

    def _service(self, name):
        return getattr(self.world, "services", {}).get(name)

    @staticmethod
    def _account_key(run_id, alias):
        return ("account", alias) if alias else ("anonymous", run_id)

    def _session(self, run_id, session_id):
        session = self.sessions.get(session_id)
        if not session or session["run_id"] != run_id or session["state"] == "closed":
            raise ValueError("Browser session does not belong to this run or is closed")
        return session

    def list_sessions(self, run_id=None):
        return [
            {
                key: session.get(key)
                for key in (
                    "session_id",
                    "run_id",
                    "alias",
                    "state",
                    "opened_at",
                    "last_active_at",
                    "url",
                    "ticket_id",
                )
            }
            for session in self.sessions.values()
            if run_id is None or session["run_id"] == run_id
        ]

    def _rate(self, run_id):
        from .lane import WORLD_TOOL_PRICES

        value = Decimal(
            str(
                self.store.get_run(run_id)["config"].get(
                    "browser_rate_usd_per_hour", WORLD_TOOL_PRICES["browser_usd_per_hour"]
                )
            )
        )
        if not value.is_finite() or value < 0:
            raise ValueError("Invalid browser-hour rate")
        return value

    def _reserve(self, run_id):
        return self.store.reserve(
            run_id, self._rate(run_id) * Decimal(str(self.idle_seconds + 60)) / 3600
        )

    def _meter(self, session, *, final=False):
        now = self.clock()
        seconds = max(0, (now - session["meter_at"]).total_seconds())
        cost = self._rate(session["run_id"]) * Decimal(str(seconds)) / 3600
        reservation = session.get("reservation")
        if reservation:
            if not self.store.settle(reservation, "browser", cost):
                self.store.update_run(session["run_id"], tainted=True)
                return False
            session["reservation"] = None
        elif cost:
            if not self.store.debit(session["run_id"], "browser", cost):
                self.store.update_run(session["run_id"], tainted=True)
                return False
        session["meter_at"] = now
        if not final and self.store.get_run(session["run_id"])["state"] == "running":
            session["reservation"] = self._reserve(session["run_id"])
            if session["reservation"] is None:
                return False
        return True

    async def _binding(self, run_id, session_id, token):
        proxy = self._service("proxy")
        if proxy:
            value = proxy.bind_browser(run_id, session_id, token)
            return await value if inspect.isawaitable(value) else value
        if self._injected_backend:
            return None
        raise ValueError("Live browsing requires the recorded network proxy")

    async def _open(self, run_id, action_id, alias=None, target_url=None):
        config = self.store.get_run(run_id)["config"]
        if config.get("stage", 0) < 2:
            return blocked(
                "policy_deny", "Browser sessions are available from the outpost stage", "stage_gate"
            )
        if config.get("research_mode") == "development" and alias and not self._injected_backend:
            return blocked(
                "policy_deny",
                "Development runs cannot open a live authenticated browser",
                "development_boundary",
            )
        key = self._account_key(run_id, alias)
        previous = self.account_sessions.get(key)
        if previous and previous in self.sessions:
            session = self.sessions[previous]
            if session["run_id"] != run_id:
                return blocked(
                    "account_busy", "This account is leased by another run", "account_serialization"
                )
            if session["lock"].locked():
                return blocked(
                    "account_busy",
                    "This account is handling another action",
                    "account_serialization",
                )
            return {"kind": "result", "state": "ok", "session_id": previous}
        vault, account, credential = self._service("vault"), None, {}
        bootstrap = None
        if alias:
            if not vault:
                return blocked(
                    "login_wall", "The credential broker is unavailable", "vault_unavailable"
                )
            try:
                account = vault.get_account(alias, run_id=run_id)
                read_urls = account["scope_rules"].get("read", [])
                request = {
                    "url": target_url or (read_urls[0] if read_urls else account["origin"] + "/"),
                    "tool": "open_session",
                    "alias": alias,
                }
            except (KeyError, ValueError):
                request = {
                    "url": "https://unprovisioned.invalid/",
                    "tool": "open_session",
                    "alias": alias,
                }
            verdict = await vault.authorize(run_id, action_id, alias, "read", request)
            if verdict.get("kind") != "result":
                return verdict
            credential = vault.consume(
                verdict["lease"]["lease_id"], run_id, action_id, "read", request
            )
            if verdict["lease"].get("grant_id"):
                bootstrap = {
                    "scope": "read",
                    "action_id": action_id,
                    "grant_id": verdict["lease"]["grant_id"],
                    "granted_request_hash": action_hash(request),
                }
        reservation = self._reserve(run_id)
        if reservation is None:
            return blocked(
                "budget_exhausted",
                "Remaining budget cannot cover a browser session",
                "browser_budget",
            )
        session_id, token = "session_" + secrets.token_hex(16), secrets.token_urlsafe(32)
        now = self.clock()
        session = {
            "session_id": session_id,
            "run_id": run_id,
            "alias": alias,
            "account": account,
            "credential": credential,
            "token": token,
            "lock": asyncio.Lock(),
            "handle": None,
            "state": "opening",
            "opened_at": timestamp(now),
            "last_active_at": timestamp(now),
            "meter_at": now,
            "reservation": reservation,
            "url": "about:blank",
            "active": None,
            "takeover_started": None,
            "ticket_id": None,
            "bootstrap_clearance": bootstrap,
        }
        self.sessions[session_id] = session
        self.account_sessions[key] = session_id
        try:
            proxy = await self._binding(run_id, session_id, token)
            browser_credential = (
                {**credential, "_world_origin": account["origin"]} if account else credential
            )
            session["handle"] = await self.backend.open(
                browser_credential, proxy=proxy, user_agent=f"landing-party/0.1 (run {run_id})"
            )
            session["state"] = "ready"
            self.store.append_record(
                run_id,
                "session_open",
                session_id=session_id,
                alias=alias,
                account_id=account["account_id"] if account else None,
            )
            if credential.get("login") and not credential.get("storage_state"):
                await self._login(session, action_id)
                challenge = await self._challenge(session, action_id)
                if challenge:
                    return challenge
            return {"kind": "result", "state": "ok", "session_id": session_id}
        except _browser_errors() as exc:
            self._meter(session, final=True)
            if session["handle"] is not None:
                try:
                    await self.backend.close(session["handle"])
                except _browser_errors() as cleanup_error:
                    self.store.append_record(
                        run_id,
                        "browser_cleanup_failed",
                        session_id=session_id,
                        error_class=type(cleanup_error).__name__,
                    )
            self.sessions.pop(session_id, None)
            self.account_sessions.pop(key, None)
            return self._failure(exc)

    @staticmethod
    def _failure(exc):
        # Exception strings may include filled values; report classes only.
        name = type(exc).__name__
        if "Timeout" in name:
            return blocked("timeout", "The browser operation timed out", "browser_timeout")
        if "SSL" in name or "CERT" in str(exc).upper() or "TLS" in str(exc).upper():
            return blocked(
                "tls", "The browser could not verify the destination certificate", "browser_tls"
            )
        if isinstance(exc, ValueError):
            return blocked(
                "policy_deny", "The browser action is outside its supported scope", "browser_scope"
            )
        return blocked(
            "platform_block", f"The browser operation failed ({name})", "browser_failure"
        )

    async def _login(self, session, action_id):
        login = session["credential"].get("login")
        if (
            not login
            or not session["account"]
            or origin(login["url"]) != session["account"]["origin"]
        ):
            return False
        session["active"] = {
            "scope": "read",
            "action_id": action_id,
            "allow_login": True,
            "request": {"url": login["url"]},
            "login_paths": login.get("allowed_paths", [urlsplit(login["url"]).path]),
        }
        try:
            await self.backend.login(session["handle"], login)
            session["url"] = await self.backend.current_url(session["handle"])
            return True
        finally:
            session["active"] = None

    def _public_tree(self, session, text):
        for secret in credential_secrets(session["credential"]):
            text = text.replace(secret, "[redacted]")
        return text

    def _public_result(self, session, value):
        if isinstance(value, str):
            return self._public_tree(session, value)
        if isinstance(value, list):
            return [self._public_result(session, item) for item in value]
        if isinstance(value, dict):
            return {key: self._public_result(session, item) for key, item in value.items()}
        return value

    async def _snapshot(self, session, action_id, view):
        if view not in {"tree", "screenshot", "both"}:
            raise ValueError("Unknown snapshot view")
        result = await self.backend.snapshot(session["handle"], view)
        result = {**result}
        screenshot = result.pop("screenshot", None)
        result = self._public_result(session, result)
        if screenshot is not None:
            directory = self.store.run_dir(session["run_id"]) / "world/browser"
            directory.mkdir(parents=True, exist_ok=True)
            name = f"{session['session_id']}-{action_id}.png"
            (directory / name).write_bytes(screenshot)
            result["screenshot_handle"] = "/world/browser/" + name
        self.store.append_record(
            session["run_id"],
            "browser_observation",
            session_id=session["session_id"],
            action_id=action_id,
            modality=view,
            path="playwright",
        )
        return {"kind": "result", "session_id": session["session_id"], **result}

    async def _challenge(self, session, action_id):
        checker = getattr(self.backend, "needs_human", None)
        reason = await checker(session["handle"]) if checker else None
        if reason == "login" and session["credential"].get("login"):
            await self._login(session, action_id)
            reason = await checker(session["handle"])
        if not reason:
            session.pop("challenge", None)
            return None
        if not session.get("challenge") or session["challenge"]["reason"] != reason:
            session["challenge"] = {
                "reason": reason,
                "action_id": action_id,
                "actual_tokens": 0,
                "call_ids": set(),
                "triggered": False,
                "clearance": session.get("active") or session.get("bootstrap_clearance"),
            }
        asks = self._service("asks")
        if not asks:
            return blocked(
                "login_wall" if reason == "login" else "captcha",
                "This session requires human assistance",
                "browser_human_required",
            )
        ticket = asks.raise_ticket(
            session["run_id"],
            action_id,
            "takeover",
            pending_action={
                "session_id": session["session_id"],
                "url": session["url"],
                "reason": reason,
            },
            platform=(session.get("account") or {}).get("platform"),
            scope="read",
        )
        session["ticket_id"] = ticket["ticket_id"]
        return {**ticket, "state": "needs_human", "session_id": session["session_id"]}

    def observe_model_usage(self, run_id, actual_tokens, call_id=None):
        """Count settled model work against a recorded, unresolved challenge."""
        if self.store.get_run(run_id)["state"] != "running":
            return []
        if (
            isinstance(actual_tokens, bool)
            or not isinstance(actual_tokens, int)
            or actual_tokens < 0
        ):
            raise ValueError("Challenge metering requires actual nonnegative integer tokens")
        config = self.store.get_run(run_id)["config"]
        threshold = config.get("breaker_policy", {}).get("challenge_tokens", 2000)
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ValueError("Challenge token threshold must be a positive integer")
        triggered = []
        for session in self.sessions.values():
            challenge = session.get("challenge")
            if session["run_id"] != run_id or session["state"] != "ready" or not challenge:
                continue
            if call_id and call_id in challenge["call_ids"]:
                continue
            if call_id:
                challenge["call_ids"].add(call_id)
            challenge["actual_tokens"] += actual_tokens
            if challenge["triggered"] or challenge["actual_tokens"] <= threshold:
                continue
            challenge["triggered"] = True
            asks = self._service("asks")
            if not asks:
                continue
            ticket = asks.get(session["ticket_id"]) if session.get("ticket_id") else None
            if not ticket:
                result = asks.raise_ticket(
                    run_id,
                    challenge["action_id"],
                    "takeover",
                    pending_action={
                        "session_id": session["session_id"],
                        "url": session["url"],
                        "reason": challenge["reason"],
                    },
                    platform=(session.get("account") or {}).get("platform"),
                    scope="read",
                )
                session["ticket_id"] = result["ticket_id"]
            event = {
                "type": "browser_challenge_threshold",
                "session_id": session["session_id"],
                "ticket_id": session["ticket_id"],
                "actual_tokens": challenge["actual_tokens"],
                "threshold_tokens": threshold,
                "call_id": call_id,
            }
            self.store.append_record(
                run_id, "breaker_triggered", rule_id="browser_challenge_tokens", **event
            )
            self.world.push(run_id, event)
            triggered.append(event)
        return triggered

    async def handle(self, run_id, action_id, tool, params):
        if tool == "open_session":
            return await self._open(
                run_id, action_id, params.get("alias"), params.get("target_url", params.get("url"))
            )
        session_id = params.get("session_id", params.get("session"))
        session = self._session(run_id, session_id)
        if session["state"] == "human_held":
            return blocked(
                "human_holds_session",
                "This browser session is held by the operator",
                "takeover_exclusive",
            )
        if session["lock"].locked():
            return blocked(
                "account_busy", "This account is handling another action", "account_serialization"
            )
        async with session["lock"]:
            if tool == "close":
                return await self._close(session)
            session["last_active_at"] = timestamp(self.clock())
            if not self._meter(session):
                await self._close(session)
                return blocked(
                    "budget_exhausted", "Browser session budget is exhausted", "browser_budget"
                )
            if tool == "snapshot":
                return await self._snapshot(session, action_id, params.get("view", "tree"))
            if session.get("challenge") and not (
                tool in {"goto", "back"} and params.get("url") != session["url"]
            ):
                return blocked(
                    "captcha",
                    "A human must complete this challenge in the takeover session",
                    "browser_human_required",
                )
            if tool not in {"goto", "click", "type", "press", "scroll", "select", "upload", "back"}:
                raise ValueError("Unknown browser tool")
            scope = params.get("scope", "read")
            url = params.get("url", params.get("target_url", session["url"]))
            if tool == "goto":
                origin(url)
            request = {
                "url": url,
                "tool": tool,
                "session_id": session_id,
                "params": {
                    key: value
                    for key, value in params.items()
                    if key not in {"session_id", "session"}
                },
            }
            if params.get("recipient"):
                request["recipient"] = params["recipient"]
            if session["account"]:
                vault = self._service("vault")
                verdict = await vault.authorize(run_id, action_id, session["alias"], scope, request)
                if verdict.get("kind") != "result":
                    return verdict
                vault.consume(verdict["lease"]["lease_id"], run_id, action_id, scope, request)
                session["active"] = {
                    "scope": scope,
                    "action_id": action_id,
                    "request": request,
                    "lease_id": verdict["lease"]["lease_id"],
                    "grant_id": verdict["lease"].get("grant_id"),
                    "granted_request_hash": action_hash(request),
                }
            elif scope != "read":
                return blocked(
                    "policy_deny", "Anonymous browser sessions are read-only", "anonymous_read_only"
                )
            else:
                session["active"] = {"scope": "read", "action_id": action_id, "request": request}
                grants = self._service("grants")
                if grants and "browse" not in self.store.get_run(run_id)["config"].get("packs", []):
                    clearance = grants.require(
                        run_id,
                        "read",
                        url=url,
                        action_hash=action_hash(request),
                        consume=True,
                        action_id=action_id,
                        tool=tool,
                    )
                    if clearance.get("kind") != "result":
                        session["active"] = None
                        return clearance
                    session["active"].update(
                        grant_id=clearance["grant_id"], granted_request_hash=action_hash(request)
                    )
            try:
                if tool == "upload":
                    raw = params.get("handle", params.get("path", ""))
                    if raw.startswith("/world/fetched/"):
                        work = (self.store.run_dir(run_id) / "world/fetched").resolve()
                        path = (work / raw.removeprefix("/world/fetched/")).resolve()
                    else:
                        work = (self.store.run_dir(run_id) / "work").resolve()
                        path = (work / raw.removeprefix("/work/")).resolve()
                    if not path.is_relative_to(work) or not path.is_file():
                        raise ValueError("Uploads must be files from this run's workspace")
                    params = {**params, "path": str(path)}
                if tool == "goto":
                    await self.backend.goto(session["handle"], url)
                else:
                    await self.backend.action(session["handle"], tool, params)
                session["url"] = await self.backend.current_url(session["handle"])
                challenge = await self._challenge(session, action_id)
                if challenge:
                    return challenge
                if scope in {"message", "post"}:
                    self._service("vault").record_contact(
                        session["alias"], counterparty=request.get("recipient")
                    )
                return await self._snapshot(session, action_id, params.get("view", "tree"))
            except asyncio.CancelledError:
                raise
            except _browser_errors() as exc:
                return self._failure(exc)
            finally:
                session["active"] = None
                session["last_active_at"] = timestamp(self.clock())
                self._meter(session)

    async def authorize_network_request(self, run_id, method, url, headers, body, *, token=None):
        decision = await self._authorize_network_request(
            run_id, method, url, headers, body, token=token
        )
        # Raw HTML/JSON can contain hidden session or CSRF tokens. The browser
        # gets the bytes privately; the agent gets only requested observations.
        return {**decision, "response_private": True}

    async def _authorize_network_request(self, run_id, method, url, headers, body, *, token=None):
        session = next(
            (
                value
                for value in self.sessions.values()
                if value["run_id"] == run_id and secrets.compare_digest(value["token"], token or "")
            ),
            None,
        )
        if not session or session["state"] not in {"ready", "human_held"}:
            return {"allow": False, "rule_id": "browser_session"}
        account, active = session["account"], session.get("active")
        read = method.upper() in {"GET", "HEAD", "OPTIONS"}
        grants = self._service("grants")
        if grants and "browse" not in self.store.get_run(run_id)["config"].get("packs", []):
            clearance_source = active
            if active and active.get("allow_login"):
                clearance_source = session.get("bootstrap_clearance")
            elif active and active.get("human"):
                clearance_source = (session.get("challenge") or {}).get("clearance")
            if not clearance_source or not clearance_source.get("grant_id"):
                return {"allow": False, "rule_id": "grant_scope"}
            clearance = grants.authorize_used(
                run_id,
                clearance_source["action_id"],
                clearance_source["scope"],
                platform=(account or {}).get("platform"),
                url=url,
                action_hash=clearance_source.get("granted_request_hash"),
                grant_id=clearance_source["grant_id"],
            )
            if clearance.get("kind") != "result":
                return {"allow": False, "rule_id": clearance.get("rule_id", "grant_scope")}
        if not account:
            return {"allow": read and session["state"] == "ready", "rule_id": "anonymous_read_only"}
        vault = self._service("vault")
        try:
            current_account = vault.get_account(session["alias"], run_id=run_id)
            vault._validate_terms(current_account["platform"], current_account["label"])
            if current_account["state"] != "ready":
                return {"allow": False, "rule_id": "account_revoked"}
        except (KeyError, ValueError):
            return {"allow": False, "rule_id": "platform_terms"}
        try:
            same_origin = origin(url) == account["origin"]
        except ValueError:
            same_origin = False
        if read and not active:
            permitted = same_origin and vault.request_within_scope(
                current_account, "read", {"url": url}
            )
            return {
                "allow": permitted and session["state"] == "ready",
                "rule_id": "browser_read",
                "allow_credentials": permitted,
            }
        if not active or not same_origin:
            return {"allow": False, "rule_id": "browser_lease_origin"}
        if active.get("allow_login"):
            permitted = urlsplit(url).path in active.get("login_paths", [])
            return {
                "allow": same_origin and (read or permitted),
                "rule_id": "vault_fill",
                "allow_credentials": True,
                "allow_login": True,
                "allow_write": permitted,
                "credential_body": permitted,
                "scope": "read",
            }
        if session["state"] == "human_held" and active.get("human"):
            return {
                "allow": same_origin,
                "rule_id": "human_takeover",
                "allow_credentials": True,
                "allow_login": True,
                "allow_write": same_origin,
                "credential_body": same_origin,
            }
        scope = active["scope"]
        permitted = self._service("vault").request_within_scope(account, scope, {"url": url})
        return {
            "allow": permitted and (read or scope != "read"),
            "rule_id": "browser_lease_scope",
            "allow_credentials": permitted,
            "allow_write": permitted and scope != "read",
            "scope": scope,
            "lease_id": active.get("lease_id"),
        }

    def takeover_start(self, session_id, ticket_id, *, reason="operator_takeover"):
        session = self.sessions.get(session_id)
        if not session or session["lock"].locked() or session["state"] != "ready":
            raise ValueError("The browser session is not available for takeover")
        asks = self._service("asks")
        asks.expire()
        ticket = asks.get(ticket_id)
        if (
            ticket["run_id"] != session["run_id"]
            or ticket["kind"] != "takeover"
            or ticket["state"] not in {"approved", "accepted"}
            or ticket["body"].get("session_id") != session_id
        ):
            raise ValueError("A takeover requires the approved ticket for this session")
        active = asks.touch(ticket_id, active=True)
        if active.get("kind") == "blocked":
            return active
        session.update(state="human_held", takeover_started=self.clock(), ticket_id=ticket_id)
        self.store.append_record(
            session["run_id"],
            "takeover_start",
            session_id=session_id,
            ticket_id=ticket_id,
            reason=reason,
        )
        return {"session_id": session_id, "state": "human_held"}

    async def human_snapshot(self, session_id, *, view="both"):
        session = self.sessions.get(session_id)
        if not session or session["state"] != "human_held":
            raise ValueError("Session is not held by the operator")
        # Researcher-only: no record, inbox, or agent observation file is written.
        result = dict(await self.backend.snapshot(session["handle"], view))
        screenshot = result.pop("screenshot", None)
        if screenshot is not None:
            result["screenshot_data_url"] = (
                "data:image/png;base64," + base64.b64encode(screenshot).decode()
            )
        return result

    async def human_action(self, session_id, tool, params):
        session = self.sessions.get(session_id)
        if not session or session["state"] != "human_held":
            raise ValueError("Session is not held by the operator")
        session["active"] = {"scope": "settings", "human": True}
        try:
            if tool == "goto":
                await self.backend.goto(session["handle"], params["url"])
            else:
                await self.backend.action(session["handle"], tool, params, human=True)
            return {"session_id": session_id, "state": "human_held"}
        finally:
            session["active"] = None

    async def takeover_done(self, session_id):
        session = self.sessions.get(session_id)
        if not session or session["state"] != "human_held":
            raise ValueError("Session is not held by the operator")
        duration = min(600, max(0, (self.clock() - session["takeover_started"]).total_seconds()))
        session["state"] = "ready"
        session["url"] = await self.backend.current_url(session["handle"])
        session["last_active_at"] = timestamp(self.clock())
        checker = getattr(self.backend, "needs_human", None)
        if checker and not await checker(session["handle"]):
            session.pop("challenge", None)
        self.store.append_record(
            session["run_id"],
            "takeover_end",
            session_id=session_id,
            ticket_id=session["ticket_id"],
            duration_s=duration,
        )
        asks = self._service("asks")
        complete = getattr(asks, "complete_takeover", None)
        if complete:
            complete(session["ticket_id"], human_seconds=0)
        return {"session_id": session_id, "state": "ready", "fresh_observation_required": True}

    async def _close(self, session):
        if session["state"] == "closed":
            return {"kind": "result", "state": "closed", "session_id": session["session_id"]}
        if session["state"] == "human_held":
            duration = min(
                600, max(0, (self.clock() - session["takeover_started"]).total_seconds())
            )
            self.store.append_record(
                session["run_id"],
                "takeover_end",
                session_id=session["session_id"],
                ticket_id=session["ticket_id"],
                duration_s=duration,
                reason="session_close",
            )
        session["state"] = "closed"
        self._meter(session, final=True)
        try:
            if session["handle"] is not None:
                state = await self.backend.close(session["handle"])
                if session["alias"] and state:
                    self._service("vault").update_storage_state(session["alias"], state)
        finally:
            session["credential"] = {}
            session["active"] = None
            self.account_sessions.pop(self._account_key(session["run_id"], session["alias"]), None)
            self.sessions.pop(session["session_id"], None)
            self.store.append_record(
                session["run_id"], "session_close", session_id=session["session_id"]
            )
        return {"kind": "result", "state": "closed", "session_id": session["session_id"]}

    async def stop_run(self, run_id):
        for session in list(self.sessions.values()):
            if session["run_id"] == run_id:
                await self._close(session)

    async def _idle_loop(self):
        while True:
            await asyncio.sleep(min(30, self.idle_seconds))
            await self.check_idle()

    async def check_idle(self):
        for session in list(self.sessions.values()):
            if session["state"] == "human_held":
                self._meter(session)
                if (self.clock() - session["takeover_started"]).total_seconds() >= 600:
                    await self.takeover_done(session["session_id"])
                continue
            if session["lock"].locked():
                continue
            idle = (
                self.clock() - datetime.fromisoformat(session["last_active_at"])
            ).total_seconds()
            if idle >= self.idle_seconds or not self._meter(session):
                await self._close(session)

    async def recover(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._idle_loop())

    async def aclose(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for session in list(self.sessions.values()):
            await self._close(session)
        closer = getattr(self.backend, "aclose", None)
        if closer:
            await closer()
