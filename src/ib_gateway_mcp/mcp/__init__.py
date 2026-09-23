"""The MCP layer: a thin adapter from MCP tools to the library's services.

* :mod:`.registry`: ``@ib_tool``, tiers, toolsets and profiles.
* :mod:`.context`: how a tool reaches the :class:`~ib_gateway_mcp.gateway.Gateway`.
* :mod:`.server`: ``build_server`` and ``run`` (stdio or streamable HTTP).
* :mod:`.auth`: static bearer-token auth for HTTP.
* :mod:`.confirm`: human confirmation of live orders through elicitation.
* :mod:`.tools`: one module of tools per toolset.
"""
