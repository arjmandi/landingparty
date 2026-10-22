"""Constrained archive retention: preserve hashes/text, expire registered raw data.

Only world-owned network captures can be registered. Workspaces, run records,
belief logs, vaults, and files outside a run can never be deleted by this service.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .storage import canonical_json


class RetentionService:
    def __init__(self, world, *, clock=None, days: int = 90):
        if not isinstance(days, int) or not 1 <= days <= 90:
            raise ValueError("Retention must be between one and ninety days")
        self.world = world
        self.store = world.store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.days = days
        self._task = None
        with self.store._lock:
            self.store.db.execute(
                "CREATE TABLE IF NOT EXISTS retained_resources ("
                "run_id TEXT NOT NULL REFERENCES runs(run_id),path TEXT NOT NULL,"
                "payload TEXT NOT NULL,PRIMARY KEY(run_id,path))"
            )

    def _path(self, run_id: str, value: str | Path) -> Path:
        root = self.store.run_dir(run_id)
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        relative = path.relative_to(root)
        if not relative.parts:
            raise ValueError("The run root cannot expire")
        allowed = (
            relative.parts[:2] == ("world", "fetched")
            or relative.parts[0] == "traces"
            or relative.parts[0] == "private-responses"
            or relative.as_posix() in {"archive.warc.gz", "archive.har"}
        )
        if (
            not allowed
            or path.resolve() != path
            or not path.resolve().is_relative_to(root.resolve())
        ):
            raise ValueError("Only nonsymlink world-owned network captures may expire")
        if ".." in relative.parts:
            raise ValueError("Resource path escapes the capture directory")
        return path

    @staticmethod
    def _timestamp(value):
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.tzinfo is None:
            raise ValueError("Retention timestamps must include a timezone")
        return parsed.astimezone(UTC)

    def register(
        self,
        run_id: str,
        path: str | Path,
        *,
        retention_until: str | None = None,
        content_type: str | None = None,
    ) -> dict:
        self.store.get_run(run_id)
        target = self._path(run_id, path)
        if not target.is_file():
            raise ValueError("Retention resources must be existing regular files")
        created = self._timestamp(self.clock())
        expires = created + timedelta(days=self.days)
        if retention_until:
            expires = min(expires, self._timestamp(retention_until))
        relative = str(target.relative_to(self.store.run_dir(run_id)))
        with self.store._transaction():
            existing = self.store.db.execute(
                "SELECT payload FROM retained_resources WHERE run_id=? AND path=?",
                (run_id, relative),
            ).fetchone()
            if existing:
                resource = json.loads(existing[0])
                if expires < self._timestamp(resource["retention_until"]):
                    resource["retention_until"] = expires.isoformat()
            else:
                resource = {
                    "run_id": run_id,
                    "path": relative,
                    "registered_at": created.isoformat(),
                    "retention_until": expires.isoformat(),
                    "content_type": content_type,
                    "state": "raw",
                    "hash": None,
                    "text_path": None,
                }
            self.store.db.execute(
                "INSERT INTO retained_resources VALUES(?,?,?) "
                "ON CONFLICT(run_id,path) DO UPDATE SET payload=excluded.payload",
                (run_id, relative, canonical_json(resource)),
            )
        return resource

    def register_run(self, run_id: str) -> list[dict]:
        """Register supported captures already produced by this world's gateway."""
        root = self.store.run_dir(run_id)
        candidates = [root / "archive.warc.gz", root / "archive.har"]
        for folder in (root / "world" / "fetched", root / "traces", root / "private-responses"):
            if folder.is_dir() and not folder.is_symlink():
                candidates.extend(path for path in folder.rglob("*") if path.is_file())
        return [self.register(run_id, path) for path in candidates if path.is_file()]

    def resources(self, run_id: str | None = None) -> list[dict]:
        with self.store._lock:
            if run_id is None:
                rows = self.store.db.execute(
                    "SELECT payload FROM retained_resources ORDER BY run_id,path"
                )
            else:
                self.store.get_run(run_id)
                rows = self.store.db.execute(
                    "SELECT payload FROM retained_resources WHERE run_id=? ORDER BY path", (run_id,)
                )
            return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _text(body: bytes, content_type: str | None, name: str) -> str:
        if content_type and "html" in content_type or name.endswith((".html", ".htm")):
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(body, "html.parser")
            for node in soup(["script", "style", "noscript"]):
                node.decompose()
            return soup.get_text("\n", strip=True)
        textual = (content_type or "").startswith("text/") or name.endswith(
            (".txt", ".md", ".json", ".xml", ".csv")
        )
        return body.decode("utf-8", errors="replace") if textual else ""

    def _retain_text(self, target, resource, destination):
        if target.suffix.lower() == ".pdf":
            import pymupdf

            from .perception import _PDF_LOCK

            with _PDF_LOCK, pymupdf.open(target) as document:
                destination.write_text(
                    "\n".join(page.get_text() for page in document), encoding="utf-8"
                )
        elif target.name.endswith(".warc.gz"):
            from warcio.archiveiterator import ArchiveIterator

            with target.open("rb") as stream, destination.open("w", encoding="utf-8") as output:
                for record in ArchiveIterator(stream):
                    if record.rec_type != "response":
                        continue
                    body = record.content_stream().read(8 * 1024 * 1024)
                    media = (
                        record.http_headers.get_header("Content-Type")
                        if record.http_headers
                        else None
                    )
                    output.write(
                        canonical_json(
                            {
                                "url": record.rec_headers.get_header("WARC-Target-URI"),
                                "hash": "sha256:" + hashlib.sha256(body).hexdigest(),
                                "text": self._text(body, media, ""),
                            }
                        )
                        + "\n"
                    )
        elif target.name == "archive.har":
            har = json.loads(target.read_text(encoding="utf-8"))
            transactions = har.get("log", {}).get("entries", []) if isinstance(har, dict) else har
            destination.write_text(
                canonical_json(
                    {
                        "transactions": [
                            {
                                "url": item.get("request", {}).get("url"),
                                "status": item.get("response", {}).get("status"),
                                "startedDateTime": item.get("startedDateTime"),
                            }
                            for item in transactions
                        ]
                    }
                ),
                encoding="utf-8",
            )
        else:
            body = target.read_bytes()
            destination.write_text(
                self._text(body, resource["content_type"], target.name), encoding="utf-8"
            )

    def sweep(self) -> dict:
        now = self._timestamp(self.clock())
        outcome = {"pruned": [], "missing": [], "failed": []}
        for resource in self.resources():
            if (
                resource["state"] not in {"raw", "expiring"}
                or self._timestamp(resource["retention_until"]) > now
            ):
                continue
            run_id = resource["run_id"]
            try:
                target = self._path(run_id, resource["path"])
                if not target.is_file():
                    if resource["state"] == "expiring" and resource["hash"]:
                        resource.update(state="expired", expired_at=now.isoformat())
                        outcome["pruned"].append(
                            {"run_id": run_id, "path": resource["path"], "hash": resource["hash"]}
                        )
                    else:
                        resource.update(state="missing", expired_at=now.isoformat())
                        outcome["missing"].append({"run_id": run_id, "path": resource["path"]})
                else:
                    digest = hashlib.sha256()
                    with target.open("rb") as source:
                        while block := source.read(1024 * 1024):
                            digest.update(block)
                    retained = self.store.run_dir(run_id) / "retained"
                    if retained.is_symlink():
                        raise ValueError("Retained evidence must stay within its run")
                    retained.mkdir(exist_ok=True, mode=0o700)
                    text = retained / (digest.hexdigest() + ".txt")
                    if text.is_symlink():
                        raise ValueError("Retained text must not be a symlink")
                    self._retain_text(target, resource, text)
                    text.chmod(0o600)
                    resource.update(
                        hash="sha256:" + digest.hexdigest(),
                        bytes=target.stat().st_size,
                        text_path=str(text.relative_to(self.store.run_dir(run_id))),
                        expired_at=now.isoformat(),
                        state="expiring",
                    )
                    # Commit the retained evidence manifest before deleting any raw bytes.
                    with self.store._transaction():
                        self.store.db.execute(
                            "UPDATE retained_resources SET payload=? WHERE run_id=? AND path=?",
                            (canonical_json(resource), run_id, resource["path"]),
                        )
                    # Revalidate immediately before deleting a capture; never follow a changed link.
                    self._path(run_id, target).unlink()
                    resource["state"] = "expired"
                    outcome["pruned"].append(
                        {"run_id": run_id, "path": resource["path"], "hash": resource["hash"]}
                    )
                with self.store._transaction():
                    self.store.db.execute(
                        "UPDATE retained_resources SET payload=? WHERE run_id=? AND path=?",
                        (canonical_json(resource), run_id, resource["path"]),
                    )
            except (OSError, ValueError, TypeError, ImportError):
                outcome["failed"].append({"run_id": run_id, "path": resource["path"]})
        outcome["services"] = {}
        for name in ("communications", "perception"):
            service = getattr(self.world, "services", {}).get(name)
            if service and hasattr(service, "prune_expired"):
                try:
                    outcome["services"][name] = service.prune_expired(now)
                except (OSError, ValueError, TypeError):
                    outcome["services"][name] = {"state": "failed"}
        return outcome

    async def recover(self):
        for run in self.store.list_runs():
            self.register_run(run["run_id"])
        self.sweep()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            await asyncio.sleep(3600)
            for run in self.store.list_runs():
                self.register_run(run["run_id"])
            self.sweep()

    async def aclose(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
