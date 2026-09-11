import json
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from netCDF4 import Dataset

from producer import (
    ProducerError,
    _normalise_longitude,
    _request,
    published_forecast_is_current,
    read_archive,
)


class ProducerTests(unittest.TestCase):
    def test_schedule_avoids_start_of_hour_load_window(self):
        workflow = Path(".github/workflows/update.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "17 7 * * *"', workflow)
        self.assertIn('cron: "17 8 * * *"', workflow)
        self.assertEqual(workflow.count("cron:"), 2)

    def test_requests_are_bounded_and_date_specific(self):
        forecast = _request(date(2026, 9, 10), "forecast", ("ozone",))
        analysis = _request(date(2026, 9, 9), "analysis", ("ozone",))
        self.assertEqual(forecast["leadtime_hour"], [str(value) for value in range(49)])
        self.assertEqual(forecast["date"], ["2026-09-10/2026-09-10"])
        self.assertEqual(analysis["time"], [f"{hour:02d}:00" for hour in range(24)])
        self.assertEqual(analysis["leadtime_hour"], ["0"])

    def test_longitude_conversion(self):
        self.assertAlmostEqual(_normalise_longitude(359.35), -0.65)

    def test_current_or_newer_published_base_skips_second_retrieval(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.json"
            document = {
                "schema_version": 1,
                "provider": "Copernicus Atmosphere Monitoring Service (CAMS)",
                "product": "cams-europe-air-quality-forecasts",
                "model": "ensemble",
                "forecast_base_utc": "2026-09-10T00:00:00Z",
                "hourly": [{"timestamp_utc": "2026-09-10T00:00:00Z"}],
            }
            output.write_text(json.dumps(document), encoding="utf-8")

            self.assertTrue(
                published_forecast_is_current(output, date(2026, 9, 10))
            )
            self.assertTrue(
                published_forecast_is_current(output, date(2026, 9, 9))
            )
            self.assertFalse(
                published_forecast_is_current(output, date(2026, 9, 11))
            )

    def test_corrupt_published_file_does_not_suppress_retrieval(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.json"
            output.write_text("not json", encoding="utf-8")

            self.assertFalse(
                published_forecast_is_current(output, date(2026, 9, 10))
            )

    def test_reads_real_cams_shape_and_nearest_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nc_path = root / "ENS_FORECAST.nc"
            with Dataset(nc_path, "w", format="NETCDF4") as document:
                document.setncattr("FORECAST", "Europe, 20260910+[0H_1H]")
                document.createDimension("time", 2)
                document.createDimension("level", 1)
                document.createDimension("latitude", 2)
                document.createDimension("longitude", 3)
                times = document.createVariable("time", "f4", ("time",))
                times.long_name = "FORECAST time from 20260910"
                times[:] = [0, 1]
                document.createVariable("level", "f4", ("level",))[:] = [0]
                document.createVariable("latitude", "f4", ("latitude",))[:] = [38.05, 38.15]
                document.createVariable("longitude", "f4", ("longitude",))[:] = [359.25, 359.35, 359.45]
                values = document.createVariable(
                    "pm10_conc", "f4", ("time", "level", "latitude", "longitude"), fill_value=-999.0
                )
                values.units = "µg/m3"
                values[:] = [[[[1, 2, 3], [4, 5, 6]]], [[[7, 8, 9], [10, 11, 12]]]]
            archive_path = root / "forecast.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(nc_path, nc_path.name)
            result = read_archive(archive_path, "forecast")
        self.assertAlmostEqual(result["grid"][0], 38.05, places=4)
        self.assertAlmostEqual(result["grid"][1], -0.65, places=4)
        self.assertEqual(
            result["rows"][datetime(2026, 9, 10, tzinfo=timezone.utc)]["particulate_matter_10um"],
            2.0,
        )

    def test_rejects_wrong_units(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nc_path = root / "ENS_FORECAST.nc"
            with Dataset(nc_path, "w", format="NETCDF4") as document:
                document.setncattr("FORECAST", "Europe, 20260910+[0H_0H]")
                for name in ("time", "level", "latitude", "longitude"):
                    document.createDimension(name, 1)
                document.createVariable("time", "f4", ("time",))[:] = [0]
                document.createVariable("level", "f4", ("level",))[:] = [0]
                document.createVariable("latitude", "f4", ("latitude",))[:] = [38.05]
                document.createVariable("longitude", "f4", ("longitude",))[:] = [359.35]
                values = document.createVariable(
                    "pm10_conc", "f4", ("time", "level", "latitude", "longitude")
                )
                values.units = "kg/m3"
                values[:] = [[[[1]]]]
            archive_path = root / "forecast.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(nc_path, nc_path.name)
            with self.assertRaises(ProducerError):
                read_archive(archive_path, "forecast")


if __name__ == "__main__":
    unittest.main()
