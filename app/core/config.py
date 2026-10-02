import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_name: str = "local_trading_bot_v2"
    debug: bool = False

    # Database — set to postgresql+asyncpg://... for TimescaleDB in production
    database_url: str = "sqlite+aiosqlite:///./data/trading.db"

    # SQLite → PostgreSQL migration: set MIGRATION_ENABLED=true and point
    # SQLITE_SOURCE_URL to the old SQLite file to transfer all data on startup.
    migration_enabled: bool = False
    sqlite_source_url: str = "sqlite:///./data/trading.db"

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
    research_llm_provider: str = ""   # openai | anthropic | ollama | openrouter | lm_studio — must be set in .env
    research_llm_model: str = ""           # empty = use provider default (e.g. gpt-4o-mini, llama3.1)
    # Extended thinking for Anthropic models (0 = disabled). budget_tokens controls depth; max ~16000.
    research_llm_thinking_budget: int = 0
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    lm_studio_base_url: str = "http://localhost:1234/v1"  # LM_STUDIO_BASE_URL env var

    # Finnhub market data (optional). When both are set, eligible symbols
    # (plain stock tickers — no =, ^, -, :) are fetched via Finnhub instead of Yahoo Finance.
    finnhub_user: str = ""
    finnhub_key: str = ""

    # Ticker resolution: exchange preference and cache TTL.
    # Comma-separated Yahoo Finance exchange codes, ordered by preference.
    # US exchanges first, then Europe, Asia, etc.
    preferred_exchanges: str = "NMS,NYQ,NGM,PCX,BTS,ASE,LSE,GER,PAR,AMS,MIL,TYO,HKG,SHH,SHZ,KSC,TSE,SAO,JSE,NSI,BSE"
    ticker_cache_max_age_days: int = 90


settings = Settings()


def validate_startup_config() -> None:
    """
    Validate required settings at startup. Called once from app/main.py's lifespan.

    Missing provider is FATAL (nothing works without an LLM backend selected). A missing
    API key for the selected provider is a WARNING, not fatal — the app still boots and
    backtesting/analysis run; only LLM rationale generation is affected.
    """
    provider = (settings.research_llm_provider or "").strip().lower()
    if not provider:
        raise RuntimeError(
            "RESEARCH_LLM_PROVIDER is not set. Set it in your .env to one of: "
            "openai | anthropic | ollama | openrouter | lm_studio. See .env.example."
        )

    _key_for_provider = {
        "openai": ("OPENAI_API_KEY", settings.openai_api_key),
        "anthropic": ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
        "openrouter": ("OPENROUTER_API_KEY", settings.openrouter_api_key),
    }
    if provider in _key_for_provider:
        env_name, value = _key_for_provider[provider]
        if not value or not str(value).strip():
            logger.warning(
                "RESEARCH_LLM_PROVIDER=%r but %s is not set — LLM rationale generation will "
                "fail. Set %s in .env (see .env.example) or switch providers.",
                provider, env_name, env_name,
            )
