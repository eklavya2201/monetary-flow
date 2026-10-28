"""Free delayed data (Yahoo Finance) so paper mode works before paying for the Groww API.
Never used for live orders: it can lag ~15 min and is unofficial."""
import time

import pandas as pd
import yfinance as yf

from . import INDEX


def _ticker(sym: str) -> str:
    return "^NSEI" if sym == INDEX else f"{sym}.NS"


class YahooData:
    def candles(self, sym: str, days: int = 400) -> pd.DataFrame:
        df = yf.download(_ticker(sym), period=f"{days}d", interval="1d", progress=False, auto_adjust=False)
        if df.empty:
            return df
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
        return df

    def bars(self, sym: str) -> pd.DataFrame:
        df = yf.download(_ticker(sym), period="1d", interval="5m", progress=False, auto_adjust=False)
        if df.empty:
            return df
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()

    def quote(self, sym: str) -> dict:
        df = yf.download(_ticker(sym), period="1d", interval="1m", progress=False, auto_adjust=False)
        if df.empty:
            return {"price": None, "ts": 0}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        last = df.dropna().iloc[-1]
        return {"price": float(last["Close"]), "ts": int(df.dropna().index[-1].timestamp()), "fetched": int(time.time())}
