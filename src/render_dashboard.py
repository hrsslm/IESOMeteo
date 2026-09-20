"""
Dashboard renderer (M3/M4).

Reads the processed CSVs and builds a static dashboard page at
docs/index.html: five headline stat cards (current Ontario demand,
current Toronto temperature, renewable share of generation,
non-emitting share of generation, and the latest Ontario zonal price)
with the five M2 charts embedded below.

Run after the data pipeline (scraper, weather fetch, chart builder):

    python3 src/ieso_scraper.py
    python3 src/weather_fetch.py
    python3 src/make_charts.py
    python3 src/render_dashboard.py

docs/ is published as-is by GitHub Pages, so docs/index.html becomes the
live dashboard. No JavaScript build step: the charts are standalone
Plotly HTML files embedded with <iframe>.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
PROCESSED_DIR = BASE_DIR / "data" / "processed"
DOCS_DIR = BASE_DIR / "docs"

TORONTO = ZoneInfo("America/Toronto")

# The five M2 charts, in display order: (file, title, description).
CHARTS = [
    (
        "demand_vs_temperature.html",
        "Demand vs Temperature",
        "Hourly Ontario demand plotted against Toronto temperature, coloured "
        "by season. The dashed line at 18 \u00b0C marks the rough point where "
        "cooling demand starts to climb.",
    ),
    (
        "demand_timeseries.html",
        "Ontario Demand History",
        "Hourly Ontario-wide electricity demand from 2003 to the present, "
        "showing daily, weekly and seasonal cycles plus long-term growth.",
    ),
    (
        "fuelmix_stacked_area.html",
        "Generation by Fuel",
        "Daily-average electricity generation by fuel type from 2015 to the "
        "present. Nuclear and hydro dominate; wind and solar grow over time.",
    ),
    (
        "kincardine_wind.html",
        "Wind: Kincardine vs Provincial Output",
        "Wind speed at 100 m in Kincardine (near the Bruce nuclear/wind hub) "
        "against total province-wide wind generation, hour by hour.",
    ),
    (
        "solar_irradiance.html",
        "Solar: Toronto Irradiance vs Output",
        "Shortwave solar irradiance in Toronto against total province-wide "
        "solar generation, hour by hour.",
    ),
]


def load_csv(name):
    """Read a processed CSV, or return None if it is missing/unreadable."""
    path = PROCESSED_DIR / name
    if not path.exists():
        print(f"  WARNING: {name} not found; card will show 'unavailable'")
        return None
    try:
        return pd.read_csv(path)
    except Exception as exc:  # noqa: BLE001 - warn and continue, never crash
        print(f"  WARNING: could not read {name} ({exc}); card will show 'unavailable'")
        return None


def pretty_time(iso_stamp):
    """Turn an ISO 8601 timestamp into 'YYYY-MM-DD HH:MM TZ' for display."""
    try:
        moment = datetime.fromisoformat(iso_stamp)
    except (TypeError, ValueError):
        return str(iso_stamp)
    # Attach the real Toronto zone (not the fixed offset from the string)
    # so %Z prints EDT/EST instead of "UTC-04:00".
    return moment.astimezone(TORONTO).strftime("%Y-%m-%d %H:%M %Z")


def latest_value(df, column):
    """Return (value, timestamp) from the last row of a dataframe."""
    if df is None or df.empty or column not in df.columns:
        return None, None
    row = df.iloc[-1]
    return row[column], row["timestamp"]


def fmt_or_unavailable(value, template):
    """Format a number with `template`, or 'unavailable' when missing.

    We check explicitly for missing values (None or NaN) instead of
    relying on truthiness, because a real reading of 0 is valid data:
    $0.00/MWh prices and 0 °C temperatures do happen.
    """
    if value is None or pd.isna(value):
        return "unavailable"
    return template.format(value)


def main():
    print("Loading processed data...")
    demand = load_csv("demand_hourly.csv")
    weather = load_csv("weather_hourly.csv")
    fuelmix = load_csv("fuelmix_hourly.csv")
    price = load_csv("price_hourly.csv")

    # --- headline numbers (each card keeps its own timestamp, because the
    # feeds update on different schedules) ---
    demand_mw, demand_ts = latest_value(demand, "ontario_demand")

    toronto = None
    if weather is not None and not weather.empty:
        toronto = weather[weather["location"] == "Toronto"]
    temp_c, temp_ts = latest_value(toronto, "temperature_2m")

    renew_pct, nonemit_pct, fuelmix_ts = None, None, None
    if fuelmix is not None and not fuelmix.empty:
        last = fuelmix.iloc[-1]
        fuel_cols = [c for c in fuelmix.columns if c != "timestamp"]
        # Renewables = biofuel + hydro + solar + wind; non-emitting adds
        # nuclear (which emits no CO2 while generating). Both are shares
        # of all reported generation that hour. Documented in README.
        renewable = sum(last[c] for c in ("biofuel", "hydro", "solar", "wind")
                        if c in fuel_cols)
        non_emitting = renewable + (last["nuclear"] if "nuclear" in fuel_cols else 0)
        total = sum(last[c] for c in fuel_cols)
        if total > 0:
            renew_pct = 100 * renewable / total
            nonemit_pct = 100 * non_emitting / total
        fuelmix_ts = last["timestamp"]

    price_val, price_ts = latest_value(price, "ontario_zonal_price")

    def card(label, value_text, stamp):
        asof = f'<div class="asof">as of {pretty_time(stamp)}</div>' if stamp else ""
        return (
            f'<div class="card"><div class="value">{value_text}</div>'
            f'<div class="label">{label}</div>{asof}</div>'
        )

    cards = [
        card("Ontario demand", fmt_or_unavailable(demand_mw, "{:,.0f} MW"), demand_ts),
        card("Toronto temperature", fmt_or_unavailable(temp_c, "{:.1f} °C"), temp_ts),
        card("Renewable share", fmt_or_unavailable(renew_pct, "{:.1f}%"), fuelmix_ts),
        card("Non-emitting share", fmt_or_unavailable(nonemit_pct, "{:.1f}%"), fuelmix_ts),
        card("Ontario zonal price", fmt_or_unavailable(price_val, "${:.2f}/MWh"), price_ts),
    ]

    chart_blocks = []
    for filename, title, description in CHARTS:
        chart_blocks.append(
            f'<section class="chart"><h2>{title}</h2><p>{description}</p>'
            f'<iframe src="charts/{filename}" loading="lazy" '
            f'title="{title}"></iframe></section>'
        )

    built_at = datetime.now(TORONTO).strftime("%Y-%m-%d %H:%M %Z")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ontario Weather\u2013Grid Dashboard</title>
<style>
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    margin: 0; background: #f4f6f8; color: #1a1a1a;
  }}
  header {{
    background: #0b3d5c; color: #fff; padding: 2rem 1.5rem; text-align: center;
  }}
  header h1 {{ margin: 0 0 0.5rem; font-size: 1.8rem; }}
  header p {{ margin: 0.25rem 0; opacity: 0.85; }}
  main {{ max-width: 1100px; margin: 0 auto; padding: 1.5rem; }}
  .cards {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 1rem; margin-bottom: 2rem;
  }}
  .card {{
    background: #fff; border-radius: 10px; padding: 1.25rem;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08); text-align: center;
  }}
  .card .value {{ font-size: 1.9rem; font-weight: 700; color: #0b3d5c; }}
  .card .label {{ margin-top: 0.35rem; font-size: 0.95rem; color: #444; }}
  .card .asof {{ margin-top: 0.35rem; font-size: 0.8rem; color: #888; }}
  .chart {{
    background: #fff; border-radius: 10px; padding: 1.25rem;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08); margin-bottom: 1.5rem;
  }}
  .chart h2 {{ margin: 0 0 0.25rem; font-size: 1.25rem; color: #0b3d5c; }}
  .chart p {{ margin: 0 0 1rem; color: #555; font-size: 0.95rem; }}
  .chart iframe {{ width: 100%; height: 520px; border: 1px solid #e0e0e0; border-radius: 6px; }}
  footer {{
    max-width: 1100px; margin: 0 auto; padding: 0 1.5rem 2rem;
    color: #666; font-size: 0.85rem;
  }}
  footer ul {{ padding-left: 1.2rem; }}
</style>
</head>
<body>
<header>
  <h1>Ontario Weather\u2013Grid Dashboard</h1>
  <p>IESO electricity-grid data meets Open-Meteo weather data.</p>
  <p>Dashboard built {built_at} (America/Toronto).</p>
</header>
<main>
  <div class="cards">
    {''.join(cards)}
  </div>
  {''.join(chart_blocks)}
</main>
<footer>
  <h3>Data &amp; methods</h3>
  <ul>
    <li>Grid data: IESO public reports (zonal demand, generation by fuel,
        realtime Ontario zonal price). Weather: Open-Meteo archive API.</li>
    <li>Each headline card shows its own timestamp because the feeds update
        on different schedules (grid data lags ~1 day, Open-Meteo weather ~5
        days, the realtime price is minutes old).</li>
    <li>Renewable share = (biofuel + hydro + solar + wind) / total reported
        generation for the latest hour. Non-emitting share adds nuclear to
        the numerator.</li>
    <li>Ontario zonal price = hourly average of the realtime 5-minute
        Ontario zonal energy price (the hourly average is also the value
        IESO uses for settlement). Each hourly run appends the latest hour
        to data/processed/price_hourly.csv, so the repo keeps a growing
        price history.</li>
    <li>All timestamps are America/Toronto local time.</li>
  </ul>
</footer>
</body>
</html>
"""

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DOCS_DIR / "index.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"WROTE {out_path.relative_to(BASE_DIR)} ({out_path.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
