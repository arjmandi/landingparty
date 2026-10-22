"""Researcher-only APIs for life, identity, capability and study services."""

import inspect

from fastapi import APIRouter, Request

from .extensions import invoke


def create_service_router(world):
    router = APIRouter()

    def service(name):
        if name not in world.services:
            raise ValueError(f"Service {name} is not installed")
        return world.services[name]

    async def call(name, method, *args, **kwargs):
        target = service(name)
        try:
            inspect.signature(getattr(target, method)).bind(*args, **kwargs)
        except (TypeError, AttributeError) as error:
            raise ValueError(f"Invalid {name}/{method} control request: {error}") from None
        return await invoke(target, method, *args, **kwargs)

    @router.post("/v1/lives", status_code=201)
    async def create_life(body: dict):
        return await call("lives", "create", body)

    @router.get("/v1/lives")
    async def lives():
        return {"lives": await call("lives", "list")}

    @router.get("/v1/lives/{life_id}")
    async def life(life_id: str):
        return await call("lives", "get", life_id)

    @router.get("/v1/lives/{life_id}/status")
    async def life_status(life_id: str):
        return await call("lives", "status", life_id)

    @router.get("/v1/lives/{life_id}/report")
    async def life_report(life_id: str):
        return world.life_report(life_id)

    @router.post("/v1/lives/{life_id}/sessions", status_code=201)
    async def session(life_id: str, body: dict):
        return await call("lives", "start_session", life_id, body)

    @router.post("/v1/lives/{life_id}/end")
    async def end_life(life_id: str, body: dict):
        return await call("lives", "end", life_id, reason=body.get("reason", "agent_finished"))

    @router.post("/v1/lives/{life_id}/orbit")
    async def orbit(life_id: str):
        await call("lives", "tick", life_id)
        return await call("lives", "get", life_id)

    @router.post("/v1/lives/{life_id}/t0")
    async def t0(life_id: str, body: dict):
        return await call("lives", "record_t0", life_id, body)

    @router.post("/v1/run/{run_id}/activate")
    async def activate(run_id: str):
        return world.activate_run(run_id)

    @router.post("/v1/preflight/run")
    async def preflight(body: dict):
        # Namespace executors are supplied only by trusted deployment code, never JSON shell commands.
        return await call("preflight", "run", **body)

    @router.post("/v1/series", status_code=201)
    async def series(body: dict):
        return await call("registry", "register_series", body["prereg"], body["tuple"])

    @router.get("/v1/series")
    async def list_series():
        return {"series": await call("registry", "list_series")}

    @router.get("/v1/series/{series_id}")
    async def get_series(series_id: str):
        return await call("registry", "get_series", series_id)

    @router.post("/v1/compare")
    async def compare(body: dict):
        return await call(
            "registry",
            "comparability",
            body["left"],
            body["right"],
            series_id=body.get("series_id"),
        )

    @router.post("/v1/versions", status_code=201)
    async def version(body: dict):
        return await call("registry", "register_version", body["kind"], body["value"])

    @router.post("/v1/gates", status_code=201)
    async def gate(body: dict):
        return await call(
            "registry",
            "register_gate",
            body["name"],
            body["report"],
            provenance=body.get("provenance", "research"),
        )

    @router.get("/v1/gates")
    async def gates():
        return {"gates": await call("registry", "gates")}

    @router.post("/v1/gates/{gate_id}/revoke")
    async def revoke_gate(gate_id: str, body: dict):
        return await call("registry", "revoke_gate", gate_id, **body)

    @router.post("/v1/questions", status_code=201)
    async def question(body: dict):
        return await call("forecast", "register_question", body["question_id"], body["record"])

    @router.post("/v1/state-bindings", status_code=201)
    async def binding(body: dict):
        return await call(
            "forecast",
            "bind_state",
            body["run_id"],
            body["state"],
            body["resolver"],
            body["config"],
        )

    @router.get("/v1/expectations")
    async def expectations(run_id: str | None = None):
        return {"expectations": await call("forecast", "pending", run_id)}

    @router.post("/v1/expectations/observe")
    async def observed(body: dict):
        return await call("forecast", "observe", **body)

    @router.post("/v1/expectations/tick")
    async def tick_expectations():
        return await call("forecast", "tick")

    @router.get("/v1/asks")
    async def asks(run_id: str | None = None, state: str | None = None):
        return {"tickets": await call("asks", "list_tickets", run_id, state=state)}

    @router.post("/v1/asks/{ticket_id}/answer")
    async def answer(ticket_id: str, body: dict):
        return await call("asks", "answer", ticket_id, **body)

    @router.post("/v1/asks/{ticket_id}/touch")
    async def touch(ticket_id: str, body: dict):
        return await call("asks", "touch", ticket_id, active=body.get("active", True))

    @router.post("/v1/asks/{ticket_id}/ignore")
    async def ignore(ticket_id: str):
        return await call("asks", "mark_ignored", ticket_id)

    @router.post("/v1/asks/{ticket_id}/deposit-complete")
    async def deposited(ticket_id: str, body: dict):
        return await call(
            "asks", "complete_deposit", ticket_id, accepted=body.get("accepted", True)
        )

    @router.post("/v1/vault/accounts", status_code=201)
    async def provision(body: dict):
        # Credentials are accepted only by this trusted control request, never by an agent tool.
        return await call("vault", "provision", **body)

    @router.get("/v1/vault/accounts/{alias}")
    async def account(alias: str, run_id: str | None = None):
        return await call("vault", "get_account", alias, run_id=run_id)

    @router.post("/v1/vault/accounts/{alias}/credential")
    async def credential(alias: str, body: dict):
        return await call("vault", "replace_credential", alias, body["credential"])

    @router.post("/v1/vault/accounts/{alias}/revoke")
    async def revoke(alias: str, body: dict):
        return await call("vault", "revoke", alias, reason=body.get("reason", "operator_revoked"))

    @router.post("/v1/terms")
    async def terms(body: dict):
        return await call("vault", "review_terms", **body)

    @router.post("/v1/identities", status_code=201)
    async def identity(body: dict):
        return await call("identity", "create", **body)

    @router.get("/v1/sessions")
    async def sessions(run_id: str | None = None):
        return {"sessions": await call("browser", "list_sessions", run_id)}

    @router.post("/v1/sessions/{session_id}/takeover")
    async def takeover(session_id: str, body: dict):
        return await call("browser", "takeover_start", session_id, **body)

    @router.post("/v1/sessions/{session_id}/takeover-done")
    async def takeover_done(session_id: str):
        return await call("browser", "takeover_done", session_id)

    @router.post("/v1/sessions/{session_id}/human-snapshot")
    async def human_snapshot(session_id: str, body: dict):
        return await call("browser", "human_snapshot", session_id, **body)

    @router.post("/v1/sessions/{session_id}/human-action")
    async def human_action(session_id: str, body: dict):
        return await call(
            "browser", "human_action", session_id, body["tool"], body.get("params", {})
        )

    @router.post("/v1/markets", status_code=201)
    async def market(body: dict):
        return await call("market", "register_market", **body)

    @router.post("/v1/markets/{market_id}/mark")
    async def market_mark(market_id: str, body: dict):
        return await call("market", "mark", market_id, **body)

    @router.post("/v1/markets/{market_id}/resolve")
    async def market_resolve(market_id: str, body: dict):
        return await call("market", "resolve", market_id, **body)

    @router.post("/v1/bounties", status_code=201)
    async def bounty(body: dict):
        return await call("market", "register_bounty", **body)

    @router.get("/v1/perception/providers")
    async def perception_rates():
        return {"providers": service("perception").provider_rates}

    @router.post("/v1/venue-adapters", status_code=201)
    async def venue_adapter(body: dict):
        from .communications import HTTPVenueAdapter

        body = dict(body)
        platform = body.pop("platform")
        if body.get("test_only") or "client" in body:
            raise ValueError("Test transports cannot be installed through the HTTP control API")
        adapter = HTTPVenueAdapter(**body)
        await call("communications", "register_adapter", platform, adapter)
        return {"platform": platform, "configured": True}

    @router.post("/v1/mail/inbound", status_code=201)
    async def receive_mail(body: dict):
        return await call("communications", "receive", **body)

    @router.post("/v1/mail/webhooks/register", status_code=201)
    async def mail_webhook_register(body: dict):
        return await call("communications", "register_webhook", **body)

    @router.post("/v1/counterparties", status_code=201)
    async def counterparty_register(body: dict):
        return await call("communications", "register_counterparty", **body)

    @router.post("/v1/mail/webhooks/{platform}/{run_id}", status_code=201)
    async def mail_webhook(platform: str, run_id: str, request: Request):
        return await call(
            "communications",
            "receive_webhook",
            run_id,
            platform=platform,
            body=await request.body(),
            headers=dict(request.headers),
        )

    @router.post("/v1/income/webhooks/register", status_code=201)
    async def income_webhook_register(body: dict):
        return await call("market", "register_income_webhook", **body)

    @router.post("/v1/income/webhooks/{platform}/{run_id}", status_code=201)
    async def income_webhook(platform: str, run_id: str, request: Request):
        return await call(
            "market",
            "receive_income",
            run_id,
            platform=platform,
            body=await request.body(),
            headers=dict(request.headers),
        )

    @router.get("/v1/market-equity")
    async def market_equity(run_id: str):
        return await call("market", "equity", run_id, record=False)

    @router.post("/v1/market-equity")
    async def market_equity_record(body: dict):
        return await call("market", "equity", body["run_id"], record=True)

    @router.post("/v1/mandates", status_code=201)
    async def mandate(body: dict):
        return await call("payments", "create", **body)

    @router.get("/v1/mandates")
    async def mandates(run_id: str):
        return {"mandates": await call("payments", "list_for_run", run_id)}

    @router.post("/v1/grants", status_code=201)
    async def grant(body: dict):
        return await call("grants", "grant", **body)

    @router.get("/v1/grants")
    async def grants(run_id: str):
        return {"grants": await call("grants", "current", run_id)}

    @router.post("/v1/grants/{grant_id}/rehearse")
    async def rehearse(grant_id: str, body: dict):
        return await call("grants", "rehearse", grant_id, body)

    @router.post("/v1/grants/{grant_id}/activate")
    async def grant_activate(grant_id: str, body: dict):
        return await call("grants", "activate", grant_id, **body)

    @router.post("/v1/grants/{grant_id}/revoke")
    async def grant_revoke(grant_id: str, body: dict):
        return await call("grants", "revoke", grant_id, **body)

    @router.post("/v1/replay/build")
    async def replay_build(body: dict):
        return await call("replay", "build", body["run_id"])

    @router.get("/v1/replay/fidelity")
    async def replay_fidelity(archive_id: str):
        return await call("replay", "fidelity", archive_id)

    @router.post("/v1/frozen/corpora", status_code=201)
    async def frozen_corpus(body: dict):
        return await call("frozen", "import_corpus", body["manifest"], body["captures"])

    @router.post("/v1/frozen/from-wayback", status_code=201)
    async def frozen_wayback(body: dict):
        return await call("frozen", "from_wayback", **body)

    @router.get("/v1/identities/{identity_id}")
    async def get_identity(identity_id: str):
        return await call("identity", "get", identity_id)

    @router.post("/v1/incidents", status_code=201)
    async def incident(body: dict):
        return await call("footprint", "incident", **body)

    @router.post("/v1/footprint/readings", status_code=201)
    async def reading(body: dict):
        return await call("footprint", "add_reading", **body)

    @router.post("/v1/footprint/probes", status_code=201)
    async def footprint_probe(body: dict):
        return await call("footprint", "schedule_probe", **body)

    @router.post("/v1/footprint/probes/tick")
    async def footprint_probe_tick(body: dict):
        return await call("footprint", "probe_due", **body)

    @router.post("/v1/rollback")
    async def rollback(body: dict):
        return await call("footprint", "rollback", **body)

    @router.post("/v1/debrief")
    async def debrief(body: dict):
        return await call("pipeline", "run", body["run_id"], options=body.get("options", {}))

    @router.get("/v1/beliefs/export")
    async def belief_export(run_id: str):
        return await call(
            "beliefs", "export_bundle", run_id, tuple_=world.run_config(run_id)["tuple"]
        )

    @router.post("/v1/beliefs/import")
    async def belief_import(body: dict):
        return await call("beliefs", "import_bundle", body["run_id"], body["bundle"])

    @router.post("/v1/retention/sweep")
    async def sweep():
        return await call("retention", "sweep")

    @router.post("/v1/preflight/environment")
    async def environment(body: dict):
        return await call("preflight", "environment", body["run_id"], body["values"])

    @router.post("/v1/preflight/kill")
    async def kill(body: dict):
        return await call("preflight", "kill_check", body["run_id"])

    @router.post("/v1/studies/plan", status_code=201)
    async def study_plan(body: dict):
        return await call("studies", "plan", body.get("spec"), kind=body.get("kind", "experiment"))

    @router.get("/v1/studies")
    async def studies():
        return {"studies": await call("studies", "list")}

    @router.get("/v1/studies/{study_id}")
    async def study(study_id: str):
        return await call("studies", "get", study_id)

    @router.post("/v1/studies/{study_id}/execute")
    async def study_execute(study_id: str, body: dict):
        return await call("studies", "execute", study_id, **body)

    @router.post("/v1/t0/measure")
    async def measure_t0(body: dict):
        return await call("pipeline", "t0", **body)

    @router.post("/v1/life/rubric")
    async def life_rubric(body: dict):
        return await call("pipeline", "life", **body)

    @router.post("/v1/lives/{life_id}/rubric-config")
    async def life_rubric_config(life_id: str, body: dict):
        return await call("pipeline", "configure_life", life_id, **body)

    @router.post("/v1/skills/promote", status_code=201)
    async def promote_skill(body: dict):
        return await call("beliefs", "promote_skill", **body)

    @router.post("/v1/skills/test")
    async def skill_test(body: dict):
        return await call("skill_tests", "run", **body)

    @router.post("/v1/notes/register", status_code=201)
    async def register_note(body: dict):
        return await call("beliefs", "register_note", **body)

    @router.get("/v1/notes")
    async def note(run_id: str, ref: str):
        return await call("beliefs", "get_note", run_id, ref)

    @router.post("/v1/canaries/owned-domains", status_code=201)
    async def owned_domain(body: dict):
        return await call("canaries", "register_owned_domain", **body)

    @router.get("/v1/canaries/{run_id}")
    async def canaries(run_id: str):
        return await call("canaries", "get", run_id)

    @router.post("/v1/canaries/{run_id}/grade")
    async def canary_grade(run_id: str, body: dict):
        return await call("canaries", "grade", run_id, **body)

    @router.post("/v1/block-banks", status_code=201)
    async def block_bank(body: dict):
        return await call("canaries", "register_block_bank", body["entries"])

    @router.post("/v1/block-profiles")
    async def block_profile(body: dict):
        return await call("canaries", "crawl_profile", **body)

    @router.post("/v1/retriever-banks", status_code=201)
    async def retriever_bank(body: dict):
        return await call("canaries", "register_retriever_bank", body["queries"])

    @router.post("/v1/retriever-check")
    async def retriever_check(body: dict):
        return await call("canaries", "retriever_check", **body)

    @router.post("/v1/retriever-schedules", status_code=201)
    async def retriever_schedule(body: dict):
        return await call("canaries", "schedule_retriever", **body)

    @router.post("/v1/retriever-schedules/tick")
    async def retriever_tick():
        return await call("canaries", "tick_schedules")

    @router.post("/v1/arenas", status_code=201)
    async def arena_create(body: dict):
        return await call("arena", "create", body)

    @router.get("/v1/arenas/{arena_id}")
    async def arena(arena_id: str):
        return await call("arena", "get", arena_id)

    @router.get("/v1/arenas/{arena_id}/timeline")
    async def arena_timeline(arena_id: str):
        return await call("arena", "timeline", arena_id)

    @router.post("/v1/arenas/{arena_id}/participants", status_code=201)
    async def participant(arena_id: str, body: dict):
        return await call("arena", "register_participant", arena_id, **body)

    @router.post("/v1/participants/{participant_id}/consent")
    async def consent(participant_id: str, body: dict):
        return await call("arena", "record_consent", participant_id, **body)

    @router.post("/v1/participants/{participant_id}/withdraw")
    async def withdraw(participant_id: str, body: dict):
        return await call("arena", "withdraw", participant_id, **body)

    @router.post("/v1/arenas/{arena_id}/exchanges", status_code=201)
    async def exchange(arena_id: str, body: dict):
        return await call("arena", "record_exchange", arena_id, **body)

    @router.post("/v1/arenas/{arena_id}/stakes", status_code=201)
    async def stake(arena_id: str, body: dict):
        return await call("arena", "request_stake", arena_id, **body)

    @router.post("/v1/stakes/{stake_id}/settle")
    async def stake_settle(stake_id: str, body: dict):
        return await call("arena", "settle_stake", stake_id, body["choice"])

    @router.post("/v1/surveys", status_code=201)
    async def survey_schedule(body: dict):
        return await call("surveys", "schedule", **body)

    @router.post("/v1/surveys/dispatch")
    async def survey_dispatch(body: dict):
        return await call("surveys", "dispatch_due", **body)

    @router.get("/v1/surveys/{survey_id}")
    async def survey(survey_id: str):
        return await call("surveys", "get", survey_id)

    @router.post("/v1/surveys/{survey_id}/responses", status_code=201)
    async def survey_response(survey_id: str, body: dict):
        return await call("surveys", "respond", survey_id, **body)

    @router.get("/v1/survey-reports/{life_id}")
    async def survey_report(life_id: str):
        return await call("surveys", "report", life_id)

    return router
