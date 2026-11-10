"""COVERA MCP server package.

Only the Python standard library is used, on purpose. The server speaks
JSON-RPC over stdio and reaches COVER through the plugin's own skill scripts,
each run as a short-lived subprocess. Nothing here is imported into the
skill runtimes, so the server adds no third-party dependency and runs on a
bare interpreter (which the uv MCPB bundle relies on).

Written for Python 3.10+ (the skill runtimes need it).
"""
