"""Configuration and the watchlist-management CLI.

Configuration lives in three places:
  * config/settings.yaml    program settings (tracked in git)
  * config/portfolio.csv    the watchlist (NOT tracked; personal holdings)
  * .env                    secrets: SMTP + optional API keys (NOT tracked)

The watchlist is a CSV so the repository ships without anyone's holdings. Until
you create config/portfolio.csv, the loader falls back to portfolio.example.csv.

CLI:
    python -m portfolio_monitor.config list
    python -m portfolio_monitor.config add NVDA "NVIDIA Corp."
    python -m portfolio_monitor.config remove NVDA
    python -m portfolio_monitor.config sync
"""
from __future__ import annotations

import argparse
import csv
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from . import db

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = _REPO_ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
PORTFOLIO_CSV = CONFIG_DIR / "portfolio.csv"
PORTFOLIO_EXAMPLE_CSV = CONFIG_DIR / "portfolio.example.csv"
ENV_PATH = _REPO_ROOT / ".env"


@dataclass
class EmailConfig:
    host: str
    port: int
    user: str
    app_password: str
    recipient: str
    cc: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.user and self.app_password and self.recipient)


@dataclass
class Ticker:
    symbol: str
    name: str = ""


@dataclass
class Config:
    tickers: list[Ticker]
    settings: dict
    email: EmailConfig

    # convenience accessors -------------------------------------------------
    @property
    def symbols(self) -> list[str]:
        return [t.symbol for t in self.tickers]

    @property
    def ma_periods(self) -> dict[str, int]:
        return self.settings["ma_periods"]

    @property
    def history_years(self) -> int:
        return int(self.settings.get("history_years", 2))

    @property
    def slope_lookback(self) -> int:
        return int(self.settings.get("signals", {}).get("slope_lookback", 10))

    @property
    def flat_threshold_pct(self) -> float:
        return float(self.settings.get("signals", {}).get("flat_threshold_pct", 0.5))

    @property
    def double_window_days(self) -> int:
        """Calendar-day window for tagging a second MA cross as a dual-trend signal."""
        return int(self.settings.get("signals", {}).get("double_window_days", 30))

    @property
    def recent_window_days(self) -> int:
        """Calendar-day window for the trend summary (recent crosses + multi-break tags)."""
        return int(self.settings.get("signals", {}).get("recent_window_days", 30))

    @property
    def crosscheck_tolerance_pct(self) -> float:
        return float(self.settings.get("data", {}).get("crosscheck_tolerance_pct", 1.0))

    @property
    def backtest_cost_bps(self) -> float:
        """Per-side trading cost (basis points) applied to every backtest fill."""
        return float(self.settings.get("backtest", {}).get("cost_bps", 5.0))

    @property
    def backtest_starting_cash(self) -> float:
        """Notional capital for the backtest equity curve (percentage metrics are
        scale-free, so this only sets the curve's units)."""
        return float(self.settings.get("backtest", {}).get("starting_cash", 10000.0))


# --------------------------------------------------------------------------- #
# settings.yaml
# --------------------------------------------------------------------------- #
def _load_settings(path: Path = SETTINGS_PATH) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# --------------------------------------------------------------------------- #
# portfolio.csv (watchlist)
# --------------------------------------------------------------------------- #
def _tickers_path() -> Path:
    """The active watchlist file: the user's portfolio.csv if present, otherwise
    the shipped example so a fresh clone still runs."""
    return PORTFOLIO_CSV if PORTFOLIO_CSV.exists() else PORTFOLIO_EXAMPLE_CSV


def _read_tickers(path: Path | None = None) -> list[Ticker]:
    path = path or _tickers_path()
    if not path.exists():
        return []
    tickers: list[Ticker] = []
    with open(path, "r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            symbol = (row.get("symbol") or "").strip()
            if not symbol or symbol.startswith("#"):
                continue
            tickers.append(Ticker(symbol=symbol.upper(), name=(row.get("name") or "").strip()))
    return tickers


def _write_tickers(tickers: list[Ticker]) -> None:
    """Persist the watchlist to portfolio.csv (never the example)."""
    PORTFOLIO_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(PORTFOLIO_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "name"])
        for t in tickers:
            writer.writerow([t.symbol, t.name])


def load_config(env_path: Path = ENV_PATH) -> Config:
    """Load settings + watchlist + email secrets into a Config."""
    if env_path.exists():
        load_dotenv(env_path)

    cc_raw = os.getenv("REPORT_CC", "")
    email = EmailConfig(
        host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        port=int(os.getenv("SMTP_PORT", "587")),
        user=os.getenv("SMTP_USER", ""),
        app_password=os.getenv("SMTP_APP_PASSWORD", ""),
        recipient=os.getenv("REPORT_RECIPIENT", ""),
        cc=[c.strip() for c in cc_raw.split(",") if c.strip()],
    )
    return Config(tickers=_read_tickers(), settings=_load_settings(), email=email)


def add_ticker(symbol: str, name: str = "") -> str:
    """Add or update a watchlist ticker. When no name is given, keep any existing
    name, otherwise look one up automatically (Tiingo/yfinance). Returns the
    resolved name (possibly empty if the lookup found nothing)."""
    symbol = symbol.upper()
    tickers = {t.symbol: t for t in _read_tickers()}
    existing = tickers.get(symbol)
    if not name:
        if existing and existing.name:
            name = existing.name
        else:
            from . import fetch  # local import keeps yfinance out of module load
            # The add CLI doesn't go through load_config(), so load .env here to
            # make TIINGO_API_KEY available to the (preferred) Tiingo name lookup.
            if ENV_PATH.exists():
                load_dotenv(ENV_PATH)
            name = fetch.fetch_company_name(symbol) or ""
    tickers[symbol] = Ticker(symbol=symbol, name=name)
    _write_tickers(sorted(tickers.values(), key=lambda t: t.symbol))
    with db.connect() as conn:
        db.upsert_security(conn, symbol, name or None)
    return name


def remove_ticker(symbol: str) -> None:
    symbol = symbol.upper()
    _write_tickers([t for t in _read_tickers() if t.symbol != symbol])
    with db.connect() as conn:
        db.remove_security(conn, symbol)


def sync_securities() -> None:
    """Ensure the DB securities table matches the watchlist exactly."""
    tickers = _read_tickers()
    with db.connect() as conn:
        existing = {r["ticker"] for r in db.list_securities(conn)}
        wanted = {t.symbol for t in tickers}
        for t in tickers:
            db.upsert_security(conn, t.symbol, t.name or None)
        for stale in existing - wanted:
            db.remove_security(conn, stale)


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="portfolio_monitor.config",
                                     description="Manage the portfolio watchlist.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="List watchlist tickers.")
    p_add = sub.add_parser("add", help="Add or update a ticker.")
    p_add.add_argument("symbol")
    p_add.add_argument("name", nargs="?", default="",
                       help="Optional; auto-looked-up (Tiingo/yfinance) when omitted.")
    p_rm = sub.add_parser("remove", help="Remove a ticker.")
    p_rm.add_argument("symbol")
    sub.add_parser("sync", help="Sync the DB securities table to the watchlist.")

    args = parser.parse_args(argv)
    if args.cmd == "list":
        cfg = load_config()
        for t in cfg.tickers:
            print(f"{t.symbol:8s} {t.name}")
        print(f"\n{len(cfg.tickers)} ticker(s) from {_tickers_path().name}.")
    elif args.cmd == "add":
        resolved = add_ticker(args.symbol, args.name)
        sym = args.symbol.upper()
        if resolved:
            print(f"Added {sym} ({resolved}).")
        else:
            print(f"Added {sym} (name not found; pass it explicitly to set one).")
    elif args.cmd == "remove":
        remove_ticker(args.symbol)
        print(f"Removed {args.symbol.upper()}.")
    elif args.cmd == "sync":
        sync_securities()
        print("Synced DB securities to watchlist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
