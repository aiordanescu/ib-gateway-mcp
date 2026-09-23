"""The generated reference docs match the code (regenerate with scripts/gen_docs.py)."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
from pathlib import Path
from types import ModuleType

import ib_gateway_mcp.mcp.tools  # noqa: F401  (registers every tool)
from ib_gateway_mcp.config import PROFILES, Settings, enabled_toolsets
from ib_gateway_mcp.mcp.registry import REGISTRY
from ib_gateway_mcp.services import MarketDataService, OrdersService

ROOT = Path(__file__).resolve().parents[2]


def load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_docs", ROOT / "scripts" / "gen_docs.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tools_md_matches_the_registry() -> None:
    generator = load_generator()
    current = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    assert generator.render_tools() == current, "run: uv run python scripts/gen_docs.py"


def test_coverage_md_names_every_tool() -> None:
    """docs/coverage.md is edited by hand; every registered tool must appear in it."""
    text = (ROOT / "docs" / "coverage.md").read_text(encoding="utf-8")
    assert len(REGISTRY) >= 70
    missing = [spec.name for spec in REGISTRY.specs() if not re.search(rf"\b{spec.name}\b", text)]
    assert missing == [], f"add these tools to docs/coverage.md: {missing}"


def _readme_table(section: str) -> list[list[str]]:
    """The rows of the first Markdown table under ``## <section>`` in the README."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    body = readme.split(f"\n## {section}\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in body.splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows[2:]]


def _documented_default(value: object) -> str:
    if value is None or value == []:
        return ""
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, float) and value.is_integer():
        return f"`{int(value)}`"
    return f"`{value}`"


def test_readme_configuration_table_matches_settings() -> None:
    """Every variable Settings reads is in the README table, with its real default."""
    documented: dict[str, str] = {}
    for variable, default, _meaning in _readme_table("Configuration"):
        names = re.findall(r"`([A-Z_]+)`", variable)
        if len(names) == 2 and names[1] == "_FILE":  # `IBKR_MCP_AUTH_TOKEN` / `_FILE`
            names = [names[0], names[0] + "_FILE"]
        for name in names:
            documented[name] = default
    fields = {
        str(field.validation_alias): field.get_default(call_default_factory=True)
        for field in Settings.model_fields.values()
    }
    assert set(documented) == set(fields)
    for alias, default in fields.items():
        if alias.endswith("_FILE"):
            continue
        assert documented[alias] == _documented_default(default), alias


def test_readme_tool_counts_match_the_registry() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"({len(REGISTRY)} tools;" in readme
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    toolsets = {spec.toolset for spec in REGISTRY.specs()}
    assert f"{len(REGISTRY)} tools in {len(toolsets)} toolsets" in changelog
    rows = _readme_table("Profiles and tools")
    assert [row[0].split()[0].strip("`") for row in rows] == list(PROFILES)
    for row in rows:
        profile = row[0].split()[0].strip("`")
        settings = Settings.model_validate({"profile": profile})
        tools = REGISTRY.specs_for(enabled_toolsets(settings))
        assert row[-1] == str(len(tools)), profile


def test_readme_library_example_compiles_and_imports() -> None:
    """The README's library example stays valid as the API moves."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Library", 1)[1]
    code = section.split("```python\n", 1)[1].split("```", 1)[0]
    tree = ast.parse(code)
    compile(tree, "README.md", "exec")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name), f"{node.module} has no {alias.name}"
    services = {"market_data": MarketDataService, "orders": OrdersService}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "gw"
            and node.value.attr in services
        ):
            assert hasattr(services[node.value.attr], node.attr), (
                f"gw.{node.value.attr}.{node.attr}"
            )
