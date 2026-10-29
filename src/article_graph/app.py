"""HTTP application factory and infrastructure liveness endpoint."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal
lazy from asyncio import to_thread

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse
lazy from neo4j.exceptions import DriverError, Neo4jError

from article_graph.auth import AuthError
from article_graph.auth_routes import router as auth_router
from article_graph.database import open_database
from article_graph.schema import SchemaNotReady, require_ready
from article_graph.settings import Settings


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # A driver is created in each process, never inherited across worker forks.
        context = open_database(configuration.neo4j)
        driver = await to_thread(context.__enter__)
        try:
            await to_thread(require_ready, driver, configuration.neo4j)
            app.state.driver = driver
            yield
        finally:
            app.state.driver = None
            await to_thread(context.__exit__, None, None, None)

    app = FastAPI(title=configuration.app_name, lifespan=lifespan)
    app.state.settings = configuration
    app.state.driver = None

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, error: AuthError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": error.code.value, "message": "Authentication failed"}},
            status_code=error.status_code,
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"}
            if error.status_code == 401
            else {"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default detail contains raw input, including auth request bodies.
        return JSONResponse(
            {"error": {"code": "validation_error", "message": "Invalid request"}},
            status_code=422,
            headers={"Cache-Control": "no-store"},
        )

    app.include_router(auth_router)

    @app.get("/health/ready", tags=["health"])
    def readiness() -> JSONResponse:
        if app.state.driver is None:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        try:
            require_ready(app.state.driver, configuration.neo4j)
        except SchemaNotReady, Neo4jError, DriverError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/health/live", response_model=Liveness, tags=["health"])
    async def liveness() -> Liveness:
        """Public process liveness; does not check external services."""
        return Liveness()

    return app
