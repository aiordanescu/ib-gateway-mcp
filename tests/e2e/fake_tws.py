"""A minimal, protocol-faithful fake of the TWS / IB Gateway API socket, for end-to-end tests.

Unit tests replace ``ib_async.IB`` with a mock, so ib_async's own handshake, framing and
decoder never run there. :class:`FakeTws` listens on ``127.0.0.1`` and speaks the wire
protocol that ib_async 2.1.0 speaks, so the real client code path runs end to end:

* **Handshake.** The client sends ``b"API\\0"`` followed by a length-prefixed version
  range (``v157..178``). The server answers with one message of two fields, the
  negotiated server version and the connection time. The client then sends
  ``startApi`` (message 71: version 2, client id, optional capabilities), and the server
  answers with ``managedAccounts`` (15) and ``nextValidId`` (9); ib_async considers the
  session ready once both have arrived. Like a real gateway, the fake then reports its
  data farms as connected (informational errors 2104, 2106 and 2158).
* **Framing.** Every message is a 4-byte big-endian length followed by NUL-terminated
  ASCII fields; the first field is the message id.
* **Requests answered.** Everything ``IB.connectAsync`` sends during its startup sync
  (positions, account updates, account updates per account, executions, and, for a
  read-write session, open and completed orders), plus ``reqCurrentTime``,
  ``reqContractDetails`` (for the instruments in :attr:`FakeTws.contracts`; anything
  else gets error 200; stocks and bonds, the latter with IBKR's fractional coupons),
  ``reqMarketRule``, ``reqUserInfo``, ``reqAccountSummary`` (end marker only),
  ``reqManagedAccts`` and ``reqIds``. Like IB Gateway, the fake ignores (no answer, no
  error) a ``reqCurrentTime`` that arrives within :attr:`FakeTws.current_time_window`
  seconds (1 s by default) of the last one it answered. ``placeOrder`` is answered only when
  :attr:`FakeTws.read_only_api` is set (warning 321, as a gateway with "Read-Only API"
  ticked does); the fake never executes or acknowledges orders. Other messages are
  recorded in :attr:`FakeSession.received` and left unanswered.
* **Failure modes.** A client id already used by another open session (or listed in
  :attr:`FakeTws.client_ids_in_use`) gets error 326 and a closed socket, as TWS does.
  :attr:`FakeTws.mode` makes new connections close right after the TCP accept (what
  ib-gateway-docker's socat relay does while the gateway is down) or hang before the
  handshake reply (a gateway that is starting or waiting on 2FA). :meth:`FakeTws.refuse`
  closes the listening socket so connections are refused; :meth:`FakeTws.drop` closes
  open sessions; :attr:`FakeSession.hung` makes one session stop answering while the
  socket stays open; :meth:`FakeTws.emit_error` sends any error or notice (1100, 1102,
  2110, 321...) to the open sessions.

Message layouts follow ib_async 2.1.0's ``decoder.py`` for server versions 166 to 178
(the error message carries the ``advancedOrderRejectJson`` field from 166 on; contract
details have no version field from 164 on). The default server version is 178, the
highest ib_async 2.1.0 negotiates, which any current gateway reaches.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import struct
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

__all__ = [
    "AAPL",
    "CLIENT_ID_IN_USE",
    "CURRENT_TIME_WINDOW",
    "NO_SECURITY_DEFINITION",
    "PAPER_ACCOUNT",
    "READ_ONLY_API",
    "SERVER_VERSION",
    "FakeBond",
    "FakeContract",
    "FakeSession",
    "FakeStock",
    "FakeTws",
    "Mode",
    "encode",
]

SERVER_VERSION = 178
"""The server version the fake offers by default (ib_async 2.1.0's maximum)."""
MIN_SERVER_VERSION = 166
"""The lowest server version whose message layouts the fake reproduces."""
PAPER_ACCOUNT = "DU1234567"
"""Placeholder paper account id."""
CURRENT_TIME_WINDOW = 1.0
"""Seconds after a ``currentTime`` answer during which IB Gateway ignores ``reqCurrentTime``."""

Mode = Literal["normal", "accept_close", "silent"]
"""How the fake treats new connections:

* ``normal``: complete the handshake and serve requests.
* ``accept_close``: close the socket right after the TCP accept.
* ``silent``: read the client's greeting, then never answer (until the client gives up).
"""

# Message ids, client to server (ib_async client.py).
_PLACE_ORDER = 3
_REQ_OPEN_ORDERS = 5
_REQ_ACCOUNT_UPDATES = 6
_REQ_EXECUTIONS = 7
_REQ_IDS = 8
_REQ_CONTRACT_DETAILS = 9
_REQ_ALL_OPEN_ORDERS = 16
_REQ_MANAGED_ACCTS = 17
_REQ_CURRENT_TIME = 49
_REQ_POSITIONS = 61
_REQ_ACCOUNT_SUMMARY = 62
_START_API = 71
_REQ_ACCOUNT_UPDATES_MULTI = 76
_REQ_MARKET_RULE = 91
_REQ_COMPLETED_ORDERS = 99
_REQ_USER_INFO = 104

# Message ids, server to client (ib_async decoder.py).
_ERR_MSG = 4
_ACCT_VALUE = 6
_ACCT_UPDATE_TIME = 8
_NEXT_VALID_ID = 9
_CONTRACT_DATA = 10
_MANAGED_ACCTS = 15
_BOND_CONTRACT_DATA = 18
_CURRENT_TIME = 49
_CONTRACT_DATA_END = 52
_OPEN_ORDER_END = 53
_ACCT_DOWNLOAD_END = 54
_EXECUTION_DATA_END = 55
_POSITION_END = 62
_ACCOUNT_SUMMARY_END = 64
_ACCOUNT_UPDATE_MULTI_END = 74
_MARKET_RULE = 93
_COMPLETED_ORDERS_END = 102
_USER_INFO = 107

# TWS API codes and the texts a gateway sends with them.
CLIENT_ID_IN_USE = 326
READ_ONLY_API = 321
NO_SECURITY_DEFINITION = 200
MESSAGES: dict[int, str] = {
    NO_SECURITY_DEFINITION: "No security definition has been found for the request",
    READ_ONLY_API: (
        "Error validating request.-'bN' : cause - The API interface is currently in Read-Only mode."
    ),
    CLIENT_ID_IN_USE: (
        "Unable to connect as the client id is already in use. Retry with a unique client id."
    ),
    1100: "Connectivity between IB and Trader Workstation has been lost.",
    1101: "Connectivity between IB and Trader Workstation has been restored - data lost.",
    1102: (
        "Connectivity between IB and Trader Workstation has been restored - data maintained. "
        "All data farms are connected: usfarm; ushmds; secdefil."
    ),
    2104: "Market data farm connection is OK:usfarm",
    2106: "HMDS data farm connection is OK:ushmds",
    2110: (
        "Connectivity between Trader Workstation and server is broken. It will be restored "
        "automatically."
    ),
    2158: "Sec-def data farm connection is OK:secdefil",
}
FARM_OK_CODES = (2104, 2106, 2158)
"""The notices a gateway sends right after ``startApi`` when its data farms are up."""

_GREETING = re.compile(r"v(\d+)(?:\.\.(\d+))?(?: .*)?")
_POLL = 0.005


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def encode(*fields: object) -> bytes:
    """Frame one message: a 4-byte big-endian length, then NUL-terminated fields."""
    payload = "".join(f"{_text(field)}\0" for field in fields).encode()
    return struct.pack(">I", len(payload)) + payload


def _session_hours(days: int = 7) -> tuple[str, str]:
    """US equity trading and liquid hours in TWS's format, from today for ``days`` days."""
    today = datetime.now(UTC).date()
    trading: list[str] = []
    liquid: list[str] = []
    for offset in range(days):
        day = (today + timedelta(days=offset)).strftime("%Y%m%d")
        if (today + timedelta(days=offset)).weekday() >= 5:
            trading.append(f"{day}:CLOSED")
            liquid.append(f"{day}:CLOSED")
        else:
            trading.append(f"{day}:0400-{day}:2000")
            liquid.append(f"{day}:0930-{day}:1600")
    return ";".join(trading), ";".join(liquid)


@dataclass(frozen=True)
class FakeStock:
    """A stock the fake gateway knows, with the details a real gateway reports."""

    con_id: int
    symbol: str
    long_name: str
    primary_exchange: str
    currency: str = "USD"
    valid_exchanges: tuple[str, ...] = ("SMART", "NASDAQ", "ARCA", "BATS", "IEX")
    market_rule_id: int = 26
    min_tick: float = 0.01
    isin: str | None = None
    industry: str = ""
    category: str = ""
    subcategory: str = ""
    market_name: str = "NMS"
    time_zone_id: str = "US/Eastern"
    order_types: str = "LMT,MKT,MOC,LOC,MIT,LIT,REL,STP,STPLMT,TRAIL,TRAILLIMIT,MIDPX,PEGMID"

    def matches(self, request: _ContractRequest) -> bool:
        """Whether ``reqContractDetails`` for ``request`` returns this stock."""
        if request.sec_type not in ("", "STK"):
            return False
        if request.con_id:
            return request.con_id == self.con_id
        if request.sec_id_type:
            return request.sec_id_type == "ISIN" and request.sec_id == self.isin
        symbol = (request.symbol or request.local_symbol).upper()
        return (
            symbol == self.symbol
            and request.currency in ("", self.currency)
            and request.primary_exchange in ("", self.primary_exchange)
            and request.exchange in ("", *self.valid_exchanges)
        )

    def details_fields(self, req_id: int, exchange: str) -> tuple[object, ...]:
        """One ``contractData`` message (id 10) for this stock, server version >= 164."""
        trading_hours, liquid_hours = _session_hours()
        sec_ids: tuple[object, ...] = (1, "ISIN", self.isin) if self.isin else (0,)
        rules = ",".join(str(self.market_rule_id) for _ in self.valid_exchanges)
        return (
            _CONTRACT_DATA,
            req_id,
            self.symbol,
            "STK",
            "",  # lastTradeDateOrContractMonth [+ lastTradeTime + time zone]
            0,  # strike
            "",  # right
            exchange,
            self.currency,
            self.symbol,  # localSymbol
            self.market_name,
            self.market_name,  # tradingClass
            self.con_id,
            self.min_tick,
            "",  # multiplier
            self.order_types,
            ",".join(self.valid_exchanges),
            1,  # priceMagnifier
            0,  # underConId
            self.long_name,
            self.primary_exchange,
            "",  # contractMonth
            self.industry,
            self.category,
            self.subcategory,
            self.time_zone_id,
            trading_hours,
            liquid_hours,
            "",  # evRule
            "",  # evMultiplier
            *sec_ids,
            1,  # aggGroup
            "",  # underSymbol
            "",  # underSecType
            rules,  # marketRuleIds, aligned with validExchanges
            "",  # realExpirationDate
            "COMMON",  # stockType
            "0.0001",  # minSize
            "0.0001",  # sizeIncrement
            "100",  # suggestedSizeIncrement
        )


AAPL = FakeStock(
    con_id=265598,
    symbol="AAPL",
    long_name="APPLE INC",
    primary_exchange="NASDAQ",
    isin="US0378331005",
    industry="Technology",
    category="Computers",
    subcategory="Computers",
)
"""The stock the fake knows by default."""


@dataclass(frozen=True)
class FakeBond:
    """A bond the fake gateway knows (``bondContractData``, message 18)."""

    con_id: int
    issuer: str
    cusip: str
    coupon: str
    """As the gateway sends it, e.g. ``"4.25"``."""
    maturity: str
    """``YYYYMMDD``."""
    long_name: str
    currency: str = "USD"
    issue_date: str = "20200315"
    bond_type: str = "CORP"
    coupon_type: str = "FIXED"

    def matches(self, request: _ContractRequest) -> bool:
        """Whether ``reqContractDetails`` for ``request`` returns this bond."""
        if request.sec_type not in ("", "BOND"):
            return False
        if request.con_id:
            return request.con_id == self.con_id
        if request.sec_id_type:
            return request.sec_id_type == "CUSIP" and request.sec_id == self.cusip
        return request.sec_type == "BOND" and request.symbol.upper() in (self.issuer, self.cusip)

    def details_fields(self, req_id: int, exchange: str) -> tuple[object, ...]:
        """One ``bondContractData`` message for this bond, server version >= 164."""
        return (
            _BOND_CONTRACT_DATA,
            req_id,
            self.issuer,
            "BOND",
            self.cusip,
            self.coupon,
            self.maturity,  # maturity [+ last trade time + time zone]
            self.issue_date,
            "",  # ratings
            self.bond_type,
            self.coupon_type,
            0,  # convertible
            1,  # callable
            0,  # putable
            "",  # descAppend
            exchange,
            self.currency,
            "CORP",  # marketName
            "CORP",  # tradingClass
            self.con_id,
            0.001,  # minTick
            "LMT,MKT",  # orderTypes
            "SMART",  # validExchanges
            "",  # nextOptionDate
            "",  # nextOptionType
            0,  # nextOptionPartial
            "",  # notes
            self.long_name,
            "",  # evRule
            "",  # evMultiplier
            1,  # secIdList: one tag/value pair
            "CUSIP",
            self.cusip,
            1,  # aggGroup
            "",  # marketRuleIds
            "1000",  # minSize
            "1000",  # sizeIncrement
            "1000",  # suggestedSizeIncrement
        )


FakeContract = FakeStock | FakeBond
"""Anything ``reqContractDetails`` can return."""


@dataclass(frozen=True)
class _ContractRequest:
    con_id: int
    symbol: str
    sec_type: str
    exchange: str
    primary_exchange: str
    currency: str
    local_symbol: str
    sec_id_type: str
    sec_id: str

    @classmethod
    def parse(cls, fields: list[str]) -> _ContractRequest:
        """Read the contract of a ``reqContractDetails`` message (id 9, version 8)."""
        (
            con_id,
            symbol,
            sec_type,
            _expiry,
            _strike,
            _right,
            _multiplier,
            exchange,
            primary_exchange,
            currency,
            local_symbol,
            _trading_class,
        ) = fields[3:15]
        _include_expired, sec_id_type, sec_id = fields[15:18]
        return cls(
            con_id=int(con_id or 0),
            symbol=symbol,
            sec_type=sec_type,
            exchange=exchange,
            primary_exchange=primary_exchange,
            currency=currency,
            local_symbol=local_symbol,
            sec_id_type=sec_id_type,
            sec_id=sec_id,
        )


class FakeSession:
    """One client connection to the fake gateway."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self.server_version: int | None = None
        """The negotiated server version, once the client has greeted."""
        self.client_id: int | None = None
        """The client id from ``startApi``, once the session is ready."""
        self.received: list[list[str]] = []
        """Every message the client sent after the handshake, as field lists."""
        self.hung = False
        """While True the session reads requests but answers nothing (a hung gateway)."""
        self.closed = False
        """True once the fake has closed its side of the socket."""
        self.current_time_answered_at: float | None = None
        """``time.monotonic()`` of the last ``currentTime`` answer on this session."""
        self.ignored_current_time = 0
        """How many ``reqCurrentTime`` requests were ignored as too soon after an answer."""

    @property
    def ready(self) -> bool:
        """True once ``startApi`` has been answered and until the socket closes."""
        return self.client_id is not None and not self.closed

    def requests(self, message_id: int) -> list[list[str]]:
        """The received messages with ``message_id``."""
        return [fields for fields in self.received if fields and fields[0] == str(message_id)]

    def send(self, *fields: object) -> None:
        """Send one message (dropped silently once the socket is closed)."""
        if not self.closed:
            self._writer.write(encode(*fields))

    def send_error(self, code: int, message: str | None = None, *, req_id: int = -1) -> None:
        """Send an error or notice (message 4, version 2), as the gateway reports them."""
        text = message if message is not None else MESSAGES.get(code, f"Error {code}")
        fields: list[object] = [_ERR_MSG, 2, req_id, code, text]
        if (self.server_version or SERVER_VERSION) >= MIN_SERVER_VERSION:
            fields.append("")  # advancedOrderRejectJson
        self.send(*fields)

    def close(self) -> None:
        """Close the socket (an orderly FIN, as a gateway shutting down sends)."""
        if not self.closed:
            self.closed = True
            self._writer.close()

    async def read_message(self) -> list[str] | None:
        """Read one framed message as its fields; None once the client has closed."""
        try:
            header = await self._reader.readexactly(4)
            payload = await self._reader.readexactly(struct.unpack(">I", header)[0])
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        fields = payload.decode(errors="backslashreplace").split("\0")
        fields.pop()  # the terminator after the last field
        return fields

    async def read_greeting(self) -> tuple[int, int] | None:
        """Read ``API\\0`` plus the version range; None if the client sent something else."""
        try:
            prefix = await self._reader.readexactly(4)
            if prefix != b"API\0":
                return None
            header = await self._reader.readexactly(4)
            payload = await self._reader.readexactly(struct.unpack(">I", header)[0])
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        match = _GREETING.fullmatch(payload.decode(errors="replace"))
        if match is None:
            return None
        low = int(match.group(1))
        return low, int(match.group(2) or low)

    async def wait_for_eof(self) -> None:
        """Read and discard until the client closes the socket."""
        with contextlib.suppress(ConnectionError):
            await self._reader.read()


class FakeTws:
    """An in-process fake of the TWS / IB Gateway API port.

    Use it as an async context manager or call :meth:`start` and :meth:`stop`; connect
    to ``127.0.0.1:{port}``. Attributes that shape behaviour can change at any time and
    apply to the next connection (``mode``, ``client_ids_in_use``) or the next message
    (``read_only_api``).

    Args:
        server_version: The highest server version to offer; the session uses the lower
            of this and the client's maximum. 166 to 178 are reproduced faithfully.
        accounts: The managed accounts reported at ``startApi``.
        contracts: The instruments ``reqContractDetails`` knows.
        market_rules: Market rule id to ``(low_edge, increment)`` rows.
        welcome_codes: Notices sent right after ``startApi`` (by default the data farm
            OK messages; add 1100 to simulate a gateway cut off from IBKR).
        current_time_window: Seconds after a ``currentTime`` answer during which a
            session's ``reqCurrentTime`` is ignored, as IB Gateway does (0 answers all).
    """

    def __init__(
        self,
        *,
        server_version: int = SERVER_VERSION,
        accounts: Iterable[str] = (PAPER_ACCOUNT,),
        contracts: Iterable[FakeContract] = (AAPL,),
        market_rules: dict[int, tuple[tuple[float, float], ...]] | None = None,
        welcome_codes: Iterable[int] = FARM_OK_CODES,
        current_time_window: float = CURRENT_TIME_WINDOW,
    ) -> None:
        self.server_version = server_version
        self.accounts = list(accounts)
        self.contracts = list(contracts)
        self.market_rules = market_rules if market_rules is not None else {26: ((0.0, 0.01),)}
        self.welcome_codes = list(welcome_codes)
        self.current_time_window = current_time_window
        self.mode: Mode = "normal"
        self.client_ids_in_use: set[int] = set()
        """Client ids rejected with error 326 as if another application held them."""
        self.read_only_api = False
        """While True, ``placeOrder`` is answered with warning 321 (Read-Only API)."""
        self.next_order_id = 1
        self.white_branding_id = ""
        """What ``reqUserInfo`` answers (empty for a regular IBKR login)."""
        self.connections: list[FakeSession] = []
        """Every connection accepted, in order, whatever became of it."""
        self._server: asyncio.Server | None = None
        self._port: int | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._handlers: dict[int, Callable[[FakeSession, list[str]], None]] = {
            _START_API: self._start_api,
            _REQ_CURRENT_TIME: self._current_time,
            _REQ_CONTRACT_DETAILS: self._contract_details,
            _REQ_POSITIONS: self._positions,
            _REQ_ACCOUNT_UPDATES: self._account_updates,
            _REQ_ACCOUNT_UPDATES_MULTI: self._account_updates_multi,
            _REQ_EXECUTIONS: self._executions,
            _REQ_OPEN_ORDERS: self._open_orders,
            _REQ_ALL_OPEN_ORDERS: self._open_orders,
            _REQ_COMPLETED_ORDERS: self._completed_orders,
            _REQ_ACCOUNT_SUMMARY: self._account_summary,
            _REQ_MARKET_RULE: self._market_rule,
            _REQ_MANAGED_ACCTS: self._managed_accounts,
            _REQ_IDS: self._next_valid_id,
            _PLACE_ORDER: self._place_order,
            _REQ_USER_INFO: self._user_info,
        }

    # --- lifecycle ---------------------------------------------------------------------

    @property
    def port(self) -> int:
        """The TCP port the fake listens on (fixed across :meth:`refuse`/:meth:`resume`)."""
        if self._port is None:
            raise RuntimeError("FakeTws is not started")
        return self._port

    async def start(self) -> None:
        """Listen on an ephemeral port on 127.0.0.1."""
        await self._listen(0)

    async def stop(self) -> None:
        """Stop listening, close every session and wait for their handlers to end."""
        await self._close_listener()
        self.drop()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=2.0)
        for task in list(self._tasks):
            task.cancel()

    async def refuse(self) -> None:
        """Close the listening socket: new connections are refused (open ones stay)."""
        await self._close_listener()

    async def resume(self) -> None:
        """Listen again on the same port after :meth:`refuse`."""
        await self._listen(self.port)

    async def __aenter__(self) -> FakeTws:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def _listen(self, port: int) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", port)
        self._port = self._server.sockets[0].getsockname()[1]

    async def _close_listener(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

    # --- control -----------------------------------------------------------------------

    @property
    def sessions(self) -> list[FakeSession]:
        """Open sessions that completed ``startApi``, oldest first."""
        return [session for session in self.connections if session.ready]

    @property
    def current(self) -> FakeSession:
        """The newest ready session."""
        sessions = self.sessions
        if not sessions:
            raise AssertionError("no ready session")
        return sessions[-1]

    def ready_count(self) -> int:
        """How many connections ever completed ``startApi`` (open or not)."""
        return sum(1 for session in self.connections if session.client_id is not None)

    async def wait_for_ready(self, count: int = 1, timeout: float = 3.0) -> FakeSession:
        """Wait until ``count`` connections in total have completed ``startApi``.

        Returns the newest ready session.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while self.ready_count() < count or not self.sessions:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"expected {count} ready session(s), saw {self.ready_count()}")
            await asyncio.sleep(_POLL)
        return self.current

    def emit_error(self, code: int, message: str | None = None, *, req_id: int = -1) -> None:
        """Send an error or notice to every ready session."""
        for session in self.sessions:
            session.send_error(code, message, req_id=req_id)

    def drop(self) -> None:
        """Close every open connection, as a gateway restart or network cut does."""
        for session in self.connections:
            session.close()

    # --- connection handling -----------------------------------------------------------

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        session = FakeSession(reader, writer)
        self.connections.append(session)
        try:
            await self._serve(session)
        finally:
            session.close()

    async def _serve(self, session: FakeSession) -> None:
        if self.mode == "accept_close":
            return
        greeting = await session.read_greeting()
        if greeting is None:
            return
        _client_min, client_max = greeting
        if self.mode == "silent":
            await session.wait_for_eof()
            return
        session.server_version = min(self.server_version, client_max)
        stamp = datetime.now(UTC).strftime("%Y%m%d %H:%M:%S GMT")
        session.send(session.server_version, stamp)
        while (fields := await session.read_message()) is not None:
            session.received.append(fields)
            if session.hung or not fields:
                continue
            handler = self._handlers.get(int(fields[0]))
            if handler is not None:
                handler(session, fields)

    # --- request handlers --------------------------------------------------------------

    def _start_api(self, session: FakeSession, fields: list[str]) -> None:
        # startApi: 71, version 2, clientId, optionalCapabilities
        client_id = int(fields[2])
        taken = {other.client_id for other in self.sessions if other is not session}
        if client_id in self.client_ids_in_use or client_id in taken:
            session.send_error(CLIENT_ID_IN_USE)
            session.close()
            return
        session.client_id = client_id
        self._managed_accounts(session, fields)
        self._next_valid_id(session, fields)
        for code in self.welcome_codes:
            session.send_error(code)

    def _managed_accounts(self, session: FakeSession, _fields: list[str]) -> None:
        session.send(_MANAGED_ACCTS, 1, ",".join(self.accounts))

    def _next_valid_id(self, session: FakeSession, _fields: list[str]) -> None:
        session.send(_NEXT_VALID_ID, 1, self.next_order_id)

    def _current_time(self, session: FakeSession, _fields: list[str]) -> None:
        # IB Gateway 10.45 drops a reqCurrentTime within a second of its last answer,
        # silently; an ignored request does not restart that second.
        now = time.monotonic()
        last = session.current_time_answered_at
        if last is not None and now - last < self.current_time_window:
            session.ignored_current_time += 1
            return
        session.current_time_answered_at = now
        session.send(_CURRENT_TIME, 1, int(time.time()))

    def _contract_details(self, session: FakeSession, fields: list[str]) -> None:
        # reqContractDetails: 9, version 8, reqId, contract (12 fields), includeExpired,
        # secIdType, secId, [issuerId from server version 176]
        req_id = int(fields[2])
        request = _ContractRequest.parse(fields)
        matches = [contract for contract in self.contracts if contract.matches(request)]
        if not matches:
            session.send_error(NO_SECURITY_DEFINITION, req_id=req_id)
            return
        for contract in matches:
            session.send(*contract.details_fields(req_id, request.exchange or "SMART"))
        session.send(_CONTRACT_DATA_END, 1, req_id)

    def _positions(self, session: FakeSession, _fields: list[str]) -> None:
        session.send(_POSITION_END, 1)

    def _account_updates(self, session: FakeSession, fields: list[str]) -> None:
        # reqAccountUpdates: 6, version 2, subscribe, account
        subscribe, account = fields[2], fields[3]
        if subscribe != "1":
            return
        for key, value in (("NetLiquidation", "100000.00"), ("TotalCashValue", "100000.00")):
            session.send(_ACCT_VALUE, 2, key, value, "USD", account)
        session.send(_ACCT_UPDATE_TIME, 1, datetime.now(UTC).strftime("%H:%M"))
        session.send(_ACCT_DOWNLOAD_END, 1, account)

    def _account_updates_multi(self, session: FakeSession, fields: list[str]) -> None:
        # reqAccountUpdatesMulti: 76, version 1, reqId, account, modelCode, ledgerAndNLV
        session.send(_ACCOUNT_UPDATE_MULTI_END, 1, int(fields[2]))

    def _executions(self, session: FakeSession, fields: list[str]) -> None:
        # reqExecutions: 7, version 3, reqId, filter...
        session.send(_EXECUTION_DATA_END, 1, int(fields[2]))

    def _open_orders(self, session: FakeSession, _fields: list[str]) -> None:
        session.send(_OPEN_ORDER_END, 1)

    def _completed_orders(self, session: FakeSession, _fields: list[str]) -> None:
        session.send(_COMPLETED_ORDERS_END)

    def _account_summary(self, session: FakeSession, fields: list[str]) -> None:
        # reqAccountSummary: 62, version 1, reqId, group, tags
        session.send(_ACCOUNT_SUMMARY_END, 1, int(fields[2]))

    def _market_rule(self, session: FakeSession, fields: list[str]) -> None:
        # reqMarketRule: 91, marketRuleId (no version field)
        rule_id = int(fields[1])
        rows = self.market_rules.get(rule_id)
        if rows is None:
            return
        flat = [value for row in rows for value in row]
        session.send(_MARKET_RULE, rule_id, len(rows), *flat)

    def _place_order(self, session: FakeSession, fields: list[str]) -> None:
        # placeOrder: 3, orderId, contract... (no version field at these server versions)
        if self.read_only_api:
            session.send_error(READ_ONLY_API, req_id=int(fields[1]))

    def _user_info(self, session: FakeSession, fields: list[str]) -> None:
        # reqUserInfo: 104, reqId (no version field)
        session.send(_USER_INFO, int(fields[1]), self.white_branding_id)
