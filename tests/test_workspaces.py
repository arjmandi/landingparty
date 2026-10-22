import os

import pytest

from worldd.storage import content_hash
from worldd.workspaces import share_workspace, snapshot_manifest, snapshot_workspace, verify_import


def test_snapshot_private_atomic_metadata_and_verified_safe_internal_links(tmp_path):
    source, target = tmp_path / "work", tmp_path / "snapshot"
    source.mkdir()
    (source / "dir").mkdir()
    (source / "dir/evidence.txt").write_text("captured evidence")
    (source / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "run.sh").chmod(0o700)
    (source / "current").symlink_to("dir/evidence.txt")
    (source / "alias").symlink_to("dir", target_is_directory=True)
    (source / "alias-current").symlink_to("alias/evidence.txt")
    assert snapshot_workspace(source, target) == []
    assert target.stat().st_mode & 0o777 == 0o700
    assert os.readlink(target / "alias-current") == "alias/evidence.txt"
    assert (target / "dir/evidence.txt").read_text() == "captured evidence"
    manifest = snapshot_manifest(target)
    assert manifest["entries"]["run.sh"]["executable"] is True
    assert manifest["entries"]["current"] == {"type": "symlink", "target": "dir/evidence.txt"}
    assert manifest["root_hash"] == content_hash(
        {key: value for key, value in manifest.items() if key != "root_hash"}
    )
    assert verify_import(target, {"manifest": manifest}) == manifest
    with pytest.raises(FileExistsError):
        snapshot_workspace(source, target)
    assert not list(tmp_path.glob(".snapshot.tmp-*"))


def test_snapshot_preserves_external_link_metadata_without_reading_target_and_omits_fifo(tmp_path):
    source, target = tmp_path / "work", tmp_path / "snapshot"
    source.mkdir()
    outside = tmp_path / "private"
    outside.write_text("WORLD PRIVATE NOT WORKSPACE DATA")
    (source / "external").symlink_to(outside)
    os.mkfifo(source / "pipe")
    omissions = snapshot_workspace(source, target)
    assert omissions == [{"path": "pipe", "reason": "special_file"}]
    manifest = snapshot_manifest(target)
    assert manifest["entries"] == {"external": {"type": "symlink", "target": str(outside)}}
    assert "WORLD PRIVATE" not in str(manifest)
    with pytest.raises(ValueError, match="relative"):
        verify_import(target, manifest)


@pytest.mark.parametrize(
    "links",
    [
        {"escape": "../private"},
        {"a": "b", "b": "a"},
        {"a": "a/child"},
        {"a": "../../outside"},
        {"alias": "../outside", "nested": "alias/child"},
    ],
)
def test_import_rejects_escaping_and_cyclic_links_even_with_matching_anchor(tmp_path, links):
    source, target = tmp_path / "work", tmp_path / "snapshot"
    source.mkdir()
    for name, value in links.items():
        (source / name).symlink_to(value)
    snapshot_workspace(source, target)
    with pytest.raises(ValueError, match="symlink"):
        verify_import(target, snapshot_manifest(target))


def test_manifest_detects_tampering_and_legacy_imports_fail_closed(tmp_path):
    source, target = tmp_path / "work", tmp_path / "snapshot"
    source.mkdir()
    (source / "one").write_text("original")
    snapshot_workspace(source, target)
    anchor = snapshot_manifest(target)
    (target / "one").write_text("mutated")
    with pytest.raises(ValueError, match="sealed content manifest"):
        verify_import(target, anchor)
    with pytest.raises(ValueError, match="anchored"):
        verify_import(target, {})
    with pytest.raises(TypeError, match="anchored"):
        verify_import(target, None)
    os.mkfifo(target / "injected-fifo")
    with pytest.raises(ValueError, match="special file"):
        snapshot_manifest(target)


def test_source_and_destination_parent_symlinks_are_never_followed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "one").write_text("one")
    (tmp_path / "alias").symlink_to(source, target_is_directory=True)
    with pytest.raises(OSError):
        snapshot_workspace(tmp_path / "alias", tmp_path / "snapshot")
    with pytest.raises(OSError):
        snapshot_workspace(source, tmp_path / "alias/snapshot")
    with pytest.raises(OSError):
        snapshot_manifest(tmp_path / "alias")
    with pytest.raises(ValueError, match="outside"):
        snapshot_workspace(source, source / "nested")


def test_replaced_child_directory_is_omitted_without_following_external_parent(
    tmp_path, monkeypatch
):
    from worldd import workspaces

    source, target = tmp_path / "work", tmp_path / "snapshot"
    source.mkdir()
    (source / "dir").mkdir()
    (source / "dir/ordinary").write_text("ordinary")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("must never be read")
    original = workspaces.os.open
    replaced = False

    def racing_open(name, flags, *args, **kwargs):
        nonlocal replaced
        if name == "dir" and not replaced and kwargs.get("dir_fd") is not None:
            replaced = True
            (source / "dir").rename(source / "moved")
            (source / "dir").symlink_to(outside, target_is_directory=True)
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(workspaces.os, "open", racing_open)
    omissions = snapshot_workspace(source, target)
    assert replaced and omissions == [{"path": "dir", "reason": "entry_changed_or_unreadable"}]
    assert snapshot_manifest(target)["entries"] == {}


def test_replaced_regular_file_is_not_dereferenced(tmp_path, monkeypatch):
    from worldd import workspaces

    source, target = tmp_path / "work", tmp_path / "snapshot"
    source.mkdir()
    (source / "one").write_text("ordinary")
    private = tmp_path / "private"
    private.write_text("never read this")
    original = workspaces.os.open
    replaced = False

    def racing_open(name, flags, *args, **kwargs):
        nonlocal replaced
        if name == "one" and flags == workspaces.FILE_FLAGS and not replaced:
            replaced = True
            (source / "one").unlink()
            (source / "one").symlink_to(private)
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(workspaces.os, "open", racing_open)
    omissions = snapshot_workspace(source, target)
    assert replaced and omissions == [{"path": "one", "reason": "entry_changed_or_unreadable"}]
    assert snapshot_manifest(target)["entries"] == {}


def test_snapshot_omits_non_utf8_filename_without_breaking_the_sealed_record(tmp_path):
    source, target = tmp_path / "work", tmp_path / "snapshot"
    source.mkdir()
    descriptor = os.open(os.fsencode(source) + b"/invalid-\xff", os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    omissions = snapshot_workspace(source, target)
    assert omissions == [
        {"path": "fsbytes:" + b"invalid-\xff".hex(), "reason": "unsupported_filename"}
    ]
    assert snapshot_manifest(target)["entries"] == {}


def test_share_workspace_preserves_executable_meaning_and_never_chmods_external_link(tmp_path):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    (work / "nested").mkdir(mode=0o700)
    (work / "nested/plain").write_text("data")
    (work / "script").write_text("#!/bin/sh\nexit 0\n")
    (work / "script").chmod(0o700)
    external = tmp_path / "private"
    external.write_text("private")
    external.chmod(0o600)
    (work / "link").symlink_to(external)
    result = share_workspace(work)
    assert result == {"directories": 2, "files": 2, "symlinks": 1}
    assert work.stat().st_mode & 0o777 == 0o770
    assert (work / "nested/plain").stat().st_mode & 0o777 == 0o660
    assert (work / "script").stat().st_mode & 0o777 == 0o770
    assert external.stat().st_mode & 0o777 == 0o600


def test_share_workspace_refuses_hardlinks_and_special_files(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    external = tmp_path / "private"
    external.write_text("private")
    external.chmod(0o600)
    os.link(external, work / "hardlink")
    with pytest.raises(ValueError, match="hard-link"):
        share_workspace(work)
    assert external.stat().st_mode & 0o777 == 0o600
    (work / "hardlink").unlink()
    os.mkfifo(work / "fifo")
    with pytest.raises(ValueError, match="special file"):
        share_workspace(work)


def test_snapshot_omits_non_utf8_symlink_target_as_metadata(tmp_path):
    source, target = tmp_path / "work", tmp_path / "snapshot"
    source.mkdir()
    os.symlink(b"invalid-\xff", os.fsencode(source / "link"))
    assert snapshot_workspace(source, target) == [
        {"path": "link", "reason": "unsupported_link_target"}
    ]
    assert snapshot_manifest(target)["entries"] == {}


def test_renamed_source_root_remains_bound_to_opened_descriptor(tmp_path, monkeypatch):
    from worldd import workspaces

    source, target = tmp_path / "work", tmp_path / "snapshot"
    source.mkdir()
    (source / "ordinary").write_text("captured original")
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret").write_text("must not be read")
    original = workspaces._entries
    replaced = False

    def replaced_entries(descriptor):
        nonlocal replaced
        if not replaced:
            replaced = True
            source.rename(tmp_path / "moved-work")
            source.symlink_to(private, target_is_directory=True)
        return original(descriptor)

    monkeypatch.setattr(workspaces, "_entries", replaced_entries)
    assert snapshot_workspace(source, target) == []
    assert (target / "ordinary").read_text() == "captured original"
    assert not (target / "secret").exists()
