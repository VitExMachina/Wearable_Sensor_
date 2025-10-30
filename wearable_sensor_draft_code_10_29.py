# app.py (single-file, big-file friendly)

import streamlit as st
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List, Optional, Tuple
from pathlib import Path
from xml.etree.ElementTree import iterparse  # for streaming XML

# =========================================================
# 1) CORE (was wearable/core.py)
# =========================================================
REQUIRED_COLS = ["timestamp"]
ALLOWED_SENSORS = {
    "heart_rate",
    "steps",
    "temperature",
    "wrist_temperature",
    "oxygen_saturation",
}

@dataclass(frozen=True)
class Metrics:
    n_rows: int
    time_start: pd.Timestamp
    time_end: pd.Timestamp
    duration_hours: float
    sensors_present: List[str]
    daily_means: pd.DataFrame
    daily_max: pd.DataFrame
    resting_hr: Optional[float]
    step_total: Optional[int]
    temp_mean: Optional[float]
    spo2_mean: Optional[float]

def _coerce_ts(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce")

def validate_and_clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "timestamp" not in df.columns:
        raise ValueError("Missing required column: 'timestamp'")
    df["timestamp"] = _coerce_ts(df["timestamp"])
    df = df.dropna(subset=["timestamp"])

    sensor_cols = [c for c in df.columns if c in ALLOWED_SENSORS]
    if not sensor_cols:
        raise ValueError(
            f"No recognized sensor columns. Expected any of: {', '.join(sorted(ALLOWED_SENSORS))}"
        )

    for c in sensor_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=sensor_cols, how="all")
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    return df[["timestamp"] + sensor_cols]

def resample_daily(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    dfi = df.set_index("timestamp")
    return (
        dfi.resample("1D").mean(numeric_only=True),
        dfi.resample("1D").max(numeric_only=True),
    )

def compute_metrics(df: pd.DataFrame) -> Metrics:
    sensor_cols = [c for c in df.columns if c in ALLOWED_SENSORS]
    daily_means, daily_max = resample_daily(df)

    resting_hr = (
        float(np.nanpercentile(df["heart_rate"], 5))
        if "heart_rate" in sensor_cols
        else None
    )
    step_total = int(np.nansum(df["steps"])) if "steps" in sensor_cols else None
    temp_mean = (
        float(np.nanmean(df["temperature"])) if "temperature" in sensor_cols else None
    )
    spo2_mean = (
        float(np.nanmean(df["oxygen_saturation"]))
        if "oxygen_saturation" in sensor_cols
        else None
    )

    t0, t1 = df["timestamp"].min(), df["timestamp"].max()
    duration_hours = (t1 - t0).total_seconds() / 3600.0 if len(df) else 0.0

    return Metrics(
        n_rows=len(df),
        time_start=t0,
        time_end=t1,
        duration_hours=duration_hours,
        sensors_present=sensor_cols,
        daily_means=daily_means,
        daily_max=daily_max,
        resting_hr=resting_hr,
        step_total=step_total,
        temp_mean=temp_mean,
        spo2_mean=spo2_mean,
    )

# =========================================================
# 2) INGEST (big-file friendly)
# =========================================================
MAX_CHUNK = 100_000  # rows per chunk for big CSVs

_ALIAS_RAW = {
    "type": "@type",
    "@type": "@type",
    "creationDate": "@creationDate",
    "@creationDate": "@creationDate",
    "unit": "@unit",
    "@unit": "@unit",
    "value": "@value",
    "@value": "@value",
}
_ALIAS_TIDY = {
    "Timestamp": "timestamp",
    "Time": "timestamp",
    "timestamp": "timestamp",
    "HeartRate": "heart_rate",
    "HR": "heart_rate",
    "heartRate": "heart_rate",
    "heart_rate": "heart_rate",
    "Steps": "steps",
    "StepCount": "steps",
    "steps": "steps",
    "Temperature": "temperature",
    "BodyTemp": "temperature",
    "temperature": "temperature",
    "WristTemperature": "wrist_temperature",
    "wristTemperature": "wrist_temperature",
    "wrist_temperature": "wrist_temperature",
    "OxygenSaturation": "oxygen_saturation",
    "oxygenSaturation": "oxygen_saturation",
    "oxygen_saturation": "oxygen_saturation",
}

TYPE_MAP = {
    "HKQuantityTypeIdentifierHeartRate": ("heart_rate", "median"),
    "HKQuantityTypeIdentifierStepCount": ("steps", "sum"),
    "HKQuantityTypeIdentifierBodyTemperature": ("temperature", "mean"),
    "HKQuantityTypeIdentifierAppleSleepingWristTemperature": ("wrist_temperature", "mean"),
    "HKQuantityTypeIdentifierOxygenSaturation": ("oxygen_saturation", "mean"),
}

def _normalize_raw_cols(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={c: _ALIAS_RAW.get(c, c) for c in df.columns})

def _normalize_tidy_cols(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={c: _ALIAS_TIDY.get(c, c) for c in df.columns})

def _read_xml_streaming(file_obj) -> pd.DataFrame:
    """Streaming Apple Health XML reader for big XML exports."""
    rows = []
    for event, elem in iterparse(file_obj):
        if elem.tag != "Record":
            elem.clear()
            continue
        t = elem.attrib.get("type")
        if not t:
            elem.clear()
            continue
        rows.append(
            {
                "@type": t,
                "@creationDate": elem.attrib.get("creationDate")
                or elem.attrib.get("endDate")
                or elem.attrib.get("startDate"),
                "@unit": elem.attrib.get("unit"),
                "@value": elem.attrib.get("value"),
            }
        )
        elem.clear()
    return pd.DataFrame(rows)

def _read_tabular_big(src, ext: str) -> pd.DataFrame:
    """Read big CSVs in chunks; Excel as usual."""
    if ext == ".csv":
        chunks = pd.read_csv(src, chunksize=MAX_CHUNK)
        parts = []
        for ch in chunks:
            parts.append(ch)
        return pd.concat(parts, ignore_index=True)
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(src)
    # fallback
    return pd.read_csv(src)

def ingest(source) -> pd.DataFrame:
    # file-like (Streamlit UploadedFile)
    if hasattr(source, "read"):
        name = getattr(source, "name", "") or ""
        ext = Path(name).suffix.lower()
        # reset pointer
        source.seek(0)
        if ext == ".xml":
            df = _read_xml_streaming(source)
        else:
            df = _read_tabular_big(source, ext)
        return _normalize_raw_cols(df)

    # path-like
    p = Path(str(source))
    ext = p.suffix.lower()
    if ext == ".xml":
        with open(p, "rb") as f:
            df = _read_xml_streaming(f)
    else:
        df = _read_tabular_big(p, ext)
    return _normalize_raw_cols(df)

def records_to_minute_tidy(df_raw: pd.DataFrame) -> pd.DataFrame:
    # if already tidy
    tidy = _normalize_tidy_cols(df_raw.copy())
    if "timestamp" in tidy.columns:
        return validate_and_clean(tidy)

    need = {"@type", "@creationDate", "@value"}
    if not need.issubset(df_raw.columns):
        raise ValueError("Unsupported format. Provide Apple Health export or tidy CSV.")

    df = df_raw.rename(
        columns={
            "@type": "Biometric_Label",
            "@creationDate": "Date",
            "@value": "Value",
        }
    )
    df["timestamp"] = pd.to_datetime(df["Date"], errors="coerce")
    df["Value"] = pd.to_numeric(
        df["Value"].astype(str).str.replace(",", "."),
        errors="coerce",
    )
    df = df.dropna(subset=["timestamp", "Value", "Biometric_Label"])
    df["minute"] = df["timestamp"].dt.floor("T")

    out = None
    for t, (col, agg) in TYPE_MAP.items():
        d = df[df["Biometric_Label"] == t]
        if d.empty:
            continue
        g = d.groupby("minute")["Value"].agg(agg).rename(col).to_frame()
        out = g if out is None else out.join(g, how="outer")

    if out is None:
        raise ValueError("No supported biometric types found in file.")

    out = (
        out.reset_index()
        .rename(columns={"minute": "timestamp"})
        .sort_values("timestamp")
    )

    # drop rows where all supported sensors are NaN
    sensor_cols = list(set(v[0] for v in TYPE_MAP.values()))
    out = out.dropna(how="all", subset=sensor_cols)

    return validate_and_clean(out)

# =========================================================
# 3) STREAMLIT UI
# =========================================================
st.set_page_config(page_title="Wearable Sensor Data Analyzer", layout="wide")
st.title("⌚ Wearable Sensor Data Analyzer")

with st.sidebar:
    uploaded = st.file_uploader(
        "Upload Apple Health export (.xml / .csv / .xlsx) or tidy CSV",
        type=["xml", "csv", "xlsx"],
    )
    if uploaded is not None:
        size_mb = len(uploaded.getvalue()) / (1024 * 1024)
        st.caption(f"File size: {size_mb:.2f} MB")
    st.caption(
        "Tidy CSV needs `timestamp` plus any of: heart_rate, steps, temperature, "
        "wrist_temperature, oxygen_saturation."
    )

if not uploaded:
    st.info("Upload a file to begin.")
    st.stop()

try:
    raw = ingest(uploaded)
    clean = records_to_minute_tidy(raw)
    m = compute_metrics(clean)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Rows", f"{m.n_rows:,}")
    c2.metric("Duration (hrs)", f"{m.duration_hours:.1f}")
    c3.metric("Sensors", ", ".join(m.sensors_present) or "—")
    c4.metric("Total Steps", f"{m.step_total:,}" if m.step_total else "—")
    c5.metric("Resting HR (≈5th %)", f"{m.resting_hr:.0f} bpm" if m.resting_hr else "—")

    c6, c7 = st.columns(2)
    if m.temp_mean is not None:
        c6.metric("Avg Temperature", f"{m.temp_mean:.1f} °C")
    if m.spo2_mean is not None:
        c7.metric("Avg SpO₂", f"{m.spo2_mean:.0f} %")

    st.subheader("Time Series")
    st.line_chart(clean.set_index("timestamp"))

    st.subheader("Daily Averages")
    st.bar_chart(m.daily_means)

    with st.expander("Clean Data"):
        st.dataframe(clean, use_container_width=True)

    st.download_button(
        "Download Clean CSV",
        clean.to_csv(index=False).encode("utf-8"),
        "cleaned_wearable.csv",
        "text/csv",
    )
    st.download_button(
        "Download Daily Means CSV",
        m.daily_means.reset_index().to_csv(index=False).encode("utf-8"),
        "daily_means.csv",
        "text/csv",
    )

except Exception as e:
    st.error(str(e))

