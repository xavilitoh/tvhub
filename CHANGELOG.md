# Changelog

All notable changes to TVHub. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/). The text of each version is what its GitHub Release shows.

Write new entries under **Unreleased**. To release: rename that heading to `## [X.Y.Z] - YYYY-MM-DD`, bump the version
in `custom_components/tvhub/manifest.json` and `tvhub_addon/config.yaml`, and push the tag `vX.Y.Z`.

## [Unreleased]

## [0.8.3] - 2026-11-13

### Changed
- The project moved from the GitHub account `xavilitoh` to `wxberroa`. The container image is now `ghcr.io/wxberroa/tvhub` (the old name does not redirect), and the repository, add-on, HACS and website links point to the new account. See the README for how to update.

### Changed
- New logo and icons ("the wall": four screens, one active) in coral, with a light-background variant; favicon and touch icon for the landing.
- Accent colour is now coral `#f5654a` (was blue) in the web app and the landing page; new demo GIF.

### Added
- Landing page (`docs/index.html`, English / Español) for GitHub Pages.

## [0.8.2] - 2026-11-13

### Fixed
- Add-on: the integration could not connect through the direct port ("requires a valid API token") while no TVHub administrator existed, which is always the case when you only use Ingress. A valid API token now works even before the first administrator is created; everything else still waits for it.

## [0.8.1] - 2026-11-13

### Fixed
- The integration now offers **TVHub discovered** even when another TVHub server is already configured (e.g. one in a separate Docker); it is only skipped when that same server is already set up.

## [0.8.0] - 2026-11-13

### Added
- Add-on: announces itself to Home Assistant (Supervisor discovery) with an API token, so the TVHub integration shows **TVHub discovered** and needs no IP or token typed by hand. The integration accepts that discovery (`async_step_hassio`).
- Add-on option **install_integration** (off by default): copies the integration, bundled in the image, to `/config/custom_components/tvhub`; never overwrites a HACS or manual install.

## [0.7.3] - 2026-11-12

### Added
- Add-on: when the Mosquitto broker add-on is installed, TVHub reads its login from the Supervisor on the first start and connects to MQTT by itself. A new **Usar el broker de Home Assistant** button in the Home Assistant dialog does the same later. An external broker is still filled in by hand; settings you saved always win.

## [0.7.2] - 2026-11-12

### Fixed
- Inside Home Assistant (Ingress) the sign-in and status calls ignored the Ingress path prefix, so the page showed the login form with "No se pudo contactar con el servidor" instead of opening. They now use the prefix like every other request.

## [0.7.1] - 2026-11-12

### Added
- CI publishes `ghcr.io/wxberroa/tvhub:dev` on every push to `dev` (never `latest`), and a hidden **TVHub (dev)** add-on (Advanced mode) runs it next to the stable one.
- Add-on option **port** (Configuration tab) to choose the port for direct access instead of the fixed 8080. Ingress now uses its own fixed port (8099), so it keeps working whatever you choose.

### Changed
- The log now states whether Ingress mode is on at startup and, once, why an Ingress request was not trusted (peer address).
- README and add-on docs: one-click button to add the add-on repository to Home Assistant.

## [0.7.0] - 2026-11-12

### Added
- **Home Assistant add-on** (HA OS / Supervised): add `https://github.com/wxberroa/tvhub` as a repository and TVHub opens from the sidebar through Ingress. Home Assistant signs you in, so there is no TVHub login there; direct access on port 8080 keeps the normal login.
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

[Unreleased]: https://github.com/wxberroa/tvhub/compare/v0.8.3...HEAD
[0.8.3]: https://github.com/wxberroa/tvhub/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/wxberroa/tvhub/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/wxberroa/tvhub/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/wxberroa/tvhub/compare/v0.7.3...v0.8.0
[0.7.3]: https://github.com/wxberroa/tvhub/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/wxberroa/tvhub/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/wxberroa/tvhub/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/wxberroa/tvhub/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/wxberroa/tvhub/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/wxberroa/tvhub/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/wxberroa/tvhub/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/wxberroa/tvhub/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/wxberroa/tvhub/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/wxberroa/tvhub/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/wxberroa/tvhub/compare/v0.1.0...v0.1.1
