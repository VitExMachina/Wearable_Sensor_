# app.py — Wearable Analyzer (single file)

import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from io import BytesIO
import altair as alt
from dataclasses import dataclass
from typing import List, Optional, Tuple
from pathlib import Path
from xml.etree.ElementTree import iterparse

# ======================= CORE =======================
ALLOWED_SENSORS = {"heart_rate", "steps", "temperature", "wrist_temperature", "oxygen_saturation"}

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

def _coerce_ts_utc(s: pd.Series) -> pd.Series:
    # parse as UTC to avoid tz comparison errors
    return pd.to_datetime(s, errors="coerce", utc=True)

def validate_and_clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "timestamp" not in df.columns:
        raise ValueError("Missing required column: 'timestamp'")
    df["timestamp"] = _coerce_ts_utc(df["timestamp"])
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

# ================= INGEST (big files + cloud) =================
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
    # Process in chunks to reduce memory usage
    rows = []
    df_chunks = []
    chunk_size = 50000
    for event, elem in iterparse(file_obj):
        if elem.tag != "Record":
            elem.clear(); continue
        t = elem.attrib.get("type")
        if not t:
            elem.clear(); continue
        rows.append({
            "@type": t,
            "@creationDate": (elem.attrib.get("creationDate") or elem.attrib.get("endDate") or elem.attrib.get("startDate")),
            "@unit": elem.attrib.get("unit"),
            "@value": elem.attrib.get("value"),
        })
        elem.clear()
        # Process in chunks to avoid excessive memory usage
        if len(rows) >= chunk_size:
            df_chunks.append(pd.DataFrame(rows))
            rows = []
    # Combine remaining rows
    if rows:
        df_chunks.append(pd.DataFrame(rows))
    if df_chunks:
        return pd.concat(df_chunks, ignore_index=True)
    return pd.DataFrame()

def _read_tabular_big(src, ext: str) -> pd.DataFrame:
    if ext == ".csv":
        # Process chunks incrementally to reduce peak memory
        parts = []
        for ch in pd.read_csv(src, chunksize=MAX_CHUNK):
            parts.append(ch)
            # Limit total chunks to prevent excessive memory
            if len(parts) >= 100:  # ~10M rows max
                break
        if parts:
            return pd.concat(parts, ignore_index=True, copy=False)
        return pd.DataFrame()
    if ext in (".xlsx", ".xls"):
        # Read only first sheet and limit rows for memory
        return pd.read_excel(src, nrows=10000000)  # 10M row limit
    return pd.read_csv(src, nrows=10000000)  # 10M row limit

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
    if m: url = f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    m = re.search(r"https://drive\.google\.com/open\?id=([^&]+)", url)
    if m: url = f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    if "dropbox.com" in url and "dl=0" in url:
        url = url.replace("dl=0","dl=1")

    resp = requests.get(url, stream=True, timeout=60, allow_redirects=True)
    if resp.status_code in (401,403):
        raise RuntimeError("Remote file is not public (401/403). For Google Drive, set sharing to 'Anyone with the link' or use the 'uc?export=download&id=FILE_ID' URL.")
    resp.raise_for_status()
    
    # Check content length to warn about large files
    content_length = resp.headers.get('Content-Length')
    if content_length:
        size_mb = int(content_length) / (1024 * 1024)
        if size_mb > 100:  # Warn if > 100MB
            st.warning(f"⚠️ Large file detected ({size_mb:.1f} MB). Processing may take time and use significant memory.")
    
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype:
        snippet = resp.text[:400].replace("\n"," ")
        raise RuntimeError("Got HTML instead of data (likely login/permission page). First 400 chars: " + snippet)

    # Stream content incrementally instead of loading all at once
    suffix = Path(url.split("?")[0]).suffix.lower() or ".csv"
    bio = BytesIO()
    max_size = 200 * 1024 * 1024  # 200MB limit
    downloaded = 0
    for chunk in resp.iter_content(chunk_size=8192):
        if chunk:
            downloaded += len(chunk)
            if downloaded > max_size:
                raise RuntimeError(f"File too large (>200MB). Please use a smaller file or split it.")
            bio.write(chunk)
    bio.seek(0)
    return _normalize_raw_cols(_read_xml_streaming(bio) if suffix == ".xml" else _read_tabular_big(bio, suffix))

# ============== FLEXIBLE RAW → TIDY ==============
def _maybe_scale_spo2(series: pd.Series) -> pd.Series:
    med = series.dropna().median()
    return series*100.0 if pd.notna(med) and med < 2 else series

def _map_type_to_col(t: str) -> Optional[str]:
    if not isinstance(t, str): return None
    if "HeartRate" in t: return "heart_rate"
    if "StepCount" in t: return "steps"
    if "OxygenSaturation" in t: return "oxygen_saturation"
    if "AppleSleepingWristTemperature" in t: return "wrist_temperature"
    if "BodyTemperature" in t or ("Temperature" in t and "Wrist" not in t): return "temperature"
    return None

def records_to_minute_tidy(df_raw: pd.DataFrame) -> pd.DataFrame:
    tidy = _normalize_tidy_cols(df_raw.copy())
    if "timestamp" in tidy.columns:
        tidy["timestamp"] = _coerce_ts_utc(tidy["timestamp"])
        if "oxygen_saturation" in tidy.columns:
            tidy["oxygen_saturation"] = _maybe_scale_spo2(tidy["oxygen_saturation"])
        return validate_and_clean(tidy)

    need = {"@type","@creationDate","@value"}
    if need.issubset(df_raw.columns):
        df = df_raw.rename(columns={"@type":"Biometric_Label","@creationDate":"Date","@value":"Value"})
        df["timestamp"] = _coerce_ts_utc(df["Date"])
        df["Value"] = pd.to_numeric(df["Value"].astype(str).str.replace(",", "."), errors="coerce")
        df = df.dropna(subset=["timestamp","Value","Biometric_Label"])
        df["minute"] = df["timestamp"].dt.floor("min")  # fix deprecated 'T'
        df["col"] = df["Biometric_Label"].map(_map_type_to_col)
        df = df.dropna(subset=["col"])

        out = None
        for col in df["col"].unique():
            d = df[df["col"] == col]
            func = "median" if col == "heart_rate" else ("sum" if col == "steps" else "mean")
            g = d.groupby("minute")["Value"].agg(func).rename(col).to_frame()
            # avoid overlap without suffix issues by outer-joining and resolving dup columns
            out = g if out is None else out.join(g, how="outer", rsuffix=f"__dup_{col}")

        if out is None or out.empty:
            raise ValueError("No supported biometric types found in file.")

        # If any accidental duplicate names slipped in, keep the first and drop the rsuffix columns
        dup_cols = [c for c in out.columns if "__dup_" in c]
        out = out.drop(columns=dup_cols) if dup_cols else out

        out = out.reset_index().rename(columns={"minute":"timestamp"}).sort_values("timestamp")

        if "oxygen_saturation" in out.columns:
            out["oxygen_saturation"] = _maybe_scale_spo2(out["oxygen_saturation"])

        out = out.dropna(how="all", subset=[c for c in out.columns if c != "timestamp"])
        return validate_and_clean(out)

    # last try: guess a time column
    guess_cols = [c for c in df_raw.columns if "date" in c.lower() or "time" in c.lower()]
    if guess_cols:
        guess = df_raw.rename(columns={guess_cols[0]:"timestamp"})
        guess = _normalize_tidy_cols(guess)
        return validate_and_clean(guess)

    raise ValueError("Unsupported format. Provide Apple Health export or tidy CSV.")

# ======================= UI =======================
st.set_page_config(page_title="Wearable Sensor Data Analyzer", layout="wide")
st.title("⌚ Wearable Sensor Data Analyzer")

# Add memory-efficient caching with size limits
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

with st.sidebar:
    src_mode = st.selectbox("Select data source", ["Upload file", "Local/server path", "Cloud / storage link"])
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

    show_diag = st.selectbox("Diagnostics", ["Off","Show raw @type counts"]) == "Show raw @type counts"

    # Date range filter (UTC-safe)
    date_mode = st.selectbox("Date filter", ["All dates", "Custom range"])
    start_date = end_date = None
    if date_mode == "Custom range":
        start_date = st.date_input("Start date")
        end_date   = st.date_input("End date")

# validate source selection
if src_mode == "Upload file" and not uploaded:
    st.info("Upload a file to begin."); st.stop()
if src_mode == "Local/server path" and not local_path:
    st.info("Enter a path to begin."); st.stop()
if src_mode == "Cloud / storage link" and not cloud_url:
    st.info("Enter a cloud URL to begin."); st.stop()

try:
    # ingest with caching
    if src_mode == "Upload file":
        # Check file size before processing
        file_size_mb = len(uploaded.getvalue()) / (1024 * 1024)
        if file_size_mb > 50:
            st.warning(f"⚠️ Large file ({file_size_mb:.1f} MB). Processing may take time.")
        raw = cached_ingest(uploaded, "upload")
    elif src_mode == "Local/server path":
        raw = cached_ingest(local_path, "path")
    else:
        raw = cached_ingest(cloud_url, "url")

    # diagnostics
    if show_diag:
        with st.expander("Diagnostics: Top @type values"):
            if "@type" in raw.columns:
                st.write(raw["@type"].value_counts().head(30))
            else:
                st.write("No @type column present (likely a tidy CSV).")

    # tidy + metrics (with caching)
    clean = cached_tidy_transform(raw)
    
    # Clear raw from memory after processing
    del raw

    # date filter (convert to UTC to match clean['timestamp'])
    if date_mode == "Custom range" and start_date and end_date:
        start_ts = pd.to_datetime(start_date).tz_localize("UTC")
        # include the full end day
        end_ts = pd.to_datetime(end_date).tz_localize("UTC") + pd.Timedelta(days=1)
        clean = clean[(clean["timestamp"] >= start_ts) & (clean["timestamp"] < end_ts)]

    if clean.empty:
        st.warning("No data after filtering. Adjust your date range.")
        st.stop()

    m = compute_metrics(clean)

    # KPI row
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Rows", f"{m.n_rows:,}")
    c2.metric("Duration (hrs)", f"{m.duration_hours:.1f}")
    c3.metric("Sensors", ", ".join(m.sensors_present) or "—")
    c4.metric("Total Steps", f"{m.step_total:,}" if m.step_total else "—")
    c5.metric("Resting HR (≈5th %)", f"{m.resting_hr:.0f} bpm" if m.resting_hr else "—")
    c6,c7 = st.columns(2)
    if m.temp_mean is not None: c6.metric("Avg Temperature", f"{m.temp_mean:.1f} °C")
    if m.spo2_mean is not None:  c7.metric("Avg SpO₂", f"{m.spo2_mean:.0f} %")

    st.subheader("Time Series (All Sensors)")
    # Limit data points for chart to reduce memory (sample if too large)
    if len(clean) > 50000:
        chart_data = clean.set_index("timestamp").resample("5min").mean()
        st.caption(f"📊 Showing resampled data (5-min averages) for {len(clean):,} points")
    else:
        chart_data = clean.set_index("timestamp")
    st.line_chart(chart_data)

    st.subheader("Daily Averages")
    st.bar_chart(m.daily_means)

    # ------- Sensor filter + per-sensor visuals -------
    st.subheader("Sensor Explorer")
    sensor_options = ["(select a sensor)"] + m.sensors_present
    sel_sensor = st.selectbox("Choose a sensor", sensor_options)
    if sel_sensor != "(select a sensor)":
        s = clean[["timestamp", sel_sensor]].dropna()
        st.line_chart(s.set_index("timestamp"))
        with st.expander("Rolling options"):
            win = st.selectbox("Rolling window (minutes)", ["5","10","15","30","60"], index=2)
        w = int(win)
        s_rolled = s.set_index("timestamp")[sel_sensor].rolling(f"{w}min").mean()
        st.line_chart(s_rolled)

    # ------- Correlations -------
    st.subheader("Correlations")
    corr_mode = st.selectbox("Select correlation view", ["Correlation matrix","Rolling correlation over time"])
    if corr_mode == "Correlation matrix":
        # prepare numeric sensors present
        num_cols = [c for c in m.sensors_present if clean[c].notna().any()]
        if len(num_cols) < 2:
            st.info("Need at least two sensors to compute a correlation matrix.")
        else:
            corr = clean[num_cols].corr()
            # Use Altair for correlation heatmap visualization
            corr_df = corr.reset_index().melt(id_vars='index', var_name='sensor2', value_name='correlation')
            corr_df = corr_df.rename(columns={'index': 'sensor1'})
            heatmap = alt.Chart(corr_df).mark_rect().encode(
                x=alt.X('sensor1:N', title='Sensor'),
                y=alt.Y('sensor2:N', title='Sensor'),
                color=alt.Color('correlation:Q', scale=alt.Scale(scheme='redyellowgreen', domain=[-1, 1]), title='Correlation'),
                tooltip=['sensor1', 'sensor2', 'correlation']
            ).properties(
                width=500,
                height=500
            )
            st.altair_chart(heatmap, use_container_width=True)
    else:
        # Rolling correlation between two selected sensors
        roll_cols = [c for c in m.sensors_present if clean[c].notna().any()]
        if len(roll_cols) < 2:
            st.info("Need two sensors to compute rolling correlation.")
        else:
            c1, c2 = st.columns(2)
            s1 = c1.selectbox("Sensor A", roll_cols, key="rc_a")
            s2 = c2.selectbox("Sensor B", roll_cols, index=1 if len(roll_cols)>1 else 0, key="rc_b")
            with st.expander("Rolling correlation settings"):
                rc_win = st.selectbox("Window (minutes)", ["10","15","30","60","120"], index=2, key="rc_win")
            wmin = int(rc_win)
            df_pair = clean[["timestamp", s1, s2]].dropna().set_index("timestamp")
            # align at minute frequency
            df_pair = df_pair.resample("1min").mean()
            # compute rolling corr
            roll_corr = df_pair[s1].rolling(f"{wmin}min").corr(df_pair[s2])
            st.line_chart(roll_corr)

    # ------- Data table & downloads -------
    with st.expander("Clean Data"):
        # Show limited rows in table to reduce memory
        st.dataframe(clean.head(10000), use_container_width=True)
        if len(clean) > 10000:
            st.caption(f"Showing first 10,000 rows of {len(clean):,} total rows. Use download button for full dataset.")

    # Generate CSV only when download button is clicked (lazy loading)
    @st.cache_data
    def generate_csv(df):
        return df.to_csv(index=False).encode("utf-8")
    
    if len(clean) > 100000:
        st.info("💡 Large dataset detected. CSV generation may take a moment.")
    st.download_button("Download Clean CSV", generate_csv(clean), "cleaned_wearable.csv", "text/csv")
    st.download_button("Download Daily Means CSV", generate_csv(m.daily_means.reset_index()), "daily_means.csv", "text/csv")

except Exception as e:
    st.error(str(e))
