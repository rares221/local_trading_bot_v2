"""
Backtesting framework: historical replay of the recommendation engine.

Components:
- data_provider: pre-fetch OHLCV history and inject into the technicals cache
- simulator: simulate trade outcomes (hit_tp / hit_sl / expired) from future bars
- replay: main loop — iterate trading days, build signals, simulate outcomes
- metrics: compute Sharpe, max drawdown, win rate per confidence bucket, etc.
"""
