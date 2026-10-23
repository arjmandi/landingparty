"""A stable identity for the implementation actually serving an episode."""

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import __version__
from .util import digest


def implementation_manifest(root=None):
    root = Path(root) if root is not None else Path(__file__).parents[1]
    files = {}
    for package in (
        "worldd",
        "analysis",
        "adapters",
        "faces",
        "policy",
        "goals",
        "prereg",
        "probes",
    ):
        for path in sorted((root / package).rglob("*")):
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix in {".py", ".json", ".html", ".js", ".css"}
            ):
                files[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    if not files:
        raise ValueError("The implementation has no packaged source files")
    dependencies = {}
    for name in (
        "fastapi",
        "starlette",
        "pydantic",
        "httpx",
        "uvicorn",
        "cryptography",
        "warcio",
        "beautifulsoup4",
        "playwright",
        "pymupdf",
        "trafilatura",
        "readability-lxml",
        "matplotlib",
        "PyYAML",
        "openenv-core",
        "inspect-ai",
        "harbor",
        "openhands-sdk",
    ):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None
    return {
        "schema": "implementation-identity@1",
        "release": __version__,
        "files": files,
        "dependencies": dependencies,
    }


@lru_cache(maxsize=1)
def implementation_identity():
    # A process pins its identity once. Restart after source/dependency changes;
    # newly edited files cannot silently relabel an already running world.
    return f"landing-party@{__version__}/{digest(implementation_manifest())}"
