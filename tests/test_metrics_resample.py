# tests/test_metrics_resample.py
import numpy as np
import pandas as pd

import wearable_sensor_draft_code_11_03 as mod

def tidy_df(**cols):
    ts = pd.date_range("2025-01-01 00:00:00", periods=5, freq="min")
    df = pd.DataFrame({"timestamp": ts})
    for k, v in cols.items():
        df[k] = v if isinstance(v, (int, float)) else v[: len(ts)]
    return df

def test_compute_metrics_basic():
    df = tidy_df(heart_rate=[60, 61, 59, np.nan, 62], steps=[0, 10, 0, 50, 5])
    clean = mod.validate_and_clean(df)
    m = mod.compute_metrics(clean)
    assert m.n_rows == len(clean)
    assert "heart_rate" in m.sensors_present and "steps" in m.sensors_present
    assert isinstance(m.daily_means, pd.DataFrame)
    assert isinstance(m.daily_max, pd.DataFrame)
    assert m.duration_hours >= 0
    assert m.step_total == 65  # 0+10+0+50+5

def test_daily_resample_shapes():
    df = tidy_df(heart_rate=[60, 61, 62, 63, 64], steps=[0, 2, 4, 6, 8])
    clean = mod.validate_and_clean(df)
    means, mx = mod.resample_daily(clean)
    assert len(means) >= 1
    assert "heart_rate" in means.columns
    assert "steps" in mx.columns
