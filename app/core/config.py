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

    # Recommendation engine: user risk stance (conservative | moderate | aggressive).
    # Only assets with risk_tier at or below this level are recommended.
    preferred_risk: str = "moderate"

    # Research / rationale (Option B): multi-provider LLM (Dexter-style).
    research_llm_provider: str = "openai"   # openai | anthropic | ollama | openrouter
    research_llm_model: str = ""           # empty = use provider default (e.g. gpt-4o-mini, llama3.1)
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"


settings = Settings()
