"""COVERA MCP server entry point (stdio). Runs with nothing to install.

    python scripts/mcp_server.py

Client configuration lives in the plugin root's .mcp.json.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from covera_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
