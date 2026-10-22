"""MkDocs hooks that publish the repository's root guides alongside docs/.

The README, getting-started guide, deployment guide and compiled design stay
where GitHub readers expect them. These hooks add them to the docs build under
short URLs and rewrite relative links so they resolve on the site. A link to a
repository file the site does not publish points at that file on GitHub.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from mkdocs.structure.files import File

ROOT = Path(__file__).resolve().parent.parent

# Repository path -> page path inside the docs build.
ROOT_PAGES = {
    "README.md": "index.md",
    "GETTING_STARTED.md": "getting-started.md",
    "deploy/README.md": "deployment.md",
    "00-landing-party.md": "design.md",
}

LINK = re.compile(r"(\]\()([^)\s]+)")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")

# Repository path -> page path for every page the build publishes.
published: dict[str, str] = {}


def on_files(files, config):
    for repo_path, src_uri in ROOT_PAGES.items():
        files.append(File.generated(config, src_uri, abs_src_path=str(ROOT / repo_path)))
    published.clear()
    for file in files.documentation_pages():
        published[repo_path_of(file)] = file.src_uri
    return files


def on_page_markdown(markdown, page, config, files):
    source = repo_path_of(page.file)
    here = posixpath.dirname(page.file.src_uri) or "."
    github = config.repo_url.rstrip("/")

    def rewrite(match: re.Match[str]) -> str:
        target, hashmark, fragment = match.group(2).partition("#")
        if not target or "://" in target or target.startswith(("/", "mailto:")):
            return match.group(0)
        path = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
        if path in published:
            target = posixpath.relpath(published[path], here)
        elif (ROOT / path).exists():
            kind = "tree" if (ROOT / path).is_dir() else "blob"
            target = f"{github}/{kind}/main/{path}"
        else:
            return match.group(0)
        return match.group(1) + target + hashmark + fragment

    lines = []
    fence = None
    for line in markdown.splitlines(keepends=True):
        marker = FENCE.match(line)
        if fence is None and marker:
            fence = marker.group(1)
        elif fence is not None and marker and marker.group(1).startswith(fence):
            fence = None
        elif fence is None:
            line = LINK.sub(rewrite, line)
        lines.append(line)
    return "".join(lines)


def on_serve(server, config, builder):
    for repo_path in ROOT_PAGES:
        server.watch(str(ROOT / repo_path))
    return server


def repo_path_of(file: File) -> str:
    return Path(file.abs_src_path).resolve().relative_to(ROOT).as_posix()
