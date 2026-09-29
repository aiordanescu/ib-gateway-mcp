# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-09-28

### Added

- `get_health` reports the gateway's login phase as `login_state` (restarting, logging in, waiting for 2FA approval, pausing after failed logins until `retry_at`, rejected, idle, logged in, or unknown), with the login attempts and 2FA challenges since the last successful login, when `IB_GATEWAY_SETTINGS_DIR` names the gateway's settings directory, mounted read-only. It reads only IB Gateway's `launcher.log` and its rotated copies, and returns the parsed state, never log text. `examples/docker-compose.yml` mounts it, and the README's "Gateway login state" section covers ib-gateway-docker's single and both modes, bind mounts and SELinux.
- `get_health` reports `last_disconnect_at`, when the connection last went down, kept after it comes back.
- An [Agent Skills](https://agentskills.io) guide for the agent that uses the server, `skills/ib-gateway-mcp`: the order workflows, which errors are final, and what to report while the gateway is down. The README gives install commands pinned to the release.

### Changed

- Outage hints no longer claim a cause the API doesn't give. For a refused connection, `get_health` and `not_connected` errors list what it can mean (the gateway is not running, the port is wrong, or it is not logged in to IBKR) and say the API doesn't tell which; without `login_state`, the hint asks to report since when the gateway has been down rather than guess a cause.
- The server instructions start with a line that stands alone, cover outages, the market data type (kept across gateway reconnects) and checking an order's status before retrying a failed submit or cancel, and stay within 2,048 characters (a long list of order limits is cut short).
- Every tool description stays within 2,048 characters, where Claude Code cuts them (`preview_order`'s lost its Errors paragraph); a test checks all of them.

### Fixed

- `examples/docker-compose.yml`: the gateway couldn't write its settings to the new `tws_settings` volume, which Docker creates owned by root while ib-gateway-docker runs as uid 1000. A one-shot `tws-settings-init` service now gives the volume to the gateway's user before it starts.

## [0.1.1] - 2026-09-25

### Added

- Published on PyPI as `ib-gateway-mcp`: `uvx ib-gateway-mcp` runs the server and `pip install ib-gateway-mcp` installs the library, with no clone. Releases now go to PyPI and ghcr.io together.

## [0.1.0] - 2026-09-25

The first release.

### Added

- MCP server (stdio, and streamable HTTP with bearer-token auth, DNS-rebinding protection on loopback hosts, and unauthenticated `/healthz` and `/readyz` probes) and async Python library for the TWS API through `ib_async` 2.1.0: 70 tools in 12 toolsets (ops, contracts, market data, history, scanners, news, fundamentals, account, options, orders, advisor, admin), selected by the `readonly`, `trading` and `full` profiles.
- Connection manager with background reconnect, liveness probes and a plain-language health report (`get_health`, optionally with a live round trip).
- Streaming subscriptions with caps, deduplication and idle reaping, read through `get_subscription_data` (or `gw.market_data.stream()`, and the typed `snapshot()` and `watch()`, in the library).
- Orders: previews with IBKR's what-if and single-use, server-side tokens; brackets, OCA groups, combos, IBKR algos; modify, cancel, cancel-all and option exercise.
- Safety rails: read-only default, live-trading switch, human confirmation of live actions through MCP elicitation (text written by the model is quoted), order limits (notional, quantity, symbols, security types, currencies), order and preview rate limits, a circuit breaker that survives restarts, opt-in switches for IBKR's global cancel and billed regulatory snapshots, a JSONL audit log (checked at start-up), and a start-up summary of the safety configuration.
- Configuration from `IB_*` and `IBKR_MCP_*` environment variables (a blank value counts as unset), with `_FILE` variants for secrets.
- Container image `ghcr.io/aiordanescu/ib-gateway-mcp` for amd64 and arm64 (tags `0.1.0`, `0.1`, `latest`, `stable`) with a build provenance attestation and an SBOM: a non-root numeric user and a healthcheck. `examples/docker-compose.yml` runs it next to ib-gateway-docker.
- Reference docs: `docs/tools.md` (generated from the registry, with the library method behind each tool and the fields of every nested input type) and `docs/coverage.md`.

[Unreleased]: https://github.com/aiordanescu/ib-gateway-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/aiordanescu/ib-gateway-mcp/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/aiordanescu/ib-gateway-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/aiordanescu/ib-gateway-mcp/releases/tag/v0.1.0
