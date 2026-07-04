"""Configuration — all settings from .env + exchange constants.

Exchange constants (source: Bybit public API + docs, verified 2026-06-23):
  BTCUSDT tick_size: 0.10 USDT  (from /v5/market/instruments-info)
  BTCUSDT min_order_qty: 0.001 BTC  (from /v5/market/instruments-info)
  BTCUSDT min_notional: 5 USDT  (from /v5/market/instruments-info)
  BTCUSDT max_leverage: 100x  (from /v5/market/instruments-info)
  Funding interval: 8h (00:00 / 08:00 / 16:00 UTC)  (from /v5/market/tickers)
  Taker fee: 0.055% (VIP0, from Bybit fee schedule docs)
  Maker fee: 0.020% (VIP0, from Bybit fee schedule docs)
  Rate limit (public): 10 req/s per endpoint
  Rate limit (private): 20 req/s per endpoint
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = PROJECT_ROOT / ".env"


def _warn_duplicate_env_keys(path: Path) -> None:
    if not path.exists():
        return
    seen: dict[str, int] = {}
    dups: list[tuple[str, int, int]] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln, raw in enumerate(f, 1):
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k = s.split("=", 1)[0].strip()
            if k in seen:
                dups.append((k, seen[k], ln))
            else:
                seen[k] = ln
    if dups:
        import sys
        for k, first, last in dups:
            print(
                f"[config] WARN duplicate env key '{k}' lines {first} and {last}",
                file=sys.stderr, flush=True,
            )


_warn_duplicate_env_keys(_ENV_PATH)
load_dotenv(_ENV_PATH)


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _get_float(key: str, default: float) -> float:
    v = os.getenv(key)
    return float(v) if v else default


def _get_int(key: str, default: int) -> int:
    v = os.getenv(key)
    return int(v) if v else default


def _get_bool(key: str, default: bool) -> bool:
    v = os.getenv(key, "")
    if not v:
        return default
    return v.lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Bybit exchange constants (source: Bybit API, verified 2026-06-23)
# DO NOT change without re-verifying against live API
# ---------------------------------------------------------------------------
BYBIT_TAKER_FEE_BPS: float = 5.5    # 0.055% VIP0 (source: Bybit fee schedule docs)
BYBIT_MAKER_FEE_BPS: float = 2.0    # 0.020% VIP0 (source: Bybit fee schedule docs)
BYBIT_FUNDING_INTERVAL_H: int = 8   # 8h (source: /v5/market/tickers fundingIntervalHour)
BYBIT_BTC_TICK_SIZE: float = 0.10   # USDT (source: /v5/market/instruments-info)
BYBIT_BTC_MIN_QTY: float = 0.001    # BTC (source: /v5/market/instruments-info)
BYBIT_BTC_MIN_NOTIONAL: float = 5.0 # USDT (source: /v5/market/instruments-info)
BYBIT_RL_PUBLIC: int = 10           # req/s (source: Bybit rate limit docs)
BYBIT_RL_PRIVATE: int = 20          # req/s (source: Bybit rate limit docs)


@dataclass(frozen=True)
class Settings:
    # --- Mode ---
    dry_run: bool        # True = paper only, no real orders
    venue: str           # 'bybit'

    # --- Keys (required only when dry_run=False) ---
    bybit_api_key: str
    bybit_api_secret: str

    # --- Capital ---
    capital: float       # total capital, e.g. 500000
    long_frac: float     # fraction to long BTC, default 0.5
    short_frac: float    # fraction to short basket, default 0.5
    leverage: float      # 1.0 = 1x net, no extra leverage

    # --- Liquidity filter ---
    min_turnover_usd: float    # 24h turnover floor per alt symbol
    liq_cap_pct: float         # max per-name short as fraction of 24h turnover

    # --- Regime gate ---
    regime_lookback_days: int  # trailing window for BTC vs alt spread momentum
    # NOTE: only 30d tested positive; 60/90/180d did NOT — this gate is
    # param-sensitive and fragile. It is a risk control, NOT alpha.

    # --- Risk ---
    max_dd_pct: float          # equity drawdown halt threshold (e.g. 0.20 = 20%)

    # --- Rebalance ---
    rebalance_hour_utc: int    # hour of day to run daily rebalance

    # --- Notifier ---
    tg_bot_token: str
    tg_chat_id: str

    # --- Paths ---
    watchlist_path: str        # path to tradeable_shorts.json
    db_path: str               # SQLite ledger path

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            dry_run=_get_bool("DRY_RUN", True),
            venue=_get("VENUE", "bybit"),
            bybit_api_key=_get("BYBIT_API_KEY", ""),
            bybit_api_secret=_get("BYBIT_API_SECRET", ""),
            capital=_get_float("CAPITAL", 500_000.0),
            long_frac=_get_float("LONG_FRAC", 0.5),
            short_frac=_get_float("SHORT_FRAC", 0.5),
            leverage=_get_float("LEVERAGE", 1.0),
            min_turnover_usd=_get_float("MIN_TURNOVER_USD", 500_000.0),
            liq_cap_pct=_get_float("LIQ_CAP_PCT", 0.003),
            regime_lookback_days=_get_int("REGIME_LOOKBACK_DAYS", 30),
            max_dd_pct=_get_float("MAX_DD_PCT", 0.20),
            rebalance_hour_utc=_get_int("REBALANCE_HOUR_UTC", 0),
            tg_bot_token=_get("TG_BOT_TOKEN", ""),
            tg_chat_id=_get("TG_CHAT_ID", ""),
            watchlist_path=_get(
                "WATCHLIST_PATH",
                str(PROJECT_ROOT / "tradeable_shorts.json"),
            ),
            db_path=_get(
                "DB_PATH",
                str(PROJECT_ROOT / "data" / "paper_ledger.db"),
            ),
        )


def load_tradeable_symbols(path: str) -> list[str]:
    """Load pre-filtered symbol list from JSON file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Watchlist not found: {path}")
    with open(p) as f:
        d = json.load(f)
    syms = d.get("symbols", [])
    if not syms:
        raise ValueError(f"Empty symbol list in {path}")
    return syms
