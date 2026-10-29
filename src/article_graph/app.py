"""HTTP application factory and infrastructure liveness endpoint."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from article_graph.settings import Settings


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings if settings is not None else Settings()
    app = FastAPI(title=configuration.app_name)
    app.state.settings = configuration

    @app.get("/health/live", response_model=Liveness, tags=["health"])
    async def liveness() -> Liveness:
        """Public process liveness; does not check external services."""
        return Liveness()

    return app
