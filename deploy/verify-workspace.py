"""Actual cross-UID workspace import permissions in disposable Docker storage."""

import hashlib
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path


def main():
    volume = "lp-workspace-" + uuid.uuid4().hex[:10]
    source = Path("worldd/workspaces.py").resolve()

    def docker(*argv, check=True):
        result = subprocess.run(["docker", *argv], capture_output=True, text=True, check=False)
        if check and result.returncode:
            raise RuntimeError(result.stderr[:1000])
        return result

    def world(script):
        return docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "10001:10002",
            "--mount",
            f"type=volume,src={volume},dst=/audit",
            "--entrypoint",
            "python3",
            "landing-party-world:0.1",
            "-c",
            script,
        )

    def agent(script, *, check=True):
        return docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "10002:10002",
            "--mount",
            f"type=volume,src={volume},dst=/work,volume-subpath=work",
            "landing-party-agent:0.1",
            "python3",
            "-c",
            script,
            check=check,
        )

    try:
        docker("volume", "create", volume)
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--user",
            "0:0",
            "--mount",
            f"type=volume,src={volume},dst=/audit",
            "landing-party-agent:0.1",
            "chmod",
            "777",
            "/audit",
        )
        initialized = world(
            "import os,shutil; from pathlib import Path; from worldd.workspaces import snapshot_workspace,snapshot_manifest; "
            "root=Path('/audit'); source=root/'source'; (source/'nested').mkdir(parents=True); "
            "(source/'nested/plain').write_text('captured'); (source/'run.sh').write_text('#!/bin/sh\\nexit 0\\n'.replace('\\\\n','\\n')); "
            "(source/'run.sh').chmod(0o700); (source/'current').symlink_to('nested/plain'); "
            "(root/'private').write_text('private fixture'); (root/'private').chmod(0o600); "
            "assert snapshot_workspace(source,root/'snapshot')==[]; shutil.copytree(root/'snapshot',root/'work',symlinks=True); print(os.getuid())"
        )
        before = agent(
            "from pathlib import Path; Path('/work/nested/plain').read_text()", check=False
        )
        shared = world(
            "import json; from worldd.workspaces import share_workspace; print(json.dumps(share_workspace('/audit/work')))"
        )
        after = agent(
            "import os,json,subprocess; from pathlib import Path; "
            "root=Path('/work'); assert (root/'current').read_text()=='captured'; "
            "(root/'nested/plain').write_text('agent changed'); (root/'new').write_text('agent created'); "
            "assert subprocess.run([str(root/'run.sh')],check=False).returncode==0; "
            "assert not Path('/audit/private').exists(); print(json.dumps({'uid':os.getuid(),'gid':os.getgid()}))"
        )
        retained = world(
            "from pathlib import Path; root=Path('/audit'); "
            "assert (root/'snapshot/nested/plain').read_text()=='captured'; "
            "assert (root/'private').stat().st_mode&0o777==0o600; print('retained')"
        )
        identity = json.loads(after.stdout)
        actual_source_hash = world(
            "import hashlib; from pathlib import Path; "
            "print(hashlib.sha256(Path('/opt/landingparty/worldd/workspaces.py').read_bytes()).hexdigest())"
        ).stdout.strip()
        checkout_source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        checks = {
            "world_runs_as_10001": initialized.stdout.strip() == "10001",
            "private_import_unreadable_before_share": before.returncode != 0,
            "shared_files_and_directories": json.loads(shared.stdout)
            == {"directories": 2, "files": 2, "symlinks": 1},
            "actual_harness_uid_and_gid": identity == {"uid": 10002, "gid": 10002},
            "read_write_create_execute_and_internal_link": after.returncode == 0,
            "private_snapshot_and_outside_permissions_retained": retained.stdout.strip()
            == "retained",
            "packaged_helper_matches_checkout": actual_source_hash == checkout_source_hash,
        }
        payload = {
            "checked_at": datetime.now(UTC).isoformat(),
            "checks": checks,
            "check_count": len(checks),
            "passed": all(checks.values()),
            "source_helper_sha256": actual_source_hash,
            "world_image_id": docker(
                "image", "inspect", "landing-party-world:0.1", "--format", "{{.Id}}"
            ).stdout.strip(),
            "agent_image_id": docker(
                "image", "inspect", "landing-party-agent:0.1", "--format", "{{.Id}}"
            ).stdout.strip(),
            "source_helper_mounted_readonly": False,
            "scientific_gate_evidence": False,
        }
        target = Path("data/validation/workspace-software.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps(payload))
        if not payload["passed"]:
            raise SystemExit(1)
    finally:
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    os.environ.setdefault("DOCKER_CONFIG", "/workspace/.cache/docker")
    main()
