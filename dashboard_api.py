"""
dashboard.py — Live Paper Trading Dashboard v2

FIXES from v1:
- Reads directly from SQLite (same DB bot writes to)
- Shows live spot chart with indicators
- Shows open positions with current P&L estimate
- Shows all today's trades
- Auto-refreshes every 30 seconds
- Works on mobile phone

Run: python3 dashboard.py
Open: http://YOUR_SERVER_IP:8080
"""

import json
import os
import sys
import math
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import database as db

PORT = 8080


# ══════════════════════════════════════════════════════
# 📊  DATA COLLECTORS
# ══════════════════════════════════════════════════════

def safe(v, default=0):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return default
    return v


def get_dashboard_data() -> dict:
    today     = date.today().isoformat()
    this_week = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    this_mo   = date.today().strftime("%Y-%m")

    # ── Live portfolio status ──────────────────────────────────────
    port   = db.get_portfolio_status()
    cap    = safe(port.get("total_cap"), config.TOTAL_CAPITAL)
    dpnl   = safe(port.get("day_pnl"), 0)
    n_open = safe(port.get("n_open"), 0)
    upd    = port.get("updated_at", "")

    # ── Open positions ─────────────────────────────────────────────
    open_pos = db.get_open_positions()

    # ── Unit statuses ──────────────────────────────────────────────
    units    = db.get_unit_statuses()

    # ── Today's closed trades ─────────────────────────────────────
    trades_today = db.get_trades(from_date=today, to_date=today)

    # ── All trades for stats ───────────────────────────────────────
    all_trades = db.get_trades()
    week_tr    = db.get_trades(from_date=this_week, to_date=today)
    month_tr   = [t for t in all_trades if t.get("month_str", "") == this_mo]

    # ── Stats calculations ─────────────────────────────────────────
    def stats(tlist):
        if not tlist:
            return {"n": 0, "pnl": 0, "wins": 0, "wr": 0, "avg_win": 0, "avg_loss": 0}
        wins  = [t for t in tlist if t["pnl"] > 0]
        loss  = [t for t in tlist if t["pnl"] <= 0]
        pnl   = sum(t["pnl"] for t in tlist)
        gw    = sum(t["pnl"] for t in wins)
        gl    = abs(sum(t["pnl"] for t in loss))
        return {
            "n"       : len(tlist),
            "pnl"     : round(pnl, 0),
            "wins"    : len(wins),
            "wr"      : round(len(wins)/len(tlist)*100, 1),
            "avg_win" : round(gw/len(wins), 0) if wins else 0,
            "avg_loss": round(-gl/len(loss), 0) if loss else 0,
            "pf"      : round(gw/gl, 2) if gl else 0,
        }

    st_all   = stats(all_trades)
    st_week  = stats(week_tr)
    st_month = stats(month_tr)
    st_today = stats(trades_today)

    # ── Equity curve today ─────────────────────────────────────────
    eq_today = db.get_equity_today()

    # ── Recent candles for chart ───────────────────────────────────
    candles = db.get_recent_candles(n=26)  # today's bars

    # ── Daily summaries ────────────────────────────────────────────
    dailies = db.get_daily_summaries(n=20)

    # ── Go-live checklist ──────────────────────────────────────────
    checks = {
        "wr"    : {"val": st_month["wr"],  "target": 35,   "pass": st_month["wr"] >= 35},
        "pf"    : {"val": st_month.get("pf", 0), "target": 1.5, "pass": st_month.get("pf", 0) >= 1.5},
        "ret"   : {"val": round(st_month["pnl"]/config.TOTAL_CAPITAL*100, 1),
                   "target": 20, "pass": st_month["pnl"]/config.TOTAL_CAPITAL*100 > 20},
        "trades": {"val": st_month["n"],   "target": 20,   "pass": st_month["n"] >= 20},
    }
    go_live = all(v["pass"] for v in checks.values())

    return {
        "portfolio"    : {
            "capital"  : round(cap, 0),
            "start"    : config.TOTAL_CAPITAL,
            "total_pnl": round(cap - config.TOTAL_CAPITAL, 0),
            "total_ret": round((cap - config.TOTAL_CAPITAL) / config.TOTAL_CAPITAL * 100, 2),
            "day_pnl"  : round(dpnl, 0),
            "n_open"   : n_open,
            "updated"  : upd[:16] if upd else "—",
        },
        "open_positions": open_pos,
        "units"         : units,
        "today"         : st_today,
        "week"          : st_week,
        "month"         : st_month,
        "all"           : st_all,
        "trades_today"  : trades_today[-20:][::-1],
        "all_trades"    : all_trades[-5:][::-1],
        "equity_today"  : eq_today,
        "candles"       : candles,
        "dailies"       : dailies,
        "checks"        : checks,
        "go_live"       : go_live,
        "last_update"   : datetime.now().strftime("%d %b %Y %H:%M:%S IST"),
    }


# ══════════════════════════════════════════════════════
# 🎨  HTML RENDERER
# ══════════════════════════════════════════════════════

def pc(v):    return "#22c55e" if v >= 0 else "#ef4444"
def sign(v):  return "+" if v >= 0 else ""
def inr(v):   return f"₹{abs(v):,.0f}"
def pct(v):   return f"{v:+.1f}%"


def render(d: dict) -> str:
    port    = d["portfolio"]
    opens   = d["open_positions"]
    units   = d["units"]
    tt      = d["today"]
    wk      = d["week"]
    mo      = d["month"]
    candles = d["candles"]
    eq      = d["equity_today"]
    dailies = d["dailies"]
    checks  = d["checks"]

    # ── Open positions rows ─────────────────────────────────────────
    def pos_rows():
        if not opens:
            return "<tr><td colspan='6' class='empty'>No open positions right now</td></tr>"
        rows = ""
        for p in opens:
            ot  = p.get("opt_type", "-")
            col = "#22c55e" if ot == "CE" else "#ef4444"
            t   = str(p.get("entry_time", ""))[:16]
            rows += f"""<tr>
              <td>Unit {p['unit_id']}</td>
              <td style="color:{col};font-weight:700">{ot}</td>
              <td style="font-size:11px">{p.get('symbol','-')}</td>
              <td>₹{p.get('entry_prem',0):.1f}</td>
              <td>₹{p.get('entry_spot',0):,.0f}</td>
              <td style="font-size:11px">{t}</td>
            </tr>"""
        return rows

    # ── Today's trades rows ─────────────────────────────────────────
    def trade_rows():
        tlist = d["trades_today"]
        if not tlist:
            return "<tr><td colspan='7' class='empty'>No trades yet today</td></tr>"
        rows = ""
        for t in tlist:
            pnl = t.get("pnl", 0)
            pc_ = "#22c55e" if pnl >= 0 else "#ef4444"
            ot  = t.get("opt_type", "-")
            oc  = "#22c55e" if ot == "CE" else "#ef4444"
            ep  = t.get("entry_prem", 0)
            xp  = t.get("exit_prem", 0)
            chg = (xp - ep) / ep * 100 if ep else 0
            r   = t.get("exit_reason", "-")
            rc  = {"TP": "#22c55e", "SL": "#ef4444",
                   "TIME_EXIT": "#f59e0b", "EOD": "#6b7280"}.get(r, "#9ca3af")
            rows += f"""<tr>
              <td>{str(t.get('exit_time',''))[:16]}</td>
              <td style="color:{oc};font-weight:700">{ot}</td>
              <td style="font-size:11px">{t.get('symbol','-')}</td>
              <td>₹{ep:.0f}→₹{xp:.0f} ({chg:+.0f}%)</td>
              <td style="color:{rc};font-weight:600">{r}</td>
              <td>{t.get('bars_held',0)}b</td>
              <td style="color:{pc_};font-weight:700">{sign(pnl)}{inr(pnl)}</td>
            </tr>"""
        return rows

    # ── Unit cards ──────────────────────────────────────────────────
    def unit_cards():
        if not units:
            # Fallback if DB not populated yet
            return f"""<div class="unit-card">
                <div style="color:#6b7280;font-size:12px">Starting up...</div>
              </div>"""
        cards = ""
        for u in units:
            cap  = u.get("capital", config.UNIT_SIZE)
            dpnl = u.get("day_pnl", 0)
            busy = bool(u.get("busy", 0))
            cd   = u.get("cooldown", 0)
            stat = "🔴 LIVE" if busy else ("⏸️ CD" if cd > 0 else "⚪ FREE")
            cc   = "#22c55e" if cap >= config.UNIT_SIZE else "#ef4444"
            dc   = "#22c55e" if dpnl >= 0 else "#ef4444"
            cards += f"""
            <div class="unit-card">
              <div class="unit-header">Unit {u['unit_id']} <span class="badge">{stat}</span></div>
              <div class="unit-cap" style="color:{cc}">₹{cap:,.0f}</div>
              <div style="color:{dc};font-size:12px">{sign(dpnl)}{inr(dpnl)} today</div>
              <div style="color:#6b7280;font-size:11px">{u.get('n_trades',0)} trades total</div>
            </div>"""
        return cards

    # ── Check rows ──────────────────────────────────────────────────
    def check_rows():
        labels = {
            "wr": "Win Rate ≥ 35%",
            "pf": "Profit Factor ≥ 1.5",
            "ret": "Month Return > 20%",
            "trades": "Trades/Month ≥ 20",
        }
        units_ = {"wr": "%", "pf": "", "ret": "%", "trades": ""}
        rows = ""
        for k, v in checks.items():
            ic = "✅" if v["pass"] else "❌"
            cc = "#22c55e" if v["pass"] else "#ef4444"
            rows += f"""
            <div class="check-row">
              <span>{ic} {labels[k]}</span>
              <span style="color:{cc};font-weight:700">
                {v['val']}{units_[k]} / {v['target']}{units_[k]}
              </span>
            </div>"""
        return rows

    # ── Candle chart data ───────────────────────────────────────────
    c_labels = [c["ts"][11:16] for c in candles]
    c_close  = [round(c["close"], 1) for c in candles]
    c_ema9   = [round(c.get("ema9", 0), 1) for c in candles]
    c_ema21  = [round(c.get("ema21", 0), 1) for c in candles]
    c_rsi    = [round(c.get("rsi", 50), 1) for c in candles]
    c_adx    = [round(c.get("adx", 0), 1) for c in candles]

    # Equity chart
    eq_ts  = [e["ts"][11:16] for e in eq]
    eq_cap = [round(e["equity"], 0) for e in eq]

    # Daily P&L chart
    d_dates = [d_["trade_date"] for d_ in dailies]
    d_pnls  = [round(d_["pnl"], 0) for d_ in dailies]

    chart_data = json.dumps({
        "c_labels": c_labels, "c_close": c_close,
        "c_ema9"  : c_ema9,  "c_ema21": c_ema21,
        "c_rsi"   : c_rsi,   "c_adx"  : c_adx,
        "eq_ts"   : eq_ts,   "eq_cap" : eq_cap,
        "d_dates" : d_dates, "d_pnls" : d_pnls,
    })

    today_pnl_col = pc(port["day_pnl"])
    total_pnl_col = pc(port["total_pnl"])
    verdict_col   = "#22c55e" if d["go_live"] else "#ef4444"
    verdict       = "🟢 READY FOR LIVE TRADING" if d["go_live"] else "🔴 CONTINUE PAPER TRADING"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Nifty Bot Live Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0e1a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}}
.hdr{{background:#111827;padding:12px 20px;border-bottom:2px solid #1e2433;
      display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}}
.hdr h1{{font-size:16px;font-weight:700;color:#3b82f6}}
.hdr .meta{{font-size:11px;color:#6b7280;text-align:right}}
.grid-4{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;padding:14px}}
.grid-3{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;padding:0 14px 14px}}
.kpi{{background:#111827;border:1px solid #1e2433;border-radius:10px;padding:14px}}
.kpi .lbl{{font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.6px}}
.kpi .val{{font-size:24px;font-weight:700;margin:4px 0}}
.kpi .sub{{font-size:11px;color:#9ca3af}}
.section{{padding:0 14px 14px}}
.section h2{{font-size:12px;font-weight:600;color:#6b7280;text-transform:uppercase;
             letter-spacing:.6px;margin-bottom:8px;padding-top:4px;
             border-top:1px solid #1e2433;padding-top:12px}}
table{{width:100%;border-collapse:collapse;background:#111827;
       border-radius:10px;overflow:hidden;font-size:12px}}
th{{background:#1e2433;padding:8px 10px;text-align:left;
    font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px}}
td{{padding:8px 10px;border-bottom:1px solid #1e2433}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#1a2235}}
.empty{{text-align:center;color:#4b5563;padding:20px!important;font-style:italic}}
.unit-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px}}
.unit-card{{background:#111827;border:1px solid #1e2433;border-radius:8px;padding:12px;text-align:center}}
.unit-header{{font-size:11px;color:#6b7280;margin-bottom:6px;display:flex;
              justify-content:space-between;align-items:center}}
.unit-cap{{font-size:18px;font-weight:700;margin:4px 0}}
.badge{{font-size:10px;background:#1e2433;padding:2px 6px;border-radius:4px}}
.chart-box{{background:#111827;border:1px solid #1e2433;border-radius:10px;
            padding:14px;margin-bottom:10px}}
.check-row{{display:flex;justify-content:space-between;align-items:center;
            padding:8px 0;border-bottom:1px solid #1e2433;font-size:12px}}
.check-row:last-child{{border-bottom:none}}
.verdict{{text-align:center;font-size:15px;font-weight:700;padding:12px;
          background:#1e2433;border-radius:8px;margin-top:10px}}
.stat-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}}
.stat-box{{background:#111827;border:1px solid #1e2433;border-radius:8px;
           padding:10px;text-align:center}}
.stat-box .sv{{font-size:18px;font-weight:700}}
.stat-box .sl{{font-size:10px;color:#6b7280;text-transform:uppercase}}
.live-dot{{width:8px;height:8px;background:#22c55e;border-radius:50%;
           display:inline-block;animation:pulse 2s infinite;margin-right:6px}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
@media(max-width:500px){{
  .grid-4{{grid-template-columns:repeat(2,1fr)}}
  .stat-grid{{grid-template-columns:repeat(2,1fr)}}
}}
</style>
</head>
<body>

<div class="hdr">
  <h1><span class="live-dot"></span>📈 Nifty Options Paper Trading Bot</h1>
  <div class="meta">
    🔄 Auto-refresh 30s<br>
    Updated: {d["last_update"]}<br>
    Bot last seen: {port["updated"]}
  </div>
</div>

<!-- ── KPI CARDS ──────────────────────────────────── -->
<div class="grid-4">
  <div class="kpi">
    <div class="lbl">Portfolio Value</div>
    <div class="val">₹{port['capital']:,.0f}</div>
    <div class="sub">Started ₹{port['start']:,}</div>
  </div>
  <div class="kpi">
    <div class="lbl">Total P&L</div>
    <div class="val" style="color:{total_pnl_col}">
      {sign(port['total_pnl'])}{inr(port['total_pnl'])}
    </div>
    <div class="sub" style="color:{total_pnl_col}">{pct(port['total_ret'])}</div>
  </div>
  <div class="kpi">
    <div class="lbl">Today P&L</div>
    <div class="val" style="color:{today_pnl_col}">
      {sign(port['day_pnl'])}{inr(port['day_pnl'])}
    </div>
    <div class="sub">{tt['n']} trades | {tt['wins']} wins</div>
  </div>
  <div class="kpi">
    <div class="lbl">Open Positions</div>
    <div class="val" style="color:{'#f59e0b' if port['n_open']>0 else '#6b7280'}">
      {port['n_open']}/{config.NUM_UNITS}
    </div>
    <div class="sub">units active</div>
  </div>
  <div class="kpi">
    <div class="lbl">This Week</div>
    <div class="val" style="color:{pc(wk['pnl'])}">{sign(wk['pnl'])}{inr(wk['pnl'])}</div>
    <div class="sub">{wk['n']} trades | WR {wk['wr']:.0f}%</div>
  </div>
  <div class="kpi">
    <div class="lbl">This Month</div>
    <div class="val" style="color:{pc(mo['pnl'])}">{sign(mo['pnl'])}{inr(mo['pnl'])}</div>
    <div class="sub">{mo['n']} trades | WR {mo['wr']:.0f}%</div>
  </div>
  <div class="kpi">
    <div class="lbl">Overall WR</div>
    <div class="val">{d['all']['wr']:.1f}%</div>
    <div class="sub">Break-even: 33%</div>
  </div>
  <div class="kpi">
    <div class="lbl">Profit Factor</div>
    <div class="val" style="color:{pc(mo.get('pf',0)-1)}">{mo.get('pf',0):.2f}</div>
    <div class="sub">Target: ≥ 1.5</div>
  </div>
</div>

<!-- ── SPOT PRICE CHART ────────────────────────────── -->
<div class="section">
  <h2>📊 Live Nifty — Today's Price + EMA</h2>
  <div class="chart-box">
    <canvas id="spotChart" height="70"></canvas>
  </div>
</div>

<!-- ── RSI + ADX ──────────────────────────────────── -->
<div class="grid-3">
  <div class="chart-box" style="padding:12px">
    <div style="font-size:11px;color:#6b7280;margin-bottom:8px;text-transform:uppercase">RSI</div>
    <canvas id="rsiChart" height="80"></canvas>
  </div>
  <div class="chart-box" style="padding:12px">
    <div style="font-size:11px;color:#6b7280;margin-bottom:8px;text-transform:uppercase">ADX (Trend Strength)</div>
    <canvas id="adxChart" height="80"></canvas>
  </div>
</div>

<!-- ── OPEN POSITIONS ─────────────────────────────── -->
<div class="section">
  <h2>🔴 Open Positions ({len(opens)})</h2>
  <table>
    <tr><th>Unit</th><th>Type</th><th>Symbol</th><th>Entry Prem</th><th>Entry Spot</th><th>Entry Time</th></tr>
    {pos_rows()}
  </table>
</div>

<!-- ── UNIT BREAKDOWN ─────────────────────────────── -->
<div class="section">
  <h2>🏦 Unit Performance</h2>
  <div class="unit-grid">{unit_cards()}</div>
</div>

<!-- ── TODAY'S TRADES ─────────────────────────────── -->
<div class="section">
  <h2>📋 Today's Trades ({tt['n']})</h2>
  <div class="stat-grid">
    <div class="stat-box">
      <div class="sv" style="color:{pc(tt['pnl'])}">{sign(tt['pnl'])}{inr(tt['pnl'])}</div>
      <div class="sl">Day P&L</div>
    </div>
    <div class="stat-box">
      <div class="sv">{tt['wr']:.0f}%</div>
      <div class="sl">Win Rate</div>
    </div>
    <div class="stat-box">
      <div class="sv" style="color:#22c55e">{inr(tt['avg_win'])}</div>
      <div class="sl">Avg Win</div>
    </div>
  </div>
  <table>
    <tr><th>Exit Time</th><th>Type</th><th>Symbol</th><th>Prem Change</th><th>Reason</th><th>Bars</th><th>P&L</th></tr>
    {trade_rows()}
  </table>
</div>

<!-- ── EQUITY CURVE ────────────────────────────────── -->
<div class="section">
  <h2>📈 Today's Equity Curve</h2>
  <div class="chart-box">
    <canvas id="eqChart" height="60"></canvas>
  </div>
</div>

<!-- ── DAILY P&L HISTORY ────────────────────────────── -->
<div class="section">
  <h2>📅 Daily P&L History</h2>
  <div class="chart-box">
    <canvas id="dailyChart" height="60"></canvas>
  </div>
</div>

<!-- ── GO-LIVE CHECKLIST ────────────────────────────── -->
<div class="section">
  <h2>🎯 Live Trading Readiness (This Month)</h2>
  <div class="kpi">
    {check_rows()}
    <div class="verdict" style="color:{verdict_col}">{verdict}</div>
  </div>
</div>

<div style="padding:14px;text-align:center;color:#374151;font-size:10px">
  Paper Trading Only — No Real Money at Risk &nbsp;|&nbsp;
  <a href="/api/data" style="color:#3b82f6">JSON API</a> &nbsp;|&nbsp;
  <a href="/api/trades" style="color:#3b82f6">All Trades</a>
</div>

<script>
const D = {chart_data};

const DARK = {{
  font: {{color:'#9ca3af',size:10}},
  grid: '#1e2433', border:'#1e2433'
}};

function baseOpts(yLabel='', showLegend=true) {{
  return {{
    responsive:true,
    animation:false,
    plugins:{{
      legend:{{display:showLegend,
               labels:{{color:'#9ca3af',font:{{size:10}},boxWidth:12}}}},
    }},
    scales:{{
      x:{{ticks:{{color:'#6b7280',font:{{size:9}},maxTicksLimit:10,maxRotation:0}},
           grid:{{color:'#1e2433'}}}},
      y:{{ticks:{{color:'#6b7280',font:{{size:9}}}},
           grid:{{color:'#1e2433'}},
           title:{{display:!!yLabel,text:yLabel,color:'#6b7280',font:{{size:9}}}}}}
    }}
  }};
}}

// 1. Spot + EMAs
if(D.c_labels.length) {{
  new Chart(document.getElementById('spotChart'), {{
    type:'line',
    data:{{
      labels:D.c_labels,
      datasets:[
        {{label:'Nifty',data:D.c_close,borderColor:'#e2e8f0',borderWidth:2,
          pointRadius:0,tension:0.2}},
        {{label:'EMA9', data:D.c_ema9, borderColor:'#3b82f6',borderWidth:1.5,
          pointRadius:0,tension:0.2,borderDash:[]}},
        {{label:'EMA21',data:D.c_ema21,borderColor:'#f59e0b',borderWidth:1.5,
          pointRadius:0,tension:0.2}},
      ]
    }},
    options:{{...baseOpts('Price'),animation:false}}
  }});
}}

// 2. RSI
if(D.c_labels.length) {{
  new Chart(document.getElementById('rsiChart'), {{
    type:'line',
    data:{{labels:D.c_labels,datasets:[{{
      label:'RSI',data:D.c_rsi,borderColor:'#a78bfa',borderWidth:1.5,
      pointRadius:0,tension:0.2,fill:false
    }}]}},
    options:{{...baseOpts('RSI',false),
      scales:{{...baseOpts().scales,
        y:{{...baseOpts().scales.y,min:0,max:100,
            ticks:{{color:'#6b7280',font:{{size:9}},
                    callback:v=>v+'%'}}}}}}}}
  }});
}}

// 3. ADX
if(D.c_labels.length) {{
  new Chart(document.getElementById('adxChart'), {{
    type:'line',
    data:{{labels:D.c_labels,datasets:[{{
      label:'ADX',data:D.c_adx,borderColor:'#22c55e',borderWidth:1.5,
      pointRadius:0,tension:0.2,fill:false
    }}]}},
    options:{{...baseOpts('ADX',false),
      plugins:{{...baseOpts().plugins,
        annotation:{{annotations:{{line1:{{type:'line',yMin:18,yMax:18,
          borderColor:'#ef4444',borderWidth:1,borderDash:[4,4]}}}}}}}}}}
  }});
}}

// 4. Equity
if(D.eq_ts.length > 1) {{
  const start = D.eq_cap[0];
  const last  = D.eq_cap[D.eq_cap.length-1];
  const col   = last >= start ? '#22c55e' : '#ef4444';
  new Chart(document.getElementById('eqChart'), {{
    type:'line',
    data:{{labels:D.eq_ts,datasets:[{{
      label:'Portfolio ₹',data:D.eq_cap,borderColor:col,borderWidth:2,
      pointRadius:0,tension:0.2,fill:true,
      backgroundColor:col+'18'
    }}]}},
    options:{{...baseOpts('₹',false)}}
  }});
}}

// 5. Daily P&L
if(D.d_dates.length) {{
  new Chart(document.getElementById('dailyChart'), {{
    type:'bar',
    data:{{labels:D.d_dates,datasets:[{{
      label:'Daily P&L',data:D.d_pnls,
      backgroundColor:D.d_pnls.map(v=>v>=0?'#22c55e44':'#ef444444'),
      borderColor:D.d_pnls.map(v=>v>=0?'#22c55e':'#ef4444'),
      borderWidth:1
    }}]}},
    options:{{...baseOpts('₹',false)}}
  }});
}}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════
# 🌐  HTTP SERVER
# ══════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # silence access logs

    def do_GET(self):
        try:
            data = get_dashboard_data()

            if self.path == "/api/data":
                body = json.dumps(data, default=str).encode()
                ct   = "application/json"
            elif self.path == "/api/trades":
                trades = db.get_trades()
                body   = json.dumps(trades, default=str).encode()
                ct     = "application/json"
            else:
                body = render(data).encode()
                ct   = "text/html; charset=utf-8"

            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", len(body))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        except Exception as e:
            err = f"<h1>Dashboard Error</h1><pre>{e}</pre>".encode()
            self.send_response(500)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(err)


if __name__ == "__main__":
    db.init_db()
    print(f"📊 Dashboard at http://0.0.0.0:{PORT}")
    print(f"   Open http://YOUR_SERVER_IP:{PORT}")
    print(f"   Auto-refreshes every 30 seconds")
    print(f"   Ctrl+C to stop")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
