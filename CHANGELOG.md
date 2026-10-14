# Changelog

All notable changes to Ohmie for Home Assistant. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). See [RELEASING.md](RELEASING.md).

## [Unreleased]

## [0.1.0] - 2026-10-15

First public release. Ohmie membership is invite-only.

### Added

- Setup wizard: sign in or sign up, create your Ohmie home or connect to one, devices found in
  Home Assistant with typical specs, a style preset, your power plan, and "suggest" or "drive" per
  device.
- Per device: Action, Setpoint (with the whole plan), Reason, Mode, Run now, Command, Next charge,
  Next export and Reserve for a battery, an Ohmie switch for on/off devices, and your targets.
- Per site: Pause Ohmie, Price now, Next cheap window, Cheap now, Connected, a plan calendar and the
  weekly "left on the table" report.
- Load balancing at the edge: per phase and per circuit, one action at a time, slow down before
  switch off, voltage-sag aware, with the reason for each action in the logbook.
- Telemetry every minute, sent every 5 minutes, kept while Home Assistant is offline.
- Live plans over NATS, with a REST fallback.
- A repair that asks you to confirm a suspected supply cut.
- Six blueprints that pass Ohmie's entities to your own devices, under your own conditions.

[Unreleased]: https://github.com/ohmie-energy/ha-ohmie/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ohmie-energy/ha-ohmie/releases/tag/v0.1.0
