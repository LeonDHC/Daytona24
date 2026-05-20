"""
Daytona 24 Race Strategy Dashboard — FastAPI application.
Run with: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import asyncio
import configparser
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import database as db
from Race import (
    AlertEngine,
    FuelModel,
    LapTimeModel,
    StintCalculator,
    ballast_delta,
    ballast_required,
    format_duration,
    format_lap_time,
    parse_lap_time,
    FLAG_MULTIPLIERS,
    RACE_END,
    RACE_START,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("daytona24")

app = FastAPI(title="Daytona 24 Race Dashboard")

# ── Static files ─────────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ── In-memory race state ─────────────────────────────────────────────────────
_fuel_model = FuelModel()
_lap_model = LapTimeModel()
_stint_calc = StintCalculator(drivers=[])
_alert_engine = AlertEngine()
_scraper_task: asyncio.Task | None = None
_scraper_status = {"running": False, "last_poll": None, "session_url": None, "team_number": None, "error": None}

utc = timezone.utc


async def _load_state_into_memory() -> None:
    state = await db.get_state()
    _fuel_model.tank_capacity_L = float(state.get("tank_capacity_L", 10.0))
    _fuel_model.current_level_L = float(state.get("fuel_level_L", 10.0))
    _fuel_model.safety_margin_laps = int(state.get("safety_margin_laps", 2))

    drivers = await db.get_drivers()
    _stint_calc.drivers = [d["name"] for d in drivers]

    # Seed lap time model from existing laps
    laps = await db.get_laps(limit=200)
    for lap in laps:
        _lap_model.update(lap["lap_time_ms"], lap["flag_condition"])

    # Seed fuel model from all known inputs (practice + completed stints)
    await _recompute_fuel_model()

    # Restore current driver index
    current = state.get("current_driver", "")
    if current and current in _stint_calc.drivers:
        _stint_calc.current_index = _stint_calc.drivers.index(current)


async def _recompute_fuel_model() -> None:
    """Rebuild the FuelModel EMA from scratch using all practice sessions and
    every completed stint that has both fuel endpoints. Called on startup and
    after any edit/delete to practice sessions or stints."""
    _fuel_model.reset()
    practice = await db.get_practice_sessions()
    _fuel_model.update_from_practice(practice)
    for s in await db.get_stints():
        if s.get("ended_at") and s.get("fuel_start_L") is not None and s.get("fuel_end_L") is not None:
            laps = (s.get("end_lap") or 0) - (s.get("start_lap") or 0)
            litres = float(s["fuel_start_L"]) - float(s["fuel_end_L"])
            if laps > 0 and litres > 0:
                _fuel_model.update_from_stint(litres, laps)


async def _broadcast_current_driver():
    """Send only the current_driver block — used after lap inserts so the
    panel-current totals refresh without rebuilding the laps table / chart."""
    full = await _full_state()
    await manager.broadcast({
        "type": "current_driver_update",
        "data": full["data"]["current_driver"],
    })


async def _full_state() -> dict:
    now = datetime.now(utc)
    state = await db.get_state()
    drivers = await db.get_drivers()
    current_stint = await db.get_current_stint()
    laps = await db.get_laps(limit=20)
    alerts = _alert_engine.active_alerts(now)
    stats = await db.get_lap_stats()

    driver_map = {d["name"]: d for d in drivers}
    curr_name = _stint_calc.current_driver()
    next_name = _stint_calc.next_driver()
    curr_driver = driver_map.get(curr_name, {})
    next_driver = driver_map.get(next_name, {})

    # Stint elapsed time
    stint_elapsed_s = 0
    if current_stint and current_stint.get("started_at"):
        try:
            started = datetime.fromisoformat(current_stint["started_at"]).replace(tzinfo=utc)
            stint_elapsed_s = int((now - started).total_seconds())
        except ValueError:
            pass

    # Per-driver cumulative stats
    driver_stats = (
        await db.get_driver_stats(curr_name)
        if curr_name and curr_name != "—"
        else {"total_laps": 0, "avg_lap_ms": None, "stint_durations": []}
    )
    total_kart_s = 0
    for started, ended in driver_stats["stint_durations"]:
        if ended:
            try:
                t0 = datetime.fromisoformat(started).replace(tzinfo=utc)
                t1 = datetime.fromisoformat(ended).replace(tzinfo=utc)
                total_kart_s += int((t1 - t0).total_seconds())
            except ValueError:
                pass
    total_kart_s += stint_elapsed_s

    current_lap_n = int(state.get("current_lap", 0))
    laps_in_kart = 0
    stint_start_lap = None
    stint_started_at = None
    if current_stint:
        stint_started_at = current_stint.get("started_at")
        stint_start_lap = current_stint.get("start_lap") or 0
        laps_in_kart = max(0, current_lap_n - stint_start_lap)

    time_remaining_s = max(0, (RACE_END - now).total_seconds())
    race_started = state.get("race_started", "0") == "1"

    return {
        "type": "full_state",
        "data": {
            "now_utc": now.isoformat(),
            "race_started": race_started,
            "time_remaining_s": int(time_remaining_s),
            "current_driver": {
                "name": curr_name,
                "weight_kg": curr_driver.get("weight_kg", 85),
                "pedal_pos": curr_driver.get("pedal_pos", "3"),
                "ballast_kg": ballast_required(curr_driver.get("weight_kg", 85)),
                "stint_elapsed_s": stint_elapsed_s,
                "stint_started_at": stint_started_at,
                "stint_start_lap": stint_start_lap,
                "laps_in_kart": laps_in_kart,
                "total_laps_raced": driver_stats["total_laps"],
                "avg_lap_ms": driver_stats["avg_lap_ms"],
                "avg_lap_formatted": format_lap_time(driver_stats["avg_lap_ms"]) if driver_stats["avg_lap_ms"] else None,
                "total_time_in_kart_s": total_kart_s,
            },
            "next_driver": {
                "name": next_name,
                "weight_kg": next_driver.get("weight_kg", 85),
                "pedal_pos": next_driver.get("pedal_pos", "3"),
                "ballast_kg": ballast_required(next_driver.get("weight_kg", 85)),
                "ballast_delta_kg": ballast_delta(
                    curr_driver.get("weight_kg", 85),
                    next_driver.get("weight_kg", 85),
                ),
            },
            "fuel": {
                "level_L": round(_fuel_model.current_level_L, 2),
                "capacity_L": _fuel_model.tank_capacity_L,
                "percent": round(_fuel_model.fuel_percent(), 1),
                "avg_consumption_Lpl": round(_fuel_model.avg_consumption_Lpl, 3),
                "laps_to_empty": round(_fuel_model.laps_to_empty(), 1),
                "laps_until_pit": round(_fuel_model.laps_until_pit(), 1),
                "time_to_pit_s": int(_fuel_model.time_to_pit_seconds(_lap_model.avg_lap_s)),
                "stops_remaining": _fuel_model.stops_remaining(now, _lap_model.avg_lap_s),
            },
            "lap_model": {
                "avg_lap_s": round(_lap_model.avg_lap_s, 2),
                "avg_lap_formatted": format_lap_time(_lap_model.avg_lap_ms),
                "total_laps": int(stats.get("cnt") or 0),
                "fastest_ms": stats.get("fastest"),
                "fastest_formatted": format_lap_time(int(stats["fastest"])) if stats.get("fastest") else None,
            },
            "drivers": drivers,
            "rotation": _stint_calc.drivers,
            "laps": [
                {**lap, "lap_time_formatted": format_lap_time(lap["lap_time_ms"])}
                for lap in laps
            ],
            "alerts": [
                {"id": a.id, "severity": a.severity, "message": a.message, "dismissible": a.dismissible}
                for a in alerts
            ],
            "scraper": _scraper_status,
            "current_lap": int(state.get("current_lap", 0)),
            "standings": {
                "position": int(state["position"]) if state.get("position", "").strip().isdigit() else None,
                "gap_ahead": state.get("gap_ahead") or None,
                "gap_behind": state.get("gap_behind") or None,
                "kart_ahead": state.get("kart_ahead") or None,
                "kart_behind": state.get("kart_behind") or None,
            },
        },
    }


# ── WebSocket connection manager ─────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        self._connections = [c for c in self._connections if c is not ws]

    async def broadcast(self, data: dict):
        msg = json.dumps(data, default=str)
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_text(json.dumps(await _full_state(), default=str))
        while True:
            await asyncio.wait_for(ws.receive_text(), timeout=30)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        manager.disconnect(ws)


# ── Background tasks ─────────────────────────────────────────────────────────

async def _alert_loop():
    while True:
        await asyncio.sleep(60)
        now = datetime.now(utc)
        alerts = _alert_engine.active_alerts(now)
        if alerts:
            await manager.broadcast({
                "type": "alerts",
                "data": [{"id": a.id, "severity": a.severity, "message": a.message, "dismissible": a.dismissible} for a in alerts],
            })


async def _state_broadcast_loop():
    while True:
        await asyncio.sleep(30)
        try:
            await manager.broadcast(await _full_state())
        except Exception as e:
            log.warning(f"State broadcast error: {e}")


# ── App lifecycle ─────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    await db.init_db()
    await _load_state_into_memory()
    asyncio.create_task(_alert_loop())
    asyncio.create_task(_state_broadcast_loop())
    log.info("Daytona 24 dashboard started")


# ── API: Race control ─────────────────────────────────────────────────────────

@app.post("/api/race/start")
async def race_start():
    now = datetime.now(utc)
    await db.set_state_many({"race_started": "1", "race_started_at": now.isoformat()})

    # Open stint #1 for the rotation's current driver if no stint is already open.
    # Without this, the first driver's burn never reaches the fuel EMA — the first
    # Driver Swap has no prior stint to close, so update_from_stint() is skipped.
    current = await db.get_current_stint()
    if current is None:
        driver = _stint_calc.current_driver()
        if driver and driver != "—":
            state = await db.get_state()
            start_lap = int(state.get("current_lap", 0))
            await db.start_stint(
                driver,
                now.isoformat(),
                _fuel_model.current_level_L,
                start_lap,
            )
            await db.set_state("current_driver", driver)

    await manager.broadcast({"type": "race_started", "data": {"started_at": now.isoformat()}})
    await manager.broadcast(await _full_state())
    return {"ok": True}


@app.post("/api/race/reset")
async def race_reset():
    """Wipe every piece of race-generated data while preserving calibration:
    drivers, practice sessions, tank/safety settings, and scraper config stay.
    Laps, stints, fuel fills, lap counter, standings, EMA state, and alert
    dismissals are all cleared."""
    global _scraper_task

    # Stop the scraper — race is being reset, no point pulling old session data
    if _scraper_task and not _scraper_task.done():
        _scraper_task.cancel()
    _scraper_status.update({"running": False, "last_poll": None, "error": None})

    # Wipe race-generated data
    laps_deleted = await db.delete_all_laps()
    stints_deleted = await db.delete_all_stints()
    fills_deleted = await db.delete_all_fuel_fills()

    # Reset race_state keys (preserve tank_capacity_L, safety_margin_laps, scraper config, drivers)
    drivers = await db.get_drivers()
    first_driver = drivers[0]["name"] if drivers else ""
    await db.set_state_many({
        "race_started": "0",
        "race_started_at": "",
        "current_lap": "0",
        "fuel_level_L": str(_fuel_model.tank_capacity_L),
        "current_driver": first_driver,
        "position": "",
        "gap_ahead": "",
        "gap_behind": "",
    })

    # Reset in-memory models
    _fuel_model.current_level_L = _fuel_model.tank_capacity_L
    _lap_model._initialised = False
    _lap_model._ema_ms = 0.0
    _alert_engine.reset()
    _stint_calc.current_index = 0

    # Re-seed fuel model from remaining inputs (practice only — stints are gone)
    await _recompute_fuel_model()

    # Broadcast full state so every connected client snaps to the clean slate
    await manager.broadcast(await _full_state())

    return {
        "ok": True,
        "laps_deleted": laps_deleted,
        "stints_deleted": stints_deleted,
        "fuel_fills_deleted": fills_deleted,
    }


@app.get("/api/state")
async def get_state():
    return await _full_state()


# ── API: Drivers ──────────────────────────────────────────────────────────────

@app.get("/api/drivers")
async def get_drivers():
    return await db.get_drivers()


@app.put("/api/drivers/rotation")
async def update_rotation(body: dict):
    ordered: list[str] = body.get("order", [])
    if not ordered:
        raise HTTPException(400, "order list required")
    await db.update_rotation(ordered)
    _stint_calc.reorder(ordered)
    await manager.broadcast({"type": "rotation_update", "data": {"rotation": ordered}})
    return {"ok": True}


@app.put("/api/drivers/{name}")
async def update_driver(name: str, body: dict):
    weight_kg = body.get("weight_kg")
    pedal_pos = body.get("pedal_pos")
    if weight_kg is not None:
        weight_kg = float(weight_kg)
    ok = await db.update_driver(name, weight_kg, pedal_pos)
    if not ok:
        raise HTTPException(400, "No fields to update")
    await manager.broadcast({"type": "state_update", "data": await _full_state()})
    return {"ok": True}


# ── API: Laps ─────────────────────────────────────────────────────────────────

@app.get("/api/laps")
async def get_laps(limit: int = 50, source: str = "all"):
    laps = await db.get_laps(limit=limit, source=source)
    return [
        {**lap, "lap_time_formatted": format_lap_time(lap["lap_time_ms"])}
        for lap in laps
    ]


@app.post("/api/laps")
async def add_lap(body: dict):
    driver = body.get("driver", "").strip()
    lap_time_str = body.get("lap_time", "").strip()
    flag = body.get("flag", "GREEN").upper()
    is_rain = bool(body.get("is_rain", False))

    if not driver or not lap_time_str:
        raise HTTPException(400, "driver and lap_time required")
    if flag not in ("GREEN", "YELLOW", "SC", "RED"):
        raise HTTPException(400, "flag must be GREEN/YELLOW/SC/RED")

    try:
        lap_time_ms = parse_lap_time(lap_time_str)
    except ValueError as e:
        raise HTTPException(400, str(e))

    now = datetime.now(utc)
    state = await db.get_state()
    current_lap = int(state.get("current_lap", 0)) + 1
    await db.set_state("current_lap", str(current_lap))

    current_stint = await db.get_current_stint()
    stint_id = current_stint["id"] if current_stint else None

    lap_id = await db.insert_lap(
        lap_number=current_lap,
        driver_name=driver,
        lap_time_ms=lap_time_ms,
        flag_condition=flag,
        is_rain=is_rain,
        source="manual",
        recorded_at=now.isoformat(),
        stint_id=stint_id,
    )

    _lap_model.update(lap_time_ms, flag)

    # Burn fuel for this lap
    burn = _fuel_model.avg_consumption_Lpl * FLAG_MULTIPLIERS.get(flag, 1.0)
    _fuel_model.current_level_L = max(0.0, _fuel_model.current_level_L - burn)
    await db.set_state("fuel_level_L", str(_fuel_model.current_level_L))

    lap_record = {
        "id": lap_id,
        "lap_number": current_lap,
        "driver_name": driver,
        "lap_time_ms": lap_time_ms,
        "lap_time_formatted": format_lap_time(lap_time_ms),
        "flag_condition": flag,
        "is_rain": is_rain,
        "source": "manual",
        "recorded_at": now.isoformat(),
    }
    await manager.broadcast({"type": "lap_update", "data": lap_record})

    # Also broadcast updated fuel/timing predictions
    await manager.broadcast({
        "type": "fuel_update",
        "data": {
            "level_L": round(_fuel_model.current_level_L, 2),
            "percent": round(_fuel_model.fuel_percent(), 1),
            "laps_to_empty": round(_fuel_model.laps_to_empty(), 1),
            "laps_until_pit": round(_fuel_model.laps_until_pit(), 1),
            "time_to_pit_s": int(_fuel_model.time_to_pit_seconds(_lap_model.avg_lap_s)),
            "avg_consumption_Lpl": round(_fuel_model.avg_consumption_Lpl, 3),
        },
    })

    # Refresh panel-current totals (laps in kart, total laps, avg lap, time in kart)
    await _broadcast_current_driver()

    return lap_record


@app.delete("/api/laps/{lap_id}")
async def delete_lap(lap_id: int):
    ok = await db.delete_lap(lap_id)
    if not ok:
        raise HTTPException(404, "Lap not found")
    return {"ok": True}


# ── API: Stints ───────────────────────────────────────────────────────────────

@app.get("/api/stints/current")
async def get_current_stint():
    stint = await db.get_current_stint()
    return stint or {}


@app.get("/api/stints")
async def get_stints():
    return await db.get_stints()


@app.post("/api/stints/end")
async def end_stint(body: dict):
    """
    Body fields:
      next_driver       — name of incoming driver (else picks rotation's next_driver())
      swap_lap          — lap the swap takes effect (defaults to current_lap + 1)
      fuel_before_L     — fuel REMAINING in the tank when the kart arrived in the pit.
                          This is what closes the prior stint and feeds the EMA.
      fuel_after_L      — fuel level AFTER refuelling. Becomes the new stint's
                          starting fuel and the live model level.
                          Defaults to a full tank.

      Legacy: `fuel_level_L` is interpreted as `fuel_after_L` if provided alone,
              so older clients keep working (but get the buggy old semantics — please update).
    """
    next_driver = body.get("next_driver", "").strip()
    swap_lap_raw = body.get("swap_lap")
    fuel_before_raw = body.get("fuel_before_L")
    fuel_after_raw = body.get("fuel_after_L", body.get("fuel_level_L"))  # legacy alias

    now = datetime.now(utc)
    state = await db.get_state()
    current_lap = int(state.get("current_lap", 0))

    # Resolve and validate swap_lap (defaults to next lap, must not be in the future)
    if swap_lap_raw is None or str(swap_lap_raw).strip() == "":
        swap_lap = current_lap + 1
    else:
        try:
            swap_lap = int(swap_lap_raw)
        except (ValueError, TypeError):
            raise HTTPException(400, "swap_lap must be an integer")
        if swap_lap < 1:
            raise HTTPException(400, "swap_lap must be >= 1")
        if swap_lap > current_lap + 1:
            raise HTTPException(
                400,
                f"swap_lap cannot be in the future (current lap is {current_lap}, max allowed is {current_lap + 1})",
            )

    # Fuel AFTER refuel → defaults to tank capacity (typical pit-stop scenario)
    if fuel_after_raw is None or str(fuel_after_raw).strip() == "":
        fuel_after = _fuel_model.tank_capacity_L
    else:
        fuel_after = float(fuel_after_raw)
    if fuel_after < 0:
        raise HTTPException(400, "fuel_after_L must be >= 0")

    # Fuel BEFORE refuel → defaults to whatever the model thinks is left right now.
    # This is the value that ends the prior stint and updates the EMA.
    if fuel_before_raw is None or str(fuel_before_raw).strip() == "":
        fuel_before = _fuel_model.current_level_L
    else:
        fuel_before = float(fuel_before_raw)
    if fuel_before < 0:
        raise HTTPException(400, "fuel_before_L must be >= 0")

    # Close prior stint at swap_lap - 1 (the last lap the previous driver completed)
    current = await db.get_current_stint()
    if current:
        prior_end_lap = max(current.get("start_lap") or 0, swap_lap - 1)
        await db.end_stint(current["id"], now.isoformat(), fuel_before, prior_end_lap)

        if current.get("fuel_start_L") is not None:
            laps_in_stint = prior_end_lap - (current.get("start_lap") or 0)
            litres_used = float(current["fuel_start_L"]) - fuel_before
            if laps_in_stint > 0 and litres_used > 0:
                _fuel_model.update_from_stint(litres_used, laps_in_stint)

    # Reset live fuel level to the post-refuel value
    _fuel_model.current_level_L = fuel_after
    await db.set_state("fuel_level_L", str(fuel_after))

    if not next_driver:
        next_driver = _stint_calc.next_driver()

    _stint_calc.reorder(_stint_calc.drivers)
    if next_driver in _stint_calc.drivers:
        _stint_calc.current_index = _stint_calc.drivers.index(next_driver)

    await db.set_state("current_driver", next_driver)
    new_stint_id = await db.start_stint(
        next_driver,
        now.isoformat(),
        fuel_after,
        swap_lap,
    )

    # Retroactively retag any laps the scraper already pulled with the new driver / stint
    reassigned = await db.reassign_laps_from(swap_lap, next_driver, new_stint_id)

    await manager.broadcast({"type": "stint_update", "data": await _full_state()})
    return {
        "ok": True,
        "new_stint_id": new_stint_id,
        "driver": next_driver,
        "swap_lap": swap_lap,
        "reassigned_laps": reassigned,
        "fuel_before_L": fuel_before,
        "fuel_after_L": fuel_after,
    }


# ── API: Fuel ─────────────────────────────────────────────────────────────────

@app.get("/api/fuel")
async def get_fuel():
    now = datetime.now(utc)
    return {
        "level_L": round(_fuel_model.current_level_L, 2),
        "capacity_L": _fuel_model.tank_capacity_L,
        "percent": round(_fuel_model.fuel_percent(), 1),
        "avg_consumption_Lpl": round(_fuel_model.avg_consumption_Lpl, 3),
        "laps_to_empty": round(_fuel_model.laps_to_empty(), 1),
        "laps_until_pit": round(_fuel_model.laps_until_pit(), 1),
        "time_to_pit_s": int(_fuel_model.time_to_pit_seconds(_lap_model.avg_lap_s)),
        "stops_remaining": _fuel_model.stops_remaining(now, _lap_model.avg_lap_s),
        "avg_lap_s": round(_lap_model.avg_lap_s, 2),
    }


@app.post("/api/fuel/fill")
async def fuel_fill(body: dict):
    litres_added = float(body.get("litres_added", 0))
    fuel_level_L = body.get("fuel_level_L")
    now = datetime.now(utc)
    state = await db.get_state()
    current_lap = int(state.get("current_lap", 0))

    if fuel_level_L is not None:
        _fuel_model.current_level_L = float(fuel_level_L)
    else:
        _fuel_model.current_level_L = min(
            _fuel_model.tank_capacity_L,
            _fuel_model.current_level_L + litres_added
        )

    await db.set_state("fuel_level_L", str(_fuel_model.current_level_L))
    fill_id = await db.insert_fuel_fill(
        now.isoformat(), litres_added,
        float(fuel_level_L) if fuel_level_L else _fuel_model.current_level_L,
        current_lap,
    )

    await manager.broadcast({
        "type": "fuel_update",
        "data": {
            "level_L": round(_fuel_model.current_level_L, 2),
            "percent": round(_fuel_model.fuel_percent(), 1),
            "laps_to_empty": round(_fuel_model.laps_to_empty(), 1),
            "laps_until_pit": round(_fuel_model.laps_until_pit(), 1),
        },
    })
    return {"ok": True, "fill_id": fill_id, "fuel_level_L": _fuel_model.current_level_L}


@app.put("/api/fuel/settings")
async def fuel_settings(body: dict):
    if "tank_capacity_L" in body:
        _fuel_model.tank_capacity_L = float(body["tank_capacity_L"])
        await db.set_state("tank_capacity_L", str(_fuel_model.tank_capacity_L))
    if "safety_margin_laps" in body:
        _fuel_model.safety_margin_laps = int(body["safety_margin_laps"])
        await db.set_state("safety_margin_laps", str(_fuel_model.safety_margin_laps))
    return {"ok": True}


# ── API: Practice sessions ────────────────────────────────────────────────────

@app.get("/api/practice")
async def get_practice():
    return await db.get_practice_sessions()


@app.post("/api/practice")
async def add_practice(body: dict):
    driver = body.get("driver_name", "").strip()
    fuel_start = float(body.get("fuel_start_L", 0))
    fuel_end = float(body.get("fuel_end_L", 0))
    laps = int(body.get("laps_completed", 0))
    avg_ms = body.get("avg_lap_time_ms")
    flag = body.get("flag_condition", "GREEN").upper()
    notes = body.get("notes")
    now = datetime.now(utc)

    if not driver or laps <= 0:
        raise HTTPException(400, "driver_name and laps_completed required")

    session_id = await db.insert_practice_session(
        driver, fuel_start, fuel_end, laps,
        int(avg_ms) if avg_ms else None,
        flag, notes, now.isoformat(),
    )

    # Update fuel model immediately
    litres_used = fuel_start - fuel_end
    if litres_used > 0:
        _fuel_model.update_from_stint(litres_used, laps, flag)

    return {"ok": True, "session_id": session_id}


@app.get("/api/practice/consumption")
async def practice_consumption():
    sessions = await db.get_practice_sessions()
    if not sessions:
        return {"avg_consumption_Lpl": DEFAULT_CONSUMPTION_LPL_VALUE, "sessions": 0}
    total_laps, total_litres = 0, 0.0
    for s in sessions:
        used = s["fuel_start_L"] - s["fuel_end_L"]
        if s["laps_completed"] > 0 and used > 0:
            total_laps += s["laps_completed"]
            total_litres += used
    if total_laps == 0:
        return {"avg_consumption_Lpl": None, "sessions": len(sessions)}
    return {
        "avg_consumption_Lpl": round(total_litres / total_laps, 4),
        "sessions": len(sessions),
        "total_laps": total_laps,
        "total_litres": round(total_litres, 3),
        "current_model_Lpl": round(_fuel_model.avg_consumption_Lpl, 4),
    }


DEFAULT_CONSUMPTION_LPL_VALUE = 0.35


# ── API: Fuel model inputs (edit/delete + breakdown) ─────────────────────────

@app.put("/api/practice/{session_id}")
async def edit_practice(session_id: int, body: dict):
    # Coerce numeric fields if present
    if "fuel_start_L" in body: body["fuel_start_L"] = float(body["fuel_start_L"])
    if "fuel_end_L" in body:   body["fuel_end_L"]   = float(body["fuel_end_L"])
    if "laps_completed" in body: body["laps_completed"] = int(body["laps_completed"])
    if "avg_lap_time_ms" in body and body["avg_lap_time_ms"] not in (None, ""):
        body["avg_lap_time_ms"] = int(body["avg_lap_time_ms"])
    if "flag_condition" in body: body["flag_condition"] = str(body["flag_condition"]).upper()
    ok = await db.update_practice_session(session_id, body)
    if not ok:
        raise HTTPException(404, "practice session not found or no editable fields supplied")
    await _recompute_fuel_model()
    await manager.broadcast(await _full_state())
    return {"ok": True}


@app.delete("/api/practice/{session_id}")
async def remove_practice(session_id: int):
    ok = await db.delete_practice_session(session_id)
    if not ok:
        raise HTTPException(404, "practice session not found")
    await _recompute_fuel_model()
    await manager.broadcast(await _full_state())
    return {"ok": True}


@app.put("/api/stints/{stint_id}")
async def edit_stint(stint_id: int, body: dict):
    if "fuel_start_L" in body and body["fuel_start_L"] not in (None, ""): body["fuel_start_L"] = float(body["fuel_start_L"])
    if "fuel_end_L" in body and body["fuel_end_L"] not in (None, ""):     body["fuel_end_L"]   = float(body["fuel_end_L"])
    if "start_lap" in body and body["start_lap"] not in (None, ""):       body["start_lap"]    = int(body["start_lap"])
    if "end_lap" in body and body["end_lap"] not in (None, ""):           body["end_lap"]      = int(body["end_lap"])
    ok = await db.update_stint(stint_id, body)
    if not ok:
        raise HTTPException(404, "stint not found or no editable fields supplied")
    await _recompute_fuel_model()
    await manager.broadcast(await _full_state())
    return {"ok": True}


@app.delete("/api/stints/{stint_id}")
async def remove_stint(stint_id: int):
    laps_deleted = await db.delete_stint(stint_id)
    await _recompute_fuel_model()
    await manager.broadcast(await _full_state())
    return {"ok": True, "laps_deleted": laps_deleted}


@app.get("/api/fuel/breakdown")
async def fuel_breakdown():
    """Per-input contribution to the EMA + the live math behind laps-to-pit."""
    now = datetime.now(utc)
    practice = await db.get_practice_sessions()
    stints = await db.get_stints()

    practice_out = []
    for s in practice:
        used = (s.get("fuel_start_L") or 0) - (s.get("fuel_end_L") or 0)
        laps = s.get("laps_completed") or 0
        flag = s.get("flag_condition") or "GREEN"
        raw = used / laps if laps > 0 and used > 0 else None
        mult = FLAG_MULTIPLIERS.get(flag, 1.0)
        norm = (raw / mult) if (raw is not None and mult > 0) else None
        practice_out.append({
            **s,
            "raw_Lpl": round(raw, 4) if raw is not None else None,
            "normalised_Lpl": round(norm, 4) if norm is not None else None,
        })

    stints_out = []
    for s in stints:
        if not s.get("ended_at"):
            continue   # exclude the currently open stint
        if s.get("fuel_start_L") is None or s.get("fuel_end_L") is None:
            continue
        laps = (s.get("end_lap") or 0) - (s.get("start_lap") or 0)
        used = float(s["fuel_start_L"]) - float(s["fuel_end_L"])
        raw = (used / laps) if laps > 0 and used > 0 else None
        stints_out.append({
            **s,
            "laps": laps,
            "raw_Lpl": round(raw, 4) if raw is not None else None,
            "normalised_Lpl": round(raw, 4) if raw is not None else None,  # stints assumed GREEN → no scaling
        })

    # Current flag is whatever the most recent lap reports; fall back to GREEN
    laps_recent = await db.get_laps(limit=1)
    current_flag = laps_recent[0]["flag_condition"] if laps_recent else "GREEN"
    flag_mult = FLAG_MULTIPLIERS.get(current_flag, 1.0)
    consumption = _fuel_model.avg_consumption_Lpl
    effective = consumption * flag_mult

    return {
        "inputs": {"practice": practice_out, "stints": stints_out},
        "ema": {
            "alpha": _fuel_model.alpha,
            "current_Lpl": round(consumption, 4),
            "default_Lpl_if_uninitialised": DEFAULT_CONSUMPTION_LPL_VALUE,
            "initialised": _fuel_model._initialised,
        },
        "calculation": {
            "current_level_L": round(_fuel_model.current_level_L, 3),
            "tank_capacity_L": _fuel_model.tank_capacity_L,
            "consumption_Lpl": round(consumption, 4),
            "current_flag": current_flag,
            "flag_multiplier": flag_mult,
            "effective_Lpl": round(effective, 4),
            "laps_to_empty": round(_fuel_model.laps_to_empty(current_flag), 2),
            "safety_margin_laps": _fuel_model.safety_margin_laps,
            "laps_until_pit": round(_fuel_model.laps_until_pit(current_flag), 2),
            "avg_lap_s": round(_lap_model.avg_lap_s, 2),
            "time_to_pit_s": int(_fuel_model.time_to_pit_seconds(_lap_model.avg_lap_s, current_flag)),
            "stops_remaining": _fuel_model.stops_remaining(now, _lap_model.avg_lap_s),
            "race_time_remaining_s": int(max(0, (RACE_END - now).total_seconds())),
        },
    }


# ── API: Scraper ──────────────────────────────────────────────────────────────

@app.get("/api/scraper/status")
async def scraper_status():
    return _scraper_status


@app.post("/api/scraper/start")
async def scraper_start(body: dict):
    global _scraper_task
    from scraper import SpeedHiveScraper

    session_url = body.get("session_url")
    team_number = body.get("team_number")

    if not session_url and not team_number:
        raise HTTPException(400, "session_url or team_number required")

    if _scraper_task and not _scraper_task.done():
        return {"ok": False, "message": "Scraper already running"}

    _scraper_status.update({"session_url": session_url, "team_number": team_number, "error": None})
    if session_url:
        await db.set_state("speedhive_session_url", session_url)
    if team_number:
        await db.set_state("speedhive_team_number", str(team_number))

    scraper = SpeedHiveScraper(
        session_url=session_url,
        team_number=str(team_number) if team_number else None,
    )

    async def on_new_lap(lap_record: dict):
        now = datetime.now(utc)
        state = await db.get_state()
        current_lap = int(state.get("current_lap", 0)) + 1
        await db.set_state("current_lap", str(current_lap))
        current_stint = await db.get_current_stint()
        # Strategy app is the source of truth for who is driving — the scraper only
        # sees the team/kart entry on SpeedHive, not the actual person in the seat.
        actual_driver = _stint_calc.current_driver()
        if not actual_driver or actual_driver == "—":
            actual_driver = lap_record.get("driver_name", "unknown")
        lap_id = await db.insert_lap(
            lap_number=lap_record.get("lap_number", current_lap),
            driver_name=actual_driver,
            lap_time_ms=lap_record["lap_time_ms"],
            flag_condition=lap_record.get("flag_condition", "GREEN"),
            is_rain=lap_record.get("is_rain", False),
            source="speedhive",
            recorded_at=now.isoformat(),
            stint_id=current_stint["id"] if current_stint else None,
        )
        # Reflect the real driver back into the broadcast payload for clients
        lap_record = {**lap_record, "driver_name": actual_driver}
        flag = lap_record.get("flag_condition", "GREEN")
        _lap_model.update(lap_record["lap_time_ms"], flag)

        burn = _fuel_model.avg_consumption_Lpl * FLAG_MULTIPLIERS.get(flag, 1.0)
        _fuel_model.current_level_L = max(0.0, _fuel_model.current_level_L - burn)
        await db.set_state("fuel_level_L", str(_fuel_model.current_level_L))

        _scraper_status["last_poll"] = now.isoformat()
        await manager.broadcast({
            "type": "lap_update",
            "data": {
                "id": lap_id,
                **lap_record,
                "lap_time_formatted": format_lap_time(lap_record["lap_time_ms"]),
                "source": "speedhive",
            },
        })
        await manager.broadcast({
            "type": "fuel_update",
            "data": {
                "level_L": round(_fuel_model.current_level_L, 2),
                "percent": round(_fuel_model.fuel_percent(), 1),
                "laps_to_empty": round(_fuel_model.laps_to_empty(), 1),
                "laps_until_pit": round(_fuel_model.laps_until_pit(), 1),
                "time_to_pit_s": int(_fuel_model.time_to_pit_seconds(_lap_model.avg_lap_s)),
                "avg_consumption_Lpl": round(_fuel_model.avg_consumption_Lpl, 3),
            },
        })
        await _broadcast_current_driver()

    scraper.on_new_lap = on_new_lap

    async def on_standings_update(standings: dict):
        await db.set_state_many({
            "position": str(standings.get("position", "")),
            "gap_ahead": str(standings.get("gap_ahead", "")),
            "gap_behind": str(standings.get("gap_behind", "")),
            "kart_ahead": str(standings.get("kart_ahead") or ""),
            "kart_behind": str(standings.get("kart_behind") or ""),
        })
        await manager.broadcast({"type": "standings_update", "data": standings})

    scraper.on_standings_update = on_standings_update

    async def run():
        _scraper_status["running"] = True
        try:
            await scraper.run_loop()
        except Exception as e:
            _scraper_status["error"] = str(e)
            log.error(f"Scraper failed: {e}")
        finally:
            _scraper_status["running"] = False

    _scraper_task = asyncio.create_task(run())
    return {"ok": True}


@app.post("/api/standings")
async def update_standings(body: dict):
    """Manual override for position, gap_ahead, gap_behind, kart_ahead, kart_behind."""
    position = body.get("position")
    gap_ahead = body.get("gap_ahead")
    gap_behind = body.get("gap_behind")
    kart_ahead = body.get("kart_ahead")
    kart_behind = body.get("kart_behind")

    updates: dict[str, str] = {}
    if position is not None and str(position).strip():
        try:
            updates["position"] = str(int(position))
        except ValueError:
            raise HTTPException(400, "position must be an integer")
    if gap_ahead is not None:
        updates["gap_ahead"] = str(gap_ahead).strip()
    if gap_behind is not None:
        updates["gap_behind"] = str(gap_behind).strip()
    if kart_ahead is not None:
        updates["kart_ahead"] = str(kart_ahead).strip().lstrip("#")
    if kart_behind is not None:
        updates["kart_behind"] = str(kart_behind).strip().lstrip("#")

    if not updates:
        raise HTTPException(400, "no fields to update")

    await db.set_state_many(updates)
    standings = {
        "position": int(updates["position"]) if "position" in updates else None,
        "gap_ahead": updates.get("gap_ahead"),
        "gap_behind": updates.get("gap_behind"),
        "kart_ahead": updates.get("kart_ahead"),
        "kart_behind": updates.get("kart_behind"),
    }
    await manager.broadcast({"type": "standings_update", "data": standings})
    return {"ok": True, **standings}


@app.post("/api/scraper/stop")
async def scraper_stop():
    global _scraper_task
    if _scraper_task and not _scraper_task.done():
        _scraper_task.cancel()
    _scraper_status["running"] = False
    return {"ok": True}


# ── API: Alerts ───────────────────────────────────────────────────────────────

@app.get("/api/alerts")
async def get_alerts():
    now = datetime.now(utc)
    alerts = _alert_engine.active_alerts(now)
    return [{"id": a.id, "severity": a.severity, "message": a.message, "dismissible": a.dismissible} for a in alerts]


@app.post("/api/alerts/dismiss/{alert_id}")
async def dismiss_alert(alert_id: str):
    _alert_engine.dismiss(alert_id)
    return {"ok": True}


@app.post("/api/alerts/maintenance-done/{stop_id}")
async def maintenance_done(stop_id: str):
    if stop_id not in ("maint1", "maint2"):
        raise HTTPException(400, "stop_id must be maint1 or maint2")
    _alert_engine.mark_maintenance_done(stop_id)
    return {"ok": True}
