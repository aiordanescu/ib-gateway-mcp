"""The agent skill (``skills/ib-gateway-mcp``) stays valid, portable and in step with the server.

- It passes the Agent Skills reference validator (``skills-ref``) and the stricter rules
  of the clients that load it: spec-only frontmatter without angle brackets, the trigger
  in the first 57 characters of the description (Hermes cuts its index there), no ``$``
  before a digit or ``!`` before a backtick in the body (Claude Code substitutes and runs
  those), no client-specific tool prefixes, and the licence next to it.
- Every backticked identifier in the body names something the server has: a tool, a
  tool parameter or output field, an enum value, an error code, a toolset or profile;
  every ``IB_*``/``IBKR_MCP_*`` name is a setting.
- ``metadata.version`` is at least the package version, and the README's install commands
  pin exactly that version.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import skills_ref
from packaging.version import Version
from skills_ref.parser import parse_frontmatter

from ib_gateway_mcp.config import PROFILES, TOOLSETS, Settings
from ib_gateway_mcp.errors import IbGatewayMcpError
from ib_gateway_mcp.mcp.registry import REGISTRY
from ib_gateway_mcp.mcp.server import build_server

ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "skills" / "ib-gateway-mcp"
SKILL_MD = SKILL_DIR / "SKILL.md"

SPEC_FIELDS = frozenset({"name", "description", "license", "compatibility", "metadata"})
"""The spec's optional ``allowed-tools`` is left out: it would pre-approve write tools."""
HERMES_INDEX_CHARS = 57
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BACKTICKED = re.compile(r"`([^`\n]+)`")
IDENTIFIER = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)*\b")
SETTING = re.compile(r"\b(?:IB|IBKR_MCP)_[A-Z0-9_]*[A-Z0-9]\b")
VERSION_TAG = re.compile(r"\bv(\d+(?:\.\d+)+(?:-?[0-9A-Za-z.]*[0-9A-Za-z])?)\b")
"""A release tag in the README, pre-releases included (``v0.2.0``, ``v0.2.0-rc.1``)."""
ALLOWLIST: frozenset[str] = frozenset()
"""Backticked words the checks below cannot derive from the server (none so far)."""
REQUIRED_TOOLS = frozenset(
    {"get_health", "preview_order", "submit_order", "get_order_status", "get_open_orders"}
    | {"set_market_data_type", "list_subscriptions", "qualify_contract", "cancel_order"}
)


def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def frontmatter_and_body() -> tuple[dict[str, Any], str]:
    metadata, body = parse_frontmatter(skill_text())
    return dict(metadata), str(body)


def raw_frontmatter() -> str:
    return skill_text().split("---", 2)[1]


def test_the_reference_validator_accepts_the_skill() -> None:
    assert skills_ref.validate(SKILL_DIR) == []


def test_frontmatter_is_portable() -> None:
    metadata, _body = frontmatter_and_body()
    assert set(metadata) <= SPEC_FIELDS
    assert metadata["name"] == SKILL_DIR.name
    assert NAME.fullmatch(metadata["name"])
    assert len(metadata["name"]) <= 64

    description = metadata["description"]
    assert 1 <= len(description) <= 1024
    assert re.search(r"IBKR|Interactive Brokers", description[:HERMES_INDEX_CHARS])
    assert "Use when" in description  # what it does first, then when to use it
    assert len(metadata["compatibility"]) <= 500
    assert "LICENSE.txt" in metadata["license"]

    meta = metadata["metadata"]
    assert set(meta) == {"version", "mcp-server"}
    assert meta["mcp-server"] == "ib-gateway-mcp"
    Version(meta["version"])  # a PEP 440 version, as pyproject.toml has

    raw = raw_frontmatter()
    assert "<" not in raw
    assert ">" not in raw
    for line in raw.strip().splitlines():
        key, _, value = line.strip().partition(": ")
        if value.startswith('"'):
            continue
        # Plain scalars: other clients' YAML parsers must read them as the same strings.
        assert ": " not in value, key
        assert " #" not in value, key
        assert not re.fullmatch(r"[\d.]+|true|false|yes|no|on|off|null|~", value.lower()), key


def test_the_directory_carries_the_licence_and_nothing_else_to_trip_on() -> None:
    assert (SKILL_DIR / "LICENSE.txt").read_bytes() == (ROOT / "LICENSE").read_bytes()
    names = {path.name for path in SKILL_DIR.iterdir()}
    assert not names & {"README.md", "CHANGELOG.md"}  # skill folders hold no human docs
    _metadata, body = frontmatter_and_body()
    # Installers that fetch SKILL.md alone drop LICENSE.txt: the body carries the notice.
    assert "MIT License" in body.splitlines()[-1]
    assert "LICENSE.txt" in body.splitlines()[-1]
    for reference in re.findall(r"\b(?:references|scripts|assets)/[\w./-]+", body):
        assert (SKILL_DIR / reference).is_file(), reference


def test_the_body_is_portable() -> None:
    _metadata, body = frontmatter_and_body()
    assert body.startswith("# ")
    assert len(body.splitlines()) < 500
    assert "mcp__" not in body  # clients add their own prefix; name tools bare
    assert not re.search(r"\$\d", body)  # Claude Code substitutes $0, $1...
    assert "!`" not in body  # Claude Code runs !`command`
    assert "```!" not in body  # ... and ```! blocks


def backticked_identifiers(body: str) -> Iterator[str]:
    for span in BACKTICKED.findall(body):
        yield from IDENTIFIER.findall(span)


def schema_names(schema: object, fields: set[str], values: set[str]) -> None:
    """Collect property names and string enum/const values from a JSON schema."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                fields.update(value)
            elif key == "enum" and isinstance(value, list):
                values.update(item for item in value if isinstance(item, str))
            elif key == "const" and isinstance(value, str):
                values.add(value)
            schema_names(value, fields, values)
    elif isinstance(schema, list):
        for item in schema:
            schema_names(item, fields, values)


def error_codes() -> set[str]:
    codes: set[str] = set()
    pending: list[type[IbGatewayMcpError]] = [IbGatewayMcpError]
    while pending:
        cls = pending.pop()
        codes.add(cls.code)
        pending.extend(cls.__subclasses__())
    return codes


async def known_identifiers() -> set[str]:
    """What the server exposes, as the model sees it: every tool of the full profile."""
    server = build_server(Settings(profile="full", toolsets=None, transport="stdio"))
    fields: set[str] = set()
    values: set[str] = set()
    for tool in await server.list_tools():
        schema_names(tool.input_schema, fields, values)
        schema_names(tool.output_schema, fields, values)
    tools = {spec.name for spec in REGISTRY.specs()}
    return tools | fields | values | error_codes() | TOOLSETS | set(PROFILES) | ALLOWLIST


async def test_backticked_identifiers_name_what_the_server_has() -> None:
    _metadata, body = frontmatter_and_body()
    known = await known_identifiers()
    unknown = sorted(set(backticked_identifiers(body)) - known)
    assert unknown == [], f"not a tool, parameter, field, enum value or error code: {unknown}"
    mentioned = set(backticked_identifiers(body))
    assert mentioned >= REQUIRED_TOOLS
    settings = {str(field.validation_alias) for field in Settings.model_fields.values()}
    assert sorted(set(SETTING.findall(body)) - settings) == []


def readme_skill_section() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    return readme.split("\n## Agent skill (optional)\n", 1)[1].split("\n## ", 1)[0]


def test_versions_follow_the_release() -> None:
    metadata, _body = frontmatter_and_body()
    version = metadata["metadata"]["version"]
    with (ROOT / "pyproject.toml").open("rb") as file:
        package = tomllib.load(file)["project"]["version"]
    # A release bumps both; between releases the skill may already name the next one.
    assert Version(version) >= Version(package)

    section = readme_skill_section()
    tags = set(VERSION_TAG.findall(section))
    assert tags == {version}, f"the README's skill install commands must pin v{version}"
    # Every install command is pinned: an unpinned one follows main, ahead of the server.
    blocks = re.findall(r"```[a-z]*\n(.*?)```", section, flags=re.DOTALL)
    commands = [line for block in blocks for line in block.splitlines() if line.strip()]
    assert len(commands) >= 4
    for line in commands:
        assert f"v{version}" in line, line
