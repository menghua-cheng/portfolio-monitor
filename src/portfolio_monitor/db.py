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

-- Cached history from the independent cross-check sources (Tiingo/Stooq). Kept
-- apart from `prices` so the canonical series stays one row per (ticker, date)
-- while several sources can be cached side by side (ADR-0007).
CREATE TABLE IF NOT EXISTS prices_ref (
    ticker    TEXT NOT NULL,
    source    TEXT NOT NULL,
    date      TEXT NOT NULL,          -- ISO YYYY-MM-DD
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    adj_close REAL,
    volume    INTEGER,
    PRIMARY KEY (ticker, source, date)
);

-- Per (ticker, source) cache bookkeeping: what we hold, and when we last looked.
-- This is what makes a run incremental — it answers "from which date do I fetch?"
-- without scanning the price table.
CREATE TABLE IF NOT EXISTS price_sync (
    ticker         TEXT NOT NULL,
    source         TEXT NOT NULL,
    first_date     TEXT,
    last_date      TEXT,
    rows           INTEGER,
    last_synced_at TEXT,
    action         TEXT,              -- full | incremental | up-to-date | failed
    note           TEXT,
    PRIMARY KEY (ticker, source)
);

-- Audit of detected price re-basings (splits, and dividend-only adj_close
-- shifts). A split silently rewrites every historical close upstream, so the
-- cache MUST notice and rescale rather than blend two price bases.
CREATE TABLE IF NOT EXISTS corporate_actions (
    ticker         TEXT NOT NULL,
    detected_on    TEXT NOT NULL,     -- ISO date the sync noticed it
    kind           TEXT NOT NULL,     -- split | adjustment
    factor         REAL,              -- new_price / old_price (0.25 == 4:1 split)
    effective_from TEXT,              -- earliest overlap date showing the new basis
    rows_rescaled  INTEGER,
    note           TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, detected_on, kind)
);

CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

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


def get_prices(conn: sqlite3.Connection, ticker: str, start: str | None = None,
               end: str | None = None) -> list[sqlite3.Row]:
    """Stored bars for a ticker, optionally bounded by ISO dates.

    The bounds matter now that the cache may hold far more history than any one
    caller wants: the daily report asks for its `history_years`, while the
    explorer asks for everything.
    """
    sql = "SELECT * FROM prices WHERE ticker = ?"
    params: list = [ticker.upper()]
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    return conn.execute(sql + " ORDER BY date", params).fetchall()


def latest_price_date(conn: sqlite3.Connection, ticker: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM prices WHERE ticker = ?", (ticker.upper(),)
    ).fetchone()
    return row["d"] if row else None


def price_span(conn: sqlite3.Connection, ticker: str) -> tuple[str | None, str | None, int]:
    """(first_date, last_date, row_count) for a ticker's cached bars."""
    row = conn.execute(
        "SELECT MIN(date) AS lo, MAX(date) AS hi, COUNT(*) AS n FROM prices WHERE ticker = ?",
        (ticker.upper(),)).fetchone()
    return (row["lo"], row["hi"], int(row["n"] or 0)) if row else (None, None, 0)


def tickers_with_prices(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM prices ORDER BY ticker").fetchall()]


def rescale_prices(conn: sqlite3.Connection, ticker: str, factor: float,
                   before_date: str) -> int:
    """Rebase cached bars **older** than `before_date` onto a new price basis.

    When a split happens, the upstream source silently divides every historical
    close by the split ratio. Rows we already hold are on the *old* basis, and an
    incremental fetch only replaces recent ones — so without this the series
    would contain a discontinuity that looks exactly like a 75% one-day crash.

    Prices scale by `factor`; volume scales by its inverse (more shares, same
    notional). Returns the number of rows changed.
    """
    if factor <= 0 or factor == 1.0:
        return 0
    cur = conn.execute(
        """UPDATE prices SET
              open = open * :f, high = high * :f, low = low * :f,
              close = close * :f, adj_close = adj_close * :f,
              volume = CAST(volume / :f AS INTEGER)
           WHERE ticker = :t AND date < :d""",
        {"f": factor, "t": ticker.upper(), "d": before_date})
    return cur.rowcount


# --------------------------------------------------------------------------- #
# prices_ref (cached cross-check sources)
# --------------------------------------------------------------------------- #
_REF_COLS = ["ticker", "source", "date", "open", "high", "low", "close",
             "adj_close", "volume"]


def upsert_prices_ref(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    placeholders = ", ".join(f":{c}" for c in _REF_COLS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in _REF_COLS[3:])
    sql = (f"INSERT INTO prices_ref ({', '.join(_REF_COLS)}) VALUES ({placeholders}) "
           f"ON CONFLICT(ticker, source, date) DO UPDATE SET {updates}")
    rows = list(rows)
    conn.executemany(sql, rows)
    return len(rows)


def get_prices_ref(conn: sqlite3.Connection, ticker: str, source: str | None = None,
                   start: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM prices_ref WHERE ticker = ?"
    params: list = [ticker.upper()]
    if source:
        sql += " AND source = ?"
        params.append(source)
    if start:
        sql += " AND date >= ?"
        params.append(start)
    return conn.execute(sql + " ORDER BY date", params).fetchall()


def latest_ref_date(conn: sqlite3.Connection, ticker: str, source: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM prices_ref WHERE ticker = ? AND source = ?",
        (ticker.upper(), source)).fetchone()
    return row["d"] if row else None


# --------------------------------------------------------------------------- #
# price_sync (cache bookkeeping) + corporate_actions (rebase audit)
# --------------------------------------------------------------------------- #
def record_sync(conn: sqlite3.Connection, ticker: str, source: str,
                first_date: str | None, last_date: str | None, rows: int,
                action: str, note: str = "") -> None:
    conn.execute(
        """INSERT INTO price_sync (ticker, source, first_date, last_date, rows,
                                   last_synced_at, action, note)
           VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?)
           ON CONFLICT(ticker, source) DO UPDATE SET
             first_date=excluded.first_date, last_date=excluded.last_date,
             rows=excluded.rows, last_synced_at=excluded.last_synced_at,
             action=excluded.action, note=excluded.note""",
        (ticker.upper(), source, first_date, last_date, rows, action, note))


def get_sync(conn: sqlite3.Connection, ticker: str | None = None) -> list[sqlite3.Row]:
    if ticker:
        return conn.execute(
            "SELECT * FROM price_sync WHERE ticker = ? ORDER BY source",
            (ticker.upper(),)).fetchall()
    return conn.execute("SELECT * FROM price_sync ORDER BY ticker, source").fetchall()


def record_corporate_action(conn: sqlite3.Connection, ticker: str, detected_on: str,
                            kind: str, factor: float | None,
                            effective_from: str | None, rows_rescaled: int,
                            note: str = "") -> None:
    conn.execute(
        """INSERT INTO corporate_actions (ticker, detected_on, kind, factor,
                                          effective_from, rows_rescaled, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(ticker, detected_on, kind) DO UPDATE SET
             factor=excluded.factor, effective_from=excluded.effective_from,
             rows_rescaled=excluded.rows_rescaled, note=excluded.note,
             created_at=datetime('now')""",
        (ticker.upper(), detected_on, kind, factor, effective_from, rows_rescaled, note))


def list_corporate_actions(conn: sqlite3.Connection,
                           ticker: str | None = None) -> list[sqlite3.Row]:
    if ticker:
        return conn.execute(
            "SELECT * FROM corporate_actions WHERE ticker = ? ORDER BY detected_on DESC",
            (ticker.upper(),)).fetchall()
    return conn.execute(
        "SELECT * FROM corporate_actions ORDER BY detected_on DESC, ticker").fetchall()


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
