"""Passive Inspect/verifiers-compatible readers; unmeasured never becomes zero."""

import json
from pathlib import Path


def code_scorer(report):
    if isinstance(report, (str, Path)):
        report = json.loads(Path(report).read_text())
    return {
        "run_id": report.get("run_id"),
        "columns": report.get("scores", report.get("columns", {})),
        "integrity": report.get("integrity", {}),
        "reward": None,
        "explanation": "No numeric reward is inferred from missing, judged, or self-reported values.",
    }


def inspect_scorer():
    """Construct the native scorer when the optional inspect-ai SDK is installed."""
    from inspect_ai.scorer import Score, scorer

    @scorer(metrics=[])
    def landing_party_report():
        async def score(state, target):
            path = state.metadata.get("landing_party_report")
            if not path:
                return Score(value={}, explanation="unmeasured: no sealed episode report provided")
            report = code_scorer(path)
            return Score(value={}, explanation="Passive sealed-episode report", metadata=report)

        return score

    return landing_party_report()
