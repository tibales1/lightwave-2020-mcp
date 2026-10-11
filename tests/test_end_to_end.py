"""End-to-end: MCP client -> server (stdio) -> UDP -> mock LightWave running the real bridge code."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main():
    ex = tempfile.mkdtemp()
    # A tiny fake LightWave install, so the command lists can be built without LightWave.
    fake = Path(tempfile.mkdtemp())
    cs = fake / "bin" / "lwsdk" / "pris" / "layout"; cs.mkdir(parents=True)
    (cs / "cs.py").write_text('def addnull(name):\n    """Proxy for the Layout CS function AddNull"""\n')
    md = fake / "support" / "python" / "lwcommandport" / "modeler"; md.mkdir(parents=True)
    (md / "__init__.py").write_text('class Modeler(object):\n    def mergepoints(self):\n        """\n        Name: Merge Points\n        """\n')
    for f in ("layout_commands.json", "modeler_commands.json"):
        (ROOT / "server" / f).unlink(missing_ok=True)
    env = dict(os.environ, LWMCP_EXCHANGE=ex, LWMCP_TIMEOUT="5", LW_INSTALL=str(fake), LWMCP_MODELER_TRIGGER="port")
    mock = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_lightwave.py")], env=env,
                            stdout=subprocess.PIPE, text=True)
    mock.stdout.readline()
    params = StdioServerParameters(command=sys.executable,
                                   args=[str(ROOT / "server" / "lightwave_mcp_server.py")], env=env)
    failures = 0
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = [t.name for t in (await s.list_tools()).tools]
                print("TOOLS", len(tools), tools)

                async def call(tool, a):
                    res = await s.call_tool(tool, a)
                    txt = res.content[0].text if res.content else ""
                    return res.isError, txt

                checks = [
                    ("lw_status", {}, lambda t: '"bridge_version"' in t and '"Modeler"' in t),
                    ("layout_scene_info", {}, lambda t: json.loads(t)["fps"] == 30.0 and len(json.loads(t)["items"]) == 4),
                    ("layout_get_item", {"name": "Camera"}, lambda t: json.loads(t)["zoom_factor"] == 3.2),
                    ("layout_add_item", {"kind": "null", "name_or_path": "Target", "position": [1, 2.5, 3]},
                     lambda t: json.loads(t)["results"][-1]["command"] == "Position 1 2.5 3"),
                    ("layout_set_transform", {"item": "Cow", "position": [5, 0, 0], "key_frame": 15},
                     lambda t: "CreateKey 0.5" in t and "SelectItem 10000000" in t),
                    ("layout_get_item", {"name": "Cow"}, lambda t: json.loads(t)["position"] == [5.0, 0.0, 0.0]),
                    ("layout_command", {"commands": ["Explode", "AddNull X"]},
                     lambda t: len(json.loads(t)["results"]) == 1 and "boom" in t),
                    ("layout_run_python", {"code": "print('hi'); result = {'n': command('AddNull Py')}"},
                     lambda t: json.loads(t)["output"].strip() == "hi" and json.loads(t)["result"] == {"n": 1}),
                    ("layout_run_python", {"code": "1/0"}, lambda t: "ZeroDivisionError" in t),
                    ("modeler_command", {"commands": ["NEW", "MAKEBOX <0 0 0> <1 1 1> <1 1 1>"]},
                     lambda t: len(json.loads(t)["results"]) == 2),
                    ("modeler_state", {}, lambda t: json.loads(t)["object"] == "box.lwo"),
                    ("modeler_run_python", {"code": "result = evaluate('SEL_INVERT')"}, lambda t: json.loads(t)["result"] == 0),
                    ("list_layout_commands", {"contains": "addnull"}, lambda t: "AddNull <name>" in t),
                    ("list_modeler_commands", {"contains": "makebox"}, lambda t: "MAKEBOX" in t),
                    ("layout_scene_file", {"action": "load"}, lambda t: "path is required" in t),
                ]
                for name, args, ok in checks:
                    err, txt = await call(name, args)
                    try:
                        good = (not err) and ok(txt)
                    except Exception:
                        good = False
                    failures += not good
                    print(("PASS " if good else "FAIL ") + name, "" if good else txt[:800])
                # leftover exchange files?
                left = [f for f in os.listdir(ex) if not f.endswith(".log")]
                print("exchange leftovers:", left)
                failures += bool(left)
        # unreachable layout -> helpful error, quickly
        env2 = dict(env, LWMCP_LAYOUT_PORT="10199", LWMCP_TIMEOUT="1")
        params2 = StdioServerParameters(command=sys.executable,
                                        args=[str(ROOT / "server" / "lightwave_mcp_server.py")], env=env2)
        async with stdio_client(params2) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                t0 = time.time()
                res = await s.call_tool("layout_scene_info", {})
                txt = res.content[0].text
                good = res.isError and "No reply from Layout" in txt and time.time() - t0 < 4
                failures += not good
                print(("PASS " if good else "FAIL ") + "timeout hint", txt[:200])
    finally:
        mock.kill()
        for f in ("layout_commands.json", "modeler_commands.json"):
            (ROOT / "server" / f).unlink(missing_ok=True)
    print("FAILURES", failures)
    sys.exit(1 if failures else 0)


asyncio.run(main())
