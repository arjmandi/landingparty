from datetime import UTC, datetime, timedelta

from worldd.asks import AskService
from worldd.storage import Store
from worldd.vault import VaultService


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class CapabilityWorld:
    def __init__(self, path, clock=None):
        self.store = Store(path)
        self.services = {}
        self.clock = clock or Clock()
        self.services["asks"] = AskService(self, clock=self.clock)
        self.services["vault"] = VaultService(self, clock=self.clock)

    def push(self, run_id, event):
        return self.store.push_event(run_id, event)

    def run(self, **config):
        return self.store.create_run(
            {
                "budget": {"usd": 20, "wall_s": 7200},
                "stage": 2,
                "reply_policy": "blockers_only",
                "identity_id": "identity_fixture",
                **config,
            }
        )["run_id"]

    def provision(self, run_id=None, alias="fixture", **changes):
        vault = self.services["vault"]
        vault.review_terms(
            "fixture",
            "permits_labelled",
            source_url="https://fixture.test/terms",
            origin_url="https://fixture.test",
        )
        values = {
            "alias": alias,
            "platform": "fixture",
            "identity_id": "identity_fixture",
            "kind": "api_key",
            "label": "bot_labelled",
            "principal": "researcher_fixture",
            "run_id": run_id,
            "credential": {"api_key": "fixture-secret-value"},
            "scope_classes": ["read", "message", "post", "purchase", "settings"],
            "scope_rules": {
                key: ["https://fixture.test/"]
                for key in ["read", "message", "post", "purchase", "settings"]
            },
        }
        return vault.provision(**{**values, **changes})

    def close(self):
        database = getattr(self, "_capability_database", None)
        if database:
            database.db.close()
        self.store.close()


async def activate_browser_grant(world, run_id, *, prefix="/", max_uses=None):
    """Run signed K state transitions with an injected archived rehearsal executor."""
    from worldd.grants import GrantService
    from worldd.storage import content_hash

    async def fixture_proxy(grant_id, action, *, run_id, archive_in):
        return {
            "mode": "rehearsal",
            "passed": True,
            "proxy_id": "injected-archive-fixture",
            "archive_id": archive_in,
            "record_ref": "fixture:" + grant_id,
            "request_hash": content_hash(action),
            "response_hash": content_hash({"fixture": True}),
        }

    grants = GrantService(world, clock=world.clock, rehearsal_executor=fixture_proxy)
    world.services["grants"] = grants
    pending = grants.grant(
        run_id,
        packs=["browse"],
        action_classes=["read"],
        platform="fixture",
        origins=["https://fixture.test"],
        path_prefixes=[prefix],
        purpose="Read the approved fixture documents",
        max_uses=max_uses,
    )
    for _ in range(10):
        reply = await grants.rehearse(
            pending["grant_id"],
            {"action_class": "read", "platform": "fixture", "url": "https://fixture.test" + prefix},
        )
        assert reply["kind"] == "result"
    grants.activate(
        pending["grant_id"],
        {
            "decision": "pass",
            "reviewer": "fixture_operator",
            "rehearsal_hash": grants.rehearsal_hash(pending["grant_id"]),
        },
    )
    return grants, pending["grant_id"]
