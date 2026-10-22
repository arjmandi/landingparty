"""Pinned local retrieval and frozen captures: no network fallback exists here."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import mimetypes
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from bs4 import BeautifulSoup

from .beliefs import _canonical, _stamp, _time
from .gateway import canonical_url
from .util import blocked, digest


def _text(body: bytes, content_type: str) -> str:
    if not (
        content_type.startswith("text/") or any(kind in content_type for kind in ("json", "xml"))
    ):
        return ""
    decoded = body.decode("utf-8", errors="replace")
    if "html" in content_type:
        parsed = BeautifulSoup(decoded, "html.parser")
        for element in parsed(["script", "style", "noscript"]):
            element.decompose()
        decoded = parsed.get_text(" ", strip=True)
    return decoded[:200000]


def _fts_query(query: str) -> str:
    tokens = re.findall(r"\w+", query, re.UNICODE)[:128]
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


class ReplayIndex:
    """FTS5 retrieval over archived search snippets and verified fetched bodies."""

    def __init__(self, world):
        self.world, self.store = world, world.store
        self.path = Path(self.store.data_dir) / "replay.sqlite3"
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS replay_fts USING fts5(
                scope UNINDEXED, url UNINDEXED, title, body,
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TABLE IF NOT EXISTS replay_manifests (
                scope TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS replay_queries (
                scope TEXT NOT NULL, query_key TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(scope,query_key)
            );
            CREATE TABLE IF NOT EXISTS frozen_corpora (
                corpus_id TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
            """
        )
        self.path.chmod(0o600)

    def _save(self, scope, documents, queries, manifest):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("DELETE FROM replay_fts WHERE scope=?", (scope,))
                self.db.execute("DELETE FROM replay_queries WHERE scope=?", (scope,))
                for document in sorted(documents.values(), key=lambda item: item["url"]):
                    self.db.execute(
                        "INSERT INTO replay_fts(scope,url,title,body) VALUES(?,?,?,?)",
                        (
                            scope,
                            document["url"],
                            document.get("title", ""),
                            document.get("body", ""),
                        ),
                    )
                for ordinal, query in enumerate(queries):
                    self.db.execute(
                        "INSERT OR REPLACE INTO replay_queries VALUES(?,?,?)",
                        (
                            scope,
                            digest({"ordinal": ordinal, "params": query["params"]}),
                            _canonical(query),
                        ),
                    )
                self.db.execute(
                    "INSERT OR REPLACE INTO replay_manifests VALUES(?,?)",
                    (scope, _canonical(manifest)),
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def indexable(self, run_id: str) -> bool:
        """Exclude private ancestry through both measurements and replay archives."""
        visiting, visited = set(), set()
        pending = [(run_id, False)]
        while pending:
            candidate, leaving = pending.pop()
            if not isinstance(candidate, str) or not candidate:
                return False
            if leaving:
                visiting.remove(candidate)
                visited.add(candidate)
                continue
            if candidate in visiting:
                return False
            if candidate in visited:
                continue
            try:
                run = self.store.get_run(candidate)
            except (KeyError, ValueError, TypeError):
                return False
            config = run["config"]
            if not isinstance(config, dict):
                return False
            if (
                config.get("canary", "off") != "off"
                or config.get("private_archive")
                or config.get("world_accounting")
                or run.get("private_archive")
                or run.get("world_accounting")
            ):
                return False
            visiting.add(candidate)
            pending.append((candidate, True))
            for key in ("measurement_of", "archive_in"):
                if config.get(key) is not None:
                    pending.append((config[key], False))
        return True

    def build(self, run_id: str) -> dict:
        if not self.indexable(run_id):
            raise ValueError(
                "private or canary archive ancestry cannot enter the shared replay index"
            )
        verification = self.store.verify_record(run_id)
        if not verification["valid"]:
            raise ValueError("cannot index an episode with a broken record chain")
        root = self.store.run_dir(run_id)
        actions = {
            record["action_id"]: record
            for record in self.store.records(run_id)
            if record["kind"] == "action" and record.get("tool") == "search"
        }
        documents, queries, inputs, errors = {}, [], [], []
        for outcome in self.store.records(run_id):
            if outcome["kind"] != "outcome" or outcome.get("action_id") not in actions:
                continue
            reply = outcome.get("reply", {})
            if reply.get("kind") != "result":
                continue
            params = actions[outcome["action_id"]].get("params", {})
            query = {
                "params": {key: params.get(key) for key in ("query", "k", "freshness", "site")},
                "items": reply.get("items", []),
            }
            query["params"]["k"] = query["params"].get("k") or 10
            queries.append(query)
            inputs.append({"search_reply": digest(reply), "params": query["params"]})
            for item in reply.get("items", []):
                if not isinstance(item.get("url"), str):
                    continue
                documents.setdefault(
                    canonical_url(item["url"]),
                    {
                        "url": item["url"],
                        "title": item.get("title", ""),
                        "body": item.get("snippet", ""),
                    },
                )
        index_path = root / "archive-index.json"
        if index_path.is_file() and not index_path.is_symlink():
            archive = json.loads(index_path.read_text())
            for key, capture in sorted(archive.items()):
                reply = capture.get("reply", {})
                if reply.get("kind") != "result":
                    continue
                relative = Path(capture.get("body_path") or "")
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or relative.parts[:2] != ("world", "fetched")
                ):
                    errors.append({"key": key, "reason": "invalid_resource_path"})
                    continue
                path = root / relative
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    errors.append({"key": key, "reason": "resource_path_escape"})
                    continue
                try:
                    body = path.read_bytes()
                except OSError:
                    errors.append({"key": key, "reason": "resource_missing"})
                    continue
                if digest(body) != reply.get("hash"):
                    errors.append({"key": key, "reason": "resource_hash_mismatch"})
                    continue
                url = reply.get("final_url") or reply.get("url")
                if not isinstance(url, str):
                    errors.append({"key": key, "reason": "missing_url"})
                    continue
                canonical = canonical_url(url)
                prior = documents.get(canonical, {})
                title = prior.get("title", "")
                content_type = reply.get("content_type", "application/octet-stream")
                if not title and "html" in content_type:
                    parsed = BeautifulSoup(body, "html.parser")
                    title = parsed.title.get_text(" ", strip=True) if parsed.title else ""
                documents[canonical] = {
                    "url": url,
                    "title": title,
                    "body": _text(body, content_type),
                }
                inputs.append({"url": url, "hash": digest(body)})
        manifest = {
            "archive_id": run_id,
            "documents": len(documents),
            "queries": len(queries),
            "input_hash": digest(inputs),
            "errors": errors,
            "index_id": "fts5-unicode61@1",
            "observed_at": _stamp(datetime.now(UTC)),
            "record_verification": verification,
        }
        self._save(run_id, documents, queries, manifest)
        manifest["fidelity"] = self.fidelity(run_id)
        with self.lock:
            self.db.execute(
                "UPDATE replay_manifests SET payload=? WHERE scope=?",
                (_canonical(manifest), run_id),
            )
        return manifest

    def search(self, archive_id: str, query: str, k=10, *, site=None, freshness=None) -> dict:
        if not isinstance(archive_id, str):
            return blocked("replay_miss", "Invalid archive identifier", "replay_archive")
        if archive_id.startswith("r_") and not self.indexable(archive_id):
            return blocked(
                "policy_deny",
                "Private archive ancestry is excluded from shared replay",
                "replay_private_archive",
            )
        if not isinstance(query, str) or not 1 <= k <= 20:
            return blocked("replay_miss", "Invalid local search request", "replay_query")
        with self.lock:
            exists = self.db.execute(
                "SELECT 1 FROM replay_manifests WHERE scope=?", (archive_id,)
            ).fetchone()
        if not exists:
            return blocked("replay_miss", "The selected archive has no local index", "replay_index")
        expression = _fts_query(query)
        if not expression:
            return blocked("replay_miss", "The query has no searchable terms", "replay_query")
        with self.lock:
            rows = self.db.execute(
                "SELECT url,title,body,bm25(replay_fts,0,0,4,1) AS relevance FROM replay_fts "
                "WHERE replay_fts MATCH ? AND scope=? ORDER BY relevance,url LIMIT 1000",
                (expression, archive_id),
            ).fetchall()
            if freshness:
                known = [
                    json.loads(row[0])
                    for row in self.db.execute(
                        "SELECT payload FROM replay_queries WHERE scope=?", (archive_id,)
                    )
                ]
                allowed = {
                    canonical_url(item["url"])
                    for saved in known
                    if saved["params"].get("query") == query
                    and saved["params"].get("freshness") == freshness
                    for item in saved["items"]
                }
                rows = [row for row in rows if canonical_url(row["url"]) in allowed]
        if site:
            site = site.lower().rstrip(".")
            rows = [
                row
                for row in rows
                if (urlsplit(row["url"]).hostname or "").lower() == site
                or (urlsplit(row["url"]).hostname or "").lower().endswith("." + site)
            ]
        items = [
            {
                "rank": index + 1,
                "url": row["url"],
                "title": row["title"],
                "snippet": row["body"][:500],
            }
            for index, row in enumerate(rows[:k])
        ]
        if not items:
            return blocked(
                "replay_miss", "No document in the pinned corpus matches the query", "replay_search"
            )
        return {
            "kind": "result",
            "provider_id": f"local@{archive_id}",
            "items": items,
            "index_id": "fts5-unicode61@1",
        }

    def fidelity(self, archive_id: str) -> dict:
        with self.lock:
            queries = [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT payload FROM replay_queries WHERE scope=? ORDER BY query_key",
                    (archive_id,),
                )
            ]
        results = []
        for saved in queries:
            expected = {canonical_url(item["url"]) for item in saved["items"][:10]}
            if not expected:
                continue
            params = saved["params"]
            local = self.search(
                archive_id,
                params["query"],
                10,
                site=params.get("site"),
                freshness=params.get("freshness"),
            )
            actual = {canonical_url(item["url"]) for item in local.get("items", [])}
            results.append(
                {
                    "query": params["query"],
                    "overlap_at_10": len(expected & actual) / len(expected),
                    "archived_count": len(expected),
                    "local_count": len(actual),
                }
            )
        if not results:
            return {"status": "unmeasured", "reason": "no_archived_nonempty_search_queries", "n": 0}
        return {
            "status": "measured",
            "metric": "overlap@10",
            "denominator": "archived_top_10_count",
            "mean": sum(item["overlap_at_10"] for item in results) / len(results),
            "n": len(results),
            "queries": results,
        }

    async def handle(self, run_id, action_id, tool, params):
        if tool != "search":
            return blocked("policy_deny", "ReplayIndex only handles search", "replay_tool")
        archive = self.store.get_run(run_id)["config"].get("archive_in")
        if not archive:
            return blocked("replay_miss", "No source archive selected", "replay_archive")
        with self.lock:
            present = self.db.execute(
                "SELECT 1 FROM replay_manifests WHERE scope=?", (archive,)
            ).fetchone()
        if not present:
            self.build(archive)
        return self.search(
            archive,
            params["query"],
            params.get("k", 10),
            site=params.get("site"),
            freshness=params.get("freshness"),
        )

    async def recover(self):
        return None

    async def aclose(self):
        self.db.close()


class FrozenCorpusService:
    """Researcher-pinned capture imports at or before T*, never live fallbacks."""

    def __init__(self, world):
        self.world, self.store = world, world.store
        self.index = getattr(world, "services", {}).get("replay") or ReplayIndex(world)
        self._owns_index = self.index is not getattr(world, "services", {}).get("replay")
        self.root = Path(self.store.data_dir) / "corpora"
        self.root.mkdir(mode=0o700, exist_ok=True)

    def import_corpus(self, manifest: dict, captures: dict) -> dict:
        manifest = copy.deepcopy(manifest)
        cutoff = _time(manifest["cutoff"])
        manifest["cutoff"] = _stamp(cutoff)
        if not isinstance(manifest.get("captures"), list) or not manifest["captures"]:
            raise ValueError("frozen corpus requires explicit captures")
        verified, seen = [], set()
        for capture in manifest["captures"]:
            entry = copy.deepcopy(capture)
            url = entry["url"]
            if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).hostname:
                raise ValueError("capture URLs must be HTTP(S)")
            key = canonical_url(url)
            if key in seen:
                raise ValueError("duplicate canonical capture URL")
            seen.add(key)
            captured_at = _time(entry["captured_at"])
            if captured_at > cutoff:
                raise ValueError("capture postdates the frozen cutoff")
            entry["captured_at"] = _stamp(captured_at)
            supplied = captures.get(url, captures.get(key))
            if isinstance(supplied, Path):
                if supplied.is_symlink():
                    raise ValueError("capture sources must not be symlinks")
                body = supplied.read_bytes()
            elif isinstance(supplied, bytes):
                body = supplied
            elif isinstance(supplied, dict) and isinstance(supplied.get("base64"), str):
                body = base64.b64decode(supplied["base64"], validate=True)
            else:
                raise ValueError("capture needs bytes, a trusted Path or {base64:...}")  # noqa: TRY004 - malformed import data.
            if len(body) > 5_000_000:
                raise ValueError("frozen capture exceeds five megabytes")
            if digest(body) != entry.get("hash"):
                raise ValueError("frozen capture hash mismatch")
            entry.setdefault("status", 200)
            entry.setdefault("content_type", "application/octet-stream")
            if (
                not isinstance(entry["status"], int)
                or isinstance(entry["status"], bool)
                or not 100 <= entry["status"] <= 599
            ):
                raise ValueError("frozen capture has an invalid HTTP status")
            if not isinstance(entry["content_type"], str) or not isinstance(
                entry.get("title", ""), str
            ):
                raise TypeError("capture content type and title must be text")
            provenance = entry.get("provenance", {})
            if not isinstance(provenance, dict):
                raise TypeError("capture provenance must be an object")
            score = provenance.get("injection_score")
            if score is not None and (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not 0 <= score <= 1
            ):
                raise ValueError("captured injection score must be a finite probability or null")
            headers = entry.get("headers_subset", {})
            if not isinstance(headers, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in headers.items()
            ):
                raise ValueError("captured headers must be a string mapping")
            entry["headers_subset"] = {
                key.lower(): value
                for key, value in headers.items()
                if key.lower()
                in {"content-type", "last-modified", "etag", "date", "content-length"}
            }
            verified.append((key, entry, body))
        manifest["captures"] = [entry for _, entry, _ in sorted(verified)]
        corpus_id = "corpus_" + digest(manifest).split(":", 1)[1]
        directory = self.root / corpus_id
        directory.mkdir(mode=0o700, exist_ok=True)
        documents = {}
        for key, entry, body in verified:
            target = directory / (entry["hash"].split(":", 1)[1] + ".bin")
            if target.exists() and (
                target.is_symlink() or digest(target.read_bytes()) != entry["hash"]
            ):
                raise ValueError("existing pinned corpus data failed integrity verification")
            if not target.exists():
                target.write_bytes(body)
                target.chmod(0o400)
            documents[key] = {
                "url": entry["url"],
                "title": entry.get("title", ""),
                "body": _text(body, entry["content_type"]),
            }
        public = {
            "corpus_id": corpus_id,
            "manifest_hash": digest(manifest),
            "manifest": manifest,
            "documents": len(documents),
            "cutoff": manifest["cutoff"],
        }
        queries = manifest.get("search_lists", [])
        self.index._save(corpus_id, documents, queries, public)
        with self.index.lock:
            self.index.db.execute(
                "INSERT OR REPLACE INTO frozen_corpora VALUES(?,?)", (corpus_id, _canonical(public))
            )
        (directory / "manifest.json").write_text(_canonical(public))
        return public

    def _kernel_body(self, run_id, reply):
        if isinstance(reply.get("body"), bytes):
            body = reply["body"]
        else:
            observation_run = reply.get("observation_run_id", run_id)
            path = (
                Path(reply["resource_path"])
                if reply.get("resource_path")
                else self.store.run_dir(observation_run) / reply["handle"].lstrip("/")
            )
            root = (self.store.run_dir(observation_run) / "world" / "fetched").resolve()
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("archive observation escaped fetched resources")
            body = path.read_bytes()
        if digest(body) != reply.get("hash"):
            raise ValueError("archive observation failed byte verification")
        return body

    async def from_wayback(
        self, run_id: str, urls: list[str], cutoff: str, *, search_lists=None
    ) -> dict:
        """Explicit researcher bootstrap from real CDX/capture reads before T*.

        Frozen misses never invoke this method. All traffic uses the ordinary
        kernel gateway; any supplied CDX payload checksum must verify.
        """
        if not isinstance(urls, list) or not 1 <= len(urls) <= 100:
            raise ValueError("Wayback import requires one to one hundred URLs")
        instant = _time(cutoff)
        captures, payloads = [], {}
        for url in urls:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.fragment
            ):
                raise ValueError(
                    "Wayback source requires an HTTP(S) URL without credentials or fragment"
                )
            arguments = urlencode(
                {
                    "url": url,
                    "output": "json",
                    "to": instant.strftime("%Y%m%d%H%M%S"),
                    "filter": "statuscode:200",
                    "limit": "-1",
                    "fl": "timestamp,original,statuscode,mimetype,digest",
                }
            )
            cdx_url = "https://web.archive.org/cdx/search/cdx?" + arguments
            listing = await self.world.fetch_kernel(run_id, cdx_url)
            if listing.get("kind") != "result":
                return {**listing, "source_url": url, "imported": False}
            rows = json.loads(self._kernel_body(run_id, listing))
            if not isinstance(rows, list) or len(rows) < 2:
                return blocked(
                    "replay_miss", "No Wayback capture exists at or before cutoff", "wayback_miss"
                )
            candidates = []
            for row in rows[1:]:
                entry = dict(zip(rows[0], row, strict=True))
                stamp = entry.get("timestamp", "")
                if not re.fullmatch(r"\d{14}", stamp) or canonical_url(
                    entry["original"]
                ) != canonical_url(url):
                    continue
                captured = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
                if captured <= instant and entry.get("statuscode") == "200":
                    candidates.append((captured, entry))
            if not candidates:
                return blocked(
                    "replay_miss", "No valid capture satisfies the cutoff", "wayback_miss"
                )
            captured_at, entry = max(candidates, key=lambda value: value[0])
            archive_url = f"https://web.archive.org/web/{entry['timestamp']}id_/" + quote(
                url, safe=":/?=&%"
            )
            response = await self.world.fetch_kernel(run_id, archive_url)
            if response.get("kind") != "result":
                return {**response, "source_url": url, "imported": False}
            final = response.get("final_url", archive_url)
            redirected_stamp = re.search(r"/web/(\d{14})(?:id_)?/", final)
            if not redirected_stamp or urlsplit(final).hostname != "web.archive.org":
                return blocked(
                    "replay_miss", "Capture redirect lost archive provenance", "wayback_integrity"
                )
            actual_time = datetime.strptime(redirected_stamp[1], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            if actual_time != captured_at or actual_time > instant:
                return blocked(
                    "replay_miss",
                    "Archive redirected to a different capture time",
                    "wayback_cutoff",
                )
            body = self._kernel_body(run_id, response)
            archive_digest = entry.get("digest")
            digest_verified = None
            if archive_digest and archive_digest != "-":
                expected = archive_digest.removeprefix("sha1:").upper()
                actual = base64.b32encode(hashlib.sha1(body).digest()).decode()
                if not re.fullmatch(r"[A-Z2-7]{32}", expected) or expected != actual:
                    return blocked(
                        "replay_miss", "CDX payload checksum did not verify", "wayback_integrity"
                    )
                digest_verified = True
            captures.append(
                {
                    "url": url,
                    "captured_at": _stamp(actual_time),
                    "hash": digest(body),
                    "content_type": entry.get(
                        "mimetype", response.get("content_type", "application/octet-stream")
                    ),
                    "status": 200,
                    "archive_provenance": {
                        "source": "wayback",
                        "cdx_url": cdx_url,
                        "cdx_hash": listing["hash"],
                        "capture_url": final,
                        "archive_payload_digest_verified": digest_verified,
                    },
                }
            )
            payloads[url] = body
        return self.import_corpus(
            {"cutoff": _stamp(instant), "captures": captures, "search_lists": search_lists or []},
            payloads,
        )

    def get(self, corpus_id):
        with self.index.lock:
            row = self.index.db.execute(
                "SELECT payload FROM frozen_corpora WHERE corpus_id=?", (corpus_id,)
            ).fetchone()
        if not row:
            raise KeyError(corpus_id)
        corpus = json.loads(row[0])
        if corpus_id != "corpus_" + digest(corpus["manifest"]).split(":", 1)[1]:
            raise ValueError("frozen corpus manifest failed integrity verification")
        return corpus

    def fetch(
        self, run_id, action_id, url, corpus_id, *, inline=False, max_inline_chars=8000, **_ignored
    ):
        corpus = self.get(corpus_id)
        max_inline_chars = max(0, min(100000, int(max_inline_chars)))
        key = canonical_url(url)
        capture = next(
            (
                entry
                for entry in corpus["manifest"]["captures"]
                if canonical_url(entry["url"]) == key
            ),
            None,
        )
        if capture is None:
            return blocked("replay_miss", "URL absent from the pinned frozen corpus", "frozen_miss")
        path = self.root / corpus_id / (capture["hash"].split(":", 1)[1] + ".bin")
        if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != capture["hash"]:
            return blocked(
                "replay_miss", "Frozen capture failed byte verification", "frozen_integrity"
            )
        body = path.read_bytes()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", action_id):
            raise ValueError("invalid action id")
        extension = mimetypes.guess_extension(capture["content_type"].split(";", 1)[0]) or ".bin"
        target = self.store.run_dir(run_id) / "world" / "fetched" / (action_id + extension)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("fetched destination must not be a symlink")
        target.write_bytes(body)
        original_provenance = capture.get("provenance", {})
        reply = {
            "kind": "result",
            "handle": "/world/fetched/" + target.name,
            "url": url,
            "final_url": capture.get("final_url", capture["url"]),
            "status": capture["status"],
            "content_type": capture["content_type"],
            "bytes": len(body),
            "hash": capture["hash"],
            "headers_subset": capture.get("headers_subset", {}),
            "preview": body.decode("utf-8", errors="replace")[:2000],
            "provenance": {
                "capture_mode": "frozen",
                "corpus_id": corpus_id,
                "capture_ts": capture["captured_at"],
                "fetch_ts": capture["captured_at"],
                "origin": urlsplit(capture["url"]).hostname,
                "cutoff": corpus["cutoff"],
                "manifest_hash": corpus["manifest_hash"],
                "trust_class": original_provenance.get("trust_class", "unknown"),
                "injection_score": original_provenance.get("injection_score"),
                "classifier_id": original_provenance.get("classifier_id", "unmeasured"),
                "classifier_status": original_provenance.get("classifier_status", "unmeasured"),
                "injection_signals": original_provenance.get("injection_signals", []),
            },
        }
        if inline:
            text = capture["content_type"].startswith("text/") or any(
                kind in capture["content_type"] for kind in ("json", "xml")
            )
            content = (
                body.decode("utf-8", errors="replace") if text else base64.b64encode(body).decode()
            )
            reply.update(
                inline=content[:max_inline_chars],
                inline_encoding="utf-8" if text else "base64",
                inline_truncated=len(content) > max_inline_chars,
            )
        # A scoped index preserves actual capture provenance for later extraction.
        # This records pinned bytes; it does not make a live request or recapture.
        run_root = self.store.run_dir(run_id)
        index_path = run_root / "archive-index.json"
        if index_path.is_symlink():
            raise ValueError("frozen run archive index must not be a symlink")
        with self.index.lock:
            archive = json.loads(index_path.read_text()) if index_path.exists() else {}
            archived_reply = {
                key: value for key, value in reply.items() if not key.startswith("inline")
            }
            archive[canonical_url(url)] = {
                "reply": archived_reply,
                "body_path": str(target.relative_to(run_root)),
            }
            temporary = run_root / (".archive-index-" + action_id + ".tmp")
            temporary.write_text(_canonical(archive))
            temporary.replace(index_path)
        return reply

    def search(self, corpus_id, query, k=10, *, site=None, freshness=None):
        corpus = self.get(corpus_id)
        result = self.index.search(corpus_id, query, k, site=site, freshness=freshness)
        for item in result.get("items", []):
            capture = next(
                entry
                for entry in corpus["manifest"]["captures"]
                if canonical_url(entry["url"]) == canonical_url(item["url"])
            )
            path = self.root / corpus_id / (capture["hash"].split(":", 1)[1] + ".bin")
            if (
                path.is_symlink()
                or not path.is_file()
                or digest(path.read_bytes()) != capture["hash"]
            ):
                return blocked(
                    "replay_miss",
                    "Frozen search capture failed byte verification",
                    "frozen_integrity",
                )
        return result

    async def handle(self, run_id, action_id, tool, params):
        config = self.store.get_run(run_id)["config"]
        corpus_id = config.get("corpus_id")
        if not corpus_id:
            return blocked("replay_miss", "No pinned frozen corpus selected", "frozen_corpus")
        if tool == "search":
            return self.search(
                corpus_id,
                params["query"],
                params.get("k", 10),
                site=params.get("site"),
                freshness=params.get("freshness"),
            )
        if tool == "fetch":
            return self.fetch(
                run_id,
                action_id,
                params["url"],
                corpus_id,
                inline=params.get("inline", False),
                max_inline_chars=params.get("max_inline_chars", 8000),
            )
        return blocked("policy_deny", "Unsupported frozen operation", "frozen_tool")

    async def recover(self):
        return None

    async def aclose(self):
        if self._owns_index:
            await self.index.aclose()
