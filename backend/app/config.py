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

    # --- ORB Algo (auto trading) -------------------------------------------
    # All ratios come from the 5-year ORB study (see ALGO_STRATEGY.md).
    # Stop / target are multiples of the day's 09:15-09:25 range (auto-adapts).
    # Which indices the auto-trader watches (first one to break takes the trade).
    algo_indices_raw: str = Field("NIFTY,SENSEX", validation_alias="ALGO_INDICES")
    algo_sl_mult: float = 0.2           # stop = 0.20 x opening range
    algo_target_mult: float = 2.0       # target = 2.0 x opening range
    algo_breakeven_at: float = 0.0      # move stop to entry after +N x OR (0 = off)
    algo_entry_cutoff: str = "15:00"    # no fresh entries after this time
    algo_exit_time: str = "15:12"       # forced square-off
    algo_strike_offset: int = 1         # +1 strike in the trade direction
    # ---- Lot sizing (READ THIS BEFORE CHANGING) ----
    # Do NOT size off a single trade's risk. This strategy wins only ~15% of the
    # time, so the 39-46 trade losing streaks in the backtest are NORMAL, not bad
    # luck. Size so a whole STREAK fits inside the drawdown budget:
    #
    #     risk%  <=  streak_budget% / max_loss_streak
    #            <=      25%        /       50        = 0.5%   <-- the default
    #
    # That works out to about Rs1,00,000 of capital per lot, which is exactly the
    # sizing the Monte Carlo in ALGO_STRATEGY.md supports (0% ruin at Rs1L/lot).
    # Setting this to a "normal" 2% sizes 4 lots on Rs1L, which Monte Carlo puts
    # at a 15% chance of blowing the account up.
    algo_streak_budget_pct: float = 25.0  # a full losing streak may cost this % of balance
    algo_max_loss_streak: int = 50        # ...measured across this many losses
    algo_risk_pct: float = 0.5          # % of balance risked per trade (see above)
    algo_max_capital_pct: float = 60.0  # max % of balance used as option premium
    algo_max_lots: int = 10             # hard cap on lots per trade
    algo_min_lots: int = 1
    algo_min_or_pct: float = 0.0        # skip if OR range < this % of price (0=off)
    algo_max_or_pct: float = 0.0        # skip if OR range > this % of price (0=off)
    algo_order_type: str = "MARKET"     # MARKET (fast) | LIMIT (no slippage)
    algo_product_type: str = "INTRADAY"
    # Optional lot-size override, e.g. "NIFTY=75,SENSEX=20". Empty = registry.
    algo_lot_sizes: str = Field("", validation_alias="ALGO_LOT_SIZES")
    # Paper-trade P&L for dry-run fills uses this assumed option delta.
    algo_paper_delta: float = 0.5

    @property
    def algo_indices(self) -> list[str]:
        return [s.strip().upper() for s in self.algo_indices_raw.split(",") if s.strip()]

    @property
    def algo_lot_override(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for part in self.algo_lot_sizes.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                try:
                    out[k.strip().upper()] = int(v)
                except ValueError:
                    continue
        return out

    @property
    def algo_effective_risk_pct(self) -> float:
        """risk_pct, but never more than the streak budget allows.

        Guards against someone putting ALGO_RISK_PCT=2 in .env and silently
        re-introducing a 4-lot-on-Rs1L position.
        """
        if self.algo_max_loss_streak > 0:
            return min(self.algo_risk_pct, self.algo_streak_budget_pct / self.algo_max_loss_streak)
        return self.algo_risk_pct

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

