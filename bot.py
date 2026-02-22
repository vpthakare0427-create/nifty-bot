"""
╔══════════════════════════════════════════════════════════════════════╗
║   NIFTY OPTIONS PAPER TRADING BOT — LIVE VERSION                     ║
║   Converted from Backtest v6 | Dhan API | Deploy-Ready               ║
╠══════════════════════════════════════════════════════════════════════╣
║  Features:                                                           ║
║  ✅ Persistent capital & state (survives restarts)                   ║
║  ✅ SQLite trade logging (months of history)                         ║
║  ✅ Paper trading (no real orders placed)                            ║
║  ✅ IST timezone aware                                               ║
║  ✅ Auto-restart safe                                                ║
║  ✅ Railway/Render deploy ready                                      ║
║  ✅ All strategy params from Backtest v6                             ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import requests
import pandas as pd
import numpy as np
import sqlite3
import json
import os
import time
import logging
import warnings
from datetime import datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

# ══════════════════════════════════════════════════════════════════════
# 🔧 LOGGING SETUP
# ══════════════════════════════════════════════════════════════════════

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s IST | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("logs/bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("NiftyBot")

IST = ZoneInfo("Asia/Kolkata")

def now_ist():
    return datetime.now(IST)

# ══════════════════════════════════════════════════════════════════════
# 🔑 CREDENTIALS — loaded from environment variables
# ══════════════════════════════════════════════════════════════════════

CLIENT_ID    = os.getenv("DHAN_CLIENT_ID",    "1106812224")
ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9"
    ".eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzcxNTY0NDY3"
    ".AAunRW0B-2epXeclux7ewL9NHZ_d0d-zTlWVcR1IKnbkXO8V4TZRpACiiZc7"
    "KS-0xulm4nGqM7lM5Rm7lA-T8g"
))

HEADERS = {
    "access-token": ACCESS_TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json"
}

# ══════════════════════════════════════════════════════════════════════
# ⚙️  STRATEGY PARAMETERS (from Backtest v6 — do not change lightly)
# ══════════════════════════════════════════════════════════════════════

TOTAL_CAPITAL   = 100_000
NUM_UNITS       = 5
UNIT_SIZE       = 20_000        # ₹20k each — fits 1-2 Nifty lots

INTERVAL        = "15"          # 15-min candles

# Market hours IST
ENTRY_START     = (9,  30)
ENTRY_END       = (14, 30)
HARD_CLOSE      = (15, 10)

# Indicators
EMA_FAST, EMA_SLOW, EMA_TREND = 9, 21, 50
BB_LEN, BB_STD                = 20, 2.0
RSI_LEN, ADX_LEN              = 14, 14

# Signal parameters
ADX_MIN         = 20
RSI_CE          = (42, 70)
RSI_PE          = (30, 58)
MIN_CONFIRMS    = 2
SIG_COOLDOWN    = 3             # bars between entries per unit

# Position sizing
MAX_COST_PCT    = 0.55
MAX_LOTS        = 2

# SL / TP on option premium
SL_DROP_PCT     = 0.40          # Exit if premium falls 40%
TP_GAIN_PCT     = 1.00          # Exit if premium doubles
TRAIL_START     = 0.60          # Trail after +60% gain
TRAIL_LOCK      = 0.75          # Lock 75% of peak gain
MIN_HOLD        = 2             # Min bars before SL/TP

# Risk management
MAX_LOSS_STREAK      = 3
COOLDOWN_AFTER_LOSS  = 6
MAX_UNIT_DAY_LOSS    = 0.15
MAX_PORT_DAY_LOSS    = 0.08

LOT_SIZE_DEFAULT     = 65

# How often the main loop runs (seconds)
LOOP_SLEEP      = 60            # check every 60 seconds

# ══════════════════════════════════════════════════════════════════════
# 💾 PERSISTENT STATE — survives restarts
# ══════════════════════════════════════════════════════════════════════

STATE_FILE = "bot_state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        log.info(f"📂 State loaded | Capital: ₹{sum(u['capital'] for u in state['units']):,.0f}")
        return state
    # First run — initialize
    log.info("🆕 First run — initializing state")
    return {
        "units": [
            {
                "uid":      i,
                "capital":  float(UNIT_SIZE),
                "streak":   0,
                "cooldown": 0,
                "n_trades": 0,
                "last_bar": -99,
                "trade":    None,        # open position (if any)
                "day_pnl":  {}
            }
            for i in range(NUM_UNITS)
        ],
        "total_bars_seen": 0,
        "rr_ptr": 0
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ══════════════════════════════════════════════════════════════════════
# 🗄️  DATABASE — persistent trade history
# ══════════════════════════════════════════════════════════════════════

DB_FILE = "trading.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        datetime     TEXT,
        unit         INTEGER,
        opt_type     TEXT,
        symbol       TEXT,
        strike       REAL,
        entry_time   TEXT,
        exit_time    TEXT,
        entry_spot   REAL,
        exit_spot    REAL,
        entry_prem   REAL,
        exit_prem    REAL,
        qty          INTEGER,
        lot_size     REAL,
        bars_held    INTEGER,
        pnl          REAL,
        reason       TEXT,
        live_data    INTEGER,
        capital_after REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_summary (
        date             TEXT PRIMARY KEY,
        opening_capital  REAL,
        closing_capital  REAL,
        total_trades     INTEGER,
        winning_trades   INTEGER,
        daily_pnl        REAL,
        signals_seen     INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        datetime  TEXT,
        direction TEXT,
        spot      REAL,
        adx       REAL,
        rsi       REAL,
        acted_on  INTEGER DEFAULT 0
    )''')
    conn.commit()
    conn.close()
    log.info("✅ Database initialized")

def log_trade_db(trade_dict):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO trades
        (datetime, unit, opt_type, symbol, strike, entry_time, exit_time,
         entry_spot, exit_spot, entry_prem, exit_prem, qty, lot_size,
         bars_held, pnl, reason, live_data, capital_after)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
        now_ist().strftime("%Y-%m-%d %H:%M:%S"),
        trade_dict["unit"], trade_dict["opt_type"], trade_dict["symbol"],
        trade_dict["strike"], str(trade_dict["entry_time"]), str(trade_dict["exit_time"]),
        trade_dict["entry_spot"], trade_dict["exit_spot"],
        trade_dict["entry_prem"], trade_dict["exit_prem"],
        trade_dict["qty"], trade_dict["lot_size"], trade_dict["bars_held"],
        trade_dict["pnl"], trade_dict["reason"],
        1 if trade_dict.get("live_data") else 0,
        trade_dict.get("capital_after", 0)
    ))
    conn.commit()
    conn.close()

def log_signal_db(ts, direction, spot, adx, rsi, acted_on=False):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO signals (datetime, direction, spot, adx, rsi, acted_on)
                 VALUES (?,?,?,?,?,?)''',
              (str(ts), direction, spot, adx, rsi, 1 if acted_on else 0))
    conn.commit()
    conn.close()

def save_daily_summary(date_str, opening_cap, closing_cap, trades, wins, pnl, signals):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO daily_summary
        (date, opening_capital, closing_capital, total_trades, winning_trades, daily_pnl, signals_seen)
        VALUES (?,?,?,?,?,?,?)''',
        (date_str, opening_cap, closing_cap, trades, wins, pnl, signals))
    conn.commit()
    conn.close()

# ══════════════════════════════════════════════════════════════════════
# 📡 DATA FETCHING
# ══════════════════════════════════════════════════════════════════════

def _to_ist(df):
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata").tz_localize(None)
    else:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    return df

def _parse(data):
    keys = ["open", "high", "low", "close", "volume", "timestamp"]
    if not all(k in data for k in keys):
        return None
    df = pd.DataFrame({k: data[k] for k in keys})
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s")
    df.set_index("datetime", inplace=True)
    df.drop("timestamp", axis=1, inplace=True)
    return _to_ist(df)

def market_hours(df):
    return df[
        ((df.index.hour == 9)  & (df.index.minute >= 15)) |
        ((df.index.hour >  9)  & (df.index.hour   <  15)) |
        ((df.index.hour == 15) & (df.index.minute <= 30))
    ]

def fetch_spot(lookback_days=5):
    end   = now_ist().strftime("%Y-%m-%d")
    start = (now_ist() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    try:
        r = requests.post(
            "https://api.dhan.co/v2/charts/intraday",
            json={
                "securityId": "13",
                "exchangeSegment": "IDX_I",
                "instrument": "INDEX",
                "interval": INTERVAL,
                "fromDate": start,
                "toDate": end
            },
            headers=HEADERS, timeout=30
        )
        if r.status_code != 200:
            log.error(f"Spot API {r.status_code}: {r.text[:100]}")
            return None
        df = _parse(r.json())
        if df is not None:
            df = market_hours(df)
            log.info(f"📊 Fetched {len(df)} spot candles")
        return df
    except Exception as e:
        log.error(f"Spot fetch error: {e}")
        return None

def fetch_option_price(security_id):
    """Fetch latest option candle price."""
    end   = now_ist().strftime("%Y-%m-%d")
    start = (now_ist() - timedelta(days=5)).strftime("%Y-%m-%d")
    try:
        r = requests.post(
            "https://api.dhan.co/v2/charts/intraday",
            json={
                "securityId": str(security_id),
                "exchangeSegment": "NSE_FNO",
                "instrument": "OPTIDX",
                "interval": INTERVAL,
                "fromDate": start,
                "toDate": end
            },
            headers=HEADERS, timeout=20
        )
        if r.status_code != 200:
            return None
        df = _parse(r.json())
        if df is not None and len(df) > 0:
            df = market_hours(df)
            return float(df["close"].iloc[-1]) if len(df) > 0 else None
        return None
    except Exception:
        return None

def load_scrip():
    for f in ["api-scrip-master.csv", "scrip-master.csv"]:
        if os.path.exists(f):
            df = pd.read_csv(f, low_memory=False)
            log.info(f"✅ Scrip master loaded: {len(df):,} rows")
            return df
    log.warning("⚠️ Scrip master not found — will use synthetic contracts")
    return None

# ══════════════════════════════════════════════════════════════════════
# 📐 INDICATORS (exact same as backtest)
# ══════════════════════════════════════════════════════════════════════

def add_indicators(df):
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    df["ema9"]  = c.ewm(span=EMA_FAST,  adjust=False).mean()
    df["ema21"] = c.ewm(span=EMA_SLOW,  adjust=False).mean()
    df["ema50"] = c.ewm(span=EMA_TREND, adjust=False).mean()

    df["bb_mid"]   = c.rolling(BB_LEN).mean()
    df["bb_std"]   = c.rolling(BB_LEN).std()
    df["bb_up"]    = df["bb_mid"] + BB_STD * df["bb_std"]
    df["bb_dn"]    = df["bb_mid"] - BB_STD * df["bb_std"]
    df["bb_width"] = (df["bb_up"] - df["bb_dn"]) / df["bb_mid"]

    d = c.diff()
    df["rsi"] = 100 - 100 / (
        1 + d.clip(lower=0).rolling(RSI_LEN).mean() /
            (-d.clip(upper=0)).rolling(RSI_LEN).mean().replace(0, np.nan)
    )

    tr  = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    dmp = (h - h.shift()).clip(lower=0)
    dmn = (l.shift() - l).clip(lower=0)
    dmp = dmp.where(dmp >= dmn, 0)
    dmn = dmn.where(dmn > dmp, 0)
    atr = tr.ewm(span=ADX_LEN, adjust=False).mean()
    dp  = 100 * dmp.ewm(span=ADX_LEN, adjust=False).mean() / atr.replace(0, np.nan)
    dn  = 100 * dmn.ewm(span=ADX_LEN, adjust=False).mean() / atr.replace(0, np.nan)
    dx  = (100 * (dp - dn).abs() / (dp + dn).replace(0, np.nan))
    df["adx"]  = dx.ewm(span=ADX_LEN, adjust=False).mean()
    df["di_p"] = dp
    df["di_n"] = dn

    df["_d"]   = df.index.date
    df["vwap"] = (c * v).groupby(df["_d"]).cumsum() / v.groupby(df["_d"]).cumsum()
    df.drop("_d", axis=1, inplace=True)

    df["vol_ma"]    = v.rolling(20).mean()
    df["vol_ratio"] = v / df["vol_ma"].replace(0, np.nan)
    df["mom3"]      = c - c.shift(3)
    df["close_pct"] = (c - l) / (h - l).replace(0, np.nan)

    df["x_up"]    = (df["ema9"] > df["ema21"]) & (df["ema9"].shift(1) <= df["ema21"].shift(1))
    df["x_dn"]    = (df["ema9"] < df["ema21"]) & (df["ema9"].shift(1) >= df["ema21"].shift(1))
    df["bb_b_up"] = (c > df["bb_dn"]) & (c.shift(1) <= df["bb_dn"])
    df["bb_b_dn"] = (c < df["bb_up"]) & (c.shift(1) >= df["bb_up"])
    df["bb_x_up"] = (c > df["bb_mid"]) & (c.shift(1) <= df["bb_mid"])
    df["bb_x_dn"] = (c < df["bb_mid"]) & (c.shift(1) >= df["bb_mid"])

    return df

# ══════════════════════════════════════════════════════════════════════
# 🎯 SIGNAL ENGINE (exact same logic as backtest)
# ══════════════════════════════════════════════════════════════════════

def get_signal(row):
    if pd.isna(row["adx"]) or row["adx"] < ADX_MIN:
        return None
    if row["bb_width"] < 0.003:
        return None

    if row["x_up"] or row["bb_b_up"] or row["bb_x_up"]:
        confirms = sum([
            RSI_CE[0] <= row["rsi"] <= RSI_CE[1],
            row["mom3"] > 0,
            row["close_pct"] > 0.50,
            row["vol_ratio"] > 0.80,
            row["di_p"] > row["di_n"],
            row["close"] > row["vwap"],
        ])
        if confirms >= MIN_CONFIRMS:
            return "CE"

    if row["x_dn"] or row["bb_b_dn"] or row["bb_x_dn"]:
        confirms = sum([
            RSI_PE[0] <= row["rsi"] <= RSI_PE[1],
            row["mom3"] < 0,
            row["close_pct"] < 0.50,
            row["vol_ratio"] > 0.80,
            row["di_n"] > row["di_p"],
            row["close"] < row["vwap"],
        ])
        if confirms >= MIN_CONFIRMS:
            return "PE"

    return None

# ══════════════════════════════════════════════════════════════════════
# 📦 CONTRACT SELECTOR
# ══════════════════════════════════════════════════════════════════════

class ContractSelector:
    def __init__(self, scrip_df):
        self._cache = {}
        self._opts  = self._prep(scrip_df)

    def _prep(self, df):
        if df is None:
            return None
        o = df[df["SEM_INSTRUMENT_NAME"] == "OPTIDX"].copy()
        o = o[o["SEM_TRADING_SYMBOL"].str.startswith("NIFTY", na=False)].copy()
        o = o[~o["SEM_TRADING_SYMBOL"].str.contains("BANK|FIN|MID|NEXT", na=False)]
        o["SEM_EXPIRY_DATE"] = pd.to_datetime(o["SEM_EXPIRY_DATE"], errors="coerce")
        return o

    def get(self, spot, opt_type, trade_date):
        atm = round(spot / 50) * 50
        key = (atm, opt_type, str(trade_date))
        if key in self._cache:
            return self._cache[key]

        if self._opts is None:
            return self._synth(spot, opt_type)

        dt  = pd.Timestamp(trade_date)
        fut = self._opts[self._opts["SEM_EXPIRY_DATE"] > dt]["SEM_EXPIRY_DATE"].unique()
        if not len(fut):
            return self._synth(spot, opt_type)

        nearest_exp = sorted(fut)[0]
        pool = self._opts[
            (self._opts["SEM_EXPIRY_DATE"] == nearest_exp) &
            (self._opts["SEM_OPTION_TYPE"] == opt_type)
        ].copy()

        if pool.empty:
            return self._synth(spot, opt_type)

        pool["diff"] = (pool["SEM_STRIKE_PRICE"] - atm).abs()
        best = pool.nsmallest(1, "diff").iloc[0]

        c = {
            "sid":      str(best["SEM_SMST_SECURITY_ID"]),
            "symbol":   best["SEM_TRADING_SYMBOL"],
            "strike":   float(best["SEM_STRIKE_PRICE"]),
            "expiry":   best["SEM_EXPIRY_DATE"],
            "lot_size": float(best["SEM_LOT_UNITS"]),
            "opt_type": opt_type,
            "dte":      (nearest_exp - dt).days,
        }
        self._cache[key] = c
        return c

    def _synth(self, spot, opt_type):
        return {
            "sid":      None,
            "symbol":   f"NIFTY_ATM_{opt_type}_SYN",
            "strike":   round(spot / 50) * 50,
            "expiry":   now_ist() + timedelta(days=5),
            "lot_size": float(LOT_SIZE_DEFAULT),
            "opt_type": opt_type,
            "dte":      5,
        }

# ══════════════════════════════════════════════════════════════════════
# 💹 OPTION PRICING
# ══════════════════════════════════════════════════════════════════════

def synth_prem(spot, dte, iv=0.15):
    T    = max(dte / 365.0, 0.5 / 365)
    prem = spot * iv * np.sqrt(T) * 0.3989
    return max(prem, spot * 0.002)

def pnl_from_premiums(entry_prem, exit_prem, lot_size, qty):
    slippage = (entry_prem + exit_prem) * 0.003 * lot_size * qty
    raw      = (exit_prem - entry_prem) * lot_size * qty
    pnl      = raw - slippage
    max_loss = -entry_prem * lot_size * qty
    return round(max(pnl, max_loss), 2)

# ══════════════════════════════════════════════════════════════════════
# 📊 PERFORMANCE REPORT
# ══════════════════════════════════════════════════════════════════════

def print_performance_report():
    conn = sqlite3.connect(DB_FILE)
    try:
        df = pd.read_sql_query("SELECT * FROM trades", conn)
        if df.empty:
            log.info("📊 No trades yet to report")
            return
        total  = len(df)
        wins   = len(df[df["pnl"] > 0])
        wr     = wins / total * 100 if total > 0 else 0
        t_pnl  = df["pnl"].sum()
        avg_w  = df[df["pnl"] > 0]["pnl"].mean() if wins > 0 else 0
        avg_l  = df[df["pnl"] <= 0]["pnl"].mean() if (total - wins) > 0 else 0
        best   = df["pnl"].max()
        worst  = df["pnl"].min()
        loss_sum = df[df["pnl"] < 0]["pnl"].sum()
        win_sum  = df[df["pnl"] > 0]["pnl"].sum()
        pf     = win_sum / abs(loss_sum) if loss_sum != 0 else float("inf")

        log.info("═" * 60)
        log.info("  📈 PERFORMANCE REPORT")
        log.info("═" * 60)
        log.info(f"  Total Trades : {total}")
        log.info(f"  Win Rate     : {wr:.1f}%")
        log.info(f"  Total P&L    : ₹{t_pnl:,.0f}")
        log.info(f"  Avg Win      : ₹{avg_w:,.0f}")
        log.info(f"  Avg Loss     : ₹{avg_l:,.0f}")
        log.info(f"  Best Trade   : ₹{best:,.0f}")
        log.info(f"  Worst Trade  : ₹{worst:,.0f}")
        log.info(f"  Profit Factor: {pf:.2f}")
        log.info("═" * 60)
    finally:
        conn.close()

# ══════════════════════════════════════════════════════════════════════
# 🚀 MAIN TRADING LOOP
# ══════════════════════════════════════════════════════════════════════

def run_bot():
    log.info("═" * 60)
    log.info("  🤖 NIFTY OPTIONS PAPER TRADING BOT STARTING")
    log.info(f"  Capital  : ₹{TOTAL_CAPITAL:,} | {NUM_UNITS} units × ₹{UNIT_SIZE:,}")
    log.info(f"  SL/TP    : -{SL_DROP_PCT*100:.0f}% / +{TP_GAIN_PCT*100:.0f}% premium")
    log.info(f"  Strategy : EMA {EMA_FAST}/{EMA_SLOW}/{EMA_TREND} | BB | RSI | ADX | VWAP")
    log.info("═" * 60)

    # Initialize
    init_db()
    state  = load_state()
    scrip  = load_scrip()
    cs     = ContractSelector(scrip)

    # Track daily opening capital
    daily_open_cap = {}
    daily_signals  = defaultdict(int)

    prev_candle_ts = None   # to track new candle events

    while True:
        try:
            ts_now = now_ist()
            h, m   = ts_now.hour, ts_now.minute
            day    = ts_now.date()

            # ── Sleep outside market hours ────────────────────────
            is_market_day = ts_now.weekday() < 5   # Mon-Fri
            after_open    = (h > 9) or (h == 9 and m >= 15)
            before_close  = (h < 15) or (h == 15 and m <= 30)

            if not is_market_day or not after_open or not before_close:
                # Print EOD report once at 15:35
                if h == 15 and m == 35:
                    print_performance_report()
                    save_daily_summary(
                        str(day),
                        daily_open_cap.get(str(day), TOTAL_CAPITAL),
                        sum(u["capital"] for u in state["units"]),
                        0, 0, 0, daily_signals.get(str(day), 0)
                    )
                next_check = 300 if not is_market_day else 60
                log.info(f"💤 Market closed — sleeping {next_check}s")
                time.sleep(next_check)
                continue

            # ── Fetch latest candles ──────────────────────────────
            spot_df = fetch_spot(lookback_days=5)
            if spot_df is None or len(spot_df) < 55:
                log.warning("⚠️ Not enough candle data — retrying in 60s")
                time.sleep(60)
                continue

            # Track daily open
            day_str = str(day)
            if day_str not in daily_open_cap:
                daily_open_cap[day_str] = sum(u["capital"] for u in state["units"])
                log.info(f"📅 New day {day_str} | Opening capital: ₹{daily_open_cap[day_str]:,.0f}")

            spot_df = add_indicators(spot_df).dropna()
            if len(spot_df) < 2:
                time.sleep(60)
                continue

            # Get the last completed candle (not the forming one)
            row     = spot_df.iloc[-2]
            bar_ts  = spot_df.index[-2]
            bar_idx = state["total_bars_seen"]

            # Skip if we already processed this candle
            if str(bar_ts) == str(prev_candle_ts):
                time.sleep(LOOP_SLEEP)
                continue

            prev_candle_ts = bar_ts
            state["total_bars_seen"] += 1

            log.info(f"🕯️  Bar [{bar_ts}] Spot={row['close']:.0f} "
                     f"ADX={row['adx']:.1f} RSI={row['rsi']:.1f}")

            in_entry = (
                (h == ENTRY_START[0] and m >= ENTRY_START[1]) or
                (ENTRY_START[0] < h < ENTRY_END[0]) or
                (h == ENTRY_END[0] and m <= ENTRY_END[1])
            )
            is_eod  = (h > HARD_CLOSE[0]) or (h == HARD_CLOSE[0] and m >= HARD_CLOSE[1])
            port_pnl_today = sum(
                u["day_pnl"].get(day_str, 0.0) for u in state["units"]
            )
            port_ok = port_pnl_today > -(TOTAL_CAPITAL * MAX_PORT_DAY_LOSS)

            # ═══ PROCESS EACH UNIT ═══════════════════════════════
            for u in state["units"]:
                # Decrement cooldown
                if u["cooldown"] > 0:
                    u["cooldown"] -= 1

                # ── EXIT LOGIC ───────────────────────────────────
                if u["trade"] is not None:
                    t         = u["trade"]
                    bars_held = bar_idx - t["bar"]
                    reason    = None

                    # Get current option premium
                    cur_prem = None
                    if t.get("sid"):
                        cur_prem = fetch_option_price(t["sid"])

                    if cur_prem is None:
                        # Synthetic price estimate
                        spot_chg  = row["close"] - t["entry_spot"]
                        sign      = 1 if t["opt_type"] == "CE" else -1
                        prem_move = sign * spot_chg * 0.50
                        cur_prem  = max(
                            t["entry_prem"] + prem_move - t["entry_prem"] * 0.00025 * bars_held,
                            0.05
                        )

                    # Update trailing peak
                    t["peak_prem"] = max(t.get("peak_prem", t["entry_prem"]), cur_prem)

                    sl_level    = t["entry_prem"] * (1 - SL_DROP_PCT)
                    tp_level    = t["entry_prem"] * (1 + TP_GAIN_PCT)
                    trail_floor = None
                    if t["peak_prem"] >= t["entry_prem"] * (1 + TRAIL_START):
                        trail_floor = t["peak_prem"] * TRAIL_LOCK

                    if is_eod:
                        reason = "EOD"
                    elif bars_held >= MIN_HOLD:
                        if cur_prem <= sl_level:
                            reason = "SL_PREM"
                        elif cur_prem >= tp_level:
                            reason = "TP_PREM"
                        elif trail_floor and cur_prem < trail_floor:
                            reason = "TRAIL"

                    if reason:
                        pnl = pnl_from_premiums(
                            t["entry_prem"], cur_prem, t["lot_size"], t["qty"]
                        )
                        u["capital"] += pnl
                        u["day_pnl"][day_str] = u["day_pnl"].get(day_str, 0.0) + pnl
                        u["trade"] = None

                        if pnl < 0:
                            u["streak"] += 1
                            if u["streak"] >= MAX_LOSS_STREAK:
                                u["cooldown"] = COOLDOWN_AFTER_LOSS
                                u["streak"]   = 0
                        else:
                            u["streak"] = 0

                        trade_record = {
                            "unit":        u["uid"],
                            "opt_type":    t["opt_type"],
                            "symbol":      t["symbol"],
                            "strike":      t["strike"],
                            "entry_time":  t["entry_time"],
                            "exit_time":   str(bar_ts),
                            "entry_spot":  t["entry_spot"],
                            "exit_spot":   row["close"],
                            "entry_prem":  t["entry_prem"],
                            "exit_prem":   cur_prem,
                            "qty":         t["qty"],
                            "lot_size":    t["lot_size"],
                            "bars_held":   bars_held,
                            "pnl":         pnl,
                            "reason":      reason,
                            "live_data":   t.get("live_entry", False),
                            "capital_after": u["capital"]
                        }
                        log_trade_db(trade_record)

                        emoji = "✅" if pnl >= 0 else "🔴"
                        log.info(
                            f"{emoji} CLOSE [Unit {u['uid']}] {t['opt_type']} | "
                            f"P&L: ₹{pnl:,.0f} | Reason: {reason} | "
                            f"Capital now: ₹{u['capital']:,.0f}"
                        )

            # ═══ ENTRY LOGIC ═════════════════════════════════════
            if in_entry and port_ok and not is_eod:
                direction = get_signal(row)

                if direction:
                    daily_signals[day_str] = daily_signals.get(day_str, 0) + 1
                    log_signal_db(bar_ts, direction, row["close"], row["adx"], row["rsi"])
                    log.info(f"🎯 Signal: {direction} | Spot={row['close']:.0f} "
                             f"ADX={row['adx']:.1f} RSI={row['rsi']:.1f}")

                    rr_ptr = state["rr_ptr"]
                    assigned = False

                    for attempt in range(NUM_UNITS):
                        uid = (rr_ptr + attempt) % NUM_UNITS
                        u   = state["units"][uid]

                        # Check unit availability
                        if u["trade"] is not None:
                            continue
                        if u["cooldown"] > 0:
                            continue
                        if u["day_pnl"].get(day_str, 0.0) <= -(UNIT_SIZE * MAX_UNIT_DAY_LOSS):
                            continue
                        if bar_idx - u.get("last_bar", -99) < SIG_COOLDOWN:
                            continue

                        # Get contract
                        contract   = cs.get(row["close"], direction, day)
                        sid        = contract["sid"]
                        lot_size   = contract["lot_size"]
                        dte        = contract.get("dte", 5)

                        # Get premium
                        entry_prem = None
                        live_entry = False
                        if sid:
                            entry_prem = fetch_option_price(sid)
                            live_entry = entry_prem is not None

                        if entry_prem is None:
                            entry_prem = synth_prem(row["close"], dte)

                        if entry_prem <= 0:
                            continue

                        cost_1lot = entry_prem * lot_size

                        # Position sizing (same as backtest)
                        if cost_1lot > u["capital"] * MAX_COST_PCT:
                            if cost_1lot > u["capital"]:
                                log.debug(f"Unit {uid}: cost ₹{cost_1lot:.0f} > capital ₹{u['capital']:.0f}")
                                continue
                            qty = 1
                        else:
                            budget = u["capital"] * MAX_COST_PCT
                            qty    = min(MAX_LOTS, max(1, int(budget / cost_1lot)))

                        total_cost = entry_prem * lot_size * qty
                        if total_cost > u["capital"]:
                            qty = max(1, int(u["capital"] / cost_1lot))
                        if qty == 0 or entry_prem * lot_size > u["capital"]:
                            continue

                        # ✅ ENTRY CONFIRMED
                        u["trade"] = {
                            "bar":        bar_idx,
                            "entry_time": str(bar_ts),
                            "entry_spot": float(row["close"]),
                            "entry_prem": float(entry_prem),
                            "sid":        sid,
                            "symbol":     contract["symbol"],
                            "strike":     float(contract["strike"]),
                            "opt_type":   direction,
                            "qty":        qty,
                            "lot_size":   float(lot_size),
                            "live_entry": live_entry,
                            "peak_prem":  float(entry_prem),
                        }
                        u["last_bar"]  = bar_idx
                        u["n_trades"] += 1
                        state["rr_ptr"] = (uid + 1) % NUM_UNITS
                        assigned = True

                        cost = entry_prem * lot_size * qty
                        log.info(
                            f"📥 ENTER [Unit {uid}] {direction} | "
                            f"Symbol: {contract['symbol']} | Strike: {contract['strike']} | "
                            f"Prem: ₹{entry_prem:.1f} | Qty: {qty} lots | "
                            f"Cost: ₹{cost:,.0f} | Live: {live_entry}"
                        )
                        break

                    if not assigned:
                        log.debug(f"Signal {direction} — no free unit available")

            # ── Save state after every bar ────────────────────────
            save_state(state)

            total_cap = sum(u["capital"] for u in state["units"])
            log.info(
                f"💼 Portfolio: ₹{total_cap:,.0f} | "
                f"Day P&L: ₹{port_pnl_today:,.0f} | "
                f"Open positions: {sum(1 for u in state['units'] if u['trade'])}"
            )

            # Sleep until next candle (~15 min)
            time.sleep(LOOP_SLEEP)

        except KeyboardInterrupt:
            log.info("🛑 Bot stopped by user")
            save_state(state)
            print_performance_report()
            break
        except Exception as e:
            log.error(f"❌ Unexpected error: {e}", exc_info=True)
            save_state(state)
            time.sleep(60)   # retry after 1 min

if __name__ == "__main__":
    run_bot()
