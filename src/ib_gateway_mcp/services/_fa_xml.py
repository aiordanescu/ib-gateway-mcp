"""Financial-advisor (FA) group XML: parsing, checks, diffs and the confirmation text.

IBKR's ``requestFA``/``replaceFA`` exchange whole XML documents. The advisor service
parses them here (DTDs refused, so no entity can be declared or expanded), checks a
proposed configuration, and describes what replacing the current one would change.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ib_gateway_mcp._util import clean_float, clean_str
from ib_gateway_mcp.models.advisor import FaDataType, FaGroupChange
from ib_gateway_mcp.models.common import quoted

KNOWN_FA_METHODS: Final = frozenset(
    {
        "AvailableEquity",
        "Equal",
        "NetLiq",
        "ContractsOrShares",
        "Ratio",
        "Percent",
        "MonetaryAmount",
        "PctChange",
    }
)
"""Allocation methods IBKR documents for FA groups; others draw a preview warning."""

_ACCOUNT_ID = re.compile(r"[A-Za-z0-9._-]{1,32}")
_MAX_NAME_CHARS: Final = 100
_DTD = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_MAX_SUMMARY_LINES: Final = 20
_MAX_LISTED: Final = 10


class _XmlError(ValueError):
    """FA XML that cannot be used; the message says why."""


@dataclass(frozen=True, slots=True)
class _Member:
    account: str
    amount: float | None


@dataclass(frozen=True, slots=True)
class _Group:
    name: str
    method: str | None
    members: tuple[_Member, ...]

    def accounts(self) -> list[str]:
        return [member.account for member in self.members]


def _tag(element: ET.Element) -> str:
    """An element's local name, lower-cased (namespaces and case do not matter)."""
    return str(element.tag).rsplit("}", 1)[-1].lower()


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _tag(child) == name), None)


def _child_text(element: ET.Element, name: str) -> str | None:
    child = _child(element, name)
    return clean_str(child.text) if child is not None else None


def _parse(xml: str) -> ET.Element | None:
    """Parse FA XML; None for a blank document.

    DTDs are refused, so no entity can be declared or expanded.
    """
    if not xml.strip():
        return None
    if _DTD.search(xml):
        raise _XmlError("DOCTYPE and ENTITY declarations are not allowed")
    try:
        return ET.fromstring(xml)  # noqa: S314 (no DTD, see above)
    except ET.ParseError as exc:
        raise _XmlError(f"not well-formed XML ({exc})") from None


def _groups_from(xml: str) -> list[_Group]:
    """Read ``<ListOfGroups>`` XML, in IBKR's current (``<Account><acct>``) or legacy
    (``<String>``) member format."""
    root = _parse(xml)
    if root is None:
        return []
    if _tag(root) != "listofgroups":
        raise _XmlError(f"the root element must be <ListOfGroups>, not <{root.tag}>")
    groups: list[_Group] = []
    for element in root:
        if _tag(element) != "group":
            continue
        members: list[_Member] = []
        accounts = _child(element, "listofaccts")
        for item in accounts if accounts is not None else ():
            kind = _tag(item)
            if kind == "string":
                account, amount = clean_str(item.text), None
            elif kind == "account":
                account = _child_text(item, "acct")
                amount = clean_float(_child_text(item, "amount"))
            else:
                continue
            if account:
                members.append(_Member(account, amount))
        groups.append(
            _Group(
                name=_child_text(element, "name") or "",
                method=_child_text(element, "defaultmethod"),
                members=tuple(members),
            )
        )
    return groups


def _aliases_from(xml: str) -> list[tuple[str, str]]:
    """Read ``<ListOfAccountAliases>`` XML as ``(account, alias)`` pairs."""
    root = _parse(xml)
    if root is None:
        return []
    if _tag(root) != "listofaccountaliases":
        raise _XmlError(f"the root element must be <ListOfAccountAliases>, not <{root.tag}>")
    pairs: list[tuple[str, str]] = []
    for element in root.iter():
        if _tag(element) != "accountalias":
            continue
        account = _child_text(element, "account")
        alias = _child_text(element, "alias")
        if account and alias:
            pairs.append((account, alias))
    return pairs


_FA_ROOTS: Final[dict[str, str]] = {"groups": "listofgroups", "aliases": "listofaccountaliases"}


def _root_fits(xml: str, data_type: FaDataType) -> bool:
    """Whether ``xml`` is (or could be) the answer for ``data_type``: its root element."""
    match = re.search(r"<\s*([A-Za-z_][\w.:-]*)", re.sub(r"<[?!][^>]*>", "", xml))
    if match is None:
        return True  # blank or unparsable: the parsers decide
    return match.group(1).rsplit(":", 1)[-1].lower() == _FA_ROOTS[data_type]


def _text_problem(what: str, value: str) -> str | None:
    """Why a group name or method cannot go into a confirmation prompt, or None."""
    if len(value) > _MAX_NAME_CHARS:
        return f"{what} {quoted(value[:40])}... is longer than {_MAX_NAME_CHARS} characters"
    if not value.isprintable():
        return f"{what} {quoted(value)} holds line breaks or invisible characters"
    return None


def _problems(groups: Sequence[_Group]) -> list[str]:
    """Errors that make a proposed group configuration unusable.

    Names and methods must be one line of visible text and accounts plain ids: they
    are shown to the human who approves the change.
    """
    problems: list[str] = []
    if not groups:
        problems.append("it defines no <Group>; IBKR would delete every group")
    seen: set[str] = set()
    for index, group in enumerate(groups, start=1):
        if not group.name:
            problems.append(f"group #{index} has no <name>")
        elif group.name in seen:
            problems.append(f"group {quoted(group.name)} is defined twice")
        seen.add(group.name)
        for what, value in (("group name", group.name), ("allocation method", group.method)):
            problem = _text_problem(what, value) if value else None
            if problem is not None:
                problems.append(problem)
        accounts = group.accounts()
        malformed = [a for a in accounts if not _ACCOUNT_ID.fullmatch(a)]
        if malformed:
            listed = ", ".join(quoted(a[:40]) for a in malformed)
            problems.append(f"group {quoted(group.name)} lists malformed account ids: {listed}")
        duplicates = sorted({a for a in accounts if accounts.count(a) > 1})
        if duplicates:
            problems.append(f"group {quoted(group.name)} lists {', '.join(duplicates)} twice")
    return problems


def _warnings(groups: Sequence[_Group], managed: frozenset[str]) -> list[str]:
    warnings: list[str] = []
    for group in groups:
        name = quoted(group.name)
        if group.method is None:
            warnings.append(f"group {name} has no <defaultMethod>")
        elif group.method not in KNOWN_FA_METHODS:
            warnings.append(
                f"group {name} uses allocation method {quoted(group.method)}, which IBKR "
                f"does not document ({', '.join(sorted(KNOWN_FA_METHODS))}); it may be rejected"
            )
        if not group.members:
            warnings.append(f"group {name} has no accounts")
        unknown = [a for a in group.accounts() if a not in managed]
        if unknown:
            warnings.append(
                f"group {name} lists accounts this login does not manage: {', '.join(unknown)}"
            )
    return warnings


def _digest(groups: Sequence[_Group]) -> str:
    """A fingerprint of a group configuration, independent of XML formatting."""
    canonical = sorted(
        (g.name, g.method or "", sorted((m.account, m.amount or 0.0) for m in g.members))
        for g in groups
    )
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


def _diff(
    before: Sequence[_Group], after: Sequence[_Group]
) -> tuple[list[FaGroupChange], list[str]]:
    """Group-by-group changes from ``before`` to ``after``, and the unchanged group names."""
    old = {group.name: group for group in before}
    new = {group.name: group for group in after}
    changes: list[FaGroupChange] = []
    unchanged: list[str] = []
    for name, group in new.items():
        previous = old.get(name)
        if previous is None:
            changes.append(
                FaGroupChange(
                    name=name,
                    change="added",
                    method_after=group.method,
                    accounts_added=group.accounts(),
                )
            )
            continue
        old_amounts = {m.account: m.amount for m in previous.members}
        new_amounts = {m.account: m.amount for m in group.members}
        added = [a for a in new_amounts if a not in old_amounts]
        removed = [a for a in old_amounts if a not in new_amounts]
        amounts = [a for a in new_amounts if a in old_amounts and new_amounts[a] != old_amounts[a]]
        if previous.method == group.method and not (added or removed or amounts):
            unchanged.append(name)
            continue
        changes.append(
            FaGroupChange(
                name=name,
                change="changed",
                method_before=previous.method,
                method_after=group.method,
                accounts_added=added,
                accounts_removed=removed,
                amounts_changed=amounts,
            )
        )
    changes.extend(
        FaGroupChange(
            name=name,
            change="removed",
            method_before=group.method,
            accounts_removed=group.accounts(),
        )
        for name, group in old.items()
        if name not in new
    )
    return changes, unchanged


def _listed(accounts: Sequence[str]) -> str:
    shown = ", ".join(accounts[:_MAX_LISTED])
    more = len(accounts) - _MAX_LISTED
    return f"{shown} and {more} more" if more > 0 else shown


def _method(method: str | None) -> str:
    return quoted(method) if method else "no method"


def _describe_change(change: FaGroupChange) -> str:
    """One line per group; names and methods are quoted (the requester wrote them)."""
    name = quoted(change.name)
    if change.change == "added":
        return (
            f"+ add group {name} ({_method(change.method_after)}): "
            f"{_listed(change.accounts_added) or 'no accounts'}"
        )
    if change.change == "removed":
        return f"- remove group {name} ({len(change.accounts_removed)} accounts)"
    parts: list[str] = []
    if change.method_before != change.method_after:
        parts.append(f"method {_method(change.method_before)} -> {_method(change.method_after)}")
    if change.accounts_added:
        parts.append(f"add {_listed(change.accounts_added)}")
    if change.accounts_removed:
        parts.append(f"remove {_listed(change.accounts_removed)}")
    if change.amounts_changed:
        parts.append(f"new amounts for {_listed(change.amounts_changed)}")
    return f"~ change group {name}: {'; '.join(parts)}"


def _summary(
    accounts: Sequence[str], before: int, after: int, changes: Sequence[FaGroupChange]
) -> tuple[str, list[str]]:
    """The one-line action and the detail lines a human confirms."""
    action = (
        f"Replace the FA group configuration of {len(accounts)} account(s) "
        f"({before} groups now, {after} after)"
    )
    details = [_describe_change(change) for change in changes[:_MAX_SUMMARY_LINES]]
    more = len(changes) - _MAX_SUMMARY_LINES
    if more > 0:
        details.append(f"... and {more} more changes")
    return action, details
