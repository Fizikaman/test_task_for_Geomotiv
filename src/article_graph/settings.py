"""Environment configuration, validated before the application is served."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARTICLE_GRAPH_",
        env_file=".env",
        extra="forbid",
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="Article Graph", min_length=1)
    app_token: SecretStr = Field(min_length=32)
