# Ontario Weather–Grid Dashboard

How Ontario's weather moves its electricity grid: demand, prices, and
renewable output, updated hourly. Built with free tools only —
GitHub Actions + GitHub Pages, no API keys, no servers, no databases.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# M1: fetch IESO reports + 2 years of Open-Meteo archive weather (7 locations)
python3 src/ieso_scraper.py
python3 src/weather_fetch.py

# M2: join the datasets and build the five Plotly charts into docs/charts/
python3 src/make_charts.py

# M3: render the static dashboard page at docs/index.html
python3 src/render_dashboard.py
```

Reruns are idempotent: raw files in `data/raw/` are skipped when present,
and `data/processed/` CSVs are rebuilt deterministically. Use `--force`
with either script to re-download. Missing or unpublished reports log a
warning and never crash the pipeline.

## Project layout

```
src/
  ieso_scraper.py    # IESO demand + fuel-mix + zonal price download/parsing (M1-M3)
  weather_fetch.py   # Open-Meteo archive fetcher with disk cache (M1/M2)
  make_charts.py     # joins + five Plotly charts (M2)
  render_dashboard.py # builds docs/index.html from processed CSVs (M3)
data/
  raw/               # original downloads, NOT committed (re-downloadable)
  processed/         # tidy hourly CSVs, committed (IESO real-time reports expire)
docs/
  index.html         # the dashboard page itself (M3); published by GitHub Pages
  charts/            # standalone Plotly HTML charts (M2), embedded by index.html
.github/workflows/  # hourly update pipeline (M4): runs the full
                    # pipeline and commits results, no servers or keys
```

## Timestamps

IESO reports use "Hour Ending" 1–24 in America/Toronto local time. We
convert to the interval *start* and store ISO 8601 with the UTC offset
(e.g. `2026-01-01T00:00:00-05:00`). On daylight-saving transition days the
wall clock skips or repeats an hour; we attach the Toronto zone to the
wall-clock time and let `zoneinfo` resolve it deterministically (fold=0).
Weather timestamps from Open-Meteo use the same convention.

## Charts (M2)

`python3 src/make_charts.py` joins the processed CSVs on UTC-normalized
timestamps and writes five standalone Plotly HTML files to `docs/charts/`
(the M3 dashboard will embed them):

- `demand_vs_temperature.html` — hourly Ontario demand vs Toronto
  temperature, points coloured by season, with a dashed reference line at
  18 °C (the usual heating/cooling balance point).
- `demand_timeseries.html` — hourly Ontario demand, 2003–present.
- `fuelmix_stacked_area.html` — generation by fuel, stacked, as daily
  averages (hourly would be unreadable noise at this scale).
- `kincardine_wind.html` — Kincardine wind speed (100 m) vs province-wide
  wind output.
- `solar_irradiance.html` — Toronto solar irradiance vs province-wide
  solar output.

Notes:

- Fuel data cleaning: a missing fuel value means the fuel wasn't reported
  that hour, so NaNs are filled with 0; `control_actions` is grid-operator
  dispatch, not generation, and is dropped.
- Plotly's JavaScript library loads from a CDN instead of being embedded,
  so each HTML file holds only its data and stays small.
- Weather covers the most recent 2 years (ERA5 archive lag); the full IESO
  history back to 2003/2015 is kept for the M5 regression.

## Dashboard (M3)

`python3 src/render_dashboard.py` reads the processed CSVs and writes
`docs/index.html` — a static page GitHub Pages serves as the dashboard.
It shows five headline cards plus the five M2 charts embedded as iframes:

- **Ontario demand** — latest `ontario_demand` from `demand_hourly.csv`.
- **Toronto temperature** — latest Toronto `temperature_2m` from
  `weather_hourly.csv`.
- **Renewable share** — `(biofuel + hydro + solar + wind) / total reported
  generation` for the latest fuel-mix hour.
- **Non-emitting share** — same denominator, with nuclear added to the
  numerator.
- **Ontario zonal price** — latest `ontario_zonal_price` from
  `price_hourly.csv`.

A card shows `unavailable` (with its timestamp hidden) when its feed has
no data; a real reading of zero (e.g. $0.00/MWh) is displayed, not hidden.

Each card carries its own "as of" timestamp, because the feeds update on
different schedules (IESO grid data lags ~1 day, Open-Meteo weather ~5
days, the realtime price is minutes old).

### Zonal price feed

The scraper also pulls IESO's **RealtimeOntarioZonalPrice** report: the
realtime 5-minute Ontario zonal energy price (OZP). Since the May 2025
market renewal this is the successor to the old HOEP — the province-wide
headline price, published every 5 minutes. Each XML file covers one hour
(twelve 5-minute intervals plus an `AveragePrice` element); we store the
hourly average, which is also the value IESO uses for settlement, into
`data/processed/price_hourly.csv`. The directory index holds ~12,000
per-interval files, so the scraper uses IESO's "global link" file
(`PUB_RealtimeOntarioZonalPrice.xml`, no date in the name), which always
resolves to the latest published hour. Each run appends the new hour to
the file (deduped by timestamp), so the repo accumulates a price history
just like the other feeds.

## Hourly updates (M4)

`.github/workflows/update.yml` runs the whole pipeline once an hour on
GitHub Actions — free for public repos, no servers, no API keys:

1. Checks out the repo, sets up Python 3.12, installs `requirements.txt`.
2. Restores the previous run's `data/raw/` downloads from a cache (keyed
   by week), so each run only re-downloads the small rolling files that
   changed instead of the full multi-year history.
3. Runs `ieso_scraper.py` → `weather_fetch.py` → `make_charts.py` →
   `render_dashboard.py` (unbuffered, so logs stream in real time).
4. Commits `data/processed/` and `docs/` and pushes with the repository's
   built-in `GITHUB_TOKEN` — no personal token needed. If nothing
   changed, it skips the commit. GitHub Pages then serves the updated
   `docs/index.html` automatically.

The workflow runs at minute 35 of every hour (cron times are UTC), giving
IESO's hourly reports time to publish. You can also trigger a run by hand
from the Actions tab ("Run workflow"). `data/raw/` stays git-ignored, so
only processed data and dashboard output are committed.

## Data sources and attribution

- Grid data: **IESO public reports** — https://reports-public.ieso.ca/public/
  (Hourly Zonal Demand, Generator Output by Fuel Type Hourly, Realtime
  Ontario Zonal Price). © Independent Electricity System Operator. Used
  under the IESO's terms for public reports.
- Weather data: **Open-Meteo** — https://open-meteo.com/ (ERA5 archive and
  forecast APIs), licensed CC-BY 4.0.
