# B1 reference loop

```bash
python adapters/reference-loop/run.py goals/14-audit/run.json --model MODEL_FROM_PRICE_TABLE
```

The loop creates a run, uses its exact mission briefing, translates only the
enabled manifest into OpenAI Chat function tools, and sends every model call
through `http://127.0.0.1:8081/openai/v1` with its `X-World-Run-Id` header.
Use `--world-url` and `--lane-url` for a different local deployment.
`--max-tokens` bounds a single model request; `--max-steps` is a harness stop
policy and therefore part of this adapter's identity.

Tool calls from one model turn run concurrently and return in the model's
original tool-call order. Tickets are passed through without polling or
inventing a new action. The model decides whether to read the inbox. There are
no reflection, prediction, journal, or continuation rituals. The run is sealed
on a final response, harness-step limit, or exception. Provider usage is priced
by the world's pinned table, so an unpriced model is refused.

The tests use mock provider/world transports, including native parallel calls
and a stopped harness. A live run requires a supported provider and a key
configured for the model lane; the script never calls a provider directly.
