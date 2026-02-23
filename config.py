"""
config.py — Nifty Bot v2 Configuration
TARGET: 2-3 trades per day, manage risk, no negative month-end
"""

import os

# ══════════════════════════════════════════════════════
# 🔑  DHAN API CREDENTIALS
# ══════════════════════════════════════════════════════
CLIENT_ID    = "1106812224"
ACCESS_TOKEN = ("eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzcxOTU3NDQ0LCJpYXQiOjE3NzE4NzEwNDQsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTA2ODEyMjI0In0.XadDHGUz8AS5HEXL09k2uxyWmMgkdrX6AgwajN00I6znuwTw75pUVRZ1RMQOTJIlN_on55uux0Td9-IKzzeBMw")
# ══════════════════════════════════════════════════════
# 📱  TELEGRAM ALERTS
# ══════════════════════════════════════════════════════
TELEGRAM_TOKEN   = ""
TELEGRAM_CHAT_ID = ""
TELEGRAM_ENABLED = False

# ══════════════════════════════════════════════════════
# 💰  CAPITAL
# ══════════════════════════════════════════════════════
TOTAL_CAPITAL  = 100_000
NUM_UNITS      = 5
UNIT_SIZE      = 20_000

# ══════════════════════════════════════════════════════
# ⏱️  MARKET HOURS (IST as tuples)
# ══════════════════════════════════════════════════════
INTERVAL       = "15"
MARKET_OPEN    = (9,  15)
MARKET_CLOSE   = (15, 30)
ENTRY_START    = (9,  30)    # wait 1 bar for confirmation
ENTRY_END      = (14, 15)    # stop new entries at 14:15 (was 14:30)
HARD_CLOSE     = (15, 10)    # force close all positions

# ══════════════════════════════════════════════════════
# 📐  SIMPLE SIGNAL — EMA + RSI only
#     TARGET: 2-3 signals per day (was getting only 1)
# ══════════════════════════════════════════════════════
EMA_FAST       = 9
EMA_SLOW       = 21
RSI_LEN        = 14
ADX_LEN        = 14

# SIGNAL THRESHOLDS (LOOSENED for more trades)
ADX_MIN        = 18          # was 20 — fires more often
RSI_BUY        = 55          # CE when RSI > 55 (was complex range)
RSI_SELL       = 45          # PE when RSI < 45 (simple)
MIN_CONFIRMS   = 2           # only 2 confirmations needed (was 2 but with harder conditions)
SIGNAL_COOLDOWN = 4          # bars between signals on same unit

# ══════════════════════════════════════════════════════
# 💹  POSITION SIZING
# ══════════════════════════════════════════════════════
MAX_COST_PCT   = 0.55
MAX_LOTS       = 1           # always 1 lot — keep it simple
LOT_SIZE       = 75          # Nifty lot size updated

# ══════════════════════════════════════════════════════
# 🚪  EXIT RULES (FIXED from v1 analysis)
#
#   BUG FIX: TIME_EXIT was not triggering because EOD
#   was checked BEFORE bars_held check.
#   Now: SL → TP → TIME_EXIT → EOD (in that priority)
#
#   SL  : -25% of entry premium
#   TP  : +50% of entry premium
#   TIME: exit after 6 bars (90 min) if stuck
# ══════════════════════════════════════════════════════
SL_DROP_PCT    = 0.25        # exit if option drops 25%
TP_GAIN_PCT    = 0.50        # exit if option gains 50%
TIME_EXIT_BARS = 6           # max hold: 6 bars = 90 minutes
MIN_HOLD_BARS  = 1           # must hold at least 1 bar

# ══════════════════════════════════════════════════════
# 🛡️  RISK MANAGEMENT
# ══════════════════════════════════════════════════════
MAX_PORT_DD        = 0.10    # halt all at -10% portfolio DD
MAX_UNIT_DAY_LOSS  = 0.15    # pause unit at -15% day loss
MAX_PORT_DAY_LOSS  = 0.05    # halt all at -5% portfolio day loss
LOSS_STREAK_MAX    = 2       # cooldown after 2 losses
COOLDOWN_BARS      = 4

# ══════════════════════════════════════════════════════
# 📁  PATHS
# ══════════════════════════════════════════════════════
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
STATE_FILE  = os.path.join(BASE_DIR, "state", "bot_state.json")
DB_FILE     = os.path.join(BASE_DIR, "data", "trades.db")
LOG_DIR     = os.path.join(BASE_DIR, "logs")
REPORT_DIR  = os.path.join(BASE_DIR, "reports")
SCRIP_FILE  = os.path.join(BASE_DIR, "api-scrip-master.csv")

for d in [os.path.join(BASE_DIR, "state"), os.path.join(BASE_DIR, "data"),
          LOG_DIR, REPORT_DIR,
          os.path.join(REPORT_DIR, "daily"),
          os.path.join(REPORT_DIR, "weekly"),
          os.path.join(REPORT_DIR, "monthly"),
          os.path.join(BASE_DIR, "backtest")]:
    os.makedirs(d, exist_ok=True)
