# Guardamar CAMS data

This public repository publishes one small, validated JSON subset of the
official CAMS European Air Quality Forecasts for Guardamar del Segura. The
scheduled GitHub Action attempts at 07:17 UTC and once more at 08:17 UTC. Both
avoid the start-of-hour GitHub Actions load window; the first normally publishes
early, while the second follows the guaranteed 08:00 UTC availability. The bot
uses bounded later checkpoints in `Europe/Madrid`, so GitHub schedule delay does
not turn one early miss into a day-long stale cycle.
If the same or a newer forecast base is already published, the later run exits
before ADS retrieval and creates no commit.

The producer retrieves only:

- the previous two UTC days of hourly ensemble analyses for PM2.5, PM10, O3,
  NO2 and SO2, providing complete trailing windows at local midnight;
- the current 00 UTC ensemble forecast at lead hours 0–48 for those pollutants,
  PM10 dust, PM10 wildfire contribution and alder, birch, grass, mugwort,
  olive and ragweed pollen;
- a 0.2° × 0.3° area around Guardamar, from which it selects the nearest grid
  point deterministically.

The workflow replaces `data/latest.json` only after complete retrieval,
parsing and validation. A failure therefore leaves the previous valid file
available. The ADS credential is stored only as the Actions secret
`CAMS_ADS_TOKEN`.

## Attribution

Contains modified Copernicus Atmosphere Monitoring Service information. CAMS
data are used under the [CC-BY 4.0 licence](https://creativecommons.org/licenses/by/4.0/).
Neither the European Commission nor ECMWF is responsible for this derived
JSON or its use.
