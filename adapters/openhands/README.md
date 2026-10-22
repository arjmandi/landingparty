# OpenHands

The native SDK launcher is:

```bash
uv sync --extra standards
WORLD_RUN_ID=r_... WORLD_RUN_TOKEN=... python -m adapters.openhands.native \
  --model gpt-4o-mini-2024-07-18 --workspace /work \
  --briefing /work/preamble.txt --goal /work/goal.txt \
  --transcript /work/openhands.jsonl
```

The researcher creates the run first and registers this harness configuration.
The launcher builds the actual SDK `LLM`, `Agent`, and `Conversation`, using
only World MCP tools plus the SDK's finish tool. This is an explicit
world-MCP-only OpenHands condition. It does not enable stock terminal/browser
tools. The lane verifies the frontend capability and replaces it with the
world-held provider credential. The current session's briefing and goal are
passed literally, and actual SDK events are written to the requested new
transcript. The caller owns sealing after the invocation ends. Provider,
webhook, payment, vault, control, and ambient credential variables are removed
before SDK startup. The API also accepts an explicit reasoning effort;
its default is unset for snapshots that do not support reasoning options.

An explicit native fixture check passed with OpenHands SDK 1.57.0: two real
SDK model requests through the local lane, an actual stdio MCP fetch, finish,
priced usage, and a verified sealed World record. The fixture had no real
provider credentials or paid external calls. Use the fence for enforced
isolation in real runs.

`openhands_binding()` produces model/MCP routing metadata. The world MCP
server remains the only world tool interface. Start
`python -m adapters.openhands.proxy --lane-url http://127.0.0.1:8081/openai/v1`
with `WORLD_RUN_ID` and `WORLD_RUN_TOKEN` supplied at runtime; the proxy listens
on loopback port 18082. Point OpenHands' OpenAI-compatible model base URL at
`http://127.0.0.1:18082/v1`.

`runtime_environment(binding, source)` builds process environment values for
an OpenHands launch. Its frontend API key is its actual scoped run capability,
which the local proxy verifies. The proxy strips that frontend key, injects
`X-World-Run-Id` and `X-World-Run-Token`, and forwards only Chat/Responses
requests to the metered world lane. Provider secrets and the researcher control
token are removed from the agent environment. No secret is written into the
binding metadata or a model prompt.

The generic binding includes stdio MCP registration metadata for other
OpenHands launch mechanisms. Proxy variables
and the world CA must be configured together when shell networking is needed.
Use the fenced deployment for enforced isolation. A cooperative environment
setting is not a claim that arbitrary shell tools cannot bypass it.

Tests verify run-header injection, capability refusal, response forwarding,
and environment separation. `tests/native_sdk_smoke.py` is the explicit native
SDK integration check and is not part of default pytest.
