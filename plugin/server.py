import sys
import os
import json

# Add backend directory to path so we can import shared health logic
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"), override=True)

from health_mock import get_health_data
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Apple Health Sync")


@mcp.tool()
def get_apple_health_data() -> str:
    """
    Fetch the user's Apple Watch health metrics for today (sleep, HRV, recovery)
    and the 7-day trend history. Returns live data if the iOS Shortcut has synced
    today, otherwise falls back to realistic mock data.
    """
    return json.dumps(get_health_data(), indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
