"""reports.py — Daily/weekly/monthly report generation"""
import os, csv, logging
from datetime import datetime
import config

logger = logging.getLogger("reports")

def generate_daily(trade_date: str, units, trades: list):
    path = os.path.join(config.REPORT_DIR, "daily", f"{trade_date}.csv")
    try:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[
                "unit_id","opt_type","symbol","entry_time","exit_time",
                "entry_prem","exit_prem","bars_held","pnl","exit_reason"])
            w.writeheader()
            w.writerows(trades)
        # Summary text
        pnl  = sum(t["pnl"] for t in trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        n    = len(trades)
        wr   = wins/n*100 if n else 0
        summary = f"""DATE: {trade_date}
TRADES: {n}  WINS: {wins}  WIN_RATE: {wr:.1f}%
PNL: Rs.{pnl:+,.0f}
"""
        spath = os.path.join(config.REPORT_DIR, "daily", f"{trade_date}_summary.txt")
        with open(spath, "w") as f:
            f.write(summary)
        logger.info(f"Daily report: {path}")
    except Exception as e:
        logger.warning(f"Report error: {e}")
