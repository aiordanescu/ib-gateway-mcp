"""The installed package version (``0.0.0`` when running from an uninstalled tree)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ib-gateway-mcp")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0"

__all__ = ["__version__"]
