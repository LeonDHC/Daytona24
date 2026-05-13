"""
SpeedHive live timing scraper.
Strategy: try httpx JSON API first, fall back to Playwright DOM scraping.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable, Awaitable

import httpx

log = logging.getLogger("daytona24.scraper")

# Known SpeedHive API endpoint patterns to try
_API_PATTERNS = [
    "https://speedhive.mylaps.com/api/v1/sessions/{sid}/results",
    "https://speedhive.mylaps.com/Sessions/{sid}/TimingData",
]

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
}


def _extract_session_id(url: str) -> str | None:
    """Extract numeric session ID from a SpeedHive URL."""
    m = re.search(r"/(\d{5,})", url)
    return m.group(1) if m else None


def _parse_lap_time_str(s: str) -> int | None:
    """Parse 'm:ss.xxx' or 'ss.xxx' into milliseconds."""
    s = s.strip()
    try:
        if ":" in s:
            parts = s.split(":")
            minutes = int(parts[0])
            sec_str = parts[1]
        else:
            minutes = 0
            sec_str = s
        sec_parts = sec_str.split(".")
        seconds = int(sec_parts[0])
        ms = int(sec_parts[1].ljust(3, "0")[:3]) if len(sec_parts) > 1 else 0
        return (minutes * 60 + seconds) * 1000 + ms
    except (ValueError, IndexError):
        return None


_GAP_LAPS_RE = re.compile(r"^\+?\s*(\d+)\s*L", re.IGNORECASE)
_LEADER_TOKENS = {"", "-", "--", "—", "leader", "interval", "gap"}


def _parse_gap_cell(s: str) -> tuple[float | None, int | None]:
    """Parse a SpeedHive gap cell.
    Returns (seconds, laps_down). Either may be None.
    "Leader"/"—"/"" → (0.0, 0); "+0.523" → (0.523, 0); "+1L" → (None, 1).
    """
    s = (s or "").strip()
    if s.lower() in _LEADER_TOKENS:
        return 0.0, 0
    m = _GAP_LAPS_RE.match(s)
    if m:
        return None, int(m.group(1))
    cleaned = s.lstrip("+").strip()
    try:
        return float(cleaned), 0
    except ValueError:
        return None, None


def _format_gap(seconds: float | None, laps_down: int | None) -> str:
    if laps_down is not None and laps_down > 0:
        return f"+{laps_down}L"
    if seconds is None:
        return "—"
    if abs(seconds) < 0.0005:
        return "0.000"
    return f"{seconds:+.3f}"


class SpeedHiveScraper:
    def __init__(
        self,
        session_url: str | None = None,
        team_number: str | None = None,
        poll_interval_s: int = 30,
    ):
        self._session_url = session_url
        self._team_number = team_number
        self._poll_interval = poll_interval_s
        self._running = False
        self._last_lap_seen = 0
        self._use_playwright = False   # flipped to True if httpx fails
        self._pw_browser = None
        self._pw_page = None
        self._last_standings: dict | None = None
        self.on_new_lap: Callable[[dict], Awaitable[None]] | None = None
        self.on_standings_update: Callable[[dict], Awaitable[None]] | None = None

    async def _try_api(self) -> list[dict] | None:
        if not self._session_url:
            return None
        sid = _extract_session_id(self._session_url)
        if not sid:
            return None

        async with httpx.AsyncClient(timeout=10, headers=_BROWSER_HEADERS, follow_redirects=True) as client:
            for pattern in _API_PATTERNS:
                url = pattern.format(sid=sid)
                try:
                    resp = await client.get(url, headers={"Referer": self._session_url})
                    if resp.status_code == 200 and "application/json" in resp.headers.get("content-type", ""):
                        data = resp.json()
                        return self._parse_api_response(data)
                except Exception as e:
                    log.debug(f"API attempt {url} failed: {e}")
        return None

    def _parse_api_response(self, data: dict | list) -> list[dict]:
        """Try to extract lap records from various SpeedHive JSON shapes."""
        results = []

        # Shape 1: list of lap objects
        if isinstance(data, list):
            entries = data
        # Shape 2: {"results": [...]} or {"laps": [...]} or {"data": [...]}
        elif isinstance(data, dict):
            entries = data.get("results") or data.get("laps") or data.get("data") or []
        else:
            return []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            lap_time_raw = (
                entry.get("lapTime") or entry.get("lap_time") or
                entry.get("LapTime") or entry.get("time") or ""
            )
            lap_num = int(entry.get("lap") or entry.get("lapNumber") or entry.get("LapNumber") or 0)
            driver = str(entry.get("participant") or entry.get("driver") or entry.get("name") or "unknown")
            ms = _parse_lap_time_str(str(lap_time_raw))
            if ms and lap_num > 0:
                results.append({
                    "lap_number": lap_num,
                    "driver_name": driver,
                    "lap_time_ms": ms,
                    "flag_condition": "GREEN",
                    "is_rain": False,
                })

        # Filter by team number if specified
        if self._team_number:
            results = [r for r in results if self._team_number in r["driver_name"]]

        return results

    async def _ensure_playwright(self):
        if self._pw_page is not None:
            return
        try:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            self._pw_browser = await pw.chromium.launch(headless=True)
            context = await self._pw_browser.new_context(
                user_agent=_BROWSER_HEADERS["User-Agent"]
            )
            self._pw_page = await context.new_page()
            if self._session_url:
                await self._pw_page.goto(self._session_url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            log.error(f"Playwright init failed: {e}")
            self._pw_page = None

    async def _scrape_playwright(self) -> list[dict]:
        if not self._session_url:
            return []
        try:
            await self._ensure_playwright()
            if not self._pw_page:
                return []
            await self._pw_page.reload(wait_until="networkidle", timeout=20000)
            # Extract table rows — SpeedHive renders a timing table with lap data
            rows = await self._pw_page.evaluate("""
                () => {
                    const rows = [];
                    // Try various table selectors SpeedHive uses
                    const tables = document.querySelectorAll('table tr, [class*="lap"] [class*="row"], [class*="timing"] tr');
                    tables.forEach(row => {
                        const cells = Array.from(row.querySelectorAll('td, [class*="cell"]'));
                        if (cells.length >= 2) {
                            rows.push(cells.map(c => c.innerText.trim()));
                        }
                    });
                    return rows;
                }
            """)
            return self._parse_dom_rows(rows)
        except Exception as e:
            log.warning(f"Playwright scrape error: {e}")
            return []

    def _parse_dom_rows(self, rows: list[list[str]]) -> list[dict]:
        results = []
        for cells in rows:
            # Look for a cell that looks like a lap time (contains ':' and '.')
            lap_time_ms = None
            lap_num = 0
            for cell in cells:
                ms = _parse_lap_time_str(cell)
                if ms and 20000 < ms < 300000:  # 20s–5min sanity check
                    lap_time_ms = ms
                # Look for lap number (short integer)
                try:
                    n = int(cell)
                    if 0 < n < 10000:
                        lap_num = n
                except ValueError:
                    pass
            if lap_time_ms and lap_num > 0:
                results.append({
                    "lap_number": lap_num,
                    "driver_name": self._team_number or "team",
                    "lap_time_ms": lap_time_ms,
                    "flag_condition": "GREEN",
                    "is_rain": False,
                })
        return results

    async def _scrape_standings_rows(self) -> list[list[str]]:
        """Pull every table-row-like element on the page (no extra reload)."""
        if not self._pw_page:
            return []
        try:
            return await self._pw_page.evaluate("""
                () => {
                    const out = [];
                    const trs = document.querySelectorAll(
                        'table tr, [class*="standings"] [class*="row"], [class*="classification"] [class*="row"]'
                    );
                    trs.forEach(tr => {
                        const cells = Array.from(tr.querySelectorAll('td, [class*="cell"]'));
                        if (cells.length >= 3) {
                            out.push(cells.map(c => c.innerText.trim()));
                        }
                    });
                    return out;
                }
            """)
        except Exception as e:
            log.debug(f"Standings DOM extract failed: {e}")
            return []

    def _parse_standings_rows(self, rows: list[list[str]]) -> dict | None:
        """Find our row by team_number, compute gap to row ahead and behind.
        Uses SpeedHive's gap-to-leader column; ahead/behind gaps are differences.
        """
        if not self._team_number:
            return None

        parsed: list[dict] = []
        for cells in rows:
            position = None
            for c in cells[:3]:
                try:
                    n = int(c.strip())
                    if 0 < n < 200:
                        position = n
                        break
                except ValueError:
                    pass
            if position is None:
                continue

            gap_s, laps_down = None, None
            for c in cells:
                cs = c.strip()
                if not cs:
                    continue
                if cs.lower() in _LEADER_TOKENS:
                    gap_s, laps_down = 0.0, 0
                    break
                if cs.startswith("+") or _GAP_LAPS_RE.match(cs):
                    g, l = _parse_gap_cell(cs)
                    if g is not None or l is not None:
                        gap_s, laps_down = g, l
                        break

            is_us = any(c.strip() == str(self._team_number) for c in cells)
            parsed.append({
                "position": position,
                "gap_to_leader_s": gap_s,
                "laps_down": laps_down or 0,
                "is_us": is_us,
            })

        if not parsed:
            return None

        # Dedupe identical positions (DOM may pick up the same row via two selectors)
        seen_positions: dict[int, dict] = {}
        for row in parsed:
            existing = seen_positions.get(row["position"])
            if existing is None or (existing["gap_to_leader_s"] is None and row["gap_to_leader_s"] is not None):
                seen_positions[row["position"]] = row
            if row["is_us"]:
                seen_positions[row["position"]]["is_us"] = True
        parsed = sorted(seen_positions.values(), key=lambda r: r["position"])

        our_idx = next((i for i, r in enumerate(parsed) if r["is_us"]), None)
        if our_idx is None:
            return None

        us = parsed[our_idx]
        ahead = parsed[our_idx - 1] if our_idx > 0 else None
        behind = parsed[our_idx + 1] if our_idx + 1 < len(parsed) else None

        def gap_between(leader: dict, follower: dict) -> tuple[float | None, int]:
            lap_diff = (follower["laps_down"] or 0) - (leader["laps_down"] or 0)
            if lap_diff > 0:
                return None, lap_diff
            if leader["gap_to_leader_s"] is None or follower["gap_to_leader_s"] is None:
                return None, 0
            return follower["gap_to_leader_s"] - leader["gap_to_leader_s"], 0

        if ahead:
            ah_s, ah_l = gap_between(ahead, us)
            gap_ahead = _format_gap(ah_s, ah_l)
        else:
            gap_ahead = "—"

        if behind:
            be_s, be_l = gap_between(us, behind)
            gap_behind = _format_gap(be_s, be_l)
        else:
            gap_behind = "—"

        return {
            "position": us["position"],
            "gap_ahead": gap_ahead,
            "gap_behind": gap_behind,
        }

    async def poll_standings(self) -> dict | None:
        if not self._team_number or not self._pw_page:
            return None
        rows = await self._scrape_standings_rows()
        return self._parse_standings_rows(rows)

    async def poll_once(self) -> list[dict]:
        laps = await self._try_api()
        if laps is None:
            if not self._use_playwright:
                log.info("SpeedHive API unavailable — switching to Playwright scraper")
                self._use_playwright = True
            laps = await self._scrape_playwright()
        else:
            self._use_playwright = False

        return [lap for lap in laps if lap["lap_number"] > self._last_lap_seen]

    async def run_loop(self):
        self._running = True
        log.info(f"Scraper started: url={self._session_url}, team={self._team_number}, interval={self._poll_interval}s")
        while self._running:
            try:
                new_laps = await self.poll_once()
                for lap in sorted(new_laps, key=lambda x: x["lap_number"]):
                    if self.on_new_lap:
                        await self.on_new_lap(lap)
                    self._last_lap_seen = max(self._last_lap_seen, lap["lap_number"])

                # Standings — only meaningful once Playwright is up and we have a kart number
                if self._team_number and self._pw_page:
                    standings = await self.poll_standings()
                    if standings and standings != self._last_standings:
                        self._last_standings = standings
                        if self.on_standings_update:
                            await self.on_standings_update(standings)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"Scraper poll error: {e}")
            await asyncio.sleep(self._poll_interval)
        log.info("Scraper stopped")

    def stop(self):
        self._running = False
