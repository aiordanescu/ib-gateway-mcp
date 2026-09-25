# Contributor guide

`ib-gateway-mcp` is an MCP server and async Python library for the Interactive Brokers TWS API, built on [`ib_async`](https://github.com/ib-api-reloaded/ib_async) and designed to run next to [`gnzsnz/ib-gateway-docker`](https://github.com/gnzsnz/ib-gateway-docker). It can place orders, so its safety rails matter as much as its coverage.

This guide is for everyone who changes the code, people and coding agents alike ([AGENTS.md](AGENTS.md) points here). Users start at [README.md](README.md). The pull request process is in [CONTRIBUTING.md](CONTRIBUTING.md), and vulnerability reports go through [SECURITY.md](SECURITY.md).

## Architecture

Everything lives in `src/ib_gateway_mcp/`, in two layers.

**Library core.** It has no runtime dependency on MCP: nothing outside `mcp/` and `cli.py` imports `ib_gateway_mcp.mcp`.

| Path | Role |
| --- | --- |
| `gateway.py` | `Gateway`, the facade: the connection, the services (`gw.orders`, `gw.market_data`...) and the safety rails in one object. |
| `connection.py` | `ConnectionManager`: the single `ib_async.IB`, connect and reconnect, health, and the trading gate `require_trading()`. |
| `config.py` | `Settings` from `IB_*` (connection) and `IBKR_MCP_*` (server) variables; profiles and toolsets. |
| `accounts.py` | Account scope: the default account and the allowlist. |
| `startup.py` | Start-up checks shared by the server and `Gateway.start`: the safety-configuration summary in the log, and the audit-file check. |
| `subscriptions.py` | `SubscriptionRegistry`: streaming subscriptions with caps, deduplication and idle reaping. |
| `errors.py` | The exception hierarchy; every class has a stable `code`. |
| `services/` | One service per domain; `base.py` holds `BaseService._call`. |
| `models/` | Pydantic inputs (`*Spec`) and outputs (`*Out`, `*Result`, `*List`). |
| `safety/` | Preview tokens, order limits, rate limit and circuit breaker, audit log. Talks to neither the gateway nor MCP. |
| `_ib_compat.py` | Every read or write of `ib_async` internals, in one place. |
| `services/_hooks.py` | Callbacks `ib_async` drops or lacks, hooked on its wrapper. |

**MCP layer** (`mcp/`), thin by design:

| Path | Role |
| --- | --- |
| `server.py` | `build_server`: registers the enabled toolsets; stdio and streamable HTTP, `/healthz`, `/readyz`. |
| `registry.py` | `@ib_tool`, toolsets, tiers (`READ`, `WRITE`, `ADMIN`) and the gate in front of every `WRITE` and `ADMIN` tool. |
| `tools/<toolset>.py` | The tools, one module per toolset. |
| `params.py` | Shared parameter types and their descriptions. |
| `confirm.py` | Human confirmation of live actions through MCP elicitation. |
| `auth.py` | Bearer-token auth and DNS-rebinding protection for HTTP. |

`cli.py` is the `ib-gateway-mcp` entry point. Outside `src/`: `tests/`, `scripts/gen_docs.py` (writes `docs/tools.md`), `docs/coverage.md` (the TWS API coverage table), the `Dockerfile` with `examples/docker-compose.yml`, and CI and releases in `.github/workflows/` (`ci.yml`, `release.yml`).

## Setup

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) 0.12 (`pyproject.toml` pins the range).

```sh
uv sync
uv run pre-commit install
```

The hadolint hook runs in Docker; without Docker, commit with `SKIP=hadolint-docker` (CI runs it).

## Checks

Every change must pass:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy                                  # strict, over src, tests and scripts
uv run pytest                                # the offline tiers, about half a minute
uv run python scripts/gen_docs.py --check    # docs/tools.md matches the registry
uv run pre-commit run --all-files            # markdownlint, hadolint, account-id guard
```

CI also runs the tests on Python 3.12 to 3.14 and on macOS, requires 95% coverage (`uv run pytest --cov`), tests against the lowest versions `pyproject.toml` allows (`uv sync --resolution lowest-direct`, so raise a floor when you rely on a newer feature), audits the locked dependencies, and builds the package and the Docker image.

## Releases

Set the version in `pyproject.toml`, move the `[Unreleased]` entries in `CHANGELOG.md` under a heading for that version, and merge. Then push an annotated tag `v<version>` on that commit. `release.yml` runs every CI job, publishes the image to `ghcr.io/aiordanescu/ib-gateway-mcp` for amd64 and arm64 (tags `<version>` and `<major>.<minor>`, plus `latest` and `stable` unless it is a pre-release), attests it, uploads the sdist and wheel to PyPI (the `pypi` environment's `PYPI_API_TOKEN`, usable from `v*` tags only), and creates the GitHub release from the changelog section. A tag that doesn't match `pyproject.toml` fails before anything is published.

## Tests

| Tier | Where | Selected by | Needs |
| --- | --- | --- | --- |
| Unit | `tests/unit/` | default | nothing: an autospecced fake `ib_async.IB` (`tests/fakes.py`) |
| MCP | `tests/mcp/` | default | nothing: an in-memory MCP client over `build_server` on the fake (`tests/conftest.py`) |
| End-to-end | `tests/e2e/` | default | nothing: the real `ib_async` client against a fake TWS socket server on 127.0.0.1 (`tests/e2e/fake_tws.py`) |
| Live read-only | `tests/integration/test_live_readonly.py` | `-m live_readonly` | `IB_HOST`, `IB_PORT`; runs on the `readonly` profile |
| Paper orders | `tests/integration/test_paper_orders.py` | `-m paper` | `IB_HOST`, `IB_PORT`, `IB_ACCOUNT=DU...`: a paper login |

- The default run deselects the integration tiers and hides any `IB_*` and `IBKR_MCP_*` variables from the offline tests. Warnings are errors, and `xfail` is strict.
- Test a service in `tests/unit/` and its tool in `tests/mcp/`. Add an end-to-end test when the behaviour depends on `ib_async`'s own handshake, framing or decoding, and an integration probe when only a real gateway can answer.
- The integration suites never run in CI. Set `IB_CLIENT_ID` (default 80) to an id no other API client on the login uses. Each module's docstring lists its optional `IB_TEST_*` variables; `IB_TEST_GLOBAL_CANCEL=1` cancels every order on the login, so use it only on a dedicated paper login.
- **Orders run only on paper.** Anything that places, modifies or cancels orders, whether a test, a tool call or an experiment, runs against a paper login and never a live one. Against a gateway with live accounts, run `-m live_readonly` only. The paper suite aborts unless every managed account is a paper account; keep that guard.
- Coding agents: don't connect to a gateway or run an integration suite unless the person you work for asked for it and supplied the environment.

## Conventions

- **Library first.** Services do the work and return pydantic models. A tool calls one service method and adds nothing but its parameters and, for live actions, the confirmation. The library applies the same account scope, trading gate and rails as the server.
- **Async only.** Call `ib_async`'s `*Async` methods (or its non-blocking cache reads) through `BaseService._call`, which adds the timeout and translates failures. Never call the blocking wrappers, `ib.sleep` or `ib.run`. Streams go through the `SubscriptionRegistry`.
- **`ib_async` is pinned** (`==2.1.0`). Its internals are touched only in `_ib_compat.py`, and `tests/unit/test_ib_compat.py` checks every attribute used there. A version bump also reviews the gaps listed in `docs/coverage.md`.
- **Read the installed sources** of `ib_async` and `mcp` (under `.venv/lib/python3.*/site-packages/`) for exact signatures, and prefer their built-ins to hand-rolled code.
- **Models.** Inputs forbid unknown fields; list outputs derive from `Truncatable` and bound their size with `_util.clamp_limit`; services resolve accounts with `AccountScope.resolve`. Everything is typed and passes `mypy --strict`.
- **Errors** are raised from `ib_gateway_mcp.errors`, never as a bare `ValueError` for bad input; `invalid_request_on` turns a pydantic validation error into `InvalidRequestError`. Messages say what went wrong and what to do next, naming the tool to call where there is one. The registry passes these to the model as tool errors prefixed by their `code`; anything else surfaces as an internal error.
- **Tools** are declared with `@ib_tool(toolset, Tier.X, title)` in `mcp/tools/`. They are `async`. Tool modules, `params.py` and `confirm.py` do not use `from __future__ import annotations`, because the SDK reads the real annotations. A tool that changes anything at IBKR is `WRITE` or `ADMIN` and belongs to a write toolset (`orders`, `advisor`, `admin`).
- **Docstrings and parameter descriptions are the model's only documentation.** They become the tool's description and input schema, so state what the tool does, its key parameters, its limits and the errors it returns. Shared parameters use the aliases in `mcp/params.py`; others use `Annotated[..., Field(description=...)]`.
- **Keep the checked docs in step.** After changing a tool's name, parameters or docstring, run `uv run python scripts/gen_docs.py`. A new or renamed parameter goes into `KEY_PARAMETERS` in `tests/mcp/test_toolsets.py`; a new tool needs a row in `docs/coverage.md` (edited by hand); a new setting needs a row in the README's configuration table. Tests check all four.
- **Settings** name their variable explicitly (`validation_alias`): `IB_*` for the connection and `IBKR_MCP_*` for the server. Secrets are `SecretStr` with a `*_FILE` variant. New settings default to the safe side.

## Safety invariants

The rails are the product. Don't weaken, bypass or make optional any of the following; a change that relaxes one needs an issue first. A new write path must go through all of them, with tests that show it does. [SECURITY.md](SECURITY.md) describes the threat model.

1. **Read-only by default.** The default `readonly` profile registers no write tool. Risky switches stay opt-in and off by default (`IBKR_MCP_ALLOW_LIVE`, `IBKR_MCP_ALLOW_GLOBAL_CANCEL`, `IBKR_MCP_ALLOW_REGULATORY_SNAPSHOTS`, `IBKR_MCP_ALLOW_NO_AUTH`), and `IBKR_MCP_LIVE_CONFIRM` stays on by default.
2. **Trading gate.** The registry runs `ConnectionManager.require_trading()` before every `WRITE` and `ADMIN` tool, and every write path in the services calls it as well, so library callers are covered too. It refuses unless the gateway is connected with a writable API, a write toolset is enabled, an account is allowed and no live account is in scope without `IBKR_MCP_ALLOW_LIVE`. Services act only on allowlisted accounts (`AccountScope.resolve`). `connectAsync(readonly=True)` is not a safety boundary.
3. **Preview tokens.** Orders, modifications, exercises, cancel-all and FA changes are previewed first and executed only by token. A token is single-use, expires, stays on the server and binds the exact payload and account; checks run again at execution. No code path places an order from model-supplied fields in one step.
4. **Human confirmation.** On live accounts, submits, cancels and FA changes ask the person at the client through elicitation. The confirmation parameter is resolved by the server and absent from the tool's schema, so the model cannot supply it. Without elicitation the action is refused (fail closed), and the service checks `human_confirmed` again. The server writes the question; text from the model is quoted as one line of plain text. Resetting the circuit breaker always asks, on paper too.
5. **Limits.** `OrderPolicy` checks at preview and again at submit. Notional is computed conservatively: an order with nothing bounding its fill price is refused, not estimated. Rate limits and the circuit breaker apply to every submission, and an open breaker survives restarts.
6. **Audit.** Every preview and write, every refusal at the trading gate and every breaker reset is recorded in the JSONL audit log, with secrets redacted. With an audit file configured, the server won't start with write tools if the file can't be written.
7. **Secrets.** Bearer tokens, preview tokens and credentials never appear in logs, errors, the audit log or tool output, and configuration errors never echo values. HTTP requires a long bearer token, compared in constant time; `IBKR_MCP_ALLOW_NO_AUTH` is accepted on loopback hosts only.

The rails' tests (`tests/unit/test_safety_*.py`, `tests/mcp/test_confirm.py`, `tests/e2e/test_e2e_trading_gate.py` and the gate tests in `tests/mcp/`) must keep passing without being loosened.

## Public repository hygiene

- Never commit account ids, usernames, credentials, tokens, hostnames or IP addresses: not in code, tests, fixtures, docs, commit messages, issues or pull requests. Use the placeholder accounts `DU1234567`, `DU7654321`, `DU1111111`, `DU9999999` (paper) and `U1234567`, `U7654321` (live), the host `ib-gateway` and the port `4004`. The `no-ibkr-account-ids` pre-commit hook, which CI also runs, rejects anything else that looks like an IBKR account id.
- Integration tests read the gateway address and account from the environment. `.env` files and the token and password files the README creates are gitignored; keep them that way.
- Scrub real gateway output (logs, account values, order and execution details) before it goes into a test, fixture, issue or pull request.
- Keep deployment details, personal notes and local agent state out of the tree.
