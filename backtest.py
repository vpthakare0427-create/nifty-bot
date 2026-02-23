"""
backtest.py — Full backtesting engine
- Fetches REAL Dhan API historical data
- Runs the SAME strategy as the live bot
- Saves results to SQLite (visible in dashboard at /backtest)
- Shows daily breakdown, trade log, equity curve
- Run: python3 backtest.py --days 30
"""

import argparse
import json
import os
import sys
import sqlite3
import time
import math
from datetime import datetime, date, timedelta

import requests
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from strategy import add_indicators, get_signal, synth_premium, \
                     estimate_exit_premium, calc_pnl

BT_DB = os.path.join(config.BASE_DIR, "backtest", "backtest_results.db")


# ══════════════════════════════════════════════════════
# 📡  DATA FETCH (REAL DHAN API)
# ══════════════════════════════════════════════════════

HEADERS = {
    "access-token": config.ACCESS_TOKEN,
    "client-id"   : config.CLIENT_ID,
    "Content-Type": "application/json",
}


def fetch_historical_spot(from_date: str, to_date: str) -> pd.DataFrame | None:
    """Fetch Nifty index 15-min data for any date range."""
    print(f"  📡 Fetching spot {from_date} → {to_date}...")
    try:
        # Dhan intraday API can fetch 30 days at a time
        r = requests.post(
            "https://api.dhan.co/v2/charts/intraday",
            json={
                "securityId"     : "13",
                "exchangeSegment": "IDX_I",
                "instrument"     : "INDEX",
                "interval"       : "15",
                "fromDate"       : from_date,
                "toDate"         : to_date,
            },
            headers=HEADERS, timeout=30
        )
        if r.status_code != 200:
            print(f"  ⚠️  API {r.status_code}: {r.text[:100]}")
            return None

        data = r.json()
        if not data.get("open"):
            print("  ⚠️  Empty response")
            return None

        df = pd.DataFrame({
            "open"     : data["open"],
            "high"     : data["high"],
            "low"      : data["low"],
            "close"    : data["close"],
            "volume"   : data["volume"],
            "timestamp": data["timestamp"],
        })
        df["datetime"] = (
            pd.to_datetime(df["timestamp"], unit="s", utc=True)
            .dt.tz_convert("Asia/Kolkata")
            .dt.tz_localize(None)
        )
        df.set_index("datetime", inplace=True)
        df.drop("timestamp", axis=1, inplace=True)

        # Market hours only
        df = df[
            ((df.index.hour == 9)  & (df.index.minute >= 15)) |
            ((df.index.hour >  9)  & (df.index.hour   <  15)) |
            ((df.index.hour == 15) & (df.index.minute <= 30))
        ].copy()

        print(f"  ✅ Got {len(df)} bars over "
              f"{df.index.date[-1] - df.index.date[0] + timedelta(1)} days")
        return df

    except Exception as e:
        print(f"  ❌ Fetch error: {e}")
        return None


# ══════════════════════════════════════════════════════
# 💾  BACKTEST DB
# ══════════════════════════════════════════════════════

def init_bt_db():
    os.makedirs(os.path.dirname(BT_DB), exist_ok=True)
    with sqlite3.connect(BT_DB) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS bt_runs (
            run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            run_dt      TEXT,
            from_date   TEXT,
            to_date     TEXT,
            total_cap   REAL,
            final_cap   REAL,
            total_pnl   REAL,
            return_pct  REAL,
            n_trades    INTEGER,
            n_wins      INTEGER,
            win_rate    REAL,
            profit_factor REAL,
            max_dd      REAL,
            avg_hold    REAL,
            params      TEXT
        );

        CREATE TABLE IF NOT EXISTS bt_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER,
            unit_id     INTEGER,
            trade_date  TEXT,
            opt_type    TEXT,
            strike      REAL,
            entry_time  TEXT,
            exit_time   TEXT,
            entry_spot  REAL,
            exit_spot   REAL,
            entry_prem  REAL,
            exit_prem   REAL,
            bars_held   INTEGER,
            pnl         REAL,
            exit_reason TEXT,
            cum_pnl     REAL
        );

        CREATE TABLE IF NOT EXISTS bt_daily (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER,
            trade_date  TEXT,
            n_trades    INTEGER,
            n_wins      INTEGER,
            pnl         REAL,
            end_cap     REAL,
            drawdown    REAL
        );

        CREATE TABLE IF NOT EXISTS bt_equity (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER,
            ts          TEXT,
            equity      REAL
        );
        """)


def save_bt_results(run_id: int, trades: list, daily: list, equity: list):
    with sqlite3.connect(BT_DB) as conn:
        cum = 0
        for t in trades:
            cum += t["pnl"]
            conn.execute("""
                INSERT INTO bt_trades
                  (run_id,unit_id,trade_date,opt_type,strike,
                   entry_time,exit_time,entry_spot,exit_spot,
                   entry_prem,exit_prem,bars_held,pnl,exit_reason,cum_pnl)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                run_id, t["unit"], t.get("trade_date",""),
                t["opt_type"], t.get("strike", 0),
                t["entry_time"], t["exit_time"],
                t["entry_spot"], t["exit_spot"],
                t["entry_prem"], t["exit_prem"],
                t["bars_held"], t["pnl"], t["exit_reason"], cum
            ))
        for d in daily:
            conn.execute("""
                INSERT INTO bt_daily
                  (run_id,trade_date,n_trades,n_wins,pnl,end_cap,drawdown)
                VALUES (?,?,?,?,?,?,?)
            """, (run_id, d["date"], d["n"], d["wins"],
                  d["pnl"], d["end_cap"], d["dd"]))
        for e in equity:
            conn.execute("""
                INSERT INTO bt_equity (run_id,ts,equity)
                VALUES (?,?,?)
            """, (run_id, e["ts"], e["eq"]))


# ══════════════════════════════════════════════════════
# 🔄  BACKTEST ENGINE
# ══════════════════════════════════════════════════════

class BacktestUnit:
    def __init__(self, uid: int):
        self.uid      = uid
        self.capital  = float(config.UNIT_SIZE)
        self.trade    = None
        self.streak   = 0
        self.cooldown = 0
        self.last_bar = -99

    @property
    def free(self):
        return self.trade is None and self.cooldown == 0

    def tick(self):
        if self.cooldown > 0:
            self.cooldown -= 1


class Backtester:

    def __init__(self, from_date: str, to_date: str):
        self.from_date  = from_date
        self.to_date    = to_date
        self.units      = [BacktestUnit(i) for i in range(config.NUM_UNITS)]
        self.rr_ptr     = 0
        self.bar_idx    = 0
        self.all_trades : list[dict] = []
        self.daily_stats: list[dict] = []
        self.equity_pts : list[dict] = []
        self.peak_cap   = config.TOTAL_CAPITAL
        self.max_dd     = 0.0

    def total_cap(self) -> float:
        return sum(u.capital for u in self.units)

    def get_unit(self, bar_idx: int) -> BacktestUnit | None:
        for i in range(config.NUM_UNITS):
            uid = (self.rr_ptr + i) % config.NUM_UNITS
            u   = self.units[uid]
            if u.free and bar_idx - u.last_bar >= config.SIGNAL_COOLDOWN:
                self.rr_ptr = (uid + 1) % config.NUM_UNITS
                return u
        return None

    def run(self) -> dict:
        print(f"\n{'='*60}")
        print(f"  BACKTEST: {self.from_date} → {self.to_date}")
        print(f"  Capital: ₹{config.TOTAL_CAPITAL:,} | Strategy: EMA+RSI")
        print(f"  SL:{config.SL_DROP_PCT*100:.0f}% TP:{config.TP_GAIN_PCT*100:.0f}% "
              f"TimeExit:{config.TIME_EXIT_BARS} bars")
        print(f"{'='*60}")

        # Fetch data
        df = fetch_historical_spot(self.from_date, self.to_date)
        if df is None or len(df) < 20:
            print("❌ Not enough data to backtest")
            return {}

        # Add indicators
        df = add_indicators(df)
        df.dropna(subset=["ema9", "ema21", "rsi", "adx"], inplace=True)

        # Group by day
        days       = sorted(df.index.date.tolist())
        trade_days = [d for d in days if
                      pd.Timestamp(d).weekday() < 5 and
                      d not in set(days) or True]
        trade_days = sorted(set([d for d in df.index.date if
                                  pd.Timestamp(d).weekday() < 5]))

        print(f"  Trading days: {len(trade_days)}")
        print()

        prev_row = None

        for day in trade_days:
            day_bars = df[df.index.date == day]
            if len(day_bars) < 5:
                continue

            day_trades = []
            day_start  = self.total_cap()

            for i, (ts, row) in enumerate(day_bars.iterrows()):
                self.bar_idx += 1
                t_hour   = ts.hour
                t_minute = ts.minute

                # Tick cooldowns
                for u in self.units:
                    u.tick()

                eod = (t_hour, t_minute) >= config.HARD_CLOSE

                # ── EXITS ─────────────────────────────────────────
                for u in self.units:
                    if u.trade is None:
                        continue
                    t = u.trade
                    bars_held = self.bar_idx - t["bar_idx"]

                    # Synthetic exit premium (no real option data in backtest)
                    exit_prem = estimate_exit_premium(
                        t["entry_prem"], t["entry_spot"],
                        float(row["close"]), t["opt_type"], bars_held
                    )

                    sl_level = t["entry_prem"] * (1 - config.SL_DROP_PCT)
                    tp_level = t["entry_prem"] * (1 + config.TP_GAIN_PCT)

                    reason = None
                    if bars_held >= config.MIN_HOLD_BARS:
                        if exit_prem <= sl_level:
                            reason = "SL"
                        elif exit_prem >= tp_level:
                            reason = "TP"
                        elif bars_held >= config.TIME_EXIT_BARS:
                            reason = "TIME_EXIT"
                    if reason is None and eod:
                        reason = "EOD"

                    if not reason:
                        continue

                    pnl = calc_pnl(t["entry_prem"], exit_prem,
                                   t["lot_size"], t["qty"])
                    u.capital += pnl
                    u.trade    = None

                    # Update cooldown/streak
                    if pnl < 0:
                        u.streak += 1
                        if u.streak >= config.LOSS_STREAK_MAX:
                            u.cooldown = config.COOLDOWN_BARS
                            u.streak   = 0
                    else:
                        u.streak = 0

                    rec = {
                        "unit"       : u.uid,
                        "trade_date" : str(day),
                        "opt_type"   : t["opt_type"],
                        "strike"     : t.get("strike", 0),
                        "entry_time" : t["entry_time"],
                        "exit_time"  : str(ts),
                        "entry_spot" : t["entry_spot"],
                        "exit_spot"  : float(row["close"]),
                        "entry_prem" : t["entry_prem"],
                        "exit_prem"  : round(exit_prem, 2),
                        "bars_held"  : bars_held,
                        "pnl"        : pnl,
                        "exit_reason": reason,
                        "lot_size"   : t["lot_size"],
                        "qty"        : t["qty"],
                    }
                    self.all_trades.append(rec)
                    day_trades.append(rec)

                # ── ENTRIES ───────────────────────────────────────
                if (config.ENTRY_START <= (t_hour, t_minute) <= config.ENTRY_END
                        and not eod
                        and not self._halted()):

                    sig = get_signal(row, prev_row)
                    if sig:
                        u = self.get_unit(self.bar_idx)
                        if u is not None:
                            spot    = float(row["close"])
                            atm     = round(spot / 50) * 50
                            prem    = synth_premium(spot, dte=5)
                            lot     = float(config.LOT_SIZE)
                            if prem * lot <= u.capital * config.MAX_COST_PCT:
                                u.trade = {
                                    "bar_idx"   : self.bar_idx,
                                    "entry_time": str(ts),
                                    "entry_spot": spot,
                                    "entry_prem": round(prem, 2),
                                    "opt_type"  : sig,
                                    "strike"    : atm,
                                    "lot_size"  : lot,
                                    "qty"       : 1,
                                }
                                u.last_bar = self.bar_idx

                prev_row = row

                # Equity snapshot every bar
                cap = self.total_cap()
                self.equity_pts.append({"ts": str(ts), "eq": cap})

                # Max DD tracking
                self.peak_cap = max(self.peak_cap, cap)
                dd = (self.peak_cap - cap) / self.peak_cap * 100
                self.max_dd = max(self.max_dd, dd)

            # Daily summary
            day_end = self.total_cap()
            day_pnl = day_end - day_start
            wins    = [t for t in day_trades if t["pnl"] > 0]
            dd      = (self.peak_cap - day_end) / self.peak_cap * 100

            self.daily_stats.append({
                "date"    : str(day),
                "n"       : len(day_trades),
                "wins"    : len(wins),
                "pnl"     : round(day_pnl, 2),
                "end_cap" : round(day_end, 2),
                "dd"      : round(dd, 2),
            })

            print(f"  {day}  Trades:{len(day_trades):2d}  "
                  f"Wins:{len(wins):2d}  "
                  f"P&L:₹{day_pnl:+,.0f}  "
                  f"Cap:₹{day_end:,.0f}")

        return self._summarise()

    def _halted(self) -> bool:
        return self.total_cap() < config.TOTAL_CAPITAL * (1 - config.MAX_PORT_DD)

    def _summarise(self) -> dict:
        trades = self.all_trades
        if not trades:
            print("\n⚠️  No trades generated — check signal conditions")
            return {}

        wins  = [t for t in trades if t["pnl"] > 0]
        loss  = [t for t in trades if t["pnl"] <= 0]
        pnl   = sum(t["pnl"] for t in trades)
        gw    = sum(t["pnl"] for t in wins)
        gl    = abs(sum(t["pnl"] for t in loss))
        final = self.total_cap()
        ret   = pnl / config.TOTAL_CAPITAL * 100
        wr    = len(wins) / len(trades) * 100
        pf    = gw / gl if gl else 0
        avg_h = sum(t["bars_held"] for t in trades) / len(trades)

        # Exit breakdown
        exits = {}
        for t in trades:
            r = t["exit_reason"]
            exits[r] = exits.get(r, 0) + 1

        summary = {
            "from_date"    : self.from_date,
            "to_date"      : self.to_date,
            "total_cap"    : config.TOTAL_CAPITAL,
            "final_cap"    : round(final, 0),
            "total_pnl"    : round(pnl, 0),
            "return_pct"   : round(ret, 2),
            "n_trades"     : len(trades),
            "n_wins"       : len(wins),
            "win_rate"     : round(wr, 1),
            "profit_factor": round(pf, 2),
            "max_dd"       : round(self.max_dd, 2),
            "avg_hold_bars": round(avg_h, 1),
            "avg_win"      : round(gw / len(wins), 0) if wins else 0,
            "avg_loss"     : round(-gl / len(loss), 0) if loss else 0,
            "exits"        : exits,
            "daily"        : self.daily_stats,
        }

        # ── Print Results ──────────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"  BACKTEST RESULTS")
        print(f"{'='*60}")
        print(f"  Period      : {self.from_date} → {self.to_date}")
        print(f"  Capital     : ₹{config.TOTAL_CAPITAL:,.0f} → ₹{final:,.0f}")
        print(f"  Total P&L   : ₹{pnl:+,.0f}  ({ret:+.2f}%)")
        print(f"  Trades      : {len(trades)}")
        print(f"  Win Rate    : {wr:.1f}%  (break-even {(config.SL_DROP_PCT/(config.SL_DROP_PCT+config.TP_GAIN_PCT)*100):.1f}%)")
        print(f"  Profit Fac  : {pf:.2f}")
        print(f"  Max DD      : {self.max_dd:.1f}%")
        print(f"  Avg Win     : ₹{summary['avg_win']:,.0f}")
        print(f"  Avg Loss    : ₹{summary['avg_loss']:,.0f}")
        print(f"  Avg Hold    : {avg_h:.1f} bars ({avg_h*15:.0f} min)")
        print(f"\n  Exit Breakdown:")
        for r, n in sorted(exits.items(), key=lambda x: -x[1]):
            pct_ = n / len(trades) * 100
            print(f"    {r:<12} {n:3d} trades ({pct_:.1f}%)")
        print(f"\n  Trades/day  : {len(trades)/max(len(self.daily_stats),1):.1f}")

        go = (wr >= 35 and pf >= 1.5 and ret > 20 and len(trades) >= 20)
        print(f"\n  {'🟢 LOOKS TRADEABLE' if go else '🔴 NOT READY — needs more optimization'}")
        print(f"{'='*60}")

        return summary


# ══════════════════════════════════════════════════════
# 🌐  BACKTEST DASHBOARD (same port 8081)
# ══════════════════════════════════════════════════════

def get_bt_data() -> dict:
    """Read latest backtest run from DB for dashboard."""
    if not os.path.exists(BT_DB):
        return {"error": "No backtest run yet. Run: python3 backtest.py"}
    with sqlite3.connect(BT_DB) as conn:
        conn.row_factory = sqlite3.Row
        # Get latest run
        run = conn.execute(
            "SELECT * FROM bt_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        if not run:
            return {"error": "No backtest data yet"}
        run    = dict(run)
        rid    = run["run_id"]
        trades = [dict(r) for r in
                  conn.execute("SELECT * FROM bt_trades WHERE run_id=? ORDER BY entry_time", (rid,)).fetchall()]
        daily  = [dict(r) for r in
                  conn.execute("SELECT * FROM bt_daily WHERE run_id=? ORDER BY trade_date", (rid,)).fetchall()]
        equity = [dict(r) for r in
                  conn.execute("SELECT * FROM bt_equity WHERE run_id=? ORDER BY ts", (rid,)).fetchall()]

        # All runs for comparison
        all_runs = [dict(r) for r in
                    conn.execute("SELECT run_id,run_dt,from_date,to_date,return_pct,win_rate,profit_factor,n_trades FROM bt_runs ORDER BY run_id DESC LIMIT 10").fetchall()]

    return {
        "run"     : run,
        "trades"  : trades[-30:][::-1],
        "daily"   : daily,
        "equity"  : equity[-200:],
        "all_runs": all_runs,
    }


def render_bt_html(d: dict) -> str:
    if "error" in d:
        return f"""<html><body style="background:#0a0e1a;color:#e2e8f0;padding:40px;font-family:monospace">
        <h2>⚠️ {d['error']}</h2>
        <p style="color:#6b7280">Run backtest first:</p>
        <pre style="background:#111827;padding:20px;border-radius:8px">python3 backtest.py --days 30</pre>
        <p><a href="/">← Back to Live Dashboard</a></p>
        </body></html>"""

    run    = d["run"]
    trades = d["trades"]
    daily  = d["daily"]
    equity = d["equity"]

    ret_col = "#22c55e" if run.get("return_pct", 0) >= 0 else "#ef4444"

    # Trade rows
    trows = ""
    for t in trades:
        pnl = t.get("pnl", 0)
        pc_ = "#22c55e" if pnl >= 0 else "#ef4444"
        ot  = t.get("opt_type", "-")
        oc  = "#22c55e" if ot == "CE" else "#ef4444"
        rc  = {"TP":"#22c55e","SL":"#ef4444","TIME_EXIT":"#f59e0b","EOD":"#6b7280"}.get(
               t.get("exit_reason","-"), "#9ca3af")
        trows += f"""<tr>
          <td>{str(t.get('trade_date',''))}</td>
          <td style="color:{oc};font-weight:700">{ot}</td>
          <td>₹{t.get('entry_prem',0):.0f}→₹{t.get('exit_prem',0):.0f}</td>
          <td style="color:{rc}">{t.get('exit_reason','-')}</td>
          <td>{t.get('bars_held',0)}b</td>
          <td style="color:{pc_};font-weight:700">{'+' if pnl>=0 else ''}₹{abs(pnl):,.0f}</td>
          <td style="color:#6b7280">₹{t.get('cum_pnl',0):+,.0f}</td>
        </tr>"""

    # Equity chart
    eq_ts  = [e["ts"][5:10] for e in equity[::max(len(equity)//100,1)]]
    eq_val = [round(e["equity"],0) for e in equity[::max(len(equity)//100,1)]]

    # Daily chart
    d_dt   = [d_["trade_date"][5:] for d_ in daily]
    d_pnl  = [round(d_["pnl"],0) for d_ in daily]
    d_n    = [d_["n_trades"] for d_ in daily]

    params = json.dumps({"eq_ts":eq_ts,"eq_val":eq_val,
                          "d_dt":d_dt,"d_pnl":d_pnl,"d_n":d_n})

    # All runs table
    run_rows = ""
    for r in d.get("all_runs", []):
        rc = "#22c55e" if r.get("return_pct",0) >= 0 else "#ef4444"
        run_rows += f"""<tr>
          <td>#{r['run_id']}</td>
          <td style="font-size:11px">{r.get('run_dt','')[:16]}</td>
          <td>{r.get('from_date','')} → {r.get('to_date','')}</td>
          <td style="color:{rc};font-weight:700">{r.get('return_pct',0):+.1f}%</td>
          <td>{r.get('win_rate',0):.1f}%</td>
          <td>{r.get('profit_factor',0):.2f}</td>
          <td>{r.get('n_trades',0)}</td>
        </tr>"""

    go = (run.get("win_rate",0) >= 35 and run.get("profit_factor",0) >= 1.5
          and run.get("return_pct",0) > 20)
    vc = "#22c55e" if go else "#ef4444"
    vt = "🟢 STRATEGY LOOKS TRADEABLE" if go else "🔴 NEEDS OPTIMIZATION"

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest Results</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0e1a;color:#e2e8f0;font-family:'Segoe UI',system-ui;font-size:14px}}
.hdr{{background:#111827;padding:12px 20px;border-bottom:2px solid #1e2433;
      display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}}
.hdr h1{{font-size:16px;font-weight:700;color:#a78bfa}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;padding:14px}}
.kpi{{background:#111827;border:1px solid #1e2433;border-radius:10px;padding:14px}}
.kpi .lbl{{font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.6px}}
.kpi .val{{font-size:22px;font-weight:700;margin:4px 0}}
.kpi .sub{{font-size:11px;color:#9ca3af}}
.section{{padding:0 14px 14px}}
.section h2{{font-size:12px;font-weight:600;color:#6b7280;text-transform:uppercase;
             letter-spacing:.6px;border-top:1px solid #1e2433;padding-top:12px;margin-bottom:8px}}
table{{width:100%;border-collapse:collapse;background:#111827;border-radius:10px;overflow:hidden;font-size:12px}}
th{{background:#1e2433;padding:8px 10px;text-align:left;font-size:10px;color:#6b7280;text-transform:uppercase}}
td{{padding:8px 10px;border-bottom:1px solid #1e2433}}
tr:last-child td{{border-bottom:none}}
.chart-box{{background:#111827;border:1px solid #1e2433;border-radius:10px;padding:14px;margin-bottom:10px}}
.verdict{{text-align:center;font-size:16px;font-weight:700;padding:14px;background:#1e2433;border-radius:8px;margin:10px 0}}
</style>
</head><body>
<div class="hdr">
  <h1>🔬 Backtest Results — Run #{run.get('run_id','?')}</h1>
  <div style="font-size:11px;color:#6b7280">
    {run.get('from_date','')} → {run.get('to_date','')}<br>
    {run.get('run_dt','')[:16]}<br>
    <a href="/" style="color:#3b82f6">← Live Dashboard</a>
  </div>
</div>

<div class="grid">
  <div class="kpi">
    <div class="lbl">Return</div>
    <div class="val" style="color:{ret_col}">{run.get('return_pct',0):+.2f}%</div>
    <div class="sub">₹{config.TOTAL_CAPITAL:,} → ₹{run.get('final_cap',0):,.0f}</div>
  </div>
  <div class="kpi">
    <div class="lbl">Total P&L</div>
    <div class="val" style="color:{ret_col}">₹{run.get('total_pnl',0):+,.0f}</div>
    <div class="sub">{run.get('n_trades',0)} trades</div>
  </div>
  <div class="kpi">
    <div class="lbl">Win Rate</div>
    <div class="val">{run.get('win_rate',0):.1f}%</div>
    <div class="sub">{run.get('n_wins',0)}/{run.get('n_trades',0)} wins</div>
  </div>
  <div class="kpi">
    <div class="lbl">Profit Factor</div>
    <div class="val" style="color:{'#22c55e' if run.get('profit_factor',0)>=1.5 else '#ef4444'}">{run.get('profit_factor',0):.2f}</div>
    <div class="sub">Target ≥ 1.5</div>
  </div>
  <div class="kpi">
    <div class="lbl">Max Drawdown</div>
    <div class="val" style="color:{'#22c55e' if run.get('max_dd',0)<12 else '#ef4444'}">{run.get('max_dd',0):.1f}%</div>
    <div class="sub">Target &lt; 12%</div>
  </div>
  <div class="kpi">
    <div class="lbl">Avg Hold</div>
    <div class="val">{run.get('avg_hold',0):.1f} bars</div>
    <div class="sub">{run.get('avg_hold',0)*15:.0f} minutes</div>
  </div>
</div>

<div class="section">
  <div class="verdict" style="color:{vc}">{vt}</div>
</div>

<div class="section">
  <h2>📈 Equity Curve</h2>
  <div class="chart-box">
    <canvas id="eqChart" height="70"></canvas>
  </div>
</div>

<div class="section">
  <h2>📅 Daily P&L</h2>
  <div class="chart-box">
    <canvas id="dChart" height="60"></canvas>
  </div>
</div>

<div class="section">
  <h2>📋 Recent Trades (last 30)</h2>
  <table>
    <tr><th>Date</th><th>Type</th><th>Prem</th><th>Exit</th><th>Hold</th><th>P&L</th><th>Cum P&L</th></tr>
    {trows}
  </table>
</div>

<div class="section">
  <h2>📊 All Backtest Runs</h2>
  <table>
    <tr><th>Run</th><th>Ran At</th><th>Period</th><th>Return</th><th>WR</th><th>PF</th><th>Trades</th></tr>
    {run_rows}
  </table>
</div>

<script>
const P = {params};
const opts = {{responsive:true,animation:false,
  plugins:{{legend:{{labels:{{color:'#9ca3af',font:{{size:10}}}}}}}},
  scales:{{x:{{ticks:{{color:'#6b7280',font:{{size:9}},maxTicksLimit:12}},grid:{{color:'#1e2433'}}}},
           y:{{ticks:{{color:'#6b7280',font:{{size:9}}}},grid:{{color:'#1e2433'}}}}}}}};

if(P.eq_val.length) {{
  const last = P.eq_val[P.eq_val.length-1];
  const col  = last >= {config.TOTAL_CAPITAL} ? '#22c55e' : '#ef4444';
  new Chart(document.getElementById('eqChart'), {{
    type:'line',
    data:{{labels:P.eq_ts,datasets:[{{label:'Portfolio ₹',data:P.eq_val,
      borderColor:col,borderWidth:2,pointRadius:0,tension:0.2,
      fill:true,backgroundColor:col+'18'}}]}},
    options:opts
  }});
}}

if(P.d_pnl.length) {{
  new Chart(document.getElementById('dChart'), {{
    type:'bar',
    data:{{labels:P.d_dt,datasets:[{{label:'Daily P&L',data:P.d_pnl,
      backgroundColor:P.d_pnl.map(v=>v>=0?'#22c55e44':'#ef444444'),
      borderColor:P.d_pnl.map(v=>v>=0?'#22c55e':'#ef4444'),borderWidth:1}}]}},
    options:opts
  }});
}}
</script>
</body></html>"""


# ══════════════════════════════════════════════════════
# ▶️  CLI RUNNER
# ══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Nifty Options Backtester")
    parser.add_argument("--days",  type=int, default=30,
                        help="Days to backtest (default 30)")
    parser.add_argument("--from",  dest="from_date", default=None,
                        help="From date YYYY-MM-DD")
    parser.add_argument("--to",    dest="to_date",   default=None,
                        help="To date YYYY-MM-DD")
    parser.add_argument("--serve", action="store_true",
                        help="Serve backtest dashboard on port 8081")
    args = parser.parse_args()

    if args.serve:
        # Serve backtest dashboard only
        from http.server import HTTPServer, BaseHTTPRequestHandler
        class BtHandler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                data = get_bt_data()
                body = render_bt_html(data).encode()
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
        print("🔬 Backtest dashboard at http://0.0.0.0:8081")
        HTTPServer(("0.0.0.0", 8081), BtHandler).serve_forever()
        return

    # Determine date range
    if args.to_date:
        to_d   = datetime.strptime(args.to_date, "%Y-%m-%d").date()
    else:
        to_d   = date.today() - timedelta(days=1)  # yesterday

    if args.from_date:
        from_d = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    else:
        from_d = to_d - timedelta(days=args.days)

    from_str = from_d.strftime("%Y-%m-%d")
    to_str   = to_d.strftime("%Y-%m-%d")

    # Run backtest
    init_bt_db()
    bt     = Backtester(from_str, to_str)
    result = bt.run()

    if not result:
        sys.exit(1)

    # Save to DB
    with sqlite3.connect(BT_DB) as conn:
        cursor = conn.execute("""
            INSERT INTO bt_runs
              (run_dt,from_date,to_date,total_cap,final_cap,total_pnl,
               return_pct,n_trades,n_wins,win_rate,profit_factor,
               max_dd,avg_hold,params)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now().isoformat(),
            from_str, to_str,
            config.TOTAL_CAPITAL, result["final_cap"], result["total_pnl"],
            result["return_pct"], result["n_trades"], result["n_wins"],
            result["win_rate"], result["profit_factor"],
            result["max_dd"], result["avg_hold_bars"],
            json.dumps({"SL": config.SL_DROP_PCT,
                        "TP": config.TP_GAIN_PCT,
                        "TIME": config.TIME_EXIT_BARS,
                        "ADX": config.ADX_MIN}),
        ))
        run_id = cursor.lastrowid

    save_bt_results(run_id, bt.all_trades, bt.daily_stats, bt.equity_pts)
    print(f"\n✅ Saved as Run #{run_id}")
    print(f"   View at http://YOUR_SERVER_IP:8081")
    print(f"   Or run: python3 backtest.py --serve")

    # Also save JSON summary
    json_path = os.path.join(config.BASE_DIR, "backtest",
                             f"backtest_{from_str}_{to_str}.json")
    with open(json_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"   JSON: {json_path}")


if __name__ == "__main__":
    main()
