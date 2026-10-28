# Monetary Flow

A rules-first, long-only trading agent for NSE equities and ETFs on **Groww**. You bring your own keys, the
money stays in your own Groww account, and you control it all from a mobile web UI.

> Profit is not guaranteed. This is an execution tool with hard risk limits, not a prediction machine.
> Run it in paper mode first.

## What it does

Each cycle (every 5 min during market hours) it:

1. Logs in to Groww (API key + TOTP, token refreshed after the daily 06:00 IST expiry) and reads free funds.
   **If any account or market call fails, or the NIFTY quote is stale, it does nothing** (this also covers holidays).
2. Manages open trades:
   - fills the protective stop-loss order (SL-M) as soon as an entry fills, and exits at market if the stop
     can't be placed
   - re-arms the stop each morning (Groww SL-M orders are DAY orders)
   - exits on target, on a broken thesis (close below the 50-day average), when NIFTY is down 2% on the day,
     or at 15:10 for intraday trades
3. Looks for at most one new entry, but only if every rule passes:
   - Market: NIFTY above its rising 50-day average.
   - Stock: price > 50-day > 200-day average, beating NIFTY over 3 months, 20-day average traded value
     ≥ ₹20 cr, ATR between 0.8% and 4% of price.
   - Setup: pulled back to the 20-day EMA on normal volume, then traded above yesterday's high. No
     spikes (>3% day move or >1.5 ATR candle), and no resistance between entry and target.
   - Risk gate (`risk.py`): stop < entry < target, R:R ≥ 2 **after Groww's charges**, risk ≤ 1% of
     equity including costs, ≤ 70% deployed, ≥ 30% cash, ≤ 3 positions, no averaging down, 2% daily and
     5% weekly loss locks, and never more risk than the last trade if that trade lost.
   - Optional AI veto: Claude or any OpenAI-compatible model reads recent headlines and can only
     **veto**. Any error counts as a veto.
4. Places a LIMIT buy, records everything, and snapshots equity.

The hard limits are clamped server-side (`config.HARD`). The UI can tighten them but never loosen them.

## The economics: read this before funding

Groww charges ₹5 minimum brokerage per order, a ₹20 DP charge per delivery sell, and GST on top. With a
1% risk rule:

| Sleeve | Risk budget/trade | Delivery round-trip cost | Intraday round-trip cost |
|---|---|---|---|
| ₹4,000 | ₹40 | ~₹38 | ~₹12 |
| ₹10,000 | ₹100 | ~₹43 | ~₹13 |
| ₹25,000 | ₹250 | ~₹64 | ~₹24 |

- **Under about ₹15k, delivery (CNC) trades are almost always blocked.** Costs eat the whole risk budget.
  The engine will correctly refuse them.
- Intraday (MIS, cash only) can size small trades, but needs a ~3% same-day move to clear 2R.
- The Groww Trade API subscription costs about ₹499 + GST/month (check the current price). On a ₹4–5k
  sleeve that is roughly 12–15% of capital every month, before a single trade.

### What a trade must achieve at each size (delivery, 1% risk, 2:1 after costs)

The risk engine computes the nearest target that still pays 2:1 **after** Groww's charges. It skips the trade
if that target lies beyond what the setup can reach (4x the stop distance, capped at resistance).

| Sleeve | Price move needed, as a multiple of the stop distance |
|---|---|
| ₹10,000 | ~4x (a 3% stop needs ~+12%) |
| ₹25,000 | ~2.8x |
| ₹50,000 | ~2.5x |

### Intraday (parked)

`intraday.py` (15-minute opening-range breakout + VWAP + volume) is built and tested but **off by default**.
Select "Intraday" in Setup to enable it. At ₹4k, its ~₹12.5 of charges against ₹40 of risk demand a ~3.4R
move, so most setups are skipped.

## Run locally (paper mode, free)

Paper mode uses free, delayed Yahoo data until Groww keys are saved.

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows; use .venv/bin on Linux/macOS
cp .env.example .env    # set MF_MASTER_KEY (32+ chars) and MF_PASSCODE
set -a; . ./.env; set +a
uvicorn monetary_flow.app:app --port 8080
# open http://localhost:8080 on your phone (same Wi-Fi: http://<laptop-ip>:8080)
pytest -q
```

## Going live

1. Groww app/web: subscribe to the **Trade API** and open **Groww Cloud → API Keys**. Generate a **TOTP**
   key, then copy the API key and the TOTP secret.
2. SEBI requires API orders to come from a **static IP**. Run this on a small cloud VM (static external IP)
   and register that IP on the Groww API Keys page. A home connection usually won't work.
3. Deploy: `docker build -t monetary-flow . && docker run -d --env-file .env -v mf-data:/app/data -p 8080:8080 monetary-flow`.
   Put it behind HTTPS (Caddy/Cloudflare) before opening it from your phone.
4. In the UI, go to **Setup**: save the keys, then press **Test (read-only)**, which reads your profile, funds
   and holdings and places nothing. Set capital and style. **Add money in the Groww app via UPI**;
   Monetary Flow never touches money.
5. Paper-trade for a few weeks. Then type `I ACCEPT REAL MONEY RISK` to switch to live.

The **kill switch** cancels pending entries, sells every position the engine opened at market, and stops the
engine. Positions you hold yourself in Groww are never touched: the engine only trades its own sleeve.

## If this becomes a product for other people

Running this for yourself is fine. Offering automated strategies to other users is regulated in India.
SEBI's retail algo framework requires algo providers to be empanelled through the broker/exchange, and
managing other people's money needs PMS/RIA registration. So:

- users' money must stay in their own broker accounts (as it does here)
- any UPI payment would only be a subscription fee
- get legal advice before you sell access

## Layout

```
monetary_flow/
  config.py    settings + hard ceilings
  costs.py     Groww charge model
  risk.py      sizing + the gate every order passes
  strategy.py  the setup, market filter, thesis check
  engine.py    cycle: account -> manage -> entry; kill switch; daily report
  llm.py       optional BYO-key veto (Anthropic SDK / OpenAI-compatible HTTP)
  store.py     SQLite journal + Fernet-encrypted secrets
  brokers/     groww.py (live), paper.py (simulated fills), yahoo.py (free data)
  app.py       FastAPI + background loop
  web/         single-file mobile UI
```

## Known gaps (verify on a real account before trusting live mode)

- Groww response field names for order average price and historical candles are read defensively. Confirm
  them with **Test** plus one tiny live order.
- The stop is a DAY SL-M order that is re-armed each morning. Groww's OCO/GTT smart orders could make it
  persist overnight. That is not wired yet.
- There is no NSE holiday calendar. Holidays are detected by the stale NIFTY quote.
- One strategy, one timeframe. Backtest before raising capital.
