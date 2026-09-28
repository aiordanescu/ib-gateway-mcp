---
name: ib-gateway-mcp
description: Places, changes and cancels Interactive Brokers (IBKR) orders and reads IBKR quotes, option chains, positions and balances through the ib-gateway-mcp MCP server, with the preview-then-submit flow, brackets, OCA groups, combos, fill checks, and what to do while the gateway is down. Use when the user wants to trade or check anything at IBKR, when an ib-gateway-mcp tool fails with not_connected, request_timeout, not_accepting, circuit_open or IBKR error 10089, or after a gateway outage, even if the user only says IB, TWS, IB Gateway or my broker.
license: MIT (see LICENSE.txt)
compatibility: Requires the ib-gateway-mcp MCP server connected to IB Gateway or TWS. Written for the release in metadata.version; with an older server some tools or fields named here may be missing. Order tools need the trading or full profile.
metadata:
  version: "0.2.0"
  mcp-server: ib-gateway-mcp
---

# ib-gateway-mcp

The ib-gateway-mcp MCP server connects you to Interactive Brokers (IBKR) through IB Gateway or TWS. Each tool's description says what the tool does. This guide says how the tools fit together: the order workflow, what to check before and after an order, which errors are final, and how to report an outage without guessing its cause.

Tool names here are the server's own, in backticks (`get_health`). Your client may show them with a prefix built from the server's name. If a tool named here is missing, either the server's profile doesn't enable that toolset or the server is older than this guide. Tell the user which tool is missing; don't substitute another tool for it.

## When to use

- The user wants to place, change, cancel or check an order at IBKR, or exercise options.
- The user asks for IBKR quotes, option chains, positions, balances, P&L or fills.
- A tool of this server fails with `not_connected`, `request_timeout` or an IBKR error, or `get_health` reports `not_accepting`.
- The gateway was down and is back, and work was in flight: orders, streams, previews.

## Ground rules

- **Only the human confirms live actions.** On a live (real-money) account, `submit_order`, `cancel_order` and FA changes make the client ask the person at it, with a question the server writes (unless the operator turned that off). You can't answer it, no parameter stands in for it, and a client that can't ask is refused. Never ask the user for a code or phrase to pass along as a confirmation.
- **Never restart the gateway, its container or this server yourself**, and never reach the gateway around this server. Tell the user what `get_health` reports; the operator decides what to do.
- **Paper or live.** `list_accounts` marks each account with `is_paper` (paper account ids start with D, such as `DU1234567`), and every preview repeats it. Say which kind of account an order is for.
- **Null means not reported, never zero.** IBKR leaves out prices, sizes and commissions it doesn't have, and the tools return null for them. Say so instead of computing with them.
- **Never retry a write blindly.** A submit or cancel that failed or timed out may still have reached IBKR; check first (see Errors).
- **Accounts.** Account-scoped tools take an optional `account`; without one they use `default_account` from `list_accounts`. When that is null, the login manages several accounts: ask the user which one, then pass it.
- **Amounts.** Write money with its currency code (USD 1,000). An order's `notional` is in the contract's currency; `what_if` margin and equity values are in the account's base currency.

## Errors

Tool errors start with a stable code, then a message that says what to do (`order_limit: ...`). This table says which errors are final.

| Code | What to do |
|---|---|
| `order_limit` | Final. The server's limits refuse the order; the message lists every limit it breaks. Tell the user. Change the order only if the user asks. |
| `live_trading_disabled` | Final. A live account is in scope and the operator hasn't allowed live trading. |
| `account_not_allowed` | Final. The account isn't in the server's allowlist; `list_accounts` shows the allowed ones. |
| `confirmation_declined` | Final. The human said no or closed the question, and a submit's token is discarded. Don't ask again unless the user brings it up. |
| `confirmation_unavailable` | Final. This client can't show the confirmation, so live orders, cancels and FA changes can't be done from it. |
| `circuit_open` | Final. Repeated IBKR rejections halted order submission. Stop placing orders and tell the user; only a human can resume it. Call `reset_circuit_breaker` only when the user asks; it always asks the human, on paper too. Cancels still work. |
| `rate_limit` | Wait the time the message states, then retry. A refused submit keeps its token until `expires_at`; after that, preview again. An action that needs more slots than the whole limit can never be sent at once. |
| `token_expired` | Preview again and show the user the new preview: prices and margin may have moved. |
| `token_not_found` | The message says why. "Already used" means an earlier submit with that token passed the server's checks, so the order may be at IBKR: check `get_order_status` or `get_open_orders` before anything else. Unknown (a server restart drops every token) or discarded: preview again if the action is still wanted. |
| `token_mismatch` | Stop and report it; it points to a fault in the server. |
| `not_connected`, `request_timeout` | Call `get_health`. Retry reads once it reports `connected`. After a submit, modify or cancel, check `get_order_status` or `get_open_orders` first: the order may have reached IBKR. |
| `ib_api_error` | IBKR refused. The message has IBKR's error code and text, often with a hint; follow it instead of retrying unchanged. |
| `invalid_request` | Fix the argument the message names. A price off the tick grid gets the nearest valid prices. |
| `not_found` | Nothing matches what was looked up: a contract, an order, a position, a report. Follow the message. For a contract, refine the spec (`qualify_contract`). For an order, find it in `get_open_orders`, or by `perm_id` in `get_order_status` if it completed before the server restarted. From `preview_cancel_all_orders` it means there is nothing to cancel: tell the user. |
| `ambiguous_contract` | The spec matches several instruments. The message lists them with their `con_id`: pick the one the user means, or ask them. |
| `subscription_limit` | Free a slot: `list_subscriptions`, then `unsubscribe` what isn't needed. |
| `subscription_not_found` | The stream is gone (idle too long, or dropped after a reconnect). Subscribe again if it is still needed. |
| `configuration_error` | A server setting blocks it: order tools not enabled, the gateway's API read-only, global cancel not allowed. Tell the user; the operator changes settings. |

## Placing an order

1. **Pin down the instrument.** `search_symbols` finds a ticker or company. `qualify_contract` resolves a spec to exactly one contract: check its `description` (the long name) and keep its `con_id` for later calls.
2. **Look at the market.** `get_quotes` gives bid, ask and last (see Market data if it fails). Prices must sit on the contract's price increments; `get_contract_details` and `get_market_rule` give them.
3. **Preview.** `preview_order` takes the `order`: `contract`, `action`, `quantity`, `order_type` and the prices that type needs. Nothing is sent. Show the user the `summary`, the account and `is_paper`, IBKR's `what_if` (margin change, commission estimate, `warning_text`) and any `warnings`, and get their go-ahead.
4. **Submit.** `submit_order` with the preview's `token`, before its `expires_at` (IBKR_MCP_TOKEN_TTL, 120 seconds by default). If the user takes longer, preview again. On a live account the client then asks the human (unless the operator turned that off).
5. **Read the result.** `accepted` false means IBKR rejected the order; `messages` says why. PreSubmitted or Submitted means the order is working, not that it filled.
6. **Confirm fills** with `get_order_status` (by `order_id`, or `perm_id`): `status`, `filled`, `remaining`, `avg_fill_price` and `fills`. `get_executions` lists the day's fills with commissions; `commission` stays null until IBKR reports it.

Report an order as filled only when `get_order_status` says Filled. Keep both ids from the result: an order that completed before the server restarted is found by `perm_id` only.

Under a notional limit the server needs a price that bounds the fill. Only a BUY limit price does (LMT, STP LMT, LIT, LOC, LOO: it caps what the buyer pays). Every other order, SELL limits and stops included, needs a market price (the check uses the higher of that and the order's own price). Without market data such an order is refused with `order_limit`, and so is a bracket or OCA group that contains one. The preview says when that price was delayed, frozen or the previous close.

## Brackets, OCA groups and combos

Each of these is one preview and one `submit_order`.

- **Bracket** (`preview_bracket_order`): an entry plus a take-profit limit and a stop-loss stop. For a BUY the stop loss sits below the entry and the take profit above it; for a SELL the other way round. The exits start working only once the entry fills, and when one exit fills IBKR cancels the other. The what-if covers the entry only, and the bracket takes 3 slots of the order rate limit. If IBKR rejects any of the three, the server cancels the rest and says so in `messages`. The submit result lists the three with their roles (`entry`, `take_profit`, `stop_loss`); in `get_open_orders` the exits carry the entry's id as `parent_id`.
- **OCA group** (`preview_oca_group`): 2 to 10 orders on any instruments; when one fills, the others are cancelled (`oca_type` 1, the default) or reduced (2 and 3). Typical uses: exits for a position already held, a SELL limit at the target and a SELL stop for the same quantity; or entries at several prices where only one should fill. Each member gets its own what-if and takes a slot of the order rate limit.
- **Combo** (`preview_combo_order`): 2 to 8 `legs`, each a contract, a `ratio` and the leg's `action` when the combo is bought. `limit_price` is the net price per combo unit, negative for a credit. Qualify each option leg first (see Option chains). Under a notional limit every leg needs a market price (options need OPRA data); the net price doesn't stand in for it. `non_guaranteed` is required for stock pairs and legs on different underlyings, and then one leg can fill without the others: tell the user before submitting.

## Changing and cancelling

- IBKR lets only the API client that placed an order change or cancel it. `get_open_orders` marks this server's orders with `modifiable` true; the rest are shown for information.
- **Modify:** `preview_modify_order` with the `order_id` and the `changes` (quantity, prices, time in force), then `submit_order`. Order type, side and contract can't change: cancel and place a new order instead. If the order fills or is changed elsewhere before the submit, the submit is refused. If IBKR rejects the change, the original order keeps working.
- **Cancel one order:** `cancel_order` with the `order_id`. It needs no preview; on a live account the human still confirms. It returns Cancelled, or PendingCancel while IBKR works on it, and fills made before the cancel stay. Cancelling a stop loss can leave a position unprotected: say so before you do it.
- **Cancel all:** `preview_cancel_all_orders` lists what would be cancelled and returns a token for `submit_order`. The default `scope`, `this_client`, covers this server's orders in the account. `global` is IBKR's global cancel of every order on the login, other programs' and manual ones included, and also hits orders placed after the preview. It works only when the operator allowed it; use it only when the user asks for exactly that.
- **Exercise or lapse options:** `preview_exercise_options`, then `submit_order`. It can't be undone, and IBKR sends no acknowledgement: the outcome shows up later in `get_positions`.
- **FA group changes** (advisor logins): `preview_replace_fa_config`, then `apply_fa_config` with its token. On `request_timeout` IBKR may still have applied it: check `get_fa_config` before retrying.

## Market data

- `get_quotes` takes up to 25 contracts. Contracts that fail are listed in `errors` while the others still get quotes. A snapshot of a quiet contract can take about 11 seconds.
- IBKR errors 354, 10089 and 10168 mean the login has no live market data for that instrument. Call `set_market_data_type` with `delayed` (15 to 20 minutes old, free for most exchanges) and retry. If 10089 comes back with delayed data already selected, IBKR has no delayed data for that instrument on this login either: tell the user instead of retrying.
- The market data type applies to every client and tool of this server and is kept across gateway reconnects. It returns to the operator's default (IBKR_MCP_MARKET_DATA_TYPE) only when the server itself restarts. Read it in `get_health` (`market_data_type`) before changing it; don't set it again after every outage. Each quote's own `market_data_type` says what IBKR actually sent.
- Delayed data has no market depth and no tick-by-tick data. Open streams keep what they had until you `unsubscribe` and subscribe again.
- Streams (`subscribe_quotes`, `subscribe_bars` and the other subscribe tools) return a `subscription_id`. Read it with `get_subscription_data`, stop it with `unsubscribe`. Each read keeps a stream alive; one nobody reads for the idle time (`idle_expires_at` in `list_subscriptions`) is cancelled. `unsubscribe` with `all` also stops streams that other clients of this server opened.
- `regulatory_snapshot` on `get_quotes` costs money per request. Use it only when the user asks for it.

## Option chains

- `get_option_chain` lists expirations and strikes per trading class (for example SPX and SPXW), without prices. For US stock and index options pass `exchange` SMART; otherwise IBKR lists a chain per options exchange and the answer can be long.
- Its `strikes` are the union across expirations: not every strike exists for every expiry. Before quoting or ordering one option, qualify it with `qualify_contract`: `symbol`, `sec_type` OPT, the expiry in `last_trade_date_or_contract_month`, `strike`, `right`, and `trading_class` when several classes list that expiry.
- `get_option_quotes` quotes a slice of one expiration (a strike range, or the strikes nearest the underlying's price) with greeks. Strikes the chain lists but that expiration lacks come back in `skipped`. Live quotes for US stock and index options need IBKR's OPRA subscription; without it, try delayed data.
- `calculate_option_price` takes `volatility` as a decimal: 0.25 means 25 percent.

## When the gateway is down

Call `get_health`. It never fails, and the server keeps reconnecting in the background. With `probe` set to true it also sends one real request, for when the state may lag behind a stalled connection.

- `state` is `connected`, `connecting`, `not_accepting`, `connectivity_lost` (the gateway is up but cut off from IBKR; this usually heals by itself) or `not_connected` (stopped, or the connection dropped and a retry is pending).
- `not_accepting` means the gateway refused or ignored the connection. The state alone doesn't say why; `hint` does when the server knows: another API client already uses this client id, or the host can't be reached. Otherwise the gateway may be stopped, starting, logging in, waiting for a second-factor approval, or stuck.
- `last_disconnect_at` is when the connection last went down, and `connected_since` when it last came up. It is null if the server's first connection attempt succeeded and the connection hasn't dropped since. If the gateway was already down when this server started, it is the server's start time, also after the connection comes back, and the outage may be older. Report an outage window only when `last_disconnect_at` is set.
- `login_state` is filled only when the operator lets the server read the gateway's log (IB_GATEWAY_SETTINGS_DIR). When it is null, relay `hint`. If the hint names the cause (client id in use, host unreachable), tell the user that; the operator fixes it. If it only lists possibilities, report since when the gateway has been down and say the cause isn't visible from here. Don't guess one; in particular, never say that a two-factor prompt is waiting or was approved.

When `login_state` is present, its `phase` says where the gateway's login stands and `since` since when, and `hint` puts it in words. Relay the hint. What each phase means for the human:

| `phase` | Meaning | Tell the human |
|---|---|---|
| `logged_in` | The last login in the gateway's log succeeded. The log records only logins, so a later stop or lost session doesn't show there, and an old `log_updated_at` is normal. | Within a minute or two of `since` the gateway is still starting its API; check again shortly. Later than that, the operator should check the gateway: its log can't say what happened after the login. |
| `restarting` | The gateway just started and is about to log in. | Nothing yet; this normally takes under a minute. |
| `logging_in` | A login is in progress (the gateway retries by itself after a network error or when IBKR doesn't answer in time), or IBKR just ended one and a new login may follow (below). | Nothing yet; relay the hint. If `login_attempts` keeps growing, IBKR is not completing the login. |
| `awaiting_2fa` | IBKR sent a second-factor challenge at `since`; `twofa_challenges` counts them. | The account holder must approve it (an IB Key push goes to IBKR Mobile). An unanswered challenge ends after about 4 to 15 minutes; what follows depends on the gateway's setup (below). |
| `throttled` | The gateway is pausing logins after repeated failures until `retry_at`. | Nothing before `retry_at`; what follows depends on the gateway's setup (below). The hint says whether the next login sends a new second-factor challenge; if so, the account holder should be ready to approve it. |
| `login_rejected` | IBKR rejected the login, and the gateway won't retry. | The operator has to act, and the hint says how: after a daily auto-restart whose saved session IBKR refused, a cold restart of the gateway container; after rejected credentials, a check of them. Approving a second-factor prompt won't help. |
| `login_idle` | The gateway is running but not logged in, and nothing is retrying. | The operator has to act: usually a restart of the gateway container, or a look at its configuration if it never began a login. |
| `unknown` | The log doesn't tell: it can't be read, it has been silent (the gateway is probably not running), or it doesn't match the connected gateway. | Report `detail`, and the outage window if `last_disconnect_at` is set. |

After an unanswered second-factor challenge, or once the gateway's pause after failed logins ends, the gateway logs in again by itself only if its login automation is set to (ib-gateway-docker's RELOGIN_AFTER_TWOFA_TIMEOUT=yes; the default is no). This server can't see that setting, so don't promise a retry. Without it the phase turns `login_idle`, and the operator restarts the gateway container.

`login_attempts` and `twofa_challenges` count from the last successful login (`counted_since`); when `counts_complete` is false they are lower bounds. Say that a second-factor prompt was approved only when `login_state` shows `logged_in` with a second-factor approval (its `detail` says how the gateway logged in).

## After an outage

Once `get_health` reports `connected`:

1. Check `trading_enabled`, `circuit_open` and `market_data_type`. A market data type set before a gateway outage still applies; after a restart of the server itself it is back to the operator's default.
2. Check streams with `list_subscriptions`. The server re-requests them after a reconnect, and each stays `stale` until it flows again. One it couldn't re-request is gone, and `get_subscription_data` returns `subscription_not_found` for it. Subscribe again only to what is still needed. A server restart ends every stream.
3. Reconcile orders. Working orders live at IBKR and may have filled or been cancelled during the outage. Compare `get_open_orders`, `get_executions`, `get_completed_orders` and `get_positions` with what you expected, and report every difference.
4. Preview again. Tokens from before the outage have almost certainly expired (`token_expired`), and a server restart drops them all (`token_not_found`).
5. Tell the user what changed and, if `last_disconnect_at` is set, the outage window (`last_disconnect_at` to `connected_since`; if it equals the server's start time, the outage may have begun earlier).

## Gotchas

- `get_executions` covers the current trading day only (up to 7 days if the gateway's trade log keeps them).
- While the gateway's API is read-only (`api_read_only` in `get_health`), orders are refused and `get_completed_orders` fails with IBKR error 321.
- `get_pnl` and `get_position_pnl` fail with `request_timeout` when IBKR sends nothing in time, which is common right after a login; retry once.
- `search_symbols` allows about one search per second. Historical data is paced by IBKR (about 60 requests per 10 minutes; error 162 on a violation).
- Times without a zone are read as UTC. `good_till_date` and `good_after_time` need a time zone.

Copyright (c) 2026 Alex Iordanescu. This skill is part of ib-gateway-mcp and licensed under the MIT License (LICENSE.txt next to this file, or [LICENSE](https://github.com/aiordanescu/ib-gateway-mcp/blob/main/LICENSE) in the repository).
