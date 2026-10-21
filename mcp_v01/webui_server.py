"""Web UI entry point: python -B mcp_v01/webui_server.py [--host H] [--port P]"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from webui.app import serve

if __name__ == "__main__":
    serve()
