"""Paper broker: real prices from a data source, simulated fills, no money moves."""
import json
import uuid
from pathlib import Path

from . import OPEN, FILLED, CANCELLED


class PaperBroker:
    name = "paper"

    def __init__(self, data, path: Path, funds: float):
        self.data, self.path, self._funds = data, path, funds
        self.orders = json.loads(path.read_text()) if path.exists() else {}

    def _save(self):
        self.path.write_text(json.dumps(self.orders, indent=1))

    def _new(self, **o) -> str:
        oid = "P" + uuid.uuid4().hex[:12]
        self.orders[oid] = {**o, "status": OPEN, "filled_qty": 0, "avg_price": None}
        self._save()
        return oid

    def funds(self) -> float:
        return self._funds

    def quote(self, sym):
        return self.data.quote(sym)

    def candles(self, sym, days=400):
        return self.data.candles(sym, days)

    def buy_limit(self, sym, qty, price):
        return self._new(sym=sym, qty=qty, side="BUY", type="LIMIT", price=price)

    def sell_stop(self, sym, qty, trigger):
        return self._new(sym=sym, qty=qty, side="SELL", type="SL_M", price=trigger)

    def sell_market(self, sym, qty):
        return self._new(sym=sym, qty=qty, side="SELL", type="MARKET", price=None)

    def cancel(self, oid):
        if self.orders[oid]["status"] == OPEN:
            self.orders[oid]["status"] = CANCELLED
            self._save()

    def order(self, oid) -> dict:
        o = self.orders[oid]
        if o["status"] == OPEN:
            px = self.data.quote(o["sym"])["price"]
            hit = px is not None and (
                o["type"] == "MARKET"
                or (o["type"] == "LIMIT" and px <= o["price"])
                or (o["type"] == "SL_M" and px <= o["price"]))
            if hit:
                fill = min(px, o["price"]) if o["type"] == "LIMIT" else px
                o.update(status=FILLED, filled_qty=o["qty"], avg_price=round(fill, 2))
                self._save()
        return {k: o[k] for k in ("status", "filled_qty", "avg_price")}
