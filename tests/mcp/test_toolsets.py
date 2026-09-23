"""Cross-toolset checks on the real registry, through an in-memory MCP client.

- Each profile exposes exactly its tool set (readonly 50, trading 60, full 70; the
  per-toolset tables are in ``docs/coverage.md``).
- Every tool in the full profile has a description, input and output schemas, a
  verb-first name and the MCP hints its tier calls for.
- Shared parameters (account, limit, contract, subscription_id, use_rth, bar_size,
  what_to_show, max_chars) are described the same way everywhere, and every tool keeps
  its key parameter names.
- Subscriptions opened by other toolsets (scanners, news, admin) work through the
  generic market_data tools: list_subscriptions, get_subscription_data, unsubscribe.
"""

import itertools
import typing
from typing import Any, get_args
from unittest.mock import MagicMock

import mcp_types as types
import pytest
from ib_async import NewsBulletin

from ib_gateway_mcp.mcp.params import (
    SUBSCRIPTION_ID_HELP,
    AccountArg,
    BarSizeArg,
    ContractArg,
    LimitArg,
    LiveBarSizeArg,
    UseRthArg,
    WhatToShowArg,
)
from ib_gateway_mcp.mcp.registry import REGISTRY, Tier
from ib_gateway_mcp.models.admin import DisplayGroupSnapshot
from ib_gateway_mcp.models.common import SubscriptionDataOut, SubscriptionOut, WhatToShow
from ib_gateway_mcp.models.market_data import SubscriptionList, UnsubscribeResult
from ib_gateway_mcp.models.news import MAX_HISTORICAL_HEADLINES, NewsBulletinSnapshot
from ib_gateway_mcp.models.scanners import MAX_SCANNER_ROWS, ScannerSnapshot
from ib_gateway_mcp.services import contracts, fundamentals, history, news, options, scanners
from ib_gateway_mcp.services import market_data as md
from ib_gateway_mcp.services.base import MAX_LISTED_CANDIDATES
from tests.conftest import McpClientFactory
from tests.unit.test_scanners_service import FakeScanner, rows_for

TOOLSET_TOOLS: dict[str, frozenset[str]] = {
    "ops": frozenset(
        {"get_health", "get_server_time", "get_connection_info", "list_accounts", "get_user_info"}
    ),
    "contracts": frozenset(
        {
            "search_symbols",
            "get_contract_details",
            "qualify_contract",
            "get_option_chain",
            "get_market_rule",
            "get_smart_components",
            "get_depth_exchanges",
        }
    ),
    "market_data": frozenset(
        {
            "get_quotes",
            "set_market_data_type",
            "subscribe_quotes",
            "subscribe_market_depth",
            "subscribe_tick_by_tick",
            "subscribe_realtime_bars",
            "subscribe_bars",
            "list_subscriptions",
            "get_subscription_data",
            "unsubscribe",
        }
    ),
    "history": frozenset(
        {
            "get_historical_bars",
            "get_historical_ticks",
            "get_head_timestamp",
            "get_histogram",
            "get_trading_schedule",
        }
    ),
    "scanners": frozenset({"get_scanner_parameters", "run_scanner", "subscribe_scanner"}),
    "news": frozenset(
        {
            "get_news_providers",
            "get_historical_news",
            "get_news_article",
            "subscribe_news_bulletins",
            "subscribe_news",
        }
    ),
    "fundamentals": frozenset({"get_fundamental_data", "get_wsh_metadata", "get_wsh_events"}),
    "account": frozenset(
        {
            "get_account_summary",
            "get_account_values",
            "get_positions",
            "get_portfolio",
            "get_pnl",
            "get_position_pnl",
            "get_executions",
            "get_open_orders",
            "get_completed_orders",
        }
    ),
    "options": frozenset(
        {"calculate_implied_volatility", "calculate_option_price", "get_option_quotes"}
    ),
    "orders": frozenset(
        {
            "preview_order",
            "preview_bracket_order",
            "preview_oca_group",
            "preview_combo_order",
            "preview_modify_order",
            "preview_exercise_options",
            "preview_cancel_all_orders",
            "submit_order",
            "cancel_order",
            "get_order_status",
        }
    ),
    "advisor": frozenset(
        {
            "get_fa_config",
            "preview_replace_fa_config",
            "apply_fa_config",
            "get_soft_dollar_tiers",
            "get_family_codes",
        }
    ),
    "admin": frozenset(
        {
            "reset_circuit_breaker",
            "list_display_groups",
            "subscribe_display_group",
            "update_display_group",
            "set_server_log_level",
        }
    ),
}
READ_SETS = (
    "ops",
    "contracts",
    "market_data",
    "history",
    "scanners",
    "news",
    "fundamentals",
    "account",
    "options",
)
READONLY_TOOLS = frozenset().union(*(TOOLSET_TOOLS[name] for name in READ_SETS))
TRADING_TOOLS = READONLY_TOOLS | TOOLSET_TOOLS["orders"]
FULL_TOOLS = TRADING_TOOLS | TOOLSET_TOOLS["advisor"] | TOOLSET_TOOLS["admin"]
VERBS = frozenset(
    {
        "apply",
        "calculate",
        "cancel",
        "get",
        "list",
        "preview",
        "qualify",
        "reset",
        "run",
        "search",
        "set",
        "submit",
        "subscribe",
        "unsubscribe",
        "update",
    }
)


def description_of(alias: Any) -> str:
    """The ``Field(description=...)`` inside a shared ``Annotated`` parameter type."""
    field = typing.get_args(alias)[1]
    return str(field.description)


def text_of(result: types.CallToolResult) -> str:
    return result.content[0].text  # type: ignore[union-attr]


async def full_tool_list(mcp_client: McpClientFactory) -> list[types.Tool]:
    async with mcp_client(profile="full") as client:
        return (await client.list_tools()).tools


# --- profiles -------------------------------------------------------------------------------


def test_expected_counts() -> None:
    assert {name: len(tools) for name, tools in TOOLSET_TOOLS.items()} == {
        "ops": 5,
        "contracts": 7,
        "market_data": 10,
        "history": 5,
        "scanners": 3,
        "news": 5,
        "fundamentals": 3,
        "account": 9,
        "options": 3,
        "orders": 10,
        "advisor": 5,
        "admin": 5,
    }
    assert (len(READONLY_TOOLS), len(TRADING_TOOLS), len(FULL_TOOLS)) == (50, 60, 70)


def test_registry_holds_exactly_the_planned_tools() -> None:
    by_toolset: dict[str, set[str]] = {}
    for spec in REGISTRY.specs():
        by_toolset.setdefault(spec.toolset, set()).add(spec.name)
    assert by_toolset == {name: set(tools) for name, tools in TOOLSET_TOOLS.items()}


@pytest.mark.parametrize(
    ("profile", "expected"),
    [("readonly", READONLY_TOOLS), ("trading", TRADING_TOOLS), ("full", FULL_TOOLS)],
)
async def test_each_profile_lists_exactly_its_tools(
    mcp_client: McpClientFactory, profile: str, expected: frozenset[str]
) -> None:
    async with mcp_client(profile=profile) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
    assert len(names) == len(set(names))
    assert set(names) == expected


# --- smoke: descriptions, schemas, hints ------------------------------------------------------


async def test_full_profile_smoke(mcp_client: McpClientFactory) -> None:
    tools = await full_tool_list(mcp_client)
    specs = {spec.name: spec for spec in REGISTRY.specs()}
    assert {tool.name for tool in tools} == FULL_TOOLS
    for tool in tools:
        spec = specs[tool.name]
        assert tool.name.split("_")[0] in VERBS, f"{tool.name} is not verb-first"
        assert tool.description, tool.name
        assert tool.description.strip(), tool.name
        assert tool.input_schema.get("type") == "object", tool.name
        assert isinstance(tool.input_schema.get("properties", {}), dict), tool.name
        # Injected values (the MCP context, human-confirmation resolvers) stay hidden.
        assert not {"ctx", "confirmation"} & set(tool.input_schema.get("properties", {}))
        for name, prop in tool.input_schema.get("properties", {}).items():
            assert prop.get("description"), f"{tool.name}.{name} has no description"
        assert tool.output_schema is not None, tool.name
        hints = tool.annotations
        assert hints is not None
        assert hints.title == spec.title
        if spec.tier is Tier.READ:
            session = tool.name in {"set_market_data_type", "unsubscribe"}
            assert hints.read_only_hint is not session, tool.name
            assert hints.destructive_hint is (False if session else None), tool.name
        else:
            assert hints.read_only_hint is False, tool.name
            assert hints.destructive_hint is True, tool.name
    write_or_admin = {tool.name for tool in tools if specs[tool.name].tier is not Tier.READ}
    assert write_or_admin == {
        "submit_order",
        "cancel_order",
        "apply_fa_config",
        "reset_circuit_breaker",
        "update_display_group",
        "set_server_log_level",
    }


async def test_shared_parameters_read_the_same_everywhere(mcp_client: McpClientFactory) -> None:
    tools = await full_tool_list(mcp_client)
    seen: dict[str, int] = {}
    for tool in tools:
        properties: dict[str, Any] = tool.input_schema.get("properties", {})
        required = set(tool.input_schema.get("required", []))
        where = tool.name
        if "account" in properties:
            seen["account"] = seen.get("account", 0) + 1
            assert properties["account"]["description"] == description_of(AccountArg), where
            assert properties["account"]["default"] is None, where
        if "limit" in properties:
            seen["limit"] = seen.get("limit", 0) + 1
            assert properties["limit"]["description"] == description_of(LimitArg), where
            assert "limit" not in required, where
        if "contract" in properties and "contract" in required:
            seen["contract"] = seen.get("contract", 0) + 1
            assert properties["contract"]["description"] == description_of(ContractArg), where
        if "subscription_id" in properties:
            seen["subscription_id"] = seen.get("subscription_id", 0) + 1
            assert properties["subscription_id"]["description"] == SUBSCRIPTION_ID_HELP, where
        if "use_rth" in properties:
            seen["use_rth"] = seen.get("use_rth", 0) + 1
            assert properties["use_rth"]["description"] == description_of(UseRthArg), where
        if "bar_size" in properties:
            seen["bar_size"] = seen.get("bar_size", 0) + 1
            expected = LiveBarSizeArg if tool.name == "subscribe_bars" else BarSizeArg
            assert properties["bar_size"]["description"] == description_of(expected), where
        what = properties.get("what_to_show", {})
        if what.get("enum") == list(get_args(WhatToShow)):
            seen["what_to_show"] = seen.get("what_to_show", 0) + 1
            assert what["description"] == description_of(WhatToShowArg), where
        if "max_chars" in properties:
            seen["max_chars"] = seen.get("max_chars", 0) + 1
            chars = properties["max_chars"]
            assert chars["type"] == "integer", where
            assert isinstance(chars["default"], int), where
            assert chars["maximum"] >= chars["default"], where
            assert "truncated is true" in chars["description"], where
    assert seen == {
        "account": 15,
        "limit": 12,
        "contract": 20,
        "subscription_id": 3,
        "use_rth": 7,
        "bar_size": 2,
        "what_to_show": 3,
        "max_chars": 4,
    }


# Key parameters per tool: (required, optional). Tools may take more; these names are
# part of the public tool interface and must not be renamed or dropped.
KEY_PARAMETERS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    name: (frozenset(required.split()), frozenset(optional.split()))
    for name, required, optional in [
        # ops
        ("get_health", "", "probe"),
        ("get_server_time", "", ""),
        ("get_connection_info", "", ""),
        ("list_accounts", "", ""),
        ("get_user_info", "", ""),
        # contracts
        ("search_symbols", "pattern", "limit"),
        ("get_contract_details", "contract", "limit"),
        ("qualify_contract", "contract", ""),
        ("get_option_chain", "underlying", "exchange fut_fop_exchange"),
        ("get_market_rule", "market_rule_ids", ""),
        ("get_smart_components", "bbo_exchange", ""),
        ("get_depth_exchanges", "", ""),
        # market_data
        ("get_quotes", "contracts", "regulatory_snapshot"),
        ("set_market_data_type", "data_type", ""),
        ("subscribe_quotes", "contract", "generic_ticks"),
        ("subscribe_market_depth", "contract", "rows smart_depth"),
        ("subscribe_tick_by_tick", "contract tick_type", "ignore_size buffer_size"),
        ("subscribe_realtime_bars", "contract", "what_to_show use_rth buffer_size"),
        ("subscribe_bars", "contract bar_size", "duration what_to_show use_rth"),
        ("list_subscriptions", "", ""),
        ("get_subscription_data", "subscription_id", "limit since"),
        ("unsubscribe", "", "subscription_id all"),
        # history
        (
            "get_historical_bars",
            "contract",
            "bar_size duration end what_to_show use_rth limit",
        ),
        (
            "get_historical_ticks",
            "contract",
            "start end count what_to_show use_rth ignore_size",
        ),
        ("get_head_timestamp", "contract", "what_to_show use_rth"),
        ("get_histogram", "contract", "period use_rth limit"),
        ("get_trading_schedule", "contract", "num_days end use_rth"),
        # scanners
        ("get_scanner_parameters", "section", "query instrument limit"),
        (
            "run_scanner",
            "scan_code",
            "instrument location_code above_price below_price above_volume "
            "market_cap_above market_cap_below filters rows",
        ),
        (
            "subscribe_scanner",
            "scan_code",
            "instrument location_code above_price below_price above_volume "
            "market_cap_above market_cap_below filters rows",
        ),
        # news
        ("get_news_providers", "", ""),
        ("get_historical_news", "contract", "provider_codes start end limit"),
        ("get_news_article", "provider_code article_id", "max_chars"),
        ("subscribe_news_bulletins", "", "all_messages"),
        ("subscribe_news", "", "contract provider_code"),
        # fundamentals
        ("get_fundamental_data", "contract report_type", "max_chars"),
        ("get_wsh_metadata", "", "query max_chars"),
        (
            "get_wsh_events",
            "",
            "contract filter_json event_types start_date end_date fill_watchlist "
            "fill_portfolio fill_competitors limit",
        ),
        # account
        ("get_account_summary", "", "account tags"),
        ("get_account_values", "", "account model_code tags currency limit"),
        ("get_positions", "", "account model_code"),
        ("get_portfolio", "", "account"),
        ("get_pnl", "", "account model_code"),
        ("get_position_pnl", "contract", "account model_code"),
        ("get_executions", "", "account symbol sec_type side since limit"),
        ("get_open_orders", "", "account include_other_clients"),
        ("get_completed_orders", "", "account api_only limit"),
        # options
        ("calculate_implied_volatility", "contract option_price underlying_price", ""),
        ("calculate_option_price", "contract volatility underlying_price", ""),
        (
            "get_option_quotes",
            "underlying expiration",
            "right strike_min strike_max strikes_around_atm exchange limit",
        ),
        # orders
        ("preview_order", "order", "account"),
        (
            "preview_bracket_order",
            "contract action quantity take_profit_price stop_loss_price",
            "entry_type entry_price tif outside_rth model_code soft_dollar_tier account",
        ),
        ("preview_oca_group", "orders", "oca_type account"),
        (
            "preview_combo_order",
            "legs action quantity",
            "order_type limit_price tif model_code soft_dollar_tier account",
        ),
        ("preview_modify_order", "order_id changes", ""),
        ("preview_exercise_options", "contract action quantity", "override account"),
        ("preview_cancel_all_orders", "", "account scope"),
        ("submit_order", "token", ""),
        ("cancel_order", "order_id", ""),
        ("get_order_status", "", "order_id perm_id"),
        # advisor
        ("get_fa_config", "", "data_type"),
        ("preview_replace_fa_config", "xml", "data_type"),
        ("apply_fa_config", "token", ""),
        ("get_soft_dollar_tiers", "", ""),
        ("get_family_codes", "", ""),
        # admin
        ("reset_circuit_breaker", "reason", ""),
        ("list_display_groups", "", ""),
        ("subscribe_display_group", "group_id", ""),
        ("update_display_group", "subscription_id contract", ""),
        ("set_server_log_level", "level", ""),
    ]
}


def test_key_parameter_table_covers_every_tool() -> None:
    assert set(KEY_PARAMETERS) == FULL_TOOLS


async def test_key_parameters_keep_their_names(mcp_client: McpClientFactory) -> None:
    tools = await full_tool_list(mcp_client)
    for tool in tools:
        required_names, optional_names = KEY_PARAMETERS[tool.name]
        properties = set(tool.input_schema.get("properties", {}))
        required = set(tool.input_schema.get("required", []))
        missing = (required_names | optional_names) - properties
        assert not missing, f"{tool.name} lost parameters {sorted(missing)}"
        assert required_names <= required, f"{tool.name}: {sorted(required_names - required)}"
        assert not optional_names & required, f"{tool.name}: {sorted(optional_names & required)}"


# --- subscriptions across toolsets ------------------------------------------------------------


async def test_subscriptions_from_other_toolsets_use_the_generic_tools(
    mcp_client: McpClientFactory, fake_ib: MagicMock
) -> None:
    """A scanner (scanners), a bulletin stream (news) and a display group (admin)."""
    scanner = FakeScanner(fake_ib, rows_for("AAA", "BBB"))
    fake_ib.client.getReqId.side_effect = itertools.count(42).__next__

    async with mcp_client(profile="full") as client:

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = await client.call_tool(name, arguments)
            assert not result.is_error, f"{name}: {text_of(result)}"
            content: dict[str, Any] | None = result.structured_content
            assert content is not None
            return content

        handles = [
            SubscriptionOut.model_validate(await call(name, arguments))
            for name, arguments in (
                ("subscribe_scanner", {"scan_code": "TOP_PERC_GAIN"}),
                ("subscribe_news_bulletins", {}),
                ("subscribe_display_group", {"group_id": 1}),
            )
        ]
        group_req_id = fake_ib.client.subscribeToGroupEvents.call_args.args[0]
        for msg_id in (7, 8, 9):
            fake_ib.newsBulletinEvent.emit(NewsBulletin(msg_id, 2, "Exchange down", "ISLAND"))
        fake_ib.wrapper.displayGroupUpdated(group_req_id, "265598@SMART")

        listing = SubscriptionList.model_validate(await call("list_subscriptions", {}))
        data = {
            handle.kind: SubscriptionDataOut.model_validate(
                await call(
                    "get_subscription_data", {"subscription_id": handle.subscription_id, "limit": 2}
                )
            )
            for handle in handles
        }
        cancelled = [
            UnsubscribeResult.model_validate(
                await call("unsubscribe", {"subscription_id": handle.subscription_id})
            )
            for handle in handles
        ]
        gone = [
            await client.call_tool("get_subscription_data", {"subscription_id": h.subscription_id})
            for h in handles
        ]
        after = SubscriptionList.model_validate(await call("list_subscriptions", {}))

    kinds = ["scanner", "news_bulletins", "display_group"]
    assert [handle.kind for handle in handles] == kinds
    assert sorted(entry.kind for entry in listing.subscriptions) == sorted(kinds)
    assert listing.used == 3
    by_id = {entry.subscription_id: entry for entry in listing.subscriptions}
    assert by_id[handles[2].subscription_id].params == {"group_id": 1}

    for handle in handles:
        assert data[handle.kind].subscription_id == handle.subscription_id
        assert data[handle.kind].stale is False
    scan = ScannerSnapshot.model_validate(data["scanner"].data)
    assert [row.contract.symbol for row in scan.rows] == ["AAA", "BBB"]  # rows are not windowed
    bulletins = data["news_bulletins"].data
    assert bulletins["truncated"] is True  # limit=2 keeps the newest two
    snapshot = NewsBulletinSnapshot.model_validate(bulletins)
    assert [b.msg_id for b in snapshot.bulletins] == [8, 9]
    group = DisplayGroupSnapshot.model_validate(data["display_group"].data)
    assert group.current is not None
    assert group.current.con_id == 265598

    assert [[c.kind for c in result.cancelled] for result in cancelled] == [[k] for k in kinds]
    assert [result.remaining for result in cancelled] == [2, 1, 0]
    fake_ib.cancelScannerSubscription.assert_called_once_with(scanner.lists[0])
    fake_ib.cancelNewsBulletins.assert_called_once_with()
    fake_ib.client.unsubscribeFromGroupEvents.assert_called_once_with(group_req_id)
    for result in gone:
        assert result.is_error
        assert "subscription_not_found:" in text_of(result)
    assert after.used == 0
    assert after.subscriptions == []


# --- numbers in descriptions ------------------------------------------------------------------

# Limits the tool descriptions quote, built from the constants that enforce them, so a
# changed constant cannot leave a stale number in the text the model reads.
QUOTED_LIMITS: list[tuple[str, str]] = [
    ("get_quotes", f"up to {md.MAX_QUOTE_CONTRACTS} contracts"),
    ("get_subscription_data", f"(default {md.DATA_LIMIT_DEFAULT}, max {md.DATA_LIMIT_MAX})"),
    (
        "subscribe_bars",
        f"the newest {md.DATA_LIMIT_DEFAULT} by default; the server keeps up to {md.BARS_KEPT_MAX}",
    ),
    (
        "get_option_quotes",
        f"default {options.QUOTES_LIMIT_DEFAULT}, max {options.QUOTES_LIMIT_MAX}",
    ),
    ("get_option_quotes", f"(default {options.STRIKES_AROUND_ATM_DEFAULT})"),
    ("calculate_implied_volatility", f"within {options.CALCULATION_TIMEOUT} seconds"),
    ("calculate_option_price", f"within {options.CALCULATION_TIMEOUT} seconds"),
    ("get_historical_ticks", f"1-{history.TICKS_MAX}"),
    ("get_historical_ticks", f"at most {history.TICKS_MAX} ticks"),
    (
        "get_histogram",
        f"Default limit {history.HISTOGRAM_LIMIT_DEFAULT} price levels, at most "
        f"{history.HISTOGRAM_LIMIT_MAX}",
    ),
    ("get_trading_schedule", f"(1-{history.SCHEDULE_DAYS_MAX})"),
    (
        "get_historical_bars",
        f"about {history.PACING_MAX_REQUESTS} requests per {history.PACING_WINDOW / 60:g} "
        f"minutes, {history.PACING_BURST_MAX} per contract",
    ),
    (
        "get_historical_news",
        f"Default limit {news.DEFAULT_HEADLINES}, at most {MAX_HISTORICAL_HEADLINES}",
    ),
    ("get_news_article", f"max_chars (default {news.DEFAULT_ARTICLE_CHARS})"),
    (
        "get_fundamental_data",
        f"{fundamentals.DEFAULT_REPORT_CHARS}, max {fundamentals.MAX_REPORT_CHARS})",
    ),
    (
        "get_wsh_metadata",
        f"(default {fundamentals.DEFAULT_WSH_METADATA_CHARS}, max "
        f"{fundamentals.MAX_WSH_METADATA_CHARS})",
    ),
    (
        "get_wsh_events",
        f"(default {fundamentals.DEFAULT_WSH_EVENTS}, max {fundamentals.MAX_WSH_EVENTS})",
    ),
    ("run_scanner", f"(1-{MAX_SCANNER_ROWS})"),
    ("run_scanner", f"at most {MAX_SCANNER_ROWS} rows"),
    ("subscribe_scanner", f"{scanners.MAX_SCANNER_SUBSCRIPTIONS} scanner subscriptions"),
    (
        "get_scanner_parameters",
        f"Default limit {scanners.DEFAULT_PARAMETER_LIMIT}, at most {scanners.MAX_PARAMETER_LIMIT}",
    ),
    ("search_symbols", f"(default {contracts.SEARCH_LIMIT_DEFAULT},"),
    ("qualify_contract", f"up to {MAX_LISTED_CANDIDATES} candidates"),
]


async def test_descriptions_quote_the_limits_in_force(mcp_client: McpClientFactory) -> None:
    listed = {tool.name: tool for tool in await full_tool_list(mcp_client)}

    def text(name: str) -> str:
        tool = listed[name]
        parameters = tool.input_schema.get("properties", {}).values()
        words = [tool.description or "", *(str(p.get("description", "")) for p in parameters)]
        return " ".join(" ".join(words).split())

    stale = [(name, phrase) for name, phrase in QUOTED_LIMITS if phrase not in text(name)]
    assert stale == [], "descriptions no longer quote these limits"
