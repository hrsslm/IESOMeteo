"""
Open-Meteo archive weather fetcher.

Pulls hourly historical weather for the project locations from the
Open-Meteo ERA5 archive (no API key needed) and saves one tidy CSV.

How it works, in plain language:
1. For each location, request 2 years of hourly data ending ~5 days ago
   (the ERA5 reanalysis lags a few days behind real time).
2. Cache the raw JSON response on disk in data/raw/ so reruns never
   re-hit the API for data we already have (idempotent).
3. Combine everything into data/processed/weather_hourly.csv with
   ISO 8601 timestamps that carry the Toronto UTC offset.

Run with --force to ignore the cache and re-fetch.

Data source: Open-Meteo (https://open-meteo.com/), CC-BY 4.0.
"""

import argparse
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "data" / "raw"
PROCESSED_DIR = BASE_DIR / "data" / "processed"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
TIMEZONE = "America/Toronto"
TORONTO = ZoneInfo(TIMEZONE)

# M2: all seven locations. Kincardine sits near Bruce Nuclear and the
# Lake Huron wind farms; the rest are Ontario's major population centres.
LOCATIONS = {
    "Toronto": (43.65, -79.38),
    "Kincardine": (44.18, -81.63),
    "Ottawa": (45.42, -75.70),
    "Windsor": (42.31, -83.04),
    "London": (42.98, -81.25),
    "Thunder Bay": (48.38, -89.25),
    "Sudbury": (46.49, -80.99),
}

HOURLY_VARS = [
    "temperature_2m",
    "apparent_temperature",
    "wind_speed_100m",
    "shortwave_radiation",
    "cloud_cover",
]

YEARS_OF_HISTORY = 2
# ERA5 reanalysis is published with a lag; 5 days is a safe margin.
ARCHIVE_LAG_DAYS = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def archive_window():
    """Return (start, end) dates for the 2-year archive pull."""
    end = date.today() - timedelta(days=ARCHIVE_LAG_DAYS)
    start = end - timedelta(days=365 * YEARS_OF_HISTORY)
    return start, end


def add_utc_offset(ts_str):
    """Turn Open-Meteo's local "YYYY-MM-DDTHH:MM" into ISO 8601 with offset.

    The API returns Toronto wall-clock time with no offset when we pass
    timezone=America/Toronto. We attach the zone so every timestamp is
    unambiguous, e.g. "2024-09-15T00:00:00-04:00". Same DST convention as
    the IESO scraper: zoneinfo resolves ambiguous/repeated hours
    deterministically (fold=0).
    """
    return datetime.fromisoformat(ts_str).replace(tzinfo=TORONTO).isoformat()


def fetch_location(name, lat, lon, start, end, force=False):
    """Fetch (or load from cache) one location's archive data as a DataFrame."""
    cache_path = RAW_DIR / f"weather_{name}_{start}_{end}.json"

    if cache_path.exists() and not force:
        print(f"  cache hit: {cache_path.name}")
    else:
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": ",".join(HOURLY_VARS),
            "timezone": TIMEZONE,
        }
        print(f"  requesting {name} ({lat}, {lon}) {start}..{end}")
        for attempt in range(1, 4):
            try:
                response = requests.get(ARCHIVE_URL, params=params, timeout=120)
                response.raise_for_status()
                cache_path.write_text(response.text)
                print(f"  cached {cache_path.name} ({cache_path.stat().st_size / 1e6:.1f} MB)")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  attempt {attempt} failed: {type(exc).__name__}: {exc}")
                time.sleep(2 * attempt)
        else:
            print(f"  WARNING: could not fetch weather for {name}; continuing without it")
            return None

    payload = json.loads(cache_path.read_text())
    hourly = payload.get("hourly") or {}
    if "time" not in hourly:
        print(f"  WARNING: no hourly data in {cache_path.name}; skipping location")
        return None

    df = pd.DataFrame(hourly)
    df["location"] = name
    df["timestamp"] = df["time"].map(add_utc_offset)
    return df[["timestamp", "location"] + HOURLY_VARS]


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Fetch Open-Meteo archive weather.")
    parser.add_argument("--force", action="store_true",
                        help="ignore the disk cache and re-fetch from the API")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    start, end = archive_window()
    print(f"Archive window: {start} to {end} (timezone {TIMEZONE})")

    frames = []
    for name, (lat, lon) in LOCATIONS.items():
        print(f"\n=== {name} ===")
        df = fetch_location(name, lat, lon, start, end, force=args.force)
        if df is not None:
            print(f"  parsed {len(df)} hourly rows")
            frames.append(df)

    if not frames:
        print("\nWARNING: no weather data fetched; nothing written")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(["location", "timestamp"])
    combined = combined.drop_duplicates(["location", "timestamp"], keep="last")
    out_path = PROCESSED_DIR / "weather_hourly.csv"
    combined.to_csv(out_path, index=False)
    print(
        f"\nWROTE {out_path.relative_to(BASE_DIR)} "
        f"({len(combined)} rows, {combined['timestamp'].iloc[0]} to {combined['timestamp'].iloc[-1]})"
    )
    print("Done.")


if __name__ == "__main__":
    main()
