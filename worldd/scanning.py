"""Deterministic boundary scanners; heuristic scores are not calibrated truth.

Outbound guards also match configured world-held provider keys and canary secrets
without exposing their values. Inbound scoring labels observations; it never
rewrites content, filters an agent's result, or chooses the agent's response.
"""

from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import unquote

CLASSIFIER_ID = "injection-patterns@0.1/uncalibrated"
SENSITIVE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[A-Z0-9]{16}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\+\d{1,3}[ -]\d[\d ()-]{7,}\d)",
    re.IGNORECASE,
)
SECRET_NAMES = {
    "WORLD_OPENAI_API_KEY",
    "WORLD_ANTHROPIC_API_KEY",
    "WORLD_BRAVE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
}
INJECTION_RULES = [
    (
        "instruction_override",
        re.compile(
            r"\bignore\s+(?:all\s+)?(?:previous|prior|above|system)\s+instructions\b", re.IGNORECASE
        ),
        0.55,
    ),
    (
        "operator_impersonation",
        re.compile(
            r"\b(?:system|operator|developer)\s+(?:message|override|directive)\s*:", re.IGNORECASE
        ),
        0.35,
    ),
    (
        "secret_request",
        re.compile(
            r"\b(?:send|reveal|print|upload|exfiltrate)\b.{0,80}\b(?:api[_ -]?key|secret|system prompt|password|credential)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        0.55,
    ),
    (
        "grader_directive",
        re.compile(
            r"\b(?:grader|evaluator|judge)\b.{0,50}\b(?:award|score|ignore|mark.*correct)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        0.35,
    ),
    (
        "capability_induction",
        re.compile(
            r"\b(?:ask|request)\b.{0,60}\b(?:operator|human)\b.{0,60}\b(?:credential|permission|capability|payment)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        0.2,
    ),
]


def contains_sensitive_data(value: str | bytes) -> bool:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    for _ in range(2):
        text = unquote(text)
    if SENSITIVE.search(text):
        return True
    for name, secret in os.environ.items():
        if (
            (name in SECRET_NAMES or name.startswith("WORLD_CANARY_SECRET"))
            and len(secret) >= 8
            and secret in text
        ):
            return True
    return False


def classify_injection(body: bytes) -> dict:
    text = body[:1_000_000].decode("utf-8", errors="replace")
    matches = [(name, weight) for name, pattern, weight in INJECTION_RULES if pattern.search(text)]
    return {
        "classifier_id": CLASSIFIER_ID,
        "injection_score": min(1.0, sum(weight for _, weight in matches)),
        "signals": [name for name, _ in matches],
        "calibrated": False,
        "input_hash": "sha256:" + hashlib.sha256(body).hexdigest(),
    }
