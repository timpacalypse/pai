from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://pai:pai_dev_secret@postgres:5432/pai"
    redis_url: str = "redis://redis:6379/0"
    ollama_url: str = "http://192.168.0.58:11434"
    ollama_default_model: str = "qwen3:8b"
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

    # Daily briefing
    briefing_schedule_hours: float = 24.0  # hours between briefings, 0 = disabled
    weather_lat: str = ""  # latitude for weather (e.g. "38.9072" for DC)
    weather_lon: str = ""  # longitude for weather (e.g. "-77.0369" for DC)

    # Autonomous background workflows
    autonomous_schedule_hours: float = 12.0  # hours between autonomous runs, 0 = disabled

    # Google Calendar
    google_credentials_path: str = "/app/credentials/google_credentials.json"
    google_token_path: str = "/app/credentials/google_token.json"

    # Fitness platform sync
    fitness_sync_hours: float = 4.0  # hours between sync runs, 0 = disabled

    # Whoop (OAuth2 — tokens stored in DB after initial auth)
    whoop_client_id: str = ""
    whoop_client_secret: str = ""
    whoop_access_token: str = ""
    whoop_refresh_token: str = ""

    # Peloton (username/password auth)
    peloton_username: str = ""
    peloton_password: str = ""

    # Tonal (Auth0 password grant)
    tonal_email: str = ""
    tonal_password: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
