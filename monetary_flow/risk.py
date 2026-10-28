"""Hard risk gate. Every order the engine places passes check() first; nothing bypasses it."""
import math
from dataclasses import dataclass, field

from .config import Settings
from .costs import charges


@dataclass
class Book:
    """The sleeve's own accounting (the Groww account may hold other things)."""
    equity: float              # capital + realized P/L + unrealized P/L
    cash: float                # sleeve cash not tied up in positions or pending buys
    invested: float            # cost of open positions + pending buys
    open_symbols: set = field(default_factory=set)
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    last_loss_risk: float | None = None  # risk budget of the last trade, if it lost

    @property
    def day_pnl(self) -> float:
        return self.equity - self.day_start_equity

    @property
    def week_pnl(self) -> float:
        return self.equity - self.week_start_equity


@dataclass
class Plan:
    symbol: str
    entry: float
    stop: float
    target: float
    qty: int = 0
    risk_amount: float = 0.0   # rupees lost if the stop fills, costs included
    reward_amount: float = 0.0  # rupees made at target, costs included
    cost_at_stop: float = 0.0

    @property
    def rr(self) -> float:
        return self.reward_amount / self.risk_amount if self.risk_amount > 0 else 0.0


def locks(book: Book, s: Settings) -> list[str]:
    out = []
    if book.day_pnl <= -s.daily_loss_pct / 100 * book.day_start_equity:
        out.append(f"daily loss limit hit ({book.day_pnl:.2f})")
    if book.week_pnl <= -s.weekly_loss_pct / 100 * book.week_start_equity:
        out.append(f"weekly loss limit hit ({book.week_pnl:.2f})")
    return out


def risk_budget(book: Book, s: Settings) -> float:
    budget = book.equity * s.risk_per_trade_pct / 100
    if book.last_loss_risk is not None:  # never increase risk after a loss
        budget = min(budget, book.last_loss_risk)
    return budget


def size(plan: Plan, book: Book, s: Settings) -> Plan:
    """Quantity from stop distance and the rupee risk budget. Never from conviction."""
    per_share = plan.entry - plan.stop
    if per_share <= 0:
        plan.qty = 0
        return plan
    budget = risk_budget(book, s)
    room = min(book.equity * s.max_deployed_pct / 100 - book.invested,
               book.cash - book.equity * (1 - s.max_deployed_pct / 100))
    qty = min(math.floor(budget / per_share), math.floor(max(room, 0) / plan.entry))
    while qty > 0:
        cost = charges(qty * plan.entry, qty * plan.stop, s.product)
        if qty * per_share + cost <= budget:
            break
        qty -= 1
    plan.qty = max(qty, 0)
    if plan.qty:
        q = plan.qty
        plan.cost_at_stop = charges(q * plan.entry, q * plan.stop, s.product)
        plan.risk_amount = round(q * per_share + plan.cost_at_stop, 2)
        plan.reward_amount = round(q * (plan.target - plan.entry) - charges(q * plan.entry, q * plan.target, s.product), 2)
    return plan


def check(plan: Plan, book: Book, s: Settings) -> list[str]:
    """Every reason this trade must NOT be placed. Empty list = allowed."""
    v = locks(book, s)
    if not (0 < plan.stop < plan.entry < plan.target):
        v.append("needs stop < entry < target (long only, stop set before entry)")
    if plan.symbol in book.open_symbols:
        v.append("already holding this symbol (no averaging)")
    if len(book.open_symbols) >= s.max_positions:
        v.append(f"max {s.max_positions} open positions")
    if plan.qty < 1:
        v.append("size rounds to 0 shares inside the risk budget")
        return v
    value = plan.qty * plan.entry
    if book.invested + value > book.equity * s.max_deployed_pct / 100 + 1e-6:
        v.append(f"would deploy more than {s.max_deployed_pct:.0f}%")
    if book.cash - value < book.equity * (1 - s.max_deployed_pct / 100) - 1e-6:
        v.append("would break the cash reserve")
    if plan.risk_amount > risk_budget(book, s) + 1e-6:
        v.append(f"risk {plan.risk_amount:.2f} over budget {risk_budget(book, s):.2f}")
    if plan.rr < s.min_rr:
        v.append(f"reward/risk {plan.rr:.2f} after costs is under {s.min_rr}")
    return v
