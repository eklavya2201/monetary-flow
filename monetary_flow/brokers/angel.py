"""Angel One SmartAPI (pip smartapi-python). Free API. Needs: API key (smartapi.angelbroking.com ->
create app), client code, MPIN, TOTP secret (enable TOTP on the SmartAPI page), and the static IP
registered on the app for order placement (SEBI rule)."""
import math
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
import pyotp
from SmartApi import SmartConnect

from . import OPEN, FILLED, CANCELLED, REJECTED, INDEX

IST = timezone(timedelta(hours=5, minutes=30))
SCRIP_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
NIFTY_TOKEN = "99926000"
_STATUS = {"complete": FILLED, "cancelled": CANCELLED, "rejected": REJECTED}


class AngelBroker:
    name = "angel"

    def __init__(self, api_key, client_code, mpin, totp_secret, product="CNC"):
        self.api_key, self.client, self.mpin, self.totp = api_key, client_code, mpin, totp_secret
        self.product = "DELIVERY" if product == "CNC" else "INTRADAY"
        self._api, self._login_day, self._refresh = None, None, None
        self._tokens, self._tokens_day = {}, None

    @property
    def api(self) -> SmartConnect:
        today = datetime.now(IST).date()
        if self._api is None or self._login_day != today:  # sessions end daily
            api = SmartConnect(api_key=self.api_key)
            r = api.generateSession(self.client, self.mpin, pyotp.TOTP(self.totp).now())
            if not r.get("status"):
                raise RuntimeError(f"Angel login failed: {r.get('message')}")
            self._api, self._login_day, self._refresh = api, today, r["data"]["refreshToken"]
        return self._api

    def _ok(self, r) -> dict | list:
        if not r or not r.get("status"):
            raise RuntimeError(f"Angel API error: {(r or {}).get('message')}")
        return r["data"]

    def _inst(self, sym) -> tuple[str, str, float]:
        """(token, tradingsymbol, tick) from Angel's instrument master, refreshed daily."""
        today = datetime.now(IST).date()
        if self._tokens_day != today:
            rows = httpx.get(SCRIP_MASTER, timeout=120).json()
            self._tokens = {r["name"]: (r["token"], r["symbol"], float(r["tick_size"]) / 100 or 0.05)
                            for r in rows if r["exch_seg"] == "NSE" and r["symbol"].endswith("-EQ")}
            self._tokens[INDEX] = (NIFTY_TOKEN, "Nifty 50", 0.05)
            self._tokens_day = today
        return self._tokens[sym]

    @staticmethod
    def _tick(price, tick, down=True):
        f = math.floor if down else math.ceil
        return round(f(price / tick) * tick, 2)

    # --- account -------------------------------------------------------
    def profile(self) -> dict:
        api = self.api
        return self._ok(api.getProfile(self._refresh))

    def funds(self) -> float:
        return float(self._ok(self.api.rmsLimit())["availablecash"])

    def holdings(self) -> list:
        return self._ok(self.api.holding()) or []

    # --- data ----------------------------------------------------------
    def quote(self, sym) -> dict:
        token = self._inst(sym)[0]
        d = self._ok(self.api.getMarketData("FULL", {"NSE": [token]}))["fetched"][0]
        ts = datetime.strptime(d["exchFeedTime"], "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST).timestamp()
        return {"price": float(d["ltp"]), "ts": int(ts), "volume": d.get("tradeVolume"),
                "upper": d.get("upperCircuit"), "lower": d.get("lowerCircuit")}

    def _candles(self, sym, start, end, interval) -> pd.DataFrame:
        token = self._inst(sym)[0]
        rows = self._ok(self.api.getCandleData({
            "exchange": "NSE", "symboltoken": token, "interval": interval,
            "fromdate": start.strftime("%Y-%m-%d %H:%M"), "todate": end.strftime("%Y-%m-%d %H:%M")})) or []
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([r[:6] for r in rows], columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.pop("ts"))
        return df.astype(float).sort_index()

    def candles(self, sym, days=400) -> pd.DataFrame:
        end = datetime.now(IST)
        return self._candles(sym, end - timedelta(days=days), end, "ONE_DAY")

    def bars(self, sym) -> pd.DataFrame:
        end = datetime.now(IST)
        return self._candles(sym, end.replace(hour=9, minute=15), end, "FIVE_MINUTE")

    # --- orders --------------------------------------------------------
    def _place(self, sym, qty, side, variety, order_type, price=0.0, trigger=None) -> str:
        token, tsym, _ = self._inst(sym)
        params = {"variety": variety, "tradingsymbol": tsym, "symboltoken": token, "transactiontype": side,
                  "exchange": "NSE", "ordertype": order_type, "producttype": self.product, "duration": "DAY",
                  "price": str(price), "triggerprice": str(trigger) if trigger else None, "quantity": str(int(qty))}
        oid = self.api.placeOrder(params)
        if not oid:
            raise RuntimeError(f"Angel rejected {side} {sym}")
        return oid

    def buy_limit(self, sym, qty, price):
        return self._place(sym, qty, "BUY", "NORMAL", "LIMIT", price=self._tick(price, self._inst(sym)[2]))

    def sell_stop(self, sym, qty, trigger):
        return self._place(sym, qty, "SELL", "STOPLOSS", "STOPLOSS_MARKET",
                           trigger=self._tick(trigger, self._inst(sym)[2], down=False))

    def sell_market(self, sym, qty):
        # marketable limit 0.5% under last price: works where plain API market orders are restricted
        px = self.quote(sym)["price"]
        return self._place(sym, qty, "SELL", "NORMAL", "LIMIT", price=self._tick(px * 0.995, self._inst(sym)[2]))

    def _find(self, oid) -> dict:
        for o in self._ok(self.api.orderBook()) or []:
            if o.get("orderid") == oid:
                return o
        raise RuntimeError(f"order {oid} not in Angel order book")

    def cancel(self, oid):
        self._ok(self.api.cancelOrder(oid, self._find(oid)["variety"]))

    def order(self, oid) -> dict:
        o = self._find(oid)
        return {"status": _STATUS.get(o.get("orderstatus", "").lower(), OPEN),
                "filled_qty": int(float(o.get("filledshares") or 0)),
                "avg_price": float(o["averageprice"]) if float(o.get("averageprice") or 0) else None, "raw": o}
