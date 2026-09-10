#!/usr/bin/env python3
"""Publish a tiny validated CAMS subset for Guardamar del Segura."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cdsapi
from netCDF4 import Dataset


DATASET = "cams-europe-air-quality-forecasts"
ADS_URL = "https://ads.atmosphere.copernicus.eu/api"
TARGET_LATITUDE = 38.0909
TARGET_LONGITUDE = -0.6556
BBOX = [38.2, -0.8, 38.0, -0.5]
CORE_VARIABLES = (
    "particulate_matter_2.5um",
    "particulate_matter_10um",
    "ozone",
    "nitrogen_dioxide",
    "sulphur_dioxide",
)
OPTIONAL_VARIABLES = (
    "dust",
    "pm10_wildfires",
    "alder_pollen",
    "birch_pollen",
    "grass_pollen",
    "mugwort_pollen",
    "olive_pollen",
    "ragweed_pollen",
)
NETCDF_NAMES = {
    "pm2p5_conc": "particulate_matter_2.5um",
    "pm10_conc": "particulate_matter_10um",
    "o3_conc": "ozone",
    "no2_conc": "nitrogen_dioxide",
    "so2_conc": "sulphur_dioxide",
    "dust": "dust",
    "pmwf_conc": "pm10_wildfires",
    "apg_conc": "alder_pollen",
    "bpg_conc": "birch_pollen",
    "gpg_conc": "grass_pollen",
    "mpg_conc": "mugwort_pollen",
    "opg_conc": "olive_pollen",
    "rwpg_conc": "ragweed_pollen",
}
EXPECTED_UNITS = {
    **{name: "µg/m3" for name in CORE_VARIABLES + OPTIONAL_VARIABLES[:2]},
    **{name: "grains/m3" for name in OPTIONAL_VARIABLES[2:]},
}


class ProducerError(RuntimeError):
    pass


def _request(
    run_date: date, kind: str, variables: tuple[str, ...]
) -> dict[str, Any]:
    is_forecast = kind == "forecast"
    return {
        "variable": list(variables),
        "model": ["ensemble"],
        "level": ["0"],
        "date": [f"{run_date.isoformat()}/{run_date.isoformat()}"],
        "type": [kind],
        "time": ["00:00"] if is_forecast else [f"{hour:02d}:00" for hour in range(24)],
        "leadtime_hour": [str(hour) for hour in range(49)] if is_forecast else ["0"],
        "data_format": "netcdf_zip",
        "area": BBOX,
    }


def _normalise_longitude(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def _time_origin(document: Dataset, kind: str) -> datetime:
    variable = document.variables.get("time")
    if variable is None:
        raise ProducerError("NetCDF has no time coordinate")
    candidates = [str(getattr(variable, "long_name", ""))]
    candidates.extend(str(document.getncattr(name)) for name in document.ncattrs())
    for candidate in candidates:
        match = re.search(r"(?:from |, )(\d{8})(?:\+|$)", candidate)
        if match:
            return datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=timezone.utc)
    raise ProducerError(f"Cannot determine {kind} time origin")


def _nearest_grid(document: Dataset) -> tuple[int, int, float, float]:
    latitudes = document.variables["latitude"][:]
    raw_longitudes = document.variables["longitude"][:]
    candidates = []
    for lat_index, raw_latitude in enumerate(latitudes):
        for lon_index, raw_longitude in enumerate(raw_longitudes):
            latitude = float(raw_latitude)
            longitude = _normalise_longitude(float(raw_longitude))
            distance = (
                (latitude - TARGET_LATITUDE) ** 2
                + (longitude - TARGET_LONGITUDE) ** 2
            )
            candidates.append(
                (distance, lat_index, lon_index, latitude, longitude)
            )
    _, lat_index, lon_index, latitude, longitude = min(candidates)
    return lat_index, lon_index, latitude, longitude


def _value_at(variable: Any, time_index: int, lat_index: int, lon_index: int) -> float | None:
    indexes = []
    for dimension in variable.dimensions:
        if dimension == "time":
            indexes.append(time_index)
        elif dimension == "latitude":
            indexes.append(lat_index)
        elif dimension == "longitude":
            indexes.append(lon_index)
        elif dimension == "level":
            indexes.append(0)
        else:
            raise ProducerError(f"Unsupported NetCDF dimension: {dimension}")
    raw = variable[tuple(indexes)]
    if getattr(raw, "mask", False) is True:
        return None
    value = float(raw)
    return value if math.isfinite(value) and value >= 0 else None


def read_archive(path: Path, kind: str) -> dict[str, Any]:
    rows: dict[datetime, dict[str, float]] = {}
    grid: tuple[float, float] | None = None
    base: datetime | None = None
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.endswith(".nc")]
        if not members:
            raise ProducerError("CAMS archive contains no NetCDF file")
        for member in members:
            with Dataset("inmemory.nc", memory=archive.read(member)) as document:
                origin = _time_origin(document, kind)
                if base is not None and origin != base:
                    raise ProducerError("CAMS archive mixes time origins")
                base = origin
                lat_index, lon_index, latitude, longitude = _nearest_grid(document)
                if grid is not None and grid != (latitude, longitude):
                    raise ProducerError("CAMS archive mixes grid points")
                grid = (latitude, longitude)
                times = document.variables["time"][:]
                for source_name, target_name in NETCDF_NAMES.items():
                    if source_name not in document.variables:
                        continue
                    variable = document.variables[source_name]
                    units = str(getattr(variable, "units", ""))
                    if units != EXPECTED_UNITS[target_name]:
                        raise ProducerError(
                            f"Unexpected units for {target_name}: {units}"
                        )
                    for index, offset in enumerate(times):
                        timestamp = origin + timedelta(hours=float(offset))
                        value = _value_at(variable, index, lat_index, lon_index)
                        if value is not None:
                            rows.setdefault(timestamp, {})[target_name] = value
    if base is None or grid is None:
        raise ProducerError("CAMS archive is empty")
    return {"base": base, "grid": grid, "rows": rows}


def _validate(
    analysis: dict[str, Any], forecast: dict[str, Any], forecast_date: date
) -> None:
    if analysis["grid"] != forecast["grid"]:
        raise ProducerError("Analysis and forecast use different grid points")
    expected_analysis = {
        datetime.combine(forecast_date - timedelta(days=2), datetime.min.time(), timezone.utc)
        + timedelta(hours=hour)
        for hour in range(48)
    }
    expected_forecast = {
        datetime.combine(forecast_date, datetime.min.time(), timezone.utc)
        + timedelta(hours=hour)
        for hour in range(49)
    }
    if set(analysis["rows"]) != expected_analysis:
        raise ProducerError("Analysis does not contain the previous 24 UTC hours")
    if set(forecast["rows"]) != expected_forecast:
        raise ProducerError("Forecast does not contain lead hours 0 through 48")
    for kind, rows in (("analysis", analysis["rows"]), ("forecast", forecast["rows"])):
        for timestamp, values in rows.items():
            missing = set(CORE_VARIABLES) - set(values)
            if missing:
                raise ProducerError(
                    f"{kind} {timestamp.isoformat()} lacks core variables: {sorted(missing)}"
                )


def _download(client: cdsapi.Client, request: dict[str, Any], target: Path) -> dict[str, Any]:
    started = time.monotonic()
    result = client.retrieve(DATASET, request)
    ready = time.monotonic()
    result.download(str(target))
    finished = time.monotonic()
    return {
        "request_seconds": round(ready - started, 3),
        "download_seconds": round(finished - ready, 3),
        "size_bytes": target.stat().st_size,
    }


def produce(output: Path, forecast_date: date, token: str) -> dict[str, Any]:
    if not token:
        raise ProducerError("CAMS_ADS_TOKEN is required")
    client = cdsapi.Client(
        url=ADS_URL,
        key=token,
        quiet=True,
        timeout=60,
        retry_max=3,
        sleep_max=10,
        progress=False,
    )
    with tempfile.TemporaryDirectory(prefix="guardamar-cams-") as directory:
        temporary = Path(directory)
        analysis_path = temporary / "analysis.zip"
        forecast_path = temporary / "forecast.zip"
        analysis_request = _request(
            forecast_date - timedelta(days=1), "analysis", CORE_VARIABLES
        )
        analysis_request["date"] = [
            f"{(forecast_date - timedelta(days=2)).isoformat()}/"
            f"{(forecast_date - timedelta(days=1)).isoformat()}"
        ]
        analysis_metrics = _download(client, analysis_request, analysis_path)
        forecast_metrics = _download(
            client,
            _request(forecast_date, "forecast", CORE_VARIABLES + OPTIONAL_VARIABLES),
            forecast_path,
        )
        analysis = read_archive(analysis_path, "analysis")
        forecast = read_archive(forecast_path, "forecast")
    _validate(analysis, forecast, forecast_date)
    hourly = []
    for kind, result in (("analysis", analysis), ("forecast", forecast)):
        for timestamp, values in sorted(result["rows"].items()):
            hourly.append(
                {
                    "timestamp_utc": timestamp.isoformat().replace("+00:00", "Z"),
                    "kind": kind,
                    "values": {name: round(value, 6) for name, value in sorted(values.items())},
                }
            )
    latitude, longitude = forecast["grid"]
    document = {
        "schema_version": 1,
        "provider": "Copernicus Atmosphere Monitoring Service (CAMS)",
        "product": DATASET,
        "model": "ensemble",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "forecast_base_utc": forecast["base"].isoformat().replace("+00:00", "Z"),
        "analysis_base_utc": analysis["base"].isoformat().replace("+00:00", "Z"),
        "requested_location": {
            "latitude": TARGET_LATITUDE,
            "longitude": TARGET_LONGITUDE,
            "bbox": BBOX,
        },
        "grid_location": {"latitude": latitude, "longitude": longitude},
        "units": EXPECTED_UNITS,
        "hourly": hourly,
        "retrieval": {"analysis": analysis_metrics, "forecast": forecast_metrics},
        "attribution": (
            f"Contains modified Copernicus Atmosphere Monitoring Service "
            f"information {forecast_date.year}."
        ),
        "disclaimer": (
            "Neither the European Commission nor ECMWF is responsible for "
            "this derived product or its use."
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output.parent, delete=False
    ) as temporary:
        json.dump(document, temporary, ensure_ascii=False, separators=(",", ":"))
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, output)
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    args = parser.parse_args()
    document = produce(args.output, args.date, os.environ.get("CAMS_ADS_TOKEN", "").strip())
    print(
        json.dumps(
            {
                "forecast_base_utc": document["forecast_base_utc"],
                "grid_location": document["grid_location"],
                "hour_count": len(document["hourly"]),
                "retrieval": document["retrieval"],
                "json_size_bytes": args.output.stat().st_size,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
