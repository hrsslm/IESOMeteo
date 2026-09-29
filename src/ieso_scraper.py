"""
IESO data scraper.

Downloads public IESO reports (zonal demand, generation output by fuel
type, and the realtime Ontario zonal price) and converts them into tidy
hourly CSV files.

How it works, in plain language:
1. For each report, fetch its directory index at runtime and list the files.
   Filenames are NEVER hardcoded.
2. Demand and fuel mix are one-file-per-year feeds. A manifest file,
   data/processed/manifest.json (committed to git), records which raw files
   are already incorporated into the processed CSVs, so each run downloads
   only what is new: the rolling current-year file (it always holds the
   latest data) plus any dated file the manifest has not seen yet -- a new
   year, a new revision, or a final file replacing a versioned one. New rows
   are merged into the existing CSV, so a missed week is fully backfilled.
   data/raw/ is disposable scratch: on a fresh machine the manifest is the
   state, not the raw downloads.
3. The realtime price is an hourly backfill. Each run downloads every dated
   per-hour file newer than the last timestamp in price_hourly.csv (minus a
   7-day overlap that picks up revised averages and heals recent failures),
   plus the rolling "global link" file, in case the very latest hour is not
   in the dated listing yet. Hours whose files fail to download or parse are
   recorded in the manifest and retried on every later run until they
   succeed -- even when they fall outside the 7-day overlap. After each run
   the script reports any hourly slots still missing from the price history.
   The index only retains about 3 months of hourly files.
4. Prefer CSV files when available, parse XML otherwise.

Safety rules:
- A missing index, a failed download, or an unparsable file logs a WARNING
  and we move on. This script never crashes because one file is absent.
- Run with --force to re-download raw files that already exist (file feeds
  re-parse everything chosen; the price feed refreshes the last 7 days, to
  pick up revised hourly averages).

Data source: IESO public reports (no API key needed).
    https://reports-public.ieso.ca/public/
"""

import argparse
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
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
        "parse": "parse_price_zonal",
        "output": "price_hourly.csv",
        # Hourly backfill (not one-file-per-year): every run downloads the
        # dated per-hour files newer than the last stored timestamp, plus
        # the global-link file for the very latest hour, and appends the
        # hourly averages (deduped). The repo accumulates a price history.
        "backfill_hourly": True,
    },
}

# manifest.json lives next to the processed CSVs (so it is committed to
# git) and records, per feed, which raw files are already incorporated.
# It is what makes weekly runs incremental without keeping data/raw/.
MANIFEST_PATH = PROCESSED_DIR / "manifest.json"

# Manifest key holding price hourly slots (like "2026091413") whose files
# have failed to download or parse so far. They are retried on every run
# until they succeed; a slot leaves this list only after its data parses
# and is incorporated into price_hourly.csv.
PRICE_RETRY_KEY = "price_retry_slots"

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
# Manifest: which raw files are already in the processed CSVs
# ---------------------------------------------------------------------------


def load_manifest():
    """Read manifest.json, or return {} when there is none / it is broken.

    The manifest maps feed -> {year -> raw filename} for the file feeds.
    It is committed to git next to the processed CSVs, so a fresh machine
    (or a GitHub Actions runner with an empty data/raw/) still knows what
    is already incorporated and only downloads what is new.
    """
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"  no usable manifest ({type(exc).__name__}); treating all files as new")
        return {}


def save_manifest(manifest):
    """Write the manifest back to data/processed/manifest.json."""
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Price feed: dated per-hour file selection (no hardcoded filenames)
# ---------------------------------------------------------------------------

# Dated price files look like PUB_RealtimeOntarioZonalPrice_2026092714.xml
# (date + hour-ending, Toronto local time), with revisions like
# ..._2026092714_v3.xml. The bare PUB_RealtimeOntarioZonalPrice.xml is the
# rolling "global link" to the latest published hour.
PRICE_DATED_RE = re.compile(r"^PUB_RealtimeOntarioZonalPrice_(\d{10})(?:_v(\d+))?\.xml$")
PRICE_ROLLING_NAME = "PUB_RealtimeOntarioZonalPrice.xml"


def choose_price_files(filenames):
    """Pick the best file per hourly slot from the price index.

    The non-versioned file is the final revision, so it wins for its slot;
    otherwise the highest _vN revision wins. Returns (slots, rolling_name)
    where slots maps "YYYYMMDDHH" -> filename.
    """
    slots = {}  # slot -> (filename, version or None for final)
    rolling_name = None
    for name in filenames:
        if name == PRICE_ROLLING_NAME:
            rolling_name = name
            continue
        match = PRICE_DATED_RE.match(name)
        if not match:
            continue
        slot, version_str = match.group(1), match.group(2)
        version = int(version_str) if version_str else None  # None = final
        prev = slots.get(slot)
        if prev is None:
            slots[slot] = (name, version)
        elif version is None:
            slots[slot] = (name, version)  # final beats any revision
        elif prev[1] is not None and version > prev[1]:
            slots[slot] = (name, version)  # higher revision wins
    return {slot: name for slot, (name, _ver) in slots.items()}, rolling_name


def price_slot_start(slot):
    """Convert a slot id like "2026092714" to an interval-start timestamp.

    The filename's HH is IESO's hour-ending in Toronto local time (verified
    against file contents: ..._2026092714.xml carries DeliveryDate
    2026-09-27, DeliveryHour 14), so this is the same conversion the XML
    parser applies to the delivery date/hour inside each file.
    """
    date_part = f"{slot[0:4]}-{slot[4:6]}-{slot[6:8]}"
    return hour_ending_to_start(date_part, int(slot[8:10]))


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
    """Run one feed: hourly price backfill, or incremental file backfill."""
    if cfg.get("backfill_hourly"):
        run_price_feed(feed, cfg, force=force)
    else:
        run_file_feed(feed, cfg, force=force)


def run_file_feed(feed, cfg, force=False):
    """Incremental backfill for one-file-per-year feeds (demand, fuel mix).

    The manifest records which raw files are already incorporated into the
    processed CSV. Each run downloads only the rolling current-year file
    (it always holds the latest data) plus any chosen file the manifest
    has not seen yet -- a new year, a new revision, or a final file
    replacing a versioned one. The new rows are merged into the existing
    CSV, so a missed week is fully backfilled and re-runs are idempotent.
    """
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

    manifest = load_manifest()
    recorded = manifest.get(feed, {})
    to_fetch = {}
    for year in sorted(chosen):
        name = chosen[year]
        is_rolling = rolling_name is not None and name == rolling_name
        if force or is_rolling or recorded.get(str(year)) != name:
            to_fetch[year] = name
        else:
            print(f"  {name} already incorporated (manifest); skipping download")

    parse = PARSERS[cfg["parse"]]
    frames = []
    incorporated = dict(recorded)  # year -> filename; updated as files parse OK
    for year in sorted(to_fetch):
        name = to_fetch[year]
        dest = RAW_DIR / name
        url = INDEX_URL.format(feed=feed) + name
        # The rolling current-year file always holds the latest data, so
        # refresh it every run; dated files download once (or when forced).
        is_rolling = rolling_name is not None and name == rolling_name
        if not download(url, dest, force=(force or is_rolling)):
            continue  # warned inside download(); the manifest keeps the old record
        try:
            df = parse(dest)
            print(f"  parsed {name}: {len(df)} rows")
            frames.append(df)
            incorporated[str(year)] = name
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: failed to parse {name} ({type(exc).__name__}: {exc}); skipping file")

    out_path = PROCESSED_DIR / cfg["output"]
    try:
        existing = pd.read_csv(out_path)
        print(f"  loaded {len(existing)} existing rows from {out_path.name}")
        frames = [existing] + frames
    except FileNotFoundError:
        print(f"  no existing {out_path.name}; building from scratch")
    except Exception as exc:  # noqa: BLE001 - warn, then build from downloads
        print(f"  WARNING: could not read existing {out_path.name} ({exc}); "
              f"building from downloaded files")

    if not frames:
        print(f"  WARNING: no usable data for {feed}; nothing written")
        return

    # A stable sort (mergesort) keeps the concatenated frame order for equal
    # timestamps, so drop_duplicates(keep="last") deterministically keeps the
    # newly parsed rows -- i.e. revised files overwrite older parses.
    combined = (pd.concat(frames, ignore_index=True)
                .sort_values("timestamp", kind="mergesort")
                .drop_duplicates("timestamp", keep="last"))

    if cfg.get("clean_fuelmix"):
        # NaN means "fuel not reported this hour" -> zero output.
        fuel_cols = [c for c in combined.columns if c != "timestamp"]
        combined[fuel_cols] = combined[fuel_cols].fillna(0)
        combined = combined.drop(columns=["control_actions"], errors="ignore")

    combined.to_csv(out_path, index=False)
    manifest[feed] = incorporated
    save_manifest(manifest)
    print(
        f"  WROTE {out_path.relative_to(BASE_DIR)} "
        f"({len(combined)} rows, {combined['timestamp'].iloc[0]} to {combined['timestamp'].iloc[-1]})"
    )
    print(f"  manifest now records {len(incorporated)} {feed} files")


def report_price_gaps(out_path):
    """Log the hourly slots still missing from the price history.

    Compares the stored timestamps against every expected hour between the
    first and last one (in UTC, so daylight-saving transitions are handled).
    A missing slot means that hour's file has failed to download or parse on
    every run so far. Recent gaps heal through the 7-day overlap; older ones
    are retried via the manifest's price_retry_slots list. Either way, the
    log states exactly which hours are missing instead of staying silent.
    """
    try:
        df = pd.read_csv(out_path)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        print("  gap check: no price history yet; nothing to check")
        return
    if df.empty:
        print("  gap check: no price history yet; nothing to check")
        return
    # Normalize to UTC so the check counts real elapsed hours, not wall-clock
    # labels (which repeat an hour when daylight saving ends).
    have = set(pd.to_datetime(df["timestamp"], utc=True))
    start, end = min(have), max(have)
    missing = []
    tick = start
    while tick <= end:
        if tick not in have:
            missing.append(tick)
        tick += timedelta(hours=1)
    span = f"{len(have)} hourly rows"
    if not missing:
        print(f"  gap check: no missing hourly slots ({span})")
        return
    print(f"  WARNING: price history is missing {len(missing)} hourly "
          f"slots ({span} total)")
    for tick in missing[:5]:
        # Show Toronto wall-clock time, matching the CSV's convention.
        print(f"    missing hour starting {tick.tz_convert(TORONTO).isoformat()}")
    if len(missing) > 5:
        print(f"    ... and {len(missing) - 5} more")


def run_price_feed(feed, cfg, force=False):
    """Hourly backfill for the realtime Ontario zonal price.

    The index retains about 3 months of dated per-hour files. Each run
    downloads every hour newer than (last stored timestamp minus a 7-day
    overlap). The overlap re-fetches recent hours so revised hourly averages
    replace preliminary ones, and it self-heals any hour whose download
    failed on the previous run. Hours whose files fail to download or parse
    are recorded in the manifest and retried on every later run until they
    succeed -- even when they fall outside the 7-day overlap. The rolling
    global-link file is always fetched too, in case the very latest hour is
    not in the dated listing yet. New rows are appended to price_hourly.csv,
    deduped by timestamp, so a missed week is fully backfilled and re-runs
    change nothing. Every run ends with an explicit gap report in the logs.
    """
    print(f"\n=== {feed} ===")
    out_path = PROCESSED_DIR / cfg["output"]
    try:
        filenames = fetch_index(feed)
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: could not list {feed} index ({type(exc).__name__}: {exc}); skipping feed")
        report_price_gaps(out_path)
        return

    slots, rolling_name = choose_price_files(filenames)
    print(f"  index holds {len(slots)} hourly slots")

    manifest = load_manifest()
    # Previously failed slots are retried until they succeed, no matter how
    # old they are. A slot that has aged out of the index (~3-month
    # retention) can never be fetched, so it is dropped with a warning
    # instead of being retried forever.
    retry_slots = set(manifest.get(PRICE_RETRY_KEY, []))
    expired = sorted(retry_slots - set(slots))
    if expired:
        print(f"  WARNING: {len(expired)} retried hours are no longer in the "
              f"index (beyond its ~3-month retention) and can never be "
              f"fetched; dropping them from the retry list")
        for slot in expired[:5]:
            print(f"    expired {price_slot_start(slot)}")
    retry_slots -= set(expired)
    if retry_slots:
        print(f"  retrying {len(retry_slots)} previously failed hours: "
              f"{', '.join(sorted(retry_slots)[:5])}"
              f"{'...' if len(retry_slots) > 5 else ''}")

    history = None
    last_ts = None
    try:
        history = pd.read_csv(out_path)
        if not history.empty:
            last_ts = pd.to_datetime(history["timestamp"]).max()
            print(f"  price history ends at {last_ts.isoformat()}")
    except FileNotFoundError:
        print(f"  no existing {out_path.name}; will seed recent history")
    except Exception as exc:  # noqa: BLE001 - warn, then seed
        print(f"  WARNING: could not read existing {out_path.name} ({exc}); "
              f"will seed recent history")
        history = None

    now = datetime.now(TORONTO)
    if force or last_ts is None:
        # First run (or forced refresh): seed/repair the most recent 7 days.
        cutoff = now - timedelta(days=7)
        reason = ("seeding the most recent 7 days" if last_ts is None
                  else "--force: refreshing the most recent 7 days")
        print(f"  {reason}")
    else:
        cutoff = last_ts - timedelta(days=7)

    wanted = sorted(
        {slot for slot in slots
         if pd.to_datetime(price_slot_start(slot)) >= cutoff}
        | retry_slots
    )
    if last_ts is not None and wanted:
        oldest = pd.to_datetime(price_slot_start(wanted[0]))
        if oldest > last_ts + pd.Timedelta(hours=1):
            print(f"  WARNING: the index only retains recent hours; the hours "
                  f"{last_ts.isoformat()}..{oldest.isoformat()} can no longer "
                  f"be backfilled and will stay missing")
    print(f"  fetching {len(wanted)} hourly files"
          + (", plus the rolling file" if rolling_name else ""))

    parse = PARSERS[cfg["parse"]]
    frames = [] if history is None else [history]
    # Slots still needing a successful parse. A slot leaves this set only
    # after its file downloads AND parses; anything else stays (or is
    # added) so the next run retries it.
    pending = set(retry_slots)
    for slot in wanted:
        name = slots[slot]
        dest = RAW_DIR / name
        url = INDEX_URL.format(feed=feed) + name
        if not download(url, dest):
            pending.add(slot)  # warned inside download(); retried next run
            continue
        try:
            frames.append(parse(dest))
            pending.discard(slot)
        except Exception as exc:  # noqa: BLE001
            # The hour's 5-minute intervals are not published yet, or the
            # file is unusable; a later run picks it up.
            print(f"  skipping {name}: {type(exc).__name__}: {exc}")
            pending.add(slot)

    # Persist the retry list before the rolling file and the write, so even
    # an interrupted run keeps an accurate record of what still needs work.
    manifest[PRICE_RETRY_KEY] = sorted(pending)
    save_manifest(manifest)
    if pending:
        print(f"  {len(pending)} hours recorded for retry on the next run")

    if rolling_name:
        dest = RAW_DIR / rolling_name
        url = INDEX_URL.format(feed=feed) + rolling_name
        if download(url, dest, force=True):
            try:
                frames.append(parse(dest))
            except Exception as exc:  # noqa: BLE001
                print(f"  skipping {rolling_name}: {type(exc).__name__}: {exc}")

    new_frames = frames[1:] if history is not None else frames
    if not new_frames:
        print(f"  no new price hours parsed; {out_path.name} unchanged")
        report_price_gaps(out_path)
        return

    # Stable sort so dedupe(keep="last") keeps the freshly parsed rows over
    # the stored history -- revised averages overwrite preliminary ones.
    combined = (pd.concat(frames, ignore_index=True)
                .sort_values("timestamp", kind="mergesort")
                .drop_duplicates("timestamp", keep="last"))
    combined.to_csv(out_path, index=False)
    print(
        f"  WROTE {out_path.relative_to(BASE_DIR)} "
        f"({len(combined)} rows, {combined['timestamp'].iloc[0]} to {combined['timestamp'].iloc[-1]})"
    )
    report_price_gaps(out_path)


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
