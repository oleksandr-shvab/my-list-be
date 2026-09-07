from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    ENVIRONMENT: str = "local"

    DATABASE_URL: str
    TEST_DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/item_list_test"
    REDIS_URL: str = "redis://localhost:6379/0"

    SECRET_KEY: str

    SESSION_COOKIE_NAME: str = "session_id"
    SESSION_EXPIRE_MINUTES: int = 60 * 24 * 14

    FRONTEND_URL: str = "http://localhost:5173"

    BACKEND_CORS_ORIGINS: list[str] = ["http://localhost:5173"]


settings = Settings()
