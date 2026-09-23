# Security

`ib-gateway-mcp` can place orders on a brokerage account, so security reports get priority.

## Supported versions

No version has been released yet; until 0.1.0 is out, fixes land on the `main` branch. After that, security fixes go into the latest release.

## Reporting a vulnerability

Please report it privately, not in a public issue: open a draft advisory at <https://github.com/aiordanescu/ib-gateway-mcp/security/advisories/new> (GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability), also on the repository's **Security** tab). Include what you found, how to reproduce it, and its impact. Never include real account ids, credentials or tokens.

Reports are acknowledged as soon as possible, and fixes are released as soon as they're ready, with credit if you want it.

## Scope

In scope, for example:

- a way to place, modify or cancel an order without a valid preview token, or on an account outside the allowlist;
- getting around the live-trading switch, the human confirmation of live actions, the order limits, the rate limit or the circuit breaker;
- reaching the HTTP transport's tools without the bearer token;
- text the model controls passing for the server's own words in a human confirmation prompt;
- secrets (bearer tokens, preview tokens, credentials) leaking into logs, errors, the audit log or tool output.

Out of scope: problems in IB Gateway or ib-gateway-docker themselves (report those upstream), and anything that needs the attacker to control the machine the server runs on.

The rails assume that the model reaches the gateway only through this server: that it cannot open the gateway's API port itself, restart the server, or change its configuration, audit file or circuit-breaker state. An agent with a shell on the same host or with Docker access can do all of that, so keep the gateway's API port unpublished and the server out of the agent's reach.

## Deployment advice

- Keep the HTTP port off the public internet; publish it on `127.0.0.1` or a private network, and use a long random bearer token (`IBKR_MCP_AUTH_TOKEN_FILE`) in a file only the server's user can read. The token travels in plain HTTP: whenever the port leaves the host, put a TLS reverse proxy in front of it.
- Do not publish the gateway's own API port (4001-4004) on a host where an agent runs: it bypasses every rail of this server.
- Start on a paper login. Enable live trading (`IBKR_MCP_ALLOW_LIVE=true`) only with order limits set (with `IBKR_MCP_ALLOWED_CURRENCIES` next to `IBKR_MCP_MAX_NOTIONAL`), and keep `IBKR_MCP_LIVE_CONFIRM=true`. The server logs a warning at start-up for each risky combination.
- For a deployment that must never trade, also turn on the gateway's own Read-Only API setting (`READ_ONLY_API=yes` in ib-gateway-docker).
- Set `IBKR_MCP_AUDIT_LOG` and keep the file; it also keeps an open circuit breaker open across restarts. The server refuses to start with write tools when the file cannot be written, but a write that fails later is only logged at ERROR and does not stop the order, so alert on those log lines.
