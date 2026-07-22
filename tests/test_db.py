"""Verify the DB layer: schema creation, idempotent upserts, round-trip reads."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import db  # noqa: E402


def _sample_price(date: str, close: float, source: str = "yfinance") -> dict:
    return dict(ticker="TEST", date=date, open=close - 1, high=close + 1,
                low=close - 2, close=close, adj_close=close, volume=1000, source=source)


def test_prices_upsert_idempotent(tmp_path):
    dbp = tmp_path / "t.db"
    with db.connect(dbp) as conn:
        db.upsert_prices(conn, [_sample_price("2026-01-02", 100.0)])
        db.upsert_prices(conn, [_sample_price("2026-01-02", 100.0)])  # re-insert same key
    with db.connect(dbp) as conn:
        rows = db.get_prices(conn, "TEST")
    assert len(rows) == 1, "duplicate key must not create a second row"
    assert rows[0]["close"] == 100.0


def test_prices_upsert_updates_value(tmp_path):
    dbp = tmp_path / "t.db"
    with db.connect(dbp) as conn:
        db.upsert_prices(conn, [_sample_price("2026-01-02", 100.0)])
        db.upsert_prices(conn, [_sample_price("2026-01-02", 111.0, source="stooq")])
    with db.connect(dbp) as conn:
        rows = db.get_prices(conn, "TEST")
    assert len(rows) == 1
    assert rows[0]["close"] == 111.0
    assert rows[0]["source"] == "stooq"


def test_securities_crud(tmp_path):
    dbp = tmp_path / "t.db"
    with db.connect(dbp) as conn:
        db.upsert_security(conn, "AAPL", "Apple Inc.")
        db.upsert_security(conn, "AAPL", "Apple Inc.")  # idempotent
        db.upsert_security(conn, "msft", "Microsoft")   # lowercased -> MSFT
    with db.connect(dbp) as conn:
        rows = db.list_securities(conn)
        tickers = [r["ticker"] for r in rows]
    assert tickers == ["AAPL", "MSFT"]
    with db.connect(dbp) as conn:
        db.remove_security(conn, "aapl")
    with db.connect(dbp) as conn:
        assert [r["ticker"] for r in db.list_securities(conn)] == ["MSFT"]


def test_signals_and_indicators(tmp_path):
    dbp = tmp_path / "t.db"
    with db.connect(dbp) as conn:
        ind = {"ticker": "TEST", "date": "2026-01-02"}
        ind.update({k: 1.0 for k in db.MA_KEYS})
        db.upsert_indicators(conn, [ind])
        db.upsert_indicators(conn, [ind])  # idempotent
        db.upsert_signal(conn, "TEST", "2026-01-02", "GOLDEN_CROSS", "sma5>sma20")
        db.upsert_signal(conn, "TEST", "2026-01-02", "GOLDEN_CROSS", "updated")  # idempotent
    with db.connect(dbp) as conn:
        assert len(db.get_indicators(conn, "TEST")) == 1
        sigs = db.signals_on_date(conn, "TEST", "2026-01-02")
        assert len(sigs) == 1 and sigs[0]["detail"] == "updated"


def test_runs_audit(tmp_path):
    dbp = tmp_path / "t.db"
    with db.connect(dbp) as conn:
        db.record_run(conn, "2026-01-02", "ok", 5, "first")
        db.record_run(conn, "2026-01-02", "ok", 7, "rerun")  # idempotent on run_date
    with db.connect(dbp) as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_date='2026-01-02'").fetchone()
    assert row["rows_updated"] == 7 and row["notes"] == "rerun"
