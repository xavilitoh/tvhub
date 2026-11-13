# Changelog

All notable changes to TVHub. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/). The text of each version is what its GitHub Release shows.

Write new entries under **Unreleased**. To release: rename that heading to `## [X.Y.Z] - YYYY-MM-DD`, bump the version
in `custom_components/tvhub/manifest.json` and `tvhub_addon/config.yaml`, and push the tag `vX.Y.Z`.

## [Unreleased]

## [0.7.0] - 2026-11-12

### Added
- **Home Assistant add-on** (HA OS / Supervised): add `https://github.com/xavilitoh/tvhub` as a repository and TVHub opens from the sidebar through Ingress. Home Assistant signs you in, so there is no TVHub login there; direct access on port 8080 keeps the normal login.
- `TVHUB_AUTH=ingress` mode: the login is skipped only for connections from the Supervisor (`172.30.32.2`) carrying `X-Ingress-Path`, and only when the Supervisor also provided `SUPERVISOR_TOKEN`. Browser writes are checked against the Home Assistant origin.
- Release notes are now taken from this file.
- `docs/demo-src`: scripts that regenerate the README demo against simulated TVs.

### Changed
- The UI works under a path prefix (Ingress), including the WebSockets, app icons and screenshot download.
- Security headers allow framing by the same origin only in Ingress mode; the standalone image keeps `X-Frame-Options: DENY`.
- New README demo GIF showing the current interface.

## [0.6.1] - 2026-11-03

### Changed
- More robust Home Assistant remote card.
- Warning about VIDAA keys that the TV ignores.
- Agent folders are ignored by git.

## [0.6.0] - 2026-11-02

### Added
- CI creates the GitHub Release when a tag is pushed (HACS only reads Releases, not bare tags).

## [0.5.0] - 2026-11-02

### Added
- Roku, Samsung Tizen and Sony Bravia drivers (experimental: tested against simulators only).
- Settings in the top bar and on each wall tile; add and search TVs from the wall.
- Android TV opens apps with `am start`.

### Changed
- Apple-style design with a slide-out menu; menu and GitHub button always visible.

## [0.4.0] - 2026-11-02

### Added
- **Security**: user accounts (Argon2id), server-side sessions, CSRF protection, brute-force lockouts, API tokens for integrations and an audit log.
- Languages: English, Español, Português, Français, Deutsch.
- The wall of TVs is the home page.

## [0.3.0] - 2026-11-01

### Added
- Wall view: all TVs at once, with a screenshot for each Android TV.
- Docker image published to GHCR (amd64 and arm64).
- Demo animation in the README.

## [0.2.0] - 2026-11-01

### Added
- Remote card for Home Assistant dashboards and the `tvhub.send_key` service.

## [0.1.1] - 2026-11-01

### Changed
- Brand icons.

## [0.1.0] - 2026-11-01

### Added
- First release: control Android TV / Fire TV (live screen over ADB), LG webOS and Hisense VIDAA from the browser, with a Home Assistant bridge over MQTT.

[Unreleased]: https://github.com/xavilitoh/tvhub/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/xavilitoh/tvhub/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/xavilitoh/tvhub/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/xavilitoh/tvhub/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/xavilitoh/tvhub/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/xavilitoh/tvhub/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/xavilitoh/tvhub/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/xavilitoh/tvhub/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/xavilitoh/tvhub/compare/v0.1.0...v0.1.1
