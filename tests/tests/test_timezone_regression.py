# tests/test_timezone_regression.py
import pandas as pd

import wearable_sensor_draft_code_11_03 as mod

def apple_health_df(records):
    return pd.DataFrame(records, columns=["@type", "@creationDate", "@unit", "@value"])

def test_minute_floor_and_tz_issue_regression():
    # Ensure tz-aware timestamps floor to minute safely (no tz comparison errors)
    df_raw = apple_health_df([
        ("HKQuantityTypeIdentifierHeartRate", "2025-01-01T00:00:30-04:00", "count/min", "60"),
        ("HKQuantityTypeIdentifierHeartRate", "2025-01-01T00:00:45-04:00", "count/min", "62"),
    ])
    out = mod.records_to_minute_tidy(df_raw)
    # Both samples should land in the same floored minute
    assert len(out) == 1
    assert "heart_rate" in out.columns
