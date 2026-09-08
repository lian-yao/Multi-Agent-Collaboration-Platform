from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


class StorageSettings(BaseSettings):
    """Storage endpoint settings used by workflow checkpoint snapshots."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://postgres:postgres@localhost:5433/multi_agent"
    )
    redis_url: str = "redis://localhost:6380/0"
    echo: bool = False


@lru_cache
def get_storage_settings() -> StorageSettings:
    return StorageSettings()


@lru_cache
def get_engine() -> Engine:
    settings = get_storage_settings()
    return create_engine(settings.database_url, echo=settings.echo, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )