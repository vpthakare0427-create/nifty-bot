"""
Performance Report — Run anytime to see how your strategy is doing.
Usage: python report.py
"""

import sqlite3
import pandas as pd
import os

DB_FILE = "trading.db"

def generate_report():
    if not os.path.exists(DB_FILE):
        print("❌ No database found. Bot hasn't traded yet.")
        return

    conn = sqlite3.connect(DB_FILE)

    # Trades
    df = pd.read_sql_query("SELECT * FROM trades", conn)
    sigs = pd.read_sql_query("SELECT * FROM signals", conn)
    daily = pd.read_sql_query("SELECT * FROM daily_summary", conn)

    conn.close()

    if df.empty:
        print("⚠️  No trades recorded yet.")
        print(f"   Signals seen so far: {len(sigs)}")
        return

    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"]  = pd.to_datetime(df["exit_time"])
    df["date"]       = df["entry_time"].dt.date

    total  = len(df)
    wins   = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]
    wr     = len(wins) / total * 100

    win_sum  = wins["pnl"].sum() if not wins.empty else 0
    loss_sum = losses["pnl"].sum() if not losses.empty else 0
    pf       = win_sum / abs(loss_sum) if loss_sum != 0 else float("inf")

    # Equity curve
    df_sorted = df.sort_values("exit_time")
    df_sorted["cum_pnl"] = df_sorted["pnl"].cumsum()
    df_sorted["eq"]      = 100000 + df_sorted["cum_pnl"]
    df_sorted["peak"]    = df_sorted["eq"].cummax()
    df_sorted["dd"]      = (df_sorted["eq"] - df_sorted["peak"]) / df_sorted["peak"] * 100
    max_dd = df_sorted["dd"].min()

    SEP = "═" * 55
    print(f"\n{SEP}")
    print("  📈  NIFTY OPTIONS BOT — PERFORMANCE REPORT")
    print(SEP)
    print(f"  Period        : {df['date'].min()} → {df['date'].max()}")
    print(f"  Trading Days  : {df['date'].nunique()}")
    print(f"  Total Signals : {len(sigs)}")
    print(f"  Total Trades  : {total}")
    print(f"  CE Trades     : {len(df[df.opt_type == 'CE'])}")
    print(f"  PE Trades     : {len(df[df.opt_type == 'PE'])}")
    print("─" * 55)
    print(f"  Win Rate      : {wr:.1f}%")
    print(f"  Profit Factor : {pf:.2f}")
    print(f"  Total P&L     : ₹{df['pnl'].sum():,.0f}")
    print(f"  Avg Win       : ₹{wins['pnl'].mean():,.0f}" if not wins.empty else "  Avg Win       : —")
    print(f"  Avg Loss      : ₹{losses['pnl'].mean():,.0f}" if not losses.empty else "  Avg Loss      : —")
    print(f"  Best Trade    : ₹{df['pnl'].max():,.0f}")
    print(f"  Worst Trade   : ₹{df['pnl'].min():,.0f}")
    print(f"  Max Drawdown  : {max_dd:.1f}%")
    print("─" * 55)

    # Exit reason breakdown
    print("\n  Exit Reason Breakdown:")
    for reason, g in df.groupby("reason"):
        w = (g["pnl"] > 0).sum()
        print(f"    {reason:<12} Trades:{len(g):>3}  Wins:{w:>3}  P&L: ₹{g['pnl'].sum():>9,.0f}")

    # Daily summary
    if not daily.empty:
        print("\n  Daily P&L:")
        for _, row in daily.iterrows():
            emoji = "✅" if row["daily_pnl"] >= 0 else "🔴"
            print(f"    {row['date']}  {emoji}  ₹{row['daily_pnl']:>9,.2f}  Trades:{int(row['total_trades'])}")

    # Unit breakdown
    print("\n  Unit Performance:")
    for uid, g in df.groupby("unit"):
        pnl = g["pnl"].sum()
        w   = (g["pnl"] > 0).sum()
        s   = "✅" if pnl >= 0 else "🔴"
        print(f"    Unit {uid}  {s}  Trades:{len(g):>3}  WR:{w/len(g)*100:.0f}%  P&L: ₹{pnl:>9,.0f}")

    print(SEP)

    # Save to CSV
    df.to_csv("trade_history.csv", index=False)
    print("💾 trade_history.csv saved")
    print(SEP)

if __name__ == "__main__":
    generate_report()
