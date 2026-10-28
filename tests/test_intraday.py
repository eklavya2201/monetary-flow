import pandas as pd

from monetary_flow import intraday
from monetary_flow.config import Settings
from monetary_flow.risk import Book, check, size


def daily(prev_close=100.0, prev_high=101.0):
    idx = pd.date_range("2026-09-01", periods=25, freq="B")
    return pd.DataFrame({"open": 99.0, "high": prev_high, "low": 98.0, "close": prev_close, "volume": 5_000_000.0}, index=idx)


def bars(breakout_close=101.0, breakout_vol=30000.0):
    rows = [(100.0, 100.8, 100.0, 100.5, 10000.0), (100.5, 100.8, 100.2, 100.6, 10000.0),
            (100.6, 100.8, 100.3, 100.7, 10000.0), (100.7, 100.8, 100.5, 100.7, 10000.0),
            (100.7, 100.8, 100.6, 100.75, 10000.0), (100.75, breakout_close + 0.05, 100.7, breakout_close, breakout_vol)]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def sleeve(cap):
    return Book(equity=cap, cash=cap, invested=0, day_start_equity=cap, week_start_equity=cap)


def test_breakout_sizes_and_targets_net_2r():
    plan, why = intraday.evaluate("ITC", bars(), daily(prev_high=100.0), 0.2, 101.0)
    assert plan and plan.stop < plan.entry < plan.target, why
    s = Settings(product="MIS", capital=25000).clamped()
    size(plan, sleeve(25000), s)
    assert plan.qty >= 1 and check(plan, sleeve(25000), s) == []
    assert plan.rr >= 2.0  # after costs


def test_small_sleeve_tight_stop_is_skipped_for_costs():
    # Rs 4k: ~Rs 12.5 of charges vs Rs 40 risk needs a ~3.4R move; this range can't pay that
    plan, _ = intraday.evaluate("ITC", bars(), daily(prev_high=100.0), 0.2, 101.0)
    s = Settings(product="MIS").clamped()
    size(plan, sleeve(4000), s)
    assert any("reward/risk" in v for v in check(plan, sleeve(4000), s))


def test_rejects_weak_volume_spike_and_no_breakout():
    assert intraday.evaluate("ITC", bars(breakout_vol=11000), daily(prev_high=100.0), 0.2, 101.0)[0] is None
    assert intraday.evaluate("ITC", bars(breakout_close=100.75), daily(prev_high=100.0), 0.2, 100.75)[0] is None
    plan, why = intraday.evaluate("ITC", bars(), daily(prev_high=100.0), 2.0, 101.0)
    assert plan is None and "weaker than NIFTY" in why


def test_market_filter():
    ib = pd.DataFrame({"open": [25000, 25010, 25020], "high": 25050, "low": 24990, "close": [25010, 25020, 25040], "volume": 0})
    assert intraday.market_ok(ib, 24950)[0]
    assert not intraday.market_ok(ib, 25100)[0]
