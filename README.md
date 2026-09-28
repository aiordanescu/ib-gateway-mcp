# ib-gateway-mcp

[![CI](https://github.com/aiordanescu/ib-gateway-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/aiordanescu/ib-gateway-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/LICENSE)
[![Python 3.12 | 3.13 | 3.14](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/pyproject.toml)

An [MCP](https://modelcontextprotocol.io) server and Python library for the **Interactive Brokers TWS API**, built as a companion to [`gnzsnz/ib-gateway-docker`](https://github.com/gnzsnz/ib-gateway-docker).

Add one service next to your IB Gateway container, and any MCP client (Claude, Cursor, and others) gets what the gateway offers: contracts, market data, history, scanners, news, fundamentals, account and P&L, and orders. Orders sit behind safety rails.

> **Status:** early development. Releases are published on [PyPI](https://pypi.org/project/ib-gateway-mcp/) as `ib-gateway-mcp` and as the container image `ghcr.io/aiordanescu/ib-gateway-mcp`. Beyond the offline test suite, it has been tested against a real IB Gateway (10.45): the order suite on a paper login, and the read-only suite on both paper and live accounts.

## Why

As of September 2026, the MCP options for Interactive Brokers are either hosted services that stop short of placing orders, or community servers that each cover a slice of the TWS API, many of them read-only.

`ib-gateway-mcp` covers the TWS API broadly (70 tools; [coverage](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/docs/coverage.md) maps every API request), runs on your own infrastructure next to your own gateway, and guards agent trading with safety rails that are on by default.

## Features

- **TWS API coverage,** through [`ib_async`](https://github.com/ib-api-reloaded/ib_async) 2.1.0:
  - contracts: symbol search, details, qualification, option chains, market rules
  - market data: snapshots, and streaming quotes (with generic ticks such as option statistics, shortability, ETF NAV), market depth, tick-by-tick data, 5-second and live-updating bars
  - historical bars and ticks, head timestamps, histograms, trading schedules
  - scanners, news (providers, headlines, articles, bulletins), Refinitiv fundamentals, Wall Street Horizon events
  - account summary and values, positions, portfolio, P&L, executions, open and completed orders
  - IBKR's option calculators and chain quotes with greeks
  - orders: MKT, LMT, STP, STP LMT, TRAIL, TRAIL LIMIT, REL, MIT, LIT, MOC, LOC, MIDPRICE, PEG MID and PEG MKT; brackets, OCA groups, combos (BAG); the Adaptive, TWAP, VWAP, Arrival Price, Percentage of Volume and Close Price algos; good-after and good-till times, all-or-none, hidden and iceberg orders; modify, cancel, cancel-all and option exercise
  - financial advisor (FA) logins: FA group configuration (read and replace), model-code orders, soft dollar tiers, family codes. Allocating one order across an FA group isn't supported yet.
  - not yet: order conditions, cash-quantity orders and the rarer order types and algos ([list](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/docs/coverage.md#not-supported-yet-orders))
- **Toolsets:** register only what you need. Three profiles: `readonly` (default), `trading`, `full`. The tool list is large (the `readonly` profile's 50 tools carry about 175,000 characters of descriptions and input schemas), so a narrower `IBKR_MCP_TOOLSETS` leaves more of the model's context for the task.
- **Safety rails for orders** (see [Safety](#safety)): preview tokens, limits, rate limits, circuit breaker, audit log, and human confirmation for live orders.
- **Operational awareness:** a health tool that reports the connection plainly (refused, lost or restored, cut off from IBKR, API in read-only mode) and when it last went down. The API doesn't say why a gateway refuses connections; with read access to the gateway's settings directory, the tool also reports its login phase, such as waiting for 2FA approval or a rejected login ([Gateway login state](#gateway-login-state-optional)).
- **Library plus server:** an MCP server over stdio or streamable HTTP (bearer-token auth), and an importable async Python library.

## Quick start with Docker

[`examples/docker-compose.yml`](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/examples/docker-compose.yml) runs ib-gateway-docker on a paper login next to this server. The gateway's own settings (2FA, trading mode, settings volume, VNC) are documented in [ib-gateway-docker's README](https://github.com/gnzsnz/ib-gateway-docker).

```sh
mkdir ib-gateway-mcp && cd ib-gateway-mcp
curl -fsSLO https://raw.githubusercontent.com/aiordanescu/ib-gateway-mcp/main/examples/docker-compose.yml
umask 077                                        # secrets readable by their owner only
printf 'TWS_USERID=...\n' > .env
printf '%s' '<paper password>' > tws_password.txt
openssl rand -hex 32 > mcp_auth_token.txt
sudo chown 1000 tws_password.txt                 # Linux only: the gateway image's user
sudo chown 10001 mcp_auth_token.txt              # Linux only: this image's user
docker compose up -d
```

Compose mounts secret files with their owner and mode from the host, hence the `chown` on Linux (Docker Desktop needs none). Run it on a host you do not share. The example publishes no gateway API port: the server reaches the gateway on the compose network, and a published API port would let any local process place orders around the safety rails. It also mounts the gateway's settings volume read-only into the server, so `get_health` reports the gateway's login phase ([Gateway login state](#gateway-login-state-optional)).

Point an MCP client at `http://127.0.0.1:8000/mcp` with the header `Authorization: Bearer <contents of mcp_auth_token.txt>`. For Claude Code:

```sh
claude mcp add --transport http ib-gateway http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $(cat mcp_auth_token.txt)"
```

If ib-gateway-docker already runs in its own stack, run the server as a second stack, so updating one never recreates the other: copy the `ib-gateway-mcp` service with its secret and volume, set `IB_HOST` to the gateway's service name, and attach the service to the gateway stack's network (declared under `networks:` with `external: true`). Inside that network the gateway listens on 4004 (paper) and 4003 (live).

The image is published for amd64 and arm64 as `ghcr.io/aiordanescu/ib-gateway-mcp`, tagged with each version (`0.1.1`), its minor series (`0.1`), and `latest` and `stable` for the latest release. Each image carries an SBOM and a build provenance attestation (`gh attestation verify oci://ghcr.io/aiordanescu/ib-gateway-mcp:0.1.1 --owner aiordanescu`). `docker build -t ib-gateway-mcp:local .` in a clone builds it yourself. The image runs as a non-root user (uid and gid 10001), serves streamable HTTP on port 8000 (`/mcp`), and has a Docker healthcheck on `/healthz`. The compose example runs it with a read-only root filesystem, no capabilities, and the audit log on a volume at `/audit`. `/healthz` (liveness) and `/readyz` (200 only while the gateway connection is up) are unauthenticated and return only `{"state", "ready"}`.

## Running without Docker

Needs Python 3.12 or newer. With [uv](https://docs.astral.sh/uv/), `uvx` runs the latest release from PyPI with nothing to install first (`pip install ib-gateway-mcp` works too):

```sh
IB_HOST=127.0.0.1 IB_PORT=4002 uvx ib-gateway-mcp                # stdio

openssl rand -hex 32 > mcp_auth_token.txt                        # HTTP on 127.0.0.1:8000
IBKR_MCP_AUTH_TOKEN_FILE=mcp_auth_token.txt uvx ib-gateway-mcp --transport http --port 8000
```

HTTP always needs a bearer token of at least 32 characters. For a quick test on the loopback address only, `IBKR_MCP_ALLOW_NO_AUTH=true` serves it without one.

A stdio entry for an MCP client:

```json
{
  "mcpServers": {
    "ib-gateway": {
      "command": "uvx",
      "args": ["ib-gateway-mcp"],
      "env": { "IB_HOST": "127.0.0.1", "IB_PORT": "4002", "IBKR_MCP_PROFILE": "readonly" }
    }
  }
}
```

Pin a version with `ib-gateway-mcp==0.1.1` in place of `ib-gateway-mcp`. To run a clone instead (for development, or an unreleased commit), use `uv sync` in it, then `uv run ib-gateway-mcp` with the same variables and options, or `"command": "uv"` with `"args": ["--directory", "/path/to/ib-gateway-mcp", "run", "ib-gateway-mcp"]` in the client entry.

The server starts even when the gateway is down and keeps reconnecting; tools then fail with `not_connected`, and `get_health` reports what is known about why.

## Gateway login state (optional)

When the gateway refuses a connection, the TWS API doesn't say why: a gateway that is stopped, starting, waiting for a 2FA approval or stuck at its login looks the same. IB Gateway does log its login steps, to `launcher.log` in its settings directory. With `IB_GATEWAY_SETTINGS_DIR` set to that directory, `get_health` adds `login_state`: the phase (`restarting`, `logging_in`, `awaiting_2fa`, `throttled`, `login_rejected`, `login_idle`, `logged_in` or `unknown`) and since when, when the gateway's pause after repeated failed logins ends (`retry_at`), and the login attempts and 2FA challenges since the last successful login. While the gateway refuses the connection, the health hint then words that phase. The gateway stops writing to `launcher.log` once a login succeeds, so `logged_in` only means that the last login succeeded. Without the setting, `get_health` still reports `last_disconnect_at`, when the connection last went down. It is null if the server's first connection attempt succeeded and the connection hasn't dropped since; if the gateway was already down when the server started, it is the server's start time, also after the connection comes back, and the outage may be older. [docs/tools.md](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/docs/tools.md#get_health) describes the fields.

After a 2FA challenge nobody answered, or once the gateway's pause after failed logins ends, the gateway logs in again by itself only with ib-gateway-docker's `RELOGIN_AFTER_TWOFA_TIMEOUT=yes` (its default is `no`), which also means a new 2FA push after each unanswered one. Otherwise `login_state` turns `login_idle`, and the gateway container needs a restart.

The gateway writes `launcher.log` to a volume only when ib-gateway-docker's `TWS_SETTINGS_PATH` is set: its default is empty, which leaves the file inside the gateway container. Mount the directory, not the file (the gateway rotates the log by renaming it), and mount it read-only. [`examples/docker-compose.yml`](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/examples/docker-compose.yml) does this for a paper login:

```yaml
services:
  tws-settings-init:            # gives the new volume to the gateway's user (uid 1000)
    image: ghcr.io/gnzsnz/ib-gateway:stable
    user: "0:0"
    entrypoint: ["chown", "1000:1000", "/home/ibgateway/tws_settings"]
    volumes:
      - tws_settings:/home/ibgateway/tws_settings
    restart: "no"
  ib-gateway:
    depends_on:
      tws-settings-init:
        condition: service_completed_successfully
    environment:
      TWS_SETTINGS_PATH: /home/ibgateway/tws_settings
    volumes:
      - tws_settings:/home/ibgateway/tws_settings
  ib-gateway-mcp:
    environment:
      IB_GATEWAY_SETTINGS_DIR: /gateway/tws_settings
    volumes:
      - type: volume
        source: tws_settings
        target: /gateway/tws_settings    # not a path in this server's image
        read_only: true
        volume:
          nocopy: true
volumes:
  tws_settings:
```

Docker creates a new named volume owned by root when the image has no such path, and ib-gateway-docker runs as uid 1000, so without `tws-settings-init` the gateway can't write its settings. The long syntax is what takes `nocopy`: together with a target that doesn't exist in this server's image, it keeps Docker from copying anything from the image into the gateway's volume.

With `TRADING_MODE=both`, ib-gateway-docker appends `_live` and `_paper` to `TWS_SETTINGS_PATH` and never uses the path itself, so put the volume on the parent directory and point the server at the directory of the login behind its `IB_PORT`:

```yaml
services:
  settings-init:                # as above, for the parent directory
    image: ghcr.io/gnzsnz/ib-gateway:stable
    user: "0:0"
    entrypoint: ["chown", "1000:1000", "/home/ibgateway/settings"]
    volumes:
      - gateway_settings:/home/ibgateway/settings
    restart: "no"
  ib-gateway:
    depends_on:
      settings-init:
        condition: service_completed_successfully
    environment:
      TRADING_MODE: both
      TWS_SETTINGS_PATH: /home/ibgateway/settings/tws    # the gateway uses tws_live and tws_paper
    volumes:
      - gateway_settings:/home/ibgateway/settings
  ib-gateway-mcp:
    environment:
      IB_PORT: "4004"                                    # paper; 4003 is live
      IB_GATEWAY_SETTINGS_DIR: /gateway/tws_paper        # /gateway/tws_live for 4003
    volumes:
      - type: volume
        source: gateway_settings
        target: /gateway
        read_only: true
        volume:
          nocopy: true
volumes:
  gateway_settings:
```

With bind mounts, mount the same host directory into both containers: `./tws_settings:/home/ibgateway/tws_settings` on the gateway and `./tws_settings:/gateway/tws_settings:ro` on the server. Create the directory before the first start and, on Linux, give it to the gateway's user (`sudo chown 1000:1000 tws_settings`): Docker creates a missing one owned by root. Under SELinux in enforcing mode (Fedora, RHEL), add `:z` to both mounts (`:ro,z` on the server's); never `:Z`, which makes the directory private to one container.

A server run on the host (stdio, `uvx`) reads the directory itself, so the gateway needs a bind mount on that same host, and `IB_GATEWAY_SETTINGS_DIR` names the host path. Docker Desktop keeps named volumes inside its virtual machine, out of the host's reach, and a gateway on another machine (reached over an SSH tunnel, for example) can't be read at all. The server only reads the directory, and warns at start-up when it could write to it; on the host, run the server as a user that can read the directory but not write to it.

This works with IB Gateway (ib-gateway-docker's gateway image) and was tested against the logs of IB Gateway 10.45 and 10.50; TWS is untested. The log format is IBKR's own and undocumented, so a gateway update may change it; whatever the server doesn't recognize reads as `unknown`.

**Privacy.** Read access to the settings directory exposes more than the login phase. `launcher.log` holds, among other things, the host's local and public IP addresses, its MAC address, IBKR session ids, masked session-token prefixes and token hashes, a log upload key, and ad-request ids that base64-encode the login's user identifier. The gateway masks the username in its login lines, and no account ids were found in the log. During the daily auto-restart the directory briefly holds a file with the session token the restart reuses. The server opens only `launcher.log` and its rotated copies (`launcher.YYYYMMDD.log`), and returns only the parsed phase, times and counts, never log text. Mount the directory read-only and into this server only, and never mount `/home/ibgateway` or IBC's directory, which can hold the password.

## Configuration

Everything is set with environment variables. `IB_*` variables describe the gateway connection, `IBKR_MCP_*` the server's behaviour; other names (such as a bare `PROFILE`) are ignored. Secrets also take a `_FILE` variant (Docker secrets). A blank value (`VAR=`) counts as unset, so the default applies.

| Variable | Default | Meaning |
|---|---|---|
| `IB_HOST` | `127.0.0.1` | Gateway host (`ib-gateway` in the image). |
| `IB_PORT` | `4004` | Gateway API port. In ib-gateway-docker's network: 4004 paper, 4003 live. |
| `IB_CLIENT_ID` | `80` | API client id; keep it stable and unique on the login. `0` is refused. |
| `IB_ACCOUNT` | | Default account. Without it, a login that manages several accounts has no default, and calls must name one. |
| `IB_CONNECT_TIMEOUT` | `10` | Seconds per connection attempt. |
| `IB_REQUEST_TIMEOUT` | `30` | Seconds per request. |
| `IB_GATEWAY_SETTINGS_DIR` | | The gateway's settings directory, where it writes `launcher.log` (ib-gateway-docker's `TWS_SETTINGS_PATH`; with `TRADING_MODE=both` the `_live` or `_paper` one for `IB_PORT`), mounted read-only. `get_health` then reports the gateway's login phase (`login_state`). IB Gateway only. |
| `IBKR_MCP_ACCOUNTS` | | Comma-separated accounts allowed besides the default; empty means only the default. |
| `IBKR_MCP_PROFILE` | `readonly` | `readonly`, `trading` or `full`. |
| `IBKR_MCP_TOOLSETS` | | Comma-separated toolsets; overrides the profile. |
| `IBKR_MCP_ALLOW_LIVE` | `false` | Allow order tools on live (non-paper) accounts. |
| `IBKR_MCP_LIVE_CONFIRM` | `true` | Ask a human (elicitation) before each live order, cancel or FA change. |
| `IBKR_MCP_TOKEN_TTL` | `120` | Seconds a preview token stays valid. |
| `IBKR_MCP_MAX_NOTIONAL` | | Largest order notional, in the order's own currency (no FX conversion). While set, bond and event-contract orders are refused (their notional is not quantity x price). |
| `IBKR_MCP_MAX_QUANTITY` | | Largest order quantity. |
| `IBKR_MCP_ALLOWED_SYMBOLS` | | Comma-separated symbols orders may use; empty means any. |
| `IBKR_MCP_ALLOWED_SEC_TYPES` | | Comma-separated security types (`STK,OPT`...); empty means any. |
| `IBKR_MCP_ALLOWED_CURRENCIES` | | Comma-separated order currencies (`USD`...); empty means any. Set it to make `IBKR_MCP_MAX_NOTIONAL` a cap in one currency. |
| `IBKR_MCP_MAX_ORDERS_PER_MINUTE` | `10` | Order rate limit. |
| `IBKR_MCP_MAX_PREVIEWS_PER_MINUTE` | `60` | Preview rate limit (each preview sends what-if checks to IBKR). |
| `IBKR_MCP_CIRCUIT_BREAKER_REJECTS` | `5` | Consecutive IBKR rejections that halt order submission. |
| `IBKR_MCP_ALLOW_GLOBAL_CANCEL` | `false` | Allow `preview_cancel_all_orders(scope="global")`, IBKR's cancel of every order on the login. |
| `IBKR_MCP_AUDIT_LOG` | | JSONL audit file; unset logs to the `ib_gateway_mcp.audit` logger only. An open circuit breaker is kept next to it (`audit.breaker.json` for `audit.jsonl`). With write tools on, the server refuses to start if the file cannot be written. |
| `IBKR_MCP_MAX_SUBSCRIPTIONS` | `50` | Open streams allowed. |
| `IBKR_MCP_SUBSCRIPTION_IDLE_TTL` | `900` | Seconds without a read before a stream is cancelled. |
| `IBKR_MCP_MARKET_DATA_TYPE` | `1` | 1 live, 2 frozen, 3 delayed, 4 delayed-frozen. |
| `IBKR_MCP_ALLOW_REGULATORY_SNAPSHOTS` | `false` | Allow `get_quotes(regulatory_snapshot=true)`, which IBKR bills (about USD 0.01 each); each is audited. |
| `IBKR_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (`http` in the image). |
| `IBKR_MCP_HTTP_HOST` | `127.0.0.1` | HTTP listen address (`0.0.0.0` in the image). |
| `IBKR_MCP_HTTP_PORT` | `8000` | HTTP listen port. |
| `IBKR_MCP_AUTH_TOKEN` / `_FILE` | | Bearer token, at least 32 characters. Required for HTTP. |
| `IBKR_MCP_ALLOW_NO_AUTH` | `false` | Allow HTTP without a token, on a loopback address only. |
| `IBKR_MCP_LOG_LEVEL` | `INFO` | Log level (logs go to stderr). The audit logger stays at INFO. |

Command-line flags (`--transport`, `--host`, `--port`, `--profile`, `--toolsets`, `--log-level`) override the environment.

## Profiles and tools

| Profile | Toolsets | Tools |
|---|---|---|
| `readonly` (default) | ops, contracts, market_data, history, scanners, news, fundamentals, account, options | 50 |
| `trading` | readonly + orders | 60 |
| `full` | trading + advisor, admin | 70 |

Every tool, with its parameters: [docs/tools.md](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/docs/tools.md). Streams (`subscribe_*`) return a subscription id; `get_subscription_data` reads it, `unsubscribe` stops it, and streams nobody reads are cancelled after `IBKR_MCP_SUBSCRIPTION_IDLE_TTL`.

## Safety

- **Read-only by default.** The `readonly` profile registers no order, advisor or admin tool. Every write path (orders, cancels, FA changes, admin settings) also passes the **trading gate**: the gateway is connected, a write toolset is enabled, the login's accounts are known and one of them is allowed, no live account is in scope unless `IBKR_MCP_ALLOW_LIVE=true`, and the gateway's API is not read-only. For a deployment that must never trade, turn on the gateway's own Read-Only API setting too (`READ_ONLY_API=yes` in ib-gateway-docker): it's the only guarantee enforced by IBKR's software rather than this server.
- **Paper unless told otherwise.** Paper and live are told apart from the account ids (paper ids start with `D`). Order tools refuse live accounts unless `IBKR_MCP_ALLOW_LIVE=true`.
- **Two-step orders.** A `preview_*` tool runs IBKR's what-if check (margin, commission) and the limits, and returns a token: a single-use, expiring key (192 random bits) to the exact order and account, which stay on the server. `submit_order` takes only the token and checks everything again.
- **A human confirms live actions.** With `IBKR_MCP_LIVE_CONFIRM=true` (the default), submitting, cancelling or changing FA configuration on a live account asks the person at the client through MCP elicitation. The model can't answer for them. The question is built by the server; anything the model wrote into it (a model code, a tier or FA group name, a reason) is quoted and must be one line of plain text.
- **Limits:** notional, quantity, symbols, security types and currencies, order and preview rate limits, and a circuit breaker that halts submission after consecutive rejections until a human resets it; with an audit file it stays open across restarts. `get_health` shows the breaker. A notional check that had to use delayed, frozen or previous-close prices says so in the preview and in the confirmation.
- **Audit log** of every preview, submit, modify, cancel, exercise, FA change, billed snapshot, circuit-breaker reset, admin change (server log level, display group) and refusal at the trading gate (JSONL, no secrets). The server logs its safety configuration at start-up and warns about risky combinations. A failed write to the audit file is logged at ERROR and does not stop the order, so watch the logs, and keep the file on a volume the server's user can write (start-up already fails when it can't).

Caveats:

- **Clients without form elicitation can't confirm live orders**, so live submits, cancels and FA changes are refused there (fail closed). Paper orders, cancels and FA changes never ask.
- **Resetting the circuit breaker always asks a human**, on paper too, through `reset_circuit_breaker` in the admin toolset (the `full` profile). Without elicitation or that toolset: with no audit file, restart the server; with one, stop the server, delete the `.breaker.json` file next to the audit file (`audit.breaker.json` for `audit.jsonl`), and start it again.
- **The rails assume the model reaches the gateway only through this server.** An agent that can open the gateway's API port itself, or restart this server and edit its files, can go around them; keep the API port unpublished and the server's host and volumes out of the agent's reach.
- **Paper logins see market data only with sharing.** Enable market data sharing with the paper account in IBKR's settings, or use delayed data (`IBKR_MCP_MARKET_DATA_TYPE=3`).
- **Client ids matter.** An API client can modify and cancel only the orders it placed itself. `IB_CLIENT_ID` defaults to 80; give every API client on the login (this server, other bots, notebooks) its own stable id. `0` is refused, because orders entered by hand in TWS bind to client 0. `get_open_orders` marks other clients' and manual orders as not `modifiable`, and `get_order_status` reads their status from IBKR each time.
- **`preview_cancel_all_orders(scope="global")`** is IBKR's global cancel: every working order on the login, including other API clients' and manual orders. It's refused unless `IBKR_MCP_ALLOW_GLOBAL_CANCEL=true` and the allowlist covers every managed account.
- Preview tokens live in memory; a restart invalidates them.

## Troubleshooting

- **Orders fail with error 321, or the gateway shows "API client needs write access".** The gateway's API is read-only: `get_health` reports `api_read_only: true` and the trading gate stays closed. Set `READ_ONLY_API=no` in ib-gateway-docker, or untick *Read-Only API* in the gateway's own settings (Configure > Settings > API > Settings, over VNC in ib-gateway-docker). If `READ_ONLY_API` is already `no` and the error persists, the gateway can still hold the old setting in its settings volume: set `READ_ONLY_API=yes`, restart the gateway, then set it back to `no` and restart it again. The server reconnects by itself after a gateway restart, which clears `api_read_only`; after a change in the gateway's settings alone, restart the server. While the API is read-only, IBKR also refuses `get_completed_orders` (error 321, at once), and `get_open_orders` for this server's own orders reads every client's orders and filters them, saying so in `note`.
- **`get_health` reports `login_rejected` after the gateway's daily auto-restart** (its `detail` says IBKR refused the saved session). The auto-restart reuses the saved session instead of logging in; IBKR refused it and asked for the password, which an auto-restart can't enter, so the gateway waits at its login screen and doesn't retry. Approving a 2FA prompt won't help: restart the gateway container, a cold restart that logs in with the password (and a second factor, where the login has one). IBKR also ends saved sessions once a week (Sundays at 1:00 am ET): with ib-gateway-docker's `TWS_COLD_RESTART` set to a time after that, the gateway restarts cold every Sunday then. That covers the weekly case only.
- **Quotes fail with error 10089 although delayed data is selected.** IBKR offers the login no delayed data for that instrument either (IB Gateway 10.45 does this for some instruments on paper logins); historical bars can still work. Subscribe to the exchange's data, or share market data with the paper account.

## Library

The services behind the tools are an async Python library:

```sh
uv add ib-gateway-mcp        # or: pip install ib-gateway-mcp
```

```python
import asyncio

from ib_gateway_mcp import ContractSpec, Gateway, Settings
from ib_gateway_mcp.models import OrderSpec, QuoteStreamData


async def main() -> None:
    # Write access (orders, FA, admin) follows the toolsets, as for the server.
    settings = Settings(ib_port=4002, profile="trading")
    async with Gateway(settings) as gw:
        await gw.wait_connected(timeout=15)
        print(gw.ops.health().state)

        spy = ContractSpec(symbol="SPY")
        quotes = await gw.market_data.quotes([spy])
        print(quotes.quotes[0].last)

        preview = await gw.orders.preview_order(
            OrderSpec(contract=spy, action="BUY", quantity=1, order_type="LMT", limit_price=1.00)
        )
        print(preview.summary, preview.what_if)
        # result = await gw.orders.submit(preview.token)

        sub = await gw.market_data.subscribe_quotes(spy)
        async for data in gw.market_data.watch(sub.subscription_id, QuoteStreamData):
            print(data.quote.last)
            break
        await gw.market_data.unsubscribe(sub.subscription_id)


asyncio.run(main())
```

Services: `gw.ops`, `gw.contracts`, `gw.market_data`, `gw.history`, `gw.scanners`, `gw.news`, `gw.fundamentals`, `gw.account`, `gw.options`, `gw.orders`, `gw.advisor`, `gw.admin`. They take and return the pydantic models in `ib_gateway_mcp.models` and raise the errors in `ib_gateway_mcp.errors` (all derived from `IbGatewayMcpError`). Error messages are written for the MCP tools and name them where they point to a next step; [docs/tools.md](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/docs/tools.md) gives the service method behind each tool. Contract resolution (`qualify`, `qualify_details`, `qualify_many`) is shared by every service; `gw.contracts` has the lookups proper. The library applies the same account scope, trading gate and order rails as the server; `gw.ib` is a raw `ib_async.IB` escape hatch that bypasses them.

## Agent skill (optional)

[`skills/ib-gateway-mcp`](https://github.com/aiordanescu/ib-gateway-mcp/tree/main/skills/ib-gateway-mcp) is an [Agent Skills](https://agentskills.io) guide for the agent that uses this server. The tool descriptions cover each tool; the skill covers the workflows across them: preview, submit and fill checks, brackets, OCA groups and combos, market data types, which errors are final, and what to report while the gateway is down. It uses only the fields of the [specification](https://agentskills.io/specification) and bare tool names, so it works in any agent that loads skills. Install the release that matches your server; the commands below pin v0.2.0.

Any agent, with [`npx skills`](https://github.com/vercel-labs/skills) (it asks which agents to install for) or the GitHub CLI's `gh skill` (a preview command):

```sh
npx skills add aiordanescu/ib-gateway-mcp#v0.2.0 --skill ib-gateway-mcp
gh skill install aiordanescu/ib-gateway-mcp ib-gateway-mcp@v0.2.0 --agent <agent> --scope user
```

Claude Code:

```sh
npx skills add aiordanescu/ib-gateway-mcp#v0.2.0 --skill ib-gateway-mcp -a claude-code -g
```

Codex, inside a session:

```text
$skill-installer install https://github.com/aiordanescu/ib-gateway-mcp/tree/v0.2.0/skills/ib-gateway-mcp
```

Hermes Agent:

```sh
hermes skills install https://raw.githubusercontent.com/aiordanescu/ib-gateway-mcp/v0.2.0/skills/ib-gateway-mcp/SKILL.md
```

Or copy `skills/ib-gateway-mcp/` from the v0.2.0 tag into the agent's skills directory: `~/.claude/skills/` for Claude Code, `~/.agents/skills/` for most other agents.

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
uv run python scripts/gen_docs.py    # regenerate docs/tools.md after changing a tool
```

Unit, MCP and end-to-end tests run against fakes (a mock `ib_async.IB`, and a fake TWS socket server on localhost) and never reach a real gateway. Two integration suites run only on demand, never in CI:

- `IB_HOST=... IB_PORT=... uv run pytest -m live_readonly`: read-only probes of every read toolset.
- `IB_HOST=... IB_PORT=... IB_ACCOUNT=DU... uv run pytest -m paper`: order round trips; they refuse to run unless every account on the login is a paper account.

The contributor guide, with the architecture, the test tiers and the safety invariants every change keeps, is [CLAUDE.md](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/CLAUDE.md). See also [CONTRIBUTING.md](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/CONTRIBUTING.md), [SECURITY.md](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/SECURITY.md) and [CHANGELOG.md](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/CHANGELOG.md).

## Disclaimer

This project is not affiliated with, endorsed by, or supported by Interactive Brokers. "Interactive Brokers", "IBKR", "IB Gateway" and "TWS" are trademarks of their owners and are used here only to name the software this project works with. Trading involves the risk of loss, and software that places orders can place wrong ones. Nothing here is investment advice. Use it at your own risk; the software comes with no warranty (see the license), and you are responsible for every order it places on your accounts.

## License

[MIT](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/LICENSE)
