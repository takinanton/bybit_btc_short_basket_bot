"""Bybit exchange adapter.

All order placement / cancellation / position queries go through here.
DRY_RUN=true  → logs intent only, simulates fills
DRY_RUN=false → live ccxt-bybit calls (requires API keys)

Rate limits (source: Bybit docs, verified 2026-06-23):
  Public:  10 req/s per endpoint
  Private: 20 req/s per endpoint
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)

# Bybit exchange constants (source: Bybit API 2026-06-23)
TAKER_FEE = 0.00055   # 0.055% VIP0
MAKER_FEE = 0.00020   # 0.020% VIP0


@dataclass
class OrderResult:
    symbol: str
    side: str         # 'Buy' or 'Sell'
    qty: float        # base asset qty
    price: float      # mark price at intended fill
    notional: float   # USD notional
    fee_usd: float    # taker fee estimate
    slippage_bps: float  # estimated from orderbook
    order_id: str     # real or simulated
    is_paper: bool


class BybitExchange:
    """Thin adapter over ccxt bybit for USDT perps.

    In DRY_RUN mode no ccxt calls are made — all public market data
    is fetched via urllib (no keys) and orders are simulated.
    """

    def __init__(self, api_key: str = "", api_secret: str = "", dry_run: bool = True) -> None:
        self.dry_run = dry_run
        self._ccxt: Any = None
        if not dry_run:
            self._init_live(api_key, api_secret)

    def _init_live(self, api_key: str, api_secret: str) -> None:
        try:
            import ccxt
            self._ccxt = ccxt.bybit({
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "linear"},
            })
            self._ccxt.load_markets()
            log.info("ccxt bybit live initialized, %d markets", len(self._ccxt.markets))
        except ImportError:
            raise RuntimeError("ccxt not installed — run: pip install ccxt")

    # ------------------------------------------------------------------
    # Public market data (no keys, urllib — works in dry_run too)
    # ------------------------------------------------------------------

    def get_tickers(self, category: str = "linear") -> dict[str, dict]:
        """Return {symbol: ticker_dict} for all linear perps."""
        import urllib.request, json as _json
        url = f"https://api.bybit.com/v5/market/tickers?category={category}"
        with urllib.request.urlopen(url, timeout=15) as r:
            data = _json.load(r)
        out = {}
        for t in data.get("result", {}).get("list", []):
            out[t["symbol"]] = t
        return out

    def get_mark_price(self, symbol: str) -> float:
        """Fetch mark price for one symbol."""
        import urllib.request, json as _json
        url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = _json.load(r)
        lst = data.get("result", {}).get("list", [])
        if not lst:
            raise ValueError(f"No ticker for {symbol}")
        return float(lst[0]["markPrice"])

    def get_klines(self, symbol: str, interval: str = "D", limit: int = 60) -> list[dict]:
        """Fetch OHLCV klines. interval: '1','5','15','60','240','D','W','M'."""
        import urllib.request, json as _json
        url = (
            f"https://api.bybit.com/v5/market/kline"
            f"?category=linear&symbol={symbol}&interval={interval}&limit={limit}"
        )
        with urllib.request.urlopen(url, timeout=15) as r:
            data = _json.load(r)
        rows = data.get("result", {}).get("list", [])
        # Each row: [startTime, open, high, low, close, volume, turnover]
        result = []
        for row in rows:
            result.append({
                "ts": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "turnover": float(row[6]),
            })
        # API returns newest first — reverse to chronological
        result.reverse()
        return result

    def get_orderbook(self, symbol: str, limit: int = 5) -> dict:
        """Fetch top-N orderbook for slippage estimation."""
        import urllib.request, json as _json
        url = (
            f"https://api.bybit.com/v5/market/orderbook"
            f"?category=linear&symbol={symbol}&limit={limit}"
        )
        with urllib.request.urlopen(url, timeout=10) as r:
            data = _json.load(r)
        result = data.get("result", {})
        return {
            "bids": [(float(p), float(q)) for p, q in result.get("b", [])],
            "asks": [(float(p), float(q)) for p, q in result.get("a", [])],
        }

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Current predicted funding rate for symbol."""
        import urllib.request, json as _json
        url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = _json.load(r)
        lst = data.get("result", {}).get("list", [])
        if not lst:
            return None
        fr = lst[0].get("fundingRate")
        return float(fr) if fr else None

    def get_funding_history(self, symbol: str, limit: int = 10) -> list[dict]:
        """Recent funding rate history."""
        import urllib.request, json as _json
        url = (
            f"https://api.bybit.com/v5/market/funding/history"
            f"?category=linear&symbol={symbol}&limit={limit}"
        )
        with urllib.request.urlopen(url, timeout=10) as r:
            data = _json.load(r)
        rows = data.get("result", {}).get("list", [])
        return [
            {"ts": int(r["fundingRateTimestamp"]), "rate": float(r["fundingRate"])}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Slippage estimation
    # ------------------------------------------------------------------

    def estimate_slippage_bps(self, symbol: str, side: str, notional_usd: float) -> float:
        """Estimate market-order slippage in bps by sweeping orderbook.

        side: 'Buy' sweeps asks, 'Sell' sweeps bids.
        Returns slippage in basis points vs mid-price.
        Returns 0 if book too thin to fill (caller should log and skip).
        """
        try:
            book = self.get_orderbook(symbol, limit=10)
            levels = book["asks"] if side == "Buy" else book["bids"]
            if not levels:
                return 0.0
            mid = (book["asks"][0][0] + book["bids"][0][0]) / 2.0
            remaining = notional_usd
            filled_notional = 0.0
            filled_cost = 0.0
            for price, qty in levels:
                level_notional = price * qty
                take = min(remaining, level_notional)
                filled_cost += take * (price / price)  # = take, but price-weighted below
                filled_notional += take
                remaining -= take
                if remaining <= 0:
                    break
            if filled_notional <= 0:
                return 0.0
            # weighted avg price
            remaining2 = notional_usd
            wavg_price = 0.0
            for price, qty in levels:
                level_notional = price * qty
                take = min(remaining2, level_notional)
                wavg_price += price * (take / notional_usd)
                remaining2 -= take
                if remaining2 <= 0:
                    break
            if mid <= 0:
                return 0.0
            slip_bps = abs(wavg_price - mid) / mid * 10_000
            return slip_bps
        except Exception as e:
            log.warning("slippage estimation failed for %s: %s", symbol, e)
            return 0.0

    # ------------------------------------------------------------------
    # Order placement (dry-run or live)
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,          # 'Buy' or 'Sell'
        qty: float,         # base asset qty
        mark_price: float,  # current mark price for notional / fee calc
        reduce_only: bool = False,
    ) -> OrderResult:
        """Place market order. In dry_run: log + simulate. Live: ccxt market order."""
        notional = qty * mark_price
        fee_usd = notional * TAKER_FEE
        slip_bps = self.estimate_slippage_bps(symbol, side, notional)

        if self.dry_run:
            order_id = f"PAPER-{symbol}-{side}-{int(time.time()*1000)}"
            log.info(
                "[DRY_RUN] INTENDED ORDER | sym=%s side=%s qty=%.6f "
                "mark_px=%.4f notional=$%.2f fee=$%.2f slip=%.2fbps",
                symbol, side, qty, mark_price, notional, fee_usd, slip_bps,
            )
            return OrderResult(
                symbol=symbol, side=side, qty=qty, price=mark_price,
                notional=notional, fee_usd=fee_usd,
                slippage_bps=slip_bps, order_id=order_id, is_paper=True,
            )

        # Live path
        if self._ccxt is None:
            raise RuntimeError("ccxt not initialized — DRY_RUN=false requires API keys")
        params: dict = {}
        if reduce_only:
            params["reduceOnly"] = True
        try:
            resp = self._ccxt.create_market_order(
                symbol=symbol,
                side=side.lower(),
                amount=qty,
                params=params,
            )
            fill_price = float(resp.get("average") or resp.get("price") or mark_price)
            order_id = str(resp.get("id", "unknown"))
            actual_fee = float(resp.get("fee", {}).get("cost") or fee_usd)
            log.info(
                "[LIVE] FILLED | sym=%s side=%s qty=%.6f fill_px=%.4f "
                "notional=$%.2f fee=$%.2f id=%s",
                symbol, side, qty, fill_price, notional, actual_fee, order_id,
            )
            return OrderResult(
                symbol=symbol, side=side, qty=qty, price=fill_price,
                notional=notional, fee_usd=actual_fee,
                slippage_bps=slip_bps, order_id=order_id, is_paper=False,
            )
        except Exception as e:
            log.error("place_order FAILED | sym=%s side=%s qty=%.6f err=%s", symbol, side, qty, e)
            raise

    def cancel_all_orders(self, symbol: str) -> None:
        """Cancel all open orders for symbol. No-op in dry_run."""
        if self.dry_run:
            log.info("[DRY_RUN] cancel_all_orders(%s) — no-op", symbol)
            return
        if self._ccxt is None:
            return
        try:
            self._ccxt.cancel_all_orders(symbol, params={"category": "linear"})
        except Exception as e:
            log.warning("cancel_all_orders(%s) error: %s", symbol, e)

    def close_position(self, symbol: str, side: str, qty: float, mark_price: float) -> Optional[OrderResult]:
        """Close existing position (reduce-only market order)."""
        close_side = "Sell" if side == "Buy" else "Buy"
        return self.place_order(symbol, close_side, qty, mark_price, reduce_only=True)

    def get_positions(self) -> list[dict]:
        """Get open positions. Returns [] in dry_run (ledger is source of truth)."""
        if self.dry_run:
            return []
        if self._ccxt is None:
            return []
        try:
            pos = self._ccxt.fetch_positions(params={"category": "linear"})
            return [p for p in pos if float(p.get("contracts") or 0) != 0]
        except Exception as e:
            log.error("get_positions error: %s", e)
            return []
