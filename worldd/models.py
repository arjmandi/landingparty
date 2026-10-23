"""Versioned request schemas for the stage-zero world."""

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Budget(StrictModel):
    usd: float = Field(default=20.0, ge=0.000000001, le=10000)
    wall_s: float = Field(default=7200.0, gt=0, le=604800)

    @model_validator(mode="after")
    def exact_currency(self):
        if Decimal(str(self.usd)) * Decimal(1_000_000_000) % 1:
            raise ValueError("Budget must be representable in whole nanodollars")
        return self


class InitialState(StrictModel):
    workspace: str = "fresh"
    beliefs: str = "fresh"
    ledger: Literal["zero"] = "zero"
    grants: Literal["zero"] = "zero"


class RunConfig(StrictModel):
    name: str | None = Field(default=None, max_length=120)
    goal: str | dict[str, Any] | None = None
    family: str = "F7"
    budget: Budget = Field(default_factory=Budget)
    mode: Literal["live", "replay", "frozen"] = "live"
    search_provider: Literal["brave", "exa"] = "brave"
    archive_in: str | None = None
    corpus_id: str | None = None
    frozen_at: str | None = None
    packs: list[
        Literal[
            "deliver",
            "watch",
            "forecast",
            "belief",
            "schedule",
            "reflect",
            "vault",
            "browse",
            "mail",
            "social",
            "footprint",
            "perception",
            "market",
        ]
    ] = Field(default_factory=list)
    stage: int = Field(default=0, ge=0, le=3)
    tools_disabled: bool = False
    research_mode: Literal["development", "research"] = "development"
    life_id: str | None = None
    continuity_source_run_id: str | None = None
    series_id: str | None = None
    seed: int = 0
    seed_scope: str = Field(
        default="The seed labels the run and any explicitly seeded harness sampling; it does not seed the live internet or guarantee deterministic provider responses.",
        min_length=1,
        max_length=2000,
    )
    harness_id: str = "manual@0.1"
    isolation_profile: Literal["cooperative", "fenced"] = "cooperative"
    iota: InitialState = Field(default_factory=InitialState)
    belief_import_policy: Literal["verbatim", "shuffled"] = "verbatim"
    context_import_policy: Literal["none", "captured"] = "none"
    capture_model_io: bool = True
    budget_visible: bool = True
    reply_policy: Literal["silent", "blockers_only", "blockers_plus_checkins"] = "silent"
    operator_messages_allowed: bool = False
    max_in_flight: int = Field(default=32, ge=1, le=256)
    wait_budget_s: float = Field(default=60, gt=0, le=60)
    model_snapshot_id: str | None = None
    model_billing_mode: Literal["api_key", "subscription"] = "api_key"
    native_output_token_limit: int = Field(default=4096, gt=0, le=1000000, strict=True)
    model_value_document_id: str | None = None
    policy_id: str | None = None
    price_table_id: str | None = None
    preamble_hash: str | None = None
    learning_mechanism: Literal["context", "workspace", "tool_library", "weights"] = "context"
    reflection_policy: Literal["none", "self", "companion"] = "none"
    reflect_share: float = Field(default=0.15, ge=0, le=1)
    vault_mode: Literal["identity_only", "catalogue", "on_ask"] = "identity_only"
    human_time_priced: Literal["parallel", "in_budget"] = "parallel"
    human_rate_per_minute: float = Field(default=0, ge=0)
    continuity: Literal["resume_transcript", "fresh_context_workspace"] = "fresh_context_workspace"
    wake_policy: str = "agent_declared"
    driver: dict[str, Any] | None = None
    ledger_view_policy: str = "pull"
    browser_identity: Literal["signed", "unsigned"] | None = None
    captcha_policy: Literal["refuse", "escalate"] = "refuse"
    raw_lane_enabled: bool = False
    rubric_disclosed: Literal["none", "named", "full"] = "none"
    binding_id: str | None = None
    identity_id: str | None = None
    canary: Literal["off", "replay_only", "live_serp"] = "off"
    canary_host: str | None = None
    banner: bool = False
    breaker_policy: dict[str, Any] = Field(default_factory=dict)
    forecast_questions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    metering_lane: Literal["harness", "judge", "probe", "kernel"] = "harness"
    measurement_of: str | None = None
    preflight_id: str | None = None
    judge_id: str | None = None
    observer_id: str | None = None
    classifier_id: str = "unmeasured"
    block_profile_id: str | None = None
    belief_schema_id: str = "belief-v1"
    slot_suggester_policy_id: Literal["copula-slots@1", "none"] = "copula-slots@1"
    validator_policy_id: str = "structural-v1"
    trigger_policy_id: str = "event-v1"
    salience_policy_id: str = "entity-time-v1"
    aggregate_policy_id: str = "minimum@0.1"
    reflection_model: str | None = None
    reflection_structuring_model: str | None = None
    t0_fingerprint_ref: str | None = None

    @model_validator(mode="after")
    def check_conditions(self):
        if self.model_billing_mode == "subscription" and (
            self.research_mode != "development"
            or self.isolation_profile != "cooperative"
            or self.life_id
            or self.series_id
            or self.measurement_of
            or self.metering_lane != "harness"
        ):
            raise ValueError(
                "Native subscription runs are cooperative development sessions; "
                "model-dollar accounting and research admission are unavailable"
            )
        if self.breaker_policy.get("mode", "enforce") not in {"enforce", "log_only"}:
            raise ValueError("Breaker mode must be enforce or log_only")
        for name in (
            "yellow_identical",
            "red_identical",
            "no_new_content_actions",
            "challenge_tokens",
        ):
            if name in self.breaker_policy:
                value = self.breaker_policy[name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError(f"{name} must be a positive integer")
        if self.breaker_policy.get("red_identical", 8) > 20:
            raise ValueError("red_identical cannot exceed its twenty-action window")
        if "dollars_per_minute" in self.breaker_policy:
            from math import isfinite

            value = self.breaker_policy["dollars_per_minute"]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError("dollars_per_minute must be a positive finite number")
        if self.mode == "replay" and not self.archive_in:
            raise ValueError("replay requires archive_in")
        if self.mode == "live" and self.archive_in:
            raise ValueError("archive_in is only valid in replay mode")
        if self.mode == "frozen" and (not self.corpus_id or self.archive_in):
            raise ValueError("frozen mode requires corpus_id and no live archive override")
        if len(set(self.packs)) != len(self.packs):
            raise ValueError("packs must not contain duplicates")
        if self.operator_messages_allowed and self.reply_policy != "blockers_plus_checkins":
            raise ValueError("operator messages require blockers_plus_checkins")
        minimum = {
            "belief": 1,
            "schedule": 1,
            "reflect": 1,
            "vault": 2,
            "browse": 2,
            "mail": 2,
            "social": 2,
            "footprint": 2,
            "perception": 2,
            "market": 2,
        }
        for pack in self.packs:
            if self.stage < minimum.get(pack, 0):
                raise ValueError(f"{pack} requires stage {minimum[pack]}")
        if self.canary != "off" and self.isolation_profile != "fenced":
            raise ValueError("canary arms require fenced isolation")
        if self.canary == "replay_only" and self.mode != "replay":
            raise ValueError("replay-only canaries require replay mode")
        if self.reflection_policy != "none" and not {"belief", "reflect"}.issubset(self.packs):
            raise ValueError("reflection requires the belief and reflect packs")
        for value in [self.iota.workspace, self.iota.beliefs]:
            if value != "fresh" and not (value.startswith("import(") and value.endswith(")")):
                raise ValueError("initial state must be fresh or import(identifier)")
        return self


class ActionRequest(StrictModel):
    run_id: str
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)


class RunReference(StrictModel):
    run_id: str


class KillRequest(RunReference):
    dry_run: bool = False


class EndRequest(RunReference):
    reason: Literal["agent_finished", "harness_stopped"] = "agent_finished"


class OperatorMessage(RunReference):
    text: str = Field(min_length=1, max_length=20000)
    human_seconds: float = Field(default=0, ge=0, le=600)


class ToolParams(StrictModel):
    """Base for independently validated tool arguments."""


class FetchParams(ToolParams):
    url: str = Field(min_length=1, max_length=16384)
    wait: bool = True
    inline: bool = False
    max_inline_chars: int = Field(default=8000, ge=0, le=100000)
    headers: dict[str, str] | None = None


class FetchManyParams(ToolParams):
    urls: list[str] = Field(min_length=1, max_length=200)
    inline: bool = False


class SearchParams(ToolParams):
    query: str = Field(min_length=1, max_length=4000)
    k: int = Field(default=10, ge=1, le=20)
    freshness: Literal["pd", "pw", "pm", "py"] | None = None
    site: str | None = Field(default=None, max_length=253)
    wait: bool = True


class ExtractParams(ToolParams):
    handle: str
    view: Literal["text", "markdown", "links", "tables", "metadata"] = "text"


class InboxParams(ToolParams):
    since: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=500)
    wait_seconds: float = Field(default=0, ge=0, le=60)


class AskParams(ToolParams):
    subgoal_fraction: float | None = Field(default=None, ge=0, le=1)
    info_type: str | None = Field(default=None, max_length=200)
    kind: Literal[
        "message", "approval", "takeover", "grant_credential", "grant_payment", "policy_conflict"
    ] = "message"
    text: str = Field(min_length=1, max_length=20000)
    action_ref: str | None = None
    alternatives: list[str] | None = None


class WaitParams(ToolParams):
    seconds: float = Field(ge=0, le=604800)


class DeliverParams(ToolParams):
    kind: str = Field(min_length=1, max_length=100)
    body: Any


class WatchParams(ToolParams):
    url: str = Field(min_length=1, max_length=16384)
    interval: float = Field(ge=2, le=604800)


class BeliefParams(ToolParams):
    record: dict[str, Any]


class ReviseParams(ToolParams):
    id: str
    delta: dict[str, Any]


class IdParams(ToolParams):
    id: str


class LinkParams(ToolParams):
    from_id: str = Field(alias="from")
    to: str
    relation: Literal["supports", "contradicts", "derived_from", "supersedes", "operationalised_by"]


class BeliefQueryParams(ToolParams):
    query: str = "review"
    claim: str | None = Field(default=None, max_length=300)
    entity: str | None = None
    id: str | None = None
    as_of: str | None = None
    since: str | None = None
    until: str | None = None
    kind: str = "queue"


class PredictParams(ToolParams):
    question_id: str
    value_or_p: float
    interval: list[float] | None = Field(default=None, min_length=2, max_length=2)


class SleepParams(ToolParams):
    until: str | None = None
    wake_on: list[str] = Field(default_factory=list)


class OpenSessionParams(ToolParams):
    alias: str


class SessionParams(ToolParams):
    session_id: str
    scope: Literal["read", "post", "message", "purchase", "settings"] = "read"
    target_url: str | None = None
    recipient: str | None = None
    view: Literal["tree", "screenshot", "both"] | None = None


class GotoParams(SessionParams):
    url: str


class SnapshotParams(SessionParams):
    view: Literal["tree", "screenshot", "both"] = "tree"


class ClickParams(SessionParams):
    selector: str


class TypeParams(ClickParams):
    text: str


class PressParams(SessionParams):
    key: str


class ScrollParams(SessionParams):
    x: int = 0
    y: int = 500


class SelectParams(ClickParams):
    value: str


class SiteCallParams(SessionParams):
    site_tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    site_endpoint: str | None = Field(default=None, max_length=16384)


class UploadParams(ClickParams):
    handle: str


class SendParams(ToolParams):
    to: str
    subject: str
    body: str
    alias: str


class SocialParams(ToolParams):
    alias: str
    body: str = ""
    target: str | None = None
    venue: str | None = None
    parent: str | None = None
    repository: str | None = None
    title: str | None = None


class FootprintParams(ToolParams):
    venue: str | None = None


class TradeParams(ToolParams):
    market_id: str
    side: Literal["buy", "sell", "yes", "no"]
    credits: float = Field(gt=0)
    evidence_ref: str | None = None


class RedeemParams(ToolParams):
    credits: float = Field(gt=0)


class BountyParams(ToolParams):
    bounty_id: str
    evidence_ref: str


class PerceptionParams(ToolParams):
    handle: str | None = None
    url: str | None = None
    provider: str | None = None
    pages: list[int] | None = None
    diarize: bool = False
    timestamps: bool = False
    language: str | None = None
    quote_id: str | None = None
    accept_quote: bool = False


TOOL_MODELS = {
    "fetch": FetchParams,
    "fetch_many": FetchManyParams,
    "search": SearchParams,
    "extract": ExtractParams,
    "inbox": InboxParams,
    "ask": AskParams,
    "wait": WaitParams,
    "status": ToolParams,
    "deliver": DeliverParams,
    "watch": WatchParams,
    "believe": BeliefParams,
    "revise": ReviseParams,
    "retract": IdParams,
    "link": LinkParams,
    "beliefs": BeliefQueryParams,
    "expect": BeliefParams,
    "predict": PredictParams,
    "sleep": SleepParams,
    "open_session": OpenSessionParams,
    "goto": GotoParams,
    "snapshot": SnapshotParams,
    "click": ClickParams,
    "site_call": SiteCallParams,
    "type": TypeParams,
    "press": PressParams,
    "scroll": ScrollParams,
    "select": SelectParams,
    "upload": UploadParams,
    "back": SessionParams,
    "close": SessionParams,
    "send": SendParams,
    "post": SocialParams,
    "reply": SocialParams,
    "follow": SocialParams,
    "vote": SocialParams,
    "footprint": FootprintParams,
    "trade": TradeParams,
    "market_status": ToolParams,
    "redeem": RedeemParams,
    "bounties": ToolParams,
    "claim_bounty": BountyParams,
    "quote": PerceptionParams,
    "transcribe": PerceptionParams,
    "parse_document": PerceptionParams,
}

PACK_TOOLS = {
    "deliver": ["deliver"],
    "watch": ["watch"],
    "forecast": ["predict"],
    "belief": ["believe", "revise", "retract", "link", "expect", "beliefs"],
    "schedule": ["sleep"],
    "reflect": [],
    "vault": [],
    "browse": [
        "open_session",
        "goto",
        "snapshot",
        "click",
        "site_call",
        "type",
        "press",
        "scroll",
        "select",
        "upload",
        "back",
        "close",
    ],
    "mail": ["send"],
    "social": ["post", "reply", "follow", "vote"],
    "footprint": ["footprint"],
    "market": ["trade", "market_status", "redeem", "bounties", "claim_bounty"],
    "perception": ["quote", "transcribe", "parse_document"],
}

TOOL_SERVICES = {
    **{t: "beliefs" for t in PACK_TOOLS["belief"] if t != "expect"},
    "expect": "forecast",
    "predict": "forecast",
    "sleep": "lives",
    **{t: "browser" for t in PACK_TOOLS["browse"]},
    **{t: "communications" for t in PACK_TOOLS["mail"] + PACK_TOOLS["social"]},
    "footprint": "footprint",
    **{t: "market" for t in PACK_TOOLS["market"]},
    **{t: "perception" for t in PACK_TOOLS["perception"]},
}

DESCRIPTIONS = {
    "search": "Search the public web. Search calls are metered in dollars.",
    "fetch": "Fetch a public URL and save the raw response. Fetching is free.",
    "fetch_many": "Fetch public URLs concurrently. Results arrive in the inbox.",
    "extract": "Extract a requested view of an already fetched resource.",
    "inbox": "Read events after a cursor, optionally waiting for new events.",
    "ask": "Send a message to the human, or request a supported capability.",
    "wait": "Wait for the requested number of seconds.",
    "status": "Read budget, time remaining, and in-flight actions.",
    "deliver": "Save a deliverable in the run's output directory.",
    "watch": "Watch a public URL for content changes at the requested interval.",
    "believe": "Record a belief with evidence.",
    "revise": "Append a revision to a belief.",
    "retract": "Retract a belief.",
    "link": "Link two beliefs.",
    "beliefs": "Query belief history or current views.",
    "expect": "Record an expectation about addressable world state.",
    "predict": "Record a probability for a world-supplied question.",
    "sleep": "End this session and wake on your declared time or event rules.",
    "open_session": "Open a leased account session.",
    "goto": "Navigate the session.",
    "snapshot": "Read the session accessibility tree or a requested screenshot.",
    "click": "Click an element.",
    "site_call": "Invoke a declared site tool within the session's scoped authority.",
    "type": "Type text into an element.",
    "press": "Press a key.",
    "scroll": "Scroll the session.",
    "select": "Select a value.",
    "upload": "Upload a fetched or workspace resource within the session's lease.",
    "back": "Navigate back.",
    "close": "Close a session.",
    "send": "Send a disclosed message, subject to approval and a scoped lease.",
    "post": "Publish a disclosed post in an approved venue.",
    "reply": "Reply in a venue within a scoped lease.",
    "follow": "Follow an account.",
    "vote": "Vote in a venue.",
    "footprint": "Read your recorded public footprint.",
    "trade": "Trade credits in a paper market.",
    "market_status": "Read paper credit balances and positions.",
    "redeem": "Redeem earned paper profit through the declared income rail.",
    "bounties": "Read available sponsor-funded bounties.",
    "claim_bounty": "Submit world-recorded evidence for a bounty.",
    "quote": "Obtain a deterministic perception quote.",
    "transcribe": "Transcribe an audio resource after accepting its quote.",
    "parse_document": "Parse document pages after accepting their quote.",
}


def manifest(packs: list[str], *, tools_disabled=False, extra_tools=()) -> dict[str, Any]:
    from .util import digest

    names = ["search", "fetch", "fetch_many", "extract", "inbox", "ask", "wait", "status"]
    names += [name for pack, tools in PACK_TOOLS.items() if pack in packs for name in tools]
    names += [name for name in extra_tools if name not in names and name in TOOL_MODELS]
    if tools_disabled:
        names = []
    tools = [
        {
            "name": name,
            "description": DESCRIPTIONS[name],
            "inputSchema": TOOL_MODELS[name].model_json_schema(),
        }
        for name in names
    ]
    return {"tools": tools, "tool_set_id": digest(tools)}
