from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr, field_validator


class Settings(BaseSettings):
    app_name: str = "CodeAtlas"
    app_env: str = "development"
    api_key: SecretStr = SecretStr("")

    database_url: str = Field(repr=False)
    elasticsearch_url: str = Field(repr=False)
    elasticsearch_api_key: SecretStr = SecretStr("")
    elasticsearch_close_search_connections: bool = False
    redis_url: str = Field(repr=False)

    search_cache_ttl: int = Field(default=300, ge=1)
    github_webhook_secret: SecretStr = SecretStr("")
    hybrid_semantic_weight: float = Field(default=0.60, ge=0, le=1, allow_inf_nan=False)
    benchmark_cache_bypass_enabled: bool = False
    embedding_device: str | None = None
    torch_num_threads: int | None = Field(default=None, ge=1)
    git_timeout_seconds: int = Field(default=120, ge=1)
    request_body_max_bytes: int = Field(default=1_048_576, ge=1)
    request_body_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    @field_validator("database_url")
    @classmethod
    def use_installed_postgres_driver(cls, value: str) -> str:
        # Render supplies a standard URL; this image installs psycopg 3.
        for scheme in ("postgres://", "postgresql://"):
            if value.startswith(scheme):
                return "postgresql+psycopg://" + value[len(scheme):]
        return value

    @field_validator("elasticsearch_url")
    @classmethod
    def accept_private_service_hostport(cls, value: str) -> str:
        # Blueprint fromService.hostport contains no scheme. Explicit HTTPS
        # endpoints (e.g. Elastic Cloud Hosted) retain TLS verification.
        return value if "://" in value else "http://" + value

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )


settings = Settings()
