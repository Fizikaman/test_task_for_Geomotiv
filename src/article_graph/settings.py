"""Environment configuration, validated before the application is served."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Limits(BaseModel):
    """Resource budgets; consumers must reject overflow, never truncate input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    upload_bytes: int = Field(default=2_097_152, gt=0)
    pdf_pages: int = Field(default=20, gt=0)
    page_pixels: int = Field(default=4_000_000, gt=0)
    pdf_total_pixels: int = Field(default=40_000_000, gt=0)
    pdf_dpi: int = Field(default=144, gt=0, le=600)
    pdf_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    pdf_memory_bytes: int = Field(default=536_870_912, gt=0)
    chunks: int = Field(default=20, gt=0)
    chunk_chars: int = Field(default=8_000, gt=0)
    chunk_overlap_chars: int = Field(default=800, ge=0)
    prepared_chars: int = Field(default=160_000, gt=0)
    llm_calls: int = Field(default=20, gt=0)
    llm_concurrency: int = Field(default=2, gt=0)
    llm_input_bytes: int = Field(default=16_000_000, gt=0)
    llm_total_input_bytes: int = Field(default=64_000_000, gt=0)
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
        if self.chunk_overlap_chars >= self.chunk_chars:
            raise ValueError("chunk_overlap_chars must be less than chunk_chars")
        if self.page_default > self.page_max:
            raise ValueError("page_default must not exceed page_max")
        return self


class Neo4jSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARTICLE_GRAPH_NEO4J__",
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    uri: str = "bolt://127.0.0.1:7687"
    username: str = "neo4j"
    password: SecretStr = SecretStr("")
    database: str = "neo4j"
    embedding_dimensions: int = Field(default=1024, ge=1, le=4096)
    schema_timeout_seconds: int = Field(default=60, gt=0)


class OllamaSettings(BaseSettings):
    """Explicit configuration for embedding consumers; never loads a .env file."""

    model_config = SettingsConfigDict(
        env_prefix="ARTICLE_GRAPH_OLLAMA__",
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    base_url: str
    model: str = Field(min_length=1, pattern=r"^\S+:\S+$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dimensions: int = Field(ge=1, le=4096)
    timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    max_response_bytes: int = Field(default=4_000_000, gt=0)
    max_batch_size: int = Field(default=100, gt=0)

    @model_validator(mode="after")
    def valid_endpoint(self) -> OllamaSettings:
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or self.base_url != self.base_url.strip()
        ):
            raise ValueError("Ollama base URL must be HTTP without credentials")
        return self


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    base_url: str
    api_key: SecretStr = Field(min_length=1)
    text_model: str = Field(min_length=1, pattern=r"\S")
    vision_model: str | None = Field(default=None, min_length=1, pattern=r"\S")
    timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    output_mode: Literal["json_schema", "json_object"]
    max_tokens: int = Field(default=4096, gt=0)
    max_response_bytes: int = Field(default=2_000_000, gt=0)

    @model_validator(mode="after")
    def valid_endpoint(self) -> LLMSettings:
        if not self.api_key.get_secret_value().strip():
            raise ValueError("LLM API key must not be blank")
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "LLM base URL must be an HTTP endpoint without credentials"
            )
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
    jwt_access_key: SecretStr = Field(min_length=32)
    jwt_refresh_key: SecretStr = Field(min_length=32)
    jwt_issuer: str = Field(default="article-graph", min_length=1)
    jwt_audience: str = Field(default="article-graph-api", min_length=1)
    app_id: str = Field(default="article-graph-app", min_length=1)
    blob_root: Path = Path(".data/blobs")
    limits: Limits = Field(default_factory=Limits)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    llm: LLMSettings
    ollama: OllamaSettings | None = None

    @model_validator(mode="after")
    def separate_credentials(self) -> Settings:
        if (
            self.ollama is not None
            and self.ollama.dimensions != self.neo4j.embedding_dimensions
        ):
            raise ValueError("Ollama dimensions must match the Neo4j vector index")
        credentials = (
            self.app_token.get_secret_value(),
            self.jwt_access_key.get_secret_value(),
            self.jwt_refresh_key.get_secret_value(),
        )
        if len(set(credentials)) != 3:
            raise ValueError("App Token, Access and Refresh keys must be distinct")
        return self
