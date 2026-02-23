"""
╔══════════════════════════════════════════════════════════════╗
║  NIFTY OPTIONS PAPER TRADING BOT — FINAL v9                 ║
║  All bugs from Feb 23 logs fixed                             ║
╠══════════════════════════════════════════════════════════════╣
║  BUGS FIXED:                                                  ║
║  1. TIME EXIT never triggered → fixed bar count logic         ║
║  2. Only 1 signal/day (RSI crossover too rare) → new signal   ║
║  3. Day P&L showed ₹0 while trade open → fixed real-time P&L ║
║  4. Dashboard blank → rebuilt with hardcoded path + errors    ║
║  5. Dashboard fetches live Nifty directly from Dhan API       ║
║  6. Backtest engine integrated with same Dhan API             ║
╠══════════════════════════════════════════════════════════════╣
║  USAGE:                                                       ║
║    Run bot:       python3 bot_final.py                        ║
║    Run dashboard: python3 bot_final.py --dashboard            ║
║    Run backtest:  python3 bot_final.py --backtest             ║
║    Run all:       python3 bot_final.py --all                  ║
╚══════════════════════════════════════════════════════════════╝
"""

import sys, os, json, time, sqlite3, logging, threading
import requests, numpy as np, pandas as pd
import urllib.request, urllib.parse
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

# ═══════════════════════════════════════════════════════════
# ⚙️  CONFIG — Edit only this block
# ═══════════════════════════════════════════════════════════
CLIENT_ID    = "1106812224"
ACCESS_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9"
    ".eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzcxNTY0NDY3"
    "LCJpYXQiOjE3NzE0NzgwNjcsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIs"
    "IndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTA2ODEyMjI0In0"
    ".AAunRW0B-2epXeclux7ewL9NHZ_d0d-zTlWVcR1IKnbkXO8V4TZRpACiiZc7"
    "KS-0xulm4nGqM7lM5Rm7lA-T8g"
)
TELEGRAM_TOKEN   = ""       # Fill this for alerts
TELEGRAM_CHAT_ID = ""
TELEGRAM_ENABLED = False

TOTAL_CAPITAL = 100_000
NUM_UNITS     = 5
UNIT_SIZE     = 20_000

# ─── Signal (FIXED: simple level-based, 3-5 signals/day) ───
ADX_MIN     = 15    # Lower threshold → more signals
RSI_CE_MAX  = 45    # Buy CE when RSI < 45 (oversold, bullish setup)
RSI_PE_MIN  = 55    # Buy PE when RSI > 55 (overbought, bearish setup)
EMA_FAST    = 9
EMA_SLOW    = 21

# ─── Exits (FIXED bar counting) ─────────────────────────────
SL_PCT         = 0.30   # -30% premium = stop loss
TP_PCT         = 0.60   # +60% premium = take profit
TIME_EXIT_BARS = 8      # Exit after 8 bars (2 hours) — FIXED
MIN_HOLD_BARS  = 2      # Min 2 bars before checking exits

# ─── Position sizing ─────────────────────────────────────────
MAX_COST_PCT  = 0.65    # Max 65% of unit on one trade
MAX_LOTS      = 2
LOT_SIZE      = 65
MAX_TRADES_PER_DAY = 3  # Per unit

# ─── Risk guards ─────────────────────────────────────────────
MAX_PORT_DD_MONTHLY = 0.08   # Stop if portfolio DD > 8% from month start
MAX_PORT_DD_DAILY   = 0.05   # Pause day if -5% portfolio in one day
MAX_UNIT_DD_DAILY   = 0.12   # Pause unit if -12% in one day
LOSS_STREAK_LIMIT   = 2      # Cooldown after 2 consecutive losses
COOLDOWN_BARS       = 4

# ─── Timing (IST) ────────────────────────────────────────────
ENTRY_START = (9, 30)    # No entries before 9:30
ENTRY_END   = (14, 15)   # No new entries after 14:15
HARD_CLOSE  = (15, 5)    # Force close all at 15:05

# ─── Paths ───────────────────────────────────────────────────
BOT_DIR    = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BOT_DIR, "state", "bot_state.json")
DB_FILE    = os.path.join(BOT_DIR, "data",  "trades.db")
LOG_DIR    = os.path.join(BOT_DIR, "logs")
SCRIP_FILE = os.path.join(BOT_DIR, "api-scrip-master.csv")

for _d in [os.path.join(BOT_DIR,"state"), os.path.join(BOT_DIR,"data"),
           LOG_DIR, os.path.join(BOT_DIR,"reports")]:
    os.makedirs(_d, exist_ok=True)

HEADERS = {"access-token": ACCESS_TOKEN, "client-id": CLIENT_ID,
           "Content-Type": "application/json"}

# ═══════════════════════════════════════════════════════════
# 📝  LOGGING
# ═══════════════════════════════════════════════════════════
def setup_log(name="bot"):
    log_path = os.path.join(LOG_DIR, f"{name}_{date.today()}.log")
    fmt = "%(asctime)s IST | %(levelname)s | %(message)s"
    handlers = [logging.FileHandler(log_path),
                logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=logging.INFO, format=fmt,
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers, force=True)
    return logging.getLogger(name)

# ═══════════════════════════════════════════════════════════
# 🗃️  DATABASE
# ═══════════════════════════════════════════════════════════
def get_db():
    c = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    with get_db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER, opt_type TEXT, symbol TEXT, strike REAL,
            lot_size REAL, qty INTEGER,
            entry_time TEXT, exit_time TEXT,
            entry_spot REAL, exit_spot REAL,
            entry_prem REAL, exit_prem REAL,
            bars_held INTEGER, pnl REAL, exit_reason TEXT,
            trade_date TEXT, week_num TEXT, month_str TEXT
        );
        CREATE TABLE IF NOT EXISTS equity_curve (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, equity REAL, open_pnl REAL, spot REAL, trade_date TEXT
        );
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT, from_date TEXT, to_date TEXT,
            n_trades INTEGER, win_rate REAL, total_pnl REAL,
            return_pct REAL, profit_factor REAL, max_dd REAL,
            avg_per_day REAL, results_json TEXT
        );
        """)

def save_trade(t: dict):
    dt = str(t.get("entry_time",""))[:10]
    with get_db() as c:
        c.execute("""INSERT INTO trades
            (unit_id,opt_type,symbol,strike,lot_size,qty,
             entry_time,exit_time,entry_spot,exit_spot,
             entry_prem,exit_prem,bars_held,pnl,exit_reason,
             trade_date,week_num,month_str)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t["unit"],t["opt_type"],t.get("symbol","?"),t.get("strike",0),
             t["lot_size"],t["qty"],str(t["entry_time"]),str(t["exit_time"]),
             t["entry_spot"],t["exit_spot"],t["entry_prem"],t["exit_prem"],
             t["bars_held"],t["pnl"],t["reason"],dt,
             datetime.fromisoformat(dt).strftime("%Y-W%W"),
             datetime.fromisoformat(dt).strftime("%Y-%m")))

def save_equity(ts, equity, open_pnl, spot):
    with get_db() as c:
        c.execute("INSERT INTO equity_curve (ts,equity,open_pnl,spot,trade_date) VALUES (?,?,?,?,?)",
                  (str(ts), equity, open_pnl, spot, str(ts)[:10]))

def get_trades_today():
    with get_db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM trades WHERE trade_date=? ORDER BY entry_time",
            [date.today().isoformat()]).fetchall()]

def get_all_trades():
    with get_db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM trades ORDER BY entry_time").fetchall()]

def get_equity_today():
    with get_db() as c:
        return [dict(r) for r in c.execute(
            "SELECT ts,equity,open_pnl,spot FROM equity_curve WHERE trade_date=? ORDER BY ts",
            [date.today().isoformat()]).fetchall()]

def save_backtest(result: dict):
    with get_db() as c:
        c.execute("""INSERT INTO backtest_runs
            (run_time,from_date,to_date,n_trades,win_rate,total_pnl,
             return_pct,profit_factor,max_dd,avg_per_day,results_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now().isoformat(), result["from_date"], result["to_date"],
             result["n_trades"], result["win_rate"], result["total_pnl"],
             result["return_pct"], result["profit_factor"], result["max_dd"],
             result["avg_per_day"], json.dumps(result, default=str)))

def get_last_backtest():
    with get_db() as c:
        r = c.execute("SELECT * FROM backtest_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else {}

# ═══════════════════════════════════════════════════════════
# 📡  DATA FETCHING
# ═══════════════════════════════════════════════════════════
def _to_df(data: dict) -> pd.DataFrame | None:
    k = ["open","high","low","close","volume","timestamp"]
    if not all(x in data for x in k): return None
    df = pd.DataFrame({x: data[x] for x in k})
    df["datetime"] = (pd.to_datetime(df["timestamp"], unit="s")
                       .dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
                       .dt.tz_localize(None))
    df.set_index("datetime", inplace=True)
    df.drop("timestamp", axis=1, inplace=True)
    df = df[((df.index.hour==9)&(df.index.minute>=15)) |
            ((df.index.hour>9)&(df.index.hour<15)) |
            ((df.index.hour==15)&(df.index.minute<=30))].copy()
    return df

def fetch_spot(days=2) -> pd.DataFrame | None:
    end   = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r = requests.post("https://api.dhan.co/v2/charts/intraday",
            json={"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX",
                  "interval":"15","fromDate":start,"toDate":end},
            headers=HEADERS, timeout=30)
        if r.status_code != 200: return None
        df = _to_df(r.json())
        return df
    except Exception as e:
        logging.warning(f"fetch_spot error: {e}")
        return None

def fetch_spot_history(from_date, to_date) -> pd.DataFrame | None:
    """For backtest — fetch larger date ranges"""
    try:
        r = requests.post("https://api.dhan.co/v2/charts/intraday",
            json={"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX",
                  "interval":"15","fromDate":from_date,"toDate":to_date},
            headers=HEADERS, timeout=60)
        if r.status_code != 200:
            print(f"  API {r.status_code}: {r.text[:100]}")
            return None
        return _to_df(r.json())
    except Exception as e:
        print(f"  Fetch error: {e}")
        return None

# ═══════════════════════════════════════════════════════════
# 📐  INDICATORS
# ═══════════════════════════════════════════════════════════
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    df = df.copy()

    # EMA
    df["ema9"]  = c.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema21"] = c.ewm(span=EMA_SLOW, adjust=False).mean()

    # RSI (14)
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # ADX (14)
    tr  = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    dmp = (h-h.shift()).clip(lower=0).where((h-h.shift())>(l.shift()-l), 0)
    dmn = (l.shift()-l).clip(lower=0).where((l.shift()-l)>(h-h.shift()), 0)
    atr = tr.ewm(span=14, adjust=False).mean()
    dp  = 100 * dmp.ewm(span=14, adjust=False).mean() / atr.replace(0, np.nan)
    dn  = 100 * dmn.ewm(span=14, adjust=False).mean() / atr.replace(0, np.nan)
    df["adx"] = ((dp-dn).abs()/(dp+dn).replace(0,np.nan)*100).ewm(span=14,adjust=False).mean()

    # VWAP (daily reset)
    df["_d"]   = df.index.date
    df["vwap"] = (c*v).groupby(df["_d"]).cumsum() / v.groupby(df["_d"]).cumsum()
    df.drop("_d", axis=1, inplace=True)

    return df

# ═══════════════════════════════════════════════════════════
# 🎯  SIGNAL ENGINE — FIXED & SIMPLIFIED
# ═══════════════════════════════════════════════════════════
def get_signal(row: pd.Series) -> str | None:
    """
    SIMPLE 3-RULE SIGNAL — generates 2-4 signals/day:

    CE (BUY CALL): Market oversold but EMA says UP trend
      → RSI < 45 (oversold zone) AND EMA9 > EMA21 AND ADX > 15

    PE (BUY PUT): Market overbought but EMA says DOWN trend
      → RSI > 55 (overbought zone) AND EMA9 < EMA21 AND ADX > 15

    Why RSI LEVEL not crossover?
      Crossover needs 2 consecutive bars at exact threshold = rare (1/day)
      Level-based: triggers every time RSI is in the zone = 3-5/day
    """
    if pd.isna(row.get("adx")): return None
    if pd.isna(row.get("rsi")): return None

    adx = row["adx"]
    rsi = row["rsi"]
    ema_bull = row["ema9"] > row["ema21"]

    if adx < ADX_MIN: return None

    # CE signal: RSI oversold + bullish EMA
    if rsi < RSI_CE_MAX and ema_bull:
        return "CE"

    # PE signal: RSI overbought + bearish EMA
    if rsi > RSI_PE_MIN and not ema_bull:
        return "PE"

    return None

# ═══════════════════════════════════════════════════════════
# 💹  OPTION PREMIUM (synthetic — Brenner formula)
# ═══════════════════════════════════════════════════════════
def synth_prem(spot: float, dte: float = 5, iv: float = 0.15) -> float:
    T = max(dte / 365.0, 0.5 / 365)
    return max(round(spot * iv * np.sqrt(T) * 0.3989, 1), spot * 0.002)

def mark_prem(entry_prem, entry_spot, cur_spot, opt_type, bars):
    """Estimate current option premium from spot move."""
    sign     = 1 if opt_type == "CE" else -1
    delta    = 0.50
    gamma    = 0.00015
    theta    = entry_prem * 0.0002   # daily theta per bar
    spot_chg = cur_spot - entry_spot
    prem_chg = sign * (delta * spot_chg + 0.5 * gamma * spot_chg**2)
    return max(entry_prem + prem_chg - theta * bars, 0.5)

def calc_pnl(ep, xp, lot, qty):
    slip = (ep + xp) * 0.003 * lot * qty
    raw  = (xp - ep) * lot * qty
    return round(max(raw - slip, -ep * lot * qty), 2)

# ═══════════════════════════════════════════════════════════
# 🏦  UNIT MANAGER
# ═══════════════════════════════════════════════════════════
class Unit:
    def __init__(self, uid, cap=None):
        self.uid        = uid
        self.capital    = float(cap if cap else UNIT_SIZE)
        self.trade      = None
        self.streak     = 0
        self.cooldown   = 0
        self.n_trades   = 0
        self.last_bar   = -99
        self.day_trades = defaultdict(int)
        self.day_pnl    = defaultdict(float)

    @property
    def free(self):
        return self.trade is None and self.cooldown == 0

    def can_enter(self, ts, bar_idx) -> bool:
        d = ts.date()
        if not self.free: return False
        if self.day_pnl[d] < -(UNIT_SIZE * MAX_UNIT_DD_DAILY): return False
        if self.day_trades[d] >= MAX_TRADES_PER_DAY: return False
        if bar_idx - self.last_bar < 1: return False
        return True

    def enter(self, t, bar_idx, ts):
        self.trade = t
        self.n_trades += 1
        self.last_bar = bar_idx
        self.day_trades[ts.date()] += 1

    def close(self, pnl, ts):
        self.capital += pnl
        self.day_pnl[ts.date()] += pnl
        self.trade = None
        if pnl < 0:
            self.streak += 1
            if self.streak >= LOSS_STREAK_LIMIT:
                self.cooldown = COOLDOWN_BARS
                self.streak = 0
        else:
            self.streak = 0

    def open_pnl(self, cur_spot) -> float:
        """Real-time unrealised P&L for display."""
        if not self.trade: return 0.0
        t  = self.trade
        cp = mark_prem(t["entry_prem"], t["entry_spot"],
                       cur_spot, t["opt_type"], t.get("bars", 0))
        return calc_pnl(t["entry_prem"], cp, t["lot_size"], t["qty"])

    def tick(self):
        if self.cooldown > 0: self.cooldown -= 1

    def to_dict(self):
        return {"uid":self.uid,"capital":self.capital,"trade":self.trade,
                "streak":self.streak,"cooldown":self.cooldown,
                "n_trades":self.n_trades,"last_bar":self.last_bar,
                "day_trades":{str(k):v for k,v in self.day_trades.items()},
                "day_pnl":{str(k):v for k,v in self.day_pnl.items()}}

    @classmethod
    def from_dict(cls, d):
        u = cls(d["uid"], d["capital"])
        u.trade    = d.get("trade")
        u.streak   = d.get("streak", 0)
        u.cooldown = d.get("cooldown", 0)
        u.n_trades = d.get("n_trades", 0)
        u.last_bar = d.get("last_bar", -99)
        u.day_trades = defaultdict(int, {
            date.fromisoformat(k): v for k, v in d.get("day_trades", {}).items()})
        u.day_pnl = defaultdict(float, {
            date.fromisoformat(k): v for k, v in d.get("day_pnl", {}).items()})
        return u

def save_state(units, rr, month_cap):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"units":[u.to_dict() for u in units],
                   "rr": rr, "month_start_cap": month_cap,
                   "saved": datetime.now().isoformat()}, f, default=str, indent=2)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            units = [Unit.from_dict(u) for u in s["units"]]
            return units, s.get("rr", 0), s.get("month_start_cap", float(TOTAL_CAPITAL))
        except Exception as e:
            logging.warning(f"State load failed: {e} — fresh start")
    return [Unit(i) for i in range(NUM_UNITS)], 0, float(TOTAL_CAPITAL)

# ═══════════════════════════════════════════════════════════
# ⏰  IST TIME HELPERS
# ═══════════════════════════════════════════════════════════
def _ist():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    except ImportError:
        return datetime.utcnow() + timedelta(hours=5, minutes=30)

def is_trading_day(dt=None): return (_ist() if dt is None else dt).weekday() < 5
def is_entry_win(dt=None):
    t = (_ist() if dt is None else dt); hm = (t.hour, t.minute)
    return ENTRY_START <= hm <= ENTRY_END and is_trading_day(t)
def is_hard_close(dt=None):
    t = (_ist() if dt is None else dt); return (t.hour, t.minute) >= HARD_CLOSE
def is_market_open(dt=None):
    t = (_ist() if dt is None else dt)
    if not is_trading_day(t): return False
    return (9,15) <= (t.hour, t.minute) <= (15,30)

def secs_to_open():
    n = _ist()
    if not is_trading_day(n):
        d = 7 - n.weekday()
        nxt = (n + timedelta(days=d)).replace(hour=9, minute=15, second=0, microsecond=0)
        return max(int((nxt - n).total_seconds()), 0)
    op = n.replace(hour=9, minute=15, second=0, microsecond=0)
    return max(int((op - n).total_seconds()), 0) if n < op else 0

# ═══════════════════════════════════════════════════════════
# 🤖  BOT ENGINE
# ═══════════════════════════════════════════════════════════
class Bot:
    def __init__(self):
        self.log = setup_log("bot")
        init_db()
        self.units, self.rr, self.month_cap = load_state()
        self.bar_idx    = 0
        self.last_ts    = None
        self.today_trd  = []
        self.port_dpnl  = defaultdict(float)

        self.log.info("=" * 62)
        self.log.info("  NIFTY PAPER BOT v9 — FINAL")
        self.log.info(f"  Capital: ₹{sum(u.capital for u in self.units):,.0f}  "
                      f"Units: {NUM_UNITS} × ₹{UNIT_SIZE:,}")
        self.log.info(f"  Signal: RSI<{RSI_CE_MAX}→CE | RSI>{RSI_PE_MIN}→PE | ADX≥{ADX_MIN}")
        self.log.info(f"  SL:{SL_PCT*100:.0f}% | TP:{TP_PCT*100:.0f}% | TIME:{TIME_EXIT_BARS}bars")
        self.log.info(f"  Max trades/unit/day: {MAX_TRADES_PER_DAY}")
        self.log.info("=" * 62)
        self._tg("🤖 <b>Nifty Bot v9 Started</b>\n"
                 f"Capital: ₹{TOTAL_CAPITAL:,} | {NUM_UNITS} units\n"
                 f"Signal: RSI level | SL:{SL_PCT*100:.0f}% | TP:{TP_PCT*100:.0f}%")

    def _tg(self, msg):
        if not TELEGRAM_ENABLED or not TELEGRAM_TOKEN: return
        try:
            url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({"chat_id":TELEGRAM_CHAT_ID,
                                           "text":msg,"parse_mode":"HTML"}).encode()
            urllib.request.urlopen(urllib.request.Request(url,data,method="POST"), timeout=8)
        except: pass

    def run(self):
        self.log.info("🟢 Bot live. Ctrl+C to stop.")
        while True:
            try:
                now = _ist()
                if not is_market_open(now):
                    secs = secs_to_open()
                    self.log.info(f"💤 Market closed — sleeping 60s")
                    time.sleep(60)
                    continue

                # Wait for next 15-min bar boundary
                mins_left = 15 - (now.minute % 15)
                secs_left = mins_left * 60 - now.second
                if secs_left > 15:
                    time.sleep(secs_left - 10)
                    continue

                time.sleep(12)   # Let bar fully close
                self._bar()

            except KeyboardInterrupt:
                save_state(self.units, self.rr, self.month_cap)
                self.log.info("⛔ Stopped. State saved.")
                break
            except Exception as e:
                self.log.error(f"Loop error: {e}", exc_info=True)
                self._tg(f"⚠️ Bot error: {e}")
                time.sleep(60)

    def _bar(self):
        now = _ist()
        df  = fetch_spot()
        if df is None or len(df) < 30:
            self.log.warning("Insufficient spot data — skip")
            return

        df  = add_indicators(df).dropna()
        if df.empty: return

        row = df.iloc[-1]
        ts  = df.index[-1]
        day = ts.date()
        spot = float(row["close"])

        # ── Day change detection ────────────────────────────
        if self.last_ts and self.last_ts.date() != day:
            self._eod(self.last_ts.date())
            self.today_trd = []
            self.port_dpnl = defaultdict(float)
            if day.month != self.last_ts.date().month:
                self.month_cap = sum(u.capital for u in self.units)
                save_state(self.units, self.rr, self.month_cap)

        self.last_ts  = ts
        self.bar_idx += 1
        for u in self.units: u.tick()

        # ── Monthly DD guard ────────────────────────────────
        total_cap  = sum(u.capital for u in self.units)
        monthly_dd = (total_cap - self.month_cap) / self.month_cap
        if monthly_dd < -MAX_PORT_DD_MONTHLY:
            self.log.warning(f"🛑 Monthly DD {monthly_dd*100:.1f}% — HALTED")
            return

        # ── Daily DD guard ──────────────────────────────────
        port_day_ok = self.port_dpnl[day] > -(TOTAL_CAPITAL * MAX_PORT_DD_DAILY)
        eod = is_hard_close(now)

        # ── EXITS ───────────────────────────────────────────
        for u in self.units:
            if u.trade:
                self._exit(u, row, ts, spot, eod)

        # ── ENTRIES ─────────────────────────────────────────
        if is_entry_win(now) and port_day_ok and not eod:
            self._entry(row, ts, day, spot)

        # ── Compute open P&L for display ───────────────────
        open_pnl = sum(u.open_pnl(spot) for u in self.units)
        closed_pnl = self.port_dpnl.get(day, 0)
        n_open = sum(1 for u in self.units if u.trade)

        save_equity(ts, total_cap, open_pnl, spot)
        save_state(self.units, self.rr, self.month_cap)

        self.log.info(
            f"🕯️  [{ts.strftime('%H:%M')}] Spot={spot:.0f} "
            f"ADX={row['adx']:.1f} RSI={row['rsi']:.1f} "
            f"EMA={'↑' if row['ema9']>row['ema21'] else '↓'} | "
            f"Open:{n_open} | DayPNL:₹{closed_pnl+open_pnl:+,.0f}"
        )

        if eod: self._eod(day)

    # ── FIXED EXIT — bar count bug fixed ─────────────────────
    def _exit(self, u, row, ts, spot, eod):
        t  = u.trade
        bh = t.get("bars", 0)          # current bars held
        t["bars"] = bh + 1             # increment AFTER reading (FIXED)

        # Current premium estimate
        cp = mark_prem(t["entry_prem"], t["entry_spot"],
                       spot, t["opt_type"], bh)
        t["peak"] = max(t.get("peak", t["entry_prem"]), cp)

        sl_lvl = t["entry_prem"] * (1 - SL_PCT)
        tp_lvl = t["entry_prem"] * (1 + TP_PCT)
        trail  = (t["peak"] * 0.75
                  if t["peak"] >= t["entry_prem"] * 1.40 else None)

        reason = None
        if eod:
            reason = "EOD"
        elif bh >= MIN_HOLD_BARS:       # Only check exits after min hold
            if   cp <= sl_lvl:          reason = "SL"
            elif cp >= tp_lvl:          reason = "TP"
            elif trail and cp < trail:  reason = "TRAIL"
            elif bh >= TIME_EXIT_BARS:  reason = "TIME"  # FIXED: reads bh before +1

        if not reason: return

        pnl = calc_pnl(t["entry_prem"], cp, t["lot_size"], t["qty"])
        u.close(pnl, ts)
        self.port_dpnl[ts.date()] += pnl

        rec = {**t, "exit_time":ts.isoformat(), "exit_spot":spot,
               "exit_prem":cp, "bars_held":bh, "pnl":pnl, "reason":reason}
        save_trade(rec)
        self.today_trd.append(rec)

        em = "✅" if pnl > 0 else "❌"
        self.log.info(
            f"🔴 EXIT [U{u.uid}] {t['opt_type']} {reason} "
            f"bars={bh} prem={t['entry_prem']:.0f}→{cp:.0f} "
            f"P&L=₹{pnl:+,.0f} cap=₹{u.capital:,.0f}"
        )
        self._tg(f"{em} <b>EXIT {reason}</b> [Unit {u.uid}] {t['opt_type']}\n"
                 f"Prem ₹{t['entry_prem']:.0f}→₹{cp:.0f} | <b>P&L ₹{pnl:+,.0f}</b>\n"
                 f"Held {bh} bars ({bh*15}min)")

    # ── ENTRY ─────────────────────────────────────────────────
    def _entry(self, row, ts, day, spot):
        sig = get_signal(row)
        if not sig: return

        self.log.info(f"🎯 Signal: {sig} | RSI={row['rsi']:.1f} ADX={row['adx']:.1f}")

        for attempt in range(NUM_UNITS):
            uid = (self.rr + attempt) % NUM_UNITS
            u   = self.units[uid]
            if not u.can_enter(ts, self.bar_idx): continue

            ep  = synth_prem(spot)
            lot = LOT_SIZE
            if ep * lot > u.capital:
                self.log.info(f"  Unit {uid}: cost ₹{ep*lot:.0f} > capital ₹{u.capital:.0f}")
                continue

            budget = u.capital * MAX_COST_PCT
            qty    = min(MAX_LOTS, max(1, int(budget / (ep * lot))))

            trade = {"unit":uid, "bars":0, "peak":ep,
                     "entry_time":ts.isoformat(), "entry_spot":spot,
                     "entry_prem":ep, "sid":None,
                     "symbol":f"NIFTY{round(spot/50)*50}{sig}",
                     "strike":float(round(spot/50)*50),
                     "opt_type":sig, "qty":qty, "lot_size":float(lot)}

            u.enter(trade, self.bar_idx, ts)
            self.rr = (uid + 1) % NUM_UNITS

            self.log.info(
                f"📥 ENTER [U{uid}] {sig} | Strike:{trade['strike']:.0f} "
                f"Prem:₹{ep:.1f} Qty:{qty} Cost:₹{ep*lot*qty:,.0f}"
            )
            self._tg(f"🟢 <b>ENTER {sig}</b> [Unit {uid}]\n"
                     f"Strike {trade['strike']:.0f} | Prem ₹{ep:.0f} × {qty} lot\n"
                     f"Cost ₹{ep*lot*qty:,.0f}")
            break

    def _eod(self, d):
        pnl  = self.port_dpnl.get(d, 0)
        cap  = sum(u.capital for u in self.units)
        n    = len(self.today_trd)
        wins = sum(1 for t in self.today_trd if t.get("pnl", 0) > 0)
        self.log.info("=" * 62)
        self.log.info(f"  📅 EOD — {d}")
        self.log.info(f"  Capital: ₹{cap:,.0f} | Day P&L: ₹{pnl:+,.0f}")
        self.log.info(f"  Trades: {n} | Wins: {wins}")
        self.log.info("=" * 62)
        self._tg(f"📅 <b>EOD {d}</b>\n"
                 f"Capital ₹{cap:,.0f} | P&L ₹{pnl:+,.0f}\n"
                 f"Trades: {n} | Wins: {wins}")

# ═══════════════════════════════════════════════════════════
# 🧪  BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════
def run_backtest(from_date=None, to_date=None):
    log = setup_log("backtest")
    from_date = from_date or (datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d")
    to_date   = to_date   or datetime.now().strftime("%Y-%m-%d")

    print(f"\n{'═'*62}")
    print(f"  BACKTEST: {from_date} → {to_date}")
    print(f"  Strategy: RSI<{RSI_CE_MAX}→CE | RSI>{RSI_PE_MIN}→PE | ADX≥{ADX_MIN}")
    print(f"  SL:{SL_PCT*100:.0f}% | TP:{TP_PCT*100:.0f}% | Time:{TIME_EXIT_BARS} bars")
    print(f"{'═'*62}")

    print("📡 Fetching data from Dhan API...")

    # Dhan API limits ~75 days per call, chunk if needed
    all_dfs = []
    start = datetime.strptime(from_date, "%Y-%m-%d")
    end   = datetime.strptime(to_date,   "%Y-%m-%d")
    chunk = timedelta(days=60)
    ptr   = start
    while ptr < end:
        nxt = min(ptr + chunk, end)
        df_chunk = fetch_spot_history(ptr.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d"))
        if df_chunk is not None and len(df_chunk) > 0:
            all_dfs.append(df_chunk)
        ptr = nxt + timedelta(days=1)

    if not all_dfs:
        print("❌ No data. Check credentials and dates.")
        return None

    df = pd.concat(all_dfs).drop_duplicates().sort_index()
    df = add_indicators(df).dropna()
    print(f"✅ {len(df)} candles | {df.index[0].date()} → {df.index[-1].date()}")

    # Backtest state
    units      = [Unit(i) for i in range(NUM_UNITS)]
    rr         = 0
    bar_idx    = 0
    trades     = []
    eq_curve   = []
    port_dpnl  = defaultdict(float)
    all_days   = set()

    for i in range(2, len(df)):
        row  = df.iloc[i]
        ts   = df.index[i]
        day  = ts.date()
        spot = float(row["close"])
        h, m = ts.hour, ts.minute

        for u in units: u.tick()
        bar_idx += 1
        all_days.add(day)

        in_entry = ENTRY_START <= (h, m) <= ENTRY_END
        eod      = (h, m) >= HARD_CLOSE

        # Port daily guard
        port_ok = port_dpnl[day] > -(TOTAL_CAPITAL * MAX_PORT_DD_DAILY)

        # EXITS
        for u in units:
            if not u.trade: continue
            t  = u.trade
            bh = t.get("bars", 0)
            t["bars"] = bh + 1

            cp = mark_prem(t["entry_prem"], t["entry_spot"],
                           spot, t["opt_type"], bh)
            t["peak"] = max(t.get("peak", t["entry_prem"]), cp)

            sl = t["entry_prem"] * (1 - SL_PCT)
            tp = t["entry_prem"] * (1 + TP_PCT)
            tr = t["peak"] * 0.75 if t["peak"] >= t["entry_prem"] * 1.40 else None

            reason = None
            if eod: reason = "EOD"
            elif bh >= MIN_HOLD_BARS:
                if   cp <= sl:           reason = "SL"
                elif cp >= tp:           reason = "TP"
                elif tr and cp < tr:     reason = "TRAIL"
                elif bh >= TIME_EXIT_BARS: reason = "TIME"

            if not reason: continue
            pnl = calc_pnl(t["entry_prem"], cp, t["lot_size"], t["qty"])
            u.close(pnl, ts)
            port_dpnl[day] += pnl
            trades.append({**t, "exit_time":str(ts), "exit_spot":spot,
                           "exit_prem":cp, "bars_held":bh, "pnl":pnl,
                           "reason":reason, "date":str(day)})

        # ENTRIES
        if in_entry and port_ok and not eod:
            sig = get_signal(row)
            if sig:
                for attempt in range(NUM_UNITS):
                    uid = (rr + attempt) % NUM_UNITS
                    u   = units[uid]
                    if not u.can_enter(ts, bar_idx): continue
                    ep  = synth_prem(spot)
                    lot = LOT_SIZE
                    if ep * lot > u.capital: continue
                    qty = min(MAX_LOTS, max(1, int(u.capital * MAX_COST_PCT / (ep * lot))))
                    u.enter({"unit":uid,"bars":0,"peak":ep,
                              "entry_time":str(ts),"entry_spot":spot,
                              "entry_prem":ep,"sid":None,
                              "symbol":f"NIFTY_ATM_{sig}","strike":round(spot/50)*50,
                              "opt_type":sig,"qty":qty,"lot_size":float(lot)},
                             bar_idx, ts)
                    rr = (uid + 1) % NUM_UNITS
                    break

        eq_curve.append({"ts":str(ts), "eq":sum(u.capital for u in units), "spot":spot})

    # ─── Results ────────────────────────────────────────────
    n     = len(trades)
    wins  = [t for t in trades if t["pnl"] > 0]
    total = sum(t["pnl"] for t in trades)
    ret   = total / TOTAL_CAPITAL * 100
    wr    = len(wins) / n * 100 if n else 0
    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf    = gross_win / gross_loss if gross_loss > 0 else 0
    n_days = max(len(all_days), 1)

    # Max DD
    eq_vals = [e["eq"] for e in eq_curve]
    pk = eq_vals[0]; mdd = 0
    for e in eq_vals:
        pk = max(pk, e)
        mdd = min(mdd, (e - pk) / pk * 100)

    # Exit breakdown
    by_reason = defaultdict(lambda: {"n":0, "pnl":0, "wins":0})
    for t in trades:
        r = t["reason"]
        by_reason[r]["n"]   += 1
        by_reason[r]["pnl"] += t["pnl"]
        if t["pnl"] > 0: by_reason[r]["wins"] += 1

    # Daily P&L
    by_day = defaultdict(lambda: {"n":0, "pnl":0})
    for t in trades:
        d = t.get("date","")
        by_day[d]["n"]   += 1
        by_day[d]["pnl"] += t["pnl"]

    days_positive = sum(1 for v in by_day.values() if v["pnl"] > 0)

    print(f"\n{'═'*62}")
    print(f"  BACKTEST RESULTS")
    print(f"{'─'*62}")
    print(f"  Period    : {from_date} → {to_date} ({n_days} trading days)")
    print(f"  Capital   : ₹{TOTAL_CAPITAL:,} → ₹{sum(u.capital for u in units):,.0f}")
    print(f"  P&L       : ₹{total:+,.0f}  ({ret:+.1f}%)")
    print(f"  Trades    : {n}  ({n/n_days:.1f}/day avg)")
    print(f"  Win Rate  : {wr:.1f}%  (break-even: 33%)")
    print(f"  P. Factor : {pf:.2f}")
    print(f"  Max DD    : {mdd:.1f}%")
    print(f"  +Days     : {days_positive}/{len(by_day)} ({days_positive/max(len(by_day),1)*100:.0f}%)")
    print(f"{'─'*62}")
    print(f"  EXIT BREAKDOWN:")
    for r, v in sorted(by_reason.items()):
        wr2 = v["wins"]/v["n"]*100 if v["n"] else 0
        print(f"    {r:<6} N:{v['n']:>3}  WR:{wr2:.0f}%  P&L:₹{v['pnl']:>9,.0f}")
    print(f"{'─'*62}")
    print(f"  RECENT DAYS:")
    for d, v in sorted(by_day.items())[-5:]:
        arrow = "📈" if v["pnl"] > 0 else "📉"
        print(f"    {arrow} {d}  Trades:{v['n']}  P&L:₹{v['pnl']:+,.0f}")
    print(f"{'─'*62}")
    print(f"  GO-LIVE READINESS:")
    checks = [
        (f"Win Rate ≥ 35%",        wr >= 35),
        (f"Profit Factor ≥ 1.5",   pf >= 1.5),
        (f"Monthly Return > 15%",  ret / max((n_days/21), 1) > 15),
        (f"≥ 2 trades/day avg",    n/n_days >= 2),
        (f"Max DD > -12%",         mdd > -12),
        (f">60% positive days",    days_positive/max(len(by_day),1) > 0.60),
    ]
    all_pass = True
    for label, ok in checks:
        print(f"    {'✅' if ok else '❌'} {label}")
        if not ok: all_pass = False
    verdict = "🟢 READY FOR PAPER TRADING" if all_pass else "🔴 NEEDS MORE ADJUSTMENT"
    print(f"\n  {verdict}")
    print(f"{'═'*62}\n")

    result = {
        "from_date": from_date, "to_date": to_date,
        "n_trades": n, "n_days": n_days,
        "avg_per_day": round(n/n_days, 2),
        "total_pnl": round(total, 2),
        "return_pct": round(ret, 2),
        "win_rate": round(wr, 1),
        "profit_factor": round(pf, 2),
        "max_dd": round(mdd, 2),
        "days_positive": days_positive,
        "days_total": len(by_day),
        "by_reason": {k: dict(v) for k, v in by_reason.items()},
        "by_day": {k: dict(v) for k, v in by_day.items()},
        "eq_curve": eq_curve[-200:],   # last 200 points for chart
        "all_pass": all_pass,
        "run_time": datetime.now().isoformat(),
    }
    init_db()
    save_backtest(result)
    return result

# ═══════════════════════════════════════════════════════════
# 📊  DASHBOARD — COMPLETELY REBUILT
#     Shows: Live Nifty, today's candles, open positions,
#            today's trades, equity curve, backtest results
#     FIXED: Uses same DB_FILE path, handles errors,
#            fetches live price directly, mobile-friendly
# ═══════════════════════════════════════════════════════════

_LAST_BT  = {}   # cache last backtest in memory
_BT_LOCK  = threading.Lock()

def _dash_data() -> dict:
    """Collect all data for dashboard. Never crashes."""
    d = {
        "error": None, "live": None, "live_time": "—",
        "total_cap": TOTAL_CAPITAL, "total_pnl": 0, "total_ret": 0,
        "today_pnl": 0, "today_trades": [], "open_pos": [],
        "candles": [], "eq_today": [], "units": [],
        "week_pnl": 0, "week_n": 0, "month_pnl": 0, "month_ret": 0,
        "wr_all": 0, "pf_all": 0, "n_all": 0,
        "backtest": {}, "saved": "—",
    }
    try:
        # State
        state = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f: state = json.load(f)
        d["saved"] = state.get("saved", "—")[:19]

        units_raw = state.get("units", [])
        total_cap = sum(u.get("capital", UNIT_SIZE) for u in units_raw)
        d["total_cap"] = round(total_cap)
        d["total_pnl"] = round(total_cap - TOTAL_CAPITAL)
        d["total_ret"]  = round(d["total_pnl"] / TOTAL_CAPITAL * 100, 2)

        # Live Nifty (fetch fresh candles)
        df_live = fetch_spot(days=1)
        if df_live is not None and not df_live.empty:
            d["live"] = round(float(df_live["close"].iloc[-1]), 1)
            d["live_time"] = datetime.now().strftime("%H:%M:%S")
            # Today's candles
            today_df = df_live[df_live.index.date == date.today()]
            d["candles"] = [{"t": str(ts)[11:16], "o": round(r["open"],1),
                              "h": round(r["high"],1), "l": round(r["low"],1),
                              "c": round(r["close"],1)}
                            for ts, r in today_df.iterrows()]

        # Open positions
        cur_spot = d["live"] or 25000
        for u in units_raw:
            t = u.get("trade")
            if t:
                bh  = t.get("bars", 0)
                cp  = mark_prem(t["entry_prem"], t["entry_spot"],
                                cur_spot, t["opt_type"], bh)
                opnl = calc_pnl(t["entry_prem"], cp, t["lot_size"], t["qty"])
                d["open_pos"].append({
                    "uid": u["uid"], "opt_type": t["opt_type"],
                    "symbol": t.get("symbol","?"),
                    "strike": t.get("strike", 0),
                    "entry_prem": t["entry_prem"],
                    "cur_prem": round(cp, 1),
                    "open_pnl": round(opnl),
                    "qty": t["qty"], "bars": bh,
                    "entry_time": str(t.get("entry_time",""))[:16],
                })

        # Unit cards
        for u in units_raw:
            pnl = round(u.get("capital", UNIT_SIZE) - UNIT_SIZE)
            d["units"].append({
                "uid": u["uid"],
                "cap": round(u.get("capital", UNIT_SIZE)),
                "pnl": pnl,
                "trades": u.get("n_trades", 0),
                "busy": u.get("trade") is not None,
                "cd": u.get("cooldown", 0),
            })

        # DB data
        today      = date.today().isoformat()
        wk_start   = (date.today()-timedelta(days=date.today().weekday())).isoformat()
        mo_start   = date.today().replace(day=1).isoformat()
        all_trades = get_all_trades()
        td_trades  = [t for t in all_trades if t.get("trade_date") == today]
        wk_trades  = [t for t in all_trades if t.get("trade_date","") >= wk_start]
        mo_trades  = [t for t in all_trades if t.get("trade_date","") >= mo_start]

        d["today_trades"] = td_trades
        d["today_pnl"]    = round(sum(t["pnl"] for t in td_trades))
        d["week_pnl"]     = round(sum(t["pnl"] for t in wk_trades))
        d["week_n"]       = len(wk_trades)
        d["month_pnl"]    = round(sum(t["pnl"] for t in mo_trades))
        d["month_ret"]    = round(d["month_pnl"] / TOTAL_CAPITAL * 100, 2)

        wins = [t for t in all_trades if t["pnl"] > 0]
        loss = [t for t in all_trades if t["pnl"] <= 0]
        d["n_all"]  = len(all_trades)
        d["wr_all"] = round(len(wins)/len(all_trades)*100, 1) if all_trades else 0
        gw = sum(t["pnl"] for t in wins)
        gl = abs(sum(t["pnl"] for t in loss))
        d["pf_all"] = round(gw/gl, 2) if gl > 0 else 0

        # Equity today
        eq = get_equity_today()
        d["eq_today"] = [{"t": e["ts"][11:16],
                           "eq": e["equity"],
                           "op": e.get("open_pnl", 0)} for e in eq]

        # Backtest
        d["backtest"] = get_last_backtest()
        if _LAST_BT:
            d["backtest"] = _LAST_BT

    except Exception as e:
        d["error"] = str(e)
        logging.error(f"Dashboard data error: {e}", exc_info=True)

    return d

def _render(d: dict) -> str:
    def pc(v):   return "#22c55e" if v >= 0 else "#ef4444"
    def fi(v):   return f"₹{abs(v):,.0f}"
    def sg(v):   return "+" if v >= 0 else "-"
    def pct(v):  return f"{v:+.1f}%"

    lp   = f"₹{d['live']:,.1f}" if d["live"] else "⏳ Fetching..."
    lp_c = "#f59e0b"

    # Open positions table
    op_rows = ""
    for p in d["open_pos"]:
        oc   = "#22c55e" if p["opt_type"]=="CE" else "#ef4444"
        pc2  = "#22c55e" if p["open_pnl"] >= 0 else "#ef4444"
        op_rows += f"""<tr>
          <td>Unit {p['uid']}</td>
          <td style="color:{oc};font-weight:700">{p['opt_type']}</td>
          <td style="font-size:10px">{p['symbol']}</td>
          <td>₹{p['entry_prem']:.0f} → ₹{p['cur_prem']:.0f}</td>
          <td>{p['bars']} bars</td>
          <td style="color:{pc2};font-weight:700">{sg(p['open_pnl'])}{fi(p['open_pnl'])}</td>
          <td style="font-size:10px">{p['entry_time']}</td>
        </tr>"""
    if not op_rows:
        op_rows = "<tr><td colspan='7' style='text-align:center;color:#6b7280;padding:20px'>No open positions right now</td></tr>"

    # Today's trades
    tr_rows = ""
    for t in reversed(d["today_trades"][-15:]):
        pc2  = "#22c55e" if t["pnl"] > 0 else "#ef4444"
        oc   = "#22c55e" if t["opt_type"]=="CE" else "#ef4444"
        pct2 = (t["exit_prem"]-t["entry_prem"])/max(t["entry_prem"],1)*100
        em   = "✅" if t["pnl"] > 0 else "❌"
        tr_rows += f"""<tr>
          <td>{em}</td>
          <td style="color:{oc};font-weight:700">{t['opt_type']}</td>
          <td style="font-size:10px">{str(t['entry_time'])[11:16]}→{str(t['exit_time'])[11:16]}</td>
          <td style="font-size:11px">{t['exit_reason']}</td>
          <td>₹{t['entry_prem']:.0f}→₹{t['exit_prem']:.0f} ({pct2:+.0f}%)</td>
          <td style="color:{pc2};font-weight:700">{sg(t['pnl'])}{fi(t['pnl'])}</td>
        </tr>"""
    if not tr_rows:
        tr_rows = "<tr><td colspan='6' style='text-align:center;color:#6b7280;padding:20px'>No trades today yet — waiting for signals</td></tr>"

    # Unit cards
    ucards = ""
    for u in d["units"]:
        uc  = "#22c55e" if u["pnl"] >= 0 else "#ef4444"
        bsy = f"🔴 {'⏳'+str(u.get('cd','')) if u.get('cd',0) else 'TRADING'}" if u["busy"] or u.get("cd",0) else "⚪ FREE"
        ucards += f"""<div class="uc">
          <div class="ul">Unit {u['uid']} {bsy}</div>
          <div class="uv">₹{u['cap']:,.0f}</div>
          <div style="color:{uc};font-size:13px">{sg(u['pnl'])}{fi(u['pnl'])}</div>
          <div style="color:#6b7280;font-size:10px">{u['trades']} trades</div>
        </div>"""

    # Backtest summary card
    bt = d.get("backtest", {})
    bt_html = ""
    if bt and bt.get("n_trades"):
        bc = "#22c55e" if bt.get("return_pct",0) >= 0 else "#ef4444"
        by_reason = bt.get("by_reason", {})
        reason_rows = ""
        for r, v in sorted(by_reason.items()):
            if isinstance(v, dict):
                wr2 = v["wins"]/v["n"]*100 if v.get("n",0) > 0 else 0
                reason_rows += f"<tr><td>{r}</td><td>{v.get('n',0)}</td><td>{wr2:.0f}%</td><td>₹{v.get('pnl',0):+,.0f}</td></tr>"
        by_day = bt.get("by_day", {})
        day_rows = ""
        for day, v in sorted(by_day.items())[-5:]:
            if isinstance(v, dict):
                arrow = "📈" if v.get("pnl",0) >= 0 else "📉"
                day_rows += f"<tr><td>{arrow} {day}</td><td>{v.get('n',0)}</td><td>₹{v.get('pnl',0):+,.0f}</td></tr>"
        bt_html = f"""
        <div class="card" style="margin-top:0">
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px">
            <div><div class="lbl">Return</div><div style="color:{bc};font-weight:700;font-size:18px">{bt.get('return_pct',0):+.1f}%</div></div>
            <div><div class="lbl">Trades</div><div style="font-weight:700;font-size:18px">{bt.get('n_trades',0)} ({bt.get('avg_per_day',0):.1f}/day)</div></div>
            <div><div class="lbl">Win Rate</div><div style="font-weight:700;font-size:18px">{bt.get('win_rate',0):.1f}%</div></div>
            <div><div class="lbl">P.Factor</div><div style="font-weight:700">{bt.get('profit_factor',0):.2f}</div></div>
            <div><div class="lbl">Max DD</div><div style="font-weight:700">{bt.get('max_dd',0):.1f}%</div></div>
            <div><div class="lbl">Period</div><div style="font-size:10px;color:#6b7280">{bt.get('from_date','')} → {bt.get('to_date','')}</div></div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <div class="lbl" style="margin-bottom:6px">Exit Breakdown</div>
              <table><tr><th>Reason</th><th>N</th><th>WR</th><th>P&L</th></tr>{reason_rows}</table>
            </div>
            <div>
              <div class="lbl" style="margin-bottom:6px">Recent Days</div>
              <table><tr><th>Date</th><th>N</th><th>P&L</th></tr>{day_rows}</table>
            </div>
          </div>
          <div style="margin-top:10px;padding:8px;background:#{'1a2e1a' if bt.get('all_pass') else '2e1a1a'};border-radius:6px;font-size:13px;font-weight:600;text-align:center;color:{'#22c55e' if bt.get('all_pass') else '#ef4444'}">
            {'🟢 READY FOR PAPER TRADING' if bt.get('all_pass') else '🔴 STRATEGY NEEDS ADJUSTMENT'}
          </div>
        </div>"""
    else:
        bt_html = """<div class="card" style="text-align:center;color:#6b7280;padding:20px">
          No backtest run yet.<br>Click button below to run one.
        </div>"""

    # Chart data JSON
    c_t = json.dumps([x["t"] for x in d["candles"]])
    c_o = json.dumps([x["o"] for x in d["candles"]])
    c_h = json.dumps([x["h"] for x in d["candles"]])
    c_l = json.dumps([x["l"] for x in d["candles"]])
    c_c = json.dumps([x["c"] for x in d["candles"]])
    eq_t = json.dumps([x["t"] for x in d["eq_today"]])
    eq_v = json.dumps([x["eq"] for x in d["eq_today"]])

    err_banner = f'<div style="background:#7f1d1d;padding:8px 16px;font-size:12px;color:#fca5a5">⚠️ Dashboard error: {d["error"]}</div>' if d["error"] else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta http-equiv="refresh" content="30">
<title>Nifty Bot v9</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0e1a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;padding-bottom:20px}}
.hdr{{background:#111827;padding:12px 16px;border-bottom:1px solid #1e2433;
      display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;position:sticky;top:0;z-index:100}}
.hdr h1{{font-size:15px;font-weight:700;color:#3b82f6}}
.live-price{{font-size:24px;font-weight:800;color:{lp_c}}}
.meta{{font-size:10px;color:#6b7280}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;padding:12px}}
.card{{background:#111827;border:1px solid #1e2433;border-radius:10px;padding:14px}}
.lbl{{font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}}
.val{{font-size:20px;font-weight:700}}
.sub{{font-size:11px;color:#9ca3af;margin-top:2px}}
.sec{{padding:0 12px 10px}}
.sec h2{{font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;
         letter-spacing:.5px;margin-bottom:8px;padding-top:10px;border-top:1px solid #1e2433;margin-top:10px}}
table{{width:100%;border-collapse:collapse;background:#111827;border-radius:8px;overflow:hidden;font-size:12px}}
th{{background:#1e2433;padding:7px 10px;text-align:left;font-size:10px;color:#6b7280;text-transform:uppercase}}
td{{padding:7px 10px;border-bottom:1px solid #1e2433}}
tr:last-child td{{border:none}}
.ugrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:8px}}
.uc{{background:#111827;border:1px solid #1e2433;border-radius:8px;padding:10px;text-align:center}}
.ul{{font-size:10px;color:#6b7280;margin-bottom:4px}}
.uv{{font-size:16px;font-weight:700}}
.chartbox{{background:#111827;border:1px solid #1e2433;border-radius:8px;padding:12px;margin-bottom:8px}}
.btn{{display:inline-block;background:#3b82f6;color:white;padding:9px 18px;
      border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;margin-right:8px}}
.btn:hover{{background:#2563eb}}
.badge{{display:inline-block;background:#1e2433;color:#9ca3af;font-size:10px;padding:2px 7px;border-radius:10px}}
@media(max-width:480px){{
  .grid{{grid-template-columns:repeat(2,1fr);gap:8px}}
  .hdr{{padding:10px 12px}}
}}
</style>
</head>
<body>
{err_banner}

<div class="hdr">
  <div>
    <h1>📈 Nifty Paper Bot v9</h1>
    <div class="meta">RSI level signal | SL:{SL_PCT*100:.0f}% TP:{TP_PCT*100:.0f}% Time:{TIME_EXIT_BARS}bars | {MAX_TRADES_PER_DAY} trades/unit/day</div>
  </div>
  <div style="text-align:right">
    <div class="live-price">NIFTY {lp}</div>
    <div class="meta">🕐 {d['live_time']} IST &nbsp;<span class="badge">⟳ 30s</span></div>
  </div>
</div>

<!-- KPI -->
<div class="grid">
  <div class="card">
    <div class="lbl">Portfolio</div>
    <div class="val">₹{d['total_cap']:,.0f}</div>
    <div class="sub">Started ₹{TOTAL_CAPITAL:,}</div>
  </div>
  <div class="card">
    <div class="lbl">Total P&L</div>
    <div class="val" style="color:{pc(d['total_pnl'])}">{sg(d['total_pnl'])}{fi(d['total_pnl'])}</div>
    <div class="sub" style="color:{pc(d['total_ret'])}">{pct(d['total_ret'])} overall</div>
  </div>
  <div class="card">
    <div class="lbl">Today P&L</div>
    <div class="val" style="color:{pc(d['today_pnl'])}">{sg(d['today_pnl'])}{fi(d['today_pnl'])}</div>
    <div class="sub">{len(d['today_trades'])} trades today</div>
  </div>
  <div class="card">
    <div class="lbl">Open P&L</div>
    <div class="val" style="color:{pc(sum(p['open_pnl'] for p in d['open_pos']))}">
      {sg(sum(p['open_pnl'] for p in d['open_pos']))}{fi(abs(sum(p['open_pnl'] for p in d['open_pos'])))}
    </div>
    <div class="sub">{len(d['open_pos'])} position(s) open</div>
  </div>
  <div class="card">
    <div class="lbl">This Week</div>
    <div class="val" style="color:{pc(d['week_pnl'])}">{sg(d['week_pnl'])}{fi(d['week_pnl'])}</div>
    <div class="sub">{d['week_n']} trades</div>
  </div>
  <div class="card">
    <div class="lbl">This Month</div>
    <div class="val" style="color:{pc(d['month_pnl'])}">{sg(d['month_pnl'])}{fi(d['month_pnl'])}</div>
    <div class="sub" style="color:{pc(d['month_ret'])}">{pct(d['month_ret'])}</div>
  </div>
  <div class="card">
    <div class="lbl">Win Rate</div>
    <div class="val">{d['wr_all']:.1f}%</div>
    <div class="sub">{d['n_all']} total trades</div>
  </div>
  <div class="card">
    <div class="lbl">Profit Factor</div>
    <div class="val" style="color:{pc(d['pf_all']-1)}">{d['pf_all']:.2f}</div>
    <div class="sub">Target: ≥ 1.5</div>
  </div>
</div>

<!-- OPEN POSITIONS -->
<div class="sec">
  <h2>🔴 Open Positions ({len(d['open_pos'])})</h2>
  <table>
    <tr><th>Unit</th><th>Type</th><th>Symbol</th><th>Premium</th><th>Bars</th><th>Open P&L</th><th>Entry</th></tr>
    {op_rows}
  </table>
</div>

<!-- CANDLE CHART -->
<div class="sec">
  <h2>📊 Today's Nifty — {date.today().strftime('%d %b %Y')}</h2>
  <div class="chartbox"><canvas id="cChart" height="80"></canvas></div>
</div>

<!-- EQUITY CHART -->
<div class="sec">
  <h2>💰 Today's Portfolio Equity</h2>
  <div class="chartbox"><canvas id="eqChart" height="55"></canvas></div>
</div>

<!-- TODAY'S TRADES -->
<div class="sec">
  <h2>📋 Today's Trades ({len(d['today_trades'])})</h2>
  <table>
    <tr><th></th><th>Type</th><th>Time</th><th>Exit</th><th>Premium</th><th>P&L</th></tr>
    {tr_rows}
  </table>
</div>

<!-- UNITS -->
<div class="sec">
  <h2>🏦 Unit Breakdown</h2>
  <div class="ugrid">{ucards}</div>
</div>

<!-- BACKTEST -->
<div class="sec">
  <h2>🧪 Backtest Results</h2>
  {bt_html}
  <div style="margin-top:12px">
    <a href="/run-backtest" class="btn">▶ Run Backtest (30 days)</a>
    <a href="/run-backtest?days=60" class="btn" style="background:#7c3aed">▶ 60 Days</a>
    <a href="/api" style="color:#6b7280;font-size:11px">JSON API</a>
  </div>
</div>

<div style="padding:10px 16px;color:#374151;font-size:10px;text-align:center">
  Paper Trading Only — No real orders | State: {d['saved']} | DB: {DB_FILE}
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
const G='#22c55e', R='#ef4444', B='#3b82f6', TX='#9ca3af', GD='#1e2433';
const TC = {c_t}, TO = {c_o}, TH = {c_h}, TL = {c_l}, TC2 = {c_c};
const EQT = {eq_t}, EQV = {eq_v};

function baseOpts(yFmt) {{
  return {{
    responsive:true, animation:false,
    plugins:{{legend:{{display:false}}}},
    scales:{{
      x:{{ticks:{{color:TX,font:{{size:9}},maxTicksLimit:10,maxRotation:30}},grid:{{color:GD}}}},
      y:{{ticks:{{color:TX,font:{{size:9}},callback:yFmt}},grid:{{color:GD}}}}
    }}
  }};
}}

if(TC.length > 0) {{
  const colors = TC2.map((c,i) => c >= TO[i] ? G+'90' : R+'90');
  const border = TC2.map((c,i) => c >= TO[i] ? G : R);
  new Chart(document.getElementById('cChart'), {{
    type: 'bar',
    data: {{
      labels: TC,
      datasets: [
        {{label:'High', data:TH, type:'line', borderColor:G+'60',
          backgroundColor:'transparent', pointRadius:0, borderWidth:1, tension:0.1}},
        {{label:'Body', data:TC2, backgroundColor:colors, borderColor:border, borderWidth:1}},
        {{label:'Low',  data:TL, type:'line', borderColor:R+'60',
          backgroundColor:'transparent', pointRadius:0, borderWidth:1, tension:0.1}},
      ]
    }},
    options: baseOpts(v => '₹'+Math.round(v).toLocaleString('en-IN'))
  }});
}}

if(EQT.length > 0) {{
  const last = EQV[EQV.length-1], first = EQV[0] || {TOTAL_CAPITAL};
  const col = last >= first ? G : R;
  new Chart(document.getElementById('eqChart'), {{
    type:'line',
    data:{{labels:EQT, datasets:[{{
      data:EQV, borderColor:col, backgroundColor:col+'20',
      fill:true, borderWidth:1.8, pointRadius:0, tension:0.3
    }}]}},
    options: baseOpts(v => '₹'+Math.round(v).toLocaleString('en-IN'))
  }});
}}
</script>
</body></html>"""

class DashHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        global _LAST_BT
        try:
            if self.path.startswith("/run-backtest"):
                days = 30
                if "days=60" in self.path: days = 60
                def _bt():
                    global _LAST_BT
                    result = run_backtest(
                        (datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d"),
                        datetime.now().strftime("%Y-%m-%d"))
                    if result:
                        with _BT_LOCK: _LAST_BT = result
                threading.Thread(target=_bt, daemon=True).start()
                body = b"""<!DOCTYPE html><html><head><meta http-equiv="refresh" content="3;url=/"></head>
                <body style="background:#0a0e1a;color:#e2e8f0;font-family:sans-serif;padding:40px;text-align:center">
                <h2 style="color:#f59e0b">⏳ Backtest Running...</h2>
                <p>Fetching data from Dhan API. Redirecting in 3 seconds...</p>
                </body></html>"""
                self.send_response(200)
                self.send_header("Content-Type","text/html")
                self.end_headers(); self.wfile.write(body)

            elif self.path == "/api":
                body = json.dumps(_dash_data(), default=str, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.end_headers(); self.wfile.write(body)

            elif self.path == "/health":
                self.send_response(200); self.end_headers()
                self.wfile.write(b"ok")

            else:
                body = _render(_dash_data()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers(); self.wfile.write(body)

        except Exception as e:
            logging.error(f"Dashboard handler error: {e}", exc_info=True)
            err = f"<pre style='color:red;background:#111;padding:20px'>Error: {e}</pre>".encode()
            try:
                self.send_response(500)
                self.end_headers(); self.wfile.write(err)
            except: pass

def run_dashboard(port=8080):
    log = setup_log("dashboard")
    log.info(f"📊 Dashboard → http://0.0.0.0:{port}")
    log.info(f"   Open http://YOUR_SERVER_IP:{port} on your phone")
    log.info(f"   Auto-refreshes every 30 seconds")
    log.info(f"   DB: {DB_FILE}")
    log.info(f"   State: {STATE_FILE}")
    server = HTTPServer(("0.0.0.0", port), DashHandler)
    server.serve_forever()

# ═══════════════════════════════════════════════════════════
# ▶️  ENTRY POINT
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    args = sys.argv[1:]
    init_db()

    if "--dashboard" in args:
        run_dashboard()

    elif "--backtest" in args:
        fd = td = None
        if "--from" in args: fd = args[args.index("--from")+1]
        if "--to"   in args: td = args[args.index("--to")+1]
        if "--days" in args:
            days = int(args[args.index("--days")+1])
            fd = (datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d")
        run_backtest(fd, td)

    elif "--all" in args:
        port = 8080
        if "--port" in args: port = int(args[args.index("--port")+1])
        t = threading.Thread(target=run_dashboard, args=(port,), daemon=True)
        t.start()
        print(f"📊 Dashboard started on port {port}")
        Bot().run()

    else:
        Bot().run()
