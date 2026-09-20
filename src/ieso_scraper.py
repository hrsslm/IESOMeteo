"""
IESO data scraper.

Downloads public IESO reports (zonal demand, generation output by fuel
type, and the realtime Ontario zonal price) and converts them into tidy
hourly CSV files.

How it works, in plain language:
1. For each report, fetch its directory index at runtime and list the files.
   Filenames are NEVER hardcoded; we pick the best file per year on the fly.
   For the realtime price report we use IESO's "global link" file
   (PUB_RealtimeOntarioZonalPrice.xml with no date in the name), which
   always resolves to the latest published interval.
2. Prefer CSV files when available, parse XML otherwise.
3. Download each chosen file into data/raw/. Dated files are skipped when
   already present (they never change), but the rolling "global link" files
   (e.g. PUB_DemandZonal.csv with no date in the name) always hold the
   latest data, so they are re-downloaded on every run.
4. Parse everything into data/processed/demand_hourly.csv,
   data/processed/fuelmix_hourly.csv and data/processed/price_hourly.csv.
   Demand and fuel mix are rebuilt from scratch each run; the price file
   *accumulates* history -- each run appends the latest hourly average
   (deduped by timestamp), so the repo keeps a growing price record.

Safety rules:
- A missing index, a failed download, or an unparsable file logs a WARNING
  and we move on. This script never crashes because one file is absent.
- Run with --force to re-download raw files that already exist.

Data source: IESO public reports (no API key needed).
    https://reports-public.ieso.ca/public/
"""

import argparse
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "data" / "raw"
PROCESSED_DIR = BASE_DIR / "data" / "processed"

INDEX_URL = "https://reports-public.ieso.ca/public/{feed}/"

# Each feed: which extension we prefer, how to parse it, and the output file.
FEEDS = {
    "DemandZonal": {
        "preferred_ext": "csv",
        "parse": "parse_demand_zonal",
        "output": "demand_hourly.csv",
    },
    "GenOutputbyFuelHourly": {
        "preferred_ext": "xml",
        "parse": "parse_fuelmix",
        "output": "fuelmix_hourly.csv",
        # A fuel column is NaN when that fuel wasn't reported for the hour
        # (report schemas vary by year); treat that as zero output.
        # "control_actions" is grid-operator dispatch, not generation, so drop it.
        "clean_fuelmix": True,
    },
    "RealtimeOntarioZonalPrice": {
        # The realtime 5-minute Ontario zonal energy price (OZP). Since the
        # May 2025 market renewal this is the successor to the old HOEP:
        # the province-wide headline price, published every 5 minutes.
        # We average each hour's 12 intervals into one hourly row (the
        # hourly average is also the value IESO uses for settlement),
        # matching the hourly grain of the other processed files.
        "preferred_ext": "xml",
        "parse": "parse_price_zonal",
        "output": "price_hourly.csv",
        # The global-link file only holds the latest hour, so instead of
        # rebuilding from scratch we append each run's new hour to the
        # existing file, building a price history over time.
        "append_history": True,
    },
}

# All IESO timestamps below are in Toronto local time.
TORONTO = ZoneInfo("America/Toronto")

HEADERS = {"User-Agent": "Mozilla/5.0 (ontario-weather-grid-dashboard)"}

# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def hour_ending_to_start(date_str, hour_ending):
    """Convert IESO 'Hour Ending' to the interval's start timestamp.

    IESO's Hour column is the *hour ending* in America/Toronto local time,
    numbered 1-24. Hour ending 1 covers midnight-1am, so the interval starts
    at hour_ending - 1. We store ISO 8601 with the UTC offset, e.g.
    "2026-01-01T00:00:00-05:00".

    DST convention: on spring-forward/fall-back days the local wall clock
    skips or repeats an hour. We attach the Toronto timezone to the naive
    wall-clock time and let zoneinfo resolve it deterministically
    (fold=0 for ambiguous times). The convention is documented here rather
    than hidden, so joins against weather data stay predictable.
    """
    year, month, day = (int(part) for part in date_str.split("-"))
    start_hour = int(hour_ending) - 1
    naive = datetime(year, month, day, start_hour)
    return naive.replace(tzinfo=TORONTO).isoformat()


# ---------------------------------------------------------------------------
# Index listing and file selection (no hardcoded filenames)
# ---------------------------------------------------------------------------


def fetch_index(feed):
    """Return the PUB_ filenames listed in a feed's directory index.

    The IESO server often drops connections mid-response (some indexes are
    megabytes of HTML), so we resume partial listings with HTTP Range
    requests, the same trick download() uses for files. A completed read
    (the server closes the connection cleanly) means we have the full
    listing; an IncompleteRead means we resume where we left off.
    run_feed() turns a total failure into a WARNING and skips the feed.
    """
    url = INDEX_URL.format(feed=feed)
    buf = b""
    for attempt in range(1, 7):
        try:
            headers = dict(HEADERS)
            if buf:
                headers["Range"] = f"bytes={len(buf)}-"
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status != 206:
                    buf = b""  # server ignored Range (or first try): start over
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    buf += chunk
            # Reaching here means the server closed the connection cleanly,
            # so buf holds the complete listing.
            html = buf.decode("utf-8", errors="replace")
            names = re.findall(r'href="([^"]+)"', html)
            pubs = [n for n in names if n.startswith(f"PUB_{feed}")]
            print(f"  index lists {len(pubs)} PUB_{feed} files")
            return pubs
        except Exception as exc:  # noqa: BLE001 - resume, then let caller warn
            print(f"  index attempt {attempt} failed "
                  f"({type(exc).__name__}); resuming...")
            time.sleep(2 * attempt)
    raise RuntimeError(f"could not fetch a complete index for {feed}")


def choose_files(filenames, feed, preferred_ext):
    """Pick one file per year: prefer `preferred_ext`, prefer final files.

    IESO publishes files like PUB_DemandZonal_2025.csv plus versioned
    revisions like PUB_DemandZonal_2025_v393.csv. The non-versioned ("final")
    file is the latest revision, so it wins over versioned ones. A rolling
    file with no year (e.g. PUB_DemandZonal.csv) is the live current-year
    file and wins for the current year.

    Returns (chosen, rolling_name): chosen maps year -> filename, and
    rolling_name is the rolling "global link" filename (or None). The
    caller re-downloads the rolling file on every run because it always
    holds the latest data; dated files are only downloaded once.
    """
    pattern = re.compile(rf"^PUB_{re.escape(feed)}(?:_(\d{{4}}))?(?:_v(\d+))?\.(csv|xml)$")
    dated = {}  # (year, ext) -> (filename, version or None for final)
    rolling = None  # (filename, ext)

    for name in filenames:
        match = pattern.match(name)
        if not match:
            continue
        year_str, version_str, ext = match.group(1), match.group(2), match.group(3).lower()
        version = int(version_str) if version_str else None  # None = final file
        if year_str is None:
            if rolling is None or ext == preferred_ext:
                rolling = (name, ext)
            continue
        key = (int(year_str), ext)
        prev = dated.get(key)
        if prev is None:
            dated[key] = (name, version)
        elif version is None and prev[1] is not None:
            dated[key] = (name, version)  # final beats any versioned file
        elif version is not None and prev[1] is not None and version > prev[1]:
            dated[key] = (name, version)  # higher version wins

    by_year = {}  # year -> (filename, ext)
    for (year, ext), (name, _version) in dated.items():
        current = by_year.get(year)
        if current is None or (ext == preferred_ext and current[1] != preferred_ext):
            by_year[year] = (name, ext)

    if rolling:
        current_year = datetime.now(TORONTO).year
        by_year[current_year] = rolling
        print(f"  using rolling file {rolling[0]} for {current_year}")

    chosen = {year: name for year, (name, _ext) in by_year.items()}
    rolling_name = rolling[0] if rolling else None
    return chosen, rolling_name


# ---------------------------------------------------------------------------
# Downloading (urllib, not requests)
# ---------------------------------------------------------------------------


def download(url, dest, force=False):
    """Download a file with retries. Returns True on success.

    NOTE: we use urllib from the standard library here instead of requests
    because the IESO server reliably drops connections mid-download when
    fetched with requests, while urllib (and curl) complete fine.

    The server also sometimes hangs up *cleanly* partway through a file
    (the same file can stop at the exact same byte twice in a row). So we
    verify the size against the Content-Length header, and when we come up
    short we resume with an HTTP Range request instead of starting over.
    A ".part" file holds the in-progress download, so even separate runs
    of this script can pick up where the last one stopped.
    """
    if dest.exists() and not force:
        print(f"  already have {dest.name}, skipping download")
        return True
    tmp = dest.with_suffix(dest.suffix + ".part")
    downloaded = tmp.stat().st_size if tmp.exists() else 0
    for attempt in range(1, 7):
        try:
            headers = dict(HEADERS)
            if downloaded:
                headers["Range"] = f"bytes={downloaded}-"
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=120) as response:
                if response.status == 206:
                    mode = "ab"  # server honored our Range request: append
                else:
                    mode = "wb"  # server ignored Range (or first try): start over
                    downloaded = 0
                remaining = response.getheader("Content-Length")
                remaining = int(remaining) if remaining else None
                # For a 206 response, Content-Length is only the *remaining*
                # bytes; add what we already have to get the expected total.
                expected = (downloaded + remaining) if remaining is not None else None
                with open(tmp, mode) as f:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                if expected is not None and downloaded < expected:
                    print(f"  short read ({downloaded}/{expected} bytes), resuming...")
                    time.sleep(2)
                    continue  # next attempt resumes from `downloaded`
            tmp.rename(dest)  # only reached on a complete download
            print(f"  downloaded {dest.name} ({downloaded / 1e6:.1f} MB)")
            return True
        except Exception as exc:  # noqa: BLE001 - we log and retry everything
            print(f"  attempt {attempt} failed for {dest.name}: {type(exc).__name__}: {exc}")
            time.sleep(2 * attempt)
    print(f"  WARNING: could not download {url}; continuing without it")
    tmp.unlink(missing_ok=True)  # never keep a partial file
    return False


# ---------------------------------------------------------------------------
# Parsers: one per report format
# ---------------------------------------------------------------------------


def parse_demand_zonal(path):
    """Parse a DemandZonal CSV into a tidy hourly DataFrame."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    # The file starts with title rows; the real header begins "Date,Hour,...".
    header_idx = next(i for i, line in enumerate(lines) if line.startswith("Date,Hour"))
    df = pd.read_csv(path, skiprows=header_idx)

    # Defensive cleanup: yearly files vary slightly. Some use YYYY/MM/DD
    # dates, some have blank trailing rows, some quote thousands like "2,300".
    df = df.dropna(subset=["Date", "Hour"])
    dates = df["Date"].astype(str).str.replace("/", "-", regex=False)

    df["timestamp"] = [
        hour_ending_to_start(d, int(float(h))) for d, h in zip(dates, df["Hour"])
    ]
    df = df.rename(columns={
        "Date": "date",
        "Hour": "hour_ending",
        "Ontario Demand": "ontario_demand",
        "Zone Total": "zone_total",
        "Diff": "diff",
    })
    # Zone columns: "Northwest" -> "northwest", etc.
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    df["date"] = dates.values
    numeric_cols = [c for c in df.columns if c not in ("timestamp", "date", "hour_ending")]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
    df = df.dropna(subset=["ontario_demand"])  # keep only real hourly rows
    cols = ["timestamp", "date", "hour_ending", "ontario_demand"] + [
        c for c in df.columns if c not in ("timestamp", "date", "hour_ending", "ontario_demand")
    ]
    return df[cols]


# XML namespace used by all IESO XML reports.
NS = {"ieso": "http://www.ieso.ca/schema"}


def parse_fuelmix(path):
    """Parse a GenOutputbyFuelHourly XML file into a tidy hourly DataFrame."""
    tree = ET.parse(path)
    root = tree.getroot()
    rows = []
    for day in root.findall(".//ieso:DailyData", NS):
        day_el = day.find("ieso:Day", NS)
        if day_el is None or day_el.text is None:
            continue
        date_str = day_el.text.strip()
        for hour_el in day.findall("ieso:HourlyData", NS):
            hour_el_text = hour_el.find("ieso:Hour", NS)
            if hour_el_text is None or hour_el_text.text is None:
                continue
            record = {"timestamp": hour_ending_to_start(date_str, int(hour_el_text.text))}
            for fuel_el in hour_el.findall("ieso:FuelTotal", NS):
                fuel_name_el = fuel_el.find("ieso:Fuel", NS)
                output_el = fuel_el.find("ieso:EnergyValue/ieso:Output", NS)
                if fuel_name_el is None or output_el is None or output_el.text is None:
                    continue  # missing data point: Quality -1 with no Output value
                record[fuel_name_el.text.strip().lower().replace(" ", "_")] = float(output_el.text)
            rows.append(record)
    df = pd.DataFrame(rows)
    # Stable column order: timestamp first, then fuels alphabetically.
    fuel_cols = sorted(c for c in df.columns if c != "timestamp")
    return df[["timestamp"] + fuel_cols]


def parse_price_zonal(path):
    """Parse a RealtimeOntarioZonalPrice XML file into one hourly price row.

    Each file describes a single hour: DocBody carries the delivery date,
    the hour-ending (DeliveryHour), twelve 5-minute ZonalPrice intervals,
    and an AveragePrice element with the hourly-average Ontario zonal
    energy price (LmpCap) -- the same hourly average IESO uses for
    settlement. We store that average, so each file yields one hourly row.
    (run_feed() appends the row to price_hourly.csv, deduped by timestamp,
    so repeated runs build up a price history.)
    """
    tree = ET.parse(path)
    root = tree.getroot()
    body = root.find("ieso:DocBody", NS)
    if body is None:
        raise ValueError("no DocBody element found")
    date_el = body.find("ieso:DeliveryDate", NS)
    hour_el = body.find("ieso:DeliveryHour", NS)
    if date_el is None or hour_el is None or not date_el.text or not hour_el.text:
        raise ValueError("missing DeliveryDate or DeliveryHour")
    avg_el = body.find("ieso:AveragePrice/ieso:LmpCap", NS)
    if avg_el is None or not (avg_el.text or "").strip():
        # The hour's intervals haven't been published yet; nothing to store.
        raise ValueError("hourly AveragePrice not published yet")
    timestamp = hour_ending_to_start(date_el.text.strip(), int(hour_el.text))
    return pd.DataFrame([{
        "timestamp": timestamp,
        "ontario_zonal_price": float(avg_el.text),
    }])


PARSERS = {
    "parse_demand_zonal": parse_demand_zonal,
    "parse_fuelmix": parse_fuelmix,
    "parse_price_zonal": parse_price_zonal,
}


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_feed(feed, cfg, force=False):
    print(f"\n=== {feed} ===")
    try:
        filenames = fetch_index(feed)
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: could not list {feed} index ({type(exc).__name__}: {exc}); skipping feed")
        return

    chosen, rolling_name = choose_files(filenames, feed, cfg["preferred_ext"])
    if not chosen:
        print(f"  WARNING: no usable files found for {feed}; skipping feed")
        return
    print(f"  selected {len(chosen)} files ({min(chosen)}..{max(chosen)})")

    parse = PARSERS[cfg["parse"]]
    frames = []
    for year in sorted(chosen):
        name = chosen[year]
        dest = RAW_DIR / name
        url = INDEX_URL.format(feed=feed) + name
        # The rolling "global link" file always holds the latest data, so
        # refresh it every run. Dated files never change: skip when present.
        refresh = force or (rolling_name is not None and name == rolling_name)
        if not download(url, dest, force=refresh):
            continue  # warned inside download(); keep going
        try:
            df = parse(dest)
            print(f"  parsed {name}: {len(df)} rows")
            frames.append(df)
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: failed to parse {name} ({type(exc).__name__}: {exc}); skipping file")

    if not frames:
        print(f"  WARNING: no usable data for {feed}; nothing written")
        return

    combined = pd.concat(frames, ignore_index=True)

    out_path = PROCESSED_DIR / cfg["output"]
    if cfg.get("append_history"):
        # Feeds whose source file only holds the latest period (like the
        # realtime price global link) accumulate history: merge the new
        # rows into the existing file instead of replacing it.
        try:
            history = pd.read_csv(out_path)
            combined = pd.concat([history, combined], ignore_index=True)
            print(f"  merged with {len(history)} existing rows from {out_path.name}")
        except FileNotFoundError:
            pass  # first run: no history yet
        except Exception as exc:  # noqa: BLE001 - warn, then start fresh
            print(f"  WARNING: could not read existing {out_path.name} ({exc}); starting fresh")

    combined = combined.sort_values("timestamp").drop_duplicates("timestamp", keep="last")

    if cfg.get("clean_fuelmix"):
        # NaN means "fuel not reported this hour" -> zero output.
        fuel_cols = [c for c in combined.columns if c != "timestamp"]
        combined[fuel_cols] = combined[fuel_cols].fillna(0)
        combined = combined.drop(columns=["control_actions"], errors="ignore")

    combined.to_csv(out_path, index=False)
    print(
        f"  WROTE {out_path.relative_to(BASE_DIR)} "
        f"({len(combined)} rows, {combined['timestamp'].iloc[0]} to {combined['timestamp'].iloc[-1]})"
    )


def main():
    parser = argparse.ArgumentParser(description="Download and parse IESO public reports.")
    parser.add_argument("--force", action="store_true",
                        help="re-download raw files even if they already exist")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    for feed, cfg in FEEDS.items():
        run_feed(feed, cfg, force=args.force)

    print("\nDone.")


if __name__ == "__main__":
    main()
