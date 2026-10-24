"""ASSAY's adapter for Landing Party: each registered action is one world tool.

The world starts ASSAY's daemon with `--adapter <this file>:factory`, so this runs
inside ASSAY's own interpreter, not the world's: it uses the standard library only.
The daemon's environment carries the run's capability (`WORLD_RUN_TOKEN`); the
player's never does, so the player reaches the world only through `assay act ...
--predict`. Predictions and reasoning stay in ASSAY's journal: the world receives
the tool and its parameters, as from any harness, and keeps no prediction.

`WORLD_ASSAY_TOOLS` names the world's mapping of registry actions to tools, written
with the registry: an action takes its parameters flat, as one `arguments` object, or
none, and a free-form object travels as JSON text the adapter parses back.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Paid world reads are waited for, so the act carries its result.
WAITED = {"search", "fetch", "fetch_many"}
# A result longer than this is kept whole in a file beside the run; the
# observation carries its head and the file's name.
RESULT_CHARS = 16000
# ASSAY's client waits about 90 seconds for an act; the world answers within that.
TIMEOUT_S = 85

try:  # ASSAY's interpreter has the contract's types; the world's own tests do not.
    from assay.adapters import SessionCapability
except ImportError:  # pragma: no cover - exercised only outside ASSAY's interpreter
    SessionCapability = None


def assay_budget(world_cap, margin="0.05") -> float:
    """ASSAY's dollar cap sits above the world's, so the world's binds first."""
    try:
        cap, extra = Decimal(str(world_cap)), Decimal(str(margin))
    except InvalidOperation:
        raise ValueError("Budgets must be finite positive numbers") from None
    if not cap.is_finite() or not extra.is_finite() or cap <= 0 or extra <= 0:
        raise ValueError("Budgets must be finite positive numbers")
    value = float(cap * (1 + extra))
    if not math.isfinite(value):
        raise ValueError("Budgets must be finite positive numbers")
    return value


class LandingParty:
    """One world session as ASSAY sees it: the observer and the actuator."""

    def __init__(self, root, tools, *, base_url, run_id, token, opener=urllib.request.urlopen):
        self.root = Path(root)
        self.tools = tools
        self.base_url = base_url.rstrip("/")
        self.run_id, self.token = run_id, token
        self.opener = opener
        self.steps = 0
        self._observation = self._observe("status", self._act("status", {}))

    @property
    def session(self):
        # Live world actions cannot be replayed: the run lives one daemon long.
        return SessionCapability(replayable=False) if SessionCapability else {"replayable": False}

    @property
    def observation(self):
        return self._observation

    def step(self, action, data, reasoning):
        del reasoning  # Predictions stay in ASSAY's journal; the world never sees them.
        spec = self.tools[action]
        self.steps += 1
        try:
            params = self._params(spec, data or {})
        except ValueError as error:
            self._observation = self._observe(spec["tool"], {"error": str(error)})
            return self._observation
        if spec["tool"] in WAITED:
            params["wait"] = True
        self._observation = self._observe(spec["tool"], self._act(spec["tool"], params))
        return self._observation

    @staticmethod
    def _params(spec, data):
        if spec["shape"] == "none":
            return {}
        params = dict(data.get("arguments") or {}) if spec["shape"] == "object" else dict(data)
        for name in spec.get("json_fields", []):
            if isinstance(params.get(name), str):
                try:
                    params[name] = json.loads(params[name])
                except ValueError:
                    raise ValueError(f"`{name}` must be JSON text") from None
        return params

    def _act(self, tool, params):
        body = json.dumps({"run_id": self.run_id, "tool": tool, "params": params}).encode()
        request = urllib.request.Request(
            self.base_url + "/v1/act",
            data=body,
            method="POST",
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with self.opener(request, timeout=TIMEOUT_S) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:2000]
            return {"error": {"status": error.code, "detail": detail}}
        except (urllib.error.URLError, OSError, ValueError) as error:
            return {"error": {"detail": str(error)[:500]}}

    def _observe(self, tool, result):
        text = json.dumps(result, ensure_ascii=False)
        data = {"tool": tool, "result": result}
        if len(text) > RESULT_CHARS:
            folder = self.root / "world-results"
            folder.mkdir(exist_ok=True)
            path = folder / f"{self.steps:04d}-{tool}.json"
            path.write_text(text)
            data = {"tool": tool, "result_head": text[:RESULT_CHARS], "result_file": str(path)}
        return {
            "state": "NOT_FINISHED",
            "levels_completed": 0,
            "win_levels": 0,
            "available_actions": list(self.tools),
            "data": data,
        }


def factory(root, config):
    del config  # Every condition the world sets reaches the daemon through its environment.
    return LandingParty(
        root,
        json.loads(Path(os.environ["WORLD_ASSAY_TOOLS"]).read_text()),
        base_url=os.environ["WORLD_BASE_URL"],
        run_id=os.environ["WORLD_RUN_ID"],
        token=os.environ["WORLD_RUN_TOKEN"],
    )
