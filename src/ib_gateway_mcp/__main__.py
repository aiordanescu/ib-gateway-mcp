"""Allow ``python -m ib_gateway_mcp``."""

import sys

from ib_gateway_mcp.cli import main

sys.exit(main())
