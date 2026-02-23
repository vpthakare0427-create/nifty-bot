"""alerts.py — Telegram notifications (optional)"""
import requests, logging, config

logger = logging.getLogger("alerts")

def _send(msg: str):
    if not config.TELEGRAM_ENABLED or not config.TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": msg,
                  "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        logger.debug(f"Telegram: {e}")

def alert_bot_start():
    _send(f"🤖 <b>Nifty Bot v2 Started</b>\nCapital: ₹{config.TOTAL_CAPITAL:,}\nUnits: {config.NUM_UNITS}")

def alert_trade_entry(uid, ot, sym, strike, qty, prem, spot):
    col = "🟢" if ot=="CE" else "🔴"
    _send(f"{col} <b>ENTRY</b> Unit {uid}\n{ot} {sym}\nStrike:{strike:.0f} Prem:₹{prem:.1f}\nSpot:{spot:.0f}")

def alert_trade_exit(uid, ot, sym, pnl, reason, ep, xp, qty):
    ic  = "✅" if pnl >= 0 else "❌"
    _send(f"{ic} <b>EXIT [{reason}]</b> Unit {uid}\n{ot} {sym}\n₹{ep:.1f}→₹{xp:.1f}  P&L: ₹{pnl:+,.0f}")

def alert_daily_summary(dt, start, end, trades):
    n    = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    pnl  = end - start
    _send(f"📊 <b>EOD {dt}</b>\nTrades:{n} Wins:{wins}\nP&L:₹{pnl:+,.0f}\nCap:₹{end:,.0f}")

def alert_risk_breach(reason, cap):
    _send(f"🚨 <b>RISK BREACH</b>\n{reason}\nCap:₹{cap:,.0f}")

def alert_error(msg: str):
    _send(f"⚠️ <b>Bot Error</b>\n{msg[:200]}")
