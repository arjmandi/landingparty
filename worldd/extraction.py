"""Explicit, local extraction of a run's own fetched resources."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .util import digest

EXTRACTOR_ID = "beautifulsoup4-v1"
BANNER_POLICY_ID = "extract-provenance-banner@1"
VIEWS = {"text", "markdown", "links", "tables", "metadata"}


def extract(data_dir: Path, run_id: str, handle: str, view: str, *, banner: bool = False) -> dict:
    if view not in VIEWS:
        return _blocked("Unsupported extraction view", "extraction_view")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id):
        return _blocked("Invalid run id", "resource_scope")
    prefix = "/world/fetched/"
    if not isinstance(handle, str) or not handle.startswith(prefix):
        return _blocked("Only fetched resource handles may be extracted", "resource_scope")
    relative = Path(handle[len(prefix) :])
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name in {".", ".."}:
        return _blocked("Resource handle escapes the run", "resource_scope")
    root = Path(data_dir) / "runs" / run_id
    if any(path.is_symlink() for path in (root, root / "world", root / "world/fetched")):
        return _blocked("Resource directory escapes the run", "resource_scope")
    fetch_root = (root / "world" / "fetched").resolve()
    path = (fetch_root / relative).resolve()
    if not path.is_relative_to(fetch_root):
        return _blocked("Resource handle escapes the run", "resource_scope")
    try:
        raw = path.read_bytes()
    except OSError:
        return _blocked("The fetched resource is unavailable", "resource_missing")
    if path.suffix.lower() not in {
        ".html",
        ".htm",
        ".txt",
        ".json",
        ".xml",
        ".md",
        ".csv",
        ".xhtml",
    }:
        return {
            "kind": "blocked",
            "type": "empty_extraction",
            "detail": "This resource type needs a document extractor that is not installed",
            "rule_id": "extractor_unavailable",
        }
    text = raw.decode("utf-8", errors="replace")
    html = path.suffix.lower() in {".html", ".htm", ".xhtml"}
    soup = BeautifulSoup(text, "html.parser")
    base_url = ""
    capture = None
    index_path = root / "archive-index.json"
    if index_path.exists():
        try:
            for entry in json.loads(index_path.read_text()).values():
                if entry["reply"].get("handle") == handle:
                    capture = entry["reply"]
                    base_url = capture.get("final_url", "")
                    break
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if capture and capture.get("hash") != digest(raw):
        return _blocked("Fetched bytes failed their capture hash", "resource_integrity")
    if banner and not (capture and capture.get("provenance")):
        return _blocked("Captured provenance is unavailable", "provenance_missing")
    if view == "metadata":
        data = {
            "title": soup.title.get_text(strip=True) if html and soup.title else None,
            "meta": {
                tag.get("name") or tag.get("property"): tag.get("content")
                for tag in (soup.find_all("meta") if html else [])
                if tag.get("name") or tag.get("property")
            },
            "bytes": len(raw),
            "source": handle,
            "url": base_url,
        }
    elif view == "links":
        data = (
            [
                {"url": urljoin(base_url, tag["href"]), "text": tag.get_text(" ", strip=True)}
                for tag in soup.find_all("a", href=True)
            ]
            if html
            else []
        )
    elif view == "tables":
        data = (
            [
                [
                    [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
                    for row in table.find_all("tr")
                ]
                for table in soup.find_all("table")
            ]
            if html
            else []
        )
    else:
        client_rendered = bool(html and soup.find("script"))
        for tag in soup(["script", "style", "noscript", "head"]):
            tag.decompose()
        if view == "markdown" and html:
            for heading in soup.find_all(re.compile(r"^h[1-6]$")):
                heading.replace_with(
                    "\n"
                    + "#" * int(heading.name[1])
                    + " "
                    + heading.get_text(" ", strip=True)
                    + "\n"
                )
            for link in soup.find_all("a", href=True):
                link.replace_with(
                    f"[{link.get_text(' ', strip=True)}]({urljoin(base_url, link['href'])})"
                )
            data = soup.get_text("\n", strip=True)
        else:
            data = soup.get_text("\n", strip=True) if html else text
        if not data.strip():
            return {
                "kind": "blocked",
                "type": "dynamic_page" if client_rendered else "empty_extraction",
                "detail": "The page needs client rendering; use an explicit browser action"
                if client_rendered
                else "No text was extracted from this resource",
                "rule_id": "client_rendered_page" if client_rendered else "empty_extraction",
            }
    provenance = None
    if banner:
        provenance = {
            name: capture["provenance"].get(name)
            for name in ("origin", "fetch_ts", "capture_mode", "trust_class", "injection_score")
        }
        provenance["source_hash"] = capture["hash"]
        provenance["classifier_calibrated"] = capture["provenance"].get("classifier_calibrated")
        if isinstance(data, str):
            data = (
                "Provenance: "
                + json.dumps(provenance, ensure_ascii=True, sort_keys=True)
                + "\n"
                + data
            )
    output = root / "world" / "extracted"
    if output.is_symlink():
        return _blocked("Extraction directory escapes the run", "resource_scope")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    suffix = ".json" if isinstance(data, (dict, list)) else ".md" if view == "markdown" else ".txt"
    filename = relative.stem + "-" + view + ("-banner" if banner else "") + suffix
    destination = output / filename
    if destination.is_symlink():
        return _blocked("Extraction file escapes the run", "resource_scope")
    document = (
        {"provenance": provenance, "data": data}
        if banner and isinstance(data, (dict, list))
        else data
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2)
        if isinstance(document, (dict, list))
        else document
    )
    result = {
        "kind": "result",
        "path": "/world/extracted/" + filename,
        "data": data,
        "extractor_id": EXTRACTOR_ID,
        "format_id": BANNER_POLICY_ID if banner else "extract-plain@1",
    }
    if banner:
        result["provenance"] = provenance
    return result


def _blocked(detail: str, rule: str) -> dict:
    return {"kind": "blocked", "type": "policy_deny", "detail": detail, "rule_id": rule}
