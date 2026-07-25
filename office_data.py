from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape


DATABASE_COLUMNS = (
    "id",
    "tel",
    "tel_normalized",
    "title",
    "address",
    "province_code",
    "city_code",
    "office_id",
    "post_code",
    "scriptorium_type",
    "headship_first_name",
    "headship_last_name",
    "headship_cell_phone",
    "latitude",
    "longitude",
    "source_file",
    "updated_at",
)

DISPLAY_NAMES = {
    "id": "ID",
    "tel": "Telephone",
    "tel_normalized": "Normalized telephone",
    "title": "Title",
    "address": "Address",
    "province_code": "Province code",
    "city_code": "City code",
    "office_id": "Office ID",
    "post_code": "Post code",
    "scriptorium_type": "Office type",
    "headship_first_name": "Head first name",
    "headship_last_name": "Head last name",
    "headship_cell_phone": "Headship phone",
    "latitude": "Latitude",
    "longitude": "Longitude",
    "source_file": "Source file",
    "updated_at": "Updated at",
}

FIELD_ALIASES = {
    "tel": ("tel", "telephone", "phone", "phone_number", "tel_number"),
    "title": ("title", "name", "office_name"),
    "address": ("address", "office_address"),
    "province_code": ("provinceCode", "province_code"),
    "city_code": ("cityCode", "city_code"),
    "office_id": (
        "notarization_office_id",
        "officeId",
        "office_id",
        "id",
        "code",
        "registration_number",
    ),
    "post_code": ("post_code", "postCode", "postal_code", "postalCode"),
    "scriptorium_type": ("scriptorium_type", "scriptoriumType", "office_type"),
    "headship_first_name": ("headship_first_name", "headshipFirstName"),
    "headship_last_name": ("headship_last_name", "headshipLastName"),
    "headship_cell_phone": (
        "headship_cell_phone",
        "headshipCellPhone",
        "manager_phone",
    ),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lng", "lon"),
}

_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


@dataclass
class ImportStats:
    files: int = 0
    records: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0


class ClosingConnection(sqlite3.Connection):
    """Commit or roll back on exit, then always release the Windows file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def normalize_telephone(value: object) -> str:
    """Return a comparison-safe Iranian telephone number."""
    text = _text(value).translate(_DIGIT_TRANSLATION)
    digits = re.sub(r"\D", "", text)
    if digits.startswith("0098"):
        digits = "0" + digits[4:]
    elif digits.startswith("98") and len(digits) in (12, 13):
        digits = "0" + digits[2:]
    elif digits.startswith("9") and len(digits) == 10:
        digits = "0" + digits
    return digits


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _first(data: dict, aliases: tuple[str, ...]) -> str:
    for key in aliases:
        if key in data and data[key] is not None:
            return _text(data[key])
    return ""


def _looks_like_record(value: dict) -> bool:
    keys = set(value)
    has_tel = bool(keys & set(FIELD_ALIASES["tel"]))
    has_office_id = bool(keys & set(FIELD_ALIASES["office_id"]))
    has_title = bool(keys & set(FIELD_ALIASES["title"]))
    has_address = bool(keys & set(FIELD_ALIASES["address"]))
    return has_tel or has_office_id or (has_title and has_address)


def iter_records(value: object):
    if isinstance(value, dict):
        if _looks_like_record(value):
            yield value
            return
        for nested in value.values():
            yield from iter_records(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_records(nested)


def record_from_json(data: dict, source_file: str) -> dict[str, str]:
    record = {
        field: _first(data, aliases)
        for field, aliases in FIELD_ALIASES.items()
    }
    record["tel_normalized"] = normalize_telephone(record["tel"])
    record["source_file"] = source_file
    record["raw_json"] = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if record["tel_normalized"]:
        record["identity_key"] = f"tel:{record['tel_normalized']}"
    elif record["office_id"]:
        record["identity_key"] = f"office:{record['office_id']}"
    else:
        digest = hashlib.sha256(record["raw_json"].encode("utf-8")).hexdigest()
        record["identity_key"] = f"json:{digest}"
    return record


class OfficeDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            factory=ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS offices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tel TEXT NOT NULL DEFAULT '',
                    tel_normalized TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    address TEXT NOT NULL DEFAULT '',
                    province_code TEXT NOT NULL DEFAULT '',
                    city_code TEXT NOT NULL DEFAULT '',
                    office_id TEXT NOT NULL DEFAULT '',
                    post_code TEXT NOT NULL DEFAULT '',
                    scriptorium_type TEXT NOT NULL DEFAULT '',
                    headship_first_name TEXT NOT NULL DEFAULT '',
                    headship_last_name TEXT NOT NULL DEFAULT '',
                    headship_cell_phone TEXT NOT NULL DEFAULT '',
                    latitude TEXT NOT NULL DEFAULT '',
                    longitude TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '',
                    source_file TEXT NOT NULL DEFAULT '',
                    identity_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_offices_tel
                    ON offices(tel_normalized)
                    WHERE tel_normalized <> '';
                CREATE INDEX IF NOT EXISTS ix_offices_office_id
                    ON offices(office_id);
                CREATE INDEX IF NOT EXISTS ix_offices_title
                    ON offices(title);
                CREATE TABLE IF NOT EXISTS import_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    folder TEXT NOT NULL,
                    files INTEGER NOT NULL,
                    records INTEGER NOT NULL,
                    inserted INTEGER NOT NULL,
                    updated INTEGER NOT NULL,
                    skipped INTEGER NOT NULL,
                    errors INTEGER NOT NULL,
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scan_points (
                    endpoint TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    entries_found INTEGER NOT NULL,
                    inserted INTEGER NOT NULL,
                    updated INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'done',
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    last_error TEXT NOT NULL DEFAULT '',
                    scanned_at TEXT NOT NULL,
                    PRIMARY KEY (endpoint, latitude, longitude)
                );
                """
            )
            scan_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(scan_points)")
            }
            migrations = {
                "status": (
                    "ALTER TABLE scan_points "
                    "ADD COLUMN status TEXT NOT NULL DEFAULT 'done'"
                ),
                "attempt_count": (
                    "ALTER TABLE scan_points "
                    "ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1"
                ),
                "last_error": (
                    "ALTER TABLE scan_points "
                    "ADD COLUMN last_error TEXT NOT NULL DEFAULT ''"
                ),
            }
            for column, sql in migrations.items():
                if column not in scan_columns:
                    connection.execute(sql)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_scan_points_status
                ON scan_points(endpoint, status)
                """
            )

    def import_folder(self, folder: str | Path) -> ImportStats:
        folder_path = Path(folder)
        stats = ImportStats()
        json_files = sorted(
            path for path in folder_path.rglob("*") if path.is_file() and path.suffix.lower() == ".json"
        )
        with self.connect() as connection:
            for path in json_files:
                stats.files += 1
                try:
                    with path.open("r", encoding="utf-8-sig") as handle:
                        document = json.load(handle)
                except (OSError, UnicodeError, json.JSONDecodeError):
                    stats.errors += 1
                    continue

                found = False
                for raw_record in iter_records(document):
                    found = True
                    stats.records += 1
                    record = record_from_json(raw_record, str(path))
                    outcome = self._store_record(connection, record)
                    if outcome == "inserted":
                        stats.inserted += 1
                    elif outcome == "updated":
                        stats.updated += 1
                    else:
                        stats.skipped += 1
                if not found:
                    stats.skipped += 1

            connection.execute(
                """
                INSERT INTO import_history (
                    folder, files, records, inserted, updated, skipped, errors, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(folder_path),
                    stats.files,
                    stats.records,
                    stats.inserted,
                    stats.updated,
                    stats.skipped,
                    stats.errors,
                    _utc_now(),
                ),
            )
        return stats

    def _store_record(self, connection: sqlite3.Connection, record: dict[str, str]) -> str:
        existing = None
        if record["tel_normalized"]:
            existing = connection.execute(
                "SELECT * FROM offices WHERE tel_normalized = ?",
                (record["tel_normalized"],),
            ).fetchone()
        if existing is None and record["office_id"]:
            existing = connection.execute(
                "SELECT * FROM offices WHERE office_id = ? ORDER BY id LIMIT 1",
                (record["office_id"],),
            ).fetchone()
        if existing is None:
            existing = connection.execute(
                "SELECT * FROM offices WHERE identity_key = ?",
                (record["identity_key"],),
            ).fetchone()

        now = _utc_now()
        fields = (
            "tel",
            "tel_normalized",
            "title",
            "address",
            "province_code",
            "city_code",
            "office_id",
            "post_code",
            "scriptorium_type",
            "headship_first_name",
            "headship_last_name",
            "headship_cell_phone",
            "latitude",
            "longitude",
            "raw_json",
            "source_file",
            "identity_key",
        )
        if existing is None:
            placeholders = ", ".join("?" for _ in range(len(fields) + 2))
            connection.execute(
                f"""
                INSERT INTO offices ({", ".join(fields)}, created_at, updated_at)
                VALUES ({placeholders})
                """,
                tuple(record[field] for field in fields) + (now, now),
            )
            return "inserted"

        merged = {}
        for field in fields:
            incoming = record[field]
            merged[field] = incoming if incoming != "" else existing[field]
        # Keep the telephone identity stable when a no-phone copy of a record is imported.
        if merged["tel_normalized"]:
            merged["identity_key"] = f"tel:{merged['tel_normalized']}"
        elif merged["office_id"]:
            merged["identity_key"] = f"office:{merged['office_id']}"

        assignments = ", ".join(f"{field} = ?" for field in fields)
        try:
            connection.execute(
                f"UPDATE offices SET {assignments}, updated_at = ? WHERE id = ?",
                tuple(merged[field] for field in fields) + (now, existing["id"]),
            )
        except sqlite3.IntegrityError:
            return "skipped"
        return "updated"

    def store_api_entries(
        self,
        connection: sqlite3.Connection,
        entries: list[dict],
        source: str,
    ) -> tuple[int, int, int]:
        inserted = updated = skipped = 0
        for entry in entries:
            outcome = self._store_record(
                connection,
                record_from_json(entry, source),
            )
            if outcome == "inserted":
                inserted += 1
            elif outcome == "updated":
                updated += 1
            else:
                skipped += 1
        return inserted, updated, skipped

    def scan_point_completed(
        self,
        connection: sqlite3.Connection,
        endpoint: str,
        latitude: float,
        longitude: float,
    ) -> bool:
        return (
            connection.execute(
                """
                SELECT 1 FROM scan_points
                WHERE endpoint = ? AND latitude = ? AND longitude = ?
                  AND status = 'done'
                """,
                (endpoint, latitude, longitude),
            ).fetchone()
            is not None
        )

    def mark_scan_point(
        self,
        connection: sqlite3.Connection,
        endpoint: str,
        latitude: float,
        longitude: float,
        entries_found: int,
        inserted: int,
        updated: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO scan_points (
                endpoint, latitude, longitude, entries_found,
                inserted, updated, status, attempt_count,
                last_error, scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'done', 1, '', ?)
            ON CONFLICT(endpoint, latitude, longitude) DO UPDATE SET
                entries_found = excluded.entries_found,
                inserted = excluded.inserted,
                updated = excluded.updated,
                status = 'done',
                attempt_count = scan_points.attempt_count + 1,
                last_error = '',
                scanned_at = excluded.scanned_at
            """,
            (
                endpoint,
                latitude,
                longitude,
                entries_found,
                inserted,
                updated,
                _utc_now(),
            ),
        )

    def mark_scan_failure(
        self,
        endpoint: str,
        latitude: float,
        longitude: float,
        error: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO scan_points (
                    endpoint, latitude, longitude, entries_found,
                    inserted, updated, status, attempt_count,
                    last_error, scanned_at
                ) VALUES (?, ?, ?, 0, 0, 0, 'failed', 1, ?, ?)
                ON CONFLICT(endpoint, latitude, longitude) DO UPDATE SET
                    status = 'failed',
                    attempt_count = scan_points.attempt_count + 1,
                    last_error = excluded.last_error,
                    scanned_at = excluded.scanned_at
                """,
                (
                    endpoint,
                    latitude,
                    longitude,
                    error,
                    _utc_now(),
                ),
            )

    def completed_scan_count(self, endpoint: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM scan_points
                WHERE endpoint = ? AND status = 'done'
                """,
                (endpoint,),
            ).fetchone()
            return int(row["total"])

    def completed_scan_points(
        self,
        endpoint: str,
    ) -> set[tuple[float, float]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT latitude, longitude
                FROM scan_points
                WHERE endpoint = ? AND status = 'done'
                """,
                (endpoint,),
            ).fetchall()
            return {
                (float(row["latitude"]), float(row["longitude"]))
                for row in rows
            }

    def scan_status_counts(self, endpoint: str) -> dict[str, int]:
        counts = {"done": 0, "failed": 0}
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM scan_points
                WHERE endpoint = ?
                GROUP BY status
                """,
                (endpoint,),
            ).fetchall()
            for row in rows:
                counts[str(row["status"])] = int(row["total"])
        return counts

    def saturated_scan_points(
        self,
        endpoint: str,
        threshold: int,
    ) -> list[tuple[float, float]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT latitude, longitude
                FROM scan_points
                WHERE endpoint = ? AND status = 'done'
                  AND entries_found >= ?
                """,
                (endpoint, threshold),
            ).fetchall()
            return [
                (float(row["latitude"]), float(row["longitude"]))
                for row in rows
            ]

    def count(self, filters: dict[str, str] | None = None) -> int:
        where, params = _where_clause(filters or {})
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS total FROM offices {where}", params
            ).fetchone()
            return int(row["total"])

    def query(
        self,
        filters: dict[str, str] | None = None,
        sort_column: str = "id",
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        if sort_column not in DATABASE_COLUMNS:
            sort_column = "id"
        direction = "DESC" if descending else "ASC"
        where, params = _where_clause(filters or {})
        sql = (
            f"SELECT {', '.join(DATABASE_COLUMNS)} FROM offices {where} "
            f"ORDER BY {sort_column} COLLATE NOCASE {direction}, id ASC"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        with self.connect() as connection:
            return connection.execute(sql, params).fetchall()


def _where_clause(filters: dict[str, str]) -> tuple[str, list[object]]:
    clauses = []
    params: list[object] = []
    for column, value in filters.items():
        if column not in DATABASE_COLUMNS or not value:
            continue
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(f"COALESCE(CAST({column} AS TEXT), '') LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def export_xlsx(path: str | Path, rows: list[sqlite3.Row]) -> None:
    """Create a standards-compliant .xlsx workbook using only the standard library."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    headers = [DISPLAY_NAMES[column] for column in DATABASE_COLUMNS]
    data_rows = [[_text(row[column]) for column in DATABASE_COLUMNS] for row in rows]
    sheet_xml = _worksheet_xml(headers, data_rows)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Office data" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""",
        )
        archive.writestr("xl/styles.xml", _styles_xml())
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def _cell_reference(column_index: int, row_index: int) -> str:
    letters = ""
    number = column_index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row_index}"


def _inline_cell(reference: str, value: str, style: int = 0) -> str:
    xml_safe_value = re.sub(
        r"[\x00-\x08\x0B\x0C\x0E-\x1F]",
        "",
        value,
    )
    safe = escape(xml_safe_value)
    preserve = (
        ' xml:space="preserve"'
        if xml_safe_value != xml_safe_value.strip()
        else ""
    )
    return (
        f'<c r="{reference}" t="inlineStr" s="{style}">'
        f"<is><t{preserve}>{safe}</t></is></c>"
    )


def _worksheet_xml(headers: list[str], rows: list[list[str]]) -> str:
    widths = []
    for index, header in enumerate(headers):
        values = [header] + [row[index] for row in rows[:1000]]
        width = min(max(max((len(value) for value in values), default=8) + 2, 10), 45)
        widths.append(f'<col min="{index + 1}" max="{index + 1}" width="{width}" customWidth="1"/>')

    xml_rows = []
    header_cells = [
        _inline_cell(_cell_reference(index, 1), value, 1)
        for index, value in enumerate(headers)
    ]
    xml_rows.append(f'<row r="1" ht="24" customHeight="1">{"".join(header_cells)}</row>')
    for row_number, row in enumerate(rows, start=2):
        cells = [
            _inline_cell(_cell_reference(index, row_number), value)
            for index, value in enumerate(row)
        ]
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')

    last_cell = _cell_reference(len(headers) - 1, max(len(rows) + 1, 1))
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetViews><sheetView workbookViewId="0" rightToLeft="1"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
<cols>{"".join(widths)}</cols>
<sheetData>{"".join(xml_rows)}</sheetData>
<autoFilter ref="A1:{last_cell}"/>
</worksheet>"""


def _styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""
