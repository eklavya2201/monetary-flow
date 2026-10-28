"""Groww Trade API (pip growwapi). Needs an active Trade API subscription, the API key + TOTP
secret from groww.in -> Groww Cloud -> API Keys, and orders must come from the static IP
registered on that page (SEBI rule)."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pyotp
from growwapi import GrowwAPI

from . import OPEN, FILLED, CANCELLED, REJECTED, INDEX

IST = timezone(timedelta(hours=5, minutes=30))
_STATUS = {"EXECUTED": FILLED, "COMPLETED": FILLED, "DELIVERY_AWAITED": FILLED,
           "CANCELLED": CANCELLED, "REJECTED": REJECTED, "FAILED": REJECTED}


def _token_expiry(now: datetime) -> datetime:
    """Groww access tokens expire daily at 06:00 IST."""
    six = now.astimezone(IST).replace(hour=6, minute=0, second=0, microsecond=0)
    return six if now.astimezone(IST) < six else six + timedelta(days=1)


class GrowwBroker:
    name = "groww"

    def __init__(self, api_key: str, totp_secret: str, product: str = "CNC"):
        self.api_key, self.totp_secret, self.product = api_key, totp_secret, product
        self._g, self._expires = None, datetime.min.replace(tzinfo=IST)

    @property
    def g(self) -> GrowwAPI:
        now = datetime.now(IST)
        if self._g is None or now >= self._expires:
            token = GrowwAPI.get_access_token(api_key=self.api_key, totp=pyotp.TOTP(self.totp_secret).now())
            self._g, self._expires = GrowwAPI(token), _token_expiry(now)
        return self._g

    # --- account -------------------------------------------------------
    def profile(self) -> dict:
        return self.g.get_user_profile(timeout=10)

    def funds(self) -> float:
        m = self.g.get_available_margin_details(timeout=10)
        key = "cnc_balance_available" if self.product == "CNC" else "mis_balance_available"
        return float(m["equity_margin_details"][key])

    def holdings(self) -> list:
        return self.g.get_holdings_for_user(timeout=10).get("holdings", [])

    def positions(self) -> list:
        return self.g.get_positions_for_user(segment=self.g.SEGMENT_CASH, timeout=10).get("positions", [])

    # --- data ----------------------------------------------------------
    def quote(self, sym: str) -> dict:
        q = self.g.get_quote(trading_symbol=sym, exchange=self.g.EXCHANGE_NSE, segment=self.g.SEGMENT_CASH, timeout=10)
        return {"price": q.get("last_price"), "ts": int((q.get("last_trade_time") or 0) / 1000),
                "volume": q.get("volume"), "upper": q.get("upper_circuit_limit"), "lower": q.get("lower_circuit_limit")}

    def candles(self, sym: str, days: int = 400) -> pd.DataFrame:
        end = datetime.now(IST)
        return self._candles(sym, end - timedelta(days=days), end, self.g.CANDLE_INTERVAL_DAY)

    def bars(self, sym: str) -> pd.DataFrame:
        end = datetime.now(IST)
        return self._candles(sym, end.replace(hour=9, minute=15, second=0), end, self.g.CANDLE_INTERVAL_MIN_5)

    def _candles(self, sym, start, end, interval) -> pd.DataFrame:
        r = self.g.get_historical_candles(
            exchange=self.g.EXCHANGE_NSE, segment=self.g.SEGMENT_CASH, groww_symbol=f"NSE-{sym}",
            start_time=start.strftime("%Y-%m-%d %H:%M:%S"), end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
            candle_interval=interval, timeout=20)
        rows = r.get("candles", []) if isinstance(r, dict) else r
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([row[:6] for row in rows], columns=["ts", "open", "high", "low", "close", "volume"])
        ts = df["ts"]
        if pd.api.types.is_numeric_dtype(ts):
            df.index = pd.to_datetime(ts, unit="ms" if ts.max() > 1e11 else "s", utc=True).dt.tz_convert(IST)
        else:
            df.index = pd.to_datetime(ts)
        return df.drop(columns="ts").astype(float).sort_index()

    # --- orders --------------------------------------------------------
    def _place(self, sym, qty, side, order_type, price=0.0, trigger=None) -> str:
        g = self.g
        r = g.place_order(trading_symbol=sym, quantity=int(qty), validity=g.VALIDITY_DAY,
                          exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH, product=self.product,
                          order_type=order_type, transaction_type=side, price=price,
                          trigger_price=trigger, timeout=10)
        return r["groww_order_id"]

    def buy_limit(self, sym, qty, price):
        return self._place(sym, qty, "BUY", "LIMIT", price=round(price, 2))

    def sell_stop(self, sym, qty, trigger):
        return self._place(sym, qty, "SELL", "SL_M", trigger=round(trigger, 2))

    def sell_market(self, sym, qty):
        return self._place(sym, qty, "SELL", "MARKET")

    def cancel(self, oid):
        self.g.cancel_order(groww_order_id=oid, segment=self.g.SEGMENT_CASH, timeout=10)

    def order(self, oid) -> dict:
        d = self.g.get_order_detail(segment=self.g.SEGMENT_CASH, groww_order_id=oid, timeout=10)
        avg = next((d[k] for k in ("average_fill_price", "avg_fill_price", "average_price") if d.get(k)), None)
        return {"status": _STATUS.get(d.get("order_status"), OPEN),
                "filled_qty": int(d.get("filled_quantity") or 0),
                "avg_price": float(avg) if avg else None, "raw": d}
