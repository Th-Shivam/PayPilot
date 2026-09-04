"""Typed application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """Raised when the process cannot safely start with its configuration."""


class Settings(BaseSettings):
    """Runtime configuration for local and deployed environments.

    Local development deliberately has safe, non-secret defaults. Production
    startup fails with a list of missing variable names, never their values.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "PayPilot API"
    app_env: Literal["local", "test", "staging", "production"] = "local"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    frontend_origin: str = "http://localhost:5173"
    allowed_origins: str = "http://localhost:5173"

    groq_api_key: SecretStr | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    groq_fallback_model: str = "llama-3.1-8b-instant"
    groq_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    agent_max_steps: int = Field(default=8, ge=1, le=8)
    embedding_model: str = "all-MiniLM-L6-v2"
    similarity_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    similarity_match_count: int = Field(default=3, ge=1, le=20)
    supabase_url: str | None = None
    supabase_anon_key: SecretStr | None = None
    supabase_service_role_key: SecretStr | None = None
    logfire_token: SecretStr | None = None

    def validate_for_runtime(self) -> None:
        """Require integrations before a production server can start.

        This is intentionally separate from Pydantic validation. Pydantic's
        validation error includes the original input mapping, which can expose
        secret constructor values in tracebacks.
        """

        if self.app_env != "production":
            return

        required = {
            "GROQ_API_KEY": self.groq_api_key,
            "SUPABASE_URL": self.supabase_url,
            "SUPABASE_ANON_KEY": self.supabase_anon_key,
            "SUPABASE_SERVICE_ROLE_KEY": self.supabase_service_role_key,
            "LOGFIRE_TOKEN": self.logfire_token,
        }
        missing = [name for name, value in required.items() if not self._has_value(value)]
        if missing:
            raise ConfigurationError(
                "Missing required production configuration: " + ", ".join(missing)
            )

    @staticmethod
    def _has_value(value: object | None) -> bool:
        if value is None:
            return False
        if isinstance(value, SecretStr):
            return bool(value.get_secret_value().strip())
        return bool(str(value).strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one validated settings instance for the process."""

    settings = Settings()
    settings.validate_for_runtime()
    return settings
