# CryptoAgent — Project Overview (for CV / portfolio)

> This document describes a personal project so an AI (or a recruiter) can
> extract accurate CV content. It is written to be factual, not promotional.
> The system is a **paper-trading** research project (no real capital).

## One-line summary

An end-to-end, fully automated algorithmic crypto-trading research system in
Python: data pipeline, feature engineering, a realistic paper-trading engine
with hard-coded risk controls, a rigorous backtesting framework, and a live
hourly agent — built to discover and validate a trading edge without
overfitting.

## What it is

CryptoAgent is an independent quantitative trading project that takes a strategy
idea from research to a live (paper) automated agent. It ingests free-tier
market, derivatives, on-chain, and sentiment data; engineers technical and
regime features; runs strategies through a backtester with strict
no-look-ahead and in-sample/out-of-sample validation; and executes the
validated strategy live on a paper account with automated risk management and
real-time alerts. The emphasis throughout is **methodological rigor** —
proving an edge is real (and rejecting edges that are merely curve-fit) before
committing to it.

## Tech stack

- **Language:** Python 3.12
- **Data/Compute:** pandas, NumPy, `ta` (technical analysis)
- **Storage:** SQLite (market data + trade log + portfolio state), ChromaDB
  (vector embeddings of trades for similarity retrieval)
- **APIs (all free-tier):** Binance via `ccxt` (multi-timeframe OHLCV),
  Binance USD-M futures REST (funding rate, open interest), alternative.me
  (Fear & Greed index), CoinGecko (BTC dominance)
- **Automation:** n8n (hourly scheduling), a lightweight stdlib HTTP service,
  Telegram Bot API (alerts)
- **Engineering:** modular package design, environment-based secrets
  (`python-dotenv`), offline smoke tests, idempotent SQLite schema with
  migrations

## Key components I designed and built

1. **Data pipeline** — paginated historical + incremental ingestion of OHLCV
   (15m/1h/4h), funding rates, open interest, sentiment, and dominance into
   SQLite; resilient per-source error isolation; self-healing backfill.
2. **Feature engineering** — RSI, Bollinger %b, ATR, multi-period EMAs,
   multi-horizon returns, realized volatility, funding z-score, sentiment
   buckets, and *computed* market-regime labels (trend × volatility).
3. **Paper-trading engine** — simulates execution with realistic costs (0.1%
   taker fee/side + 0.05% slippage); ATR-based stops/targets; **active trade
   management** (partial take-profit + move-to-breakeven); and **hard-coded
   risk limits the strategy cannot bypass** (position sizing, max concurrent
   positions, daily/weekly loss kill-switches, macro event no-trade windows).
   State persists in SQLite so the stateless hourly agent resumes seamlessly.
4. **Backtesting framework** — vectorized, **no look-ahead** (causal indicators
   + as-of joins for funding/sentiment); **in-sample/out-of-sample split**;
   **cross-timeframe** and **multi-pair** robustness testing; a
   **correlation-aware portfolio mode** (shared account, shared position cap);
   per-trade logging with MFE/MAE; results persisted to CSV/JSON for
   experiment tracking.
5. **Strategy layer** — parameter-driven, regime-adaptive strategy variants
   behind a regime router, enabling systematic head-to-head variant testing.
6. **Live orchestration** — a run-once orchestrator driven hourly by n8n via an
   HTTP trigger; logs every trade (with full feature snapshot) to SQLite +
   ChromaDB; sends Telegram alerts on entries, exits, and risk halts; plus a
   read-only status dashboard.

## Methodology highlights (the part that matters)

- **Rejected overfitting explicitly.** A more selective variant looked
  excellent in-sample (profit factor 3.6) but collapsed out-of-sample (0.4) —
  identified and discarded as curve-fit; refused to tune parameters to fit
  history.
- **Validated across regimes, timeframes, and assets.** Tuned on pre-2026 data,
  tested on the unseen 2026 bear market; confirmed the edge persisted on 1h/4h
  (and that 15m was destroyed by fees, matching theory) and generalized across
  multiple liquid altcoins rather than a single lucky pair.
- **Measured the real, correlation-aware risk** via a shared-account portfolio
  backtest, not just isolated per-pair results.

## Results (honest)

- Discovered a **short-side mean-reversion edge** (fading rallies into
  resistance during confirmed downtrends) that is **positive out-of-sample**
  across multiple altcoins.
- Portfolio out-of-sample: **profit factor ~1.33, win rate ~54%, max drawdown
  ~0.5%** with conservative sizing.
- Honest assessment: the edge is **real but thin** (~1%/yr at current sizing) —
  validated as survivable and slightly positive, and now **forward paper-traded
  live** to confirm it holds in real time. The value is a clean, rigorously
  validated foundation rather than a finished money-maker.

## Skills demonstrated

Quantitative/algorithmic trading · backtesting & strategy validation
(out-of-sample, walk-forward thinking, anti-overfitting) · time-series & technical
analysis · feature engineering · data engineering & ETL from REST APIs ·
Python (pandas/NumPy) · SQL/SQLite · vector databases (ChromaDB) · risk
management systems · workflow automation (n8n) · API integration · clean
modular software design with tests.

## Suggested CV bullet points (an AI can adapt these)

- Built an end-to-end automated crypto algo-trading system in Python (data
  pipeline → feature engineering → backtester → live paper agent), integrating
  4 free-tier market/derivatives/sentiment APIs into SQLite + a vector store.
- Designed a rigorous backtesting framework with no-look-ahead validation,
  in/out-of-sample splits, and cross-timeframe/multi-asset robustness testing;
  identified and discarded overfit strategy variants.
- Engineered a realistic paper-trading engine with fee/slippage modeling, ATR
  stops, partial-profit/breakeven management, and hard-coded risk limits
  (sizing, kill-switches, macro no-trade windows).
- Discovered and validated an out-of-sample-positive short-side mean-reversion
  edge across multiple altcoins (portfolio profit factor ~1.33, max drawdown
  ~0.5%); deployed it as a live paper agent on an hourly schedule (n8n) with
  Telegram alerting.

---

*Personal / independent project. Paper trading only — no live capital deployed.*
