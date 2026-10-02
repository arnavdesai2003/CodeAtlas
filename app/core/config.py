from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr


class Settings(BaseSettings):
    app_name: str = "CodeAtlas"
    app_env: str = "development"
    api_key: SecretStr = SecretStr("")

    database_url: str
    elasticsearch_url: str
    elasticsearch_close_search_connections: bool = False
    redis_url: str

    search_cache_ttl: int = Field(default=300, ge=1)
    github_webhook_secret: str = ""
    hybrid_semantic_weight: float = Field(default=0.60, ge=0, le=1, allow_inf_nan=False)
    benchmark_cache_bypass_enabled: bool = False
    embedding_device: str | None = None
    torch_num_threads: int | None = Field(default=None, ge=1)
    git_timeout_seconds: int = Field(default=120, ge=1)
    request_body_max_bytes: int = Field(default=1_048_576, ge=1)
    request_body_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )


settings = Settings()
