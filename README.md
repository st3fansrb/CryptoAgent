# CryptoAgent — Phase 1 Foundation

A rule-based, memory-augmented crypto trading agent. **Phase 1** delivers the
data → features → strategy → paper-execution → trade-log pipeline described in
Section 9 of the market brief. Everything is free-tier (no paid API keys),
rule-based (no ML yet), and runs offline except for the data-fetch API calls.

> **Regime context (June 2026):** bearish-to-sideways, Extreme Fear, ETF
> outflows. The agent is deliberately conservative — few trades, tight 3%
> sizing, hard-coded risk limits it cannot bypass, and no-trade windows around
> FOMC/CPI.

## Quickstart (3 commands)

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env          # optional: Binance keys are NOT needed in Phase 1
python main.py --once         # run one decision cycle and exit
```

Verify the wiring offline (no network) at any time:

```bash
python tests/smoke_test.py
```

## What it does

`main.py` runs **one cycle then exits** (run-once design) — schedule it
externally for hourly cadence. Each cycle:

1. Refreshes Binance OHLCV candles (1h primary, 4h context, 15m features); on a
   4h boundary also refreshes funding, open interest, Fear & Greed, and BTC
   dominance.
2. Enforces hard-coded risk limits (halts on daily/weekly loss breaches).
3. Computes the feature vector and checks open positions for SL/TP hits.
4. Routes on the computed regime label and evaluates the baseline
   mean-reversion strategy.
5. Opens a paper position on a valid signal (blocked during macro no-trade
   windows), then logs everything to SQLite + ChromaDB and prints an equity
   summary.

### Scheduling (cron example)

```cron
# Run the agent at the top of every hour (adjust paths)
0 * * * * cd /path/to/CryptoAgent && .venv/bin/python main.py --once >> agent.log 2>&1
```

n8n: use a Schedule trigger (every 1 hour) → Execute Command node running the
same `main.py --once`.

## Data sources (all free, no keys)

| Data | Source |
| --- | --- |
| OHLCV (1h/4h/15m) | Binance public via `ccxt` |
| Funding rate (current + 30d) | Binance USD-M futures REST (`/fapi/v1`) |
| Open interest (1h) | Binance futures data endpoint |
| Fear & Greed index | alternative.me |
| BTC dominance | CoinGecko `/global` |

> BTC dominance 7-day trend is built from snapshots this agent persists itself
> (CoinGecko's free tier has no cheap historical dominance), so it reads `FLAT`
> until ~7 days of history accumulate.

## Project layout

```
config.py        # all parameters: capital, pairs, risk limits, no-trade dates
schema.sql       # SQLite schema (market data + trade log + portfolio state)
main.py          # run-once orchestrator (--once / --dry-run / --no-fetch)
data/
  pipeline.py    # ingestion to data/market.db
  features.py    # RSI/BB/ATR/EMA, derivatives, sentiment, computed regime label
  logger.py      # trade log -> SQLite + ChromaDB embeddings
trading/
  paper_engine.py# paper execution + hard-coded risk enforcement
strategies/
  baseline_mean_reversion.py  # first rule-based agent (entries only)
  regime_router.py            # routes regime -> strategy + params (stubs for more)
tests/
  smoke_test.py  # offline end-to-end verification
```

## Risk model (hard-coded, agent cannot bypass)

- Max position size: **3%** of equity per trade (1× leverage).
- Stop-loss **1.5× ATR**, take-profit **3.0× ATR** (2:1 R:R minimum).
- Max **2** simultaneous open positions.
- Daily loss limit **−3%** → flatten + halt until next UTC day.
- Weekly loss limit **−8%** → halt, requires manual `reset_halt()`.
- **No new entries within 24h of known FOMC/CPI/NFP events** (`config.NO_TRADE_EVENTS`).

## Important notes & limitations

- **Shorts are simulated.** Real Binance *spot* cannot short; the engine keeps
  the spot fee/slippage model but allows SHORT as a directional paper position
  (PnL inverts). Going live with shorts would require margin/perps.
- Execution costs modeled: **0.1%** taker fee per side (0.2% round trip) +
  **0.05%** slippage per fill.
- All state (equity, positions, halt flags, day/week anchors) lives in SQLite,
  so run-once invocations resume seamlessly.
- API keys are read only from `.env` and are unused in Phase 1.

## Out of scope (later phases)

ML policy head, ChromaDB nearest-neighbor retrieval into live decisions,
SOL/TRX pairs, trend-following/breakout strategy bodies, live order execution,
VPS/n8n deployment, dashboards.
