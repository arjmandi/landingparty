# The operator application

Start `worldd` and open its control address in a browser, normally
`http://127.0.0.1:7777/`. The application serves its interface and API from the
same origin. The website explains the project; this application runs and observes
your own world. The advanced console remains at `/bridge`.

## Start and follow a run

1. Open **Settings** to check model access and provider configuration. Supply
   credentials through the protected settings flow or the server's environment;
   do not put keys in goals, chat, or workspace files.
2. Choose a supported integration in the **Harness Library**. Its installation
   and connection controls describe what is available on this host. A configured
   integration is separate from a running agent.
3. Create a run with its goal, model, harness, dollar allowance, and time limit.
   The creation flow records these conditions. Connect or start the selected
   harness through the available run controls.
4. Follow the run's timeline, state, costs, open asks, beliefs, and footprint.
   Reading these panels does not call an agent tool, consume its inbox, change
   its beliefs, or purchase a completion.
5. Handle individual asks through their explicit approval or reply controls.
   Stop the run through its stop control when finished. There is no general
   operator chat that silently steers an ongoing agent.

The run list shows actual subject runs; internal measurement and world-accounting
runs are omitted from that product list. Detail views retain recorded conditions,
limits, and the version tuple. Timeline pages use bounded record-sequence cursors,
so refreshing a panel does not append an observation or change what the agent
has read. Missing beliefs or unrecorded token usage appear as unavailable, rather
than as invented records or estimated counts.

Belief views show the current recorded fold, its real observation overlay,
literal entity matches, version history, and existing evidence. They do not ask
the model to explain itself or generate a missing provenance note. Footprint
views show items and observations already recorded for this run; refreshing does
not revisit an external site. Ask views show persisted ticket state without
expiring tickets as a side effect of viewing them.

## Model access and dollars

**API-key mode** uses the world's metered model lane. Its model calls and tool
charges enter the ledger and count against the recorded dollar allowance.
Provider credentials stay with the world; the agent uses its scoped capability.

**Subscription mode** connects supported native clients using the operator's
own client subscription. Subscription model dollars are **unmeasured** in the
application. A missing price is not a zero-dollar completion. The displayed
dollar cap and remaining allowance cover recorded world-tool charges only;
subscription limits and provider quotas remain separate. Actual captured token
usage may be shown when the client reports it, without inferring a dollar price.
This mode is a cooperative development workflow, not a metered research cell.

Billing mode is explicit on each run. A recorded ledger total is distinguished
from a full model-and-tools total; the latter remains unavailable for native
subscription use. Paid post-run measurement is a separate, explicitly budgeted
operation and is never triggered by opening a report or panel.

## Access to the application

On a loopback-only development server without `WORLD_CONTROL_TOKEN`, local
operator access is permitted. When the control token is configured, authenticate
through the application's session flow. The session uses an HttpOnly cookie with
SameSite Strict; the operator token is not an agent capability or a provider key.

For a VPS or another non-loopback deployment, configure `WORLD_CONTROL_TOKEN`
and serve the application through a TLS reverse proxy. Agent, model-lane, and optional
public identity routes retain their own authentication and capability boundaries;
an application session does not become a general agent credential.

Studio's host-native launcher is **cooperative**. The agent runs as the same OS
user as `worldd` and can access that user's private files, including the world
data directory and native login profiles. Separate client profiles and filtering
inherited environment variables do not isolate those files. Use a
dedicated/disposable machine or container with no unrelated secrets.

The [advanced fenced deployment](../deploy/README.md) provides separate
world/agent users, controlled mounts and a restricted network namespace. Keep
its control listener outside that agent namespace. Starting a native harness
from Studio does not create this fenced deployment.

The operator HTTP projections omit provider keys, native authentication material,
run/resume capabilities, and raw captured model requests and responses. Reviewing
an action or accepting a ticket still uses the corresponding protected control
operation; merely loading a view grants no capability.

## Development and research

The application is useful before a scientific study is ready. A working local
run demonstrates that its harness and tools connect. It does not establish a
positive reset test, consent, a calibrated judge, or completed afterlife evidence.

Research mode retains registered conditions, integrity/completeness checks,
stage gates, explicit human and measurement costs, and metered billing. Reports
keep G, V, J, and S separate and preserve missing reasons and all started rows.
The product interface does not turn a development run, a subscription session,
or a software fixture into scientific evidence. See [research](research.md) and
the [design build audit](design-build-audit.md) for those execution requirements.
