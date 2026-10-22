# ASSAY face

`adapter.py` provides the service-archetype HTTP mapping that an external ASSAY
runner can import: `AssayAdapter.act()` forwards to the core, requests
`wait=true` for search/fetch, and preserves tickets and typed blocks.
`assay_budget()` puts the runner cap above the world's cap by a positive margin.
The world remains the budget authority; fetching costs zero world dollars even
when ASSAY categorizes it as a paid action.

The supplied HTTP client should point at the agent listener (default
`http://127.0.0.1:7778`). `AssayAdapter(client, run_id, run_token=...)` uses the
run capability for both manifest and action requests; alternatively inject
`WORLD_RUN_TOKEN` at launch. Do not give the runner the researcher control
token. Neither the capability nor a provider key belongs in a task prompt.

ASSAY's mandatory prediction remains in its runner policy. This adapter never
sends it to the world. Free reads are inbox, status, extract, and (only once its
stage gate is passed) beliefs. The current stage 0 manifest does not expose
beliefs. The core manifest remains the authority for enabled tools.

The HTTP mapping is covered with mocked transports. No ASSAY SDK is bundled;
an external ASSAY runner invocation and its end-to-end budget behavior have not
been validated yet.
