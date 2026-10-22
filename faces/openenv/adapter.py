"""Gym-style HTTP face: reset starts a new run, and live reward is always None."""

from __future__ import annotations

import asyncio
import inspect
import os
import time

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field


class StepRequest(BaseModel):
    tool: str
    params: dict = Field(default_factory=dict)
    wait_seconds: float = Field(default=0, ge=0, le=60)


class OpenEnvFace:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        agent_client=None,
        replay_scorer=None,
        harness_id="openenv@0.1/submit-wait",
    ):
        self.client, self.replay_scorer = client, replay_scorer
        self.agent_client = agent_client or client
        self.run_id, self.mode, self.cursor = None, None, 0
        self.headers = {}
        self.harness_id = harness_id

    async def reset(self, config: dict):
        if self.run_id:
            current = await self.state()
            if current["state"] in {"running", "frozen"}:
                response = await self.client.post(
                    "/v1/beam-up", json={"run_id": self.run_id, "reason": "harness_stopped"}
                )
                response.raise_for_status()
        config = {**config, "harness_id": self.harness_id}
        response = await self.client.post("/v1/run", json=config)
        response.raise_for_status()
        run = response.json()
        self.headers = (
            {"Authorization": "Bearer " + run["run_token"]} if run.get("run_token") else {}
        )
        run = {key: value for key, value in run.items() if key != "run_token"}
        self.run_id, self.mode, self.cursor = run["run_id"], config.get("mode", "live"), 0
        return {
            "observation": run,
            "reward": None,
            "terminated": False,
            "truncated": False,
            "info": {
                "reset_semantics": "new_run; external internet is not reset",
                "run_id": self.run_id,
            },
        }

    async def state(self):
        if not self.run_id:
            raise ValueError("reset must create a run before state/step")
        response = await self.agent_client.get(
            "/v1/status", params={"run_id": self.run_id}, headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    async def step(self, tool, params=None, *, wait_seconds=0):
        if not self.run_id:
            raise ValueError("reset must create a run before step")
        response = await self.agent_client.post(
            "/v1/act",
            json={"run_id": self.run_id, "tool": tool, "params": params or {}},
            headers=self.headers,
        )
        response.raise_for_status()
        observation = response.json()
        deadline = time.monotonic() + max(0, min(60, wait_seconds))
        while observation.get("kind") == "ticket" and time.monotonic() < deadline:
            inbox = await self.agent_client.get(
                "/v1/inbox",
                params={
                    "run_id": self.run_id,
                    "since": self.cursor,
                    "wait_seconds": min(60, max(0, deadline - time.monotonic())),
                },
                headers=self.headers,
            )
            inbox.raise_for_status()
            batch = inbox.json()
            self.cursor = batch["cursor"]
            for event in batch["events"]:
                if event.get("type") == "job_done" and event.get("action_id") == observation.get(
                    "action_id"
                ):
                    observation = event["reply"]
                    break
            if not batch["events"]:
                break
        state = await self.state()
        reward = None
        if self.mode == "replay" and self.replay_scorer:
            reward = self.replay_scorer(self.run_id, observation, state)
            if inspect.isawaitable(reward):
                reward = await reward
        ended = state["state"] in {"sealed", "ended", "killed"}
        end = state.get("end") or {}
        truncated = ended and bool(end.get("truncated"))
        return {
            "observation": observation,
            "reward": reward,
            "terminated": ended and not truncated,
            "truncated": truncated,
            "info": {"run_id": self.run_id, "state": state},
        }

    async def manifest(self):
        if not self.run_id:
            raise ValueError("reset must create a run before requesting spaces")
        response = await self.agent_client.get(
            "/v1/manifest", params={"run_id": self.run_id}, headers=self.headers
        )
        response.raise_for_status()
        return {
            "action_space": response.json(),
            "observation_space": {"kind": ["result", "blocked", "ticket", "silence", "state"]},
        }


def create_openenv_app(
    world_url="http://127.0.0.1:7777",
    *,
    client=None,
    agent_client=None,
    agent_url="http://127.0.0.1:7778",
    replay_scorer=None,
):
    """Expose an isolated face session; it never grants new core capabilities."""
    from contextlib import asynccontextmanager

    owned = client is None
    client = client or httpx.AsyncClient(
        base_url=world_url,
        timeout=65,
        trust_env=False,
        headers={"X-World-Control-Token": os.environ["WORLD_CONTROL_TOKEN"]}
        if os.environ.get("WORLD_CONTROL_TOKEN")
        else {},
    )
    own_agent = agent_client is None and owned
    agent_client = agent_client or (
        httpx.AsyncClient(base_url=agent_url, timeout=65, trust_env=False) if owned else client
    )
    face = OpenEnvFace(client, agent_client=agent_client, replay_scorer=replay_scorer)
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owned:
            await client.aclose()
        if own_agent:
            await agent_client.aclose()

    app = FastAPI(title="Landing Party OpenEnv face", lifespan=lifespan)
    app.state.face = face

    @app.post("/reset")
    async def reset(config: dict):
        async with lock:
            return await face.reset(config)

    @app.post("/step")
    async def step(body: StepRequest):
        async with lock:
            return await face.step(**body.model_dump())

    @app.get("/state")
    async def state():
        return await face.state()

    @app.get("/spaces")
    async def spaces():
        return await face.manifest()

    return app
