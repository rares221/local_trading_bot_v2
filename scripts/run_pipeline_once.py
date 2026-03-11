#!/usr/bin/env python3
"""
Run the full pipeline once: ingest RSS → sentiment → analysis → daily batch.
Use this to populate data so /api/analysis/latest and /api/recommendations return something.
"""
import asyncio
import sys
from pathlib import Path

# Run from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.jobs.scheduler import (
    job_ingest_and_sentiment,
    job_run_analysis,
    job_daily_batch,
)


async def main() -> None:
    print("1/3 Ingesting RSS + sentiment...")
    await job_ingest_and_sentiment()
    print("2/3 Running market analysis...")
    await job_run_analysis()
    print("3/3 Creating daily recommendation batch (if none for today)...")
    await job_daily_batch()
    print("Done. Check GET /api/analysis/latest and GET /api/recommendations")


if __name__ == "__main__":
    asyncio.run(main())
