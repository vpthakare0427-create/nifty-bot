"""
bot.py — Nifty Paper Trading Bot v2
FIXES:
1. TIME_EXIT now actually triggers (checked BEFORE EOD)
2. Day P&L correctly shown in logs
3. All data written to SQLite (dashboard reads from there)
4. Simpler signal logic = 2-3 trades/day
5. Portfolio halt at -10% DD, day halt at -5%
"""

import time
import logging
import os
import sys
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict

import config
import database as db
from strategy import add_indicators, get_signal, synth_premium, \
                     estimate_exit_premium, calc_pnl
from data import fetch_spot, ContractSelector, PriceCache
from units import UnitManager
import alerts


# ══════════════════════════════════════════════════════
# 📝  LOGGING
# ══════════════════════════════════════════════════════

def setup_logging():
    log_file = os.path.join(config.LOG_DIR,
                            f"bot_{date.today().isoformat()}.log")
    fmt = "%(asctime)s IST | %(levelname)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO, format=fmt,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ]
    )
    # Silence noisy libs
    for lib in ["urllib3", "requests", "charset_normalizer"]:
        logging.getLogger(lib).setLevel(logging.WARNING)
    return logging.getLogger("bot")


# ══════════════════════════════════════════════════════
# ⏰  IST TIME HELPERS
# ══════════════════════════════════════════════════════

def now_ist() -> datetime:
    return datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo", fromlist=["ZoneInfo"]).ZoneInfo("Asia/Kolkata")
    ).replace(tzinfo=None)


def is_weekday(dt: datetime = None) -> bool:
    return (dt or now_ist()).weekday() < 5


def time_between(dt: datetime, start: tuple, end: tuple) -> bool:
    t = (dt.hour, dt.minute)
    return start <= t <= end


def is_market_open(dt=None) -> bool:
    dt = dt or now_ist()
    return is_weekday(dt) and time_between(dt, config.MARKET_OPEN, config.MARKET_CLOSE)


def is_entry_window(dt=None) -> bool:
    dt = dt or now_ist()
    return time_between(dt, config.ENTRY_START, config.ENTRY_END)


def is_hard_close(dt=None) -> bool:
    dt = dt or now_ist()
    t  = (dt.hour, dt.minute)
    return t >= config.HARD_CLOSE


def secs_to_next_bar(dt: datetime = None) -> int:
    dt     = dt or now_ist()
    mins   = dt.minute % 15
    secs   = (15 - mins) * 60 - dt.second
    return max(secs, 0)


def secs_to_open() -> int:
    now = now_ist()
    if not is_weekday(now):
        days  = 7 - now.weekday()
        nxt   = (now + timedelta(days=days)).replace(
                    hour=9, minute=15, second=5, microsecond=0)
        return int((nxt - now).total_seconds())
    opener = now.replace(hour=9, minute=15, second=5, microsecond=0)
    if now < opener:
        return int((opener - now).total_seconds())
    return 0


# ══════════════════════════════════════════════════════
# 🤖  BOT
# ══════════════════════════════════════════════════════

class NiftyBot:

    def __init__(self):
        self.log = setup_logging()
        self.log.info("=" * 60)
        self.log.info("  NIFTY OPTIONS PAPER BOT v2 — Starting")
        self.log.info("=" * 60)

        db.init_db()
        self.um           = UnitManager()
        self.cs           = ContractSelector()
        self.pc           = PriceCache()
        self.bar_idx      = 0
        self.prev_row     = None       # previous candle (for RSI direction)
        self.last_bar_ts  = None
        self.today_start  = date.today()
        self.start_cap    = self.um.total_capital()
        self.today_trades : list[dict] = []
        self._running     = True

        # Publish initial status to DB
        db.update_portfolio(self.start_cap, 0, 0)
        db.update_unit_status(self.um.units)

        alerts.alert_bot_start()
        self.log.info(f"Capital: ₹{self.start_cap:,.0f}  "
                      f"Units: {config.NUM_UNITS} × ₹{config.UNIT_SIZE:,}")
        self.log.info(f"SL:{config.SL_DROP_PCT*100:.0f}%  "
                      f"TP:{config.TP_GAIN_PCT*100:.0f}%  "
                      f"TimeExit:{config.TIME_EXIT_BARS} bars")

    # ── Main loop ──────────────────────────────────────────────────

    def run(self):
        self.log.info("🟢 Bot running — Ctrl+C to stop")
        while self._running:
            try:
                now = now_ist()
                if not is_market_open(now):
                    secs = secs_to_open()
                    if secs > 300:
                        self.log.info(f"💤 Market closed — sleeping "
                                      f"{secs//3600}h {(secs%3600)//60}m")
                        time.sleep(min(secs - 120, 3600))
                    else:
                        time.sleep(max(secs, 30))
                    continue

                # Wait for bar boundary
                wait = secs_to_next_bar(now)
                if wait > 12:
                    time.sleep(wait - 8)
                    continue

                time.sleep(9)  # let bar fully close on exchange
                self._on_bar()

            except KeyboardInterrupt:
                self.log.info("⛔ Shutdown")
                self._shutdown()
                break
            except Exception as e:
                self.log.error(f"Loop error: {e}", exc_info=True)
                alerts.alert_error(str(e))
                time.sleep(60)

    # ── Per-bar handler ────────────────────────────────────────────

    def _on_bar(self):
        now = now_ist()

        # Fetch spot data
        spot_df = fetch_spot(days_back=2)
        if spot_df is None or len(spot_df) < 20:
            self.log.warning("No/insufficient spot data")
            return

        spot_df = add_indicators(spot_df)
        spot_df.dropna(subset=["ema9", "ema21", "rsi", "adx"], inplace=True)
        if len(spot_df) == 0:
            return

        row = spot_df.iloc[-1]
        ts  = spot_df.index[-1]
        day = ts.date()

        # ── Day rollover ───────────────────────────────────────────
        if self.last_bar_ts and self.last_bar_ts.date() != day:
            self._end_of_day(self.last_bar_ts.date())
            self.start_cap    = self.um.total_capital()
            self.today_trades = []
            self.prev_row     = None
            self.cs.clear_cache()
            db.clear_today_open_positions()

        self.last_bar_ts = ts
        self.bar_idx    += 1

        # Log candle to DB (dashboard charts)
        row_with_signal = row.copy()
        db.log_candle(row_with_signal)

        # Tick cooldowns
        self.um.tick_all()

        self.log.info(
            f"🕯️  Bar [{ts.strftime('%H:%M')}]  "
            f"Spot={row['close']:.0f}  "
            f"ADX={row['adx']:.1f}  RSI={row['rsi']:.1f}  "
            f"EMA9={row['ema9']:.0f} EMA21={row['ema21']:.0f}"
        )

        # ── EXITS (always run, even at EOD) ───────────────────────
        eod = is_hard_close(now)
        for u in self.um.units:
            if u.trade is not None:
                self._check_exit(u, row, ts, day, eod)

        # ── Portfolio guards ───────────────────────────────────────
        if self.um.is_halted():
            self.log.warning(
                f"🚨 Portfolio DD > {config.MAX_PORT_DD*100:.0f}% — HALTED")
            alerts.alert_risk_breach(
                f"DD > {config.MAX_PORT_DD*100:.0f}%",
                self.um.total_capital())
        elif self.um.day_loss_breach(day):
            self.log.warning(
                f"⚠️  Day loss > {config.MAX_PORT_DAY_LOSS*100:.0f}% — "
                f"no new entries today")
        elif is_entry_window(now) and not eod:
            # ── ENTRIES ───────────────────────────────────────────
            sig = get_signal(row, self.prev_row)
            if sig:
                self._try_entry(sig, row, ts, day)

        self.prev_row = row

        # ── Update dashboard DB ────────────────────────────────────
        cap     = self.um.total_capital()
        dpnl    = self.um.day_pnl_total(day)
        n_open  = self.um.active_count()
        db.update_portfolio(cap, dpnl, n_open)
        db.update_unit_status(self.um.units)
        db.log_equity(ts, cap)
        self.um.save()

        self.log.info(
            f"💼 Portfolio: ₹{cap:,.0f}  "
            f"Day P&L: ₹{dpnl:+,.0f}  "
            f"Open: {n_open}/{config.NUM_UNITS}"
        )

    # ── Exit logic ─────────────────────────────────────────────────

    def _check_exit(self, u, row, ts, day, eod: bool):
        t         = u.trade
        bars_held = self.bar_idx - t.get("bar_idx", 0)
        reason    = None

        # Get current option price
        cur_prem = self.pc.get_price(t.get("sid"), ts)
        live     = cur_prem is not None
        if not live:
            cur_prem = estimate_exit_premium(
                t["entry_prem"], t["entry_spot"],
                float(row["close"]), t["opt_type"], bars_held
            )

        # Update peak premium
        t["peak"] = max(t.get("peak", t["entry_prem"]), cur_prem)

        sl_level = t["entry_prem"] * (1 - config.SL_DROP_PCT)
        tp_level = t["entry_prem"] * (1 + config.TP_GAIN_PCT)

        # ══ EXIT PRIORITY (FIXED ORDER) ══════════════════════
        # 1. SL hit (most important — protect capital)
        # 2. TP hit (take profit)
        # 3. TIME_EXIT (stuck position — free the unit)
        # 4. EOD (cleanup)
        # This fixes the v1 bug where EOD was checked first,
        # preventing TIME_EXIT from ever triggering.
        # ════════════════════════════════════════════════════
        if bars_held >= config.MIN_HOLD_BARS:
            if cur_prem <= sl_level:
                reason = "SL"
            elif cur_prem >= tp_level:
                reason = "TP"
            elif bars_held >= config.TIME_EXIT_BARS:
                reason = "TIME_EXIT"   # ← was broken in v1
        if reason is None and eod:
            reason = "EOD"

        if not reason:
            return

        pnl = calc_pnl(t["entry_prem"], cur_prem, t["lot_size"], t["qty"])
        u.close(pnl, day)

        trade_rec = {
            "unit_id"    : u.uid,
            "opt_type"   : t["opt_type"],
            "symbol"     : t["symbol"],
            "strike"     : t["strike"],
            "lot_size"   : t["lot_size"],
            "qty"        : t["qty"],
            "entry_time" : t["entry_time"],
            "exit_time"  : ts.isoformat(),
            "entry_spot" : t["entry_spot"],
            "exit_spot"  : float(row["close"]),
            "entry_prem" : t["entry_prem"],
            "exit_prem"  : round(cur_prem, 2),
            "bars_held"  : bars_held,
            "pnl"        : pnl,
            "exit_reason": reason,
            "live_data"  : int(live),
        }
        db.log_trade(trade_rec)
        db.remove_open_position(u.uid)
        self.today_trades.append(trade_rec)

        icon = "✅" if pnl >= 0 else "❌"
        pct  = (cur_prem - t["entry_prem"]) / t["entry_prem"] * 100
        self.log.info(
            f"{icon} EXIT [{reason}] Unit {u.uid} | {t['opt_type']}  "
            f"Prem {t['entry_prem']:.1f}→{cur_prem:.1f} ({pct:+.1f}%)  "
            f"P&L=₹{pnl:+,.0f}  bars={bars_held}"
        )
        alerts.alert_trade_exit(u.uid, t["opt_type"], t["symbol"],
                                pnl, reason, t["entry_prem"], cur_prem, t["qty"])

    # ── Entry logic ────────────────────────────────────────────────

    def _try_entry(self, direction: str, row, ts, day):
        u = self.um.get_next_free(day, self.bar_idx)
        if u is None:
            self.log.debug("No free unit")
            return

        contract = self.cs.get(float(row["close"]), direction, day)
        sid      = contract["sid"]
        lot_size = contract["lot_size"]
        dte      = contract.get("dte", 5)

        # Load option data if needed
        if sid and not self.pc.is_loaded(sid):
            self.pc.load(sid)

        # Entry premium
        entry_prem = self.pc.get_price(sid, ts)
        live_entry = entry_prem is not None
        if not live_entry or entry_prem <= 0:
            from strategy import synth_premium
            entry_prem = synth_premium(float(row["close"]), dte)

        if entry_prem <= 0:
            return

        cost_1lot = entry_prem * lot_size
        if cost_1lot > u.capital:
            self.log.info(
                f"⛔ Signal {direction} blocked — "
                f"cost ₹{cost_1lot:,.0f} > unit ₹{u.capital:,.0f}")
            return

        qty = config.MAX_LOTS  # always 1 lot (config default)

        trade_dict = {
            "bar_idx"   : self.bar_idx,
            "entry_time": ts.isoformat(),
            "entry_spot": float(row["close"]),
            "entry_prem": round(entry_prem, 2),
            "sid"       : sid,
            "symbol"    : contract["symbol"],
            "strike"    : contract["strike"],
            "opt_type"  : direction,
            "qty"       : qty,
            "lot_size"  : lot_size,
            "peak"      : entry_prem,
        }
        u.enter(trade_dict, self.bar_idx)
        db.upsert_open_position(u.uid, trade_dict)

        cost = entry_prem * lot_size * qty
        self.log.info(
            f"📥 ENTER Unit {u.uid} | {direction} {contract['symbol']}  "
            f"Strike={contract['strike']:.0f}  Prem=₹{entry_prem:.1f}  "
            f"Cost=₹{cost:,.0f}  {'LIVE' if live_entry else 'SYNTH'}"
        )
        alerts.alert_trade_entry(u.uid, direction, contract["symbol"],
                                 contract["strike"], qty, entry_prem,
                                 float(row["close"]))

    # ── End of day ─────────────────────────────────────────────────

    def _end_of_day(self, eod_date: date):
        self.log.info(f"=== EOD: {eod_date} ===")
        end_cap = self.um.total_capital()
        pnl     = end_cap - self.start_cap
        n       = len(self.today_trades)
        wins    = sum(1 for t in self.today_trades if t["pnl"] > 0)
        wr      = wins / n * 100 if n else 0

        self.log.info(
            f"  Trades: {n}  Wins: {wins}  WR: {wr:.1f}%  "
            f"P&L: ₹{pnl:+,.0f}  Capital: ₹{end_cap:,.0f}")

        db.save_daily_summary(str(eod_date), self.start_cap,
                              end_cap, self.today_trades)
        db.clear_today_open_positions()

        # Try to generate reports (if matplotlib available)
        try:
            import reports
            reports.generate_daily(str(eod_date), self.um.units, self.today_trades)
        except Exception as e:
            self.log.warning(f"Report gen failed: {e}")

        alerts.alert_daily_summary(str(eod_date), self.start_cap,
                                   end_cap, self.today_trades)

    def _shutdown(self):
        self.log.info("Saving state...")
        self.um.save()
        self.log.info("Done")


# ══════════════════════════════════════════════════════
# ▶️  ENTRY POINT
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    bot = NiftyBot()
    bot.run()
