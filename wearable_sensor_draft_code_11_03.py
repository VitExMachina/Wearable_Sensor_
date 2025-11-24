 # app.py (single-file) — flexible type matching + diagnostics + big-file/cloud ingest

import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from io import BytesIO
from dataclasses import dataclass
from typing import List, Optional, Tuple
from pathlib import Path
from xml.etree.ElementTree import iterparse

# =========== CORE ===========
ALLOWED_SENSORS = {
    "heart_rate", "steps", "temperature", "wrist_temperature", "oxygen_saturation",
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
    sens = [c for c in df.columns if c in ALLOWED_SENSORS]
    if not sens:
        raise ValueError("No recognized sensor columns. Expected any of: " + ", ".join(sorted(ALLOWED_SENSORS)))
    for c in sens:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=sens, how="all").drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    return df[["timestamp"] + sens]

def resample_daily(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    dfi = df.set_index("timestamp")
    return dfi.resample("1D").mean(numeric_only=True), dfi.resample("1D").max(numeric_only=True)

def compute_metrics(df: pd.DataFrame) -> Metrics:
    sens = [c for c in df.columns if c in ALLOWED_SENSORS]
    daily_means, daily_max = resample_daily(df)
    resting_hr = float(np.nanpercentile(df["heart_rate"].dropna(), 5)) if "heart_rate" in sens and df["heart_rate"].notna().any() else None
    step_total = int(np.nansum(df["steps"])) if "steps" in sens else None
    temp_mean  = float(np.nanmean(df["temperature"])) if "temperature" in sens else None
    spo2_mean  = float(np.nanmean(df["oxygen_saturation"])) if "oxygen_saturation" in sens else None
    t0, t1 = df["timestamp"].min(), df["timestamp"].max()
    duration_hours = (t1 - t0).total_seconds()/3600.0 if len(df) else 0.0
    return Metrics(len(df), t0, t1, duration_hours, sens, daily_means, daily_max, resting_hr, step_total, temp_mean, spo2_mean)

# =========== INGEST (big files + cloud) ===========
MAX_CHUNK = 100_000

_ALIAS_RAW = {
    "type":"@type", "@type":"@type",
    "creationDate":"@creationDate", "@creationDate":"@creationDate",
    "unit":"@unit", "@unit":"@unit",
    "value":"@value", "@value":"@value",
}
_ALIAS_TIDY = {
    "Timestamp":"timestamp","Time":"timestamp","timestamp":"timestamp",
    "HeartRate":"heart_rate","HR":"heart_rate","heartRate":"heart_rate","heart_rate":"heart_rate",
    "Steps":"steps","StepCount":"steps","steps":"steps",
    "Temperature":"temperature","BodyTemp":"temperature","temperature":"temperature",
    "WristTemperature":"wrist_temperature","wristTemperature":"wrist_temperature","wrist_temperature":"wrist_temperature",
    "OxygenSaturation":"oxygen_saturation","oxygenSaturation":"oxygen_saturation","oxygen_saturation":"oxygen_saturation",
}

def _normalize_raw_cols(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={c: _ALIAS_RAW.get(c, c) for c in df.columns})

def _normalize_tidy_cols(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={c: _ALIAS_TIDY.get(c, c) for c in df.columns})

def _read_xml_streaming(file_obj) -> pd.DataFrame:
    rows = []
    for event, elem in iterparse(file_obj):
        if elem.tag != "Record":
            elem.clear(); continue
        t = elem.attrib.get("type"); 
        if not t: elem.clear(); continue
        rows.append({
            "@type": t,
            "@creationDate": (elem.attrib.get("creationDate") or elem.attrib.get("endDate") or elem.attrib.get("startDate")),
            "@unit": elem.attrib.get("unit"),
            "@value": elem.attrib.get("value"),
        })
        elem.clear()
    return pd.DataFrame(rows)

def _read_tabular_big(src, ext: str) -> pd.DataFrame:
    if ext == ".csv":
        parts = [ch for ch in pd.read_csv(src, chunksize=MAX_CHUNK)]
        return pd.concat(parts, ignore_index=True, copy=False) if parts else pd.DataFrame()
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(src)
    return pd.read_csv(src)

def ingest(source) -> pd.DataFrame:
    if hasattr(source, "read"):
        name = getattr(source, "name", "") or ""
        ext = Path(name).suffix.lower()
        source.seek(0)
        df = _read_xml_streaming(source) if ext == ".xml" else _read_tabular_big(source, ext)
        return _normalize_raw_cols(df)
    p = Path(str(source)); ext = p.suffix.lower()
    if ext == ".xml":
        with open(p, "rb") as f:
            df = _read_xml_streaming(f)
    else:
        df = _read_tabular_big(p, ext)
    return _normalize_raw_cols(df)

def ingest_from_url(url: str) -> pd.DataFrame:
    url = url.strip()
    m = re.search(r"https://drive\.google\.com/file/d/([^/]+)/", url)
    if m:
        url = f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    m = re.search(r"https://drive\.google\.com/open\?id=([^&]+)", url)
    if m:
        url = f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    if "dropbox.com" in url and "dl=0" in url:
        url = url.replace("dl=0","dl=1")

    resp = requests.get(url, stream=True, timeout=60, allow_redirects=True)
    if resp.status_code in (401,403):
        raise RuntimeError("Remote file is not public (401/403). For Google Drive, set sharing to 'Anyone with the link' or use the 'uc?export=download&id=FILE_ID' URL.")
    resp.raise_for_status()
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype:
        snippet = resp.text[:400].replace("\n"," ")
        raise RuntimeError("Got HTML instead of data (likely login/permission page). First 400 chars: " + snippet)

    bio = BytesIO(resp.content)
    suffix = Path(url.split("?")[0]).suffix.lower() or ".csv"
    return _normalize_raw_cols(_read_xml_streaming(bio) if suffix == ".xml" else _read_tabular_big(bio, suffix))

# =========== NORMALIZATION FUNCTIONS ===========
def normalize_min_max(series: pd.Series) -> pd.Series:
    """Normalize series to 0-1 range using min-max scaling."""
    min_val = series.min()
    max_val = series.max()
    if pd.isna(min_val) or pd.isna(max_val) or (max_val - min_val) == 0:
        return series
    return (series - min_val) / (max_val - min_val)

def normalize_zscore(series: pd.Series) -> pd.Series:
    """Normalize series using z-score (standardization)."""
    mean_val = series.mean()
    std_val = series.std()
    if pd.isna(mean_val) or pd.isna(std_val) or std_val == 0:
        return series
    return (series - mean_val) / std_val

def normalize_percentage_max(series: pd.Series) -> pd.Series:
    """Normalize series as percentage of maximum value (0-100%)."""
    max_val = series.max()
    if pd.isna(max_val) or max_val == 0:
        return series
    return (series / max_val) * 100

# =========== FLEXIBLE RAW → TIDY ===========
def _maybe_scale_spo2(series: pd.Series) -> pd.Series:
    med = series.dropna().median()
    return series*100.0 if pd.notna(med) and med < 2 else series

def _map_type_to_col(t: str) -> Optional[str]:
    """Flexible mapping: match by substring so minor variations don’t break aggregation."""
    if not isinstance(t, str): return None
    if "HeartRate" in t: return "heart_rate"
    if "StepCount" in t: return "steps"
    if "OxygenSaturation" in t: return "oxygen_saturation"
    if "AppleSleepingWristTemperature" in t: return "wrist_temperature"
    if "BodyTemperature" in t or ("Temperature" in t and "Wrist" not in t): return "temperature"
    return None

def records_to_minute_tidy(df_raw: pd.DataFrame) -> pd.DataFrame:
    # Already tidy?
    tidy = _normalize_tidy_cols(df_raw.copy())
    if "timestamp" in tidy.columns:
        if "oxygen_saturation" in tidy.columns:
            tidy["oxygen_saturation"] = _maybe_scale_spo2(tidy["oxygen_saturation"])
        return validate_and_clean(tidy)

    # Apple Health raw?
    need = {"@type","@creationDate","@value"}
    if need.issubset(df_raw.columns):
        df = df_raw.rename(columns={"@type":"Biometric_Label","@creationDate":"Date","@value":"Value"})
        df["timestamp"] = pd.to_datetime(df["Date"], errors="coerce")
        df["Value"] = pd.to_numeric(df["Value"].astype(str).str.replace(",", "."), errors="coerce")
        df = df.dropna(subset=["timestamp","Value","Biometric_Label"])
        df["minute"] = df["timestamp"].dt.floor("min")  # <- fix deprecated 'T'
        df["col"] = df["Biometric_Label"].map(_map_type_to_col)
        df = df.dropna(subset=["col"])

        # Aggregate per-minute per-sensor with appropriate function
        out = None
        for col in df["col"].unique():
            d = df[df["col"] == col]
            func = "median" if col == "heart_rate" else ("sum" if col == "steps" else "mean")
            g = d.groupby("minute")["Value"].agg(func).rename(col).to_frame()
            out = g if out is None else out.join(g, how="outer")

        if out is None or out.empty:
            raise ValueError("No supported biometric types found in file.")

        out = out.reset_index().rename(columns={"minute":"timestamp"}).sort_values("timestamp")
        # Normalize SpO₂ if present
        if "oxygen_saturation" in out.columns:
            out["oxygen_saturation"] = _maybe_scale_spo2(out["oxygen_saturation"])
        # Drop rows where all sensors are NaN
        out = out.dropna(how="all", subset=[c for c in out.columns if c != "timestamp"])
        return validate_and_clean(out)

    # Last attempt: guess a time column
    guess_cols = [c for c in df_raw.columns if "date" in c.lower() or "time" in c.lower()]
    if guess_cols:
        guess = df_raw.rename(columns={guess_cols[0]:"timestamp"})
        guess = _normalize_tidy_cols(guess)
        return validate_and_clean(guess)

    raise ValueError("Unsupported format. Provide Apple Health export or tidy CSV.")

# =========== UI ===========
st.set_page_config(page_title="Wearable Sensor Data Analyzer", layout="wide")
st.title("⌚ Wearable Sensor Data Analyzer")

# Add caching for expensive operations
@st.cache_data(max_entries=2, ttl=3600, show_spinner="Processing data...")
def cached_ingest(source, source_type):
    """Cache ingestion to avoid reprocessing on reruns."""
    if source_type == "upload":
        return ingest(source)
    elif source_type == "path":
        return ingest(source)
    else:  # url
        return ingest_from_url(source)

@st.cache_data(max_entries=2, ttl=3600)
def cached_tidy_transform(raw_df):
    """Cache the tidy transformation."""
    return records_to_minute_tidy(raw_df)

@st.cache_resource(max_entries=2)
def cached_compute_metrics(clean_df):
    """Cache metrics computation. Uses cache_resource because Metrics contains DataFrames."""
    return compute_metrics(clean_df)

with st.sidebar:
    src_mode = st.radio("Select data source", ["Upload file", "Local/server path", "Cloud / storage link"])
    uploaded = None; local_path = None; cloud_url = None
    if src_mode == "Upload file":
        uploaded = st.file_uploader("Upload Apple Health export (.xml / .csv / .xlsx) or tidy CSV", type=["xml","csv","xlsx"])
        if uploaded is not None:
            st.caption(f"Uploaded size: {len(uploaded.getvalue())/(1024*1024):.2f} MB")
    elif src_mode == "Local/server path":
        local_path = st.text_input("Enter full/local path to file")
    else:
        cloud_url = st.text_input("Enter cloud URL (S3 / Dropbox / public Google Drive / raw GitHub)")
        st.caption("Google Drive tip: use https://drive.google.com/uc?export=download&id=FILE_ID and make the file public.")
    show_diag = st.checkbox("Show raw type counts (diagnostics)")

if src_mode == "Upload file" and not uploaded:
    st.info("Upload a file to begin."); st.stop()
if src_mode == "Local/server path" and not local_path:
    st.info("Enter a path to begin."); st.stop()
if src_mode == "Cloud / storage link" and not cloud_url:
    st.info("Enter a cloud URL to begin."); st.stop()

try:
    # Use cached ingestion
    if src_mode == "Upload file":
        file_size_mb = len(uploaded.getvalue()) / (1024 * 1024)
        if file_size_mb > 50:
            st.warning(f"⚠️ Large file ({file_size_mb:.1f} MB). Processing may take time.")
        raw = cached_ingest(uploaded, "upload")
    elif src_mode == "Local/server path":
        raw = cached_ingest(local_path, "path")
    else:
        raw = cached_ingest(cloud_url, "url")

    # Optional quick diagnostics: what @type values exist?
    if show_diag:
        with st.expander("Diagnostics: Top @type values"):
            if "@type" in raw.columns:
                counts = raw["@type"].value_counts().head(30)
                st.write(counts)
            else:
                st.write("No @type column present (likely a tidy CSV).")

    # Use cached transformations
    clean = cached_tidy_transform(raw)
    # Clear raw from memory after processing
    del raw
    m = cached_compute_metrics(clean)

    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Rows", f"{m.n_rows:,}")
    c2.metric("Duration (hrs)", f"{m.duration_hours:.1f}")
    c3.metric("Sensors", ", ".join(m.sensors_present) or "—")
    c4.metric("Total Steps", f"{m.step_total:,}" if m.step_total else "—")
    c5.metric("Resting HR (≈5th %)", f"{m.resting_hr:.0f} bpm" if m.resting_hr else "—")

    c6,c7 = st.columns(2)
    if m.temp_mean is not None: c6.metric("Avg Temperature", f"{m.temp_mean:.1f} °C")
    if m.spo2_mean is not None:  c7.metric("Avg SpO₂", f"{m.spo2_mean:.0f} %")

    st.subheader("Time Series")
    # Filter by sensor
    available_sensors_ts = [col for col in clean.columns if col in ALLOWED_SENSORS]
    if available_sensors_ts:
        selected_sensors_ts = st.multiselect(
            "Filter sensors to display",
            options=available_sensors_ts,
            default=available_sensors_ts,
            key="time_series_filter"
        )
        if selected_sensors_ts:
            filtered_clean = clean[["timestamp"] + selected_sensors_ts]
            st.line_chart(filtered_clean.set_index("timestamp"))
        else:
            st.info("Select at least one sensor to display.")
    else:
        st.line_chart(clean.set_index("timestamp"))

    st.subheader("Sensor Comparison")
    # Compare two sensors
    available_sensors_compare = [col for col in clean.columns if col in ALLOWED_SENSORS]
    if len(available_sensors_compare) >= 2:
        col1, col2 = st.columns(2)
        with col1:
            sensor1 = st.selectbox(
                "Select first sensor",
                options=available_sensors_compare,
                key="sensor1_compare"
            )
        with col2:
            sensor2 = st.selectbox(
                "Select second sensor",
                options=[s for s in available_sensors_compare if s != sensor1],
                key="sensor2_compare"
            )
        
        # Normalization option
        normalize_option = st.radio(
            "Normalization method (for better comparison across different scales):",
            ["None", "Min-Max (0-1)", "Z-Score (Standardized)", "Percentage of Max (0-100%)"],
            horizontal=True,
            key="normalize_method"
        )
        
        if sensor1 and sensor2:
            comparison_data = clean[["timestamp", sensor1, sensor2]].set_index("timestamp").copy()
            
            # Apply normalization if selected
            if normalize_option == "Min-Max (0-1)":
                comparison_data[sensor1] = normalize_min_max(comparison_data[sensor1])
                comparison_data[sensor2] = normalize_min_max(comparison_data[sensor2])
                st.caption("📊 Data normalized to 0-1 range for comparison")
            elif normalize_option == "Z-Score (Standardized)":
                comparison_data[sensor1] = normalize_zscore(comparison_data[sensor1])
                comparison_data[sensor2] = normalize_zscore(comparison_data[sensor2])
                st.caption("📊 Data standardized (z-score) for comparison")
            elif normalize_option == "Percentage of Max (0-100%)":
                comparison_data[sensor1] = normalize_percentage_max(comparison_data[sensor1])
                comparison_data[sensor2] = normalize_percentage_max(comparison_data[sensor2])
                st.caption("📊 Data shown as percentage of maximum value")
            
            st.line_chart(comparison_data)
        else:
            st.info("Please select two sensors to compare.")
    elif len(available_sensors_compare) == 1:
        st.info(f"Only one sensor ({available_sensors_compare[0]}) is available. Need at least two sensors for comparison.")
    else:
        st.info("No sensors available for comparison.")

    st.subheader("Daily Averages")
    # Filter by sensor
    available_sensors = [col for col in m.daily_means.columns if col in ALLOWED_SENSORS]
    if available_sensors:
        selected_sensors = st.multiselect(
            "Filter sensors to display",
            options=available_sensors,
            default=available_sensors,
            key="daily_avg_filter"
        )
        if selected_sensors:
            filtered_daily_means = m.daily_means[selected_sensors]
            st.bar_chart(filtered_daily_means)
        else:
            st.info("Select at least one sensor to display.")
    else:
        st.bar_chart(m.daily_means)

    with st.expander("Clean Data"):
        st.dataframe(clean, use_container_width=True)

    st.download_button("Download Clean CSV", clean.to_csv(index=False).encode("utf-8"), "cleaned_wearable.csv", "text/csv")
    st.download_button("Download Daily Means CSV", m.daily_means.reset_index().to_csv(index=False).encode("utf-8"), "daily_means.csv", "text/csv")

except Exception as e:
    st.error(str(e))
