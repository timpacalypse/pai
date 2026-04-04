from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://pai:pai_dev_secret@postgres:5432/pai"
    redis_url: str = "redis://redis:6379/0"
    ollama_url: str = "http://192.168.0.58:11434"
    ollama_default_model: str = "llama3.1:8b"
    orchestrator_env: str = "development"
    orchestrator_log_level: str = "info"

    # Gmail notifications
    gmail_address: str = ""
    gmail_app_password: str = ""
    gmail_recipient: str = ""  # defaults to gmail_address if empty

    # Scheduled research
    research_schedule_hours: float = 6.0  # 0 = disabled
    research_topics: str = ""  # pipe-delimited override, e.g. "topic1|topic2"

    # Daily meal scheduler
    meal_schedule_hours: float = 24.0  # hours between meal emails, 0 = disabled

    # Home maintenance alerts
    home_alert_hours: float = 24.0  # hours between alert checks, 0 = disabled

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
