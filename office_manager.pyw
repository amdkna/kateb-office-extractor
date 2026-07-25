from __future__ import annotations

import math
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from office_data import (
    DATABASE_COLUMNS,
    DISPLAY_NAMES,
    OfficeDatabase,
    export_xlsx,
)


APP_DIR = Path(__file__).resolve().parent
DATABASE_PATH = APP_DIR / "data" / "office_data.sqlite3"
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


class OfficeManagerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.database = OfficeDatabase(DATABASE_PATH)
        self.sort_column = "id"
        self.sort_descending = False
        self.current_page = 1
        self.total_rows = 0
        self.filter_job = None
        self.busy = False
        self.filter_vars = {
            column: tk.StringVar(root) for column in DATABASE_COLUMNS
        }

        self._configure_window()
        self._build_menu()
        self._build_layout()
        self.refresh()

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

        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            relief="sunken",
            anchor="w",
            padding=(8, 4),
        )
        status.pack(fill="x", side="bottom")

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
