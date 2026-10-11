"""Rebuild the Layout/Modeler command lists from your LightWave install.

Usage:  python tools/build_command_cache.py ["C:\\Program Files\\NewTek\\LightWave_2020.0.3"]
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import command_cache  # noqa: E402

install = Path(sys.argv[1] if len(sys.argv) > 1 else
               os.environ.get("LW_INSTALL", r"C:\Program Files\NewTek\LightWave_2020.0.3"))
for name in ("layout_commands.json", "modeler_commands.json"):
    (ROOT / "server" / name).unlink(missing_ok=True)
command_cache.ensure(ROOT / "server", install)
for name in ("layout_commands.json", "modeler_commands.json"):
    p = ROOT / "server" / name
    print(p, "OK" if p.exists() else "FAILED (check the install path)")
