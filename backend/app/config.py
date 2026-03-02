from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Supabase
    supabase_url: str
    supabase_key: str

    # Encryption
    encryption_key: str

    # OpenAI
    openai_api_key: str

    # App settings
    detection_interval_minutes: int = 5
    default_detection_threshold: int = 5
    anomaly_window_minutes: int = 30
    rage_click_threshold: int = 5
    rage_click_window_seconds: int = 3
    dead_click_timeout_ms: int = 3000  # No navigation/network within this window = dead click
    dead_click_min_sessions: int = 3   # Must occur across this many sessions
    dead_end_max_duration_seconds: int = 15   # Page visit shorter than this with no interaction = bounce
    dead_end_min_sessions: int = 5            # Must occur across this many sessions
    dead_end_min_bounce_rate: float = 0.6     # 60%+ sessions bouncing on same page
    confusing_flow_min_sessions: int = 10     # Min sessions entering a flow to detect drop-off
    confusing_flow_drop_threshold: float = 0.5  # 50%+ drop-off at a step = confusing
    confidence_threshold: float = 0.75
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000,https://main.dmmdetom9xhhv.amplifyapp.com,https://buglyft.com,https://www.buglyft.com"

    # SMTP (email notifications)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
