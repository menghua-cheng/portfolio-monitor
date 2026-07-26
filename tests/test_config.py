"""Watchlist add-flow + automatic company-name lookup.

add_ticker writes to portfolio.csv and the DB, so the isolated fixture redirects
both onto tmp paths — tests never touch the real watchlist.
"""
import functools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from portfolio_monitor import config, db, fetch  # noqa: E402


# --- fetch_company_name precedence -----------------------------------------
def test_company_name_prefers_tiingo(monkeypatch):
    monkeypatch.setattr(fetch, "_tiingo_name", lambda t: "Apple Inc")
    monkeypatch.setattr(fetch, "_yfinance_name", lambda t: "SHOULD NOT BE USED")
    assert fetch.fetch_company_name("AAPL") == "Apple Inc"


def test_company_name_falls_back_to_yfinance(monkeypatch):
    monkeypatch.setattr(fetch, "_tiingo_name", lambda t: None)
    monkeypatch.setattr(fetch, "_yfinance_name", lambda t: "Apple Inc.")
    assert fetch.fetch_company_name("AAPL") == "Apple Inc."


def test_company_name_none_when_both_fail(monkeypatch):
    monkeypatch.setattr(fetch, "_tiingo_name", lambda t: None)
    monkeypatch.setattr(fetch, "_yfinance_name", lambda t: None)
    assert fetch.fetch_company_name("ZZZZ") is None


def test_tiingo_name_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    assert fetch._tiingo_name("AAPL") is None


# --- add_ticker wiring ------------------------------------------------------
@pytest.fixture
def isolated(tmp_path, monkeypatch):
    csv = tmp_path / "portfolio.csv"
    dbp = tmp_path / "portfolio.db"
    monkeypatch.setattr(config, "PORTFOLIO_CSV", csv)
    monkeypatch.setattr(config, "_tickers_path", lambda: csv)
    monkeypatch.setattr(config.db, "connect", functools.partial(db.connect, dbp))
    return csv


def test_add_ticker_autofetches_name_when_omitted(isolated, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_company_name", lambda s: "NVIDIA Corp")
    resolved = config.add_ticker("nvda")
    assert resolved == "NVIDIA Corp"
    assert {t.symbol: t.name for t in config._read_tickers()}["NVDA"] == "NVIDIA Corp"


def test_add_ticker_explicit_name_skips_lookup(isolated, monkeypatch):
    calls = {"n": 0}

    def spy(_s):
        calls["n"] += 1
        return "AUTO"

    monkeypatch.setattr(fetch, "fetch_company_name", spy)
    resolved = config.add_ticker("AAPL", "My Apple")
    assert resolved == "My Apple" and calls["n"] == 0


def test_add_ticker_preserves_existing_name_on_readd(isolated, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_company_name", lambda s: "AUTO")
    config.add_ticker("AAPL", "Apple Inc")          # explicit name first
    resolved = config.add_ticker("aapl")            # re-add with no name
    assert resolved == "Apple Inc"                  # not overwritten by AUTO


def test_add_ticker_empty_when_lookup_finds_nothing(isolated, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_company_name", lambda s: None)
    resolved = config.add_ticker("ZZZZ")
    assert resolved == ""
    assert {t.symbol: t.name for t in config._read_tickers()}["ZZZZ"] == ""
