"""Intraday (MIS, cash only) setup: opening-range breakout with VWAP and volume confirmation.

Opening range = first 15 minutes (09:15-09:30). Long only. Every rule is a plain number.
"""
import pandas as pd

from .risk import Plan

OR_BARS = 3          # three 5-minute bars = 15 minutes
MAX_STOP_PCT = 0.012  # skip if the stop would be more than 1.2% away
MAX_TARGET_R = 4.0    # furthest intraday target, in multiples of the stop distance


def vwap(bars: pd.DataFrame) -> pd.Series:
    typical = (bars.high + bars.low + bars.close) / 3
    return (typical * bars.volume).cumsum() / bars.volume.cumsum()


def market_ok(index_bars: pd.DataFrame, index_prev_close: float) -> tuple[bool, str]:
    if len(index_bars) < OR_BARS:
        return False, "NIFTY opening range not complete"
    last, day_open = index_bars.close.iloc[-1], index_bars.open.iloc[0]
    if last > day_open and last > index_prev_close:
        return True, f"NIFTY {last:.0f} above open {day_open:.0f} and prev close {index_prev_close:.0f}"
    return False, f"NIFTY {last:.0f} not above both open {day_open:.0f} and prev close {index_prev_close:.0f}"


def evaluate(sym: str, bars: pd.DataFrame, daily: pd.DataFrame, index_change_pct: float,
             price: float) -> tuple[Plan | None, str]:
    """bars = today's 5-minute candles (oldest first); daily = completed daily candles."""
    if len(bars) < OR_BARS + 3 or len(daily) < 21:
        return None, "not enough data yet"
    prev = daily.iloc[-1]
    if (daily.close * daily.volume).iloc[-20:].mean() < 20e7:
        return None, "illiquid: 20-day avg traded value < Rs 20 cr"

    rng = bars.iloc[:OR_BARS]
    or_high, or_low = rng.high.max(), rng.low.min()
    width = (or_high - or_low) / price
    if not (0.004 <= width <= 0.015):
        return None, f"opening range {width:.2%} outside 0.4%-1.5%"
    change = (price / prev.close - 1) * 100
    if change <= index_change_pct:
        return None, f"weaker than NIFTY today ({change:.2f}% vs {index_change_pct:.2f}%)"
    if change > 3.0:
        return None, f"up {change:.1f}% already; not chasing"

    v = vwap(bars).iloc[-1]
    last = bars.iloc[-1]
    if price <= or_high:
        return None, f"no breakout: {price:.2f} <= range high {or_high:.2f}"
    if price < v:
        return None, f"below VWAP {v:.2f}"
    if price > or_high + 0.25 * (or_high - or_low):
        return None, "already extended past the breakout; not chasing"
    avg_vol = bars.volume.iloc[:-1].mean()
    if last.volume < 1.5 * avg_vol:
        return None, f"breakout volume {last.volume:.0f} < 1.5x average {avg_vol:.0f}"
    avg_range = (bars.high - bars.low).iloc[:-1].mean()
    if last.high - last.low > 2.5 * avg_range:
        return None, "breakout candle is a spike; not chasing"

    entry = round(price, 2)
    stop = round(max(or_low, v * 0.998) - 0.05, 2)
    if (entry - stop) / entry > MAX_STOP_PCT:
        return None, f"stop {stop} is {(entry - stop) / entry:.1%} away (> {MAX_STOP_PCT:.1%})"
    target = entry + MAX_TARGET_R * (entry - stop)
    if prev.high > entry:
        target = min(target, prev.high)  # yesterday's high is resistance
    target = round(target, 2)
    why = (f"broke 15-min range high {or_high:.2f} on {last.volume / avg_vol:.1f}x volume, above VWAP {v:.2f}, "
           f"{change:+.2f}% vs NIFTY {index_change_pct:+.2f}%")
    return Plan(symbol=sym, entry=entry, stop=stop, target=target), why


def thesis_broken(bars: pd.DataFrame, entry: float) -> str | None:
    if len(bars) < 2:
        return None
    done = bars.iloc[-2]  # last completed bar
    v = vwap(bars.iloc[:-1]).iloc[-1]
    if done.close < v and done.close < entry:
        return f"5-min close {done.close:.2f} back below VWAP {v:.2f} and entry"
    return None
