# OpenHands

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

The binding includes stdio MCP registration metadata; apply it using the
configuration mechanism of the installed OpenHands release. Proxy variables
and the world CA must be configured together when shell networking is needed.
Use the fenced deployment for enforced isolation. A cooperative environment
setting is not a claim that arbitrary shell tools cannot bypass it.

Tests verify run-header injection, capability refusal, response forwarding,
and environment separation. A full OpenHands harness execution is not part of
the deterministic implementation tests.
