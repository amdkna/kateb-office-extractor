import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from extractor import Settings, grid_points, refinement_points, run
from office_data import OfficeDatabase


def test_settings(database_path: Path) -> Settings:
    return Settings(
        base_url="https://example.test",
        endpoint="/geo",
        search_url="https://example.test/search",
        auth_mode="public",
        chrome_exe="",
        chrome_profile_dir="",
        database_path=database_path,
        min_lat=35.60,
        max_lat=35.60,
        min_lng=51.40,
        max_lng=51.40,
        coverage_radius_km=1.8,
        request_delay=0,
        timeout=1,
        max_retries=0,
        verify_ssl=True,
        headless=True,
        saturation_threshold=45,
        user_agent="test",
        cookie_header="",
    )


class ExtractorTests(unittest.TestCase):
    def test_grid_has_far_fewer_points_than_dense_square_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = replace(
                test_settings(Path(temporary) / "db.sqlite3"),
                min_lat=35.45,
                max_lat=35.95,
                min_lng=50.80,
                max_lng=52.05,
            )
            points = grid_points(settings)
            self.assertGreater(len(points), 700)
            self.assertLess(len(points), 1000)
            self.assertTrue(any(lat < settings.min_lat for lat, _ in points))
            self.assertTrue(any(lng < settings.min_lng for _, lng in points))

            maximum_distance = 0.0
            for lat_index in range(21):
                latitude = settings.min_lat + (
                    settings.max_lat - settings.min_lat
                ) * lat_index / 20
                for lng_index in range(51):
                    longitude = settings.min_lng + (
                        settings.max_lng - settings.min_lng
                    ) * lng_index / 50
                    nearest = min(
                        math.hypot(
                            (latitude - point_lat) * 110.574,
                            (longitude - point_lng)
                            * 111.320
                            * math.cos(math.radians(latitude)),
                        )
                        for point_lat, point_lng in points
                    )
                    maximum_distance = max(maximum_distance, nearest)
            self.assertLessEqual(
                maximum_distance,
                settings.coverage_radius_km,
            )

    def test_refinement_stays_inside_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = replace(
                test_settings(Path(temporary) / "db.sqlite3"),
                min_lat=35.50,
                max_lat=35.60,
                min_lng=51.30,
                max_lng=51.40,
            )
            points = refinement_points(settings, 35.50, 51.30)
            self.assertEqual(len(points), 1)
            self.assertGreater(points[0][0], 35.50)
            self.assertGreater(points[0][1], 51.30)

    def test_run_deduplicates_by_tel_and_resumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "offices.sqlite3"
            settings = test_settings(database_path)
            response = {
                "data": [
                    {
                        "notarization_office_id": "one",
                        "title": "Original",
                        "address": "Tehran",
                        "tel": "۰۹۱۲۵۲۵۴۲۱۴",
                    },
                    {
                        "notarization_office_id": "two",
                        "title": "Updated duplicate",
                        "address": "Tehran",
                        "tel": "+98 912 525 4214",
                    },
                ]
            }
            expected_points = len(grid_points(settings))

            with patch("extractor.request_public", return_value=response) as request:
                self.assertEqual(run(settings), 0)
                self.assertEqual(request.call_count, expected_points)

            database = OfficeDatabase(database_path)
            self.assertEqual(database.count(), 1)
            self.assertEqual(database.query()[0]["title"], "Updated duplicate")
            self.assertEqual(
                database.completed_scan_count("/geo"),
                expected_points,
            )

            with patch("extractor.request_public") as request:
                self.assertEqual(run(settings), 0)
                request.assert_not_called()

    def test_failed_coordinate_is_logged_and_only_failure_is_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "offices.sqlite3"
            settings = test_settings(database_path)
            expected_points = len(grid_points(settings))
            responses = [RuntimeError("temporary authentication failure")]
            responses.extend({"data": []} for _ in range(expected_points - 1))

            with patch(
                "extractor.request_public",
                side_effect=responses,
            ):
                self.assertEqual(run(settings), 1)

            database = OfficeDatabase(database_path)
            self.assertEqual(
                database.scan_status_counts("/geo"),
                {"done": expected_points - 1, "failed": 1},
            )

            with patch(
                "extractor.request_public",
                return_value={"data": []},
            ) as request:
                self.assertEqual(run(settings), 0)
                request.assert_called_once()

            self.assertEqual(
                database.scan_status_counts("/geo"),
                {"done": expected_points, "failed": 0},
            )


if __name__ == "__main__":
    unittest.main()
