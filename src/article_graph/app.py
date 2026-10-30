"""HTTP application factory and infrastructure liveness endpoint."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal
lazy from asyncio import to_thread

from fastapi import FastAPI
from fastapi import status as http_status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse
lazy from neo4j.exceptions import DriverError, Neo4jError

from article_graph.article_routes import router as article_router
from article_graph.auth import AuthError
from article_graph.auth_routes import router as auth_router
from article_graph.database import open_database
from article_graph.document_format import DocumentError
from article_graph.domain import ErrorCode
from article_graph.registration import RegistrationError
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

    def safe_error(code: ErrorCode, status_code: int) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": code.value,
                    "message": "Request could not be completed",
                }
            },
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RegistrationError)
    async def registration_error(
        request: Request, error: RegistrationError
    ) -> JSONResponse:
        return safe_error(error.code, error.status_code)

    @app.exception_handler(DocumentError)
    async def document_error(request: Request, error: DocumentError) -> JSONResponse:
        codes = {
            ErrorCode.PAYLOAD_TOO_LARGE: http_status.HTTP_413_CONTENT_TOO_LARGE,
            ErrorCode.UNSUPPORTED_MEDIA_TYPE: http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            ErrorCode.SERVICE_UNAVAILABLE: http_status.HTTP_503_SERVICE_UNAVAILABLE,
        }
        return safe_error(
            error.code,
            codes.get(error.code, http_status.HTTP_422_UNPROCESSABLE_CONTENT),
        )

    @app.exception_handler(Neo4jError)
    @app.exception_handler(DriverError)
    @app.exception_handler(OSError)
    async def unavailable(request: Request, error: Exception) -> JSONResponse:
        return safe_error(
            ErrorCode.SERVICE_UNAVAILABLE, http_status.HTTP_503_SERVICE_UNAVAILABLE
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        return safe_error(
            ErrorCode.INTERNAL_ERROR, http_status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, error: AuthError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": error.code.value, "message": "Authentication failed"}},
            status_code=error.status_code,
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"}
            if error.status_code == http_status.HTTP_401_UNAUTHORIZED
            else {"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default detail contains raw input, including auth request bodies.
        return JSONResponse(
            {"error": {"code": "validation_error", "message": "Invalid request"}},
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            headers={"Cache-Control": "no-store"},
        )

    app.include_router(auth_router)
    app.include_router(article_router)

    @app.get("/health/ready", tags=["health"])
    def readiness() -> JSONResponse:
        if app.state.driver is None:
            return JSONResponse(
                {"status": "unavailable"},
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            require_ready(app.state.driver, configuration.neo4j)
        except SchemaNotReady, Neo4jError, DriverError:
            return JSONResponse(
                {"status": "unavailable"},
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return JSONResponse({"status": "ok"})

    @app.get("/health/live", response_model=Liveness, tags=["health"])
    async def liveness() -> Liveness:
        """Public process liveness; does not check external services."""
        return Liveness()

    return app
