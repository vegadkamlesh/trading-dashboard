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

    # --- Dhan credentials ---
    dhan_client_id: str = ""
    dhan_access_token: str = ""

    # --- Security ---
    app_session_secret: str = "insecure-dev-secret-change-me"
    app_order_pin: str = "000000"

    # --- Safety ---
    app_dry_run: bool = True
    # Fast orders: when True, the order PIN is NOT required (1-click scalping).
    # Set to True only while actively scalping; keep False otherwise.
    app_fast_orders: bool = False

    # --- Logging ---
    # Directory for the app log files (relative to backend/, or absolute).
    log_dir: str = "logs"
    # Files older than this many days are auto-deleted at startup + daily.
    log_retention_days: int = 7
    # DEBUG / INFO / WARNING / ERROR
    log_level: str = "INFO"

    # --- Network ---
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    # Comma separated in .env. Parsed lazily via property.
    app_allowed_origins_raw: str = Field(
        "http://127.0.0.1:5173,http://localhost:5173",
        validation_alias="APP_ALLOWED_ORIGINS",
    )

    # --- Option chain poller ---
    oc_polled_indices_raw: str = Field("NIFTY,SENSEX", validation_alias="OC_POLLED_INDICES")
    oc_poll_interval_seconds: float = 3.0

    # --- Intelligence / news ---
    # Optional custom RSS feeds, format: "Name|url;Name|url". Empty = built-ins.
    app_news_feeds: str = Field("", validation_alias="APP_NEWS_FEEDS")
    # Default look-back window (days) for expired-options history studies.
    intel_history_days: int = 90

    # ---- parsed lists (CSV -> list) ----
    @property
    def app_allowed_origins(self) -> list[str]:
        return [s.strip() for s in self.app_allowed_origins_raw.split(",") if s.strip()]

    @property
    def oc_polled_indices(self) -> list[str]:
        return [s.strip().upper() for s in self.oc_polled_indices_raw.split(",") if s.strip()]

    @field_validator("oc_poll_interval_seconds")
    @classmethod
    def _min_interval(cls, v: float) -> float:
        # Dhan enforces 1 request / 3s per underlying. Never go below.
        return max(3.0, float(v))

    def is_configured(self) -> bool:
        """True when Dhan creds are present. Used to fail fast with a clear message."""
        return bool(self.dhan_client_id) and bool(self.dhan_access_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()

