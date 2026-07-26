from __future__ import annotations

import argparse
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from office_data import OfficeDatabase


LOG = logging.getLogger("kateb-extractor")


@dataclass(frozen=True)
class Settings:
    base_url: str
    endpoint: str
    search_url: str
    auth_mode: str
    chrome_exe: str
    chrome_profile_dir: str
    database_path: Path
    min_lat: float
    max_lat: float
    min_lng: float
    max_lng: float
    coverage_radius_km: float
    request_delay: float
    batch_query_count: int
    batch_delay: float
    timeout: float
    max_retries: int
    verify_ssl: bool
    headless: bool
    saturation_threshold: int
    user_agent: str
    cookie_header: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            base_url=os.getenv(
                "BASE_URL", "https://officialoffice.web.kateb.ir"
            ).rstrip("/"),
            endpoint=os.getenv(
                "GEO_ENDPOINT",
                "/api/marriageOffice/getNotarizationOfficesByGeo",
            ),
            search_url=os.getenv(
                "SEARCH_URL",
                "https://officialoffice.web.kateb.ir/search",
            ),
            auth_mode=os.getenv("AUTH_MODE", "browser").strip().lower(),
            chrome_exe=os.getenv(
                "CHROME_EXE",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            ),
            chrome_profile_dir=os.getenv(
                "CHROME_PROFILE_DIR", "./chrome-profile"
            ),
            database_path=Path(
                os.getenv("DATABASE_PATH", "./data/office_data.sqlite3")
            ),
            min_lat=float(os.getenv("MIN_LAT", "35.45")),
            max_lat=float(os.getenv("MAX_LAT", "35.95")),
            min_lng=float(os.getenv("MIN_LNG", "50.80")),
            max_lng=float(os.getenv("MAX_LNG", "52.05")),
            coverage_radius_km=float(
                os.getenv("COVERAGE_RADIUS_KM", "1.8")
            ),
            request_delay=float(os.getenv("REQUEST_DELAY_SECONDS", "1.0")),
            batch_query_count=int(os.getenv("BATCH_QUERY_COUNT", "10")),
            batch_delay=float(os.getenv("BATCH_DELAY_SECONDS", "10")),
            timeout=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
            max_retries=int(os.getenv("MAX_RETRIES", "3")),
            verify_ssl=os.getenv("VERIFY_SSL", "true").lower() in {"1", "true", "yes"},
            headless=os.getenv("HEADLESS", "false").lower() in {"1", "true", "yes"},
            saturation_threshold=int(os.getenv("SATURATION_THRESHOLD", "45")),
            user_agent=os.getenv(
                "USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36",
            ),
            cookie_header=os.getenv("COOKIE_HEADER", "").strip(),
        )


@dataclass
class ScanControl:
    """Thread-safe controls shared by CLI and graphical collection runs."""

    pause_event: threading.Event = field(default_factory=threading.Event)
    stop_event: threading.Event = field(default_factory=threading.Event)

    def pause(self) -> None:
        self.pause_event.set()

    def resume(self) -> None:
        self.pause_event.clear()

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()

    def wait(self, seconds: float = 0) -> bool:
        """Wait interruptibly; paused time does not consume the delay."""
        remaining = max(0.0, float(seconds))
        last_tick = time.monotonic()
        while True:
            if self.stop_event.is_set():
                return False
            if self.pause_event.is_set():
                self.stop_event.wait(0.1)
                last_tick = time.monotonic()
                continue
            if remaining <= 0:
                return True
            wait_slice = min(0.1, remaining)
            if self.stop_event.wait(wait_slice):
                return False
            now = time.monotonic()
            remaining -= now - last_tick
            last_tick = now


class ScanStopped(Exception):
    pass


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def build_session(settings: Settings) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=settings.max_retries,
        connect=settings.max_retries,
        read=settings.max_retries,
        status=settings.max_retries,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": settings.search_url,
            "User-Agent": settings.user_agent,
        }
    )
    if settings.cookie_header:
        session.headers["Cookie"] = settings.cookie_header
    return session


def frange(start: float, stop: float, step: float) -> Iterable[float]:
    if step <= 0:
        raise ValueError("Grid step must be greater than zero.")
    count = int(math.floor((stop - start) / step)) + 1
    for index in range(max(0, count)):
        yield round(start + index * step, 8)
    if count > 0 and start + (count - 1) * step < stop - 1e-10:
        yield round(stop, 8)


def grid_points(settings: Settings) -> list[tuple[float, float]]:
    """
    Cover the target rectangle with a triangular lattice.

    The observed API radius is about 2 km. Circle centers on an equilateral
    triangular lattice cover the plane with much less overlap than a square
    grid. A small outside buffer ensures the rectangle's edges are covered.
    """
    radius = settings.coverage_radius_km
    if radius <= 0:
        raise ValueError("COVERAGE_RADIUS_KM must be greater than zero.")

    latitude_km_per_degree = 110.574
    vertical_spacing_km = 1.5 * radius
    horizontal_spacing_km = math.sqrt(3) * radius
    latitude_padding = radius / latitude_km_per_degree
    latitude_step = vertical_spacing_km / latitude_km_per_degree
    start_latitude = settings.min_lat - latitude_padding
    stop_latitude = settings.max_lat + latitude_padding

    points: list[tuple[float, float]] = []
    row = 0
    latitude = start_latitude
    while latitude <= stop_latitude + 1e-10:
        longitude_km_per_degree = 111.320 * math.cos(math.radians(latitude))
        longitude_spacing = horizontal_spacing_km / longitude_km_per_degree
        longitude_padding = radius / longitude_km_per_degree
        longitude = settings.min_lng - longitude_padding
        if row % 2:
            longitude += longitude_spacing / 2
        stop_longitude = settings.max_lng + longitude_padding
        while longitude <= stop_longitude + 1e-10:
            points.append((round(latitude, 8), round(longitude, 8)))
            longitude += longitude_spacing
        row += 1
        latitude = start_latitude + row * latitude_step
    return points


def refinement_points(
    settings: Settings, lat: float, lng: float
) -> list[tuple[float, float]]:
    """Add local diagonal probes when a response may be capped."""
    offset_km = settings.coverage_radius_km / 2
    lat_offset = offset_km / 110.574
    longitude_km_per_degree = 111.320 * math.cos(math.radians(lat))
    lng_offset = offset_km / longitude_km_per_degree
    candidates = (
        (lat - lat_offset, lng - lng_offset),
        (lat - lat_offset, lng + lng_offset),
        (lat + lat_offset, lng - lng_offset),
        (lat + lat_offset, lng + lng_offset),
    )
    return [
        (round(candidate_lat, 8), round(candidate_lng, 8))
        for candidate_lat, candidate_lng in candidates
        if settings.min_lat <= candidate_lat <= settings.max_lat
        and settings.min_lng <= candidate_lng <= settings.max_lng
    ]


def planned_scan_points(
    settings: Settings,
    database: OfficeDatabase,
) -> list[tuple[float, float]]:
    """Build the current base grid plus known saturation refinements."""
    points = grid_points(settings)
    queued_points = set(points)
    for saturated_lat, saturated_lng in database.saturated_scan_points(
        settings.endpoint,
        settings.saturation_threshold,
    ):
        for point in refinement_points(settings, saturated_lat, saturated_lng):
            if point not in queued_points:
                queued_points.add(point)
                points.append(point)
    return points


def request_public(
    session: requests.Session, settings: Settings, lat: float, lng: float
) -> Any:
    url = f"{settings.base_url}{settings.endpoint}"
    response = session.get(
        url,
        params={"lat": lat, "lng": lng},
        timeout=settings.timeout,
        verify=settings.verify_ssl,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        raise RuntimeError(
            f"Expected JSON but received {content_type or 'unknown content type'}."
        )
    return response.json()


def create_browser_context(settings: Settings):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Browser mode requires Playwright. Run: pip install -r requirements.txt"
        ) from exc

    chrome_path = Path(settings.chrome_exe)
    if not chrome_path.exists():
        raise FileNotFoundError(
            f"Chrome was not found at: {chrome_path}\n"
            "Set CHROME_EXE correctly in .env."
        )

    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(Path(settings.chrome_profile_dir).resolve()),
        executable_path=str(chrome_path),
        headless=settings.headless,
        viewport={"width": 1500, "height": 950},
        args=["--start-maximized"],
    )
    return playwright, context


def ensure_browser_login(
    context,
    settings: Settings,
    login_callback: Callable[[], bool] | None = None,
) -> None:
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(settings.search_url, wait_until="domcontentloaded", timeout=120_000)

    if settings.headless:
        return

    if login_callback is not None:
        if not login_callback():
            raise ScanStopped("Collection stopped before browser login was confirmed.")
        return

    print(
        "\nChrome is open with a dedicated reusable profile.\n"
        "If the site asks for login, log in manually and return here.\n"
        "When the map page is visible, press Enter in this terminal."
    )
    input()


def request_browser(context, settings: Settings, lat: float, lng: float) -> Any:
    url = f"{settings.base_url}{settings.endpoint}"
    response = context.request.get(
        url,
        params={"lat": str(lat), "lng": str(lng)},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": settings.search_url,
        },
        timeout=settings.timeout * 1000,
    )
    if not response.ok:
        raise RuntimeError(
            f"Browser request failed: HTTP {response.status} for lat={lat}, lng={lng}"
        )
    return response.json()


def find_entry_list(payload: Any) -> list[dict[str, Any]]:
    """
    Finds the most likely list of office objects inside an unknown JSON envelope.
    It supports:
      [...]
      {"data": [...]}
      {"result": [...]}
      {"data": {"items": [...]}}
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    preferred_keys = (
        "data",
        "result",
        "results",
        "items",
        "offices",
        "notarizationOffices",
        "marriageOffices",
        "content",
        "value",
    )
    for key in preferred_keys:
        value = payload.get(key)
        if isinstance(value, list):
            dict_items = [item for item in value if isinstance(item, dict)]
            if dict_items or value == []:
                return dict_items
        if isinstance(value, dict):
            nested = find_entry_list(value)
            if nested:
                return nested

    candidates: list[list[dict[str, Any]]] = []
    for value in payload.values():
        if isinstance(value, list):
            dict_items = [item for item in value if isinstance(item, dict)]
            if dict_items:
                candidates.append(dict_items)
        elif isinstance(value, dict):
            nested = find_entry_list(value)
            if nested:
                candidates.append(nested)

    return max(candidates, key=len, default=[])


def run(
    settings: Settings,
    verbose: bool = False,
    rescan: bool = False,
    control: ScanControl | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    login_callback: Callable[[], bool] | None = None,
) -> int:
    configure_logging(verbose)
    control = control or ScanControl()

    def report_progress(current: int, total: int, status: str) -> None:
        if progress_callback is not None:
            progress_callback(current, total, status)

    database = OfficeDatabase(settings.database_path)
    points = planned_scan_points(settings, database)
    queued_points = set(points)
    planned_count = len(points)
    status_before = database.scan_status_counts(settings.endpoint)
    if not rescan:
        completed_points = database.completed_scan_points(settings.endpoint)
        points = [point for point in points if point not in completed_points]
    LOG.info(
        "Coordinate ledger: %d done; %d failed; %d remaining of %d planned.",
        status_before["done"],
        status_before["failed"],
        len(points),
        planned_count,
    )
    LOG.info(
        "Bounds: lat %.6f..%.6f, lng %.6f..%.6f",
        settings.min_lat,
        settings.max_lat,
        settings.min_lng,
        settings.max_lng,
    )
    LOG.info("Writing directly to SQLite: %s", settings.database_path)
    if rescan:
        LOG.info("Rescan enabled: completed coordinates will be checked again.")

    auth_mode = settings.auth_mode
    if auth_mode not in {"auto", "public", "browser"}:
        raise ValueError("AUTH_MODE must be auto, public, or browser.")

    session = build_session(settings)
    browser_runtime = None
    browser_context = None
    total_inserted = 0
    total_updated = 0
    total_skipped = 0
    completed_this_run = 0
    failed_this_run = 0
    attempted_requests = 0
    stopped = False
    report_progress(0, len(points), "ready")

    try:
        index = 0
        while index < len(points):
            if not control.wait():
                stopped = True
                break
            lat, lng = points[index]
            index += 1
            attempted_requests += 1

            LOG.info("[%d/%d] Querying %.6f, %.6f", index, len(points), lat, lng)
            payload = None

            try:
                if auth_mode in {"auto", "public"}:
                    try:
                        payload = request_public(session, settings, lat, lng)
                    except Exception as exc:
                        if auth_mode == "public":
                            raise
                        LOG.warning("Public request failed: %s", exc)

                if payload is None and auth_mode in {"auto", "browser"}:
                    if browser_context is None:
                        LOG.info("Starting Chrome browser fallback...")
                        browser_runtime, browser_context = create_browser_context(settings)
                        ensure_browser_login(
                            browser_context,
                            settings,
                            login_callback,
                        )
                    payload = request_browser(browser_context, settings, lat, lng)

                entries = find_entry_list(payload)
                source = (
                    f"{settings.base_url}{settings.endpoint}"
                    f"?lat={lat}&lng={lng}"
                )
                with database.connect() as connection:
                    inserted, updated, skipped = database.store_api_entries(
                        connection,
                        entries,
                        source,
                    )
                    database.mark_scan_point(
                        connection,
                        settings.endpoint,
                        lat,
                        lng,
                        len(entries),
                        inserted,
                        updated,
                    )

                total_inserted += inserted
                total_updated += updated
                total_skipped += skipped
                completed_this_run += 1
                LOG.info(
                    "DONE %.6f, %.6f | found %d; new %d; "
                    "duplicates updated %d; database total %d",
                    lat,
                    lng,
                    len(entries),
                    inserted,
                    updated,
                    database.count(),
                )

                if len(entries) >= settings.saturation_threshold:
                    added = 0
                    for point in refinement_points(settings, lat, lng):
                        if point not in queued_points:
                            queued_points.add(point)
                            points.append(point)
                            added += 1
                    if added:
                        LOG.info(
                            "Response may be capped; queued %d denser follow-up points.",
                            added,
                        )
            except ScanStopped:
                stopped = True
                break
            except Exception as exc:
                failed_this_run += 1
                database.mark_scan_failure(
                    settings.endpoint,
                    lat,
                    lng,
                    str(exc),
                )
                LOG.error(
                    "Coordinate %.6f, %.6f failed and remains available for retry: %s",
                    lat,
                    lng,
                    exc,
                )

            report_progress(index, len(points), "running")
            if index < len(points):
                if not control.wait(settings.request_delay):
                    stopped = True
                    break
                if (
                    settings.batch_query_count > 0
                    and settings.batch_delay > 0
                    and attempted_requests % settings.batch_query_count == 0
                ):
                    LOG.info(
                        "Batch delay: %d queries completed; waiting %.1f seconds.",
                        attempted_requests,
                        settings.batch_delay,
                    )
                    report_progress(index, len(points), "batch-delay")
                    if not control.wait(settings.batch_delay):
                        stopped = True
                        break

    except KeyboardInterrupt:
        LOG.warning(
            "Stopped by user. Database progress is already saved; rerun to resume."
        )
        return 130
    finally:
        if browser_context is not None:
            try:
                browser_context.close()
            except Exception:
                LOG.debug("Browser context was already closed.", exc_info=True)
        if browser_runtime is not None:
            browser_runtime.stop()

    if stopped:
        LOG.warning(
            "Collection force-stopped. Database progress is saved; rerun to resume."
        )
        report_progress(index, len(points), "stopped")
        return 130

    LOG.info(
        "Scan complete. Coordinates checked: %d; failed: %d; "
        "new offices: %d; duplicates updated: %d; skipped conflicts: %d; "
        "database total: %d",
        completed_this_run,
        failed_this_run,
        total_inserted,
        total_updated,
        total_skipped,
        database.count(),
    )
    final_status = database.scan_status_counts(settings.endpoint)
    LOG.info(
        "Coordinate ledger now contains %d done and %d failed points.",
        final_status["done"],
        final_status["failed"],
    )
    report_progress(len(points), len(points), "finished")
    return 1 if failed_this_run else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect Kateb official-office map data directly into a "
            "deduplicated SQLite database."
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print configuration and number of grid points without making requests.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )
    parser.add_argument(
        "--rescan",
        action="store_true",
        help=(
            "Query coordinates again even when they are already marked complete. "
            "Existing database rows are updated, not duplicated."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()

    if args.check:
        configure_logging(args.verbose)
        points = grid_points(settings)
        print(
            json.dumps(
                {
                    "base_url": settings.base_url,
                    "endpoint": settings.endpoint,
                    "auth_mode": settings.auth_mode,
                    "database_path": str(settings.database_path),
                    "bounds": {
                        "min_lat": settings.min_lat,
                        "max_lat": settings.max_lat,
                        "min_lng": settings.min_lng,
                        "max_lng": settings.max_lng,
                    },
                    "coverage": {
                        "layout": "triangular",
                        "effective_radius_km": settings.coverage_radius_km,
                        "observed_api_radius_km": 2.0,
                    },
                    "grid_points": len(points),
                    "estimated_minutes": round(
                        (
                            len(points) * settings.request_delay
                            + (
                                len(points) // settings.batch_query_count
                                if settings.batch_query_count > 0
                                else 0
                            )
                            * settings.batch_delay
                        )
                        / 60,
                        1,
                    ),
                    "batch_delay": {
                        "every_queries": settings.batch_query_count,
                        "wait_seconds": settings.batch_delay,
                    },
                    "saturation_threshold": settings.saturation_threshold,
                },
                indent=2,
            )
        )
        return 0

    return run(settings, args.verbose, args.rescan)


if __name__ == "__main__":
    raise SystemExit(main())
