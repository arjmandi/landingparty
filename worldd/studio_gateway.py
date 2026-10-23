"""One HTTP origin with separate operator, agent, model and public boundaries."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .security import create_agent_app, enable_browser_sessions


def create_studio_gateway(control_app, *, public=False):
    """Mount scoped capabilities outside the control app's operator middleware.

    Mounted Starlette applications do not run their own lifespan automatically,
    so the wrapper owns the original control application's lifespan exactly once.
    The model mount always requires a run capability, including in local mode.
    """
    enable_browser_sessions(control_app)

    @asynccontextmanager
    async def lifespan(app):
        async with control_app.router.lifespan_context(control_app):
            yield

    app = FastAPI(
        title="Landing Party studio gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.world = control_app.state.world
    app.state.control_app = control_app
    model = FastAPI(
        title="Landing Party scoped model lane", docs_url=None, redoc_url=None, openapi_url=None
    )
    model.state.require_run_token = True
    model.include_router(control_app.state.lane_router)
    app.mount("/agent", create_agent_app(control_app.state.world))
    app.mount("/model", model)
    if public:
        from .public_identity import create_public_app

        app.mount("/public", create_public_app(control_app.state.world))
    app.mount("/", control_app)
    return app
