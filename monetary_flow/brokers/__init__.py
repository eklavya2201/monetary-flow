"""Brokers share one small interface so the engine never knows which one it drives.

  funds() -> float                      cash the broker says is free for equity buys
  quote(sym) -> {"price", "ts"}         ts = epoch seconds of the last trade
  candles(sym, days) -> DataFrame        daily open/high/low/close/volume, oldest first
  bars(sym) -> DataFrame                 today's 5-minute candles, oldest first (intraday)
  buy_limit(sym, qty, price) -> id
  sell_stop(sym, qty, trigger) -> id     stop-loss market (SL-M), the broker-side protection
  sell_market(sym, qty) -> id
  order(id) -> {"status", "filled_qty", "avg_price"}   status: OPEN | FILLED | CANCELLED | REJECTED
  cancel(id)
"""
OPEN, FILLED, CANCELLED, REJECTED = "OPEN", "FILLED", "CANCELLED", "REJECTED"
INDEX = "NIFTY"  # market trend reference
