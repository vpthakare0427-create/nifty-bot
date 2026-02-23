"""
units.py — Trade unit management with state persistence
"""

import json
import logging
import os
from collections import defaultdict
from datetime import date
import config

logger = logging.getLogger("units")


class TradeUnit:
    def __init__(self, uid: int, capital: float = None):
        self.uid      = uid
        self.capital  = capital if capital is not None else float(config.UNIT_SIZE)
        self.trade    = None       # active trade dict or None
        self.streak   = 0
        self.cooldown = 0
        self.n_trades = 0
        self.last_bar = -99
        self.day_pnl  = defaultdict(float)

    @property
    def free(self):
        return self.trade is None and self.cooldown == 0

    def day_ok(self, ts_date) -> bool:
        return self.day_pnl[ts_date] > -(config.UNIT_SIZE * config.MAX_UNIT_DAY_LOSS)

    def enter(self, trade_dict: dict, bar_idx: int):
        self.trade    = trade_dict
        self.n_trades += 1
        self.last_bar  = bar_idx

    def close(self, pnl: float, ts_date) -> dict:
        self.capital           += pnl
        self.day_pnl[ts_date]  += pnl
        trade                   = self.trade
        self.trade              = None
        if pnl < 0:
            self.streak += 1
            if self.streak >= config.LOSS_STREAK_MAX:
                self.cooldown = config.COOLDOWN_BARS
                self.streak   = 0
                logger.warning(f"[Unit {self.uid}] Loss streak — cooldown {config.COOLDOWN_BARS} bars")
        else:
            self.streak = 0
        return trade

    def tick(self):
        if self.cooldown > 0:
            self.cooldown -= 1

    def to_dict(self) -> dict:
        return {
            "uid"     : self.uid,
            "capital" : self.capital,
            "trade"   : self.trade,
            "streak"  : self.streak,
            "cooldown": self.cooldown,
            "n_trades": self.n_trades,
            "last_bar": self.last_bar,
            "day_pnl" : {str(k): v for k, v in self.day_pnl.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TradeUnit":
        u          = cls(d["uid"], d["capital"])
        u.trade    = d.get("trade")
        u.streak   = d.get("streak", 0)
        u.cooldown = d.get("cooldown", 0)
        u.n_trades = d.get("n_trades", 0)
        u.last_bar = d.get("last_bar", -99)
        u.day_pnl  = defaultdict(float, {
            date.fromisoformat(k): v
            for k, v in d.get("day_pnl", {}).items()
        })
        return u


class UnitManager:
    def __init__(self):
        self.units  = []
        self.rr_ptr = 0
        self._load()

    def _load(self):
        if os.path.exists(config.STATE_FILE):
            try:
                with open(config.STATE_FILE) as f:
                    state = json.load(f)
                self.units  = [TradeUnit.from_dict(u) for u in state["units"]]
                self.rr_ptr = state.get("rr_ptr", 0)
                logger.info(f"State loaded: {len(self.units)} units, "
                            f"capital ₹{self.total_capital():,.0f}")
                return
            except Exception as e:
                logger.warning(f"State load failed ({e}) — fresh start")
        self.units  = [TradeUnit(i) for i in range(config.NUM_UNITS)]
        self.rr_ptr = 0
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
        with open(config.STATE_FILE, "w") as f:
            json.dump({
                "units" : [u.to_dict() for u in self.units],
                "rr_ptr": self.rr_ptr,
            }, f, indent=2, default=str)

    def tick_all(self):
        for u in self.units:
            u.tick()

    def get_next_free(self, ts_date, bar_idx: int) -> "TradeUnit | None":
        for i in range(config.NUM_UNITS):
            uid = (self.rr_ptr + i) % config.NUM_UNITS
            u   = self.units[uid]
            if not u.free:
                continue
            if not u.day_ok(ts_date):
                continue
            if bar_idx - u.last_bar < config.SIGNAL_COOLDOWN:
                continue
            self.rr_ptr = (uid + 1) % config.NUM_UNITS
            return u
        return None

    def total_capital(self) -> float:
        return sum(u.capital for u in self.units)

    def day_pnl_total(self, today: date) -> float:
        return sum(u.day_pnl.get(today, 0) for u in self.units)

    def active_count(self) -> int:
        return sum(1 for u in self.units if u.trade is not None)

    def is_halted(self) -> bool:
        return self.total_capital() < config.TOTAL_CAPITAL * (1 - config.MAX_PORT_DD)

    def day_loss_breach(self, today: date) -> bool:
        dpnl = self.day_pnl_total(today)
        return dpnl < -(config.TOTAL_CAPITAL * config.MAX_PORT_DAY_LOSS)
