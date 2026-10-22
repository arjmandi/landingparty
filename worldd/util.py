"""Small deterministic serialization helpers."""

import hashlib
import json
import uuid
from datetime import UTC, datetime


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value) -> str:
    body = value if isinstance(value, bytes) else canonical(value).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def blocked(kind: str, detail: str, rule_id: str, **extra) -> dict:
    return {"kind": "blocked", "type": kind, "detail": detail, "rule_id": rule_id, **extra}
