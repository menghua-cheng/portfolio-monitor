"""SQLite persistence layer.

All writes are idempotent (INSERT ... ON CONFLICT DO UPDATE) so the daily
pipeline can be safely re-run for the same date without creating duplicates.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

# Repo root = three levels up from this file (src/portfolio_monitor/db.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = _REPO_ROOT / "data" / "portfolio.db"

# Moving-average period keys shared across the schema and indicator computation.
MA_KEYS = ["sma5", "sma20", "sma60", "sma120", "sma240",
           "ema5", "ema20", "ema60", "ema120", "ema240"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS securities (
    ticker   TEXT PRIMARY KEY,
    name     TEXT,
    added_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prices (
    ticker    TEXT NOT NULL,
    date      TEXT NOT NULL,          -- ISO YYYY-MM-DD
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    adj_close REAL,
    volume    INTEGER,
    source    TEXT,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS indicators (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    sma5 REAL, sma20 REAL, sma60 REAL, sma120 REAL, sma240 REAL,
    ema5 REAL, ema20 REAL, ema60 REAL, ema120 REAL, ema240 REAL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS signals (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, date, signal_type)
);

CREATE TABLE IF NOT EXISTS runs (
    run_date     TEXT PRIMARY KEY,
    status       TEXT,
    rows_updated INTEGER,
    notes        TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);
"""


@contextmanager
def connect(db_path: str | Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Open a connection with sane defaults and ensure the schema exists."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# securities
# --------------------------------------------------------------------------- #
def upsert_security(conn: sqlite3.Connection, ticker: str, name: str | None = None) -> None:
    conn.execute(
        """INSERT INTO securities (ticker, name) VALUES (?, ?)
           ON CONFLICT(ticker) DO UPDATE SET name = COALESCE(excluded.name, securities.name)""",
        (ticker.upper(), name),
    )


def remove_security(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute("DELETE FROM securities WHERE ticker = ?", (ticker.upper(),))


def list_securities(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ticker, name, added_at FROM securities ORDER BY ticker"
    ).fetchall()


# --------------------------------------------------------------------------- #
# prices
# --------------------------------------------------------------------------- #
def upsert_prices(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """rows: dicts with keys ticker,date,open,high,low,close,adj_close,volume,source."""
    sql = """
        INSERT INTO prices (ticker, date, open, high, low, close, adj_close, volume, source)
        VALUES (:ticker, :date, :open, :high, :low, :close, :adj_close, :volume, :source)
        ON CONFLICT(ticker, date) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, adj_close=excluded.adj_close,
            volume=excluded.volume, source=excluded.source
    """
    rows = list(rows)
    conn.executemany(sql, rows)
    return len(rows)


def get_prices(conn: sqlite3.Connection, ticker: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM prices WHERE ticker = ? ORDER BY date", (ticker.upper(),)
    ).fetchall()


def latest_price_date(conn: sqlite3.Connection, ticker: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM prices WHERE ticker = ?", (ticker.upper(),)
    ).fetchone()
    return row["d"] if row else None


# --------------------------------------------------------------------------- #
# indicators
# --------------------------------------------------------------------------- #
def upsert_indicators(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    cols = ["ticker", "date", *MA_KEYS]
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{k}=excluded.{k}" for k in MA_KEYS)
    sql = (
        f"INSERT INTO indicators ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(ticker, date) DO UPDATE SET {updates}"
    )
    rows = list(rows)
    conn.executemany(sql, rows)
    return len(rows)


def get_indicators(conn: sqlite3.Connection, ticker: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM indicators WHERE ticker = ? ORDER BY date", (ticker.upper(),)
    ).fetchall()


# --------------------------------------------------------------------------- #
# signals
# --------------------------------------------------------------------------- #
def upsert_signal(conn: sqlite3.Connection, ticker: str, date: str,
                  signal_type: str, detail: str = "") -> None:
    conn.execute(
        """INSERT INTO signals (ticker, date, signal_type, detail)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(ticker, date, signal_type) DO UPDATE SET detail=excluded.detail""",
        (ticker.upper(), date, signal_type, detail),
    )


def latest_signal(conn: sqlite3.Connection, ticker: str,
                  category_prefix: str | None = None) -> sqlite3.Row | None:
    """Most recent signal for a ticker, optionally filtered by a signal_type prefix
    (used to detect state changes for a given signal family)."""
    if category_prefix:
        return conn.execute(
            """SELECT * FROM signals WHERE ticker = ? AND signal_type LIKE ?
               ORDER BY date DESC, created_at DESC LIMIT 1""",
            (ticker.upper(), f"{category_prefix}%"),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM signals WHERE ticker = ? ORDER BY date DESC, created_at DESC LIMIT 1",
        (ticker.upper(),),
    ).fetchone()


def signals_on_date(conn: sqlite3.Connection, ticker: str, date: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM signals WHERE ticker = ? AND date = ? ORDER BY signal_type",
        (ticker.upper(), date),
    ).fetchall()


# --------------------------------------------------------------------------- #
# runs (audit trail)
# --------------------------------------------------------------------------- #
def record_run(conn: sqlite3.Connection, run_date: str, status: str,
               rows_updated: int, notes: str = "") -> None:
    conn.execute(
        """INSERT INTO runs (run_date, status, rows_updated, notes)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(run_date) DO UPDATE SET
             status=excluded.status, rows_updated=excluded.rows_updated,
             notes=excluded.notes, created_at=datetime('now')""",
        (run_date, status, rows_updated, notes),
    )
