"""Private, anchored snapshots of an untrusted writable workspace.

Every traversal stays on opened directory descriptors. Symlinks are metadata,
never sources of file bytes; imports additionally require safe in-tree targets.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .storage import content_hash

POLICY_ID = "workspace-snapshot@1"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@contextmanager
def _directory(path):
    """Open each path component without following a replaced/symlink parent."""
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _entries(descriptor):
    with os.scandir(descriptor) as iterator:
        return sorted(entry.name for entry in iterator)


class SnapshotLimit(Exception):
    """A passive capture exhausted its declared resource budget."""


class SnapshotBudget:
    def __init__(self, *, max_entries, max_bytes, max_seconds):
        if any(isinstance(v, bool) or v <= 0 for v in (max_entries, max_bytes, max_seconds)):
            raise ValueError("Snapshot bounds must be positive")
        self.max_entries, self.max_bytes = int(max_entries), int(max_bytes)
        self.deadline = time.monotonic() + max_seconds
        self.entries = self.bytes = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise SnapshotLimit("capture_time_limit")

    def names(self, descriptor):
        # Never materialize/sort an attacker-sized directory before checking bounds.
        names = []
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                self.check()
                self.entries += 1
                if self.entries > self.max_entries:
                    raise SnapshotLimit("capture_entry_limit")
                names.append(entry.name)
        return sorted(names)

    def file(self, size):
        self.check()
        if size > self.max_bytes - self.bytes:
            raise SnapshotLimit("capture_byte_limit")
        self.bytes += size


def _copy_tree(source, target, prefix, omissions, budget=None):
    for name in budget.names(source) if budget else _entries(source):
        if budget:
            budget.check()
        relative = (prefix / name).as_posix()
        try:
            relative.encode("utf-8")
        except UnicodeEncodeError:
            omissions.append(
                {"path": "fsbytes:" + os.fsencode(relative).hex(), "reason": "unsupported_filename"}
            )
            continue
        try:
            info = os.stat(name, dir_fd=source, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                link = os.readlink(name, dir_fd=source)
                try:
                    link.encode("utf-8")
                except UnicodeEncodeError:
                    omissions.append({"path": relative, "reason": "unsupported_link_target"})
                    continue
                os.symlink(link, name, dir_fd=target)
            elif stat.S_ISDIR(info.st_mode):
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=source)
                try:
                    os.mkdir(name, mode=0o700, dir_fd=target)
                    destination = os.open(name, DIRECTORY_FLAGS, dir_fd=target)
                    try:
                        _copy_tree(child, destination, prefix / name, omissions, budget)
                    finally:
                        os.close(destination)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                child = os.open(name, FILE_FLAGS, dir_fd=source)
                try:
                    actual = os.fstat(child)
                    if not stat.S_ISREG(actual.st_mode):
                        raise ValueError("changed_file_type")
                    # Limit a concurrently growing file to its observed extent.
                    remaining = actual.st_size
                    if budget:
                        budget.file(remaining)
                    destination = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o700 if actual.st_mode & 0o111 else 0o600,
                        dir_fd=target,
                    )
                    try:
                        while remaining:
                            if budget:
                                budget.check()
                            block = os.read(child, min(1024 * 1024, remaining))
                            if not block:
                                break
                            remaining -= len(block)
                            view = memoryview(block)
                            while view:
                                view = view[os.write(destination, view) :]
                        after = os.fstat(child)
                        if remaining or any(
                            getattr(actual, key) != getattr(after, key)
                            for key in ("st_size", "st_mtime_ns", "st_ctime_ns")
                        ):
                            omissions.append(
                                {"path": relative, "reason": "file_changed_during_capture"}
                            )
                        os.fsync(destination)
                    finally:
                        os.close(destination)
                finally:
                    os.close(child)
            else:
                omissions.append({"path": relative, "reason": "special_file"})
        except (OSError, ValueError) as exc:
            # No exception content/host path enters the public record.
            omissions.append(
                {
                    "path": relative,
                    "reason": "entry_changed_or_unreadable"
                    if isinstance(exc, OSError)
                    else str(exc),
                }
            )
    if budget:
        budget.check()
    os.fsync(target)


def _rename_private(source, target, parent):
    """Atomic no-replace rename: an existing snapshot is never overwritten."""
    libc = ctypes.CDLL(None, use_errno=True)
    # Linux renameat2 with RENAME_NOREPLACE; macOS renameatx_np with RENAME_EXCL.
    rename, flags = getattr(libc, "renameat2", None), 1
    if rename is None:
        rename, flags = getattr(libc, "renameatx_np", None), 4
    if rename is None:
        raise OSError(errno.ENOTSUP, "Atomic no-replace snapshots require renameat2")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(source), parent, os.fsencode(target), flags) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def snapshot_workspace(source, target, *, limits=None):
    """Snapshot bytes/metadata without following untrusted links; return omissions."""
    source, target = Path(os.path.abspath(source)), Path(os.path.abspath(target))
    if target == source or target.is_relative_to(source):
        raise ValueError("Snapshot target must be outside the writable source")
    temporary = "." + target.name + ".tmp-" + uuid.uuid4().hex
    omissions = []
    budget = SnapshotBudget(**limits) if limits is not None else None
    with _directory(source) as source_fd, _directory(target.parent) as parent_fd:
        os.mkdir(temporary, mode=0o700, dir_fd=parent_fd)
        try:
            target_fd = os.open(temporary, DIRECTORY_FLAGS, dir_fd=parent_fd)
            try:
                try:
                    _copy_tree(source_fd, target_fd, PurePosixPath(), omissions, budget)
                except SnapshotLimit as exc:
                    omissions.append({"path": ".", "reason": str(exc)})
            finally:
                os.close(target_fd)
            _rename_private(temporary, target.name, parent_fd)
            os.fsync(parent_fd)
        finally:
            # Staging is private and rmtree's descriptor traversal never follows links.
            try:
                shutil.rmtree(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    return omissions


def _manifest_tree(descriptor, prefix, entries):
    for name in _entries(descriptor):
        relative = (prefix / name).as_posix()
        try:
            relative.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Snapshot contains a filename that is not valid UTF-8") from exc
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            entries[relative] = {"type": "symlink", "target": os.readlink(name, dir_fd=descriptor)}
        elif stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                entries[relative] = {"type": "directory"}
                _manifest_tree(child, prefix / name, entries)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            child = os.open(name, FILE_FLAGS, dir_fd=descriptor)
            try:
                actual = os.fstat(child)
                if not stat.S_ISREG(actual.st_mode):
                    raise ValueError("Workspace entry changed type while hashing")
                hashed, size = hashlib.sha256(), 0
                while block := os.read(child, 1024 * 1024):
                    hashed.update(block)
                    size += len(block)
                entries[relative] = {
                    "type": "file",
                    "sha256": "sha256:" + hashed.hexdigest(),
                    "bytes": size,
                    "executable": bool(actual.st_mode & 0o111),
                }
            finally:
                os.close(child)
        else:
            raise ValueError("Snapshot contains a special file")


def snapshot_manifest(snapshot):
    """Describe exact private snapshot content; never dereference any symlink."""
    entries = {}
    with _directory(snapshot) as descriptor:
        _manifest_tree(descriptor, PurePosixPath(), entries)
    manifest = {"policy_id": POLICY_ID, "entries": entries}
    return {**manifest, "root_hash": content_hash(manifest)}


def _resolve_link(name, entries):
    """Resolve POSIX components using only captured metadata, including parent links."""
    pending = list(PurePosixPath(name).parts)
    resolved, visited, expansions = [], set(), 0
    while pending:
        component = pending.pop(0)
        if component in {"", "."}:
            continue
        if component == "..":
            if not resolved:
                raise ValueError("Workspace symlink escapes its snapshot")
            resolved.pop()
            continue
        candidate = "/".join([*resolved, component])
        entry = entries.get(candidate)
        if entry and entry["type"] == "symlink":
            state = (candidate, tuple(pending))
            if state in visited or expansions >= 40:
                raise ValueError("Workspace symlink is cyclic or too deep")
            visited.add(state)
            expansions += 1
            target = entry["target"]
            if PurePosixPath(target).is_absolute() or "\x00" in target:
                raise ValueError("Workspace symlink target must be relative")
            pending = [*PurePosixPath(target).parts, *pending]
        else:
            if entry and entry["type"] == "file" and pending:
                raise ValueError("Workspace symlink traverses a regular file")
            resolved.append(component)


def verify_import(snapshot, anchor):
    """Require an exact sealed-record manifest and relative, nonescaping links."""
    if not isinstance(anchor, dict):
        raise TypeError("An anchored workspace snapshot is required")
    anchor = anchor.get("manifest", anchor)
    if (
        not isinstance(anchor, dict)
        or anchor.get("policy_id") != POLICY_ID
        or not isinstance(anchor.get("entries"), dict)
    ):
        raise ValueError("An anchored workspace snapshot is required")
    actual = snapshot_manifest(snapshot)
    if any(actual[key] != anchor.get(key) for key in ("policy_id", "entries", "root_hash")):
        raise ValueError("Workspace snapshot failed its sealed content manifest")
    for name, entry in actual["entries"].items():
        if entry["type"] == "symlink":
            _resolve_link(name, actual["entries"])
    return actual


def share_workspace(target):
    """Grant the harness group access to a private import without following links."""
    counts = {"directories": 0, "files": 0, "symlinks": 0}

    def share(descriptor):
        os.fchmod(descriptor, 0o770)
        counts["directories"] += 1
        for name in _entries(descriptor):
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                counts["symlinks"] += 1
            elif stat.S_ISDIR(info.st_mode):
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    share(child)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                child = os.open(name, FILE_FLAGS, dir_fd=descriptor)
                try:
                    actual = os.fstat(child)
                    if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                        raise ValueError(
                            "Sharing requires a regular file without hard-link aliases"
                        )
                    os.fchmod(child, 0o770 if actual.st_mode & 0o111 else 0o660)
                    counts["files"] += 1
                finally:
                    os.close(child)
            else:
                raise ValueError("Shared workspace contains a special file")

    with _directory(target) as descriptor:
        share(descriptor)
    return counts
