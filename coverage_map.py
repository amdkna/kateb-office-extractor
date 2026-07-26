from __future__ import annotations

import math
import threading
import tkinter as tk
from pathlib import Path
from typing import Callable

import requests


TILE_SIZE = 256
TILE_URL = "https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
TILE_USER_AGENT = "KatebOfficeExtractor/1.0 (desktop coverage map)"

STATUS_COLORS = {
    "done": "#16a34a",
    "failed": "#dc2626",
    "pending": "#2563eb",
}


def world_pixel(latitude: float, longitude: float, zoom: int) -> tuple[float, float]:
    latitude = max(-85.05112878, min(85.05112878, latitude))
    scale = TILE_SIZE * (2**zoom)
    x = (longitude + 180.0) / 360.0 * scale
    radians = math.radians(latitude)
    y = (
        1.0
        - math.asinh(math.tan(radians)) / math.pi
    ) / 2.0 * scale
    return x, y


class CoverageMap(tk.Canvas):
    """A lightweight OpenStreetMap canvas with scan-state cell overlays."""

    def __init__(
        self,
        parent,
        cache_directory: Path,
        tiles_ready_callback: Callable[[], None],
        **kwargs,
    ):
        super().__init__(
            parent,
            background="#dbe4ea",
            highlightthickness=1,
            highlightbackground="#9ca3af",
            **kwargs,
        )
        self.cache_directory = Path(cache_directory)
        self.tiles_ready_callback = tiles_ready_callback
        self.cells: dict[tuple[float, float], str] = {}
        self.bounds = (35.45, 35.95, 50.8, 52.05)
        self.radius_km = 1.8
        self.zoom = 10
        self.origin = (0.0, 0.0)
        self.tile_images: list[tk.PhotoImage] = []
        self.downloading: set[tuple[int, int, int]] = set()
        self.render_job = None
        self.bind("<Configure>", self._schedule_render)

    def set_coverage(
        self,
        cells: dict[tuple[float, float], str],
        bounds: tuple[float, float, float, float],
        radius_km: float,
    ) -> None:
        self.cells = cells
        self.bounds = bounds
        self.radius_km = radius_km
        self.render()

    def _schedule_render(self, _event=None) -> None:
        if self.render_job is not None:
            self.after_cancel(self.render_job)
        self.render_job = self.after(150, self.render)

    def _choose_zoom(self, width: int, height: int) -> int:
        min_lat, max_lat, min_lng, max_lng = self.bounds
        for zoom in range(15, 5, -1):
            left, bottom = world_pixel(min_lat, min_lng, zoom)
            right, top = world_pixel(max_lat, max_lng, zoom)
            if abs(right - left) <= width * 0.94 and abs(bottom - top) <= height * 0.88:
                return zoom
        return 6

    def render(self) -> None:
        self.render_job = None
        width = max(300, self.winfo_width())
        height = max(180, self.winfo_height())
        self.zoom = self._choose_zoom(width, height)
        min_lat, max_lat, min_lng, max_lng = self.bounds
        left, bottom = world_pixel(min_lat, min_lng, self.zoom)
        right, top = world_pixel(max_lat, max_lng, self.zoom)
        center_x = (left + right) / 2
        center_y = (top + bottom) / 2
        self.origin = (center_x - width / 2, center_y - height / 2)

        self.delete("all")
        self.tile_images.clear()
        self._draw_tiles(width, height)
        self._draw_cells()
        self._draw_map_labels(width, height)

    def _draw_tiles(self, width: int, height: int) -> None:
        origin_x, origin_y = self.origin
        first_x = math.floor(origin_x / TILE_SIZE)
        last_x = math.floor((origin_x + width) / TILE_SIZE)
        first_y = math.floor(origin_y / TILE_SIZE)
        last_y = math.floor((origin_y + height) / TILE_SIZE)
        maximum = 2**self.zoom
        missing: list[tuple[int, int, int]] = []

        for tile_y in range(first_y, last_y + 1):
            if not 0 <= tile_y < maximum:
                continue
            for tile_x in range(first_x, last_x + 1):
                wrapped_x = tile_x % maximum
                path = self._tile_path(self.zoom, wrapped_x, tile_y)
                if path.exists():
                    try:
                        image = tk.PhotoImage(file=str(path))
                    except tk.TclError:
                        path.unlink(missing_ok=True)
                        missing.append((self.zoom, wrapped_x, tile_y))
                        continue
                    self.tile_images.append(image)
                    self.create_image(
                        tile_x * TILE_SIZE - origin_x,
                        tile_y * TILE_SIZE - origin_y,
                        image=image,
                        anchor="nw",
                    )
                else:
                    missing.append((self.zoom, wrapped_x, tile_y))

        if missing:
            self.create_text(
                width / 2,
                22,
                text="Loading street map…",
                fill="#374151",
                font=("Segoe UI Semibold", 10),
            )
            self._download_tiles(missing)

    def _tile_path(self, zoom: int, x: int, y: int) -> Path:
        return self.cache_directory / str(zoom) / str(x) / f"{y}.png"

    def _download_tiles(self, tiles: list[tuple[int, int, int]]) -> None:
        queued = [tile for tile in tiles if tile not in self.downloading]
        if not queued:
            return
        self.downloading.update(queued)

        def worker() -> None:
            changed = False
            session = requests.Session()
            session.headers["User-Agent"] = TILE_USER_AGENT
            try:
                for zoom, x, y in queued:
                    path = self._tile_path(zoom, x, y)
                    try:
                        response = session.get(
                            TILE_URL.format(zoom=zoom, x=x, y=y),
                            timeout=12,
                        )
                        response.raise_for_status()
                        if not response.content.startswith(b"\x89PNG"):
                            continue
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(response.content)
                        changed = True
                    except (OSError, requests.RequestException):
                        continue
            finally:
                self.downloading.difference_update(queued)
                session.close()
            if changed:
                self.tiles_ready_callback()

        threading.Thread(target=worker, daemon=True).start()

    def _draw_cells(self) -> None:
        origin_x, origin_y = self.origin
        latitude = (self.bounds[0] + self.bounds[1]) / 2
        lat_spacing = 1.5 * self.radius_km / 110.574
        lng_km_per_degree = 111.320 * math.cos(math.radians(latitude))
        lng_spacing = math.sqrt(3) * self.radius_km / lng_km_per_degree
        center_x, center_y = world_pixel(latitude, self.bounds[2], self.zoom)
        spaced_x, _ = world_pixel(
            latitude,
            self.bounds[2] + lng_spacing,
            self.zoom,
        )
        _, spaced_y = world_pixel(
            latitude + lat_spacing,
            self.bounds[2],
            self.zoom,
        )
        half_size = max(
            2.0,
            min(abs(spaced_x - center_x), abs(spaced_y - center_y)) * 0.42,
        )

        for (lat, lng), status in self.cells.items():
            center_x, center_y = world_pixel(lat, lng, self.zoom)
            center_x -= origin_x
            center_y -= origin_y
            color = STATUS_COLORS.get(status, STATUS_COLORS["pending"])
            self.create_rectangle(
                center_x - half_size,
                center_y - half_size,
                center_x + half_size,
                center_y + half_size,
                fill=color,
                outline=color,
                width=1,
                stipple="gray50",
            )

    def _draw_map_labels(self, width: int, height: int) -> None:
        self.create_rectangle(
            6,
            height - 25,
            225,
            height - 5,
            fill="#ffffff",
            outline="#9ca3af",
        )
        self.create_text(
            12,
            height - 15,
            anchor="w",
            text="© OpenStreetMap contributors",
            fill="#374151",
            font=("Segoe UI", 8),
        )
