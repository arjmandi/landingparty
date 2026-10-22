"""Thin HTTP mapping for an ASSAY service-archetype runner.

This module does not require or pretend to implement the ASSAY runner itself.
Its caller owns ASSAY's prediction and scoring policies.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import httpx

PAID_ACTIONS = frozenset({"search", "fetch"})
FREE_READS = frozenset({"inbox", "status", "extract", "beliefs"})


def assay_budget(world_cap: float | str | Decimal, margin: float | str = "0.05") -> float:
    cap, extra = Decimal(str(world_cap)), Decimal(str(margin))
    if not cap.is_finite() or not extra.is_finite() or cap <= 0 or extra <= 0:
        raise ValueError("world cap and ASSAY margin must be finite and positive")
    return float(cap * (1 + extra))


class AssayAdapter:
    def __init__(self, client: httpx.AsyncClient, run_id: str, *, run_token=None):
        self.client, self.run_id = client, run_id
        token = run_token or os.environ.get("WORLD_RUN_TOKEN")
        self.headers = {"Authorization": "Bearer " + token} if token else {}

    async def act(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        if tool in PAID_ACTIONS:
            params["wait"] = True
        # Ticket and Blocked remain observations. A slow call is never silently resubmitted.
        response = await self.client.post(
            "/v1/act",
            json={"run_id": self.run_id, "tool": tool, "params": params},
            headers=self.headers,
        )
        response.raise_for_status()
        return response.json()

    async def manifest(self) -> dict[str, Any]:
        response = await self.client.get(
            "/v1/manifest", params={"run_id": self.run_id}, headers=self.headers
        )
        response.raise_for_status()
        return response.json()
