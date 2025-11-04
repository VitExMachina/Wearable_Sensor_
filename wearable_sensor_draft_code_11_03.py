# app.py (single-file version with: big-file handling, cloud link fetch, HTML guard, SpO₂ scaling, friendlier errors)

import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from io import BytesIO
from dataclasses import dataclass
from typing import List, Optional, Tuple
from pathlib import Path
from xml.etree.ElementTree import iterparse  # streaming XML parser

# =========================================================
# 1) CORE
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
            "No recognized sensor columns. Expected any of: "
            + ", ".join(sorted(ALLOWED_SENSORS))
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

    # Guard against empty HR arrays
    if "heart_rate" in sensor_cols:
        hr_vals = df["heart_rate"].dropna()
        resting_hr = float(np.nanpercentile(hr_vals, 5)) if len(hr_vals) else None
    else:
        resting_hr = None

    step_total = int(np.nansum(df["steps"])) if "steps" in sensor_cols else None
    temp_mean  = float(np.nanmean(df["temperature"])) if "temperature" in sensor_cols else None
    spo2_mean  = float(np.nanmean(df["oxygen_saturation"])) if "oxygen_saturation" in sensor_cols else None

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
# 2) INGEST (upload / path / cloud) + BIG FILES
# =========================================================
MAX_CHUNK = 100_000  # rows per chunk when reading large CSVs

_ALIAS_RAW = {
    "type": "@type", "@type": "@type",
    "creationDate": "@creationDate", "@creationDate": "@creationDate",
    "unit": "@unit", "@unit": "@unit",
    "value": "@value", "@value": "@value",
}
_ALIAS_TIDY = {
    "Timestamp": "timestamp", "Time": "timestamp", "timestamp": "timestamp",
    "HeartRate": "heart_rate", "HR": "heart_rate", "heartRate": "heart_rate", "heart_rate": "heart_rate",
    "Steps": "steps", "StepCount": "steps", "steps": "steps",
    "Temperature": "temperature", "BodyTemp": "temperature", "temperature": "temperature",
    "WristTemperature": "wrist_temperature", "wristTemperature": "wrist_temperature", "wrist_temperature": "wrist_temperature",
    "OxygenSaturation": "oxygen_saturation", "oxygenSaturation": "oxygen_saturation", "oxygen_saturation": "oxygen_saturation",
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
    """Streaming Apple Health XML reader for large exports."""
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
                "@creationDate": (
                    elem.attrib.get("creationDate")
                    or elem.attrib.get("endDate")
                    or elem.attrib.get("startDate")
                ),
                "@unit": elem.attrib.get("unit"),
                "@value": elem.attrib.get("value"),
            }
        )
        elem.clear()
    return pd.DataFrame(rows)

def _read_tabular_big(src, ext: str) -> pd.DataFrame:
    """Read big CSVs in chunks; Excel normal."""
    if ext == ".csv":
        parts = []
        for ch in pd.read_csv(src, chunksize=MAX_CHUNK):
            parts.append(ch)
        return pd.concat(parts, ignore_index=True, copy=False)
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(src)
    return pd.read_csv(src)

def ingest(source) -> pd.DataFrame:
    # file-like (Streamlit UploadedFile)
    if hasattr(source, "read"):
        name = getattr(source, "name", "") or ""
        ext = Path(name).suffix.lower()
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

def ingest_from_url(url: str) -> pd.DataFrame:
    """
    Download file from cloud/storage link and run through same ingestion.
    Supports:
      - direct http(s) links
      - Google Drive (file/d/<ID>/view, open?id=<ID>) public links
      - Dropbox (?dl=0 -> ?dl=1)
      - Raw GitHub URLs
    """
    url = url.strip()

    # Normalize common GDrive forms to direct download
    gd_file_match = re.search(r"https://drive\.google\.com/file/d/([^/]+)/", url)
    if gd_file_match:
        file_id = gd_file_match.group(1)
        url = f"https://drive.google.com/uc?export=download&id={file_id}"

    gd_open_match = re.search(r"https://drive\.google\.com/open\?id=([^&]+)", url)
    if gd_open_match:
        file_id = gd_open_match.group(1)
        url = f"https://drive.google.com/uc?export=download&id={file_id}"

    # Dropbox force download
    if "dropbox.com" in url and "dl=0" in url:
        url = url.replace("dl=0", "dl=1")

    resp = requests.get(url, stream=True, timeout=60, allow_redirects=True)
    if resp.status_code in (401, 403):
        raise RuntimeError(
            "Remote file isn’t public (401/403). For Google Drive, set sharing to "
            "‘Anyone with the link’ or use the direct ‘uc?export=download&id=FILE_ID’ URL."
        )
    resp.raise_for_status()

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype:
        body = resp.text[:400].replace("\n", " ")
        raise RuntimeError(
            "Got HTML instead of data (likely a login/permissions page). "
            f"First 400 chars: {body}"
        )

    content = resp.content
    suffix = Path(url.split("?")[0]).suffix.lower() or ".csv"
    bio = BytesIO(content)

    if suffix == ".xml":
        return _normalize_raw_cols(_read_xml_streaming(bio))
    else:
        return _normalize_raw_cols(_read_tabular_big(bio, suffix))

def _maybe_scale_spo2(series: pd.Series) -> pd.Series:
    """If SpO₂ looks 0–1, scale to %."""
    med = series.dropna().median()
    if pd.notna(med) and med < 2:
        return series * 100.0
    return series

def records_to_minute_tidy(df_raw: pd.DataFrame) -> pd.DataFrame:
    # Already tidy?
    tidy = _normalize_tidy_cols(df_raw.copy())
    if "timestamp" in tidy.columns:
        # Optional SpO₂ normalization if present
        if "oxygen_saturation" in tidy.columns:
            tidy["oxygen_saturation"] = _maybe_scale_spo2(tidy["oxygen_saturation"])
        return validate_and_clean(tidy)

    # Apple Health raw?
    need = {"@type", "@creationDate", "@value"}
    if need.issubset(df_raw.columns):
        df = df_raw.rename(
            columns={
                "@type": "Biometric_Label",
                "@creationDate": "Date",
                "@value": "Value",
            }
        )
        df["timestamp"] = pd.to_datetime(df["Date"], errors="coerce")
        df["Value"] = pd.to_numeric(df["Value"].astype(str).str.replace(",", "."), errors="coerce")
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

        # Drop rows where all sensors are NaN
        sensor_cols = list(set(v[0] for v in TYPE_MAP.values()))
        out = out.dropna(how="all", subset=sensor_cols)

        # Normalize SpO₂ if needed
        if "oxygen_saturation" in out.columns:
            out["oxygen_saturation"] = _maybe_scale_spo2(out["oxygen_saturation"])

        return validate_and_clean(out)

    # Last resort: try to guess a timestamp column by name and validate
    dt_guess = [c for c in df_raw.columns if "date" in c.lower() or "time" in c.lower()]
    if dt_guess:
        guess = df_raw.rename(columns={dt_guess[0]: "timestamp"})
        guess = _normalize_tidy_cols(guess)
        try:
            return validate_and_clean(guess)
        except Exception as e:
            cols_preview = list(df_raw.columns)[:12]
            raise ValueError(
                f"Tried '{dt_guess[0]}' as timestamp but failed: {e}\n"
                f"Columns seen: {cols_preview}"
            )

    cols_preview = list(df_raw.columns)[:12]
    raise ValueError(
        "Unsupported format. Provide Apple Health export or a tidy CSV.\n"
        f"Columns seen: {cols_preview}"
    )

# =========================================================
# 3) STREAMLIT UI
# =========================================================
st.set_page_config(page_title="Wearable Sensor Data Analyzer", layout="wide")
st.title("⌚ Wearable Sensor Data Analyzer")

with st.sidebar:
    src_mode = st.radio(
        "Select data source",
        ["Upload file", "Local/server path", "Cloud / storage link"],
    )

    uploaded = None
    local_path = None
    cloud_url = None

    if src_mode == "Upload file":
        uploaded = st.file_uploader(
            "Upload Apple Health export (.xml / .csv / .xlsx) or tidy CSV",
            type=["xml", "csv", "xlsx"],
            help=(
                "If your file is very large, consider the Cloud link option below. "
                "Apple Health XML is supported; CSV/XLSX should include Apple columns "
                "(@type, @creationDate, @value) or tidy columns (timestamp, heart_rate, etc.)."
            ),
        )
        if uploaded is not None:
            size_mb = len(uploaded.getvalue()) / (1024 * 1024)
            st.caption(f"Uploaded file size: {size_mb:.2f} MB")
    elif src_mode == "Local/server path":
        local_path = st.text_input("Enter full/local path to file")
        st.caption("Example: /Users/you/data/garrick_health_data_export.xml")
    else:  # Cloud / storage link
        cloud_url = st.text_input("Enter cloud URL (S3 / Dropbox / public Google Drive / raw GitHub)")
        st.caption(
            "For Google Drive, ensure the file is public OR use this format:\n"
            "https://drive.google.com/uc?export=download&id=FILE_ID\n"
            "If you get an HTML/login page, the app will show the first 400 chars for debugging."
        )

    st.caption(
        "Tidy CSV needs `timestamp` plus any of: heart_rate, steps, temperature, "
        "wrist_temperature, oxygen_saturation."
    )

# Decide source
if src_mode == "Upload file" and not uploaded:
    st.info("Upload a file to begin.")
    st.stop()
elif src_mode == "Local/server path" and not local_path:
    st.info("Enter a path to begin.")
    st.stop()
elif src_mode == "Cloud / storage link" and not cloud_url:
    st.info("Enter a cloud URL to begin.")
    st.stop()

try:
    if src_mode == "Upload file":
        raw = ingest(uploaded)
    elif src_mode == "Local/server path":
        raw = ingest(local_path)
    else:
        raw = ingest_from_url(cloud_url)

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
