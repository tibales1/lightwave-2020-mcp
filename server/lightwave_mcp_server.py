#!/usr/bin/env python3
"""
LightWave 2020 MCP server.

Talks to:
  * Layout   - via its Command Port (UDP) + the "Claude MCP Bridge" Master plug-in,
               which runs requests on Layout's main thread and writes JSON replies.
  * Modeler  - via its Command Port + the "Claude MCP Modeler" CommandSequence plug-in.
  * LWSN     - LightWave's headless ScreamerNet renderer, for renders and FBX export
               without the GUI (this is how NewTek's own Unity AppLink works).

Requests and replies are exchanged as small JSON files in the exchange folder
(%USERPROFILE%\\.lightwave_mcp by default); the UDP message only says "go".

Configuration (environment variables, all optional):
  LW_INSTALL           LightWave install folder  (C:\\Program Files\\NewTek\\LightWave_2020.0.3)
  LW_CONFIG_DIR        LightWave config folder   (%USERPROFILE%\\.NewTek\\LightWave\\2020.0.3)
  LW_CONTENT_DIR       Default content directory for LWSN jobs
  LWMCP_HOST           Host running LightWave     (localhost)
  LWMCP_LAYOUT_PORT    Layout Command Port        (10101)
  LWMCP_MODELER_PORT   Modeler Command Port       (10102)
  LWMCP_EXCHANGE       Exchange folder            (%USERPROFILE%\\.lightwave_mcp)
  LWMCP_TIMEOUT        Seconds to wait for a reply (30)
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP, Image
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "This server needs the MCP Python SDK v1:  pip install \"mcp[cli]>=1.10,<2\"\n")
    raise

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("USERPROFILE") or Path.home())

LW_INSTALL = Path(os.environ.get("LW_INSTALL", r"C:\Program Files\NewTek\LightWave_2020.0.3"))
LW_CONFIG_DIR = Path(os.environ.get("LW_CONFIG_DIR", str(HOME / ".NewTek" / "LightWave" / "2020.0.3")))
LW_CONTENT_DIR = os.environ.get("LW_CONTENT_DIR", "")
HOST = os.environ.get("LWMCP_HOST", "localhost")
LAYOUT_PORT = int(os.environ.get("LWMCP_LAYOUT_PORT", "10101"))
MODELER_PORT = int(os.environ.get("LWMCP_MODELER_PORT", "10102"))
EXCHANGE = Path(os.environ.get("LWMCP_EXCHANGE", str(HOME / ".lightwave_mcp")))
TIMEOUT = float(os.environ.get("LWMCP_TIMEOUT", "30"))
# Modeler 2020 ignores its Command Port, so requests are triggered by pressing a
# keyboard shortcut assigned (once, by the user) to the "Claude MCP Modeler" plug-in.
MODELER_HOTKEY = os.environ.get("LWMCP_MODELER_HOTKEY", "shift+f12")
SETTINGS_FILE = HERE.parent / "settings.json"   # optional, re-read on every request


def _setting(key: str, default: Any) -> Any:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get(key, default)
    except (OSError, ValueError):
        return default
MODELER_TRIGGER = os.environ.get("LWMCP_MODELER_TRIGGER", "hotkey")  # hotkey | port

LAYOUT_MASTER = "LW_ClaudeMCPBridge"
MODELER_CS = "LW_ClaudeMCPModeler"
RING_TOPIC = "ClaudeMCP"

mcp = FastMCP(
    "lightwave",
    instructions=(
        "Controls LightWave 3D 2020 (Layout, Modeler, and the LWSN headless renderer). "
        "Start with lw_status. Layout commands are LightWave CommandSequence commands "
        "(e.g. 'AddNull Pivot', 'Position 0 1 0'); most act on the current selection, so select "
        "first. Rotations are heading/pitch/bank in degrees. Use list_layout_commands / "
        "list_modeler_commands to find exact command names. For Unity/Unreal hand-off, export FBX."
    ),
)


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

class BridgeError(RuntimeError):
    pass


def _udp_send(port: int, text: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(text.encode("utf-8"), (HOST, port))


def _trigger(app: str, req_id: str) -> None:
    if app == "layout":
        # The Layout bridge polls the exchange folder; the UDP ping is a bonus.
        _udp_send(LAYOUT_PORT, "{%s} %s" % (RING_TOPIC, req_id))
        return
    _udp_send(MODELER_PORT, "cmdseq %s %s" % (MODELER_CS, req_id))
    if MODELER_TRIGGER == "hotkey":
        import win_hotkey
        try:
            hwnd = win_hotkey.find_window("Modeler")
        except OSError as e:
            raise BridgeError(f"Modeler keyboard trigger unavailable: {e}")
        if not hwnd:
            raise BridgeError("LightWave Modeler is not running (no window titled 'Modeler...'). "
                              "Start it with launch_modeler or open it yourself.")
        try:
            hotkey = _setting("modeler_hotkey", MODELER_HOTKEY)
            win_hotkey.press_in_window(hwnd, hotkey)
        except (OSError, ValueError) as e:
            raise BridgeError(f"Could not press the Modeler shortcut: {e}")


HINTS = {
    "layout": (
        "No reply from Layout. Check: (1) Layout is open; "
        "(2) lw_mcp_layout_bridge.py (and lw_mcp_layout_core.py next to it) was added via Utilities > Add Plugins; "
        "(3) 'Claude MCP Bridge' is listed under Utilities > Master Plugins (it is restored automatically on later starts); "
        "(4) Layout isn't stuck in a modal dialog or a render."
    ),
    "modeler": (
        "No reply from Modeler. Check: (1) Modeler is open; (2) lw_mcp_modeler_bridge.py was added "
        "via Utilities > Add Plugins; (3) the keyboard shortcut (modeler_hotkey in settings.json, "
        "default " + MODELER_HOTKEY + ") is assigned to 'Claude MCP Modeler Bridge' (Edit > Edit Keyboard Shortcuts) - pressing it by hand should print "
        "a line in Modeler's Python console; (4) Modeler isn't showing a modal dialog."
    ),
}


async def bridge(app: str, op: str, args: dict | None = None, timeout: float | None = None) -> Any:
    """Send one request to the in-app bridge and wait for its JSON reply."""
    EXCHANGE.mkdir(parents=True, exist_ok=True)
    # "L..." requests are picked up by the Layout bridge's poller, "M..." by Modeler's.
    req_id = ("L" if app == "layout" else "M") + uuid.uuid4().hex[:15]
    req = EXCHANGE / f"{req_id}.req.json"
    resp = EXCHANGE / f"{req_id}.resp.json"
    tmp = EXCHANGE / f"{req_id}.req.tmp"
    tmp.write_text(json.dumps({"op": op, "args": args or {}}), encoding="utf-8")
    os.replace(tmp, req)
    try:
        _trigger(app, req_id)
    except Exception:
        try:
            req.unlink()
        except OSError:
            pass
        raise
    start = time.monotonic()
    deadline = start + (timeout or TIMEOUT)
    delay = 0.02
    presses, last_press = 1, start
    while time.monotonic() < deadline:
        # Modeler's keyboard trigger occasionally misses: if the request is still
        # waiting untouched after a few seconds, press the shortcut again.
        if (app == "modeler" and MODELER_TRIGGER == "hotkey" and presses < 4
                and time.monotonic() - last_press > 3.0 and req.exists() and not resp.exists()):
            try:
                _trigger(app, req_id)
            except BridgeError:
                pass
            presses, last_press = presses + 1, time.monotonic()
        if resp.exists():
            try:
                data = json.loads(resp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, PermissionError):
                await asyncio.sleep(0.05)
                continue
            try:
                resp.unlink()
            except OSError:
                pass
            if not data.get("ok"):
                raise BridgeError(f"LightWave {app} error:\n{data.get('error')}")
            return data.get("data")
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 0.25)
    try:
        req.unlink()
    except OSError:
        pass
    port = LAYOUT_PORT if app == "layout" else MODELER_PORT
    raise BridgeError(HINTS[app].format(port=port))


def _fmt(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _fnum(v: float) -> str:
    return ("%.6f" % float(v)).rstrip("0").rstrip(".") or "0"


async def _layout_cmds(cmds: list[str], stop_on_error: bool = True) -> dict:
    return await bridge("layout", "command", {"commands": cmds, "stop_on_error": stop_on_error})


# ---------------------------------------------------------------------------
# status / launching
# ---------------------------------------------------------------------------

@mcp.tool()
async def lw_status() -> str:
    """Check what is reachable: Layout bridge, Modeler bridge, install paths. Call this first."""
    out: dict[str, Any] = {
        "install": str(LW_INSTALL),
        "install_found": (LW_INSTALL / "bin" / "Layout.exe").exists(),
        "config_dir": str(LW_CONFIG_DIR),
        "exchange": str(EXCHANGE),
        "layout_port": LAYOUT_PORT,
        "modeler_port": MODELER_PORT,
    }
    for app in ("layout", "modeler"):
        try:
            out[app] = await bridge(app, "ping", timeout=3)
        except BridgeError as e:
            out[app] = {"connected": False, "hint": str(e)}
    return _fmt(out)


def _launch(exe: str, port: int, extra: list[str]) -> int:
    path = LW_INSTALL / "bin" / exe
    if not path.exists():
        raise BridgeError(f"{path} not found - set LW_INSTALL")
    args = [str(path), f"--command-port={port}", f"-c{LW_CONFIG_DIR}"] + extra
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    p = subprocess.Popen(args, cwd=str(path.parent), creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.pid


@mcp.tool()
async def launch_layout(scene: str = "", wait_seconds: int = 90) -> str:
    """Start LightWave Layout with its Command Port enabled, then start the Claude bridge inside it.

    scene: optional .lws file to open once the bridge is connected.
    """
    pid = _launch("Layout.exe", LAYOUT_PORT, [])
    deadline = time.monotonic() + wait_seconds
    info = None
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        try:
            _udp_send(LAYOUT_PORT, f"ActivateMasterUnique {LAYOUT_MASTER}")
            info = await bridge("layout", "ping", timeout=3)
            break
        except (BridgeError, OSError):
            continue
    if info is None:
        return (f"Layout started (pid {pid}) but the bridge did not answer within {wait_seconds}s. "
                "If this is the first run, add lw_mcp_layout_bridge.py via Utilities > Add Plugins "
                "and add 'Claude MCP Bridge' under Utilities > Master Plugins.")
    msg = f"Layout running (pid {pid}); bridge connected: {_fmt(info)}"
    if scene:
        r = await _layout_cmds([f"LoadScene {scene}"])
        msg += "\n" + _fmt(r)
    return msg


@mcp.tool()
async def activate_layout_bridge() -> str:
    """Ask an already-running Layout (Command Port on) to start the Claude bridge master plug-in."""
    _udp_send(LAYOUT_PORT, f"ActivateMasterUnique {LAYOUT_MASTER}")
    await asyncio.sleep(1.0)
    try:
        return _fmt(await bridge("layout", "ping", timeout=5))
    except BridgeError as e:
        return str(e)


@mcp.tool()
async def launch_modeler(object_file: str = "", wait_seconds: int = 60) -> str:
    """Start LightWave Modeler with its Command Port enabled. object_file: optional .lwo to load."""
    pid = _launch("Modeler.exe", MODELER_PORT, [])
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        try:
            info = await bridge("modeler", "ping", timeout=3)
        except BridgeError:
            continue
        msg = f"Modeler running (pid {pid}); bridge connected: {_fmt(info)}"
        if object_file:
            msg += "\n" + _fmt(await bridge("modeler", "command", {"commands": [f"LOAD {object_file}"]}))
        return msg
    return (f"Modeler started (pid {pid}) but the bridge did not answer. Make sure "
            "lw_mcp_modeler_bridge.py is added via Utilities > Add Plugins.")


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

@mcp.tool()
async def layout_command(commands: list[str], stop_on_error: bool = True) -> str:
    """Run one or more Layout CommandSequence commands in order and return their result codes.

    Examples: ["AddNull Target", "Position 0 2 -5"], ["GoToFrame 30", "CreateKey 1.25"],
    ["LoadObject C:/content/Objects/box.lwo"]. Commands act on the current selection.
    """
    return _fmt(await _layout_cmds(commands, stop_on_error))


@mcp.tool()
async def layout_scene_info(detail: bool = True, include_bones: bool = False, limit: int = 500) -> str:
    """Describe the open Layout scene: file, frame range, fps, resolution, selection, and every
    object/light/camera with parent and position/rotation(radians)/scale at the current frame."""
    return _fmt(await bridge("layout", "scene", {"detail": detail, "bones": include_bones, "limit": limit}))


@mcp.tool()
async def layout_get_item(name: str, frame: float | None = None) -> str:
    """Full details for one item (by name or hex ID): transform, children, tags, object/light/camera data."""
    args: dict[str, Any] = {"name": name}
    if frame is not None:
        scene = await bridge("layout", "scene", {"detail": False, "limit": 0})
        args["time"] = frame / float(scene["fps"])
    return _fmt(await bridge("layout", "item", args))


@mcp.tool()
async def layout_select_item(name: str) -> str:
    """Make the named item the current selection (needed before most item commands)."""
    return _fmt(await bridge("layout", "select", {"name": name}))


ADD_COMMANDS = {
    "null": "AddNull", "camera": "AddCamera", "point_light": "AddPointLight",
    "distant_light": "AddDistantLight", "spot_light": "AddSpotlight",
    "area_light": "AddAreaLight", "linear_light": "AddLinearLight",
}


@mcp.tool()
async def layout_add_item(kind: str, name_or_path: str, parent: str = "",
                          position: list[float] | None = None) -> str:
    """Add an item to the scene.

    kind: null | camera | point_light | distant_light | spot_light | area_light | linear_light | object
    name_or_path: the item name, or for kind=object the full path of an .lwo/.fbx/.obj file.
    parent: optional name of an item to parent to. position: optional [x, y, z] in metres.
    """
    kind = kind.lower().strip()
    if kind == "object":
        cmds = [f"LoadObject {name_or_path}"]
    elif kind in ADD_COMMANDS:
        cmds = [f"{ADD_COMMANDS[kind]} {name_or_path}"]
    else:
        return f"Unknown kind {kind!r}; use one of: object, " + ", ".join(ADD_COMMANDS)
    if parent:
        p = await bridge("layout", "item", {"name": parent})
        cmds.append(f"ParentItem {p['id']}")
    if position:
        cmds.append("Position " + " ".join(_fnum(v) for v in position))
    return _fmt(await _layout_cmds(cmds))


@mcp.tool()
async def layout_set_transform(item: str, position: list[float] | None = None,
                               rotation: list[float] | None = None, scale: list[float] | None = None,
                               key_frame: float | None = None) -> str:
    """Move/rotate/scale an item. position [x,y,z] metres; rotation [heading, pitch, bank] degrees;
    scale [x,y,z]. If key_frame is given, goes to that frame and creates a keyframe."""
    sel = await bridge("layout", "select", {"name": item})
    cmds = []
    fps = None
    if key_frame is not None:
        fps = (await bridge("layout", "scene", {"detail": False, "limit": 0}))["fps"]
        cmds.append(f"GoToFrame {_fnum(key_frame)}")
    if position:
        cmds.append("Position " + " ".join(_fnum(v) for v in position))
    if rotation:
        cmds.append("Rotation " + " ".join(_fnum(v) for v in rotation))
    if scale:
        cmds.append("Scale " + " ".join(_fnum(v) for v in scale))
    if key_frame is not None:
        cmds.append(f"CreateKey {_fnum(key_frame / float(fps))}")
    res = await _layout_cmds(cmds)
    return _fmt({"select": sel, **res})


@mcp.tool()
async def layout_set_frame(frame: int) -> str:
    """Move Layout's current time to a frame."""
    return _fmt(await _layout_cmds([f"GoToFrame {frame}"]))


@mcp.tool()
async def layout_scene_file(action: str, path: str = "") -> str:
    """Scene file operations. action: load (path required) | save | save_as (path required) | clear.
    'clear' discards the current scene without asking - save first if needed."""
    action = action.lower()
    if action == "load":
        cmds = [f"LoadScene {path}"]
    elif action == "save":
        cmds = ["SaveScene"]
    elif action == "save_as":
        cmds = [f"SaveSceneAs {path}"]
    elif action == "clear":
        cmds = ["ClearScene"]
    else:
        return "action must be load, save, save_as or clear"
    if action in ("load", "save_as") and not path:
        return "path is required"
    return _fmt(await _layout_cmds(cmds))


@mcp.tool()
async def layout_run_python(code: str, timeout: float = 60) -> str:
    """Run Python 2.7 code inside Layout on its main thread, with full access to the LightWave SDK.

    Available names: lwsdk, command(str) (runs a Layout command), cs (PRIS command proxies,
    lwsdk.pris.layout.cs), find_item(name). Assign to `result` to return JSON data;
    print() output is captured. Example:
        info = lwsdk.LWItemInfo(); result = info.name(find_item('Camera'))
    """
    return _fmt(await bridge("layout", "python", {"code": code}, timeout=timeout))


@mcp.tool()
async def layout_capture_viewport(viewport: int = 0, scale: float = 1.0, image_type: str = ""):
    """Save a screenshot of a Layout viewport (OpenGL view) and return it as an image.
    Experimental: the image-saver name LightWave expects varies; several are tried."""
    EXCHANGE.mkdir(parents=True, exist_ok=True)
    candidates = [image_type] if image_type else ["LW_PNG(.png)", "LW_PNG32(.png)", "PNG", "LW_JPEG(.jpg)", "0"]
    tried = []
    for it in candidates:
        out = EXCHANGE / f"viewport_{uuid.uuid4().hex[:8]}"
        r = await _layout_cmds([f'SaveViewportImage "{out}" "{it}" {viewport} {_fnum(scale)}'], False)
        tried.append({"image_type": it, **r["results"][0]})
        await asyncio.sleep(0.3)
        hits = sorted(EXCHANGE.glob(out.name + "*"))
        if hits:
            f = hits[0]
            fmt = "jpeg" if f.suffix.lower() in (".jpg", ".jpeg") else "png"
            return Image(data=f.read_bytes(), format=fmt)
    return "No viewport image was written. Attempts:\n" + _fmt(tried)


@mcp.tool()
async def layout_export_fbx(fbx_path: str, unity_mode: bool = True) -> str:
    """Export the open Layout scene to FBX (for Unity / Unreal). unity_mode applies the
    Unity axis conversion NewTek's AppLink uses. Uses the same ExportFBXCommand generic as AppLink."""
    r = await _layout_cmds([f"Generic_ExportFBXCommand '{fbx_path}' Unity3DMode={'True' if unity_mode else 'False'}"])
    r["file_exists"] = Path(fbx_path).exists()
    return _fmt(r)


def _load_cache(name: str) -> list[dict]:
    # The lists are built on first use from the user's own LightWave install.
    try:
        import command_cache
        command_cache.ensure(HERE, LW_INSTALL)
    except Exception:
        pass
    try:
        return json.loads((HERE / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


@mcp.tool()
def list_layout_commands(contains: str = "", limit: int = 200) -> str:
    """Search Layout's CommandSequence commands (about 840 in LightWave 2020) (name + argument names). contains: substring filter."""
    c = contains.lower()
    hits = [x for x in _load_cache("layout_commands.json") if c in x["name"].lower()]
    lines = [f"{x['name']} " + " ".join(f"<{a}>" for a in x["args"]) for x in hits[:limit]]
    return f"{len(hits)} match(es)\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Modeler
# ---------------------------------------------------------------------------

@mcp.tool()
async def modeler_command(commands: list[str], stop_on_error: bool = True) -> str:
    """Run Modeler CommandSequence commands (grouped as one undo), e.g.
    ["NEW", "MAKEBOX <-0.5 -0.5 -0.5> <0.5 0.5 0.5> <1 1 1>", "SAVE C:/tmp/box.lwo"]."""
    return _fmt(await bridge("modeler", "command", {"commands": commands, "stop_on_error": stop_on_error}))


@mcp.tool()
async def modeler_state() -> str:
    """Current Modeler object, layers, surface, bounding box and point/polygon counts."""
    return _fmt(await bridge("modeler", "state"))


@mcp.tool()
async def modeler_run_python(code: str, timeout: float = 60) -> str:
    """Run Python 2.7 inside Modeler. Names: lwsdk, mod (LWModCommand), evaluate(str),
    pm (lwsdk.pris.modeler, initialised - LScript-style helpers such as pm.addpoint, pm.addpolygon,
    pm.editbegin/editend). Assign to `result` to return data."""
    return _fmt(await bridge("modeler", "python", {"code": code}, timeout=timeout))


@mcp.tool()
def list_modeler_commands(contains: str = "") -> str:
    """List known Modeler CommandSequence commands (the command-port set plus common geometry
    commands). Modeler accepts any CommandSequence command, so this list is not exhaustive."""
    c = contains.lower()
    hits = [x for x in _load_cache("modeler_commands.json") if c in x["name"].lower()]
    return "\n".join(f"{x['name']}  - {x.get('usage', '')}" for x in hits)


# ---------------------------------------------------------------------------
# LWSN (headless)
# ---------------------------------------------------------------------------

def _lwsn() -> Path:
    p = LW_INSTALL / "bin" / "lwsn.exe"
    if not p.exists():
        raise BridgeError(f"{p} not found - set LW_INSTALL")
    return p


def _guess_content_dir(scene: str) -> str:
    p = Path(scene).resolve().parent
    for d in [p, *p.parents]:
        if d.name.lower() == "scenes":
            return str(d.parent)
    return LW_CONTENT_DIR or str(p)


async def _run(args: list[str], cwd: Path, timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"timed out after {timeout}s"
    text = out.decode("utf-8", "replace")
    return f"exit code {proc.returncode}\n" + text[-6000:]


@mcp.tool()
async def lwsn_render(scene: str, first_frame: int, last_frame: int, step: int = 1,
                      content_dir: str = "", timeout: float = 3600) -> str:
    """Render frames of a saved .lws scene headlessly with LWSN (mode -3). Output files, format and
    renderer settings come from the scene's own Render/Output settings - set them in Layout and
    save the scene first."""
    exe = _lwsn()
    cd = content_dir or _guess_content_dir(scene)
    args = [str(exe), "-3", f"-c{LW_CONFIG_DIR}", f"-d{cd}", scene,
            str(first_frame), str(last_frame), str(step)]
    return await _run(args, exe.parent, timeout)


@mcp.tool()
async def lwsn_run_commands(commands: list[str], content_dir: str = "", timeout: float = 1800) -> str:
    """Run Layout commands headlessly with LWSN (mode -5), e.g.
    ["LoadScene C:/content/Scenes/shot.lws", "Generic_ExportFBXCommand 'C:/out/shot.fbx' Unity3DMode=True"].
    No GUI and no need for Layout to be open."""
    exe = _lwsn()
    args = [str(exe), "-5", f"-c{LW_CONFIG_DIR}"]
    if content_dir or LW_CONTENT_DIR:
        args.append(f"-d{content_dir or LW_CONTENT_DIR}")
    args += commands
    return await _run(args, exe.parent, timeout)


@mcp.tool()
async def lwsn_export_fbx(scene: str, fbx_path: str = "", unity_mode: bool = True,
                          content_dir: str = "", timeout: float = 1800) -> str:
    """Convert a saved .lws scene to FBX headlessly (same method as NewTek's Unity AppLink).
    fbx_path defaults to the scene path with .fbx. Use the result in Unity or Unreal."""
    fbx = fbx_path or str(Path(scene).with_suffix(".fbx"))
    cd = content_dir or _guess_content_dir(scene)
    mode = "True" if unity_mode else "False"
    out = await lwsn_run_commands([f"LoadScene {scene}", f"Generic_ExportFBXCommand '{fbx}' Unity3DMode={mode}"],
                                  cd, timeout)
    exists = Path(fbx).exists()
    return f"fbx: {fbx}\nexists: {exists}\n{out}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
