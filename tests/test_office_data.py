import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from office_data import OfficeDatabase, export_xlsx, normalize_telephone


class OfficeDataTests(unittest.TestCase):
    def test_legacy_scan_table_is_migrated(self):
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "legacy.sqlite3"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE scan_points (
                        endpoint TEXT NOT NULL,
                        latitude REAL NOT NULL,
                        longitude REAL NOT NULL,
                        entries_found INTEGER NOT NULL,
                        inserted INTEGER NOT NULL,
                        updated INTEGER NOT NULL,
                        scanned_at TEXT NOT NULL,
                        PRIMARY KEY (endpoint, latitude, longitude)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO scan_points
                    VALUES ('/geo', 35.7, 51.4, 3, 2, 1, 'old')
                    """
                )
                connection.commit()
            finally:
                connection.close()

            database = OfficeDatabase(database_path)
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM scan_points"
                ).fetchone()
                columns = {
                    item["name"]
                    for item in connection.execute(
                        "PRAGMA table_info(scan_points)"
                    )
                }
            self.assertTrue(
                {"status", "attempt_count", "last_error"} <= columns
            )
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["attempt_count"], 1)

    def test_telephone_normalization(self):
        variants = (
            "0912 525 4214",
            "۰۹۱۲-۵۲۵-۴۲۱۴",
            "+98 912 525 4214",
            "0098-912-525-4214",
            "9125254214",
        )
        self.assertEqual(
            {normalize_telephone(value) for value in variants},
            {"09125254214"},
        )

    def test_duplicate_telephone_updates_one_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incoming = root / "incoming"
            incoming.mkdir()
            first = {
                "notarization_office_id": "office-1",
                "title": "First title",
                "address": "First address",
                "tel": "۰۹۱۲۵۲۵۴۲۱۴",
            }
            second = {
                **first,
                "title": "Updated title",
                "tel": "+98 912 525 4214",
            }
            (incoming / "first.json").write_text(
                json.dumps(first, ensure_ascii=False), encoding="utf-8"
            )
            (incoming / "second.json").write_text(
                json.dumps(second, ensure_ascii=False), encoding="utf-8"
            )

            database = OfficeDatabase(root / "offices.sqlite3")
            stats = database.import_folder(incoming)

            self.assertEqual(stats.inserted, 1)
            self.assertEqual(stats.updated, 1)
            self.assertEqual(database.count(), 1)
            row = database.query()[0]
            self.assertEqual(row["tel_normalized"], "09125254214")
            self.assertEqual(row["title"], "Updated title")

    def test_filters_sorting_and_excel_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incoming = root / "incoming"
            incoming.mkdir()
            records = [
                {"title": "Alpha", "address": "Tehran", "tel": "02111111111"},
                {"title": "Beta", "address": "Karaj", "tel": "02122222222"},
            ]
            (incoming / "all_entries.json").write_text(
                json.dumps(records), encoding="utf-8"
            )
            database = OfficeDatabase(root / "offices.sqlite3")
            database.import_folder(incoming)

            rows = database.query({"address": "kar"}, "title", True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], "Beta")

            workbook = root / "offices.xlsx"
            export_xlsx(workbook, database.query())
            with zipfile.ZipFile(workbook) as archive:
                self.assertIsNone(archive.testzip())
                for name in archive.namelist():
                    if name.endswith((".xml", ".rels")):
                        ElementTree.fromstring(archive.read(name))


if __name__ == "__main__":
    unittest.main()
