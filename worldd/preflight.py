"""Executable preflight checks and the operator's two-second kill sentinel."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .preflight_probes import (
    EVALUATION_PATHS,
    environment_command,
    evaluation_command,
    isolation_commands,
    surface_command,
)


class PreflightService:
    def __init__(self, world, *, sentinel_enabled=False):
        self.sentinel_enabled = sentinel_enabled
        self.world = world
        self.store = world.store
        self.monitor = None
        self.harnesses = {}
        self._host_bridge = None
        self._source_observer = None

    def host_challenge(
        self, run_id, observer_public_key, container_id, firewall_id, *, evaluation_paths=()
    ):
        if self._host_bridge is None:
            from .host_observer import HostObserverBridge

            self._host_bridge = HostObserverBridge(self)
        return self._host_bridge.challenge(
            run_id,
            observer_public_key,
            container_id,
            firewall_id,
            evaluation_paths=evaluation_paths,
        )

    async def host_submit(
        self,
        run_id,
        challenge_id,
        receipt,
        signature,
        *,
        metadata=None,
        sources=None,
        t0_ref=None,
        run_kill=True,
    ):
        if self._host_bridge is None:
            raise ValueError("Issue a host observer challenge first")
        return await self._host_bridge.submit(
            run_id,
            challenge_id,
            receipt,
            signature,
            metadata=metadata,
            sources=sources,
            t0_ref=t0_ref,
            run_kill=run_kill,
        )

    async def observe_sources(
        self,
        run_id,
        *,
        provider_run_id=None,
        provider_call_id=None,
        search_run_id=None,
        search_action_id=None,
        egress_url=None,
    ):
        if self._source_observer is None:
            from .preflight_sources import SourceObserver

            self._source_observer = SourceObserver(self)
        return await self._source_observer.observe(
            run_id,
            provider_run_id=provider_run_id,
            provider_call_id=provider_call_id,
            search_run_id=search_run_id,
            search_action_id=search_action_id,
            egress_url=egress_url,
        )

    def register_harness(self, run_id, *, execute, firewall_inspect=None, evaluation_paths=()):
        """Trusted Python integration; callbacks are never deserialized from JSON."""
        self.store.get_run(run_id)
        if not callable(execute) or (
            firewall_inspect is not None and not callable(firewall_inspect)
        ):
            raise TypeError("Harness observers must be trusted callables")
        self.harnesses[run_id] = {
            "execute": execute,
            "firewall_inspect": firewall_inspect,
            "evaluation_paths": list(dict.fromkeys([*EVALUATION_PATHS, *evaluation_paths])),
        }
        return {"run_id": run_id, "registered": True}

    async def bind_container(self, run_id, container_id, firewall_id, *, evaluation_paths=()):
        """Control-only Docker binding makes /preflight/run usable with JSON refs."""
        from .isolation import DockerHarness

        harness = await DockerHarness.bind(
            run_id, container_id, firewall_id, data_dir=self.store.data_dir
        )
        self.register_harness(
            run_id,
            execute=harness.execute,
            firewall_inspect=harness.firewall_inspect,
            evaluation_paths=evaluation_paths,
        )
        self.harnesses[run_id]["validate"] = harness.validate
        result = {
            "run_id": run_id,
            "container_id": harness.agent_id,
            "firewall_id": harness.fence_id,
        }
        self.store.append_record(
            run_id,
            "control_plane_audit",
            test="harness_binding",
            **{k: v for k, v in result.items() if k != "run_id"},
        )
        return result

    @staticmethod
    def _tree_manifest(root):
        """Hash a visible tree, preserving only safe internal links as metadata."""
        from .workspaces import snapshot_manifest, verify_import

        root = Path(root)
        if root.resolve() != root or root.is_symlink():
            raise ValueError("Agent surfaces must not be symlinks")
        if not root.exists():
            return None
        if not root.is_dir():
            raise ValueError("Agent surface must be a directory")
        manifest = snapshot_manifest(root)
        verify_import(root, manifest)
        entries = manifest["entries"]
        return {
            "files": {
                name: {"sha256": entry["sha256"], "executable": entry["executable"]}
                for name, entry in entries.items()
                if entry["type"] == "file"
            },
            "directories": [
                name for name, entry in entries.items() if entry["type"] == "directory"
            ],
            "links": {
                name: entry["target"]
                for name, entry in entries.items()
                if entry["type"] == "symlink"
            },
        }

    async def workspace(self, run_id, execute):
        """Compare iota, the trusted run tree, and actual harness-visible bytes."""
        from .util import digest

        run = self.store.get_run(run_id)
        plan = run["config"].get("iota", {}).get("workspace", "fresh")
        if plan == "fresh":
            expected = {"files": {}, "directories": ["out"], "links": {}}
        else:
            from .workspaces import verify_import

            match = re.fullmatch(r"import\((r_[A-Za-z0-9_-]+)\)", plan)
            if not match:
                raise ValueError("Workspace iota must name a sealed donor")
            source_id = match[1]
            source = self.store.get_run(source_id)
            records = self.store.records(source_id)
            if (
                source["state"] not in {"sealed", "killed", "ended"}
                or not self.store.verify_record(source_id)["valid"]
                or not records
                or records[-1].get("kind") != "seal"
            ):
                raise ValueError("Workspace donor must retain its verified seal")
            snapshot = self.store.run_dir(source_id) / "work-snapshot"
            anchors = [record for record in records if record.get("kind") == "workspace_snapshot"]
            if len(anchors) != 1:
                raise ValueError("Workspace donor needs its exact sealed snapshot anchor")
            verify_import(snapshot, anchors[0])
            expected = self._tree_manifest(snapshot)
            if expected is None:
                raise ValueError("Workspace donor snapshot is missing")
        actual = self._tree_manifest(self.store.run_dir(run_id) / "work")
        beliefs = self._tree_manifest(self.store.run_dir(run_id) / "world/beliefs")
        # This program is run by the registered unprivileged harness executor.
        # Only names, sizes-independent hashes and directory shape cross back.
        command = surface_command()
        observation = await self._call(execute, command)
        if observation.get("returncode"):
            raise ValueError("Harness workspace inspection failed")
        visible = json.loads(observation.get("stdout", ""))
        checks = {
            "workspace_matches_iota": actual == expected,
            "harness_workspace_matches": visible.get("work") == actual,
            "harness_beliefs_match": visible.get("beliefs") == beliefs,
        }
        result = {
            "passed": all(checks.values()),
            "checks": checks,
            "workspace_hash": digest(actual),
            "beliefs_hash": digest(beliefs),
        }
        self.store.append_record(run_id, "preflight", test="initial_surfaces", **result)
        return result

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
        commands = isolation_commands()
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
        if after is not None and "rules_reviewed" in after:
            checks["reviewed_firewall_rules"] = after["rules_reviewed"] is True
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
        proxy = services.get("proxy")
        if proxy is not None and hasattr(proxy, "prepare_run"):
            proxy.prepare_run(run_id)
        binding = self.harnesses.get(run_id)
        if binding:
            if binding.get("validate"):
                await binding["validate"]()
            execute = execute or binding["execute"]
            firewall_inspect = firewall_inspect or binding.get("firewall_inspect")
            evaluation_paths = evaluation_paths or binding.get("evaluation_paths")
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
            command = environment_command()
            try:
                observation = await self._call(execute, command)
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
                if config.get("research_mode") == "research" and label != "block_profile":
                    from .preflight_sources import SourceObserver

                    observer = self._source_observer or SourceObserver(self)
                    if loaded[label].get(
                        "observer_source"
                    ) != "world-preflight-sources@1" or not observer.registered(
                        run_id, label, loaded[label]
                    ):
                        loaded.pop(label)
                        mark(
                            label,
                            False,
                            "Research requires an actual registered world source observation",
                        )
            except (OSError, ValueError, TypeError):
                mark(label, False, "World-observed artifact is invalid or inaccessible")
        if "egress" in loaded:
            value = loaded["egress"]
            mark(
                "egress",
                None
                if value.get("status") == "unmeasured"
                else bool(value.get("ip") and value.get("reverse_dns"))
                and fresh(value.get("observed_at"), timedelta(days=1)),
                "Egress and reverse DNS must be observed within 24 hours",
            )
        if "provider_canary" in loaded:
            value = loaded["provider_canary"]
            try:
                priced = Decimal(str(value["metered_usd"]))
                reported = Decimal(
                    str(value.get("provider_usage_price_usd", value.get("provider_reported_usd")))
                )
                valid = (
                    value.get("model_snapshot_id") == config.get("model_snapshot_id")
                    and priced == reported
                    and priced > 0
                    and fresh(
                        value.get("source_observed_at", value.get("observed_at")), timedelta(days=1)
                    )
                    and value.get("price_table_id") == getattr(self.world, "price_table_id", None)
                )
                valid = valid and bool(
                    value.get("provider_response_hash")
                    and value.get("usage")
                    and value.get("price_table_id")
                )
                if value.get("observer_source") == "world-preflight-sources@1":
                    valid = (
                        valid
                        and self._source_observer is not None
                        and self._source_observer.registered(run_id, "provider_canary", value)
                        and value.get("first_request_no_hosted_tools") is True
                    )
                    if config.get("research_mode") == "research":
                        valid = (
                            valid
                            and value.get("fixture_transport") is False
                            and value.get("upstream_origin")
                            in {"https://api.openai.com", "https://api.anthropic.com"}
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
                        and matching[0].get("model") == config.get("model_snapshot_id")
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
                mark(
                    "provider_canary",
                    None if value.get("status") == "unmeasured" else False,
                    "Provider usage evidence is incomplete",
                )
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
            signer = getattr(self.world.gateway, "signer", None)
            signed = config.get("browser_identity", "unsigned") == "signed"
            browser_required = "browse" in packs
            valid = valid and {"browser_build", "signing_key_id"}.issubset(value)
            valid = valid and value.get("signing_key_id") == (
                signer.key_id if signed and signer else None
            )
            valid = valid and (not signed or signer is not None)
            if browser_required or value.get("observer_source") == "world-preflight-sources@1":
                from .preflight_sources import SourceObserver

                observer = self._source_observer or SourceObserver(self)
                actual_browser = await observer.browser_build(run_id)
                valid = valid and observer.registered(run_id, "versions", value)
                valid = valid and value.get("browser_build") == actual_browser["build"]
                valid = valid and (not browser_required or actual_browser["status"] == "measured")
                harness_observation = value.get("harness_observation")
                valid = valid and bool(
                    harness_observation
                    and harness_observation.get("declared_id") == config.get("harness_id")
                    and harness_observation.get("observed_version")
                )
                valid = (
                    valid
                    and fresh(value.get("observed_at"), timedelta(days=1))
                    and fresh(value.get("search_response_date"), timedelta(days=1))
                )
                if config.get("research_mode") == "research":
                    valid = valid and not (value.get("search_observation") or {}).get(
                        "fixture_transport"
                    )
            mark(
                "versions",
                None if value.get("status") == "partial" else bool(valid),
                "Observed harness, extractor, model, search date, browser build, signing key and pinned prices",
            )
        signed = config.get("browser_identity", "unsigned") == "signed"
        signer = getattr(self.world.gateway, "signer", None)
        mark(
            "signed_identity",
            not signed or signer is not None,
            "Signed identity requires an actual world-held Ed25519 key",
        )
        if execute is not None and evaluation_paths:
            command = evaluation_command(evaluation_paths)
            result = await self._call(execute, command)
            mark("evaluation_isolation", result.get("returncode") == 0)
        else:
            mark(
                "evaluation_isolation",
                None,
                "Evaluation/probe paths have not been tested as the harness user",
            )
        if execute is None:
            mark(
                "initial_surfaces",
                None,
                "The actual harness workspace/beliefs have not been inspected",
            )
        else:
            try:
                surfaces = await self.workspace(run_id, execute)
                mark("initial_surfaces", surfaces["passed"])
                artifacts["workspace"] = surfaces["workspace_hash"]
                artifacts["beliefs"] = surfaces["beliefs_hash"]
            except (OSError, ValueError, TypeError, KeyError):
                mark(
                    "initial_surfaces",
                    False,
                    "Workspace or beliefs differ from their registered initial surfaces",
                )
        if config.get("life_id"):
            try:
                from .storage import content_hash

                service = services["lives"]
                life = service.get(config["life_id"])
                path = self.store.data_dir / "lives" / life["life_id"] / "t0.json"
                if path.resolve() != path or not path.is_file():
                    raise ValueError("The registered life artifact is unavailable")
                value = json.loads(path.read_text())
                fingerprint = content_hash(value)
                if t0_ref is not None:
                    reference = t0_ref.get("path") if isinstance(t0_ref, dict) else t0_ref
                    if reference != fingerprint:
                        reference_path = Path(reference)
                        if not reference_path.is_absolute():
                            reference_path = self.store.data_dir / reference_path
                        if reference_path != path:
                            raise ValueError(
                                "The t=0 reference must name the registered life artifact"
                            )
                    if isinstance(t0_ref, dict) and t0_ref.get("hash", fingerprint) not in {
                        fingerprint,
                        digest(path.read_bytes()),
                    }:
                        raise ValueError("The t=0 reference hash differs")
                valid = (
                    value.get("study") == "t0"
                    and value.get("completed") is True
                    and value.get("life_id") == life["life_id"]
                    and fingerprint == life.get("t0_fingerprint_ref")
                    and value.get("model_snapshot_id") == config.get("model_snapshot_id")
                    and (
                        config.get("research_mode") != "research" or value.get("scientific") is True
                    )
                )
                children = value.get("measurement_run_ids", [])
                valid = valid and bool(children) and len(children) == len(set(children))
                for child_id in children:
                    child = self.store.get_run(child_id)
                    valid = (
                        valid
                        and child["state"] == "sealed"
                        and not child.get("tainted")
                        and child["config"].get("metering_lane") == "probe"
                        and child["config"].get("model_snapshot_id")
                        == value.get("model_snapshot_id")
                        and self.store.verify_record(child_id)["valid"]
                        and self.store.reconcile(child_id)["valid"]
                    )
                artifacts["t0"] = fingerprint
                mark(
                    "t0_battery",
                    bool(valid),
                    "The registered immutable life battery and sealed measurement children",
                )
                driver = life.get("driver")
                start = driver.get("reading_at_start") if driver else None
                valid_start = not driver or (
                    isinstance(start, (int, float))
                    and float("-inf") < start < float("inf")
                    and (config.get("driver") or {}).get("reading_at_start", start) == start
                )
                mark(
                    "driver_reading_at_start",
                    bool(valid_start),
                    "The life's registered initial driver reading",
                )
            except (OSError, ValueError, TypeError, KeyError):
                mark("t0_battery", False, "A valid registered t=0 life artifact is required")
                mark(
                    "driver_reading_at_start",
                    None,
                    "The registered life driver could not be inspected",
                )
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
        if binding and binding.get("host_observer"):
            report["host_observer"] = binding["host_observer"]
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
