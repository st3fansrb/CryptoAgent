"""Download long historical data for backtesting (free tier, no keys).

Fetches and stores into the same ``data/market.db``:

* 1h OHLCV candles (paginated via ccxt) for each configured pair.
* Funding-rate history (paginated, Binance USD-M futures).
* Full Fear & Greed index history (alternative.me).

Run:
    python -m backtest.download_history                # ~2 years, both pairs
    python -m backtest.download_history --years 3      # custom window
"""

from __future__ import annotations

import argparse
import time

import requests

import config
from data import pipeline

_MS_PER_HOUR = 3_600_000


def download_candles(symbol: str, timeframe: str = "1h", years: float = 2.0,
                     conn=None) -> int:
    """Paginate OHLCV from Binance and store it, returning the row count.

    Binance returns up to 1000 candles per request, so we walk forward from
    ``start`` in ``limit``-sized pages until we reach the present.

    Args:
        symbol: Compact symbol such as ``"BTCUSDT"``.
        timeframe: Candle timeframe (``"1h"`` for the backtest).
        years: How far back to fetch.
        conn: Optional shared SQLite connection.

    Returns:
        Total number of candles stored.
    """
    own = conn is None
    conn = conn or pipeline.get_connection()
    exchange = pipeline._make_exchange()
    ccxt_symbol = pipeline.to_ccxt_symbol(symbol)

    interval = config.TIMEFRAME_MS[timeframe]
    now_ms = int(time.time() * 1000)
    since = now_ms - int(years * 365 * 24 * _MS_PER_HOUR)
    total = 0
    try:
        while since < now_ms:
            batch = exchange.fetch_ohlcv(ccxt_symbol, timeframe=timeframe,
                                         since=since, limit=1000)
            if not batch:
                break
            total += pipeline.store_candles(symbol, timeframe, batch, conn=conn)
            since = int(batch[-1][0]) + interval
            print(f"  {symbol} {timeframe}: {total} candles "
                  f"(up to {time.strftime('%Y-%m-%d', time.gmtime(batch[-1][0] / 1000))})")
            if len(batch) < 1000:
                break
            time.sleep(exchange.rateLimit / 1000)
        return total
    finally:
        if own:
            conn.close()


def download_funding(symbol: str, years: float = 2.0, conn=None) -> int:
    """Paginate funding-rate history and store it, returning the row count.

    Args:
        symbol: Compact futures symbol such as ``"BTCUSDT"``.
        years: How far back to fetch.
        conn: Optional shared SQLite connection.

    Returns:
        Total number of funding rows stored.
    """
    own = conn is None
    conn = conn or pipeline.get_connection()
    url = f"{config.BINANCE_FAPI}/fapi/v1/fundingRate"
    now_ms = int(time.time() * 1000)
    start = now_ms - int(years * 365 * 24 * _MS_PER_HOUR)
    total = 0
    try:
        while start < now_ms:
            resp = requests.get(
                url, params={"symbol": symbol, "startTime": start, "limit": 1000},
                timeout=config.HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            payload = [
                {"symbol": symbol, "funding_time": int(r["fundingTime"]),
                 "funding_rate": float(r["fundingRate"])}
                for r in rows
            ]
            total += pipeline.store_funding(payload, conn=conn)
            start = int(rows[-1]["fundingTime"]) + 1
            if len(rows) < 1000:
                break
            time.sleep(0.25)
        print(f"  {symbol} funding: {total} rows")
        return total
    finally:
        if own:
            conn.close()


def download_fear_greed(conn=None) -> int:
    """Download the full Fear & Greed history (alternative.me, ``limit=0``).

    Returns:
        Number of daily sentiment rows stored.
    """
    own = conn is None
    conn = conn or pipeline.get_connection()
    try:
        rows = pipeline.fetch_fear_greed(limit=0)  # 0 = entire history
        n = pipeline.store_fear_greed(rows, conn=conn)
        print(f"  Fear & Greed: {n} daily rows")
        return n
    finally:
        if own:
            conn.close()


def main() -> None:
    """CLI: download candles (one or more timeframes) + funding + sentiment.

    Examples:
        python -m backtest.download_history                       # 1h
        python -m backtest.download_history --timeframes 15m,1h,4h
        python -m backtest.download_history --symbols BTCUSDT,SOLUSDT
    """
    parser = argparse.ArgumentParser(description="Download backtest history")
    parser.add_argument("--years", type=float, default=2.0,
                        help="How many years of history to fetch (default 2).")
    parser.add_argument("--timeframes", default="1h",
                        help="Comma-separated timeframes (e.g. 15m,1h,4h).")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated pairs (default: config.PAIRS).")
    args = parser.parse_args()

    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    symbols = ([s.strip() for s in args.symbols.split(",")]
               if args.symbols else config.PAIRS)

    conn = pipeline.get_connection()
    pipeline.init_db(conn)
    print(f"Downloading ~{args.years}y into {config.MARKET_DB} "
          f"| pairs={symbols} | timeframes={timeframes}")
    for symbol in symbols:
        for tf in timeframes:
            try:
                download_candles(symbol, tf, args.years, conn=conn)
            except Exception as exc:  # noqa: BLE001
                print(f"  {symbol} {tf}: ERROR {exc}")
        try:
            download_funding(symbol, args.years, conn=conn)
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol} funding: ERROR {exc}")
    try:
        download_fear_greed(conn=conn)
    except Exception as exc:  # noqa: BLE001
        print(f"  Fear & Greed: ERROR {exc}")
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
