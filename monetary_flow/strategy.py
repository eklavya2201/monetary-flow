"""One transparent long-only swing setup: trend pullback that resumes, in a rising market.

Every rule is a plain number so a human can check why a trade was (or was not) taken.
"""
import pandas as pd

from .risk import Plan


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["sma50"] = d.close.rolling(50).mean()
    d["sma200"] = d.close.rolling(200).mean()
    d["ema20"] = d.close.ewm(span=20, adjust=False).mean()
    tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(), (d.low - d.close.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["vol20"] = d.volume.rolling(20).mean()
    d["value20"] = (d.close * d.volume).rolling(20).mean()
    return d


def market_ok(index_df: pd.DataFrame) -> tuple[bool, str]:
    d = indicators(index_df)
    if len(d) < 60:
        return False, "not enough index history"
    last, prev = d.iloc[-1], d.iloc[-6]
    if last.close > last.sma50 and last.sma50 > prev.sma50:
        return True, f"NIFTY {last.close:.0f} above rising 50-day average"
    return False, f"NIFTY {last.close:.0f} not in an uptrend (50-day avg {last.sma50:.0f})"


def evaluate(sym: str, df: pd.DataFrame, index_df: pd.DataFrame, price: float) -> tuple[Plan | None, str]:
    """Return (plan, why). plan is None when there is no valid setup."""
    if len(df) < 210:
        return None, "less than 210 days of history"
    d = indicators(df)
    last, y = d.iloc[-1], d.iloc[-2]
    atr = last.atr

    if last.value20 < 20e7:
        return None, f"illiquid: 20-day avg traded value Rs {last.value20 / 1e7:.1f} cr < 20 cr"
    if not (0.008 <= atr / price <= 0.04):
        return None, f"volatility {atr / price:.1%} of price outside 0.8%-4%"
    if not (price > last.sma50 > last.sma200):
        return None, "trend: needs price > 50-day > 200-day average"
    rs_stock = price / d.close.iloc[-64] - 1
    rs_index = index_df.close.iloc[-1] / index_df.close.iloc[-64] - 1
    if rs_stock <= rs_index:
        return None, f"relative strength {rs_stock:.1%} not above NIFTY {rs_index:.1%}"
    # never chase a spike
    if price / y.close - 1 > 0.03 or price - last.open > 1.5 * atr:
        return None, "today's move is a spike; not chasing"
    # the pullback: touched the 20-day EMA in the last 5 sessions, now back above yesterday's high
    recent = d.iloc[-6:-1]
    if not (recent.low <= recent.ema20 * 1.01).any():
        return None, "no pullback to the 20-day EMA in the last 5 sessions"
    if price <= y.high:
        return None, f"not yet resuming: price {price:.2f} <= yesterday's high {y.high:.2f}"
    if recent.volume.mean() > 1.3 * last.vol20:
        return None, "pullback came on heavy volume (distribution)"

    entry = round(price, 2)
    stop = round(min(recent.low.min(), entry - 1.0 * atr) - 0.05, 2)
    stop = max(stop, round(entry - 2.5 * atr, 2))  # cap the stop distance
    target = round(entry + 2.2 * (entry - stop), 2)
    resistance = d.high.iloc[-120:-1].max()
    if entry < resistance < target:
        return None, f"resistance at {resistance:.2f} sits before target {target:.2f}"
    why = (f"uptrend (>{last.sma50:.0f} 50d, >{last.sma200:.0f} 200d), RS {rs_stock:.1%} vs NIFTY {rs_index:.1%}, "
           f"pullback to 20-EMA then broke yesterday's high {y.high:.2f}; ATR {atr:.2f}")
    return Plan(symbol=sym, entry=entry, stop=stop, target=target), why


def thesis_broken(df: pd.DataFrame) -> str | None:
    d = indicators(df)
    last = d.iloc[-1]
    if last.close < last.sma50:
        return f"closed {last.close:.2f} below the 50-day average {last.sma50:.2f}"
    return None
