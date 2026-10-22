"""Native openenv-core Environment and typed websocket client (optional SDK)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
from importlib.metadata import version
from pathlib import Path

import httpx
from openenv.core.client_types import StepResult
from openenv.core.env_client import EnvClient
from openenv.core.env_server.http_server import create_app
from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import Action, EnvironmentMetadata, Observation, State
from pydantic import Field

from .adapter import OpenEnvFace


class LandingPartyAction(Action):
    tool: str = Field(min_length=1)
    params: dict = Field(default_factory=dict)
    wait_seconds: float = Field(default=0, ge=0, le=60)


class LandingPartyObservation(Observation):
    reply: dict = Field(default_factory=dict)
    run_id: str | None = None
    terminated: bool = False
    truncated: bool = False


class LandingPartyState(State):
    world_state: dict = Field(default_factory=dict)


class LandingPartyEnvironment(
    Environment[LandingPartyAction, LandingPartyObservation, LandingPartyState]
):
    SUPPORTS_CONCURRENT_SESSIONS = True

    def __init__(
        self,
        *,
        world_url="http://127.0.0.1:7777",
        agent_url="http://127.0.0.1:7778",
        config=None,
        control_token=None,
        replay_scorer=None,
    ):
        super().__init__()
        self.config, self.steps = dict(config or {}), 0
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.closed = False
        self._call(self._initialize(world_url, agent_url, control_token, replay_scorer))

    async def _initialize(self, world_url, agent_url, control_token, replay_scorer):
        token = control_token or os.environ.get("WORLD_CONTROL_TOKEN")
        self.control = httpx.AsyncClient(
            base_url=world_url,
            timeout=70,
            trust_env=False,
            headers={"X-World-Control-Token": token} if token else {},
        )
        self.agent = httpx.AsyncClient(base_url=agent_url, timeout=70, trust_env=False)
        self.face = OpenEnvFace(
            self.control,
            agent_client=self.agent,
            replay_scorer=replay_scorer,
            harness_id="openenv-core@" + version("openenv-core") + "/submit-wait",
        )

    def _call(self, awaitable):
        if self.closed:
            awaitable.close()
            raise RuntimeError("The OpenEnv session is closed")
        return asyncio.run_coroutine_threadsafe(awaitable, self.loop).result(timeout=80)

    @staticmethod
    def _observation(result):
        return LandingPartyObservation(
            reply=result["observation"],
            run_id=result["info"]["run_id"],
            done=result["terminated"] or result["truncated"],
            terminated=result["terminated"],
            truncated=result["truncated"],
            reward=result["reward"],
            metadata=result["info"],
        )

    def reset(self, seed=None, episode_id=None, **kwargs):
        if episode_id is not None:
            raise ValueError(
                "An external episode id cannot resume or select a world run; reset creates a new run"
            )
        config = {**self.config, **kwargs.pop("config", {})}
        if kwargs:
            raise ValueError("Only an explicit world config is supported at reset")
        if seed is not None:
            config["seed"] = seed
        result = self._call(self.face.reset(config))
        self.steps = 0
        return self._observation(result)

    async def reset_async(self, seed=None, episode_id=None, **kwargs):
        return await asyncio.to_thread(self.reset, seed, episode_id, **kwargs)

    def step(self, action, timeout_s=None, **kwargs):
        if kwargs:
            raise ValueError("Unregistered step parameters are not supported")
        action = LandingPartyAction.model_validate(action)
        wait = (
            action.wait_seconds
            if timeout_s is None
            else min(action.wait_seconds, max(0, timeout_s))
        )
        result = self._call(self.face.step(action.tool, action.params, wait_seconds=wait))
        self.steps += 1
        return self._observation(result)

    async def step_async(self, action, timeout_s=None, **kwargs):
        return await asyncio.to_thread(self.step, action, timeout_s, **kwargs)

    @property
    def state(self):
        state = self._call(self.face.state()) if self.face.run_id else {}
        return LandingPartyState(
            episode_id=self.face.run_id, step_count=self.steps, world_state=state
        )

    def get_metadata(self):
        return EnvironmentMetadata(
            name="landing-party",
            version=version("openenv-core"),
            description="Reset creates a new run in the same external world. Step submits and optionally waits; tickets are valid observations. Live reward is None.",
        )

    async def _close(self):
        if self.face.run_id:
            state = await self.face.state()
            if state["state"] in {"running", "frozen", "stopping"}:
                response = await self.control.post(
                    "/v1/beam-up", json={"run_id": self.face.run_id, "reason": "harness_stopped"}
                )
                response.raise_for_status()
        await self.control.aclose()
        await self.agent.aclose()

    def close(self):
        if not self.closed:
            try:
                self._call(self._close())
            finally:
                self.closed = True
                self.loop.call_soon_threadsafe(self.loop.stop)
                self.thread.join(timeout=5)
                self.loop.close()


class LandingPartyClient(EnvClient[LandingPartyAction, LandingPartyObservation, LandingPartyState]):
    def _step_payload(self, action):
        return LandingPartyAction.model_validate(action).model_dump()

    def _parse_result(self, payload):
        return StepResult(
            observation=LandingPartyObservation.model_validate(payload["observation"]),
            reward=payload.get("reward"),
            done=payload.get("done", False),
        )

    def _parse_state(self, payload):
        return LandingPartyState.model_validate(payload)


def create_native_app(**kwargs):
    return create_app(
        lambda: LandingPartyEnvironment(**kwargs),
        LandingPartyAction,
        LandingPartyObservation,
        env_name="landing-party",
        max_concurrent_envs=8,
    )


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--world-url", default="http://127.0.0.1:7777")
    parser.add_argument("--agent-url", default="http://127.0.0.1:7778")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(
        create_native_app(
            world_url=args.world_url,
            agent_url=args.agent_url,
            config=json.loads(args.config.read_text()),
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
