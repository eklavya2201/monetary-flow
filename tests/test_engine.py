"""Order lifecycle against a fake broker: entry fills -> stop placed -> target -> closed."""
import time
from datetime import datetime

from monetary_flow import engine as eng
from monetary_flow.brokers import OPEN, FILLED
from monetary_flow.store import Store


class FakeBroker:
    def __init__(self):
        self.px, self.orders, self.n = {"NIFTY": 25000, "ITC": 400}, {}, 0

    def funds(self): return 100000
    def quote(self, s): return {"price": self.px[s], "ts": time.time()}
    def _new(self, **o):
        self.n += 1
        self.orders[str(self.n)] = {**o, "status": OPEN}
        return str(self.n)
    def buy_limit(self, s, q, p): return self._new(sym=s, side="BUY", price=p)
    def sell_stop(self, s, q, t): return self._new(sym=s, side="SL", price=t)
    def sell_market(self, s, q): return self._new(sym=s, side="MKT", price=None)
    def cancel(self, oid): self.orders[oid]["status"] = "CANCELLED"
    def order(self, oid):
        o = self.orders[oid]
        return {"status": o["status"], "filled_qty": 0, "avg_price": o.get("fill")}


def setup(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "t.db"), "k" * 40)
    e = eng.Engine(st, tmp_path)
    b = FakeBroker()
    e.broker = lambda: b
    e.max_quote_age = lambda: 180
    e._index_change = lambda b, n: 0.0
    monkeypatch.setattr(eng, "session_open", lambda t: True)
    monkeypatch.setattr(eng, "now_ist", lambda: datetime(2026, 10, 28, 11, 0, tzinfo=eng.IST))
    tid = st.add_trade(mode="paper", symbol="ITC", status="PENDING", qty=5, plan_entry=400, stop=390,
                       target=425, why="test", risk_amount=40, entry_order=b.buy_limit("ITC", 5, 400),
                       opened_at=time.time())
    st.put("running", True)
    e.try_entry = lambda *a: "no entry in test"
    return st, e, b, tid


def test_fill_protect_target_close(tmp_path, monkeypatch):
    st, e, b, tid = setup(tmp_path, monkeypatch)
    b.orders["1"].update(status=FILLED, fill=400.0)
    e.cycle()
    t = st.trades("id=?", (tid,))[0]
    assert t["status"] == "OPEN" and t["stop_order"] and b.orders[t["stop_order"]]["side"] == "SL"

    b.px["ITC"] = 426
    e.cycle()
    t = st.trades("id=?", (tid,))[0]
    assert t["status"] == "EXITING" and b.orders[t["stop_order"]]["status"] == "CANCELLED"

    b.orders[t["exit_order"]].update(status=FILLED, fill=426.0)
    e.cycle()
    t = st.trades("id=?", (tid,))[0]
    assert t["status"] == "CLOSED" and t["pnl"] < 5 * 26 and t["pnl"] > 0  # costs deducted


def test_stop_hit_and_overnight_rearm(tmp_path, monkeypatch):
    st, e, b, tid = setup(tmp_path, monkeypatch)
    b.orders["1"].update(status=FILLED, fill=400.0)
    e.cycle()
    sid = st.trades("id=?", (tid,))[0]["stop_order"]
    b.orders[sid]["status"] = "CANCELLED"  # DAY order expired overnight
    e.cycle()
    new_sid = st.trades("id=?", (tid,))[0]["stop_order"]
    assert new_sid != sid and b.orders[new_sid]["side"] == "SL"
    b.orders[new_sid].update(status=FILLED, fill=389.5)
    e.cycle()
    t = st.trades("id=?", (tid,))[0]
    assert t["status"] == "CLOSED" and t["exit_reason"] == "stop-loss hit" and t["pnl"] < 0


def test_stop_placement_failure_exits(tmp_path, monkeypatch):
    st, e, b, tid = setup(tmp_path, monkeypatch)
    b.orders["1"].update(status=FILLED, fill=400.0)
    b.sell_stop = lambda *a: (_ for _ in ()).throw(RuntimeError("rejected"))
    e.cycle()
    t = st.trades("id=?", (tid,))[0]
    assert t["status"] == "EXITING" and t["exit_reason"] == "stop-loss could not be placed"
