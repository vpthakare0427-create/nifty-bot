"""
data.py — Dhan API data fetching, contract selection, price cache
"""

import requests
import pandas as pd
import numpy as np
import logging
import os
from datetime import datetime, timedelta
import config

logger = logging.getLogger("data")

HEADERS = {
    "access-token": config.ACCESS_TOKEN,
    "client-id"   : config.CLIENT_ID,
    "Content-Type": "application/json",
}


# ══════════════════════════════════════════════════════
# 📡  DHAN API HELPERS
# ══════════════════════════════════════════════════════

def _to_ist(df: pd.DataFrame) -> pd.DataFrame:
    df["datetime"] = (
        pd.to_datetime(df["timestamp"], unit="s", utc=True)
        .dt.tz_convert("Asia/Kolkata")
        .dt.tz_localize(None)
    )
    df.set_index("datetime", inplace=True)
    df.drop("timestamp", axis=1, errors="ignore", inplace=True)
    return df


def fetch_spot(days_back: int = 2) -> pd.DataFrame | None:
    """Fetch Nifty spot 15-min candles."""
    end   = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    try:
        r = requests.post(
            "https://api.dhan.co/v2/charts/intraday",
            json={
                "securityId"    : "13",
                "exchangeSegment": "IDX_I",
                "instrument"    : "INDEX",
                "interval"      : config.INTERVAL,
                "fromDate"      : start,
                "toDate"        : end,
            },
            headers=HEADERS, timeout=30
        )
        if r.status_code != 200:
            logger.error(f"Spot API {r.status_code}: {r.text[:120]}")
            return None

        data = r.json()
        df   = pd.DataFrame({
            "open"     : data["open"],
            "high"     : data["high"],
            "low"      : data["low"],
            "close"    : data["close"],
            "volume"   : data["volume"],
            "timestamp": data["timestamp"],
        })
        df = _to_ist(df)

        # Keep only market hours
        df = df[
            ((df.index.hour == 9)  & (df.index.minute >= 15)) |
            ((df.index.hour >  9)  & (df.index.hour   <  15)) |
            ((df.index.hour == 15) & (df.index.minute <= 30))
        ].copy()
        return df if len(df) >= 10 else None

    except Exception as e:
        logger.error(f"fetch_spot error: {e}")
        return None


def fetch_option(security_id: str, days_back: int = 2) -> pd.DataFrame | None:
    """Fetch option 15-min candles."""
    if not security_id:
        return None
    end   = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    try:
        r = requests.post(
            "https://api.dhan.co/v2/charts/intraday",
            json={
                "securityId"    : str(security_id),
                "exchangeSegment": "NSE_FNO",
                "instrument"    : "OPTIDX",
                "interval"      : config.INTERVAL,
                "fromDate"      : start,
                "toDate"        : end,
            },
            headers=HEADERS, timeout=30
        )
        if r.status_code != 200:
            return None
        data = r.json()
        df   = pd.DataFrame({
            "open": data["open"], "high": data["high"],
            "low": data["low"], "close": data["close"],
            "volume": data["volume"], "timestamp": data["timestamp"],
        })
        df = _to_ist(df)
        return df if len(df) > 0 else None
    except Exception:
        return None


def fetch_ltp(security_id: str) -> float | None:
    """Get live last traded price."""
    try:
        r = requests.post(
            "https://api.dhan.co/v2/marketfeed/ltp",
            json={"NSE_FNO": [str(security_id)]},
            headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            data = r.json().get("data", {})
            for seg in data.values():
                if isinstance(seg, list):
                    for item in seg:
                        if str(item.get("securityId")) == str(security_id):
                            p = float(item.get("lastTradedPrice", 0))
                            return p if p > 0 else None
    except Exception as e:
        logger.debug(f"LTP fail {security_id}: {e}")
    return None


# ══════════════════════════════════════════════════════
# 📦  CONTRACT SELECTOR
# ══════════════════════════════════════════════════════

class ContractSelector:
    def __init__(self):
        self._df    = self._load()
        self._cache = {}
        n = len(self._df) if self._df is not None else 0
        logger.info(f"ContractSelector: {n} rows loaded")

    def _load(self) -> pd.DataFrame | None:
        if not os.path.exists(config.SCRIP_FILE):
            logger.warning(f"No scrip master at {config.SCRIP_FILE} — synthetic mode")
            return None
        try:
            df = pd.read_csv(config.SCRIP_FILE, low_memory=False)
            df = df[df["SEM_INSTRUMENT_NAME"] == "OPTIDX"].copy()
            df = df[df["SEM_TRADING_SYMBOL"].str.startswith("NIFTY", na=False)].copy()
            df = df[~df["SEM_TRADING_SYMBOL"].str.contains(
                "BANK|FIN|MID|NEXT", na=False)].copy()
            df["SEM_EXPIRY_DATE"] = pd.to_datetime(
                df["SEM_EXPIRY_DATE"], errors="coerce")
            return df
        except Exception as e:
            logger.error(f"Scrip load error: {e}")
            return None

    def get(self, spot: float, opt_type: str, trade_date=None) -> dict:
        from datetime import date
        td  = trade_date or date.today()
        atm = round(spot / 50) * 50
        key = (atm, opt_type, str(td))
        if key not in self._cache:
            self._cache[key] = self._find(spot, atm, opt_type, td)
        return self._cache[key]

    def _find(self, spot, atm, opt_type, td) -> dict:
        if self._df is None:
            return self._synth(spot, opt_type)
        dt   = pd.Timestamp(td)
        fut  = self._df[self._df["SEM_EXPIRY_DATE"] > dt]["SEM_EXPIRY_DATE"].unique()
        if not len(fut):
            return self._synth(spot, opt_type)
        nearest = sorted(fut)[0]
        pool    = self._df[
            (self._df["SEM_EXPIRY_DATE"] == nearest) &
            (self._df["SEM_OPTION_TYPE"] == opt_type)
        ].copy()
        if pool.empty:
            return self._synth(spot, opt_type)
        pool["diff"] = (pool["SEM_STRIKE_PRICE"] - atm).abs()
        best = pool.nsmallest(1, "diff").iloc[0]
        dte  = (nearest - dt).days
        return {
            "sid"     : str(best["SEM_SMST_SECURITY_ID"]),
            "symbol"  : best["SEM_TRADING_SYMBOL"],
            "strike"  : float(best["SEM_STRIKE_PRICE"]),
            "expiry"  : best["SEM_EXPIRY_DATE"],
            "lot_size": float(best.get("SEM_LOT_UNITS", config.LOT_SIZE)),
            "opt_type": opt_type,
            "dte"     : int(dte),
        }

    def _synth(self, spot, opt_type) -> dict:
        atm = round(spot / 50) * 50
        return {
            "sid"     : "",
            "symbol"  : f"NIFTY{atm}{opt_type}",
            "strike"  : atm,
            "expiry"  : None,
            "lot_size": float(config.LOT_SIZE),
            "opt_type": opt_type,
            "dte"     : 5,
        }

    def clear_cache(self):
        self._cache.clear()


# ══════════════════════════════════════════════════════
# 📈  OPTION PRICE CACHE
# ══════════════════════════════════════════════════════

class PriceCache:
    """In-memory cache of option OHLCV data."""

    def __init__(self):
        self._data: dict[str, pd.DataFrame] = {}

    def load(self, sid: str):
        if not sid:
            return
        df = fetch_option(sid, days_back=2)
        if df is not None and len(df) > 0:
            self._data[sid] = df
            logger.info(f"Loaded option data: {sid} ({len(df)} bars)")

    def get_price(self, sid: str, ts=None) -> float | None:
        """Get option price at given timestamp."""
        if not sid:
            return None
        if sid not in self._data:
            # Try live LTP
            return fetch_ltp(sid)
        df  = self._data[sid]
        if ts is None:
            return float(df["close"].iloc[-1]) if len(df) > 0 else None
        pos = df.index.searchsorted(ts, side="right")
        idx = min(max(pos - 1, 0), len(df) - 1)
        p   = float(df["close"].iloc[idx])
        return p if p > 0.5 else None

    def is_loaded(self, sid: str) -> bool:
        return sid in self._data and len(self._data[sid]) > 0

    def refresh(self, sid: str):
        if sid:
            df = fetch_option(sid, days_back=1)
            if df is not None and len(df) > 0:
                self._data[sid] = df
