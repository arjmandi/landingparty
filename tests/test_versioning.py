"""The served operator UI belongs to the implementation's version identity."""

import pytest

from worldd.util import digest
from worldd.versioning import implementation_manifest


@pytest.mark.parametrize("asset", ["studio.js", "studio.css"])
def test_served_ui_asset_changes_implementation_identity(tmp_path, asset):
    package = tmp_path / "worldd"
    package.mkdir()
    (package / "runtime.py").write_text("# packaged runtime\n")
    (package / "studio.html").write_text("<main>Operator UI</main>")
    target = package / asset
    target.write_text("first actual UI content")
    first = implementation_manifest(tmp_path)
    assert first["files"]["worldd/" + asset] == digest(target.read_bytes())
    target.write_text("second actual UI content")
    assert digest(implementation_manifest(tmp_path)) != digest(first)


def test_ui_identity_excludes_private_runtime_files_and_link_targets(tmp_path):
    package = tmp_path / "worldd"
    package.mkdir()
    (package / "studio.js").write_text("export const application = 1;")
    (package / "studio.css").write_text("body { color: black; }")
    private = tmp_path / "data"
    private.mkdir()
    source = private / "private.js"
    source.write_text("private runtime content")
    (package / "linked.js").symlink_to(source)
    first = implementation_manifest(tmp_path)
    assert set(first["files"]) == {"worldd/studio.js", "worldd/studio.css"}
    source.write_text("changed private runtime content")
    assert digest(implementation_manifest(tmp_path)) == digest(first)
