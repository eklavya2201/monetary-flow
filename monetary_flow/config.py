"""Risk settings. The user may make any limit stricter, never looser than HARD."""
from dataclasses import dataclass, asdict, field

# Absolute ceilings from the operating rules. Settings are clamped to these.
HARD = {
    "risk_per_trade_pct": 1.0,   # max loss per trade, % of sleeve equity
    "max_deployed_pct": 70.0,    # 30% always stays cash
    "max_positions": 3,
    "daily_loss_pct": 2.0,
    "weekly_loss_pct": 5.0,
}
MIN_RR = 2.0  # reward:risk floor, after costs

# Liquid large caps and ETFs that a small sleeve can actually buy whole shares of.
DEFAULT_UNIVERSE = [
    "NIFTYBEES", "JUNIORBEES", "BANKBEES", "ITBEES", "GOLDBEES",
    "ITC", "SBIN", "ONGC", "NTPC", "POWERGRID", "COALINDIA", "TATASTEEL",
    "HINDALCO", "BPCL", "WIPRO", "BHARTIARTL", "ICICIBANK", "HDFCBANK",
    "AXISBANK", "RELIANCE", "INFY", "SUNPHARMA", "BEL",
]


@dataclass
class Settings:
    capital: float = 4000.0            # the sleeve this agent manages, not the whole account
    risk_per_trade_pct: float = 1.0
    max_deployed_pct: float = 70.0
    max_positions: int = 3
    daily_loss_pct: float = 2.0
    weekly_loss_pct: float = 5.0
    min_rr: float = MIN_RR
    product: str = "CNC"               # CNC = delivery swing, MIS = intraday (cash only)
    mode: str = "paper"                # paper | live
    universe: list = field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    llm_provider: str = "none"         # none | anthropic | openai_compatible
    llm_model: str = "claude-opus-5-5"
    llm_base_url: str = ""             # only for openai_compatible
    cycle_minutes: int = 5

    def clamped(self) -> "Settings":
        s = Settings(**asdict(self))
        for k, cap in HARD.items():
            setattr(s, k, max(0, min(getattr(s, k), cap)))
        s.max_positions = int(s.max_positions)
        s.min_rr = max(MIN_RR, float(s.min_rr))
        s.capital = max(0.0, float(s.capital))
        s.product = "MIS" if s.product == "MIS" else "CNC"
        s.mode = "live" if s.mode == "live" else "paper"
        s.cycle_minutes = max(1, int(s.cycle_minutes))
        s.universe = [u.strip().upper() for u in s.universe if u.strip()]
        return s
