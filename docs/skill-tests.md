# Isolated procedural skill checks

`SkillTestService` runs Python `unittest` modules in an explicitly configured trusted Docker worker. Candidate code is never evaluated by the world process or a host Python subprocess. The agent tool manifest does not expose this control.

The researcher-only `POST /v1/skills/test` accepts `run_id`, `name`, `source`, `tests`, and optional `promote: true`. A running session with the belief pack is required. The unittest module imports the proposed function from `skill`, for example:

```python
import unittest
from skill import double


class DoubleTests(unittest.TestCase):
    def test_double(self):
        self.assertEqual(double(4), 8)
```

The worker records a hash-chained `skill_test` event with the actual unittest count, outcome, exact UTF-8 source/test hashes, immutable inspected image ID, exit status, and isolation policy. With `promote: true`, a passing result is passed to `BeliefService.promote_skill` using that event's evidence reference. The promotion mechanism checks the exact hashes. Caller-supplied `passed`, test counts, or receipt dictionaries cannot replace the execution step. A zero-test suite, ordinary assertion module without unittests, abrupt exit without a valid runner receipt, failure, timeout, or output overflow cannot pass the normal runner checks.

Passing tests establish the outcome of those checks. They do not establish semantic correctness, test quality, absence of deliberate unittest manipulation, suitability for a new goal, or any scientific gate. Review the test code as an instrument; arbitrary Python checks may be vacuous or dishonest. No Python-in-process security sandbox is claimed.

## Operator configuration

The default refuses execution. A trusted host installation may explicitly set both:

```sh
export WORLD_SKILL_TEST_WORKER=local-docker
export WORLD_SKILL_TEST_IMAGE=sha256:<approved-local-image-id>
```

The worker inspects the already installed image and executes its immutable `sha256` ID. It never pulls an image. Image names, Docker arguments, resource limits, filesystem paths and environment forwarding are not request fields. Images with implicit Docker volumes are rejected. Use a reviewed Python image without embedded credentials; it must provide `/usr/bin/env` and `/usr/local/bin/python3`.

The production world/agent deployment does not mount a Docker daemon or enable this worker automatically. Run this boundary on a trusted worker host, or inject a trusted `DockerSkillWorker` instance through operator deployment code. Do not grant the agent or ordinary world container access to the host Docker socket.

The host diagnostic command is:

```sh
python -m worldd.skill_tests --image <approved-local-image-id> --source skill.py --tests test_skill.py
```

This command executes only Docker's trusted client on the host and reports the isolated result. Its stdout alone is not a promotable world receipt; use the configured service to create the authoritative event.

## Isolation and lifecycle

Each container uses no network, a read-only root filesystem, all capabilities dropped, `no-new-privileges`, UID/GID 65534, private IPC, a 32-process limit, 128 MiB memory with no extra swap, half a CPU, and a 16 MiB `noexec`/`nosuid`/`nodev` temporary filesystem. The default wall limit is ten seconds and combined stdout/stderr are capped at 64 KiB. Docker's actual container configuration is inspected before Python starts. The host worker also caps its Docker subprocess output and duration.

The sole bind mount is a newly staged directory containing the exact source, tests and runner, all read-only. No workspace, world data, private credential directory, provider environment, host home or daemon socket is mounted. The child runs under an empty environment with only its explicit path/home/locale. Raw execution output stays in a mode-0600 control artifact; the public record contains output hashes.

`stop_run` cancels and drains active jobs, forcibly removes their containers, and records cancellation before the episode's seal. The world lifecycle calls it before reconciliation. The worker labels containers with a world-specific namespace; recovery removes only interrupted containers in that namespace. Unconfirmed cleanup taints the episode and closes the optional worker pending reconciliation. A worker unavailable during startup remains closed while other world services can start.

## Validation

The normal suite does not require a Docker daemon. The explicit trusted-host smoke command is:

```sh
WORLD_RUN_DOCKER_SKILL_TESTS=1 python -m pytest tests/test_skill_tests.py -q
```

The actual-container tests exercise promotion against recorded evidence; failing, empty and abruptly exiting checks; network/credential/mount isolation; observed cgroup CPU/memory/process limits; output and wall limits; interruption recovery; and cancellation of an actually running container before seal. The software smoke used installed Python image `sha256:9e87977b867847e186d066f531ef783b006d582a985c341c269446088d90f2c4`. These are software checks, not empirical research results.
