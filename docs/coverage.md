# TWS API coverage

<!-- Edited by hand; tests/unit/test_docs.py checks that every tool is listed. -->

How the requests of the official TWS API (`EClient`, API 10.45; 10.50 only removes `reqFundamentalData`) map onto the tools of this server, through [`ib_async`](https://github.com/ib-api-reloaded/ib_async) 2.1.0. Tool details: [tools.md](tools.md).

Legend: `X Async` is an awaitable `ib_async` request; `IB.X` sends and returns a live object; `cache` reads `ib_async`'s state; `client` means only `ib.client.X` exists, and `+hook` means the server installs the missing callback; **missing** means `ib_async` 2.1.0 cannot send it. Cancel requests behind streams are served by the shared `unsubscribe` tool.

## Requests by toolset

### ops (always on)

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| reqCurrentTime | `reqCurrentTimeAsync()`, spaced (C) | get_server_time | READ |
| reqCurrentTimeInMillis | **missing** (server ≥197) | (get_server_time) | — |
| reqUserInfo | `reqUserInfoAsync()` | get_user_info | READ |
| reqManagedAccts | internal; `cache managedAccounts()` | list_accounts | READ |
| serverVersion / twsConnectionTime / isConnected | `client.serverVersion()`, `client.connectionStats()`, `isConnected()` | get_health, get_connection_info | READ |

### contracts

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| reqContractDetails | `reqContractDetailsAsync(c)`; `BaseService.qualify` / `qualify_details` | get_contract_details, qualify_contract | READ |
| cancelContractData | **missing** (≥215, protobuf) | — | — |
| reqMatchingSymbols | `reqMatchingSymbolsAsync(pattern)` | search_symbols | READ |
| reqSecDefOptParams | `reqSecDefOptParamsAsync(sym, futFopExchange, secType, conId)` | get_option_chain | READ |
| reqMarketRule | `reqMarketRuleAsync(id)` | get_market_rule | READ |
| reqSmartComponents | `reqSmartComponentsAsync(bboExchange)` | get_smart_components | READ |
| reqMktDepthExchanges | `reqMktDepthExchangesAsync()` | get_depth_exchanges | READ |

### market_data

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| reqMktData (snapshot=True) | `reqTickersAsync(*c, regulatorySnapshot=)` | get_quotes | READ |
| reqMktData (streaming) | `IB.reqMktData(c, genericTickList)` | subscribe_quotes | READ |
| cancelMktData | `IB.cancelMktData(c)` | unsubscribe | READ |
| reqMarketDataType | `IB.reqMarketDataType(n)` | set_market_data_type | READ (session setting, not read-only) |
| reqMktDepth | `IB.reqMktDepth(c, numRows, isSmartDepth)` | subscribe_market_depth | READ |
| cancelMktDepth | `IB.cancelMktDepth(c, isSmartDepth)` | unsubscribe | READ |
| reqTickByTickData | `IB.reqTickByTickData(c, tickType, n, ignoreSize)` | subscribe_tick_by_tick | READ |
| cancelTickByTickData | `IB.cancelTickByTickData(c, tickType)` | unsubscribe | READ |
| reqRealTimeBars | `IB.reqRealTimeBars(c, 5, what, useRTH)` → RealTimeBarList | subscribe_realtime_bars | READ |
| cancelRealTimeBars | `IB.cancelRealTimeBars(bars)` | unsubscribe | READ |
| reqHistoricalData (keepUpToDate=True) | `reqHistoricalDataAsync(..., keepUpToDate=True)` → live BarDataList | subscribe_bars | READ |
| cancelHistoricalData | `IB.cancelHistoricalData(bars)` | unsubscribe | READ |
| *(server-side)* | SubscriptionRegistry | list_subscriptions, get_subscription_data, unsubscribe | READ |

### history

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| reqHistoricalData | `reqHistoricalDataAsync(c, end, duration, barSize, what, useRTH, formatDate=2, timeout=…)` | get_historical_bars | READ |
| reqHistoricalData (whatToShow=SCHEDULE) | `reqHistoricalScheduleAsync(c, numDays, end, useRTH)` | get_trading_schedule | READ |
| reqHistoricalTicks | `reqHistoricalTicksAsync(c, start, end, n, what, useRth, ignoreSize)` | get_historical_ticks | READ |
| cancelHistoricalTicks | **missing** (≥215, protobuf) | — | — |
| reqHeadTimeStamp | `reqHeadTimeStampAsync(c, what, useRTH, formatDate=2)` | get_head_timestamp | READ |
| cancelHeadTimeStamp | internal to `reqHeadTimeStampAsync` | — | — |
| reqHistogramData | `reqHistogramDataAsync(c, useRTH, period)` | get_histogram | READ |
| cancelHistogramData | `client` | — | — |

### scanners

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| reqScannerParameters | `reqScannerParametersAsync()` → XML str | get_scanner_parameters | READ |
| reqScannerSubscription | `IB.reqScannerSubscription(sub, opts, filterOpts)` → ScanDataList; `reqScannerDataAsync` | run_scanner (one-shot), subscribe_scanner | READ |
| cancelScannerSubscription | `IB.cancelScannerSubscription(dataList)` | unsubscribe; internal after run_scanner | READ |

### news

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| reqNewsProviders | `reqNewsProvidersAsync()` | get_news_providers | READ |
| reqHistoricalNews | `reqHistoricalNewsAsync(conId, "A+B", start, end, total)` | get_historical_news | READ |
| reqNewsArticle | `reqNewsArticleAsync(provider, articleId)` | get_news_article | READ |
| reqNewsBulletins | `IB.reqNewsBulletins(allMessages)`; `cache newsBulletins()` | subscribe_news_bulletins | READ |
| cancelNewsBulletins | `IB.cancelNewsBulletins()` | unsubscribe | READ |
| *(reqMktData, generic tick 292)* | `IB.reqMktData(c, "mdoff,292:PROV")`; `cache newsTicks()` | subscribe_news | READ |

### fundamentals

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| reqFundamentalData | `reqFundamentalDataAsync(c, reportType)` → XML str | get_fundamental_data | READ |
| cancelFundamentalData | `client` | — | — |
| reqWshMetaData | `getWshMetaDataAsync()` → JSON str | get_wsh_metadata | READ |
| cancelWshMetaData | `IB.cancelWshMetaData()` | — | — |
| reqWshEventData | `getWshEventDataAsync(WshEventData)` → JSON str | get_wsh_events | READ |
| cancelWshEventData | `IB.cancelWshEventData()` | — | — |

### account

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| reqAccountSummary | `accountSummaryAsync(account)` (runs `reqAccountSummaryAsync` once) | get_account_summary | READ |
| cancelAccountSummary | `client` | — | — |
| reqAccountUpdates | `reqAccountUpdatesAsync(account)`; `cache accountValues(acct)`, `portfolio(acct)` | get_account_values, get_portfolio | READ |
| reqAccountUpdatesMulti | `reqAccountUpdatesMultiAsync(account, modelCode)` | get_account_values (`model_code`, non-default accounts) | READ |
| cancelAccountUpdatesMulti | `client` | — | — |
| reqPositions | `reqPositionsAsync()`; `cache positions(acct)` | get_positions | READ |
| cancelPositions | `client` | — | — |
| reqPositionsMulti | `client` **+hook** (`positionMulti`/`positionMultiEnd` are stubs) | get_positions (`model_code` set) | READ |
| cancelPositionsMulti | `client` | — | — |
| reqPnL | `IB.reqPnL(account, modelCode)` → live PnL; `cache pnl()` | get_pnl | READ |
| cancelPnL | `IB.cancelPnL(account, modelCode)` | — | — |
| reqPnLSingle | `IB.reqPnLSingle(account, modelCode, conId)` → live PnLSingle | get_position_pnl | READ |
| cancelPnLSingle | `IB.cancelPnLSingle(...)` | — | — |
| reqExecutions | `reqExecutionsAsync(ExecutionFilter)` → Fill list (with commissionReport) | get_executions | READ |
| reqOpenOrders | `reqOpenOrdersAsync()` (on a read-only API: `reqAllOpenOrdersAsync()` filtered by client id, see C) | get_open_orders (`include_other_clients=false`) | READ |
| reqAllOpenOrders | `reqAllOpenOrdersAsync()` | get_open_orders (default `include_other_clients=true`) | READ |
| reqCompletedOrders | `reqCompletedOrdersAsync(apiOnly)` | get_completed_orders | READ |

### options

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| calculateImpliedVolatility | `client.send(54, ...)` + `wrapper.startReq` (C) | calculate_implied_volatility | READ |
| cancelCalculateImpliedVolatility | internal (`client.cancelCalculateImpliedVolatility`) | — | — |
| calculateOptionPrice | `client.send(55, ...)` + `wrapper.startReq` (C) | calculate_option_price | READ |
| cancelCalculateOptionPrice | internal (`client.cancelCalculateOptionPrice`) | — | — |
| *(composite)* | `reqSecDefOptParamsAsync` + `BaseService.qualify_many` + `reqTickersAsync` | get_option_quotes | READ |

### orders (profile `trading`)

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| placeOrder (whatIf=True) | `whatIfOrderAsync(c, order)` → OrderState | preview_order, preview_bracket_order, preview_oca_group, preview_combo_order, preview_modify_order | READ |
| placeOrder | `IB.placeOrder(c, order)` → Trade | submit_order | WRITE |
| cancelOrder | `IB.cancelOrder(order, manualCancelOrderTime="")` | cancel_order | WRITE |
| reqGlobalCancel | `IB.reqGlobalCancel()` (only with `IBKR_MCP_ALLOW_GLOBAL_CANCEL`) | preview_cancel_all_orders → submit_order | WRITE |
| exerciseOptions | `IB.exerciseOptions(c, action, qty, account, override)` | preview_exercise_options → submit_order | WRITE |
| reqIds | internal (`client.getReqId()`, nextValidId) | — | — |
| reqAutoOpenOrders | `IB.reqAutoOpenOrders(True)` | — | — |
| *(cache)* | `cache trades()`, `openTrades()`; `reqAllOpenOrdersAsync` fallback | get_order_status | READ |

### advisor (profile `full`; FA or IBroker master logins only)

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| requestFA | `requestFAAsync(faDataType)` → XML | get_fa_config | READ |
| replaceFA | `IB.replaceFA(faDataType, xml)` (fire-and-forget) **+hook** `replaceFAEnd` | preview_replace_fa_config → apply_fa_config | WRITE |
| reqSoftDollarTiers | `client` **+hook** (`softDollarTiers` is a stub) | get_soft_dollar_tiers | READ |
| reqFamilyCodes | `client` **+hook** (`familyCodes` is a stub) | get_family_codes | READ |

### admin (profile `full`)

| EClient | ib_async 2.1.0 | Tool | Tier |
| --- | --- | --- | --- |
| setServerLogLevel | `client.setServerLogLevel(level)` | set_server_log_level | ADMIN |
| queryDisplayGroups | `client` **+hook** `displayGroupList` | list_display_groups | READ |
| subscribeToGroupEvents | `client` **+hook** `displayGroupUpdated` | subscribe_display_group | READ |
| updateDisplayGroup | `client` | update_display_group | ADMIN |
| unsubscribeFromGroupEvents | `client` | unsubscribe | READ |
| *(server-side)* | `safety/ratelimit.py` | reset_circuit_breaker | ADMIN |
| reqConfig (protobuf only) | **missing** (≥219) | — | — |
| updateConfig (protobuf only) | **missing** (≥221) | not exposed | — |

### internal / n/a (not tools)

| EClient | ib_async 2.1.0 |
| --- | --- |
| connect, startApi, disconnect, isConnected, reset, setConnState, checkConnected | `connectAsync` / `disconnect` / `isConnected` |
| run, msgLoopTmo, msgLoopRec, keyboardInterrupt, keyboardInterruptHard | n/a (ib_async runs on asyncio) |
| sendMsg, sendMsgProtoBuf, useProtoBuf, logRequest, validateInvalidSymbols, validateOrderParameters, validateAttachedOrdersParameters | client internals |
| setConnectOptions | `client.setConnectOptions` |
| setOptionalCapabilities | `client.optCapab` |
| serverVersion, twsConnectionTime | see ops |
| verifyRequest, verifyMessage, verifyAndAuthRequest, verifyAndAuthMessage | `client` (no callbacks) |
| all 80 `*ProtoBuf` twins, startApiProtoBuf | not supported (client v178 < 201) |

### Totals (84 EClient API requests)

| Disposition | Count | Methods |
| --- | --- | --- |
| Backs a tool directly | 51 | ops 2, contracts 6, market_data 5, history 4 (reqHistoricalData also backs subscribe_bars and get_trading_schedule), scanners 2, news 4, fundamentals 3, account 11, options 2, orders 4, advisor 4, admin 4 |
| Cancelled through the shared `unsubscribe` | 8 | cancelMktData, cancelMktDepth, cancelTickByTickData, cancelRealTimeBars, cancelHistoricalData, cancelScannerSubscription, cancelNewsBulletins, unsubscribeFromGroupEvents |
| Called internally (by ib_async, a service cleanup or the registry reaper) | 11 | cancelHeadTimeStamp, cancelHistogramData, cancelFundamentalData, cancelWshMetaData, cancelWshEventData, cancelAccountUpdatesMulti, cancelPositionsMulti, cancelPnL, cancelPnLSingle, cancelCalculateImpliedVolatility, cancelCalculateOptionPrice |
| Unreachable in ib_async 2.1.0 | 5 | reqCurrentTimeInMillis, cancelContractData, cancelHistoricalTicks, reqConfig, updateConfig |
| Deliberately not exposed | 9 | reqManagedAccts (cache instead), reqIds, reqAutoOpenOrders, cancelAccountSummary, cancelPositions, verifyRequest, verifyMessage, verifyAndAuthRequest, verifyAndAuthMessage |
| **Total** | **84** |  |

## Deliberately not exposed

| Call | Why |
|---|---|
| updateConfig | It can turn off `readOnlyApi` and the order precautions, and reset order ids. A model must never be able to disable the gateway's own safety rails, so it stays out even once ib_async can send it. |
| reqAutoOpenOrders | Only valid for client id 0, which this server refuses: orders entered by hand in TWS would bind to the model-driven client, which could then modify or cancel them. |
| reqIds | ib_async owns order-id allocation (nextValidId plus `getReqId`). |
| reqManagedAccts | Accounts arrive in the handshake (`list_accounts` reads the cache). |
| cancelAccountSummary, cancelPositions | ib_async relies on those standing subscriptions for its caches. |
| verifyRequest, verifyMessage, verifyAndAuthRequest, verifyAndAuthMessage | IBKR's legacy ISV (vendor) authentication; unrelated to end users. |
| startApi, connect, disconnect, run, msgLoop*, keyboardInterrupt*, sendMsg*, logRequest, validate*, setConnState, setConnectOptions, setOptionalCapabilities, useProtoBuf, checkConnected, reset | Plumbing. `ConnectionManager` owns the lifecycle. |
| all `*ProtoBuf` twins | Encoding variants, not supported by ib_async 2.1.0. |
| reqMktData with `regulatorySnapshot` by default | Exposed only as an opt-in flag with a fee warning (about $0.01 per request), refused unless the operator sets `IBKR_MCP_ALLOW_REGULATORY_SNAPSHOTS`; each billed request is audited. |

## Known ib_async 2.1.0 gaps

**A. Missing entirely, and unreachable at client version 178** (they need a newer, protobuf-capable client than ib_async 2.1.0):

- `reqCurrentTimeInMillis` (≥197)
- `cancelContractData` and `cancelHistoricalTicks` (≥215)
- `reqConfig` (≥219) and `updateConfig` (≥221), protobuf only

Field-level gaps behind the same ceiling:

- exercise `manualOrderTime` / `customerAccount` / `professionalCustomer`
- the `OrderCancel` object (CME tagging) on cancelOrder and globalCancel
- `ExecutionFilter.lastNDays` / `specificDates`
- `includeOvernight` and the newer order attributes (≥189)
- `Z`-suffixed UTC timestamps (214) and error timestamps (194)

**B. In `ib.client` but with no `IB` wrapper.** The callbacks are handled with `services/_hooks.py`:

- `reqSoftDollarTiers` (hook `softDollarTiers`)
- `reqFamilyCodes` (hook `familyCodes`)
- `reqPositionsMulti` / `cancelPositionsMulti` (hooks `positionMulti`, `positionMultiEnd`)
- `queryDisplayGroups` (hook `displayGroupList`)
- `subscribeToGroupEvents` / `updateDisplayGroup` / `unsubscribeFromGroupEvents` (hook `displayGroupUpdated`)
- `setServerLogLevel` (no callback)
- `cancelAccountSummary`, `cancelPositions`, `cancelAccountUpdatesMulti`, `cancelHistogramData`, `cancelFundamentalData`
- `replaceFA` has an `IB` wrapper but ib_async drops its completion callback `replaceFAEnd` (hooked by this server).

**C. Behavioural quirks the services handle:**

- `RaiseRequestErrors=False`; 321 counts as a warning.
- Internal timeouts return `None`; historical data returns an empty list on timeout.
- `reqScannerDataAsync` leaks when cancelled.
- Account summary has fixed tags; account updates cover one account at a time.
- FA profiles (type 2) are desupported.
- One Ticker per conId; tick lists are cleared on every packet.
- `readonly=True` skips the order-cache sync.
- `bracketOrder()` spends order ids.
- `calculateImpliedVolatilityAsync` and `calculateOptionPriceAsync` send a tag count before the misc-options string, which the official client (10.45) does not; IB Gateway reads the count as the options and rejects the request (error 320). The options service sends both calculators through `ib.client.send` in the official layout.
- IB Gateway (10.45) silently ignores a `reqCurrentTime` sent within a second of its last answer; `ConnectionManager.request_current_time` spaces them.
- A read-only API refuses `reqOpenOrders` and `reqCompletedOrders` with error 321 under request id -1, which ib_async never ties to the request (it would wait out its timeout). The account service fails such a read at once, and reads this client's open orders from `reqAllOpenOrders` instead.
- Bonds on a login without bond reference data arrive with an empty coupon, which ib_async decodes as 0; with no maturity either, the coupon is reported as unknown (null).

**D. Deprecated upstream:** `reqFundamentalData` was removed from API 10.50. It still works on Gateway `stable` (10.45); the tool's description says so.

## Not supported yet (orders)

- **Order conditions** (price, time, margin, volume, execution, percent change; `conditionsCancelOrder`, `conditionsIgnoreRth`).
- **FA group allocation** (`faGroup`, `faMethod` EqualQuantity/NetLiq/AvailableEquity/PctChange, `faPercentage`). An allocated order spans several accounts, so it needs the group's members checked against the allowlist, paper/live gating across members, and account-scoped reads that understand allocations. It can't be tested without an FA login.
- **`cash_qty`**: the size is a money amount, so the quantity limits need a reference price first.
- **Order types** PEG BEST and PEG MID's IBKR ATS offsets (`minTradeQty`, `minCompeteSize`, `competeAgainstBestOffset`, `midOffsetAtWhole/Half`), PEG STK, PEG BENCH, MKT PRT, STP PRT, MTL, BOX TOP, VOL and SNAP MID/MKT/PRIM.
- **Attributes** `min_qty`, adjustable/attached stops, hedge orders, scale orders, discretionary amount, `sweep_to_fill`.
- **IBKR algos** DarkIce, AD (accumulate/distribute), BalanceImpactRisk, MinImpact.
