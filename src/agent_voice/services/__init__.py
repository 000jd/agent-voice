"""Transport-agnostic services shared by the REST routes and the MCP server.

Living here rather than in ``api/`` is what lets MCP call real logic with an
explicit caller identity instead of synthesising a fake HTTP request.
"""
