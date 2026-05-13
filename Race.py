"""
Race calculation engine — pure Python, no FastAPI dependencies.
All datetime arithmetic is in UTC. BST = UTC+1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

utc = timezone.utc

# Race schedule (UTC)
RACE_START = datetime(2026, 5, 23, 12, 0, 0, tzinfo=utc)   # 13:00 BST
RACE_END   = datetime(2026, 5, 24, 12, 0, 0, tzinfo=utc)   # 13:00 BST

MAINT_STOP_1 = datetime(2026, 5, 23, 20, 0, 0, tzinfo=utc)  # 21:00 BST
MAINT_STOP_2 = datetime(2026, 5, 24,  5, 0, 0, tzinfo=utc)  # 06:00 BST

VISOR_START  = datetime(2026, 5, 23, 20, 0, 0, tzinfo=utc)  # 21:00 BST
VISOR_END    = datetime(2026, 5, 24,  4, 30, 0, tzinfo=utc) # 05:30 BST

FUEL_BAY_OPEN  = RACE_START + timedelta(minutes=10)
FUEL_BAY_CLOSE = RACE_END   - timedelta(minutes=15)

# Fuel multipliers
# Relative to GREEN baseline
FLAG_MULTIPLIERS: dict[str, float] = {
    "GREEN":  1.0,
    "YELLOW": 0.82,
    "SC":     0.70,
    "RED":    0.0,
}

DEFAULT_CONSUMPTION_LPL = 0.35   # litres per lap if no practice data available


# Fuel model

@dataclass
class FuelModel:
    tank_capacity_L: float = 5.5
    current_level_L: float = 5.5
    safety_margin_laps: int = 2
    alpha: float = 0.3   # EMA smoothing factor
    _ema: float = field(default=0.0, init=False)
    _initialised: bool = field(default=False, init=False)

    def update_from_practice(self, sessions: list[dict]) -> None:
        """Seed the EMA from practice session records."""
        for s in sessions:
            litres_used = s["fuel_start_L"] - s["fuel_end_L"]
            if s["laps_completed"] > 0 and litres_used > 0:
                raw_Lpl = litres_used / s["laps_completed"]
                flag = s.get("flag_condition", "GREEN")
                self._update_ema(raw_Lpl, flag)

    def update_from_stint(self, litres_used: float, laps: int, flag: str = "GREEN") -> None:
        if laps > 0 and litres_used > 0:
            self._update_ema(litres_used / laps, flag)

    def _update_ema(self, raw_Lpl: float, flag: str) -> None:
        # Normalise to GREEN baseline so yellow/SC stints don't understate consumption
        multiplier = FLAG_MULTIPLIERS.get(flag, 1.0)
        if multiplier > 0:
            normalised = raw_Lpl / multiplier
        else:
            return  # RED flag — no usable data
        if not self._initialised:
            self._ema = normalised
            self._initialised = True
        else:
            self._ema = self.alpha * normalised + (1 - self.alpha) * self._ema

    @property
    def avg_consumption_Lpl(self) -> float:
        return self._ema if self._initialised else DEFAULT_CONSUMPTION_LPL

    def laps_to_empty(self, flag: str = "GREEN") -> float:
        multiplier = FLAG_MULTIPLIERS.get(flag, 1.0)
        effective = self.avg_consumption_Lpl * multiplier
        if effective <= 0:
            return float("inf")
        return self.current_level_L / effective

    def laps_until_pit(self, flag: str = "GREEN") -> float:
        return max(0.0, self.laps_to_empty(flag) - self.safety_margin_laps)

    def time_to_pit_seconds(self, avg_lap_s: float, flag: str = "GREEN") -> float:
        return self.laps_until_pit(flag) * avg_lap_s

    def fuel_percent(self) -> float:
        if self.tank_capacity_L <= 0:
            return 0.0
        return max(0.0, min(100.0, self.current_level_L / self.tank_capacity_L * 100))

    def stops_remaining(self, now: datetime, avg_lap_s: float) -> int:
        if avg_lap_s <= 0:
            return 0
        time_left = (RACE_END - now).total_seconds()
        if time_left <= 0:
            return 0
        laps_left = time_left / avg_lap_s
        total_fuel_needed = laps_left * self.avg_consumption_Lpl
        return max(0, math.ceil(total_fuel_needed / self.tank_capacity_L) - 1)


# ── Stint calculator ─────────────────────────────────────────────────────────

@dataclass
class StintCalculator:
    drivers: list[str]            # ordered rotation list (mutable)
    current_index: int = 0
    max_stint_minutes: int = 180  # from RunPlan.ini

    def current_driver(self) -> str:
        if not self.drivers:
            return "—"
        return self.drivers[self.current_index % len(self.drivers)]

    def next_driver(self) -> str:
        if not self.drivers:
            return "—"
        return self.drivers[(self.current_index + 1) % len(self.drivers)]

    def rotate(self) -> str:
        self.current_index = (self.current_index + 1) % len(self.drivers)
        return self.current_driver()

    def reorder(self, new_order: list[str]) -> None:
        current = self.current_driver()
        self.drivers = new_order
        if current in self.drivers:
            self.current_index = self.drivers.index(current)
        else:
            self.current_index = 0

    def driver_at(self, offset: int) -> str:
        if not self.drivers:
            return "—"
        return self.drivers[(self.current_index + offset) % len(self.drivers)]


# ── Lap time rolling average ─────────────────────────────────────────────────

@dataclass
class LapTimeModel:
    _ema_ms: float = field(default=0.0, init=False)
    _initialised: bool = field(default=False, init=False)
    alpha: float = 0.3

    def update(self, lap_time_ms: int, flag: str = "GREEN") -> None:
        if flag == "RED":
            return
        if not self._initialised:
            self._ema_ms = float(lap_time_ms)
            self._initialised = True
        else:
            self._ema_ms = self.alpha * lap_time_ms + (1 - self.alpha) * self._ema_ms

    @property
    def avg_lap_s(self) -> float:
        return self._ema_ms / 1000 if self._initialised else 55.0  # ~55s default

    @property
    def avg_lap_ms(self) -> int:
        return int(self._ema_ms) if self._initialised else 55000


# ── Alert engine ─────────────────────────────────────────────────────────────

@dataclass
class Alert:
    id: str
    severity: str        # INFO / WARNING / CRITICAL
    message: str
    dismissible: bool = True


@dataclass
class AlertEngine:
    _dismissed: set[str] = field(default_factory=set)
    _maintenance_done: set[str] = field(default_factory=set)

    def mark_maintenance_done(self, stop_id: str) -> None:
        self._maintenance_done.add(stop_id)

    def dismiss(self, alert_id: str) -> None:
        self._dismissed.add(alert_id)

    def active_alerts(self, now: datetime) -> list[Alert]:
        alerts: list[Alert] = []
        self._check_visor(now, alerts)
        self._check_maintenance(now, alerts)
        self._check_fuel_bay(now, alerts)
        return alerts

    def _check_visor(self, now: datetime, alerts: list[Alert]) -> None:
        warn_time = VISOR_START - timedelta(minutes=30)
        if warn_time <= now < VISOR_START and "visor_warn" not in self._dismissed:
            minutes_left = int((VISOR_START - now).total_seconds() / 60)
            alerts.append(Alert("visor_warn", "WARNING", f"Switch to clear visor in {minutes_left} min (21:00 BST)"))
        if VISOR_START <= now < VISOR_END:
            alerts.append(Alert("visor_on", "CRITICAL", "CLEAR VISOR MANDATORY — black flag risk (until 05:30 BST)", dismissible=False))

    def _check_maintenance(self, now: datetime, alerts: list[Alert]) -> None:
        for stop_id, stop_time, label in [
            ("maint1", MAINT_STOP_1, "21:00"),
            ("maint2", MAINT_STOP_2, "06:00"),
        ]:
            if stop_id in self._maintenance_done:
                continue
            warn_time = stop_time - timedelta(minutes=30)
            window_end = stop_time + timedelta(minutes=10)
            if warn_time <= now < stop_time and f"{stop_id}_warn" not in self._dismissed:
                minutes_left = int((stop_time - now).total_seconds() / 60)
                alerts.append(Alert(f"{stop_id}_warn", "WARNING", f"Mandatory maintenance stop in {minutes_left} min ({label} BST)"))
            if stop_time <= now < window_end:
                alerts.append(Alert(f"{stop_id}_now", "CRITICAL", f"MAINTENANCE STOP WINDOW OPEN ({label} BST) — pit now", dismissible=False))

    def _check_fuel_bay(self, now: datetime, alerts: list[Alert]) -> None:
        close_warn = FUEL_BAY_CLOSE - timedelta(minutes=15)
        if close_warn <= now < FUEL_BAY_CLOSE and "fuel_close_warn" not in self._dismissed:
            alerts.append(Alert("fuel_close_warn", "WARNING", "Fuel bay closes in 15 minutes — last chance to refuel"))
        if now >= FUEL_BAY_CLOSE and "fuel_closed" not in self._dismissed:
            alerts.append(Alert("fuel_closed", "INFO", "Fuel bay is CLOSED — no more refuelling"))


# ── Ballast helpers ──────────────────────────────────────────────────────────

def ballast_required(driver_weight_kg: float) -> float:
    """Returns kg of ballast needed on kart for this driver.
    DMAX: min total 219 kg = 134 kg kart + 85 kg driver target.
    """
    return max(0.0, 85.0 - driver_weight_kg)


def ballast_delta(current_weight_kg: float, next_weight_kg: float) -> float:
    """Signed kg change: positive = add ballast, negative = remove ballast."""
    return ballast_required(next_weight_kg) - ballast_required(current_weight_kg)


# ── Lap time string helpers ──────────────────────────────────────────────────

def parse_lap_time(s: str) -> int:
    """Parse 'mm:ss.xxx' or 'ss.xxx' into milliseconds."""
    s = s.strip()
    try:
        if ":" in s:
            parts = s.split(":")
            minutes = int(parts[0])
            sec_parts = parts[1].split(".")
            seconds = int(sec_parts[0])
            ms = int(sec_parts[1].ljust(3, "0")[:3]) if len(sec_parts) > 1 else 0
        else:
            sec_parts = s.split(".")
            minutes = 0
            seconds = int(sec_parts[0])
            ms = int(sec_parts[1].ljust(3, "0")[:3]) if len(sec_parts) > 1 else 0
        return (minutes * 60 + seconds) * 1000 + ms
    except (ValueError, IndexError):
        raise ValueError(f"Cannot parse lap time: '{s}'")


def format_lap_time(ms: int) -> str:
    """Format milliseconds as 'm:ss.xxx'."""
    total_s, rem_ms = divmod(ms, 1000)
    minutes, seconds = divmod(total_s, 60)
    return f"{minutes}:{seconds:02d}.{rem_ms:03d}"


def format_duration(seconds: float) -> str:
    """Format seconds as 'HH:MM:SS'."""
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"
