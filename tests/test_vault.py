import json

import pytest
from capability_helpers import CapabilityWorld


@pytest.fixture
def world(tmp_path):
    result = CapabilityWorld(tmp_path)
    yield result
    result.close()


async def test_fill_only_lease_is_exact_single_use_and_secrets_encrypted(world):
    run_id = world.run()
    account = world.provision(run_id)
    vault = world.services["vault"]
    request = {"url": "https://fixture.test/doc", "method": "GET"}
    result = await vault.authorize(run_id, "a_read", "fixture", "read", request)
    assert "fixture-secret-value" not in json.dumps(result)
    lease = result["lease"]["lease_id"]
    with pytest.raises(ValueError, match="exact"):
        vault.consume(
            lease, run_id, "a_read", "read", {**request, "url": "https://fixture.test/else"}
        )
    assert (
        vault.consume(lease, run_id, "a_read", "read", request)["api_key"] == "fixture-secret-value"
    )
    with pytest.raises(ValueError, match="used"):
        vault.consume(lease, run_id, "a_read", "read", request)
    raw = vault.database.db.execute(
        "SELECT encrypted FROM accounts WHERE account_id=?", (account["account_id"],)
    ).fetchone()[0]
    assert b"fixture-secret-value" not in raw
    assert "fixture-secret-value" not in json.dumps(world.store.records(run_id))
    assert world.store.verify_record(run_id)["valid"]


def test_forbidden_and_unreviewed_platforms_cannot_be_provisioned(world):
    vault = world.services["vault"]
    for platform in ["youtube", "github", "unknown"]:
        with pytest.raises(ValueError, match="terms|review"):
            vault.provision(
                alias=platform,
                platform=platform,
                identity_id="identity_fixture",
                kind="api_key",
                label="bot_labelled",
                principal="fixture",
                credential={"api_key": "x"},
            )


def test_label_requirement_and_identity_attribution(world):
    vault = world.services["vault"]
    vault.review_terms("fixture", "permits_labelled", source_url="https://fixture.test/terms")
    with pytest.raises(ValueError, match="labelled"):
        vault.provision(
            alias="fixture",
            platform="fixture",
            identity_id="fixture",
            kind="api_key",
            label="human_registered",
            principal="principal",
            credential={"api_key": "secret"},
        )


async def test_scope_origin_identity_and_stage_cannot_be_overridden(world):
    run_id = world.run()
    world.provision(run_id)
    vault = world.services["vault"]
    for scope, url in [("read", "https://other.test/"), ("unknown", "https://fixture.test/")]:
        result = await vault.authorize(run_id, "a_out", "fixture", scope, {"url": url})
        assert result["kind"] == "blocked"
    other = world.run(identity_id="someone_else")
    assert (
        await vault.authorize(other, "a_other", "fixture", "read", {"url": "https://fixture.test/"})
    )["kind"] == "blocked"
    stage0 = world.run(stage=0)
    assert (
        await vault.authorize(stage0, "a_zero", "fixture", "read", {"url": "https://fixture.test/"})
    )["rule_id"] == "stage_gate"


async def test_outside_scope_approval_does_not_itself_expand_delegation(world):
    run_id = world.run()
    world.provision(run_id, scope_classes=["read"], scope_rules={"read": ["https://fixture.test/"]})
    vault, asks = world.services["vault"], world.services["asks"]
    request = {"url": "https://fixture.test/settings", "body": {"name": "Fixture"}}
    pending = await vault.authorize(run_id, "a_settings", "fixture", "settings", request)
    assert pending["kind"] == "ticket" and asks.get(pending["ticket_id"])["kind"] == "approval"
    asks.answer(pending["ticket_id"], "approve")
    assert (await vault.authorize(run_id, "a_retry", "fixture", "settings", request))[
        "kind"
    ] == "ticket"
    vault.update_scope(
        "fixture",
        run_id=run_id,
        scope_classes=["read", "settings"],
        scope_rules={scope: ["https://fixture.test/"] for scope in ["read", "settings"]},
    )
    assert (await vault.authorize(run_id, "a_after_amendment", "fixture", "settings", request))[
        "kind"
    ] == "result"
    with pytest.raises(ValueError, match="escape"):
        vault.update_scope(
            "fixture",
            run_id=run_id,
            scope_classes=["read"],
            scope_rules={"read": ["https://other.test/"]},
        )


async def test_actual_accounts_held_driver_tracks_same_identity_provision_revoke(world):
    from worldd.identity import IdentityService
    from worldd.lives import LifeService

    lives, identities = LifeService(world, clock=world.clock), IdentityService(world)
    world.services.update(lives=lives, identity=identities)
    try:
        life = lives.create(
            {
                "stage": 2,
                "budget": {"usd_life": 20},
                "driver": {"metric": "accounts_held", "reading_at_start": 1, "floor": -1},
            }
        )
        identity = identities.create(
            life_id=life["life_id"],
            principal="researcher_fixture",
            name="Fixture agent",
            mailbox="fixture@example.test",
            homepage="https://fixture.test/agent",
        )
        config = lives.prepare_session(life["life_id"], {"budget": {"usd": 20, "wall_s": 7200}})
        run_id = world.store.create_run(config)["run_id"]
        lives.attach_session(life["life_id"], run_id)
        world.provision(run_id, identity_id=identity["identity_id"])
        assert lives.get(life["life_id"])["driver"]["reading"] == 1
        world.provision(run_id, alias="second", identity_id=identity["identity_id"])
        assert lives.get(life["life_id"])["driver"]["reading"] == 2
        world.services["vault"].revoke("fixture")
        assert lives.get(life["life_id"])["driver"]["reading"] == 1
        world.services["vault"].replace_credential("fixture", {"api_key": "replacement-private"})
        assert lives.get(life["life_id"])["driver"]["reading"] == 2
        mutation = (
            world.services["vault"]
            .database.db.execute(
                "SELECT payload,signature FROM vault_mutations ORDER BY rowid DESC LIMIT 1"
            )
            .fetchone()
        )
        assert (
            world.services["vault"].database.sign(json.loads(mutation["payload"]))
            == mutation["signature"]
        )
    finally:
        await lives.aclose()
        await identities.aclose()


async def test_purchase_and_first_contact_require_exact_approval(world):
    run_id = world.run()
    world.provision(run_id)
    vault, asks = world.services["vault"], world.services["asks"]
    request = {"url": "https://fixture.test/send", "recipient": "new@example.test", "body": "hello"}
    pending = await vault.authorize(run_id, "a_send", "fixture", "message", request)
    assert pending["kind"] == "ticket"
    asks.answer(pending["ticket_id"], "approve", ask_class="approval")
    swapped = await vault.authorize(
        run_id, "a_swap", "fixture", "message", {**request, "body": "changed"}
    )
    assert swapped["kind"] == "ticket"
    approved = await vault.authorize(run_id, "a_retry", "fixture", "message", request)
    assert approved["kind"] == "result"
    vault.consume(approved["lease"]["lease_id"], run_id, "a_retry", "message", request)
    vault.record_contact("fixture", counterparty="new@example.test")
    assert (
        await vault.authorize(
            run_id, "a_again", "fixture", "message", {**request, "body": "second"}
        )
    )["kind"] == "result"
    assert (
        await vault.authorize(
            run_id,
            "a_pay",
            "fixture",
            "purchase",
            {"url": "https://fixture.test/pay", "amount": "1.00"},
        )
    )["kind"] == "ticket"


async def test_lease_expiration_revocation_and_terms_expiry(world):
    run_id = world.run()
    world.provision(run_id)
    vault = world.services["vault"]
    request = {"url": "https://fixture.test/doc"}
    result = await vault.authorize(run_id, "a_read", "fixture", "read", request)
    world.clock.advance(2 * 3600 + 1)
    with pytest.raises(ValueError, match="expired"):
        vault.consume(result["lease"]["lease_id"], run_id, "a_read", "read", request)
    fresh = await vault.authorize(run_id, "a_fresh", "fixture", "read", request)
    vault.revoke("fixture")
    with pytest.raises(ValueError, match="revoked|unavailable"):
        vault.consume(fresh["lease"]["lease_id"], run_id, "a_fresh", "read", request)


def test_scope_prefix_does_not_escape_path_segment(world):
    world.provision()
    vault = world.services["vault"]
    account = vault.get_account("fixture")
    account["scope_rules"]["purchase"] = ["https://fixture.test/pay"]
    assert vault.request_within_scope(account, "purchase", {"url": "https://fixture.test/pay/item"})
    assert not vault.request_within_scope(
        account, "purchase", {"url": "https://fixture.test/pay-attacker"}
    )
    for url in [
        "https://fixture.test/pay/../private",
        "https://fixture.test/pay/%2e%2e/private",
        "https://fixture.test/pay/%252e%252e/private",
    ]:
        assert not vault.request_within_scope(account, "purchase", {"url": url})


async def test_credentials_survive_reopen_and_wrong_key_is_not_exposed(world):
    run_id = world.run()
    world.provision(run_id)
    from worldd.vault import VaultService

    replacement = VaultService(world, clock=world.clock)
    request = {"url": "https://fixture.test/doc"}
    result = await replacement.authorize(run_id, "a_read", "fixture", "read", request)
    assert (
        replacement.consume(result["lease"]["lease_id"], run_id, "a_read", "read", request)[
            "api_key"
        ]
        == "fixture-secret-value"
    )


def test_vault_catalogue_is_registered_policy(world):
    world.provision()
    assert world.services["vault"].catalogue(world.run()) == []
    assert len(world.services["vault"].catalogue(world.run(vault_mode="catalogue"))) == 1


async def test_identity_registered_after_run_creation_is_resolved_without_crossrun_leak(world):
    from worldd.identity import IdentityService

    identities = IdentityService(world)
    world.services["identity"] = identities
    first = world.run(identity_id=None)
    second = world.run(identity_id=None)
    identity = identities.create(
        principal="principal_fixture",
        name="Fixture",
        mailbox="agent@example.test",
        homepage="https://fixture.test/agent",
        run_id=first,
    )
    vault = world.services["vault"]
    vault.review_terms("fixture", "permits_labelled", source_url="https://fixture.test/terms")
    vault.provision(
        alias="registered",
        platform="fixture",
        identity_id=identity["identity_id"],
        kind="api_key",
        label="bot_labelled",
        principal="principal_fixture",
        credential={"api_key": "private"},
        origin_url="https://fixture.test",
    )
    request = {"url": "https://fixture.test/doc"}
    assert (await vault.authorize(first, "a_owner", "registered", "read", request))[
        "kind"
    ] == "result"
    assert (await vault.authorize(second, "a_other", "registered", "read", request))[
        "kind"
    ] == "blocked"
    identities.create(
        principal="principal_fixture",
        name="Other",
        mailbox="other@example.test",
        homepage="https://fixture.test/other",
        run_id=second,
    )
    assert (await vault.authorize(second, "a_other2", "registered", "read", request))[
        "kind"
    ] == "blocked"
    await identities.aclose()


async def test_supplemental_grant_enforces_destination_and_consumes_once(world):
    from capability_helpers import activate_browser_grant

    run_id = world.run(packs=["vault"])
    world.provision(run_id)
    vault = world.services["vault"]
    grants, grant_id = await activate_browser_grant(world, run_id, prefix="/docs", max_uses=1)
    denied = await vault.authorize(
        run_id,
        "a_outside",
        "fixture",
        "read",
        {"url": "https://fixture.test/settings", "tool": "goto"},
    )
    assert denied["kind"] == "blocked"
    request = {"url": "https://fixture.test/docs/one", "tool": "goto"}
    allowed = await vault.authorize(run_id, "a_read", "fixture", "read", request)
    assert allowed["lease"]["grant_id"] == grant_id
    assert (
        grants.authorize_used(
            run_id,
            "a_read",
            "read",
            platform="fixture",
            url=request["url"],
            action_hash=allowed["lease"]["request_hash"],
            grant_id=grant_id,
        )["kind"]
        == "result"
    )
    assert (await vault.authorize(run_id, "a_again", "fixture", "read", request))[
        "kind"
    ] == "blocked"
    assert grants.verify(run_id)["valid"]
    await grants.aclose()


def test_briefing_obeys_registered_disclosure_and_identity_scope(world):
    world.provision()
    vault = world.services["vault"]
    assert vault.briefing("identity_fixture") == []
    assert vault.briefing("identity_fixture", "on_ask") == []
    assert vault.briefing("another_identity", "catalogue") == []
    assert vault.briefing("identity_fixture", "catalogue") == [
        {"alias": "fixture", "platform": "fixture", "state": "ready"}
    ]
    assert "fixture-secret-value" not in json.dumps(vault.briefing("identity_fixture", "catalogue"))
