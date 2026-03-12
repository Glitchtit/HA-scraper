# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.2] - 2026-03-12

### Fixed

- Add-on: resolved `s6-overlay-suexec: fatal: can only run as pid 1` error by adding `init: false` to `config.yaml` (prevents Docker `--init` from displacing s6-overlay as PID 1) and migrating service definitions from the deprecated `/etc/services.d/` (s6-overlay v2) to `/etc/s6-overlay/s6-rc.d/` (s6-overlay v3).

## [1.1.1] - 2026-03-12

### Fixed

- Add-on: removed legacy `CMD ["/run.sh"]` in favour of proper s6-overlay service directories.

### Added

- Add-on: enabled ingress with `ingress: true`, `ingress_port`, `panel_icon`, and `panel_title` in `config.yaml` so the "Show in sidebar" toggle is available.
- Add-on: added a minimal ingress web server (`ingress_server.py`) to serve a status page when the sidebar entry is opened.

## [1.1.0] - 2026-03-12

### Added

- Add-on config (`config.yaml`): added missing `location_id`, `quantity_unit_id`, `flaresolverr_url`, `gemini_api_key`, and `gemini_model` options and schema entries.
- `.env.example`: added `GROCY_LOCATION_ID`, `GROCY_QUANTITY_UNIT_ID`, `GEMINI_API`, and `GEMINI_MODEL` entries.

## [1.0.0] - 2025-01-01

### Added

- Initial release.
- K-Ruoka GraphQL scraper with category browsing.
- K-Ruoka REST API fallback with Cloudflare bypass support.
- Grocy REST API client for product creation and management.
- Home Assistant add-on packaging.
- Home Assistant custom integration with frontend panel.
- Barcode Buddy integration for automated product discovery.
