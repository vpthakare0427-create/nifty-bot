"""
strategy.py — Simple, effective signal engine
DESIGN GOAL: 2-3 trades per day
LOGIC: EMA crossover + RSI confirmation + ADX filter
No over-engineering. Simple rules that work.
"""

import numpy as np
import pandas as pd
import config


# ══════════════════════════════════════════════════════
# 📐  INDICATORS
# ══════════════════════════════════════════════════════

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add EMA9, EMA21, RSI14, ADX14 to dataframe."""
    c = df["close"]
    h = df["high"]
    l = df["low"]

    # EMAs
    df["ema9"]  = c.ewm(span=9,  adjust=False).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()

    # RSI
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ADX (simplified True Range version)
    tr  = pd.concat([h - l,
                     (h - c.shift()).abs(),
                     (l - c.shift()).abs()], axis=1).max(axis=1)
    dmp = (h - h.shift()).clip(lower=0)
    dmn = (l.shift() - l).clip(lower=0)
    dmp = dmp.where(dmp >= dmn, 0)
    dmn = dmn.where(dmn > dmp, 0)
    atr  = tr.ewm(span=14, adjust=False).mean().replace(0, np.nan)
    dip  = 100 * dmp.ewm(span=14, adjust=False).mean() / atr
    din  = 100 * dmn.ewm(span=14, adjust=False).mean() / atr
    dx   = (100 * (dip - din).abs() / (dip + din).replace(0, np.nan))
    df["adx"]  = dx.ewm(span=14, adjust=False).mean()
    df["di_p"] = dip
    df["di_n"] = din

    # VWAP (daily reset)
    v = df["volume"]
    df["_d"]   = df.index.date
    df["vwap"] = (c * v).groupby(df["_d"]).cumsum() / \
                 v.groupby(df["_d"]).cumsum().replace(0, np.nan)
    df.drop("_d", axis=1, inplace=True)

    # Previous bar EMA positions for crossover detection
    df["ema_above"] = df["ema9"] > df["ema21"]    # 1 = ema9 above ema21
    df["ema_cross_up"] = df["ema_above"] & ~df["ema_above"].shift(1).fillna(False)
    df["ema_cross_dn"] = ~df["ema_above"] & df["ema_above"].shift(1).fillna(True)

    return df


# ══════════════════════════════════════════════════════
# 🎯  SIGNAL ENGINE — SIMPLE VERSION
#
# CE (Buy Call) when:
#   1. EMA9 crosses above EMA21  OR  EMA9 already above + RSI rising
#   2. RSI > RSI_BUY (55) — momentum confirming
#   3. ADX > 18 — trend exists (not sideways)
#   4. Price above VWAP
#
# PE (Buy Put) when:
#   1. EMA9 crosses below EMA21  OR  EMA9 below + RSI falling
#   2. RSI < RSI_SELL (45) — downward momentum
#   3. ADX > 18
#   4. Price below VWAP
#
# This generates 2-4 signals per day in normal trending market.
# ══════════════════════════════════════════════════════

def get_signal(row: pd.Series, prev_row: pd.Series = None) -> str | None:
    """
    Returns 'CE', 'PE', or None.
    prev_row used for momentum check (RSI direction).
    """
    # Safety checks
    for col in ["adx", "rsi", "ema9", "ema21", "vwap", "close"]:
        if pd.isna(row.get(col)):
            return None

    adx   = float(row["adx"])
    rsi   = float(row["rsi"])
    close = float(row["close"])
    vwap  = float(row["vwap"])

    # Must have some trend
    if adx < config.ADX_MIN:
        return None

    # RSI direction (is momentum increasing?)
    rsi_rising  = True
    rsi_falling = True
    if prev_row is not None and not pd.isna(prev_row.get("rsi")):
        rsi_rising  = rsi > float(prev_row["rsi"])
        rsi_falling = rsi < float(prev_row["rsi"])

    cross_up = bool(row.get("ema_cross_up", False))
    cross_dn = bool(row.get("ema_cross_dn", False))
    ema_up   = float(row["ema9"]) > float(row["ema21"])
    ema_dn   = float(row["ema9"]) < float(row["ema21"])

    # ── CE signal ────────────────────────────────────
    ce_confirms = sum([
        cross_up or (ema_up and rsi_rising),  # primary: crossover or continuing up
        rsi > config.RSI_BUY,                 # RSI momentum
        close > vwap,                          # price above VWAP
        row.get("di_p", 0) > row.get("di_n", 0),  # bulls dominating
    ])
    if ce_confirms >= config.MIN_CONFIRMS:
        return "CE"

    # ── PE signal ────────────────────────────────────
    pe_confirms = sum([
        cross_dn or (ema_dn and rsi_falling),  # primary: crossover or continuing down
        rsi < config.RSI_SELL,                  # RSI momentum
        close < vwap,                            # price below VWAP
        row.get("di_n", 0) > row.get("di_p", 0),  # bears dominating
    ])
    if pe_confirms >= config.MIN_CONFIRMS:
        return "PE"

    return None


# ══════════════════════════════════════════════════════
# 💹  OPTION PRICING
# ══════════════════════════════════════════════════════

def synth_premium(spot: float, dte: float = 5, iv: float = 0.15) -> float:
    """ATM option fair value estimate (Brenner-Subrahmanyam)."""
    T    = max(dte / 365.0, 0.5 / 365)
    prem = spot * iv * (T ** 0.5) * 0.3989
    return max(round(prem, 1), spot * 0.002)


def estimate_exit_premium(entry_prem: float, entry_spot: float,
                          exit_spot: float, opt_type: str,
                          bars_held: int) -> float:
    """Model exit premium when live price unavailable."""
    sign     = 1 if opt_type == "CE" else -1
    spot_chg = exit_spot - entry_spot
    delta    = 0.50
    gamma    = 0.00008
    prem_chg = sign * (delta * spot_chg + 0.5 * gamma * spot_chg**2)
    theta    = entry_prem * 0.0002 * bars_held
    result   = entry_prem + prem_chg - theta
    return max(result, entry_prem * 0.05)   # floor at 5% of entry


def calc_pnl(entry_prem: float, exit_prem: float,
             lot_size: float, qty: int) -> float:
    """Net P&L including slippage (0.3% round-trip)."""
    raw      = (exit_prem - entry_prem) * lot_size * qty
    slip     = (entry_prem + exit_prem) * 0.003 * lot_size * qty
    max_loss = -entry_prem * lot_size * qty   # can't lose more than paid
    return round(max(raw - slip, max_loss), 2)
