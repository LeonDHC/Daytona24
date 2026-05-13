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
    _fuel_model.tank_capacity_L = float(state.get("tank_capacity_L", 5.5))
    _fuel_model.current_level_L = float(state.get("fuel_level_L", 5.5))
    _fuel_model.safety_margin_laps = int(state.get("safety_margin_laps", 2))

    drivers = await db.get_drivers()
    _stint_calc.drivers = [d["name"] for d in drivers]

    # Seed lap time model from existing laps
    laps = await db.get_laps(limit=200)
    for lap in laps:
        _lap_model.update(lap["lap_time_ms"], lap["flag_condition"])

    # Seed fuel model from practice sessions
    practice = await db.get_practice_sessions()
    _fuel_model.update_from_practice(practice)

    # Restore current driver index
    current = state.get("current_driver", "")
    if current and current in _stint_calc.drivers:
        _stint_calc.current_index = _stint_calc.drivers.index(current)


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
    await manager.broadcast({"type": "race_started", "data": {"started_at": now.isoformat()}})
    return {"ok": True}


@app.post("/api/race/reset")
async def race_reset():
    await db.set_state_many({"race_started": "0", "current_lap": "0", "fuel_level_L": str(_fuel_model.tank_capacity_L)})
    _fuel_model.current_level_L = _fuel_model.tank_capacity_L
    _lap_model._initialised = False
    return {"ok": True}


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
            "laps_to_empty": round(_fuel_model.laps_to_empty(), 1),
            "laps_until_pit": round(_fuel_model.laps_until_pit(), 1),
            "time_to_pit_s": int(_fuel_model.time_to_pit_seconds(_lap_model.avg_lap_s)),
            "percent": round(_fuel_model.fuel_percent(), 1),
            "avg_consumption_Lpl": round(_fuel_model.avg_consumption_Lpl, 3),
        },
    })

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
    next_driver = body.get("next_driver", "").strip()
    fuel_level_L = body.get("fuel_level_L")

    now = datetime.now(utc)
    state = await db.get_state()
    current_lap = int(state.get("current_lap", 0))

    current = await db.get_current_stint()
    if current:
        fuel_end = float(fuel_level_L) if fuel_level_L is not None else None
        await db.end_stint(current["id"], now.isoformat(), fuel_end, current_lap)

        if current.get("fuel_start_L") is not None and fuel_end is not None:
            laps_in_stint = current_lap - (current.get("start_lap") or 0)
            litres_used = float(current["fuel_start_L"]) - fuel_end
            if laps_in_stint > 0 and litres_used > 0:
                _fuel_model.update_from_stint(litres_used, laps_in_stint)

    if fuel_level_L is not None:
        _fuel_model.current_level_L = float(fuel_level_L)
        await db.set_state("fuel_level_L", str(fuel_level_L))

    if not next_driver:
        next_driver = _stint_calc.next_driver()

    _stint_calc.reorder(_stint_calc.drivers)
    if next_driver in _stint_calc.drivers:
        _stint_calc.current_index = _stint_calc.drivers.index(next_driver)

    await db.set_state("current_driver", next_driver)
    new_stint_id = await db.start_stint(
        next_driver,
        now.isoformat(),
        float(fuel_level_L) if fuel_level_L is not None else None,
        current_lap,
    )

    await manager.broadcast({"type": "stint_update", "data": await _full_state()})
    return {"ok": True, "new_stint_id": new_stint_id, "driver": next_driver}


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
        lap_id = await db.insert_lap(
            lap_number=lap_record.get("lap_number", current_lap),
            driver_name=lap_record.get("driver_name", "unknown"),
            lap_time_ms=lap_record["lap_time_ms"],
            flag_condition=lap_record.get("flag_condition", "GREEN"),
            is_rain=lap_record.get("is_rain", False),
            source="speedhive",
            recorded_at=now.isoformat(),
            stint_id=current_stint["id"] if current_stint else None,
        )
        _lap_model.update(lap_record["lap_time_ms"], lap_record.get("flag_condition", "GREEN"))
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

    scraper.on_new_lap = on_new_lap

    async def on_standings_update(standings: dict):
        await db.set_state_many({
            "position": str(standings.get("position", "")),
            "gap_ahead": str(standings.get("gap_ahead", "")),
            "gap_behind": str(standings.get("gap_behind", "")),
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
    """Manual override for position / gap_ahead / gap_behind (pit-board entry)."""
    position = body.get("position")
    gap_ahead = body.get("gap_ahead")
    gap_behind = body.get("gap_behind")

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

    if not updates:
        raise HTTPException(400, "no fields to update")

    await db.set_state_many(updates)
    standings = {
        "position": int(updates["position"]) if "position" in updates else None,
        "gap_ahead": updates.get("gap_ahead"),
        "gap_behind": updates.get("gap_behind"),
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
