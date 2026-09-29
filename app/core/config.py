from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    app_name: str = "CodeAtlas"
    app_env: str = "development"

    database_url: str
    elasticsearch_url: str
    redis_url: str

    search_cache_ttl: int = 300
    github_webhook_secret: str = ""
    hybrid_semantic_weight: float = 0.60
    benchmark_cache_bypass_enabled: bool = False
    embedding_device: str | None = None
    torch_num_threads: int | None = Field(default=None, ge=1)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
