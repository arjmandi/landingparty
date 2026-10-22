"""Deterministic loop alerts and bounded emergency-stop sentinel polling."""

import asyncio

from .util import digest


def semantic_reply(value):
    """Ignore delivery IDs, clocks and accounting that change between identical outcomes."""
    if isinstance(value, dict):
        if value.get("hash"):
            return {
                key: value.get(key)
                for key in ("kind", "hash", "status", "final_url", "url", "content_type")
            }
        volatile = {
            "action_id",
            "ticket_id",
            "event_id",
            "spent_after",
            "ts",
            "handle",
            "path",
            "elapsed_s",
            "observed_at",
            "wall_left_s",
            "in_flight",
            "inbox_pending",
            "spent",
            "reserved",
        }
        return {key: semantic_reply(item) for key, item in value.items() if key not in volatile}
    if isinstance(value, list):
        return [semantic_reply(item) for item in value]
    return value


class BreakerService:
    def __init__(self, world):
        self.world = world
        self.task = None

    def observe(self, run_id, action_id, reply):
        store = self.world.store
        run = store.get_run(run_id)
        if run["state"] != "running":
            return
        policy = run["config"].get("breaker_policy", {})
        records = store.records(run_id)
        actions = {r["action_id"]: r for r in records if r["kind"] == "action"}
        outcomes = [r for r in records if r["kind"] == "outcome" and r.get("world_action", True)]

        def signature(record):
            action = actions.get(record.get("action_id"), {})
            return digest(
                [action.get("tool"), action.get("params_hash"), semantic_reply(record.get("reply"))]
            )

        recent = outcomes[-20:]
        signatures = [signature(record) for record in recent]
        current = signatures[-1] if signatures else None
        consecutive = 0
        for item in reversed(signatures):
            if item != current:
                break
            consecutive += 1
        yellow = int(policy.get("yellow_identical", 3))
        red = int(policy.get("red_identical", 8))
        if consecutive == yellow:
            store.append_record(
                run_id,
                "probe",
                probe_type="loop",
                action_id=action_id,
                signature=current,
                consecutive=consecutive,
            )
        no_content = int(policy.get("no_new_content_actions", 100))
        seen = set()
        novel = []
        for outcome in outcomes:
            resource = outcome.get("resource") or outcome.get("reply") or {}
            hash_ = resource.get("hash")
            novel.append(bool(hash_ and hash_ not in seen))
            if hash_:
                seen.add(hash_)
        exhausted_content = len(novel) >= no_content and not any(novel[-no_content:])
        if (current and signatures.count(current) >= red) or exhausted_content:
            store.append_record(
                run_id, "intervention", reason="agent_loop", action_id=action_id, signature=current
            )
            asyncio.create_task(self._freeze(run_id))

    async def _freeze(self, run_id):
        if self.world.store.get_run(run_id)["state"] == "running":
            await self.world.freeze(run_id)

    async def _poll(self):
        while True:
            for run in self.world.store.list_runs():
                if run["state"] in {"running", "frozen"}:
                    sentinel = self.world.store.run_dir(run["run_id"]) / "KILL"
                    if sentinel.is_file():
                        await self.world.end_run(
                            run["run_id"], reason="operator_stop", truncated=True
                        )
            await asyncio.sleep(2)

    async def recover(self):
        if self.task is None:
            self.task = asyncio.create_task(self._poll())

    async def aclose(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
