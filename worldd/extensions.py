"""Explicit construction order for independently testable world services."""

import importlib
import inspect

SERVICE_MODULES = [
    ("registry", "worldd.registry", "RegistryService"),
    ("lives", "worldd.lives", "LifeService"),
    ("identity", "worldd.identity", "IdentityService"),
    ("asks", "worldd.asks", "AskService"),
    ("vault", "worldd.vault", "VaultService"),
    ("payments", "worldd.approvals", "PaymentMandateLedger"),
    ("beliefs", "worldd.kernel", "BeliefService"),
    ("skill_tests", "worldd.skill_tests", "SkillTestService"),
    ("forecast", "worldd.forecast", "ForecastService"),
    ("reflection", "worldd.reflection", "ReflectionService"),
    ("footprint", "worldd.footprint", "FootprintService"),
    ("incident_reports", "worldd.public_identity", "IncidentReportService"),
    ("workspace_checkpoints", "worldd.checkpoints", "WorkspaceCheckpointService"),
    ("communications", "worldd.communications", "CommunicationsService"),
    ("market", "worldd.market", "MarketService"),
    ("perception", "worldd.perception", "PerceptionService"),
    ("browser", "worldd.browser", "BrowserService"),
    ("retention", "worldd.retention", "RetentionService"),
    ("proxy", "worldd.proxy", "ProxyService"),
    ("replay", "worldd.replay", "ReplayIndex"),
    ("frozen", "worldd.replay", "FrozenCorpusService"),
    ("canaries", "worldd.canaries", "CanaryService"),
    ("grants", "worldd.grants", "GrantService"),
    ("arena", "worldd.arena", "ArenaService"),
    ("surveys", "worldd.surveys", "SurveyService"),
    ("preflight", "worldd.preflight", "PreflightService"),
    ("pipeline", "analysis.pipeline", "PostRunPipeline"),
    ("judge_canaries", "worldd.judge_canaries", "JudgeCanaryService"),
    ("studies", "worldd.studies", "StudyService"),
]


def initialize(world):
    for name, module, classname in SERVICE_MODULES:
        if importlib.util.find_spec(module) is None:
            continue
        service_class = getattr(importlib.import_module(module), classname)
        world.services[name] = service_class(world)
        setattr(world, name, world.services[name])


async def invoke(service, method, /, *args, **kwargs):
    result = getattr(service, method)(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result
