"""
database.py — SQLite persistence, single source of truth
Both bot.py AND dashboard.py read from this same DB.
"""

import sqlite3
import json
import os
from datetime import datetime, date
import config


def get_conn():
    conn = sqlite3.connect(config.DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id     INTEGER,
            opt_type    TEXT,
            symbol      TEXT,
            strike      REAL,
            lot_size    REAL,
            qty         INTEGER,
            entry_time  TEXT,
            exit_time   TEXT,
            entry_spot  REAL,
            exit_spot   REAL,
            entry_prem  REAL,
            exit_prem   REAL,
            bars_held   INTEGER,
            pnl         REAL,
            exit_reason TEXT,
            live_data   INTEGER DEFAULT 0,
            trade_date  TEXT,
            week_num    TEXT,
            month_str   TEXT
        );

        CREATE TABLE IF NOT EXISTS open_positions (
            unit_id     INTEGER PRIMARY KEY,
            opt_type    TEXT,
            symbol      TEXT,
            strike      REAL,
            lot_size    REAL,
            qty         INTEGER,
            entry_time  TEXT,
            entry_spot  REAL,
            entry_prem  REAL,
            bar_idx     INTEGER,
            sid         TEXT,
            updated_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS portfolio_status (
            id          INTEGER PRIMARY KEY CHECK (id=1),
            total_cap   REAL,
            day_pnl     REAL,
            trade_date  TEXT,
            n_open      INTEGER,
            updated_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS unit_status (
            unit_id     INTEGER PRIMARY KEY,
            capital     REAL,
            n_trades    INTEGER,
            day_pnl     REAL,
            busy        INTEGER,
            cooldown    INTEGER,
            updated_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS spot_candles (
            ts          TEXT PRIMARY KEY,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            volume      REAL,
            adx         REAL,
            rsi         REAL,
            ema9        REAL,
            ema21       REAL,
            signal      TEXT
        );

        CREATE TABLE IF NOT EXISTS equity_curve (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            equity      REAL,
            trade_date  TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_summary (
            trade_date  TEXT PRIMARY KEY,
            start_cap   REAL,
            end_cap     REAL,
            pnl         REAL,
            return_pct  REAL,
            n_trades    INTEGER,
            n_wins      INTEGER,
            win_rate    REAL
        );

        INSERT OR IGNORE INTO portfolio_status(id,total_cap,day_pnl,trade_date,n_open,updated_at)
        VALUES (1,100000,0,'',0,'');
        """)
    print("✅ DB ready")


# ── Write functions ────────────────────────────────────────────────

def log_trade(t: dict):
    dt  = datetime.fromisoformat(str(t["entry_time"]))
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO trades
              (unit_id,opt_type,symbol,strike,lot_size,qty,
               entry_time,exit_time,entry_spot,exit_spot,
               entry_prem,exit_prem,bars_held,pnl,exit_reason,
               live_data,trade_date,week_num,month_str)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            t["unit_id"], t["opt_type"], t["symbol"],
            t["strike"], t["lot_size"], t["qty"],
            str(t["entry_time"]), str(t["exit_time"]),
            t["entry_spot"], t["exit_spot"],
            t["entry_prem"], t["exit_prem"],
            t["bars_held"], t["pnl"], t["exit_reason"],
            int(t.get("live_data", 0)),
            dt.strftime("%Y-%m-%d"),
            dt.strftime("%Y-W%W"),
            dt.strftime("%Y-%m"),
        ))


def upsert_open_position(unit_id: int, trade: dict):
    """Insert or update open position in DB (visible to dashboard)."""
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO open_positions
              (unit_id,opt_type,symbol,strike,lot_size,qty,
               entry_time,entry_spot,entry_prem,bar_idx,sid,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            unit_id, trade["opt_type"], trade["symbol"],
            trade["strike"], trade["lot_size"], trade["qty"],
            str(trade["entry_time"]), trade["entry_spot"],
            trade["entry_prem"], trade.get("bar_idx", 0),
            trade.get("sid", ""), datetime.now().isoformat()
        ))


def remove_open_position(unit_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM open_positions WHERE unit_id=?", (unit_id,))


def update_portfolio(total_cap: float, day_pnl: float, n_open: int):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO portfolio_status
              (id,total_cap,day_pnl,trade_date,n_open,updated_at)
            VALUES (1,?,?,?,?,?)
        """, (total_cap, day_pnl, date.today().isoformat(), n_open,
              datetime.now().isoformat()))


def update_unit_status(units: list):
    now = datetime.now().isoformat()
    today = date.today()
    with get_conn() as conn:
        for u in units:
            dpnl = u.day_pnl.get(today, 0)
            conn.execute("""
                INSERT OR REPLACE INTO unit_status
                  (unit_id,capital,n_trades,day_pnl,busy,cooldown,updated_at)
                VALUES (?,?,?,?,?,?,?)
            """, (u.uid, round(u.capital, 2), u.n_trades,
                  round(dpnl, 2), int(u.trade is not None),
                  u.cooldown, now))


def log_candle(row):
    """Save latest spot candle + indicators to DB for dashboard chart."""
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO spot_candles
              (ts,open,high,low,close,volume,adx,rsi,ema9,ema21,signal)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            str(row.name), float(row.get("open", 0)),
            float(row.get("high", 0)), float(row.get("low", 0)),
            float(row["close"]), float(row.get("volume", 0)),
            round(float(row.get("adx", 0)), 2),
            round(float(row.get("rsi", 50)), 2),
            round(float(row.get("ema9", 0)), 2),
            round(float(row.get("ema21", 0)), 2),
            row.get("signal", ""),
        ))


def log_equity(ts, equity: float):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO equity_curve (ts,equity,trade_date)
            VALUES (?,?,?)
        """, (str(ts), equity,
              datetime.fromisoformat(str(ts)).strftime("%Y-%m-%d")))


def save_daily_summary(trade_date: str, start_cap: float,
                       end_cap: float, trades: list):
    wins = [t for t in trades if t["pnl"] > 0]
    wr   = len(wins) / len(trades) * 100 if trades else 0
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO daily_summary
              (trade_date,start_cap,end_cap,pnl,return_pct,n_trades,n_wins,win_rate)
            VALUES (?,?,?,?,?,?,?,?)
        """, (trade_date, start_cap, end_cap,
              end_cap - start_cap,
              (end_cap - start_cap) / start_cap * 100,
              len(trades), len(wins), wr))


# ── Read functions ─────────────────────────────────────────────────

def get_trades(from_date=None, to_date=None) -> list:
    q      = "SELECT * FROM trades"
    params = []
    if from_date:
        q += " WHERE trade_date >= ?"
        params.append(from_date)
    if to_date:
        q += (" AND" if from_date else " WHERE") + " trade_date <= ?"
        params.append(to_date)
    q += " ORDER BY entry_time"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_open_positions() -> list:
    with get_conn() as conn:
        return [dict(r) for r in
                conn.execute("SELECT * FROM open_positions ORDER BY unit_id").fetchall()]


def get_portfolio_status() -> dict:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM portfolio_status WHERE id=1").fetchone()
        return dict(r) if r else {}


def get_unit_statuses() -> list:
    with get_conn() as conn:
        return [dict(r) for r in
                conn.execute("SELECT * FROM unit_status ORDER BY unit_id").fetchall()]


def get_recent_candles(n: int = 30) -> list:
    with get_conn() as conn:
        return [dict(r) for r in
                conn.execute(
                    "SELECT * FROM spot_candles ORDER BY ts DESC LIMIT ?",
                    (n,)).fetchall()][::-1]


def get_equity_today() -> list:
    today = date.today().isoformat()
    with get_conn() as conn:
        return [dict(r) for r in
                conn.execute(
                    "SELECT ts,equity FROM equity_curve WHERE trade_date=? ORDER BY ts",
                    (today,)).fetchall()]


def get_daily_summaries(n: int = 30) -> list:
    with get_conn() as conn:
        return [dict(r) for r in
                conn.execute(
                    "SELECT * FROM daily_summary ORDER BY trade_date DESC LIMIT ?",
                    (n,)).fetchall()][::-1]


def clear_today_open_positions():
    """Called at EOD to clean up."""
    with get_conn() as conn:
        conn.execute("DELETE FROM open_positions")
