# transition_mcp_server.py
import sys
import os
import json
import requests
from datetime import date

def get_transition_health_data(api_key: str):
    """Fetch real Apple Health data via Transition API."""
    base_url = "https://api.transition.fun/api/v1"
    headers = {"X-API-Key": api_key}
    
    try:
        # Ask coach for today's health summary
        coach_res = requests.post(
            f"{base_url}/coach/chat",
            headers=headers,
            json={"message": "Give me today's key health metrics as a JSON object: sleep_hours, sleep_score, hrv_ms, resting_hr, recovery_score, steps_yesterday. Reply ONLY with JSON."},
            timeout=15
        )
        coach_res.raise_for_status()
        raw = coach_res.json()
        
        # Parse JSON from text
        import re
        text = raw.get("message", raw.get("response", str(raw)))
        match = re.search(r'\{[^}]+\}', text, re.DOTALL)
        if not match:
            return {"error": "Could not parse JSON", "raw": text}
            
        return json.loads(match.group())
    except Exception as e:
        return {"error": str(e)}

def handle_mcp_request():
    """Minimal MCP (Model Context Protocol) Server for OpenClaw"""
    # Read JSON-RPC from stdin
    line = sys.stdin.readline()
    if not line:
        return
        
    try:
        req = json.loads(line)
        if req.get("method") == "tools/list":
            res = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "result": {
                    "tools": [{
                        "name": "get_apple_health_data",
                        "description": "Fetch the user's real Apple Health metrics for today (sleep, HRV, recovery, etc.) via the Transition app.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "required": []
                        }
                    }]
                }
            }
        elif req.get("method") == "tools/call":
            tool = req.get("params", {}).get("name")
            if tool == "get_apple_health_data":
                api_key = os.environ.get("TRANSITION_API_KEY")
                if not api_key:
                    data = {"error": "TRANSITION_API_KEY environment variable is not set."}
                else:
                    data = get_transition_health_data(api_key)
                    
                res = {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(data, indent=2)}
                        ]
                    }
                }
            else:
                res = {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32601, "message": "Method not found"}}
        else:
            res = {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32601, "message": "Method not found"}}
            
        print(json.dumps(res))
        sys.stdout.flush()
    except Exception as e:
        pass

if __name__ == "__main__":
    handle_mcp_request()
