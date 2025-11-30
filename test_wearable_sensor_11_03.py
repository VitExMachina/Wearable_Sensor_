"""
Unit tests for wearable_sensor_draft_code_11_03.py

Run with: pytest test_wearable_sensor_11_03.py -v
"""

import pytest
import pandas as pd
import numpy as np
from io import BytesIO, StringIO
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET

# Import functions from the main module
# Note: In a real setup, you'd import from a module, but since it's a single file,
# we'll need to extract testable functions or refactor slightly
import sys
import importlib.util

# Load the module
spec = importlib.util.spec_from_file_location("wearable_sensor", "wearable_sensor_draft_code_11_03.py")
wearable_sensor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wearable_sensor)

# Extract functions for testing
validate_and_clean = wearable_sensor.validate_and_clean
resample_daily = wearable_sensor.resample_daily
compute_metrics = wearable_sensor.compute_metrics
_normalize_raw_cols = wearable_sensor._normalize_raw_cols
_normalize_tidy_cols = wearable_sensor._normalize_tidy_cols
_maybe_scale_spo2 = wearable_sensor._maybe_scale_spo2
_map_type_to_col = wearable_sensor._map_type_to_col
records_to_minute_tidy = wearable_sensor.records_to_minute_tidy
ALLOWED_SENSORS = wearable_sensor.ALLOWED_SENSORS
Metrics = wearable_sensor.Metrics


# =========== Test Data Helpers ===========

def create_sample_tidy_data():
    """Create sample tidy CSV data."""
    dates = pd.date_range("2025-01-01", periods=100, freq="1min")
    return pd.DataFrame({
        "timestamp": dates,
        "heart_rate": np.random.randint(60, 100, 100),
        "steps": np.random.randint(0, 10, 100),
        "temperature": np.random.uniform(36.0, 37.5, 100),
    })


def create_sample_raw_apple_health():
    """Create sample Apple Health XML structure."""
    root = ET.Element("HealthData")
    export_date = ET.SubElement(root, "ExportDate")
    export_date.set("value", "2025-01-01")
    
    # Add some records
    for i in range(10):
        record = ET.SubElement(root, "Record")
        record.set("type", "HKQuantityTypeIdentifierHeartRate")
        record.set("creationDate", f"2025-01-01 00:0{i}:00 +0000")
        record.set("value", str(70 + i))
        record.set("unit", "count/min")
    
    return root


# =========== Test validate_and_clean ===========

def test_validate_and_clean_basic():
    """Test basic validation and cleaning."""
    df = create_sample_tidy_data()
    result = validate_and_clean(df)
    
    assert "timestamp" in result.columns
    assert "heart_rate" in result.columns
    assert "steps" in result.columns
    assert "temperature" in result.columns
    assert len(result) == 100
    assert result["timestamp"].dtype == "datetime64[ns]"


def test_validate_and_clean_missing_timestamp():
    """Test that missing timestamp raises ValueError."""
    df = pd.DataFrame({
        "heart_rate": [70, 72],
        "steps": [0, 1]
    })
    
    with pytest.raises(ValueError, match="Missing required column: 'timestamp'"):
        validate_and_clean(df)


def test_validate_and_clean_no_sensor_columns():
    """Test that missing sensor columns raises ValueError."""
    df = pd.DataFrame({
        "timestamp": ["2025-01-01", "2025-01-02"],
        "other_col": [1, 2]
    })
    
    with pytest.raises(ValueError, match="No recognized sensor columns"):
        validate_and_clean(df)


def test_validate_and_clean_drops_invalid_timestamps():
    """Test that invalid timestamps are dropped."""
    df = pd.DataFrame({
        "timestamp": ["invalid", "2025-01-01 00:00:00", "also_invalid"],
        "heart_rate": [70, 72, 73]
    })
    
    result = validate_and_clean(df)
    assert len(result) == 1
    assert pd.notna(result["timestamp"].iloc[0])


def test_validate_and_clean_drops_duplicates():
    """Test that duplicate timestamps are removed."""
    df = pd.DataFrame({
        "timestamp": ["2025-01-01 00:00:00", "2025-01-01 00:00:00", "2025-01-01 00:01:00"],
        "heart_rate": [70, 72, 73]
    })
    
    result = validate_and_clean(df)
    assert len(result) == 2  # One duplicate removed


def test_validate_and_clean_drops_all_nan_sensor_rows():
    """Test that rows with all NaN sensor values are dropped."""
    df = pd.DataFrame({
        "timestamp": ["2025-01-01 00:00:00", "2025-01-01 00:01:00", "2025-01-01 00:02:00"],
        "heart_rate": [70, np.nan, np.nan],
        "steps": [0, np.nan, np.nan]
    })
    
    result = validate_and_clean(df)
    assert len(result) == 1  # Two rows with all NaN sensors dropped


def test_validate_and_clean_sorts_by_timestamp():
    """Test that result is sorted by timestamp."""
    df = pd.DataFrame({
        "timestamp": ["2025-01-01 00:02:00", "2025-01-01 00:00:00", "2025-01-01 00:01:00"],
        "heart_rate": [72, 70, 71]
    })
    
    result = validate_and_clean(df)
    assert result["timestamp"].iloc[0] < result["timestamp"].iloc[1]
    assert result["timestamp"].iloc[1] < result["timestamp"].iloc[2]


# =========== Test resample_daily ===========

def test_resample_daily_basic():
    """Test daily resampling produces means and max."""
    df = create_sample_tidy_data()
    daily_means, daily_max = resample_daily(df)
    
    assert isinstance(daily_means, pd.DataFrame)
    assert isinstance(daily_max, pd.DataFrame)
    assert len(daily_means) >= 1
    assert "heart_rate" in daily_means.columns
    assert "steps" in daily_max.columns


def test_resample_daily_multiple_days():
    """Test resampling across multiple days."""
    dates = pd.date_range("2025-01-01", periods=2880, freq="1min")  # 2 days
    df = pd.DataFrame({
        "timestamp": dates,
        "heart_rate": np.random.randint(60, 100, 2880),
        "steps": np.random.randint(0, 10, 2880),
    })
    
    daily_means, daily_max = resample_daily(df)
    assert len(daily_means) == 2  # Two days


# =========== Test compute_metrics ===========

def test_compute_metrics_basic():
    """Test basic metrics computation."""
    df = create_sample_tidy_data()
    metrics = compute_metrics(df)
    
    assert isinstance(metrics, Metrics)
    assert metrics.n_rows == 100
    assert metrics.duration_hours > 0
    assert "heart_rate" in metrics.sensors_present
    assert metrics.resting_hr is not None
    assert metrics.step_total is not None
    assert metrics.temp_mean is not None


def test_compute_metrics_resting_hr():
    """Test that resting HR is 5th percentile."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=100, freq="1min"),
        "heart_rate": [50] * 95 + [100] * 5  # 95 values at 50, 5 at 100
    })
    
    metrics = compute_metrics(df)
    assert metrics.resting_hr == 50.0  # 5th percentile should be 50


def test_compute_metrics_step_total():
    """Test that step total is sum of all steps."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=10, freq="1min"),
        "steps": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    })
    
    metrics = compute_metrics(df)
    assert metrics.step_total == 55  # Sum of 1-10


def test_compute_metrics_missing_sensors():
    """Test metrics with missing sensor types."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=10, freq="1min"),
        "heart_rate": [70] * 10
        # No steps, temperature, etc.
    })
    
    metrics = compute_metrics(df)
    assert metrics.step_total is None
    assert metrics.temp_mean is None
    assert metrics.spo2_mean is None
    assert metrics.resting_hr is not None


def test_compute_metrics_empty_dataframe():
    """Test metrics with empty dataframe."""
    # Create empty DataFrame with proper datetime dtype for timestamp
    df = pd.DataFrame({
        "timestamp": pd.to_datetime([]),
        "heart_rate": []
    })
    
    metrics = compute_metrics(df)
    assert metrics.n_rows == 0
    assert metrics.duration_hours == 0.0


# =========== Test column normalization ===========

def test_normalize_raw_cols():
    """Test raw column normalization."""
    df = pd.DataFrame({
        "type": ["A", "B"],
        "@type": ["C", "D"],
        "creationDate": ["2025-01-01", "2025-01-02"]
    })
    
    result = _normalize_raw_cols(df)
    assert "@type" in result.columns
    assert "@creationDate" in result.columns


def test_normalize_tidy_cols():
    """Test tidy column normalization."""
    df = pd.DataFrame({
        "Timestamp": [1, 2],
        "HeartRate": [70, 72],
        "Steps": [0, 1]
    })
    
    result = _normalize_tidy_cols(df)
    assert "timestamp" in result.columns
    assert "heart_rate" in result.columns
    assert "steps" in result.columns


# =========== Test SpO2 scaling ===========

def test_maybe_scale_spo2_scales_low_values():
    """Test that SpO2 values < 2 are scaled by 100."""
    series = pd.Series([0.95, 0.96, 0.97])
    result = _maybe_scale_spo2(series)
    
    assert result.iloc[0] == 95.0
    assert result.iloc[1] == 96.0
    assert result.iloc[2] == 97.0


def test_maybe_scale_spo2_doesnt_scale_high_values():
    """Test that SpO2 values >= 2 are not scaled."""
    series = pd.Series([95, 96, 97])
    result = _maybe_scale_spo2(series)
    
    assert result.iloc[0] == 95
    assert result.iloc[1] == 96
    assert result.iloc[2] == 97


def test_maybe_scale_spo2_handles_nan():
    """Test that SpO2 scaling handles NaN values."""
    series = pd.Series([0.95, np.nan, 0.97])
    result = _maybe_scale_spo2(series)
    
    assert pd.isna(result.iloc[1])
    assert result.iloc[0] == 95.0


# =========== Test type mapping ===========

def test_map_type_to_col_heart_rate():
    """Test mapping heart rate types."""
    assert _map_type_to_col("HKQuantityTypeIdentifierHeartRate") == "heart_rate"
    assert _map_type_to_col("SomeHeartRateType") == "heart_rate"


def test_map_type_to_col_steps():
    """Test mapping step count types."""
    assert _map_type_to_col("HKQuantityTypeIdentifierStepCount") == "steps"


def test_map_type_to_col_temperature():
    """Test mapping temperature types."""
    assert _map_type_to_col("HKQuantityTypeIdentifierBodyTemperature") == "temperature"
    assert _map_type_to_col("SomeTemperatureType") == "temperature"


def test_map_type_to_col_wrist_temperature():
    """Test mapping wrist temperature types."""
    assert _map_type_to_col("HKQuantityTypeIdentifierAppleSleepingWristTemperature") == "wrist_temperature"


def test_map_type_to_col_oxygen_saturation():
    """Test mapping oxygen saturation types."""
    assert _map_type_to_col("HKQuantityTypeIdentifierOxygenSaturation") == "oxygen_saturation"


def test_map_type_to_col_unknown():
    """Test that unknown types return None."""
    assert _map_type_to_col("UnknownType") is None
    assert _map_type_to_col(None) is None
    assert _map_type_to_col(123) is None


# =========== Test records_to_minute_tidy ===========

def test_records_to_minute_tidy_already_tidy():
    """Test that already tidy data passes through."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=10, freq="1min"),
        "heart_rate": [70] * 10,
        "steps": [0] * 10
    })
    
    result = records_to_minute_tidy(df)
    assert len(result) == 10
    assert "heart_rate" in result.columns


def test_records_to_minute_tidy_apple_health_format():
    """Test conversion from Apple Health raw format."""
    df_raw = pd.DataFrame({
        "@type": [
            "HKQuantityTypeIdentifierHeartRate",
            "HKQuantityTypeIdentifierHeartRate",
            "HKQuantityTypeIdentifierStepCount",
        ],
        "@creationDate": [
            "2025-01-01 00:00:00",
            "2025-01-01 00:00:30",
            "2025-01-01 00:00:00",
        ],
        "@value": [70, 72, 5]
    })
    
    result = records_to_minute_tidy(df_raw)
    assert "heart_rate" in result.columns
    assert "steps" in result.columns
    assert len(result) >= 1


def test_records_to_minute_tidy_aggregates_by_minute():
    """Test that records are aggregated by minute."""
    df_raw = pd.DataFrame({
        "@type": ["HKQuantityTypeIdentifierHeartRate"] * 3,
        "@creationDate": [
            "2025-01-01 00:00:00",
            "2025-01-01 00:00:30",
            "2025-01-01 00:00:45",
        ],
        "@value": [70, 72, 74]
    })
    
    result = records_to_minute_tidy(df_raw)
    # Should aggregate 3 values into 1 minute (median for heart_rate)
    assert len(result) == 1
    assert result["heart_rate"].iloc[0] == 72.0  # Median of [70, 72, 74]


def test_records_to_minute_tidy_unsupported_format():
    """Test that unsupported format raises ValueError."""
    df_raw = pd.DataFrame({
        "unknown_col": [1, 2, 3]
    })
    
    with pytest.raises(ValueError, match="Unsupported format"):
        records_to_minute_tidy(df_raw)


def test_records_to_minute_tidy_no_supported_types():
    """Test that no supported types raises ValueError."""
    df_raw = pd.DataFrame({
        "@type": ["UnsupportedType"] * 3,
        "@creationDate": ["2025-01-01 00:00:00"] * 3,
        "@value": [1, 2, 3]
    })
    
    with pytest.raises(ValueError, match="No supported biometric types"):
        records_to_minute_tidy(df_raw)


# =========== Test edge cases ===========

def test_validate_and_clean_with_mixed_case_columns():
    """Test validation with mixed case column names."""
    df = pd.DataFrame({
        "Timestamp": pd.date_range("2025-01-01", periods=5, freq="1min"),
        "HeartRate": [70, 72, 74, 76, 78]
    })
    
    # After normalization, should work
    df_normalized = _normalize_tidy_cols(df)
    result = validate_and_clean(df_normalized)
    assert len(result) == 5


def test_compute_metrics_with_single_row():
    """Test metrics computation with single row."""
    # Convert timestamp to datetime for proper resampling
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2025-01-01 00:00:00"]),
        "heart_rate": [70],
        "steps": [0]
    })
    
    metrics = compute_metrics(df)
    assert metrics.n_rows == 1
    assert metrics.duration_hours == 0.0  # Single timestamp = 0 duration


def test_validate_and_clean_with_string_numbers():
    """Test that string numbers are converted to numeric."""
    df = pd.DataFrame({
        "timestamp": ["2025-01-01 00:00:00", "2025-01-01 00:01:00"],
        "heart_rate": ["70", "72"],  # String numbers
        "steps": ["0", "1"]
    })
    
    result = validate_and_clean(df)
    assert pd.api.types.is_numeric_dtype(result["heart_rate"])
    assert pd.api.types.is_numeric_dtype(result["steps"])


# =========== Test data ingestion helpers ===========

def test_read_tabular_big_csv():
    """Test reading CSV files."""
    csv_content = "timestamp,heart_rate\n2025-01-01 00:00:00,70\n2025-01-01 00:01:00,72"
    csv_bytes = BytesIO(csv_content.encode())
    
    result = wearable_sensor._read_tabular_big(csv_bytes, ".csv")
    assert len(result) == 2
    assert "timestamp" in result.columns


def test_read_tabular_big_empty_csv():
    """Test reading empty CSV."""
    csv_content = "timestamp,heart_rate\n"
    csv_bytes = BytesIO(csv_content.encode())
    
    result = wearable_sensor._read_tabular_big(csv_bytes, ".csv")
    assert len(result) == 0


# =========== Integration Tests ===========

def test_full_pipeline_tidy_csv():
    """Test full pipeline with tidy CSV."""
    df = create_sample_tidy_data()
    clean = validate_and_clean(df)
    metrics = compute_metrics(clean)
    
    assert metrics.n_rows == 100
    assert len(metrics.sensors_present) > 0
    assert metrics.duration_hours > 0


def test_full_pipeline_apple_health_format():
    """Test full pipeline with Apple Health format."""
    df_raw = pd.DataFrame({
        "@type": [
            "HKQuantityTypeIdentifierHeartRate",
            "HKQuantityTypeIdentifierHeartRate",
            "HKQuantityTypeIdentifierStepCount",
        ] * 10,
        "@creationDate": [
            f"2025-01-01 00:{i:02d}:00" for i in range(30)
        ],
        "@value": [70 + i for i in range(30)]
    })
    
    tidy = records_to_minute_tidy(df_raw)
    metrics = compute_metrics(tidy)
    
    assert metrics.n_rows > 0
    assert "heart_rate" in metrics.sensors_present or "steps" in metrics.sensors_present


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

