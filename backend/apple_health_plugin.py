import sys
import os
import json

# Add backend directory to path so we can import the existing mock/live logic
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from health_mock import get_health_data
from dotenv import load_dotenv

# Load .env from the project root
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'), override=True)

from mcp.server.fastmcp import FastMCP

# Create the MCP Plugin Server
mcp = FastMCP("Apple Health Sync")

@mcp.tool()
def get_apple_health_data() -> str:
    """Fetch the user's Apple Health metrics for today (sleep, HRV, recovery) and the 7-day trend history. Automatically falls back to realistic mock data if live sync is unavailable."""
    
    # This automatically calls your existing get_health_data() 
    # which uses Transition API if the key works, or falls back to your 7-day mock trend!
    data = get_health_data()
    
    return json.dumps(data, indent=2)

if __name__ == "__main__":
    mcp.run(transport='stdio')
