# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

Parse new fields from G-code file:
- filament_names parsed from `; filament_settings_id`
- total_filament_changes parsed from `; total filament change`
- per slot filament_cost parsed from `; filament_cost`

### Fixed

### Changed

### Removed

### Deprecated

### Security


## [1.0.0] - 2026-03-16

### Initial Release

- HTTP reverse proxy that intercepts `PUT /upload` and captures G-code files.
- Support for chunked uploads (Content-Range) and single-shot uploads.
- MQTT and MJPEG camera TCP pass-through for slicer Device page compatibility.
- Configurable retention (auto-delete files older than N days).
- Configurable timezone for file timestamps and date directories.
- Docker support with non-root container for port 80.
- Environment-based configuration via `.env`.
- JSON-only storage mode (default): the proxy parses G-code metadata and stores only a
  lightweight JSON sidecar file. Full `.gcode` files are discarded after parsing unless
  `STORE_GCODE=true` is set. This keeps disk usage minimal on resource-constrained
  devices like Raspberry Pi.
- REST API on the same HTTP port (80) for querying captured metadata:
  - `GET /api/filament?filename=...` — look up by original filename.
  - `GET /api/filament/latest` — most recently captured metadata.
  - `GET /api/health` — connectivity check.
- `STORE_GCODE` environment variable to opt in to archiving full `.gcode` files
  alongside JSON metadata.
- MQTT-over-WebSocket TCP pass-through on port 9001 (`MQTT_WS_PORT`). The CC2
  device view's bundled JavaScript creates its own MQTT client via WebSocket on
  port 9001, separate from the C++ library's MQTT-over-TCP on port 1883. Without
  this relay, the slicer's full Device page (controls, camera, file list,
  temperatures) showed "Offline" through the proxy while the printer list worked
  fine.
- Filename falls back to the HTTP `X-File-Name` upload header when the G-code
  content alone cannot determine it.