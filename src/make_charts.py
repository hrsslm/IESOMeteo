"""
M2 chart builder: join IESO grid data with Open-Meteo weather and write
five standalone Plotly HTML charts to docs/charts/ for the M3 dashboard.

Charts:
  (a) demand_vs_temperature.html - hourly Ontario demand vs Toronto
      temperature, points coloured by season, vertical reference line at 18 C
  (b) demand_timeseries.html     - hourly Ontario demand, 2003 to present
  (c) fuelmix_stacked_area.html  - daily-average generation by fuel, stacked
  (d) kincardine_wind.html       - Kincardine wind speed vs wind output
  (e) solar_irradiance.html      - Toronto solar irradiance vs solar output

Joins are done on timestamps converted to UTC, so daylight-saving
transitions line up exactly between the IESO and weather files.

Run:  python3 src/make_charts.py
Needs: data/processed/{demand_hourly,fuelmix_hourly,weather_hourly}.csv
"""

from pathlib import Path

import pandas as pd
import plotly.express as px

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
PROCESSED_DIR = BASE_DIR / "data" / "processed"
CHARTS_DIR = BASE_DIR / "docs" / "charts"

# Plotly's JS library is loaded from a CDN instead of being embedded in
# every file. That keeps each HTML small (data only) and is fine because
# the M3 dashboard will be served online anyway.
PLOTLY_JS = "cdn"

# Nice legend labels for the fuel columns.
FUEL_LABELS = {
    "biofuel": "Biofuel",
    "gas": "Gas",
    "hydro": "Hydro",
    "nuclear": "Nuclear",
    "other": "Other",
    "solar": "Solar",
    "wind": "Wind",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load(name):
    """Read a processed CSV and add a UTC timestamp column for joining."""
    df = pd.read_csv(PROCESSED_DIR / name)
    # The CSV timestamps are Toronto local ISO 8601 with UTC offsets
    # (e.g. "2026-01-01T00:00:00-05:00"). Converting to UTC gives one
    # unambiguous join key for both data sources.
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def season_of(month):
    """Map a calendar month number to a season name."""
    if month in (12, 1, 2):
        return "Winter"
    if month in (3, 4, 5):
        return "Spring"
    if month in (6, 7, 8):
        return "Summer"
    return "Fall"


def save(fig, filename, title):
    """Write one chart to docs/charts/ as a standalone HTML file."""
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    path = CHARTS_DIR / filename
    fig.write_html(path, include_plotlyjs=PLOTLY_JS, full_html=True)
    print(f"  wrote {path.relative_to(BASE_DIR)} ({path.stat().st_size / 1e6:.1f} MB) - {title}")


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def chart_demand_vs_temperature(demand, weather):
    """(a) Hourly demand vs Toronto temperature, coloured by season."""
    toronto = weather[weather["location"] == "Toronto"][["ts", "temperature_2m"]]
    merged = demand[["ts", "timestamp", "ontario_demand"]].merge(toronto, on="ts", how="inner")
    # The timestamp string is Toronto local time, so its month gives the
    # season directly with no timezone fiddling.
    merged["season"] = merged["timestamp"].str[5:7].astype(int).map(season_of)
    print(f"  (a) demand vs temperature: {len(merged)} overlapping hours")

    fig = px.scatter(
        merged,
        x="temperature_2m",
        y="ontario_demand",
        color="season",
        opacity=0.45,
        title="Ontario electricity demand vs Toronto temperature",
        labels={
            "temperature_2m": "Temperature (°C)",
            "ontario_demand": "Ontario demand (MW)",
            "season": "Season",
        },
    )
    # 18 C is the usual heating/cooling balance point: demand rises when
    # it is colder (heating) and when it is hotter (air conditioning).
    fig.add_vline(x=18, line_dash="dash", line_color="black",
                  annotation_text="18 °C reference")
    return fig


def chart_demand_timeseries(demand):
    """(b) Hourly Ontario demand over the full history."""
    print(f"  (b) demand time series: {len(demand)} hourly points")
    fig = px.line(
        demand,
        x="ts",
        y="ontario_demand",
        title="Hourly Ontario electricity demand (2003-present)",
        labels={"ts": "Date", "ontario_demand": "Ontario demand (MW)"},
        # 200k+ points: WebGL keeps the chart fast in the browser.
        render_mode="webgl",
    )
    return fig


def chart_fuelmix_stacked_area(fuelmix):
    """(c) Generation by fuel, stacked, as daily averages for readability."""
    fuel_cols = [c for c in FUEL_LABELS if c in fuelmix.columns]
    daily = fuelmix.set_index("ts")[fuel_cols].resample("D").mean().reset_index()
    daily = daily.rename(columns=FUEL_LABELS)
    print(f"  (c) fuel-mix stacked area: {len(daily)} daily points")
    fig = px.area(
        daily,
        x="ts",
        y=[FUEL_LABELS[c] for c in fuel_cols],
        title="Ontario generation by fuel (daily average, stacked)",
        labels={"ts": "Date", "value": "Output (MW)", "variable": "Fuel"},
    )
    return fig


def chart_kincardine_wind(fuelmix, weather):
    """(d) Kincardine wind speed vs actual province-wide wind output."""
    kincardine = weather[weather["location"] == "Kincardine"][["ts", "wind_speed_100m"]]
    merged = fuelmix[["ts", "wind"]].merge(kincardine, on="ts", how="inner")
    print(f"  (d) Kincardine wind: {len(merged)} overlapping hours")
    fig = px.scatter(
        merged,
        x="wind_speed_100m",
        y="wind",
        opacity=0.45,
        title="Kincardine wind speed vs Ontario wind output",
        labels={
            "wind_speed_100m": "Wind speed at 100 m (m/s)",
            "wind": "Wind output (MW)",
        },
    )
    return fig


def chart_solar_irradiance(fuelmix, weather):
    """(e) Toronto solar irradiance vs actual province-wide solar output."""
    toronto = weather[weather["location"] == "Toronto"][["ts", "shortwave_radiation"]]
    merged = fuelmix[["ts", "solar"]].merge(toronto, on="ts", how="inner")
    print(f"  (e) solar irradiance: {len(merged)} overlapping hours")
    fig = px.scatter(
        merged,
        x="shortwave_radiation",
        y="solar",
        opacity=0.45,
        title="Toronto solar irradiance vs Ontario solar output",
        labels={
            "shortwave_radiation": "Solar irradiance (W/m²)",
            "solar": "Solar output (MW)",
        },
    )
    return fig


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def main():
    print("Loading processed data...")
    demand = load("demand_hourly.csv")
    fuelmix = load("fuelmix_hourly.csv")
    weather = load("weather_hourly.csv")
    print(f"  demand: {len(demand)} rows, fuelmix: {len(fuelmix)} rows, "
          f"weather: {len(weather)} rows")

    print("\nBuilding charts...")
    save(chart_demand_vs_temperature(demand, weather),
         "demand_vs_temperature.html", "demand vs temperature")
    save(chart_demand_timeseries(demand),
         "demand_timeseries.html", "demand time series")
    save(chart_fuelmix_stacked_area(fuelmix),
         "fuelmix_stacked_area.html", "fuel-mix stacked area")
    save(chart_kincardine_wind(fuelmix, weather),
         "kincardine_wind.html", "Kincardine wind")
    save(chart_solar_irradiance(fuelmix, weather),
         "solar_irradiance.html", "solar irradiance")

    print("\nDone.")


if __name__ == "__main__":
    main()
