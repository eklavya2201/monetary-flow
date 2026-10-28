"""Replay the swing strategy + risk gate over real daily history (free Yahoo data), costs included.

  python -m monetary_flow.backtest --capital 10000 --years 3

Assumptions (stated, not hidden): a signal on day t buys at that day's close; stops and targets are
checked against later days' high/low, gaps fill at the open, and if one day touches both the stop
wins (conservative). No slippage beyond that, no taxes on profit.
"""
import argparse
from collections import defaultdict

import pandas as pd

from . import risk, strategy
from .brokers import INDEX
from .brokers.yahoo import YahooData
from .config import DEFAULT_UNIVERSE, Settings
from .costs import charges


def load(symbols, years):
    y, days = YahooData(), int(years * 365) + 420  # + warm-up for the 200-day average
    data = {s: y.candles(s, days) for s in [INDEX, *symbols]}
    return {s: d for s, d in data.items() if len(d) > 260}


def run(capital=10000.0, years=3, data=None, symbols=DEFAULT_UNIVERSE):
    data = data or load(symbols, years)
    s = Settings(capital=capital, product="CNC").clamped()
    idx = data[INDEX]
    dates = idx.index[-int(years * 248):]
    cash, open_, trades, curve = capital, {}, [], []
    marks = {}
    for day in dates:
        # 1) manage open positions on today's bar
        for sym, p in list(open_.items()):
            df = data[sym]
            if day not in df.index:
                continue
            bar = df.loc[day]
            exit_px, why = None, None
            if bar.low <= p["stop"]:
                exit_px, why = min(bar.open, p["stop"]), "stop"
            elif bar.high >= p["target"]:
                exit_px, why = max(bar.open, p["target"]), "target"
            else:
                hist = df.loc[:day].iloc[-260:]
                if strategy.thesis_broken(hist):
                    exit_px, why = bar.close, "thesis"
            if exit_px:
                fee = charges(p["qty"] * p["entry"], p["qty"] * exit_px, "CNC")
                pnl = p["qty"] * (exit_px - p["entry"]) - fee
                cash += p["qty"] * exit_px - (fee - charges(p["qty"] * p["entry"], 0, "CNC"))
                trades.append({**p, "exit": exit_px, "exit_day": day, "why": why, "pnl": pnl, "fees": fee})
                del open_[sym]
        # 2) mark to market
        mtm = sum(p["qty"] * data[sym].close.get(day, p["entry"]) for sym, p in open_.items())
        equity = cash + mtm
        curve.append((day, equity))
        wk = day.isocalendar()[:2]
        marks.setdefault(("d", day), equity)
        marks.setdefault(("w", wk), equity)
        last_loss = trades[-1]["risk_amount"] if trades and trades[-1]["pnl"] < 0 else None
        book = risk.Book(equity=equity, cash=cash, invested=sum(p["qty"] * p["entry"] for p in open_.values()),
                         open_symbols=set(open_), day_start_equity=marks[("d", day)],
                         week_start_equity=marks[("w", wk)], last_loss_risk=last_loss)
        if risk.locks(book, s) or len(open_) >= s.max_positions:
            continue
        # 3) scan for entries at today's close
        hist_idx = idx.loc[:day].iloc[-260:]
        if not strategy.market_ok(hist_idx)[0]:
            continue
        cands = []
        for sym, df in data.items():
            if sym == INDEX or sym in open_ or day not in df.index:
                continue
            hist = df.loc[:day].iloc[-260:]
            plan, why = strategy.evaluate(sym, hist, hist_idx, float(hist.close.iloc[-1]))
            if plan:
                risk.size(plan, book, s)
                if not risk.check(plan, book, s):
                    cands.append(plan)
        if cands:
            p = max(cands, key=lambda c: c.rr)
            cash -= p.qty * p.entry + charges(p.qty * p.entry, 0, "CNC")
            open_[p.symbol] = {"symbol": p.symbol, "qty": p.qty, "entry": p.entry, "stop": p.stop,
                               "target": p.target, "risk_amount": p.risk_amount, "day": day}
    return summarize(capital, trades, curve, open_, data, dates)


def summarize(capital, trades, curve, open_, data, dates):
    eq = pd.Series(dict(curve))
    peak = eq.cummax()
    wins = [t for t in trades if t["pnl"] > 0]
    monthly = eq.resample("ME").last().pct_change().dropna() * 100
    bench = data.get("NIFTYBEES")
    bh = (bench.close.loc[dates[-1]] / bench.close.loc[dates[0]] - 1) * 100 if bench is not None else None
    by_why = defaultdict(int)
    for t in trades:
        by_why[t["why"]] += 1
    return {
        "capital": capital, "final_equity": round(eq.iloc[-1], 2),
        "total_return_pct": round((eq.iloc[-1] / capital - 1) * 100, 2),
        "trades": len(trades), "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "avg_trade": round(sum(t["pnl"] for t in trades) / len(trades), 2) if trades else 0,
        "total_fees": round(sum(t["fees"] for t in trades), 2),
        "max_drawdown_pct": round(((peak - eq) / peak).max() * 100, 2),
        "best_month_pct": round(monthly.max(), 2) if len(monthly) else 0,
        "worst_month_pct": round(monthly.min(), 2) if len(monthly) else 0,
        "positive_months": f"{(monthly > 0).sum()}/{len(monthly)}",
        "exits": dict(by_why), "still_open": len(open_),
        "niftybees_buy_hold_pct": round(bh, 2) if bh is not None else None,
        "period": f"{dates[0].date()} to {dates[-1].date()}",
        "trade_list": trades,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, nargs="+", default=[10000])
    ap.add_argument("--years", type=float, default=3)
    a = ap.parse_args()
    data = load(DEFAULT_UNIVERSE, a.years)
    for cap in a.capital:
        r = run(cap, a.years, data)
        tl = r.pop("trade_list")
        print(f"\n=== capital Rs {cap:,.0f} ===")
        for k, v in r.items():
            print(f"  {k}: {v}")
        for t in tl[-8:]:
            print(f"    {t['day'].date()} {t['symbol']:10} {t['qty']:>3} @ {t['entry']:.2f} -> {t['exit']:.2f} "
                  f"({t['why']}) P/L {t['pnl']:+.2f}")
