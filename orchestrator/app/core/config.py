from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://pai:pai_dev_secret@postgres:5432/pai"
    redis_url: str = "redis://redis:6379/0"
    ollama_url: str = "http://192.168.0.58:11434"
    ollama_default_model: str = "llama3.1:8b"
    orchestrator_env: str = "development"
    orchestrator_log_level: str = "info"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
