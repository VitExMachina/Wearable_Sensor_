# tests/test_ingest.py
import io
import pandas as pd

import wearable_sensor_draft_code_11_03 as mod

def test_read_xml_streaming_minimal():
    # Minimal Apple Health XML with one Record
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData>
  <Record type="HKQuantityTypeIdentifierHeartRate" creationDate="2025-01-01 00:00:01" unit="count/min" value="60"/>
</HealthData>"""
    df = mod._read_xml_streaming(io.BytesIO(xml))
    assert set(df.columns) == {"@type", "@creationDate", "@unit", "@value"}
    assert df.iloc[0]["@type"] == "HKQuantityTypeIdentifierHeartRate"
    assert df.iloc[0]["@value"] == "60"

def test_read_tabular_big_csv_chunks(tmp_path):
    csv_path = tmp_path / "big.csv"
    rows = 300_000
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=rows, freq="s"),
        "heart_rate": 60
    })
    df.to_csv(csv_path, index=False)
    out = mod._read_tabular_big(str(csv_path), ".csv")
    assert len(out) == rows
    assert list(out.columns) == ["timestamp", "heart_rate"]
