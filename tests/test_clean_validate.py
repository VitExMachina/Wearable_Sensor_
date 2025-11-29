# tests/test_clean_validate.py
import numpy as np
import pandas as pd
import pytest

import wearable_sensor_draft_code_11_03 as mod

def tidy_df(**cols):
    ts = pd.date_range("2025-01-01 00:00:00", periods=5, freq="min")
    df = pd.DataFrame({"timestamp": ts})
    for k, v in cols.items():
        df[k] = v if isinstance(v, (int, float)) else v[: len(ts)]
    return df

def test_validate_and_clean_happy_path():
    df = tidy_df(heart_rate=[60, 62, 61, 64, 63], steps=[0, 10, 0, 50, 20])
    out = mod.validate_and_clean(df)
    assert list(out.columns) == ["timestamp", "heart_rate", "steps"]
    assert len(out) == 5
    assert pd.api.types.is_datetime64_any_dtype(out["timestamp"])

def test_validate_and_clean_missing_timestamp_raises():
    df = pd.DataFrame({"heart_rate": [60, 61]})
    with pytest.raises(ValueError, match="Missing required column: 'timestamp'"):
        mod.validate_and_clean(df)

def test_validate_and_clean_no_known_sensors_raises():
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=3, freq="min"),
        "foo": [1, 2, 3],
    })
    with pytest.raises(ValueError, match="No recognized sensor columns"):
        mod.validate_and_clean(df)

def test_validate_and_clean_drops_all_nan_sensor_rows():
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=3, freq="min"),
        "heart_rate": [np.nan, 70, np.nan],
        "steps": [np.nan, np.nan, np.nan],
    })
    out = mod.validate_and_clean(df)
    assert len(out) == 1
    assert out["heart_rate"].iloc[0] == 70

def test_validate_and_clean_deduplicates_and_sorts():
    ts = pd.Timestamp("2025-01-01 00:00:00")
    df = pd.DataFrame({
        "timestamp": [ts, ts, ts + pd.Timedelta(minutes=1)],
        "heart_rate": [60, 60, 61],
    })
    out = mod.validate_and_clean(df)
    assert len(out) == 2
    assert out["timestamp"].is_monotonic_increasing
