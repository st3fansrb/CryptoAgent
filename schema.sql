-- CryptoAgent — SQLite schema (Phase 1)
-- All tables use natural unique keys so repeated inserts are idempotent
-- (run-once orchestration may re-fetch overlapping windows).

-- ---------------------------------------------------------------------------
-- Market data
-- ---------------------------------------------------------------------------

-- Raw OHLCV candles for every (symbol, timeframe).
CREATE TABLE IF NOT EXISTS candles (
    symbol     TEXT    NOT NULL,
    timeframe  TEXT    NOT NULL,
    open_time  INTEGER NOT NULL,   -- candle open, epoch milliseconds (UTC)
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     REAL    NOT NULL,
    PRIMARY KEY (symbol, timeframe, open_time)
);

CREATE INDEX IF NOT EXISTS idx_candles_lookup
    ON candles (symbol, timeframe, open_time DESC);

-- Funding rate observations (current + historical) from Binance USD-M futures.
CREATE TABLE IF NOT EXISTS funding (
    symbol       TEXT    NOT NULL,
    funding_time INTEGER NOT NULL,   -- epoch milliseconds (UTC)
    funding_rate REAL    NOT NULL,   -- e.g. 0.0001 == 0.01%
    PRIMARY KEY (symbol, funding_time)
);

-- Open interest history (period = 1h) from Binance futures data endpoint.
CREATE TABLE IF NOT EXISTS open_interest (
    symbol             TEXT    NOT NULL,
    ts                 INTEGER NOT NULL,   -- epoch milliseconds (UTC)
    open_interest      REAL    NOT NULL,   -- in contracts/coins
    open_interest_usd  REAL,               -- notional value if provided
    PRIMARY KEY (symbol, ts)
);

-- Fear & Greed index snapshots (alternative.me), one row per day.
CREATE TABLE IF NOT EXISTS sentiment (
    ts            INTEGER NOT NULL,   -- epoch seconds (UTC), start of day
    value         INTEGER NOT NULL,   -- 0..100
    classification TEXT,              -- e.g. "Extreme Fear"
    PRIMARY KEY (ts)
);

-- BTC dominance snapshots (CoinGecko global). We accumulate our own history
-- because the free tier does not expose cheap historical dominance.
CREATE TABLE IF NOT EXISTS dominance (
    ts         INTEGER NOT NULL,   -- epoch seconds (UTC) when sampled
    btc_dom    REAL    NOT NULL,   -- percent, e.g. 57.3
    eth_dom    REAL,               -- percent
    PRIMARY KEY (ts)
);

-- ---------------------------------------------------------------------------
-- Trading state (paper engine) — persisted so run-once survives restarts
-- ---------------------------------------------------------------------------

-- Single-row portfolio state (id = 1).
CREATE TABLE IF NOT EXISTS portfolio_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    equity          REAL    NOT NULL,
    cash            REAL    NOT NULL,
    day_anchor      REAL    NOT NULL,   -- equity at start of current UTC day
    week_anchor     REAL    NOT NULL,   -- equity at start of current UTC week
    day_key         TEXT    NOT NULL,   -- e.g. "2026-06-23"
    week_key        TEXT    NOT NULL,   -- e.g. "2026-W26"
    trading_halted  INTEGER NOT NULL DEFAULT 0,
    halt_reason     TEXT,
    updated_at      INTEGER NOT NULL
);

-- Currently open paper positions.
CREATE TABLE IF NOT EXISTS open_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,   -- 'LONG' | 'SHORT'
    size_usd        REAL    NOT NULL,   -- notional at entry
    qty             REAL    NOT NULL,   -- base-asset quantity
    leverage        REAL    NOT NULL,
    entry_price     REAL    NOT NULL,   -- fill price incl. slippage
    stop_loss       REAL    NOT NULL,
    take_profit     REAL    NOT NULL,
    entry_fee_usd   REAL    NOT NULL,
    risk_usd        REAL    NOT NULL,   -- |entry - stop| * qty_at_open (1R in USD)
    opened_at       INTEGER NOT NULL,   -- epoch milliseconds (UTC)
    features_json   TEXT    NOT NULL,   -- feature snapshot at entry
    regime_label    TEXT,
    r_price              REAL    DEFAULT 0,  -- price distance of 1R (= SL distance at open)
    be_moved             INTEGER DEFAULT 0,  -- 1 once stop moved to breakeven
    partial_done         INTEGER DEFAULT 0,  -- 1 once the partial exit was taken
    realized_partial_usd REAL    DEFAULT 0   -- PnL already booked from partial exits
);

CREATE INDEX IF NOT EXISTS idx_open_positions_symbol
    ON open_positions (symbol);

-- ---------------------------------------------------------------------------
-- Trade log (completed trades) — Section 8.1 schema
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_entry  INTEGER NOT NULL,   -- epoch milliseconds (UTC)
    timestamp_exit   INTEGER,            -- epoch milliseconds (UTC)
    pair             TEXT    NOT NULL,
    direction        TEXT    NOT NULL,   -- 'LONG' | 'SHORT' | 'FLAT'
    size_usd         REAL    NOT NULL,
    leverage         REAL    NOT NULL,
    fee_usd          REAL    NOT NULL,   -- round-trip fees
    pnl_usd          REAL,
    pnl_pct          REAL,
    features_json    TEXT    NOT NULL,   -- full feature snapshot at entry
    regime_label     TEXT,
    win              INTEGER,            -- 1 win, 0 loss (NULL while open)
    r_multiple       REAL,               -- realized PnL / 1R
    exit_reason      TEXT                -- 'STOP_LOSS' | 'TAKE_PROFIT' | 'MANUAL' | 'RISK_HALT'
);

CREATE INDEX IF NOT EXISTS idx_trades_pair_time
    ON trades (pair, timestamp_entry DESC);
