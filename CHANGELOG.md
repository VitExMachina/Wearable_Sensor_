# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **2025-11-03**: Increased maximum file upload limit from 200 MB to 1000 MB in `wearable_sensor_draft_code_11_03.py`
  - Updated upload limit constant to 1000 MB
  - Added file size validation with warning message for files exceeding the limit
  - Updated UI caption to reflect new maximum upload limit
  - Renamed file from `wearable_sensor_draft_code_10_29.py` to `wearable_sensor_draft_code_11_03.py` to reflect current date
  - Updated `.devcontainer/devcontainer.json` to reference the new filename in `openFiles` and `postAttachCommand` for codespace configuration

