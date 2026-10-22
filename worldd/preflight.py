"""Executable preflight checks and the operator's two-second kill sentinel."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit


class PreflightService:
    def __init__(self, world, *, sentinel_enabled=False):
        self.sentinel_enabled = sentinel_enabled
        self.world = world
        self.store = world.store
        self.monitor = None

    def environment(self, run_id: str, values: dict) -> dict:
        """Validate supplied harness configuration without recording its values."""
        run = self.store.get_run(run_id)
        model_urls = [
            values.get(key) for key in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL") if values.get(key)
        ]

        def local_http(value, ports, *, allow_credentials=False):
            try:
                parsed = urlsplit(value or "")
                return (
                    parsed.scheme == "http"
                    and parsed.hostname
                    in {"127.0.0.1", "localhost", "world", "worldd", "172.30.77.2"}
                    and parsed.port in ports
                    and (allow_credentials or parsed.username is None)
                )
            except ValueError:
                return False

        cert = values.get("SSL_CERT_FILE")
        valid_ca = False
        proxy = getattr(self.world, "services", {}).get("proxy")
        if proxy is not None and values.get("WORLD_CA_SHA256"):
            from cryptography.hazmat.primitives import hashes

            valid_ca = (
                values["WORLD_CA_SHA256"] == proxy.ca.certificate.fingerprint(hashes.SHA256()).hex()
            )
        elif cert and Path(cert).is_file():
            try:
                from cryptography import x509
                from cryptography.hazmat.primitives import hashes

                candidate_ca = x509.load_pem_x509_certificate(Path(cert).read_bytes())
                valid_ca = proxy is not None and candidate_ca.fingerprint(
                    hashes.SHA256()
                ) == proxy.ca.certificate.fingerprint(hashes.SHA256())
            except (ValueError, OSError):
                valid_ca = False
        checks = {
            "run_binding": values.get("WORLD_RUN_ID") == run_id,
            "model_lane": bool(model_urls)
            and all(local_http(value, {8081}) for value in model_urls),
            "http_proxy": local_http(values.get("HTTP_PROXY"), {8080}, allow_credentials=True),
            "https_proxy": local_http(values.get("HTTPS_PROXY"), {8080}, allow_credentials=True),
            "world_ca": valid_ca,
            "no_control_or_provider_env": not values.get("WORLD_PRIVATE_ENV_PRESENT"),
            "fenced_requested": run["config"].get("isolation_profile") == "fenced",
        }
        result = {
            "checks": checks,
            "passed": all(value for key, value in checks.items() if key != "fenced_requested"),
            "fenced_verified": False,
        }
        self.store.append_record(run_id, "preflight", test="harness_environment", **result)
        return result

    async def isolation(self, run_id: str, execute, *, firewall_inspect=None) -> dict:
        """Run real commands in the harness namespace through a trusted executor.

        ``execute(argv)`` returns {returncode, stdout?, stderr?}; output is never
        copied into the record. A failure alone is insufficient: firewall policy
        and increasing drop counters must independently corroborate enforcement.
        """
        commands = {
            "curl": [
                "curl",
                "--noproxy",
                "*",
                "--max-time",
                "3",
                "--connect-timeout",
                "2",
                "-I",
                "https://1.1.1.1/",
            ],
            "python": [
                "python3",
                "-c",
                "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('https://1.1.1.1/',timeout=2).read(1)",
            ],
            "dns": [
                "python3",
                "-c",
                "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(2); s.sendto(bytes.fromhex('123401000001000000000000076578616d706c6503636f6d0000010001'),('1.1.1.1',53)); s.recvfrom(512)",
            ],
        }
        before = await self._call(firewall_inspect) if firewall_inspect else None
        checks = {}
        for label, command in commands.items():
            result = await self._call(execute, command)
            checks[label + "_blocked"] = result.get("returncode") not in {None, 0}
        after = await self._call(firewall_inspect) if firewall_inspect else None
        checks["default_drop"] = bool(after and after.get("output_policy") == "DROP")
        checks["drop_counter_increased"] = bool(
            before and after and after.get("drop_packets", 0) > before.get("drop_packets", 0)
        )
        checks["harness_unprivileged"] = bool(after and after.get("harness_net_admin") is False)
        result = {
            "checks": checks,
            "passed": all(checks.values()),
            "profile": "fenced",
            "unverified": []
            if firewall_inspect
            else ["firewall_policy", "packet_drop_counters", "harness_capabilities"],
        }
        self.store.append_record(run_id, "preflight", test="fenced_egress", **result)
        return result

    def _artifact(self, reference):
        """Load instrument evidence only from world-private, content-hashed files."""
        from .util import digest

        if not isinstance(reference, (str, dict)):
            raise TypeError("An artifact reference is required")
        name = reference.get("path") if isinstance(reference, dict) else reference
        candidate = Path(name)
        root = self.store.data_dir.resolve()
        requested = candidate if candidate.is_absolute() else root / candidate
        path = requested.resolve()
        if (
            not requested.is_relative_to(root)
            or not path.is_relative_to(root)
            or any(
                part in {"work", "world", "work-snapshot"}
                for relative in (requested.relative_to(root), path.relative_to(root))
                for part in relative.parts
            )
        ):
            raise ValueError("Preflight evidence must be in a world-private instrument directory")
        raw = path.read_bytes()
        actual_hash = digest(raw)
        if (
            isinstance(reference, dict)
            and reference.get("hash")
            and reference["hash"] != actual_hash
        ):
            raise ValueError("Preflight evidence failed its content hash")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("observer") != "world":
            raise ValueError("Preflight evidence must identify its world-side observer")
        return value, actual_hash

    async def run(
        self,
        run_id: str,
        *,
        harness_environment=None,
        execute=None,
        firewall_inspect=None,
        metadata=None,
        evaluation_paths=None,
        t0_ref=None,
        run_kill=True,
    ) -> dict:
        """Compose actual checks; missing observations never become passing flags.

        Metadata keys ``egress``, ``provider_canary``, ``block_profile`` and
        ``versions`` reference private world-observer JSON artifacts. Their hashes
        are included in the report. An instrument callback may generate them, but
        arbitrary booleans in this API cannot substitute for observed evidence.
        """
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal, InvalidOperation

        from .util import canonical, digest, now

        run = self.store.get_run(run_id)
        config = run["config"]
        checks, artifacts = {}, {}

        def fresh(value, age):
            try:
                stamp = datetime.fromisoformat(value)
                return stamp.tzinfo is not None and timedelta(0) <= datetime.now(UTC) - stamp < age
            except (TypeError, ValueError):
                return False

        def mark(name, passed=None, detail=None):
            checks[name] = {
                "status": "unmeasured" if passed is None else "passed" if passed else "failed"
            }
            if detail:
                checks[name]["detail"] = detail

        packs = set(config.get("packs", []))
        services = getattr(self.world, "services", {})
        has_writes = bool(packs & {"mail", "social", "market", "vault", "browse"})
        mark(
            "rule_of_two",
            not has_writes or all(name in services for name in ("vault", "asks")),
            "Read-only by construction, or writes mediated by both delegation and approval services",
        )
        if "vault" in services:
            vault = services["vault"]
            try:
                if hasattr(vault, "list_accounts"):
                    accounts = vault.list_accounts()
                elif hasattr(vault, "database"):
                    with vault.database.lock:
                        aliases = [
                            row["alias"]
                            for row in vault.database.db.execute(
                                "SELECT alias FROM accounts WHERE state != 'revoked'"
                            )
                        ]
                    accounts = [vault.get_account(alias) for alias in aliases]
                else:
                    accounts = []
                for account in accounts:
                    vault._validate_terms(account["platform"], account["label"])
                mark(
                    "platform_terms",
                    True if accounts or not has_writes else None,
                    None if accounts or not has_writes else "No accounts inspected",
                )
            except (ValueError, KeyError, TypeError):
                mark("platform_terms", False, "A delegation lacks a current permitted terms review")
        else:
            mark("platform_terms", True if not has_writes else None)
        if config.get("life_id") and int(config.get("stage", 0)) >= 2:
            identity = services.get("identity")
            mark(
                "identity_and_binding",
                None if identity is None else bool(config.get("binding_id")),
                "An attributable identity and registered life binding are required",
            )
        else:
            mark("identity_and_binding", True)
        if execute is not None:
            script = (
                "import hashlib,json,os,ssl; from pathlib import Path; "
                "keys=['WORLD_RUN_ID','ANTHROPIC_BASE_URL','OPENAI_BASE_URL',"
                "'HTTP_PROXY','HTTPS_PROXY','SSL_CERT_FILE']; "
                "values={key:os.environ[key] for key in keys if key in os.environ}; "
                "path=Path(values.get('SSL_CERT_FILE','/nonexistent')); "
                "values['WORLD_CA_SHA256']=hashlib.sha256(ssl.PEM_cert_to_DER_cert("
                "path.read_text())).hexdigest() if path.is_file() else None; "
                "private=['WORLD_CONTROL_TOKEN','WORLD_OPENAI_API_KEY',"
                "'WORLD_ANTHROPIC_API_KEY','WORLD_BRAVE_API_KEY']; "
                "values['WORLD_PRIVATE_ENV_PRESENT']=any(os.environ.get(key) for key in private); "
                "print(json.dumps(values))"
            )
            try:
                observation = await self._call(execute, ["python3", "-c", script])
                if observation.get("returncode") != 0:
                    raise ValueError("Harness environment inspection failed")
                harness_environment = json.loads(observation.get("stdout", ""))
                if not isinstance(harness_environment, dict):
                    raise TypeError("Invalid environment inspection result")
            except (OSError, ValueError, TypeError):
                harness_environment = None
                mark("harness_environment", False, "Actual namespace inspection failed")
        if harness_environment is None and "harness_environment" not in checks:
            mark("harness_environment", None, "Actual harness environment has not been inspected")
        elif harness_environment is not None:
            mark("harness_environment", self.environment(run_id, harness_environment)["passed"])
        profile = config.get("isolation_profile", "cooperative")
        if profile == "fenced":
            if execute is None:
                mark("fenced_egress", None, "No executor inside the actual harness namespace")
            else:
                mark(
                    "fenced_egress",
                    (await self.isolation(run_id, execute, firewall_inspect=firewall_inspect))[
                        "passed"
                    ],
                )
        else:
            required = config.get("canary", "off") != "off" or config.get(
                "untrusted_harness", False
            )
            mark(
                "fenced_egress",
                not required,
                "Cooperative isolation is registered; completeness is audited after seal",
            )
        if run_kill and run["state"] in {"running", "preflight"}:
            original = run["state"]
            if original == "preflight":
                self.store.update_run(run_id, state="running")
            try:
                result = await self.kill_check(run_id)
                mark("kill_switch", result["passed"])
            except (ValueError, RuntimeError):
                mark("kill_switch", False, "The actual dry-run kill did not complete")
            finally:
                if original == "preflight" and self.store.get_run(run_id)["state"] in {
                    "running",
                    "frozen",
                }:
                    self.store.update_run(run_id, state="preflight")
        else:
            mark("kill_switch", None, "Dry-run kill has not executed")
        metadata = metadata or {}
        loaded = {}
        for label in ("egress", "provider_canary", "block_profile", "versions"):
            if label not in metadata:
                mark(label, None, "World-observed artifact is missing")
                continue
            try:
                loaded[label], artifacts[label] = self._artifact(metadata[label])
            except (OSError, ValueError, TypeError):
                mark(label, False, "World-observed artifact is invalid or inaccessible")
        if "egress" in loaded:
            value = loaded["egress"]
            mark(
                "egress",
                bool(value.get("ip") and value.get("reverse_dns"))
                and fresh(value.get("observed_at"), timedelta(days=1)),
                "Egress and reverse DNS must be observed within 24 hours",
            )
        if "provider_canary" in loaded:
            value = loaded["provider_canary"]
            try:
                priced = Decimal(str(value["metered_usd"]))
                reported = Decimal(str(value["provider_reported_usd"]))
                valid = (
                    value.get("model_snapshot_id") == config.get("model_snapshot_id")
                    and priced == reported
                    and priced > 0
                    and fresh(value.get("observed_at"), timedelta(days=1))
                    and value.get("price_table_id") == getattr(self.world, "price_table_id", None)
                )
                valid = valid and bool(
                    value.get("provider_response_hash")
                    and value.get("usage")
                    and value.get("price_table_id")
                )
                source_run = value.get("provider_run_id")
                source_call = value.get("call_id")
                try:
                    calls = self.store.records(source_run) if source_run else []
                    matching = [
                        record
                        for record in calls
                        if record.get("kind") == "model_call"
                        and record.get("call_id") == source_call
                    ]
                    valid = (
                        valid
                        and len(matching) == 1
                        and matching[0].get("usage") == value.get("usage")
                        and Decimal(str(matching[0].get("usd"))) == priced
                        and matching[0].get("price_table_id") == value.get("price_table_id")
                    )
                except (KeyError, ValueError, TypeError):
                    valid = False

                mark(
                    "provider_canary",
                    bool(valid),
                    "Actual provider usage and pinned-price metering must agree",
                )
            except (KeyError, ValueError, TypeError, InvalidOperation):
                mark("provider_canary", False, "Provider usage evidence is incomplete")
        if "block_profile" in loaded:
            value = loaded["block_profile"]
            try:
                rates = value["rates_by_domain_class"]
                valid = (
                    fresh(value.get("observed_at"), timedelta(days=7))
                    and int(value["url_count"]) == 300
                    and value.get("coverage") == 1
                    and value.get("status") == "measured"
                    and bool(rates)
                    and all(0 <= rate <= 1 for rate in rates.values())
                )
                valid = valid and value.get("browser_identity") == config.get(
                    "browser_identity", "unsigned"
                )
                service = services.get("canaries")
                registered = (
                    service.get_measurement(value["measurement_id"])
                    if service is not None and hasattr(service, "get_measurement")
                    else None
                )
                bank = (
                    service.get_bank(value["bank_id"])
                    if service is not None and hasattr(service, "get_bank")
                    else None
                )
                source = self.store.get_run(value["measurement_run_id"])
                valid = (
                    valid
                    and registered == value
                    and bank is not None
                    and bank.get("kind") == "block_profile"
                    and len(bank.get("entries", [])) == 300
                    and digest(bank) == value.get("bank_hash")
                    and source["state"] == "sealed"
                    and source["config"].get("mode") == "live"
                    and source["config"].get("browser_identity", "unsigned")
                    == config.get("browser_identity", "unsigned")
                    and bool(value.get("bank_id") and value.get("bank_hash"))
                    and (
                        not config.get("block_profile_id")
                        or value.get("block_profile_id") == config["block_profile_id"]
                    )
                )
                mark(
                    "block_profile",
                    bool(valid),
                    "A fixed 300-URL profile must be fresh for this identity",
                )
            except (KeyError, ValueError, TypeError):
                mark("block_profile", False, "Block-profile evidence is incomplete")
        if "versions" in loaded:
            value = loaded["versions"]
            required = {
                "extractor_id",
                "harness_id",
                "model_snapshot_id",
                "price_table_id",
                "search_response_date",
            }
            valid = required.issubset(value) and all(value[name] for name in required)
            valid = (
                valid
                and value.get("harness_id") == config.get("harness_id")
                and value.get("model_snapshot_id") == config.get("model_snapshot_id")
                and value.get("price_table_id") == getattr(self.world, "price_table_id", None)
                and value.get("extractor_id") == config.get("tuple", {}).get("extractor_id")
            )
            mark(
                "versions",
                bool(valid),
                "Observed harness, extractor, model, search date and pinned prices",
            )
        signed = config.get("browser_identity", "unsigned") == "signed"
        signer = getattr(self.world.gateway, "signer", None)
        mark(
            "signed_identity",
            not signed or signer is not None,
            "Signed identity requires an actual world-held Ed25519 key",
        )
        if execute is not None and evaluation_paths:
            script = (
                "import os,sys; sys.exit(1 if any(os.access(p,os.R_OK) for p in "
                + repr(list(evaluation_paths))
                + ") else 0)"
            )
            result = await self._call(execute, ["python3", "-c", script])
            mark("evaluation_isolation", result.get("returncode") == 0)
        else:
            mark(
                "evaluation_isolation",
                None,
                "Evaluation/probe paths have not been tested as the harness user",
            )
        if config.get("life_id"):
            try:
                value, artifacts["t0"] = self._artifact(t0_ref)
                mark(
                    "t0_battery",
                    bool(
                        value.get("completed")
                        and value.get("forked_context")
                        and value.get("fingerprint_ref")
                    ),
                )
            except (OSError, ValueError, TypeError):
                mark("t0_battery", None, "A completed forked t=0 battery is required before a life")
        else:
            mark("t0_battery", True)
        failures = [name for name, value in checks.items() if value["status"] == "failed"]
        unmeasured = [name for name, value in checks.items() if value["status"] == "unmeasured"]
        report = {
            "run_id": run_id,
            "created_at": now(),
            "config_hash": digest(config),
            "checks": checks,
            "failures": failures,
            "unmeasured": unmeasured,
            "artifact_hashes": artifacts,
            "passed": not failures and not unmeasured,
        }
        report["report_hash"] = digest(report)
        directory = self.store.run_dir(run_id) / "preflight"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory / "report.json").write_text(canonical(report), encoding="utf-8")
        self.store.append_record(
            run_id,
            "preflight",
            test="full_preflight",
            report_hash=report["report_hash"],
            config_hash=report["config_hash"],
            passed=report["passed"],
            failures=failures,
            unmeasured=unmeasured,
        )
        return report

    async def kill_check(self, run_id: str) -> dict:
        started = time.monotonic()
        await self.world.freeze(run_id)
        reply = await self.world.act(run_id, "fetch", {"url": "https://example.com/"})
        proxy = getattr(self.world, "proxy", None) or getattr(self.world, "services", {}).get(
            "proxy"
        )
        checks = {
            "proxy_listener_active": bool(
                proxy is not None and proxy.server is not None and proxy.server.is_serving()
            ),
            "tool_admission_closed": reply.get("rule_id") == "run_state",
            "proxy_sockets_closed": bool(proxy is not None and not proxy.connections.get(run_id)),
            "lane_tasks_closed": False,
        }
        lane = getattr(self.world, "lane", None)
        if lane is not None and hasattr(lane, "active_count"):
            checks["lane_tasks_closed"] = lane.active_count(run_id) == 0
        elapsed = time.monotonic() - started
        checks["within_two_seconds"] = elapsed <= 2
        await self.world.resume(run_id)
        result = {"checks": checks, "elapsed_s": elapsed, "passed": all(checks.values())}
        self.store.append_record(run_id, "control_plane_audit", test="dry_run_kill", **result)
        return result

    @staticmethod
    async def _call(callback, *args):
        result = callback(*args)
        return await result if inspect.isawaitable(result) else result

    async def _sentinel(self):
        sentinel = self.store.data_dir / "kill-switch"
        while True:
            if sentinel.is_file() and not sentinel.is_symlink():
                text = sentinel.read_text(encoding="utf-8").strip()
                if text == "*":
                    ids = [
                        run["run_id"]
                        for run in self.store.list_runs()
                        if run["state"] in {"running", "frozen"}
                    ]
                else:
                    ids = [
                        value
                        for value in text.splitlines()
                        if re.fullmatch(r"r_[A-Za-z0-9_-]+", value)
                    ]
                for run_id in ids:
                    try:
                        await self.world.end_run(run_id, reason="operator_stop", truncated=True)
                    except KeyError:
                        continue
                sentinel.unlink(missing_ok=True)
            await asyncio.sleep(2)

    async def recover(self):
        if self.sentinel_enabled and (self.monitor is None or self.monitor.done()):
            self.monitor = asyncio.create_task(self._sentinel())

    async def aclose(self):
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
