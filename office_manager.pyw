from __future__ import annotations

import logging
import math
import queue
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from coverage_map import CoverageMap
from extractor import (
    ScanControl,
    Settings,
    planned_scan_points,
    run as run_collector,
)
from office_data import (
    DATABASE_COLUMNS,
    DISPLAY_NAMES,
    OfficeDatabase,
    export_xlsx,
)


APP_DIR = Path(__file__).resolve().parent
PAGE_SIZE = 300

COLUMN_WIDTHS = {
    "id": 70,
    "tel": 145,
    "tel_normalized": 165,
    "title": 330,
    "address": 420,
    "province_code": 115,
    "city_code": 100,
    "office_id": 250,
    "post_code": 115,
    "scriptorium_type": 110,
    "headship_first_name": 140,
    "headship_last_name": 170,
    "headship_cell_phone": 150,
    "latitude": 145,
    "longitude": 145,
    "source_file": 340,
    "updated_at": 175,
}


class QueueLogHandler(logging.Handler):
    def __init__(self, event_queue: queue.Queue):
        super().__init__()
        self.event_queue = event_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.event_queue.put(("log", self.format(record)))
        except Exception:
            self.handleError(record)


class OfficeManagerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        settings = Settings.from_env()
        self.database = OfficeDatabase(settings.database_path)
        self.sort_column = "id"
        self.sort_descending = False
        self.current_page = 1
        self.total_rows = 0
        self.filter_job = None
        self.busy = False
        self.collector_events: queue.Queue = queue.Queue()
        self.collector_thread = None
        self.collector_control = None
        self.collector_login_event = threading.Event()
        self.collector_running = False
        self.collector_log_handler = None
        self.collector_settings = settings
        self.batch_count_var = tk.StringVar(value=str(settings.batch_query_count))
        self.batch_delay_var = tk.StringVar(value=f"{settings.batch_delay:g}")
        self.collector_status_var = tk.StringVar(value="Ready to collect")
        self.collector_progress_text = tk.StringVar(value="0 / 0")
        self.map_counts_var = tk.StringVar(value="Loading scan plan…")
        self.filter_vars = {
            column: tk.StringVar(root) for column in DATABASE_COLUMNS
        }

        self._configure_window()
        self._build_menu()
        self._build_layout()
        self.refresh()
        self._refresh_coverage_map()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_collector_events)

    def _configure_window(self) -> None:
        self.root.title("Kateb Office Data Manager")
        self.root.geometry("1450x820")
        self.root.minsize(1000, 600)
        self.root.option_add("*Font", ("Segoe UI", 10))
        try:
            self.root.tk.call("tk", "scaling", 1.15)
        except tk.TclError:
            pass

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=29)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 16))
        style.configure("Muted.TLabel", foreground="#555555")

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self.root)
        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="Import new data…", command=self.import_folder)
        file_menu.add_command(label="Export to Excel…", command=self.export_data)
        menu_bar.add_cascade(label="File", menu=file_menu)
        self.root.configure(menu=menu_bar)

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, padding=(14, 12, 14, 8))
        outer.pack(fill="both", expand=True)

        title_row = ttk.Frame(outer)
        title_row.pack(fill="x", pady=(0, 10))
        ttk.Label(title_row, text="Office telephone database", style="Title.TLabel").pack(
            side="left"
        )
        self.summary_label = ttk.Label(title_row, text="", style="Muted.TLabel")
        self.summary_label.pack(side="left", padx=(18, 0), pady=(5, 0))
        ttk.Button(
            title_row, text="Import JSON folder", command=self.import_folder
        ).pack(side="right")
        ttk.Button(
            title_row, text="Export Excel", command=self.export_data
        ).pack(side="right", padx=(0, 8))

        notebook = ttk.Notebook(outer)
        self.notebook = notebook
        notebook.pack(fill="both", expand=True)
        data_tab = ttk.Frame(notebook, padding=(8, 8, 8, 6))
        notebook.add(data_tab, text="All data")

        ttk.Label(
            data_tab,
            text="Type in any box to filter that column. Click a column title to sort.",
            style="Muted.TLabel",
        ).pack(fill="x", pady=(0, 5))

        table_frame = ttk.Frame(data_tab)
        table_frame.pack(fill="both", expand=True)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(1, weight=1)

        self.filter_canvas = tk.Canvas(
            table_frame,
            height=38,
            highlightthickness=0,
            background="#f3f3f3",
        )
        self.filter_canvas.grid(row=0, column=0, sticky="ew")
        self._build_filter_row()

        self.tree = ttk.Treeview(
            table_frame,
            columns=DATABASE_COLUMNS,
            show="headings",
            selectmode="browse",
        )
        for column in DATABASE_COLUMNS:
            self.tree.column(
                column,
                width=COLUMN_WIDTHS[column],
                minwidth=60,
                stretch=False,
                anchor="e" if column in ("title", "address") else "center",
            )
            self.tree.heading(
                column,
                text=DISPLAY_NAMES[column],
                command=lambda selected=column: self.change_sort(selected),
            )
        self.tree.grid(row=1, column=0, sticky="nsew")

        vertical = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.tree.yview
        )
        vertical.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vertical.set)

        horizontal = ttk.Scrollbar(
            table_frame, orient="horizontal", command=self._horizontal_scroll
        )
        horizontal.grid(row=2, column=0, sticky="ew")
        self.tree.configure(xscrollcommand=self._set_horizontal_thumb)
        self.horizontal_scrollbar = horizontal

        pager = ttk.Frame(data_tab)
        pager.pack(fill="x", pady=(8, 0))
        self.previous_button = ttk.Button(
            pager, text="‹ Previous", command=self.previous_page
        )
        self.previous_button.pack(side="left")
        self.page_label = ttk.Label(pager, text="Page 1 of 1")
        self.page_label.pack(side="left", padx=12)
        self.next_button = ttk.Button(pager, text="Next ›", command=self.next_page)
        self.next_button.pack(side="left")
        ttk.Button(pager, text="Clear filters", command=self.clear_filters).pack(
            side="right"
        )

        self._build_collector_tab(notebook)

        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            relief="sunken",
            anchor="w",
            padding=(8, 4),
        )
        status.pack(fill="x", side="bottom")

    def _build_collector_tab(self, notebook: ttk.Notebook) -> None:
        collector_tab = ttk.Frame(notebook, padding=(12, 12, 12, 8))
        notebook.add(collector_tab, text="Website collection")

        controls = ttk.LabelFrame(
            collector_tab,
            text="Collection controls",
            padding=(12, 10),
        )
        controls.pack(fill="x")

        throttle = ttk.Frame(controls)
        throttle.pack(side="left", fill="x", expand=True)
        ttk.Label(throttle, text="After every").pack(side="left")
        ttk.Spinbox(
            throttle,
            from_=0,
            to=10000,
            width=7,
            textvariable=self.batch_count_var,
        ).pack(side="left", padx=(6, 6))
        ttk.Label(throttle, text="API queries, wait").pack(side="left")
        ttk.Spinbox(
            throttle,
            from_=0,
            to=3600,
            increment=1,
            width=7,
            textvariable=self.batch_delay_var,
        ).pack(side="left", padx=(6, 6))
        ttk.Label(throttle, text="seconds").pack(side="left")
        ttk.Label(
            throttle,
            text="Set either value to 0 to disable the batch wait.",
            style="Muted.TLabel",
        ).pack(side="left", padx=(14, 0))

        self.force_stop_button = ttk.Button(
            controls,
            text="Force stop",
            command=self.force_stop_collection,
            state="disabled",
        )
        self.force_stop_button.pack(side="right")
        self.pause_button = ttk.Button(
            controls,
            text="Pause",
            command=self.toggle_collection_pause,
            state="disabled",
        )
        self.pause_button.pack(side="right", padx=(0, 8))
        self.start_button = ttk.Button(
            controls,
            text="Start / resume collection",
            command=self.start_collection,
        )
        self.start_button.pack(side="right", padx=(0, 8))

        progress_row = ttk.Frame(collector_tab)
        progress_row.pack(fill="x", pady=(10, 8))
        ttk.Label(
            progress_row,
            textvariable=self.collector_status_var,
        ).pack(side="left")
        ttk.Label(
            progress_row,
            textvariable=self.collector_progress_text,
            style="Muted.TLabel",
        ).pack(side="right")
        self.collector_progress = ttk.Progressbar(
            progress_row,
            mode="determinate",
            maximum=1,
            value=0,
        )
        self.collector_progress.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(14, 14),
        )

        collection_view = ttk.Panedwindow(collector_tab, orient="vertical")
        collection_view.pack(fill="both", expand=True)
        map_panel = ttk.Frame(collection_view)
        console_panel = ttk.Frame(collection_view)
        collection_view.add(map_panel, weight=3)
        collection_view.add(console_panel, weight=2)

        map_toolbar = ttk.Frame(map_panel)
        map_toolbar.pack(fill="x", pady=(0, 5))
        ttk.Label(
            map_toolbar,
            text="Scan coverage map",
            style="Muted.TLabel",
        ).pack(side="left")
        for label, color in (
            ("Downloaded", "#16a34a"),
            ("Failed", "#dc2626"),
            ("Not tried", "#2563eb"),
        ):
            tk.Label(
                map_toolbar,
                text=f"  {label}  ",
                background=color,
                foreground="#ffffff",
                font=("Segoe UI Semibold", 9),
            ).pack(side="left", padx=(10, 0))
        ttk.Button(
            map_toolbar,
            text="Refresh map",
            command=self._refresh_coverage_map,
        ).pack(side="right")
        ttk.Label(
            map_toolbar,
            textvariable=self.map_counts_var,
            style="Muted.TLabel",
        ).pack(side="right", padx=(0, 12))

        self.coverage_map = CoverageMap(
            map_panel,
            cache_directory=APP_DIR / "data" / "map_tiles",
            tiles_ready_callback=self._queue_map_tiles_ready,
            height=300,
        )
        self.coverage_map.pack(fill="both", expand=True)

        console_toolbar = ttk.Frame(console_panel)
        console_toolbar.pack(fill="x", pady=(0, 5))
        ttk.Label(
            console_toolbar,
            text="Collector console — select text normally or use Ctrl+C",
            style="Muted.TLabel",
        ).pack(side="left")
        ttk.Button(
            console_toolbar,
            text="Copy selected",
            command=self.copy_console_selection,
        ).pack(side="right")
        ttk.Button(
            console_toolbar,
            text="Clear console",
            command=self.clear_console,
        ).pack(side="right", padx=(0, 8))

        console_frame = ttk.Frame(console_panel)
        console_frame.pack(fill="both", expand=True)
        console_frame.columnconfigure(0, weight=1)
        console_frame.rowconfigure(0, weight=1)
        self.console = tk.Text(
            console_frame,
            wrap="none",
            background="#111827",
            foreground="#d1d5db",
            insertbackground="#ffffff",
            selectbackground="#2563eb",
            selectforeground="#ffffff",
            font=("Consolas", 10),
            padx=10,
            pady=8,
            undo=False,
            state="disabled",
        )
        self.console.grid(row=0, column=0, sticky="nsew")
        console_y = ttk.Scrollbar(
            console_frame,
            orient="vertical",
            command=self.console.yview,
        )
        console_y.grid(row=0, column=1, sticky="ns")
        console_x = ttk.Scrollbar(
            console_frame,
            orient="horizontal",
            command=self.console.xview,
        )
        console_x.grid(row=1, column=0, sticky="ew")
        self.console.configure(
            yscrollcommand=console_y.set,
            xscrollcommand=console_x.set,
        )
        self.console.bind("<Control-a>", self._select_all_console)
        self.console.bind("<Button-3>", self._show_console_menu)
        self.console_menu = tk.Menu(self.root, tearoff=False)
        self.console_menu.add_command(
            label="Copy selected",
            command=self.copy_console_selection,
        )
        self.console_menu.add_command(
            label="Select all",
            command=self.select_all_console,
        )
        self.console_menu.add_separator()
        self.console_menu.add_command(
            label="Clear console",
            command=self.clear_console,
        )

    def _append_console(self, line: str) -> None:
        self.console.configure(state="normal")
        self.console.insert("end", line.rstrip("\n") + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def clear_console(self) -> None:
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def copy_console_selection(self) -> None:
        try:
            selected = self.console.get("sel.first", "sel.last")
        except tk.TclError:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(selected)

    def select_all_console(self) -> None:
        self.console.tag_add("sel", "1.0", "end-1c")
        self.console.mark_set("insert", "1.0")
        self.console.see("insert")

    def _select_all_console(self, _event=None):
        self.select_all_console()
        return "break"

    def _show_console_menu(self, event) -> None:
        try:
            self.console_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.console_menu.grab_release()

    def _queue_map_tiles_ready(self) -> None:
        self.collector_events.put(("map-tiles-ready",))

    def _refresh_coverage_map(self, settings: Settings | None = None) -> None:
        settings = settings or self.collector_settings
        try:
            points = planned_scan_points(settings, self.database)
            persisted = self.database.scan_point_statuses(settings.endpoint)
            cells = {
                point: persisted.get(point, "pending")
                for point in points
            }
        except Exception as exc:
            self.map_counts_var.set(f"Map unavailable: {exc}")
            return

        counts = {"done": 0, "failed": 0, "pending": 0}
        for status in cells.values():
            counts[status if status in counts else "pending"] += 1
        self.map_counts_var.set(
            f"{counts['done']:,} downloaded  •  "
            f"{counts['failed']:,} failed  •  "
            f"{counts['pending']:,} not tried"
        )
        self.coverage_map.set_coverage(
            cells,
            (
                settings.min_lat,
                settings.max_lat,
                settings.min_lng,
                settings.max_lng,
            ),
            settings.coverage_radius_km,
        )

    def start_collection(self) -> None:
        if self.collector_running:
            return
        try:
            batch_count = int(self.batch_count_var.get().strip())
            batch_delay = float(self.batch_delay_var.get().strip())
            if batch_count < 0 or batch_delay < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid delay settings",
                "Query count and wait seconds must be zero or positive numbers.",
                parent=self.root,
            )
            return

        settings = replace(
            Settings.from_env(),
            batch_query_count=batch_count,
            batch_delay=batch_delay,
        )
        self.collector_settings = settings
        self.database = OfficeDatabase(settings.database_path)
        self._refresh_coverage_map(settings)
        self.collector_control = ScanControl()
        self.collector_login_event.clear()
        self.collector_running = True
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal", text="Pause")
        self.force_stop_button.configure(state="normal")
        self.collector_status_var.set("Preparing collection…")
        self.collector_progress_text.set("0 / 0")
        self.collector_progress.configure(maximum=1, value=0)
        self._append_console(
            "\n=== Starting website collection "
            f"(every {batch_count} queries, wait {batch_delay:g}s) ==="
        )

        handler = QueueLogHandler(self.collector_events)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        self.collector_log_handler = handler
        root_logger = logging.getLogger()
        self.collector_previous_log_level = root_logger.level
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

        def progress(current: int, total: int, status: str) -> None:
            self.collector_events.put(
                ("progress", current, total, status)
            )

        def worker() -> None:
            try:
                exit_code = run_collector(
                    settings,
                    control=self.collector_control,
                    progress_callback=progress,
                    login_callback=self._collector_login_callback,
                )
            except Exception as exc:
                self.collector_events.put(
                    ("log", f"Collector crashed: {exc}")
                )
                exit_code = 1
            finally:
                root_logger.removeHandler(handler)
                root_logger.setLevel(self.collector_previous_log_level)
                self.collector_events.put(("finished", exit_code))

        self.collector_thread = threading.Thread(target=worker, daemon=True)
        self.collector_thread.start()

    def _collector_login_callback(self) -> bool:
        self.collector_events.put(("login",))
        while not self.collector_login_event.wait(0.1):
            if self.collector_control.stop_event.is_set():
                return False
        return not self.collector_control.stop_event.is_set()

    def toggle_collection_pause(self) -> None:
        if not self.collector_running or self.collector_control is None:
            return
        if self.collector_control.pause_event.is_set():
            self.collector_control.resume()
            self.pause_button.configure(text="Pause")
            self.collector_status_var.set("Running")
            self._append_console("--- Collection resumed by user ---")
        else:
            self.collector_control.pause()
            self.pause_button.configure(text="Resume")
            self.collector_status_var.set(
                "Paused (an active request may finish first)"
            )
            self._append_console("--- Collection paused by user ---")

    def force_stop_collection(self) -> None:
        if not self.collector_running or self.collector_control is None:
            return
        if not messagebox.askyesno(
            "Force stop collection",
            (
                "Stop collection now? Completed coordinates are already saved. "
                "An active API request may take a moment to return."
            ),
            parent=self.root,
        ):
            return
        self.collector_control.stop()
        self.collector_login_event.set()
        self.pause_button.configure(state="disabled")
        self.force_stop_button.configure(state="disabled")
        self.collector_status_var.set("Stopping…")
        self._append_console("--- Force stop requested by user ---")

    def _poll_collector_events(self) -> None:
        try:
            while True:
                event = self.collector_events.get_nowait()
                event_type = event[0]
                if event_type == "log":
                    self._append_console(event[1])
                elif event_type == "progress":
                    _, current, total, status = event
                    maximum = max(1, total)
                    self.collector_progress.configure(
                        maximum=maximum,
                        value=min(current, maximum),
                    )
                    self.collector_progress_text.set(
                        f"{current:,} / {total:,}"
                    )
                    labels = {
                        "ready": "Ready",
                        "running": "Running",
                        "batch-delay": "Batch delay",
                        "stopped": "Stopped",
                        "finished": "Finished",
                    }
                    if not (
                        self.collector_control
                        and self.collector_control.pause_event.is_set()
                    ):
                        self.collector_status_var.set(
                            labels.get(status, status)
                        )
                    if status in {"running", "stopped", "finished"}:
                        self._refresh_coverage_map()
                elif event_type == "map-tiles-ready":
                    self.coverage_map.render()
                elif event_type == "login":
                    if (
                        self.collector_control is not None
                        and self.collector_control.stop_event.is_set()
                    ):
                        self.collector_login_event.set()
                        continue
                    self.collector_status_var.set(
                        "Waiting for browser login confirmation"
                    )
                    messagebox.showinfo(
                        "Complete Kateb login",
                        (
                            "Chrome has opened with the dedicated profile.\n\n"
                            "Sign in if needed and wait until the Kateb map is "
                            "fully visible. Then return here and click OK."
                        ),
                        parent=self.root,
                    )
                    self.collector_login_event.set()
                elif event_type == "finished":
                    self._collection_finished(event[1])
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_collector_events)

    def _collection_finished(self, exit_code: int) -> None:
        self.collector_running = False
        self.start_button.configure(state="normal")
        self.pause_button.configure(state="disabled", text="Pause")
        self.force_stop_button.configure(state="disabled")
        if exit_code == 0:
            label = "Collection completed"
        elif exit_code == 130:
            label = "Collection force-stopped; progress saved"
        else:
            label = "Collection finished with failed coordinates"
        self.collector_status_var.set(label)
        self.status_var.set(label)
        self._append_console(f"=== {label} ===\n")
        self._refresh_coverage_map()
        self.current_page = 1
        self.refresh()

    def _on_close(self) -> None:
        if self.collector_running and self.collector_control is not None:
            if not messagebox.askyesno(
                "Collection is running",
                "Force stop collection and close the application?",
                parent=self.root,
            ):
                return
            self.collector_control.stop()
            self.collector_login_event.set()
        self.root.destroy()

    def _build_filter_row(self) -> None:
        x = 0
        for column in DATABASE_COLUMNS:
            width = COLUMN_WIDTHS[column]
            entry = ttk.Entry(
                self.filter_canvas,
                textvariable=self.filter_vars[column],
                justify="right" if column in ("title", "address") else "left",
            )
            self.filter_canvas.create_window(
                x + 3,
                5,
                anchor="nw",
                width=width - 6,
                height=28,
                window=entry,
            )
            self.filter_vars[column].trace_add("write", self._filters_changed)
            x += width
        self.filter_canvas.configure(scrollregion=(0, 0, x, 38))

    def _horizontal_scroll(self, *args) -> None:
        self.tree.xview(*args)
        self.filter_canvas.xview(*args)

    def _set_horizontal_thumb(self, first: str, last: str) -> None:
        self.horizontal_scrollbar.set(first, last)
        self.filter_canvas.xview_moveto(first)

    def _filters_changed(self, *_args) -> None:
        if self.filter_job is not None:
            self.root.after_cancel(self.filter_job)
        self.filter_job = self.root.after(250, self._apply_filters)

    def _apply_filters(self) -> None:
        self.filter_job = None
        self.current_page = 1
        self.refresh()

    def active_filters(self) -> dict[str, str]:
        return {
            column: variable.get().strip()
            for column, variable in self.filter_vars.items()
            if variable.get().strip()
        }

    def refresh(self) -> None:
        filters = self.active_filters()
        self.total_rows = self.database.count(filters)
        page_count = max(1, math.ceil(self.total_rows / PAGE_SIZE))
        self.current_page = min(max(1, self.current_page), page_count)
        offset = (self.current_page - 1) * PAGE_SIZE
        rows = self.database.query(
            filters,
            self.sort_column,
            self.sort_descending,
            PAGE_SIZE,
            offset,
        )

        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        for row in rows:
            self.tree.insert(
                "",
                "end",
                values=[row[column] for column in DATABASE_COLUMNS],
            )

        self.page_label.configure(text=f"Page {self.current_page:,} of {page_count:,}")
        self.previous_button.configure(
            state="normal" if self.current_page > 1 else "disabled"
        )
        self.next_button.configure(
            state="normal" if self.current_page < page_count else "disabled"
        )
        filter_note = " matching filters" if filters else ""
        self.summary_label.configure(
            text=f"{self.total_rows:,} unique records{filter_note}"
        )
        self._update_headings()

    def _update_headings(self) -> None:
        for column in DATABASE_COLUMNS:
            label = DISPLAY_NAMES[column]
            if column == self.sort_column:
                label += " ▼" if self.sort_descending else " ▲"
            self.tree.heading(
                column,
                text=label,
                command=lambda selected=column: self.change_sort(selected),
            )

    def change_sort(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column = column
            self.sort_descending = False
        self.current_page = 1
        self.refresh()

    def previous_page(self) -> None:
        if self.current_page > 1:
            self.current_page -= 1
            self.refresh()

    def next_page(self) -> None:
        page_count = max(1, math.ceil(self.total_rows / PAGE_SIZE))
        if self.current_page < page_count:
            self.current_page += 1
            self.refresh()

    def clear_filters(self) -> None:
        for variable in self.filter_vars.values():
            variable.set("")
        self.current_page = 1
        self.refresh()

    def _set_busy(self, busy: bool, status: str) -> None:
        self.busy = busy
        self.status_var.set(status)
        self.root.configure(cursor="watch" if busy else "")

    def import_folder(self) -> None:
        if self.busy:
            return
        selected = filedialog.askdirectory(
            parent=self.root,
            title="Choose a folder containing JSON files",
            initialdir=str(APP_DIR / "output"),
            mustexist=True,
        )
        if not selected:
            return
        self._set_busy(True, "Importing JSON files and checking telephone duplicates…")

        def worker() -> None:
            try:
                stats = self.database.import_folder(selected)
            except Exception as exc:
                self.root.after(0, lambda: self._import_failed(str(exc)))
                return
            self.root.after(0, lambda: self._import_finished(stats))

        threading.Thread(target=worker, daemon=True).start()

    def _import_failed(self, detail: str) -> None:
        self._set_busy(False, "Import failed")
        messagebox.showerror(
            "Import failed",
            f"The selected folder could not be imported.\n\n{detail}",
            parent=self.root,
        )

    def _import_finished(self, stats) -> None:
        self._set_busy(False, "Import completed")
        self.current_page = 1
        self.refresh()
        messagebox.showinfo(
            "Import completed",
            (
                f"JSON files checked: {stats.files:,}\n"
                f"Records found: {stats.records:,}\n"
                f"New records: {stats.inserted:,}\n"
                f"Duplicates updated: {stats.updated:,}\n"
                f"Skipped: {stats.skipped:,}\n"
                f"Unreadable files: {stats.errors:,}"
            ),
            parent=self.root,
        )

    def export_data(self) -> None:
        if self.busy:
            return
        suggested = APP_DIR / "office_data.xlsx"
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export data to Excel",
            initialdir=str(suggested.parent),
            initialfile=suggested.name,
            defaultextension=".xlsx",
            filetypes=(("Excel workbook", "*.xlsx"),),
        )
        if not selected:
            return
        filters = self.active_filters()
        self._set_busy(True, "Creating Excel workbook…")

        def worker() -> None:
            try:
                rows = self.database.query(
                    filters, self.sort_column, self.sort_descending
                )
                export_xlsx(selected, rows)
            except Exception as exc:
                self.root.after(0, lambda: self._export_failed(str(exc)))
                return
            self.root.after(0, lambda: self._export_finished(selected, len(rows)))

        threading.Thread(target=worker, daemon=True).start()

    def _export_failed(self, detail: str) -> None:
        self._set_busy(False, "Export failed")
        messagebox.showerror(
            "Export failed",
            f"The Excel workbook could not be created.\n\n{detail}",
            parent=self.root,
        )

    def _export_finished(self, path: str, row_count: int) -> None:
        self._set_busy(False, f"Exported {row_count:,} records")
        messagebox.showinfo(
            "Export completed",
            f"{row_count:,} records were exported to:\n\n{path}",
            parent=self.root,
        )


def main() -> None:
    root = tk.Tk()
    OfficeManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
