"""The trading loop. One cycle = check account + market, manage open trades, maybe enter one."""
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import intraday, llm, risk, strategy
from .brokers import FILLED, CANCELLED, REJECTED, INDEX
from .brokers.paper import PaperBroker
from .brokers.yahoo import YahooData
from .costs import charges
from .store import Store

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST)


def session_open(t: datetime) -> bool:
    if t.weekday() >= 5:
        return False
    return (9, 15) <= (t.hour, t.minute) < (15, 30)


class Engine:
    def __init__(self, store: Store, data_dir: Path):
        self.store, self.data_dir = store, data_dir
        self.lock = threading.Lock()
        self._broker, self._broker_key = None, None
        self._candles: dict = {}

    # ---------------------------------------------------------------- wiring
    def broker(self):
        s, st = self.store.settings(), self.store
        key = (s.mode, s.product, s.capital, st.secret("groww_api_key"), st.secret("groww_totp_secret"))
        if key != self._broker_key:
            groww = None
            if key[3] and key[4]:
                from .brokers.groww import GrowwBroker
                groww = GrowwBroker(key[3], key[4], s.product)
            if s.mode == "live":
                if not groww:
                    raise RuntimeError("live mode needs the Groww API key and TOTP secret")
                self._broker = groww
            else:
                self._broker = PaperBroker(groww or YahooData(), self.data_dir / "paper_orders.json", s.capital)
            self._broker_key = key
        return self._broker

    def max_quote_age(self) -> int:
        b = self.broker()
        delayed = isinstance(b, PaperBroker) and isinstance(b.data, YahooData)
        return 20 * 60 if delayed else 3 * 60  # Yahoo lags ~15 min; live data must be fresh

    def candles(self, sym):
        hit = self._candles.get(sym)
        if hit and time.time() - hit[0] < 1800:
            return hit[1]
        df = self.broker().candles(sym, 400)
        self._candles[sym] = (time.time(), df)
        return df

    # ---------------------------------------------------------------- book
    def _rollover(self, mode, equity, t):
        marks = self.store.get(f"marks:{mode}", {})
        day, week = t.date().isoformat(), f"{t.isocalendar()[0]}-W{t.isocalendar()[1]}"
        if marks.get("day") != day:
            marks.update(day=day, day_equity=equity)
        if marks.get("week") != week:
            marks.update(week=week, week_equity=equity)
        self.store.put(f"marks:{mode}", marks)
        return marks

    def book(self, prices: dict | None = None) -> risk.Book:
        s, prices = self.store.settings(), prices or {}
        closed = self.store.trades("mode=? and status='CLOSED'", (s.mode,))
        live = self.store.trades("mode=? and status in ('PENDING','OPEN','EXITING')", (s.mode,))
        realized = sum(t["pnl"] or 0 for t in closed)
        invested = sum(t["qty"] * (t["entry_price"] or t["plan_entry"]) for t in live)
        unreal = sum(t["qty"] * (prices.get(t["symbol"], t["entry_price"]) - t["entry_price"])
                     for t in live if t["entry_price"])
        equity = s.capital + realized + unreal
        marks = self._rollover(s.mode, equity, now_ist())
        last_loss = closed[-1]["risk_amount"] if closed and (closed[-1]["pnl"] or 0) < 0 else None
        return risk.Book(equity=equity, cash=s.capital + realized - invested, invested=invested,
                         open_symbols={t["symbol"] for t in live}, day_start_equity=marks["day_equity"],
                         week_start_equity=marks["week_equity"], last_loss_risk=last_loss)

    # ---------------------------------------------------------------- cycle
    def cycle(self, force=False) -> str:
        with self.lock:
            try:
                return self._cycle(force)
            except Exception as e:  # never let one bad response crash the loop
                self.store.log(f"cycle aborted, nothing new placed: {type(e).__name__}: {e}", "error")
                return "error"

    def _cycle(self, force) -> str:
        s, t = self.store.settings(), now_ist()
        if not force and not self.store.get("running", False):
            return "stopped"
        if not session_open(t):
            return "market closed"
        b = self.broker()
        try:
            funds = b.funds()
            nifty = b.quote(INDEX)
        except Exception as e:
            self.store.log(f"account/market data unavailable, not trading: {e}", "warn")
            return "no data"
        if not nifty.get("price") or time.time() - nifty["ts"] > self.max_quote_age():
            self.store.log("NIFTY quote is stale (holiday or feed issue), not trading", "warn")
            return "stale"

        prices = self.manage(b, s, t, nifty)
        book = self.book(prices)
        self.store.snapshot(book.equity)
        blocked = risk.locks(book, s)
        if blocked:
            self.store.put("halted", blocked)
            return "locked: " + "; ".join(blocked)
        self.store.put("halted", [])
        last_entry = (13, 30) if s.product == "MIS" else (15, 0)
        if (t.hour, t.minute) < (9, 30) or (t.hour, t.minute) >= last_entry:
            return "outside entry window"
        if len(book.open_symbols) >= s.max_positions:
            return "position slots full"
        return self.try_entry(b, s, book, funds)

    def manage(self, b, s, t, nifty) -> dict:
        prices = {}
        index_day = None
        for tr in self.store.trades("mode=? and status in ('PENDING','OPEN','EXITING')", (s.mode,)):
            sym, tid = tr["symbol"], tr["id"]
            q = b.quote(sym)
            px = q.get("price")
            if px:
                prices[sym] = px
            if tr["status"] == "PENDING":
                o = b.order(tr["entry_order"])
                if o["status"] == FILLED or (o["status"] in (CANCELLED, REJECTED) and o["filled_qty"]):
                    self._protect(b, tr, o)
                elif o["status"] in (CANCELLED, REJECTED):
                    self.store.update_trade(tid, status="CANCELLED", closed_at=time.time(), exit_reason=o["status"].lower())
                elif time.time() - tr["opened_at"] > 30 * 60 or (px and px >= tr["target"]) or (px and px <= tr["stop"]):
                    b.cancel(tr["entry_order"])  # stale limit; next cycle settles any partial fill
                    self.store.log(f"{sym}: entry limit not filled, cancelled")
            elif tr["status"] == "OPEN":
                so = b.order(tr["stop_order"])
                if so["status"] == FILLED:
                    self._close(tr, so["avg_price"] or tr["stop"], "stop-loss hit", s)
                    continue
                if so["status"] in (CANCELLED, REJECTED):  # DAY stop expired overnight: re-arm it
                    if px and px <= tr["stop"]:
                        self._exit(b, tr, "stop breached while unprotected")
                    else:
                        self.store.update_trade(tid, stop_order=b.sell_stop(sym, tr["qty"], tr["stop"]))
                        self.store.log(f"{sym}: stop-loss re-armed at {tr['stop']}")
                    continue
                reason = None
                if px and px >= tr["target"]:
                    reason = "target reached"
                elif s.product == "MIS" and (t.hour, t.minute) >= (15, 10):
                    reason = "intraday square-off"
                elif nifty.get("price") and self._index_change(b, nifty) <= -2.0:
                    reason = "market falling hard (NIFTY -2% today)"
                elif s.product == "MIS":
                    broken = intraday.thesis_broken(b.bars(sym), tr["entry_price"])
                    reason = f"thesis invalid: {broken}" if broken else None
                elif (t.hour, t.minute) >= (15, 15) and px:
                    df = self.candles(sym)
                    broken = strategy.thesis_broken(df) if len(df) > 60 else None
                    reason = f"thesis invalid: {broken}" if broken else None
                if reason:
                    self._exit(b, tr, reason)
            elif tr["status"] == "EXITING":
                o = b.order(tr["exit_order"])
                if o["status"] == FILLED:
                    self._close(tr, o["avg_price"] or px, tr["exit_reason"], s)
                elif o["status"] in (CANCELLED, REJECTED):
                    self.store.log(f"{sym}: exit order {o['status']}, retrying", "warn")
                    self.store.update_trade(tid, exit_order=b.sell_market(sym, tr["qty"]))
        return prices

    def _index_change(self, b, nifty) -> float:
        df = self.candles(INDEX)
        prev = df.close.iloc[-2] if len(df) > 1 else None
        return (nifty["price"] / prev - 1) * 100 if prev else 0.0

    def _protect(self, b, tr, o):
        qty = o["filled_qty"] or tr["qty"]
        price = o["avg_price"] or tr["plan_entry"]
        self.store.update_trade(tr["id"], status="OPEN", qty=qty, entry_price=price)
        try:
            sid = b.sell_stop(tr["symbol"], qty, tr["stop"])
            self.store.update_trade(tr["id"], stop_order=sid)
            self.store.log(f"BOUGHT {qty} {tr['symbol']} @ {price}; stop-loss order {sid} at {tr['stop']}")
        except Exception as e:
            self.store.log(f"{tr['symbol']}: could not place stop-loss ({e}); exiting now", "error")
            self._exit(b, {**tr, "qty": qty, "stop_order": None}, "stop-loss could not be placed")

    def _exit(self, b, tr, reason):
        if tr.get("stop_order"):
            try:
                b.cancel(tr["stop_order"])
            except Exception:
                pass
        oid = b.sell_market(tr["symbol"], tr["qty"])
        self.store.update_trade(tr["id"], status="EXITING", exit_order=oid, exit_reason=reason)
        self.store.log(f"SELL {tr['qty']} {tr['symbol']} at market: {reason}")

    def _close(self, tr, exit_price, reason, s):
        fees = charges(tr["qty"] * tr["entry_price"], tr["qty"] * exit_price, s.product)
        pnl = round(tr["qty"] * (exit_price - tr["entry_price"]) - fees, 2)
        self.store.update_trade(tr["id"], status="CLOSED", exit_price=exit_price, exit_reason=reason,
                                fees=fees, pnl=pnl, closed_at=time.time())
        self.store.log(f"CLOSED {tr['symbol']} @ {exit_price}: {reason}, P/L {pnl:+.2f} after Rs {fees} costs")

    def completed_days(self, sym):
        df = self.candles(sym)
        return df[[d.date() < now_ist().date() for d in df.index]] if len(df) else df

    def try_entry(self, b, s, book, funds) -> str:
        if s.product == "MIS":
            index_days = self.completed_days(INDEX)
            index_bars = b.bars(INDEX)
            if len(index_days) < 2 or index_bars.empty:
                return "no entries: index data missing"
            prev_close = index_days.close.iloc[-1]
            ok, why = intraday.market_ok(index_bars, prev_close)
            index_change = (index_bars.close.iloc[-1] / prev_close - 1) * 100
            setup = lambda sym, price: intraday.evaluate(sym, b.bars(sym), self.completed_days(sym), index_change, price)
        else:
            index_df = self.candles(INDEX)
            ok, why = strategy.market_ok(index_df)
            setup = lambda sym, price: strategy.evaluate(sym, self.candles(sym), index_df, price)
        if not ok:
            return f"no entries: {why}"
        candidates = []
        for sym in s.universe:
            if sym in book.open_symbols:
                continue
            try:
                q = b.quote(sym)
                if not q.get("price") or time.time() - q["ts"] > self.max_quote_age():
                    continue  # stale or missing: skip, never guess
                plan, reason = setup(sym, q["price"])
            except Exception as e:
                self.store.log(f"{sym}: data error, skipped ({type(e).__name__})", "warn")
                continue
            if plan:
                risk.size(plan, book, s)
                problems = risk.check(plan, book, s)
                if problems:
                    self.store.log(f"{sym}: setup found but blocked: {'; '.join(problems)}")
                else:
                    candidates.append((plan.rr, plan, reason))
        if not candidates:
            return "no valid setup"
        _, plan, reason = max(candidates, key=lambda c: c[0])
        need = plan.qty * plan.entry + charges(plan.qty * plan.entry, 0, s.product)
        if funds < need:
            self.store.log(f"{plan.symbol}: broker shows Rs {funds:.2f} free, need Rs {need:.2f}; not trading", "warn")
            return "insufficient broker funds"
        st = self.store
        approve, verdict = llm.review(s.llm_provider, s.llm_model, st.secret("llm_api_key"), s.llm_base_url, plan, reason)
        if not approve:
            st.log(f"{plan.symbol}: LLM veto: {verdict}")
            return "vetoed"
        oid = b.buy_limit(plan.symbol, plan.qty, plan.entry)
        st.add_trade(mode=s.mode, symbol=plan.symbol, status="PENDING", qty=plan.qty, plan_entry=plan.entry,
                     stop=plan.stop, target=plan.target, why=f"{reason} | LLM: {verdict}",
                     risk_amount=plan.risk_amount, entry_order=oid, opened_at=time.time())
        st.log(f"BUY LIMIT {plan.qty} {plan.symbol} @ {plan.entry} (stop {plan.stop}, target {plan.target}, "
               f"risk Rs {plan.risk_amount}, R:R {plan.rr:.2f}) order {oid}")
        return f"entry placed: {plan.symbol}"

    # ---------------------------------------------------------------- controls
    def kill(self) -> str:
        """Cancel pending entries, exit every position at market, stop the engine."""
        with self.lock:
            self.store.put("running", False)
            b, s = self.broker(), self.store.settings()
            for tr in self.store.trades("mode=? and status in ('PENDING','OPEN')", (s.mode,)):
                try:
                    if tr["status"] == "PENDING":
                        b.cancel(tr["entry_order"])
                    else:
                        self._exit(b, tr, "kill switch")
                except Exception as e:
                    self.store.log(f"kill: {tr['symbol']} failed: {e} - CHECK GROWW APP", "error")
            self.store.log("KILL SWITCH: engine stopped", "warn")
            return "killed"

    def report(self, prices: dict | None = None) -> dict:
        s = self.store.settings()
        book = self.book(prices)
        start_of_day = datetime.combine(now_ist().date(), datetime.min.time(), IST).timestamp()
        today = [t for t in self.store.trades("mode=?", (s.mode,)) if (t["opened_at"] or 0) >= start_of_day
                 or (t["closed_at"] or 0) >= start_of_day]
        curve = [e for _, e in self.store.equity_curve()] or [book.equity]
        peak, mdd = curve[0], 0.0
        for e in curve:
            peak = max(peak, e)
            mdd = max(mdd, (peak - e) / peak * 100 if peak else 0)
        realized_today = sum(t["pnl"] or 0 for t in today if t["status"] == "CLOSED" and (t["closed_at"] or 0) >= start_of_day)
        blocked = risk.locks(book, s)
        return {
            "mode": s.mode, "starting_equity": round(book.day_start_equity, 2), "ending_equity": round(book.equity, 2),
            "capital": s.capital, "cash": round(book.cash, 2), "invested": round(book.invested, 2),
            "realized_today": round(realized_today, 2),
            "realized_total": round(sum(t["pnl"] or 0 for t in self.store.trades("mode=? and status='CLOSED'", (s.mode,))), 2),
            "unrealized": round(book.equity - s.capital - sum(t["pnl"] or 0 for t in self.store.trades("mode=? and status='CLOSED'", (s.mode,))), 2),
            "day_pnl": round(book.day_pnl, 2), "fees_today": round(sum(t["fees"] or 0 for t in today), 2),
            "max_drawdown_pct": round(mdd, 2),
            "open_positions": [{**t, "price": (prices or {}).get(t["symbol"])} for t in
                               self.store.trades("mode=? and status in ('PENDING','OPEN','EXITING')", (s.mode,))],
            "trades_today": today, "locks": blocked,
            "continue_tomorrow": not any("weekly" in x for x in blocked),
        }
