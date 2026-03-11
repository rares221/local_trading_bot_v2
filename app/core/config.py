from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_name: str = "local_trading_bot_v2"
    debug: bool = False

    # Database (SQLite by default)
    database_url: str = "sqlite+aiosqlite:///./data/trading.db"

    # Data paths (reuse pattern from codeops-trading-app: optional file cache)
    data_dir: Path = Path("./data")
    # Max age for feed items in days (codeops default: 30)
    feed_max_age_days: int = 30

    # Jobs
    daily_batch_hour: int = 6
    update_active_trades_interval_hours: int = 2
    analysis_run_interval_hours: int = 2


settings = Settings()
