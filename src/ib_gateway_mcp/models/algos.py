"""IBKR algo strategies an order can run (SMART-routed MKT and LMT orders only).

Each model maps its fields to the IBKR tag/value pairs of the strategy
(:meth:`AdaptiveAlgo.tag_values` and so on); :data:`AlgoSpec` picks one by ``strategy``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ib_gateway_mcp.models._base import _Spec

__all__ = [
    "AdaptiveAlgo",
    "AlgoSpec",
    "ArrivalPxAlgo",
    "ClosePxAlgo",
    "PctVolAlgo",
    "TwapAlgo",
    "VwapAlgo",
]

_AlgoTime = Annotated[
    str,
    Field(
        min_length=1,
        max_length=40,
        pattern=r"^[0-9A-Za-z :/_+.-]+$",
        description="IBKR time, e.g. '09:45:00 US/Eastern' or '20261218-14:30:00' (UTC).",
    ),
]


def _flag(value: bool) -> str:
    return "1" if value else "0"


class AdaptiveAlgo(_Spec):
    """IBKR Adaptive algo: works the order between the bid and ask by urgency."""

    strategy: Literal["Adaptive"]
    priority: Literal["Urgent", "Normal", "Patient"] = Field(
        "Normal", description="Urgent fills faster; Patient waits for better prices."
    )

    def tag_values(self) -> list[tuple[str, str]]:
        """The algo parameters as IBKR tag/value pairs."""
        return [("adaptivePriority", self.priority)]


class TwapAlgo(_Spec):
    """Time-weighted average price: slices the order evenly over a time window."""

    strategy: Literal["Twap"]
    strategy_type: Literal[
        "Marketable", "Matching Midpoint", "Matching Same Side", "Matching Last"
    ] = Field("Marketable", description="Which prices the slices trade at.")
    start_time: _AlgoTime | None = None
    end_time: _AlgoTime | None = None
    allow_past_end_time: bool = Field(False, description="Keep working after end_time.")

    def tag_values(self) -> list[tuple[str, str]]:
        """The algo parameters as IBKR tag/value pairs."""
        return [
            ("strategyType", self.strategy_type),
            ("startTime", self.start_time or ""),
            ("endTime", self.end_time or ""),
            ("allowPastEndTime", _flag(self.allow_past_end_time)),
        ]


class VwapAlgo(_Spec):
    """Volume-weighted average price: trades along the day's volume profile."""

    strategy: Literal["Vwap"]
    max_pct_vol: float = Field(
        0.1, ge=0.01, le=0.5, description="Largest share of market volume (0.1 = 10%)."
    )
    start_time: _AlgoTime | None = None
    end_time: _AlgoTime | None = None
    allow_past_end_time: bool = Field(False, description="Keep working after end_time.")
    no_take_liq: bool = Field(False, description="Only add liquidity (never cross the spread).")

    def tag_values(self) -> list[tuple[str, str]]:
        """The algo parameters as IBKR tag/value pairs."""
        return [
            ("maxPctVol", f"{self.max_pct_vol:g}"),
            ("startTime", self.start_time or ""),
            ("endTime", self.end_time or ""),
            ("allowPastEndTime", _flag(self.allow_past_end_time)),
            ("noTakeLiq", _flag(self.no_take_liq)),
        ]


class ArrivalPxAlgo(_Spec):
    """Arrival price: aims at the price when the order arrived, by risk appetite."""

    strategy: Literal["ArrivalPx"]
    max_pct_vol: float = Field(
        0.1, ge=0.01, le=0.5, description="Largest share of market volume (0.1 = 10%)."
    )
    risk_aversion: Literal["Get Done", "Aggressive", "Neutral", "Passive"] = Field(
        "Neutral", description="Urgency: Get Done is most aggressive, Passive the least."
    )
    start_time: _AlgoTime | None = None
    end_time: _AlgoTime | None = None
    force_completion: bool = Field(False, description="Try to finish by the end of the day.")
    allow_past_end_time: bool = Field(False, description="Keep working after end_time.")

    def tag_values(self) -> list[tuple[str, str]]:
        """The algo parameters as IBKR tag/value pairs."""
        return [
            ("maxPctVol", f"{self.max_pct_vol:g}"),
            ("riskAversion", self.risk_aversion),
            ("startTime", self.start_time or ""),
            ("endTime", self.end_time or ""),
            ("forceCompletion", _flag(self.force_completion)),
            ("allowPastEndTime", _flag(self.allow_past_end_time)),
        ]


class PctVolAlgo(_Spec):
    """Percentage of volume: participates at a fixed share of the market's volume."""

    strategy: Literal["PctVol"]
    pct_vol: float = Field(
        0.1, ge=0.01, le=0.5, description="Target share of market volume (0.1 = 10%)."
    )
    start_time: _AlgoTime | None = None
    end_time: _AlgoTime | None = None
    no_take_liq: bool = Field(False, description="Only add liquidity (never cross the spread).")

    def tag_values(self) -> list[tuple[str, str]]:
        """The algo parameters as IBKR tag/value pairs."""
        return [
            ("pctVol", f"{self.pct_vol:g}"),
            ("startTime", self.start_time or ""),
            ("endTime", self.end_time or ""),
            ("noTakeLiq", _flag(self.no_take_liq)),
        ]


class ClosePxAlgo(_Spec):
    """Close price: aims at the closing price while limiting market impact."""

    strategy: Literal["ClosePx"]
    max_pct_vol: float = Field(
        0.1, ge=0.01, le=0.5, description="Largest share of market volume (0.1 = 10%)."
    )
    risk_aversion: Literal["Get Done", "Aggressive", "Neutral", "Passive"] = Field(
        "Neutral", description="Urgency: Get Done is most aggressive, Passive the least."
    )
    start_time: _AlgoTime | None = None
    force_completion: bool = Field(False, description="Try to finish by the close.")

    def tag_values(self) -> list[tuple[str, str]]:
        """The algo parameters as IBKR tag/value pairs."""
        return [
            ("maxPctVol", f"{self.max_pct_vol:g}"),
            ("riskAversion", self.risk_aversion),
            ("startTime", self.start_time or ""),
            ("forceCompletion", _flag(self.force_completion)),
        ]


AlgoSpec = Annotated[
    AdaptiveAlgo | TwapAlgo | VwapAlgo | ArrivalPxAlgo | PctVolAlgo | ClosePxAlgo,
    Field(discriminator="strategy"),
]
"""An IBKR algo, chosen by ``strategy``; only for SMART-routed MKT and LMT orders."""
