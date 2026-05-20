import asyncio
import configparser
import os
import aiosqlite

DB_PATH = os.path.join(os.path.dirname(__file__), "race_data.db")
INI_PATH = os.path.join(os.path.dirname(__file__), "RunPlan.ini")

SCHEMA = """
CREATE TABLE IF NOT EXISTS drivers (
    name TEXT PRIMARY KEY,
    weight_kg REAL NOT NULL DEFAULT 85.0,
    pedal_pos TEXT NOT NULL DEFAULT '3',
    rotation_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS laps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lap_number INTEGER NOT NULL,
    driver_name TEXT NOT NULL,
    lap_time_ms INTEGER NOT NULL,
    flag_condition TEXT NOT NULL DEFAULT 'GREEN',
    is_rain INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'manual',
    recorded_at TEXT NOT NULL,
    stint_id INTEGER
);

CREATE TABLE IF NOT EXISTS stints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    fuel_start_L REAL,
    fuel_end_L REAL,
    start_lap INTEGER,
    end_lap INTEGER
);

CREATE TABLE IF NOT EXISTS fuel_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filled_at TEXT NOT NULL,
    litres_added REAL NOT NULL,
    fuel_level_L REAL,
    lap_number INTEGER
);

CREATE TABLE IF NOT EXISTS practice_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_name TEXT NOT NULL,
    fuel_start_L REAL NOT NULL,
    fuel_end_L REAL NOT NULL,
    laps_completed INTEGER NOT NULL,
    avg_lap_time_ms INTEGER,
    flag_condition TEXT NOT NULL DEFAULT 'GREEN',
    notes TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS race_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_STATE = {
    "tank_capacity_L": "10.0",
    "safety_margin_laps": "2",
    "scraper_enabled": "0",
    "fuel_level_L": "10.0",
    "current_lap": "0",
    "race_started": "0",
}


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        for key, value in DEFAULT_STATE.items():
            await db.execute(
                "INSERT OR IGNORE INTO race_state (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()
    await seed_drivers_from_ini()


def _parse_driver_ini(cfg: configparser.ConfigParser) -> dict[str, dict]:
    """Parse [DRIVERS] section into {name: {weight_kg, pedal_pos}}."""
    out = {}
    if not cfg.has_section("DRIVERS"):
        return out
    for name, value in cfg.items("DRIVERS"):
        if name == "__name__":
            continue
        fields = {}
        for part in value.split(","):
            part = part.strip()
            if ":" in part:
                k, v = part.split(":", 1)
                fields[k.strip()] = v.strip()
        out[name.lower()] = fields
    return out


async def seed_drivers_from_ini() -> None:
    cfg = configparser.ConfigParser()
    cfg.read(INI_PATH)
    if not cfg.has_section("TESTING"):
        return
    raw = cfg.get("TESTING", "drivers", fallback="")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    driver_config = _parse_driver_ini(cfg)

    async with aiosqlite.connect(DB_PATH) as db:
        for i, name in enumerate(names):
            cfg_entry = driver_config.get(name.lower(), {})
            weight_kg = float(cfg_entry.get("weight_kg", 85.0))
            pedal_pos = cfg_entry.get("pedal_pos", "3")
            # Insert if not present, then sync weight/pedal from INI on every startup
            await db.execute(
                "INSERT OR IGNORE INTO drivers (name, weight_kg, pedal_pos, rotation_order) VALUES (?, ?, ?, ?)",
                (name, weight_kg, pedal_pos, i),
            )
            await db.execute(
                "UPDATE drivers SET weight_kg = ?, pedal_pos = ?, rotation_order = ? WHERE name = ?",
                (weight_kg, pedal_pos, i, name),
            )
        await db.commit()


async def get_state() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT key, value FROM race_state") as cur:
            rows = await cur.fetchall()
    return {r["key"]: r["value"] for r in rows}


async def set_state(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO race_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def set_state_many(updates: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR REPLACE INTO race_state (key, value) VALUES (?, ?)",
            list(updates.items()),
        )
        await db.commit()


async def get_drivers() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT name, weight_kg, pedal_pos, rotation_order FROM drivers ORDER BY rotation_order"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def update_driver(name: str, weight_kg: float | None, pedal_pos: str | None) -> bool:
    fields, vals = [], []
    if weight_kg is not None:
        fields.append("weight_kg = ?")
        vals.append(weight_kg)
    if pedal_pos is not None:
        fields.append("pedal_pos = ?")
        vals.append(pedal_pos)
    if not fields:
        return False
    vals.append(name)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE drivers SET {', '.join(fields)} WHERE name = ?", vals)
        await db.commit()
    return True


async def update_rotation(ordered_names: list[str]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "UPDATE drivers SET rotation_order = ? WHERE name = ?",
            [(i, name) for i, name in enumerate(ordered_names)],
        )
        await db.commit()


async def insert_lap(
    lap_number: int,
    driver_name: str,
    lap_time_ms: int,
    flag_condition: str,
    is_rain: bool,
    source: str,
    recorded_at: str,
    stint_id: int | None = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO laps
               (lap_number, driver_name, lap_time_ms, flag_condition, is_rain, source, recorded_at, stint_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (lap_number, driver_name, lap_time_ms, flag_condition, int(is_rain), source, recorded_at, stint_id),
        )
        await db.commit()
        return cur.lastrowid


async def get_laps(limit: int = 50, source: str = "all") -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if source == "all":
            async with db.execute(
                "SELECT * FROM laps ORDER BY id DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM laps WHERE source = ? ORDER BY id DESC LIMIT ?", (source, limit)
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]


async def delete_all_laps() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM laps")
        await db.commit()
        return cur.rowcount


async def delete_all_stints() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM stints")
        await db.commit()
        return cur.rowcount


async def delete_all_fuel_fills() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM fuel_fills")
        await db.commit()
        return cur.rowcount


async def reassign_laps_from(from_lap_number: int, new_driver: str, new_stint_id: int) -> int:
    """Reassign laps with lap_number >= from_lap_number to new driver + stint.
    Used on driver swap to retroactively retag laps the scraper pulled before
    the operator recorded the swap. Returns count of rows updated."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE laps SET driver_name = ?, stint_id = ? WHERE lap_number >= ?",
            (new_driver, new_stint_id, from_lap_number),
        )
        await db.commit()
        return cur.rowcount


async def delete_lap(lap_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM laps WHERE id = ?", (lap_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_lap_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) as cnt, MIN(lap_time_ms) as fastest, AVG(lap_time_ms) as avg_ms FROM laps WHERE flag_condition = 'GREEN'"
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else {"cnt": 0, "fastest": None, "avg_ms": None}


async def get_driver_stats(driver_name: str) -> dict:
    """Cumulative stats for a single driver across all stints."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT COUNT(*) AS total_laps,
                      AVG(CASE WHEN flag_condition='GREEN' THEN lap_time_ms END) AS avg_green_ms
               FROM laps WHERE driver_name = ?""",
            (driver_name,),
        ) as cur:
            lap_row = await cur.fetchone()
        async with db.execute(
            "SELECT started_at, ended_at FROM stints WHERE driver_name = ?",
            (driver_name,),
        ) as cur:
            stint_rows = await cur.fetchall()
    return {
        "total_laps": int(lap_row["total_laps"] or 0),
        "avg_lap_ms": int(lap_row["avg_green_ms"]) if lap_row["avg_green_ms"] else None,
        "stint_durations": [(r["started_at"], r["ended_at"]) for r in stint_rows],
    }


async def start_stint(driver_name: str, started_at: str, fuel_start_L: float | None, start_lap: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO stints (driver_name, started_at, fuel_start_L, start_lap) VALUES (?, ?, ?, ?)",
            (driver_name, started_at, fuel_start_L, start_lap),
        )
        await db.commit()
        return cur.lastrowid


async def end_stint(stint_id: int, ended_at: str, fuel_end_L: float | None, end_lap: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE stints SET ended_at = ?, fuel_end_L = ?, end_lap = ? WHERE id = ?",
            (ended_at, fuel_end_L, end_lap, stint_id),
        )
        await db.commit()


async def get_current_stint() -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM stints WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_stints() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM stints ORDER BY id") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def insert_fuel_fill(filled_at: str, litres_added: float, fuel_level_L: float | None, lap_number: int | None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO fuel_fills (filled_at, litres_added, fuel_level_L, lap_number) VALUES (?, ?, ?, ?)",
            (filled_at, litres_added, fuel_level_L, lap_number),
        )
        await db.commit()
        return cur.lastrowid


async def get_fuel_fills() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM fuel_fills ORDER BY id") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def insert_practice_session(
    driver_name: str,
    fuel_start_L: float,
    fuel_end_L: float,
    laps_completed: int,
    avg_lap_time_ms: int | None,
    flag_condition: str,
    notes: str | None,
    recorded_at: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO practice_sessions
               (driver_name, fuel_start_L, fuel_end_L, laps_completed, avg_lap_time_ms, flag_condition, notes, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (driver_name, fuel_start_L, fuel_end_L, laps_completed, avg_lap_time_ms, flag_condition, notes, recorded_at),
        )
        await db.commit()
        return cur.lastrowid


async def get_practice_sessions() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM practice_sessions ORDER BY id") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


_PRACTICE_FIELDS = {"driver_name", "fuel_start_L", "fuel_end_L", "laps_completed", "avg_lap_time_ms", "flag_condition", "notes"}


async def update_practice_session(session_id: int, fields: dict) -> bool:
    """Update one or more columns on a practice session."""
    cols, vals = [], []
    for k, v in fields.items():
        if k in _PRACTICE_FIELDS:
            cols.append(f"{k} = ?")
            vals.append(v)
    if not cols:
        return False
    vals.append(session_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE practice_sessions SET {', '.join(cols)} WHERE id = ?", vals,
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_practice_session(session_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM practice_sessions WHERE id = ?", (session_id,))
        await db.commit()
        return cur.rowcount > 0


_STINT_EDITABLE = {"fuel_start_L", "fuel_end_L", "start_lap", "end_lap"}


async def update_stint(stint_id: int, fields: dict) -> bool:
    """Update fuel + lap-range fields on an existing stint."""
    cols, vals = [], []
    for k, v in fields.items():
        if k in _STINT_EDITABLE:
            cols.append(f"{k} = ?")
            vals.append(v)
    if not cols:
        return False
    vals.append(stint_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE stints SET {', '.join(cols)} WHERE id = ?", vals,
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_stint(stint_id: int) -> int:
    """Delete a stint and cascade-delete its laps. Returns count of laps deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM laps WHERE stint_id = ?", (stint_id,))
        laps_deleted = cur.rowcount
        await db.execute("DELETE FROM stints WHERE id = ?", (stint_id,))
        await db.commit()
        return laps_deleted
