"""Application configuration.

Everything is environment driven so the same image runs as the API, the Celery
worker and beat with only env differences between them.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "TaskFlow"
    api_v1_prefix: str = "/api/v1"
    environment: str = "local"
    debug: bool = False
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://taskflow:taskflow@localhost:5432/taskflow"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_pre_ping: bool = True

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    jwt_secret: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 7

    # Argon2id parameters. Defaults follow the argon2-cffi "low memory" profile,
    # which is a reasonable balance for an API doing a hash per login.
    argon2_time_cost: int = 3
    argon2_memory_cost_kib: int = 64 * 1024
    argon2_parallelism: int = 2

    cache_enabled: bool = True
    cache_ttl_seconds: int = 60
    cache_empty_ttl_seconds: int = 15
    cache_key_prefix: str = "tf"

    overdue_scan_interval_seconds: int = 60
    overdue_scan_batch_size: int = 500

    default_page_size: int = 20
    max_page_size: int = 100

    # Only meaningful in tests: run Celery tasks inline instead of via a broker.
    celery_task_always_eager: bool = False

    cors_origins: list[str] = Field(default_factory=list)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
