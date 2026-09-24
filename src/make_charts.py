"""
M2 chart builder: join IESO grid data with Open-Meteo weather and write
five standalone Plotly HTML charts to docs/charts/ for the M3 dashboard.

Charts:
  (a) demand_vs_temperature.html - hourly Ontario demand vs Toronto
      temperature, points coloured by season, vertical reference line at 18 C
  (b) demand_timeseries.html     - hourly Ontario demand, 2003 to present
  (c) fuelmix_stacked_area.html  - daily-average generation by fuel, stacked
  (d) kincardine_wind.html       - dual-axis time series: Ontario-wide wind
      output (left axis) vs Kincardine 100 m wind speed (right axis),
      7-day rolling means over faint hourly points, range slider with a
      default view of the most recent 90 days
  (e) solar_irradiance.html      - dual-axis time series: Ontario-wide solar
      output (left axis) vs Toronto shortwave irradiance (right axis),
      same layout as the wind chart

Joins are done on timestamps converted to UTC, so daylight-saving
transitions line up exactly between the IESO and weather files. The
time-series charts then display in America/Toronto local time.

Run:  python3 src/make_charts.py
Needs: data/processed/{demand_hourly,fuelmix_hourly,weather_hourly}.csv
"""

from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

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


def merge_station(fuelmix, weather, location, fuel_col, weather_col):
    """Join one fuel column with one weather-station column on UTC
    timestamps -- exactly like the old scatter charts -- and add a
    Toronto-local datetime column for the time-series x-axis."""
    station = weather[weather["location"] == location][["ts", weather_col]]
    merged = fuelmix[["ts", fuel_col]].merge(station, on="ts", how="inner")
    merged = merged.sort_values("ts").reset_index(drop=True)
    merged["ts_local"] = merged["ts"].dt.tz_convert("America/Toronto")
    return merged


def chart_output_vs_weather(merged, fuel_col, weather_col, title,
                            output_name, weather_name,
                            output_unit, weather_unit):
    """Dual-axis time series: province-wide grid output on the left axis,
    single-station weather on the right axis.

    The main lines are 7-day rolling means (168 hourly points); raw
    hourly points sit underneath at ~10% opacity for context. A range
    slider lets the reader zoom, and the default view shows the most
    recent 90 days."""
    print(f"  {title}: {len(merged)} overlapping hours")

    # 7-day rolling mean of hourly data = 168 points. min_periods=1 so
    # the line starts at the first hour instead of leaving a gap.
    for col in (fuel_col, weather_col):
        merged[f"{col}_7d"] = merged[col].rolling(168, min_periods=1).mean()

    end = merged["ts_local"].max()
    start = end - pd.Timedelta(days=90)

    out_color = "#1f77b4"  # blue for grid output
    wx_color = "#ff7f0e"   # orange for weather

    fig = go.Figure()

    # Faint raw hourly points: WebGL (Scattergl) keeps them fast.
    fig.add_trace(go.Scattergl(
        x=merged["ts_local"], y=merged[fuel_col], yaxis="y",
        mode="markers", marker=dict(size=3, color=out_color),
        opacity=0.1,
        name=f"{output_name} ({output_unit}, hourly)"))
    fig.add_trace(go.Scattergl(
        x=merged["ts_local"], y=merged[weather_col], yaxis="y2",
        mode="markers", marker=dict(size=3, color=wx_color),
        opacity=0.1,
        name=f"{weather_name} ({weather_unit}, hourly, single station)"))

    # Main 7-day rolling-mean lines.
    fig.add_trace(go.Scatter(
        x=merged["ts_local"], y=merged[f"{fuel_col}_7d"], yaxis="y",
        mode="lines", line=dict(color=out_color, width=2),
        name=f"{output_name} ({output_unit}, 7-day mean)"))
    fig.add_trace(go.Scatter(
        x=merged["ts_local"], y=merged[f"{weather_col}_7d"], yaxis="y2",
        mode="lines", line=dict(color=wx_color, width=2),
        name=f"{weather_name} ({weather_unit}, 7-day mean, single station)"))

    fig.update_layout(
        title=title,
        xaxis=dict(title="Date (Toronto time)",
                   rangeslider=dict(visible=True),
                   range=[start, end]),
        yaxis=dict(title=f"{output_name} ({output_unit})"),
        yaxis2=dict(title=f"{weather_name} ({weather_unit})",
                    overlaying="y", side="right"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="center", x=0.5),
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
    wind = merge_station(fuelmix, weather, "Kincardine", "wind", "wind_speed_100m")
    save(chart_output_vs_weather(
        wind, "wind", "wind_speed_100m",
        "Ontario wind output vs Kincardine wind speed.",
        "Ontario-wide wind output", "Kincardine station wind speed",
        "MW", "m/s"),
        "kincardine_wind.html", "Kincardine wind")
    solar = merge_station(fuelmix, weather, "Toronto", "solar", "shortwave_radiation")
    save(chart_output_vs_weather(
        solar, "solar", "shortwave_radiation",
        "Ontario solar output vs Toronto solar irradiance.",
        "Ontario-wide solar output", "Toronto station irradiance",
        "MW", "W/m²"),
        "solar_irradiance.html", "solar irradiance")

    print("\nDone.")


if __name__ == "__main__":
    main()
