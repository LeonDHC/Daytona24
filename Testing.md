# Daytona 24 Race Strategy Dashboard

## Overview

A real-time pit-wall dashboard for a DMAX team competing in the Daytona 24 Hours at Milton Keynes (23–24 May 2026). Runs on a single laptop, accessed via browser. No internet required after initial load (except for SpeedHive scraping and the rain radar iframe).

**What it does:**
- Displays current driver, next driver, ballast/pedal requirements, and stint timer
- Predicts laps and time until fuel stop using a live-calibrated fuel model
- Tracks driver rotation with drag-and-drop reordering
- Records lap times manually or by scraping SpeedHive live timing
- Fires regulation alerts (visor change at 21:00, maintenance stop windows)
- Shows a rain radar for Milton Keynes
- Persists all data to SQLite — survives restarts

---

## Starting the App

```bash
cd Daytona24
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000
# Open http://localhost:8000 in a browser
```

Use `--reload` during development to auto-restart on file changes. The `--host 0.0.0.0` flag makes it reachable from other devices on the same WiFi (e.g. a phone on the pit wall).

---

## File Structure

```
Daytona24/
├── main.py              FastAPI application — routes, WebSocket, background tasks
├── Race.py              Pure Python calculation engine (fuel, stints, alerts)
├── database.py          SQLite schema and all async DB functions
├── scraper.py           SpeedHive live timing scraper
├── RunPlan.ini          Driver roster and stint limit configuration
├── race_data.db         SQLite database (created on first run, gitignore this)
├── requirements.txt     Python dependencies
├── venv/                Virtual environment (gitignore this)
└── static/
    ├── index.html       Single-page dashboard HTML
    ├── style.css        Dark theme, CSS Grid layout
    └── app.js           Frontend JavaScript (WebSocket, Chart.js, modals)
```

---

## Configuration — RunPlan.ini

```ini
[TESTING]
drivers = leo, lui, fre, chr, ben, mar
limit = 180
```

- **`drivers`** — comma-separated names in initial rotation order. These are seeded into the database on first run (`INSERT OR IGNORE` — changing this after the DB exists has no effect unless you delete `race_data.db`).
- **`limit`** — maximum stint length in minutes (180 = 3 hours). The stint timer on the dashboard turns orange at 90 min and red at 150 min as a fatigue warning.

To reset the database and re-seed from the INI file: `rm race_data.db` then restart the app.

---

## Race.py — Calculation Engine

This file contains no FastAPI code. All calculations are pure Python and independently testable.

### Key Constants (all UTC — BST = UTC+1)

```python
RACE_START   = datetime(2026, 5, 23, 12, 0, 0, tzinfo=utc)  # 13:00 BST
RACE_END     = datetime(2026, 5, 24, 12, 0, 0, tzinfo=utc)  # 13:00 BST
MAINT_STOP_1 = datetime(2026, 5, 23, 20, 0, 0, tzinfo=utc)  # 21:00 BST
MAINT_STOP_2 = datetime(2026, 5, 24,  5, 0, 0, tzinfo=utc)  # 06:00 BST
VISOR_START  = datetime(2026, 5, 23, 20, 0, 0, tzinfo=utc)  # 21:00 BST
VISOR_END    = datetime(2026, 5, 24,  4, 30, 0, tzinfo=utc) # 05:30 BST
```

To adjust for a different race date or different regulations, change these constants. Everything else derives from them automatically.

### Flag Multipliers

```python
FLAG_MULTIPLIERS = {
    "GREEN":  1.0,
    "YELLOW": 0.82,   # slower = 18% less fuel burned per lap
    "SC":     0.70,   # safety car = 30% less fuel per lap
    "RED":    0.0,    # stopped, no consumption
}
```

These affect how much fuel is predicted to be consumed per lap under each flag condition. The multipliers also normalise practice data: a yellow-flag practice stint is converted to a GREEN-equivalent rate before updating the model, so mixed-condition testing doesn't distort the baseline.

To tune these (e.g. if observed SC fuel usage is different), edit `FLAG_MULTIPLIERS` in `Race.py`.

### FuelModel

The fuel model uses an **Exponential Moving Average (EMA)** with `alpha = 0.3`. This means recent stints are weighted more heavily than earlier ones, but the model doesn't overreact to a single unusual lap.

```
normalised_Lpl = raw_Lpl / FLAG_MULTIPLIERS[flag]
ema_new = 0.3 * normalised_Lpl + 0.7 * ema_previous
```

Key methods:
- `update_from_practice(sessions)` — seed model from Friday testing records
- `update_from_stint(litres_used, laps, flag)` — update after each pit stop when fuel_start and fuel_end are known
- `laps_to_empty(flag)` — how many laps at current fuel level and flag condition
- `laps_until_pit(flag)` — laps_to_empty minus the safety margin (default: 2 laps)
- `time_to_pit_seconds(avg_lap_s, flag)` — laps_until_pit × rolling average lap time
- `stops_remaining(now, avg_lap_s)` — total additional fuel fills needed to finish the race

**Default consumption before any data:** `0.35 L/lap`. This is used until at least one practice session or stint is recorded. After that, the EMA takes over.

To change the default: edit `DEFAULT_CONSUMPTION_LPL = 0.35` in `Race.py`.
To change the smoothing factor: edit `alpha = 0.3` in the `FuelModel` dataclass.
To change the safety margin: adjust via the Settings panel in the UI, or edit `safety_margin_laps = 2` in the dataclass default.

### Ballast Calculation

DMAX minimum weight is 219 kg (134 kg kart estimate + 85 kg driver component).

```python
ballast_required(driver_weight_kg) = max(0, 85.0 - driver_weight_kg)
```

A 78 kg driver needs 7 kg of ballast. A 90 kg driver needs none. The dashboard shows the ballast required for the current driver and the signed change needed for the next driver (e.g. "+5.0 kg" means add 5 kg before next driver gets in).

To change the target driver weight (the 85 kg figure): edit the `85.0` constant in `ballast_required()` and `ballast_delta()` in `Race.py`.

### AlertEngine

Alerts fire based on the current UTC time. The alert lifecycle:

| Alert ID | Triggers At | Duration | Severity |
|---|---|---|---|
| `visor_warn` | 20:30 UTC (30 min before mandatory) | 30 min | WARNING |
| `visor_on` | 20:00 UTC | Until 04:30 UTC | CRITICAL |
| `maint1_warn` | 19:30 UTC | 30 min | WARNING |
| `maint1_now` | 20:00 UTC | 10 min | CRITICAL |
| `maint2_warn` | 04:30 UTC | 30 min | WARNING |
| `maint2_now` | 05:00 UTC | 10 min | CRITICAL |
| `fuel_close_warn` | 14 min before RACE_END | 15 min | WARNING |
| `fuel_closed` | 15 min before RACE_END | — | INFO |

CRITICAL alerts (visor_on, maint1_now, maint2_now) are **not dismissible** — they show as a pulsing full-width banner at the top of the screen regardless of the alerts panel. WARNING alerts are dismissible per-session (dismissals are not persisted to the database; they reset on app restart).

To mark a maintenance stop as done (clears its alert permanently for the session): click "✓ Stop 1 Done" / "✓ Stop 2 Done" in the Maintenance panel, or call `POST /api/alerts/maintenance-done/maint1`.

---

## database.py — Persistence Layer

All data is stored in `race_data.db` (SQLite). The file is created automatically on first run. Deleting it resets everything to defaults.

### Tables

**`drivers`** — one row per driver, persists weights, pedal positions, and rotation order.

**`laps`** — every lap ever recorded. `source` is either `"manual"` or `"speedhive"`. `flag_condition` is `GREEN`, `YELLOW`, `SC`, or `RED`. `lap_time_ms` is always in milliseconds for precision. `stint_id` links to the stint the driver was on.

**`stints`** — one row per stint (one driver's continuous time in the kart). `ended_at` is NULL for the current active stint. `fuel_start_L` and `fuel_end_L` are set during driver swap; when both are present, the fuel model updates its EMA from this stint's real consumption data.

**`fuel_fills`** — every refuel event, with litres added and total level after fill.

**`practice_sessions`** — Friday testing records. Each row: driver, fuel_start, fuel_end, laps_completed, average lap time, flag condition. These seed the fuel model at startup.

**`race_state`** — key-value store for settings and live state. Default values:

| Key | Default | Meaning |
|---|---|---|
| `tank_capacity_L` | `10.0` | Full tank volume |
| `safety_margin_laps` | `2` | Laps before empty to trigger pit |
| `fuel_level_L` | `10.0` | Current estimated fuel level |
| `current_lap` | `0` | Running lap counter |
| `race_started` | `0` | Whether race has been started |
| `scraper_enabled` | `0` | Scraper on/off flag |
| `current_driver` | — | Name of driver currently in kart |

The `tank_capacity_L` default of 10.0 is an estimate. **Measure your actual tank before the race** and update it via Settings → Tank Capacity, or `PUT /api/fuel/settings` with `{"tank_capacity_L": X}`.

---

## main.py — Application Server

FastAPI with uvicorn. Handles all HTTP routes and WebSocket connections.

### In-Memory State

Four objects are kept in memory and updated on every relevant event:

- `_fuel_model` (FuelModel) — tracks current fuel level and EMA consumption
- `_lap_model` (LapTimeModel) — rolling EMA of lap times (used to convert laps→time predictions)
- `_stint_calc` (StintCalculator) — ordered driver rotation, current index
- `_alert_engine` (AlertEngine) — tracks dismissed alerts and maintenance done flags

On startup, these are seeded from the database (existing laps, practice sessions, race_state). On app restart, all state is recovered from SQLite — the only thing lost on restart is in-memory dismissals of WARNING alerts.

### Real-Time Updates (WebSocket)

All connected browser tabs receive pushed updates via WebSocket at `/ws`. On connect, the server immediately sends a `full_state` message containing everything the dashboard needs. Thereafter it receives targeted updates:

| Message type | When sent |
|---|---|
| `full_state` | On connect, every 30 seconds, after driver swap |
| `lap_update` | Every new lap (manual or scraped) |
| `fuel_update` | After every lap, fuel fill, or fuel setting change |
| `stint_update` | After driver swap (contains a fresh `full_state`) |
| `rotation_update` | After drag-and-drop reorder |
| `alerts` | Every 60 seconds if any alerts are active |
| `race_started` | When Start Race is clicked |

The client reconnects automatically with exponential backoff if the WebSocket drops.

### API Routes Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Serve dashboard HTML |
| `WS` | `/ws` | WebSocket push channel |
| `GET` | `/api/state` | Full state snapshot (JSON) |
| `POST` | `/api/race/start` | Mark race as started |
| `POST` | `/api/race/reset` | Reset lap counter and fuel (keep practice data) |
| `GET` | `/api/drivers` | List all drivers |
| `PUT` | `/api/drivers/{name}` | Update weight and/or pedal position |
| `PUT` | `/api/drivers/rotation` | Reorder rotation `{"order": ["leo","lui",...]}` |
| `GET` | `/api/laps` | Recent laps `?limit=50&source=all\|manual\|speedhive` |
| `POST` | `/api/laps` | Add manual lap `{"driver","lap_time","flag","is_rain"}` |
| `DELETE` | `/api/laps/{id}` | Remove a lap entry |
| `GET` | `/api/stints/current` | Current open stint |
| `GET` | `/api/stints` | All stint history |
| `POST` | `/api/stints/end` | Driver swap `{"next_driver","fuel_level_L"}` |
| `GET` | `/api/fuel` | Fuel model state |
| `POST` | `/api/fuel/fill` | Record refuel `{"litres_added","fuel_level_L"}` |
| `PUT` | `/api/fuel/settings` | Set tank capacity / safety margin |
| `GET` | `/api/practice` | List practice sessions |
| `POST` | `/api/practice` | Add practice session |
| `GET` | `/api/practice/consumption` | Computed L/lap from all practice data |
| `GET` | `/api/scraper/status` | Scraper running/stopped/error |
| `POST` | `/api/scraper/start` | Start scraper `{"session_url","team_number"}` |
| `POST` | `/api/scraper/stop` | Stop scraper |
| `GET` | `/api/alerts` | Active alerts |
| `POST` | `/api/alerts/dismiss/{id}` | Dismiss a warning alert |
| `POST` | `/api/alerts/maintenance-done/{maint1\|maint2}` | Mark stop as complete |

---

## scraper.py — SpeedHive Live Timing

The scraper polls SpeedHive every 30 seconds for new laps. It uses a two-path approach:

**Path 1 — httpx API (preferred):** Attempts to call SpeedHive's backend REST endpoints directly with browser-like headers. If a JSON response is returned, it is parsed. This is fast and lightweight.

```
GET https://speedhive.mylaps.com/api/v1/sessions/{sessionId}/results
GET https://speedhive.mylaps.com/Sessions/{sessionId}/TimingData
```

The session ID is extracted from the URL you paste (the numeric component from the path, e.g. `/Sessions/123456/` → `123456`).

**Path 2 — Playwright DOM scraping (fallback):** If the API endpoints return non-JSON or fail, a headless Chromium browser loads the full page, extracts table rows via JavaScript `evaluate()`, and parses lap times from the DOM. This is slower and requires `playwright install chromium` to have been run once.

**Team number filter:** If a team number is provided, only rows where the team number appears in the driver/participant field are kept. Without a filter, all laps from the session are ingested.

**Scraper status** is shown in the header as a coloured dot: green (running), red (error), grey (stopped). If the scraper fails, manual entry continues unaffected.

To install Playwright browsers (needed only for the DOM fallback):
```bash
source venv/bin/activate
playwright install chromium
```

---

## Dashboard UI — Panel Reference

The dashboard is a CSS Grid with nine fixed panels. All times display in BST using the browser's locale (`Europe/London` timezone).

### Current Driver (top-left)
Shows: driver name (large green text), time in kart (stopwatch, updates every second), ballast required, pedal position, body weight.

The stint timer colour changes:
- White → normal
- Orange → over 90 minutes (approaching fatigue)
- Red → over 150 minutes (consider swap regardless of fuel)

### Next Driver (top, second)
Shows: upcoming driver name (blue), ballast change needed (colour-coded badge), their required pedal position and weight. "Swap in X laps / HH:MM:SS" is derived from the fuel model — this is when the pit stop is predicted.

The ballast delta badge:
- Green "ADD +X kg" — next driver is lighter, needs more ballast
- Red "REMOVE -X kg" — next driver is heavier, remove ballast
- Blue "±0 kg" — no change needed

### Fuel (top, third)
Shows: vertical CSS fuel bar (colour changes green → yellow at 40% → red at 20%), laps until pit stop, estimated fuel level in litres and %, time until pit, consumption rate in L/lap, stops remaining for the race, rolling average lap time.

### Alerts (top-right)
Shows all active regulation and operational alerts. WARNING alerts have a dismiss button (✕). CRITICAL alerts (visor, maintenance window) cannot be dismissed from this panel — they also appear as a pulsing full-width banner above the grid.

### Recent Laps (middle-left, spanning two columns)
Table of the last 20 laps. Newest at top. Columns: lap number, driver, time, flag badge (colour-coded), rain indicator, source (M = manual, SH = SpeedHive). Fastest GREEN-flag lap is highlighted in purple.

### Lap Time Chart (middle-right, spanning two columns)
Chart.js line chart of the last 40 laps. Point colour matches flag condition (green/yellow/orange/red). A second dashed line shows the rolling EMA average. Animation is disabled for performance during live updates.

### Driver Rotation (bottom-left)
Ordered list of all 6 drivers. Current driver has a green border and "IN" badge. Next driver has a blue border and "NEXT" badge. Drag the ⠿ handle to reorder — the new order is saved immediately to the database and broadcast to all connected clients.

### Maintenance Stops (bottom, centre)
Two countdown timers (Stop 1: 21:00 BST, Stop 2: 06:00 BST). Each turns yellow at T-30 min and red/pulsing when the window is open. After clicking "✓ Stop X Done", the alert clears for the rest of the session.

### Rain Radar (bottom-right)
Windy.com iframe centred on Milton Keynes (52.04°N, 0.76°W) showing the rain overlay. No API key required. To change location or zoom, edit the `<iframe src="...">` in `static/index.html`.

---

## UI Buttons and Modals

**+ Lap** — Manual lap entry. Fields: driver (dropdown), lap time (`m:ss.xxx` format — e.g. `0:54.876` or `1:02.500`), flag condition, rain checkbox. The lap counter increments automatically. After submission the chart and laps table update instantly.

**Driver Swap** — Records end of current stint and start of next. Pre-populates with the next driver in rotation. Enter fuel level after refuel (litres) to update the fuel model and start a new fuel fill record. Leaving fuel blank still completes the swap.

**Fuel Fill** — Records a standalone refuel without a driver change (e.g. splash-and-dash). Enter litres added, or total level after fill. If both are provided, the total level takes precedence.

**Practice** — Records a Friday practice session for fuel model calibration. Fields: driver, fuel start (litres), fuel end (litres), laps completed, optional average lap time, flag condition (green or simulated yellow/SC), notes. After saving, the fuel model EMA updates immediately.

**Scraper** — Enter the SpeedHive session URL (copy from browser address bar on the live timing page) and/or your kart number. Click Start Scraper. The dot in the header goes green when polling is active.

**⚙ Settings** — Per-driver weight and pedal position editor. Also sets tank capacity and current fuel level. Changes are saved to the database and broadcast immediately.

**Start Race** — Marks the race as started (timestamps in DB). Does not affect any calculations — it is a marker for your records. The button disables after clicking.

---

## Friday Practice Workflow

Use this process to calibrate the fuel model before the race starts.

1. Note fuel level at start of stint (litres, or fill to full = tank capacity)
2. Complete a representative number of laps (ideally 15–20 minimum)
3. Note fuel level at end of stint (measure with a gauge or weigh the kart)
4. Click **Practice** in the dashboard and fill in the form
5. For simulated yellow/SC stints, select the appropriate condition — the model normalises this correctly
6. Repeat with a second stint if possible; the EMA will converge toward the true consumption rate
7. Check **GET /api/practice/consumption** to see the computed model vs the EMA

After 2–3 sessions the model should stabilise. The fuel stop predictions during the race will be based on this data plus live updates from each real pit stop.

---

## Race Day Workflow

1. Start app: `uvicorn main:app --host 0.0.0.0 --port 8000`
2. Open `http://localhost:8000` (share the URL to any other devices if needed)
3. Open **Settings**, set each driver's actual weight and pedal position
4. Set **Tank Capacity** to your measured tank volume
5. Click **Scraper**, paste the SpeedHive session URL and your kart number
6. At race start, click **Start Race**
7. When the kart pits: click **Driver Swap**, select the incoming driver, enter fuel level after fill
8. If laps are missing from SpeedHive, add them via **+ Lap**
9. Monitor the **Fuel** panel for laps/time until next stop
10. Watch for the visor alert at 20:30 BST and maintenance alerts at 20:30 BST / 05:30 BST

---

## Adjusting Critical Parameters

| What to change | Where |
|---|---|
| Race date / start time | `RACE_START`, `RACE_END` in `Race.py` |
| Maintenance stop times | `MAINT_STOP_1`, `MAINT_STOP_2` in `Race.py` |
| Visor rule hours | `VISOR_START`, `VISOR_END` in `Race.py` |
| Flag fuel multipliers | `FLAG_MULTIPLIERS` dict in `Race.py` |
| Default consumption before practice | `DEFAULT_CONSUMPTION_LPL = 0.35` in `Race.py` |
| EMA smoothing factor | `alpha = 0.3` in `FuelModel` dataclass in `Race.py` |
| Ballast target weight (85 kg) | `85.0` constant in `ballast_required()` in `Race.py` |
| Kart weight estimate (134 kg) | Not in code — only relevant for manual ballast check |
| Tank capacity | Settings panel, or `PUT /api/fuel/settings` |
| Safety margin laps | Settings panel, or `PUT /api/fuel/settings` |
| Driver roster / rotation order | `RunPlan.ini` (delete `race_data.db` to re-seed) |
| Stint length warning thresholds | `90 * 60` / `150 * 60` in `static/app.js` `startStintTimer()` |
| Scraper poll interval | `poll_interval_s=30` in `SpeedHiveScraper.__init__()` in `scraper.py` |
| Rain radar location | `lat=` / `lon=` in the iframe `src` in `static/index.html` |
| Alert warning lead time (30 min) | `timedelta(minutes=30)` in `AlertEngine._check_visor()` / `_check_maintenance()` in `Race.py` |
| State broadcast interval | `asyncio.sleep(30)` in `_state_broadcast_loop()` in `main.py` |

---

## Querying the Database Directly

```bash
source venv/bin/activate
python3 -c "
import asyncio, database as db, json
async def run():
    # Show all practice sessions
    print(json.dumps(await db.get_practice_sessions(), indent=2))
asyncio.run(run())
"
```

Or open with any SQLite browser (e.g. DB Browser for SQLite) — point it at `race_data.db`.

Useful queries:
```sql
-- Fuel consumption per practice session
SELECT driver_name, laps_completed,
       round((fuel_start_L - fuel_end_L) / laps_completed, 4) as Lpl
FROM practice_sessions;

-- Fastest lap per driver (green only)
SELECT driver_name, min(lap_time_ms)/1000.0 as fastest_s
FROM laps WHERE flag_condition = 'GREEN'
GROUP BY driver_name ORDER BY fastest_s;

-- Stint summary
SELECT driver_name, start_lap, end_lap,
       end_lap - start_lap as laps,
       round(fuel_start_L - fuel_end_L, 2) as litres_used
FROM stints WHERE ended_at IS NOT NULL;
```
