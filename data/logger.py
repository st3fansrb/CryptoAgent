"""Trade logging for CryptoAgent (Phase 1).

Persists completed trades to SQLite (``trades`` table, Section 8.1 schema) and
mirrors each trade into a ChromaDB collection (``trades``) as a numeric
embedding plus metadata, ready for Phase 2 similar-situation retrieval.

A "trade row" is a plain dict with at least these keys:
    timestamp_entry, timestamp_exit, pair, direction, size_usd, leverage,
    fee_usd, pnl_usd, pnl_pct, features (dict), regime_label, win,
    r_multiple, exit_reason
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import numpy as np

import config
from data.features import NUMERIC_FEATURE_KEYS
from data.pipeline import get_connection, init_db

# Lazily-created ChromaDB client/collection (chromadb import is heavy).
_chroma_collection = None


def init_trade_db(conn: sqlite3.Connection | None = None) -> None:
    """Ensure the trade-log tables exist (delegates to the shared schema)."""
    init_db(conn)


def log_trade(trade_row: dict[str, Any],
              conn: sqlite3.Connection | None = None) -> int:
    """Insert a completed trade into the ``trades`` table.

    The ``features`` dict is serialized to JSON in ``features_json``.

    Args:
        trade_row: Trade dict (see module docstring).
        conn: Optional existing connection.

    Returns:
        The new trade's row id.
    """
    own = conn is None
    conn = conn or get_connection()
    try:
        features = trade_row.get("features", {})
        cur = conn.execute(
            """
            INSERT INTO trades (
                timestamp_entry, timestamp_exit, pair, direction, size_usd,
                leverage, fee_usd, pnl_usd, pnl_pct, features_json,
                regime_label, win, r_multiple, exit_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(trade_row["timestamp_entry"]),
                _maybe_int(trade_row.get("timestamp_exit")),
                trade_row["pair"],
                trade_row["direction"],
                float(trade_row["size_usd"]),
                float(trade_row.get("leverage", config.DEFAULT_LEVERAGE)),
                float(trade_row.get("fee_usd", 0.0)),
                _maybe_float(trade_row.get("pnl_usd")),
                _maybe_float(trade_row.get("pnl_pct")),
                json.dumps(features, default=float),
                trade_row.get("regime_label"),
                _maybe_int(trade_row.get("win")),
                _maybe_float(trade_row.get("r_multiple")),
                trade_row.get("exit_reason"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def vector_embed(trade_row: dict[str, Any]) -> np.ndarray:
    """Build a fixed-order numeric embedding from a trade's entry features.

    Only keys in :data:`NUMERIC_FEATURE_KEYS` are used, in that exact order,
    so embeddings remain comparable across trades. Missing/non-finite values
    become ``0.0``.

    Args:
        trade_row: Trade dict containing a ``features`` sub-dict.

    Returns:
        A 1-D ``float32`` numpy array of length ``len(NUMERIC_FEATURE_KEYS)``.
    """
    features = trade_row.get("features", {})
    vec = np.zeros(len(NUMERIC_FEATURE_KEYS), dtype=np.float32)
    for i, key in enumerate(NUMERIC_FEATURE_KEYS):
        try:
            val = float(features.get(key, 0.0))
        except (TypeError, ValueError):
            val = 0.0
        vec[i] = val if np.isfinite(val) else 0.0
    return vec


def get_chroma_collection():
    """Return the persistent ChromaDB ``trades`` collection (cached).

    The collection is configured for externally-supplied embeddings (cosine
    space). The client persists under ``config.CHROMA_DIR``.
    """
    global _chroma_collection
    if _chroma_collection is None:
        import logging

        import chromadb  # local import: heavy dependency
        from chromadb.config import Settings

        # Silence chromadb's posthog telemetry noise (harmless version mismatch).
        logging.getLogger("chromadb.telemetry").setLevel(logging.CRITICAL)
        config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(
            path=str(config.CHROMA_DIR),
            settings=Settings(anonymized_telemetry=False),
        )
        _chroma_collection = client.get_or_create_collection(
            name="trades", metadata={"hnsw:space": "cosine"},
        )
    return _chroma_collection


def log_to_chromadb(trade_row: dict[str, Any], trade_id: int | str) -> str:
    """Store a trade's embedding + metadata in the ChromaDB ``trades`` collection.

    Args:
        trade_row: Trade dict (see module docstring).
        trade_id: Unique id (e.g. the SQLite row id) used as the Chroma id.

    Returns:
        The string id under which the embedding was stored.
    """
    collection = get_chroma_collection()
    embedding = vector_embed(trade_row).tolist()
    metadata = {
        "pair": str(trade_row.get("pair", "")),
        "direction": str(trade_row.get("direction", "")),
        "regime_label": str(trade_row.get("regime_label", "")),
        "pnl_usd": _maybe_float(trade_row.get("pnl_usd")) or 0.0,
        "pnl_pct": _maybe_float(trade_row.get("pnl_pct")) or 0.0,
        "win": int(trade_row.get("win") or 0),
        "exit_reason": str(trade_row.get("exit_reason", "")),
        "timestamp_entry": int(trade_row.get("timestamp_entry", 0)),
    }
    cid = str(trade_id)
    collection.upsert(ids=[cid], embeddings=[embedding], metadatas=[metadata])
    return cid


def log_completed_trade(trade_row: dict[str, Any],
                        conn: sqlite3.Connection | None = None) -> int:
    """Convenience: persist a completed trade to SQLite *and* ChromaDB.

    ChromaDB failures are swallowed (logged to stderr) so a vector-store
    hiccup never blocks the authoritative SQLite write.

    Returns:
        The SQLite row id of the inserted trade.
    """
    trade_id = log_trade(trade_row, conn=conn)
    try:
        log_to_chromadb(trade_row, trade_id)
    except Exception as exc:  # noqa: BLE001 - vector store is best-effort
        print(f"[logger] ChromaDB ingest failed for trade {trade_id}: {exc}")
    return trade_id


# ---------------------------------------------------------------------------
# Small coercion helpers
# ---------------------------------------------------------------------------
def _maybe_int(value: Any) -> int | None:
    """Coerce to int, returning None for None/empty."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    return int(value)


def _maybe_float(value: Any) -> float | None:
    """Coerce to float, returning None for None."""
    if value is None:
        return None
    return float(value)
