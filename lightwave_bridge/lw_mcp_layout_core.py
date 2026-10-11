# -*- Mode: Python -*-
# -*- coding: ascii -*-
"""
Core of the Claude MCP Bridge for LightWave Layout (LightWave 2020, Python 2.7).

Not a plug-in by itself: lw_mcp_layout_bridge.py (the Master plug-in) imports
this module. Requests arrive as <exchange>/<id>.req.json, are run on Layout's
main thread, and answered in <exchange>/<id>.resp.json.

<exchange> is %USERPROFILE%\\.lightwave_mcp unless LWMCP_EXCHANGE is set.
"""

from __future__ import print_function

__author__ = "lightwave-mcp"
__version__ = "0.2.0"

import os
import re
import sys
import json
import time
import traceback

try:
    from StringIO import StringIO          # Python 2
except ImportError:                         # pragma: no cover
    from io import StringIO

import lwsdk

SERVER_NAME = "LW_ClaudeMCPBridge"
RING_TOPIC = "ClaudeMCP"
DEFAULT_PORT = int(os.environ.get("LWMCP_LAYOUT_PORT", "10101"))
BRIDGE_VERSION = __version__


def exchange_dir():
    d = os.environ.get("LWMCP_EXCHANGE")
    if not d:
        home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        d = os.path.join(home, ".lightwave_mcp")
    if not os.path.isdir(d):
        try:
            os.makedirs(d)
        except OSError:
            pass
    return d


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _jsonable(v, depth=0):
    if depth > 8:
        return repr(v)
    if v is None or isinstance(v, (bool, int, float)):
        return v
    try:
        if isinstance(v, long):             # noqa: F821  (py2)
            return int(v)
    except NameError:
        pass
    if isinstance(v, bytes):                # py2 str
        try:
            return v.decode("utf-8")
        except Exception:
            return v.decode("latin-1")
    if isinstance(v, str):                  # py3 str
        return v
    try:
        if isinstance(v, unicode):          # noqa: F821
            return v
    except NameError:
        pass
    if isinstance(v, dict):
        return dict((str(k), _jsonable(x, depth + 1)) for k, x in v.items())
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x, depth + 1) for x in v]
    # lwsdk.Vector and friends usually support indexing / len
    try:
        return [float(v[i]) for i in range(len(v))]
    except Exception:
        pass
    return repr(v)


def _is_null(item):
    if item is None:
        return True
    try:
        if item == lwsdk.LWITEM_NULL:
            return True
    except Exception:
        pass
    try:
        return int(item) == 0
    except Exception:
        return False


def _item_hex(item):
    try:
        return "%X" % int(item)
    except Exception:
        return None


def _vec(v):
    try:
        return [round(float(v[0]), 6), round(float(v[1]), 6), round(float(v[2]), 6)]
    except Exception:
        return _jsonable(v)


TYPE_NAMES = {}
for _n in ("OBJECT", "LIGHT", "CAMERA", "BONE"):
    try:
        TYPE_NAMES[getattr(lwsdk, "LWI_" + _n)] = _n.lower()
    except AttributeError:
        pass


# --------------------------------------------------------------------------
# request handlers (all run on Layout's main thread)
# --------------------------------------------------------------------------

def run_command(cmd):
    cmd = cmd.strip()
    if not cmd:
        return {"command": cmd, "result": None, "error": "empty command"}
    try:
        r = lwsdk.command(cmd)
        return {"command": cmd, "result": _jsonable(r)}
    except Exception as e:
        return {"command": cmd, "result": None, "error": "%s: %s" % (type(e).__name__, e)}


def op_ping(args):
    si = lwsdk.LWSceneInfo()
    cp = lwsdk.LWCommandPort()
    return {
        "app": "Layout",
        "bridge_version": BRIDGE_VERSION,
        "python": sys.version.split()[0],
        "scene": _jsonable(si.filename),
        "command_port": cp.currentPort(),
        "exchange": exchange_dir(),
    }


def op_command(args):
    cmds = args.get("commands")
    if cmds is None:
        cmds = [args.get("command", "")]
    if isinstance(cmds, basestring):        # noqa: F821
        cmds = [cmds]
    results = []
    for c in cmds:
        r = run_command(c)
        results.append(r)
        if r.get("error") and args.get("stop_on_error", True):
            break
    return {"results": results}


def _describe_item(info, item, t, detail):
    itype = info.type(item)
    d = {
        "name": _jsonable(info.name(item)),
        "id": _item_hex(item),
        "type": TYPE_NAMES.get(itype, str(itype)),
    }
    par = info.parent(item)
    d["parent"] = None if _is_null(par) else _jsonable(info.name(par))
    try:
        d["position"] = _vec(info.param(item, lwsdk.LWIP_POSITION, t))
        d["rotation_hpb_rad"] = _vec(info.param(item, lwsdk.LWIP_ROTATION, t))
        d["scale"] = _vec(info.param(item, lwsdk.LWIP_SCALING, t))
    except Exception as e:
        d["transform_error"] = str(e)
    if not detail:
        return d
    try:
        d["world_position"] = _vec(info.param(item, lwsdk.LWIP_W_POSITION, t))
    except Exception:
        pass
    try:
        if itype == lwsdk.LWI_OBJECT:
            oi = lwsdk.LWObjectInfo()
            d["filename"] = _jsonable(oi.filename(item))
            d["points"] = oi.numPoints(item)
            d["polygons"] = oi.numPolygons(item)
        elif itype == lwsdk.LWI_LIGHT:
            li = lwsdk.LWLightInfo()
            d["light_type"] = li.type(item)
            d["intensity"] = li.intensity(item, t)
            d["color"] = _vec(li.color(item, t))
        elif itype == lwsdk.LWI_CAMERA:
            ci = lwsdk.LWCameraInfo()
            d["zoom_factor"] = ci.zoomFactor(item, t)
            d["focal_length_mm"] = ci.focalLength(item, t)
            d["resolution"] = _jsonable(ci.resolution(item))
    except Exception as e:
        d["detail_error"] = str(e)
    return d


def _iter_items(info, itype, parent=None):
    item = info.first(itype, parent if parent is not None else lwsdk.LWITEM_NULL)
    guard = 0
    while not _is_null(item) and guard < 100000:
        yield item
        item = info.next(item)
        guard += 1


def op_scene(args):
    si = lwsdk.LWSceneInfo()
    ii = lwsdk.LWInterfaceInfo()
    info = lwsdk.LWItemInfo()
    t = ii.curTime
    detail = bool(args.get("detail", True))
    out = {
        "filename": _jsonable(si.filename),
        "name": _jsonable(si.name),
        "frame_start": si.frameStart,
        "frame_end": si.frameEnd,
        "frame_step": si.frameStep,
        "fps": si.framesPerSecond,
        "resolution": [si.frameWidth, si.frameHeight],
        "current_time": t,
        "current_frame": int(round(t * si.framesPerSecond)),
        "preview_range": [ii.previewStart, ii.previewEnd],
        "total_points": si.numPoints,
        "total_polygons": si.numPolygons,
    }
    try:
        out["selected"] = [_jsonable(info.name(x)) for x in (ii.selected_items() or [])]
    except Exception:
        out["selected"] = []
    items = []
    limit = int(args.get("limit", 500))
    for itype in (lwsdk.LWI_OBJECT, lwsdk.LWI_LIGHT, lwsdk.LWI_CAMERA):
        for item in _iter_items(info, itype):
            if len(items) >= limit:
                break
            items.append(_describe_item(info, item, t, detail))
            if args.get("bones") and itype == lwsdk.LWI_OBJECT:
                for b in _iter_items(info, lwsdk.LWI_BONE, item):
                    items.append(_describe_item(info, b, t, False))
    out["items"] = items
    out["truncated"] = len(items) >= limit
    return out


def _find_item(name):
    info = lwsdk.LWItemInfo()
    want = name.strip()
    for itype in (lwsdk.LWI_OBJECT, lwsdk.LWI_LIGHT, lwsdk.LWI_CAMERA):
        for item in _iter_items(info, itype):
            n = info.name(item)
            if n == want or _item_hex(item) == want.upper():
                return item
            if itype == lwsdk.LWI_OBJECT:
                for b in _iter_items(info, lwsdk.LWI_BONE, item):
                    if info.name(b) == want:
                        return b
    return None


def op_item(args):
    item = _find_item(args.get("name", ""))
    if item is None:
        raise ValueError("No item named %r" % args.get("name"))
    info = lwsdk.LWItemInfo()
    t = args.get("time")
    if t is None:
        t = lwsdk.LWInterfaceInfo().curTime
    d = _describe_item(info, item, t, True)
    kids = []
    c = info.firstChild(item)
    guard = 0
    while not _is_null(c) and guard < 10000:
        kids.append(_jsonable(info.name(c)))
        c = info.nextChild(item, c)
        guard += 1
    d["children"] = kids
    tags = []
    for i in range(1, 64):
        tg = info.getTag(item, i)
        if tg is None:
            break
        tags.append(_jsonable(tg))
    d["tags"] = tags
    return d


def op_select(args):
    item = _find_item(args.get("name", ""))
    if item is None:
        raise ValueError("No item named %r" % args.get("name"))
    hx = _item_hex(item)
    if hx:
        return run_command("SelectItem %s" % hx)
    return run_command('SelectByName %s' % args.get("name"))


def op_python(args):
    code = args.get("code", "")
    ns = {
        "lwsdk": lwsdk,
        "command": lwsdk.command,
        "find_item": _find_item,
        "result": None,
        "__name__": "__lwmcp__",
    }
    try:
        from lwsdk.pris.layout import cs as pris_cs   # noqa
        ns["cs"] = pris_cs
    except Exception:
        pass
    buf = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buf
    err = None
    try:
        exec(compile(code, "<claude>", "exec"), ns)
    except Exception:
        err = traceback.format_exc()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return {"output": buf.getvalue()[-20000:], "result": _jsonable(ns.get("result")), "error": err}


OPS = {
    "ping": op_ping,
    "command": op_command,
    "scene": op_scene,
    "item": op_item,
    "select": op_select,
    "python": op_python,
}


def handle_request(req_id):
    d = exchange_dir()
    req_path = os.path.join(d, req_id + ".req.json")
    resp_path = os.path.join(d, req_id + ".resp.json")
    if not os.path.exists(req_path):
        return
    t0 = time.time()
    try:
        with open(req_path, "rb") as f:
            req = json.loads(f.read().decode("utf-8"))
        fn = OPS.get(req.get("op"))
        if fn is None:
            resp = {"ok": False, "error": "unknown op %r" % req.get("op")}
        else:
            resp = {"ok": True, "data": fn(req.get("args") or {})}
    except Exception:
        resp = {"ok": False, "error": traceback.format_exc()}
    resp["id"] = req_id
    resp["elapsed"] = round(time.time() - t0, 4)
    tmp = resp_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(json.dumps(resp).encode("utf-8"))
    os.rename(tmp, resp_path)
    try:
        os.remove(req_path)
    except OSError:
        pass


_MSG_RE = re.compile(r"^\{(.+?)\}\s*(.*)$")
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")


# --------------------------------------------------------------------------
# Windows-timer poller.
# LightWave 2020's Command Port does not reliably run what it receives, so the
# bridge also polls the exchange folder for "L*.req.json" from a Win32 timer.
# The timer callback is dispatched by Layout's own message loop, i.e. on the
# main thread, so it is safe to run commands from it.
# --------------------------------------------------------------------------

def _log(msg):
    try:
        with open(os.path.join(exchange_dir(), "layout_bridge.log"), "a") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def log_exception(where):
    _log("%s error:\n%s" % (where, traceback.format_exc()))


class _Poller(object):
    INTERVAL_MS = 100

    def __init__(self):
        self.timer_id = 0
        self.busy = False
        self.cb = None
        self.user32 = None

    def start(self):
        if self.timer_id:
            return True
        try:
            import ctypes
            from ctypes import wintypes
            self.user32 = ctypes.windll.user32
            proto = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, wintypes.DWORD)
            self.user32.SetTimer.restype = ctypes.c_size_t
            self.user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, proto]
            self.user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
            self.cb = proto(self._tick)
            self.timer_id = self.user32.SetTimer(None, 0, self.INTERVAL_MS, self.cb)
            _log("poller started (timer %s)" % self.timer_id)
            return bool(self.timer_id)
        except Exception:
            _log("poller failed:\n" + traceback.format_exc())
            return False

    def stop(self):
        if self.timer_id and self.user32:
            self.user32.KillTimer(None, self.timer_id)
            _log("poller stopped")
        self.timer_id = 0

    def _tick(self, hwnd, msg, tid, t):
        if self.busy:
            return
        self.busy = True
        try:
            d = exchange_dir()
            for name in sorted(os.listdir(d)):
                if not name.endswith(".req.json"):
                    continue
                rid = name[:-len(".req.json")]
                # "L..." ids (current server) or bare 16-hex ids (older server build)
                if rid.startswith("L") or _HEX16_RE.match(rid):
                    handle_request(rid)
        except Exception:
            _log("tick error:\n" + traceback.format_exc())
        finally:
            self.busy = False



def get_poller():
    """One poller per Layout session. An earlier one (from a previous
    activation) is stopped first so its timer cannot fire with stale code."""
    old = sys.modules.get("__lwmcp_layout_poller__")
    if old is not None and getattr(old, "timer_id", 0):
        try:
            import ctypes
            ctypes.windll.user32.KillTimer(None, old.timer_id)
        except Exception:
            pass
        old.timer_id = 0
    p = _Poller()
    sys.modules["__lwmcp_layout_poller__"] = p
    return p


def handle_ring_message(text):
    m = _MSG_RE.match(text.strip())
    if m and m.group(1) == RING_TOPIC:
        req_id = m.group(2).strip()
        if _ID_RE.match(req_id):
            handle_request(req_id)

