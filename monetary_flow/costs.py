"""Groww equity charges (groww.in/pricing, checked 2026-10-28). Re-check if Groww changes them."""

GST = 0.18


def brokerage(value: float) -> float:
    # Rs 20 or 0.1% per executed order, whichever is lower, minimum Rs 5
    return 0.0 if value <= 0 else max(5.0, min(20.0, 0.001 * value))


def charges(buy_value: float, sell_value: float, product: str = "CNC", exchange: str = "NSE") -> float:
    """All-in cost of one buy + one sell, in rupees."""
    delivery = product == "CNC"
    brk = brokerage(buy_value) + brokerage(sell_value)
    stt = 0.001 * (buy_value + sell_value) if delivery else 0.00025 * sell_value
    stamp = (0.00015 if delivery else 0.00003) * buy_value
    turnover = buy_value + sell_value
    exch = turnover * (0.0000297 if exchange == "NSE" else 0.0000375)
    sebi = turnover * 0.000001
    ipft = turnover * 0.000001 if exchange == "NSE" else 0.0
    dp = (20.0 if sell_value >= 100 else 3.5) if delivery and sell_value > 0 else 0.0
    gst = GST * (brk + exch + sebi + ipft + dp)
    return round(brk + stt + stamp + exch + sebi + ipft + dp + gst, 2)
