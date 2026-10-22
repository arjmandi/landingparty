# world-mcp

The `world-mcp` console command is installed by the root Python package. Its
implementation is `worldd/mcp.py`; this directory records the design's stdio
bridge boundary without creating a second implementation.

```bash
WORLD_RUN_ID=r_EXAMPLE world-mcp
```

Supply the created run's capability as `WORLD_RUN_TOKEN`. Set `WORLD_BASE_URL`
or pass `--url` for a different agent listener (default port 7778). Standard
output contains newline-delimited JSON-RPC only. The bridge exposes the
current run's manifest, forwards calls to `/v1/act`, and serves
`world://inbox/<run_id>` as a read-only JSON resource. Cursor-based pagination
and long-polling remain available through the world's inbox tool. Independent
calls execute concurrently; blocked replies remain ordinary observations.
Known read tools carry MCP `readOnlyHint` metadata so native harnesses can
dispatch independent reads concurrently. Contact and write tools do not
receive that hint.
