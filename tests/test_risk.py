from monetary_flow.config import Settings
from monetary_flow.costs import charges
from monetary_flow.risk import Book, Plan, check, size


def book(**kw):
    b = dict(equity=4000, cash=4000, invested=0, day_start_equity=4000, week_start_equity=4000)
    b.update(kw)
    return Book(**b)


def test_settings_cannot_loosen_hard_limits():
    s = Settings(risk_per_trade_pct=5, max_deployed_pct=100, max_positions=10, daily_loss_pct=9,
                 weekly_loss_pct=20, min_rr=1).clamped()
    assert (s.risk_per_trade_pct, s.max_deployed_pct, s.max_positions) == (1.0, 70.0, 3)
    assert (s.daily_loss_pct, s.weekly_loss_pct, s.min_rr) == (2.0, 5.0, 2.0)


def test_groww_delivery_costs_small_order():
    # Rs 1000 buy + sell: Rs 5 min brokerage x2, Rs 20 DP, 0.1% STT each side, GST
    c = charges(1000, 1000, "CNC")
    assert 37 < c < 40
    assert charges(1000, 1000, "MIS") < 13


def test_small_delivery_sleeve_is_eaten_by_fixed_costs():
    # Rs 40 risk budget; one CNC round trip already costs ~Rs 35 (Rs 5 min brokerage x2 + Rs 20 DP + GST)
    s = Settings(product="CNC").clamped()
    p = size(Plan("ITC", entry=400, stop=388, target=430), book(), s)
    assert p.qty == 0 and "rounds to 0" in " ".join(check(p, book(), s))


def test_size_respects_rupee_risk_including_costs():
    s = Settings(capital=25000).clamped()
    b = book(equity=25000, cash=25000, day_start_equity=25000, week_start_equity=25000)
    p = size(Plan("ITC", entry=400, stop=388, target=440), b, s)
    assert p.qty >= 1 and p.risk_amount <= 250.0  # 1% of 25000, costs included


def test_intraday_small_sleeve_can_size():
    s = Settings(product="MIS").clamped()
    p = size(Plan("ITC", entry=400, stop=396, target=416), book(), s)
    assert p.qty >= 1 and p.risk_amount <= 40.0


def test_deploy_cap_and_cash_reserve():
    s = Settings().clamped()
    b = book(invested=2700, cash=1300)
    p = size(Plan("SBIN", entry=500, stop=495, target=530), b, s)
    assert p.qty * 500 <= 100 + 1e-6  # only Rs 100 of room left under 70%


def test_rules_block_bad_trades():
    s = Settings().clamped()
    b = book(open_symbols={"A", "B", "C"})
    p = size(Plan("A", entry=100, stop=101, target=110), b, s)
    v = " ".join(check(p, b, s))
    assert "stop < entry" in v and "no averaging" in v and "max 3" in v


def test_daily_loss_lock_and_no_risk_increase_after_loss():
    s = Settings().clamped()
    locked = book(equity=3910)
    assert any("daily" in x for x in check(Plan("X", 100, 95, 120, qty=1, risk_amount=5, reward_amount=15), locked, s))
    after_loss = book(last_loss_risk=25.0)
    p = size(Plan("ITC", entry=400, stop=390, target=440), after_loss, s)
    assert p.risk_amount <= 25.0


def test_angel_tick_rounding():
    from monetary_flow.brokers.angel import AngelBroker
    assert AngelBroker._tick(101.03, 0.05) == 101.0
    assert AngelBroker._tick(101.01, 0.05, down=False) == 101.05
