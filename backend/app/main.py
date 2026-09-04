"""PayPilot FastAPI application entrypoint."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from .config import Settings, get_settings


class HealthResponse(BaseModel):
    """Stable response returned by the health check."""

    status: Literal["ok"]
    service: str
    environment: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API application, allowing tests to inject settings."""

    runtime_settings = settings or get_settings()
    runtime_settings.validate_for_runtime()
    application = FastAPI(
        title=runtime_settings.app_name,
        version="0.1.0",
        description="PayPilot transaction reconciliation API",
    )

    @application.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=runtime_settings.app_name,
            environment=runtime_settings.app_env,
        )

    return application


app = create_app()
