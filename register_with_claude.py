"""Add (or update) the "lightwave" entry in the Claude desktop app's MCP config. Keeps a backup."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
SERVER = ROOT / "server" / "lightwave_mcp_server.py"

appdata = os.environ.get("APPDATA")
if not appdata:
    sys.exit("APPDATA is not set - is this Windows?")
cfg_path = Path(appdata) / "Claude" / "claude_desktop_config.json"

entry = {
    "command": str(PY if PY.exists() else Path(sys.executable)),
    "args": [str(SERVER)],
    "env": {
        "LW_INSTALL": r"C:\Program Files\NewTek\LightWave_2020.0.3",
        "LWMCP_LAYOUT_PORT": "10101",
        "LWMCP_MODELER_PORT": "10102",
    },
}

cfg = {}
if cfg_path.exists():
    raw = cfg_path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        sys.exit(f"{cfg_path} is not valid JSON ({e}); fix it or add this entry by hand:\n"
                 + json.dumps({"lightwave": entry}, indent=2))
    backup = cfg_path.with_name(f"claude_desktop_config.backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
    shutil.copy2(cfg_path, backup)
    print("Backed up existing config to", backup)
else:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

cfg.setdefault("mcpServers", {})["lightwave"] = entry
cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
print("Registered 'lightwave' MCP server in", cfg_path)
