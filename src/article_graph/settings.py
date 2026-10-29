"""Environment configuration, validated before the application is served."""

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Limits(BaseModel):
    """Resource budgets; consumers must reject overflow, never truncate input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    upload_bytes: int = Field(default=2_097_152, gt=0)
    pdf_pages: int = Field(default=20, gt=0)
    page_pixels: int = Field(default=4_000_000, gt=0)
    chunks: int = Field(default=20, gt=0)
    llm_calls: int = Field(default=20, gt=0)
    processing_seconds: int = Field(default=300, gt=0)
    provider_timeout_seconds: int = Field(default=30, gt=0)
    job_attempts: int = Field(default=3, gt=0)
    retry_backoff_seconds: int = Field(default=2, gt=0)
    characters: int = Field(default=100, gt=0)
    relations: int = Field(default=200, gt=0)
    evidence_per_relation: int = Field(default=5, gt=0)
    page_default: int = Field(default=20, gt=0)
    page_max: int = Field(default=100, gt=0)
    access_ttl_seconds: int = Field(default=900, gt=0)
    refresh_ttl_seconds: int = Field(default=86_400, gt=0)

    @model_validator(mode="after")
    def pagination_bounds(self) -> Limits:
        if self.page_default > self.page_max:
            raise ValueError("page_default must not exceed page_max")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARTICLE_GRAPH_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="forbid",
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="Article Graph", min_length=1)
    app_token: SecretStr = Field(min_length=32)
    limits: Limits = Field(default_factory=Limits)
