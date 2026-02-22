"""
Dashboard API — Serves trading data to dashboard.html
Run this alongside bot.py on Railway.

Add to Procfile:
  web: python dashboard_api.py
  worker: python bot.py
"""

from flask import Flask, jsonify, send_from_directory
import sqlite3, os, json

app = Flask(__name__)
DB_FILE = "trading.db"

def query(sql, args=()):
    if not os.path.exists(DB_FILE):
        return []
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")

@app.route("/api/dashboard")
def dashboard():
    trades    = query("SELECT * FROM trades ORDER BY entry_time DESC")
    daily     = query("SELECT * FROM daily_summary ORDER BY date")
    signals   = query("SELECT * FROM signals ORDER BY datetime DESC LIMIT 50")
    unit_rows = query("""
        SELECT unit as uid,
               SUM(pnl) as pnl,
               COUNT(*) as trades
        FROM trades GROUP BY unit
    """)

    # Build unit stats with current capital
    state_file = "bot_state.json"
    unit_stats = []
    if os.path.exists(state_file):
        with open(state_file) as f:
            state = json.load(f)
        for u in state["units"]:
            matched = next((r for r in unit_rows if r["uid"]==u["uid"]), None)
            unit_stats.append({
                "uid":     u["uid"],
                "capital": u["capital"],
                "pnl":     u["capital"] - 20000,
                "trades":  matched["trades"] if matched else 0
            })
    else:
        for i in range(5):
            matched = next((r for r in unit_rows if r["uid"]==i), None)
            unit_stats.append({
                "uid": i, "capital": 20000 + (matched["pnl"] if matched else 0),
                "pnl": matched["pnl"] if matched else 0,
                "trades": matched["trades"] if matched else 0
            })

    # Build equity timeline from trades
    equity = []
    eq = 100000
    for t in sorted(trades, key=lambda x: x["exit_time"]):
        eq += t["pnl"]
        equity.append({"ts": t["exit_time"], "eq": eq})

    return jsonify({
        "trades":    trades,
        "daily":     daily,
        "signals":   signals,
        "unitStats": unit_stats,
        "equity":    equity
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
