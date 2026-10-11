# -*- Mode: Python -*-
# -*- coding: ascii -*-
"""
Claude MCP Bridge for LightWave Modeler (LightWave 2020, Python 2.7).

A CommandSequence plug-in. LightWave 2020's Modeler does not run commands that
arrive on its Command Port, and modeling commands only work inside a real
plug-in call, so the MCP server triggers this plug-in with a keyboard shortcut
(Shift+F12 by default) that the user assigns to it once. Each run processes
every waiting <exchange>/M<id>.req.json and writes <exchange>/M<id>.resp.json.

<exchange> is %USERPROFILE%\\.lightwave_mcp unless LWMCP_EXCHANGE is set.

Install: Modeler > Utilities > Add Plugins > pick this file, then
Edit > Edit Keyboard Shortcuts > "Claude MCP Modeler Bridge" > assign Shift-F12.
"""

from __future__ import print_function

__author__ = "lightwave-mcp"
__version__ = "0.2.0"
__lwver__ = "2018"

import os
import re
import sys
import json
import time
import traceback

try:
    from StringIO import StringIO
except ImportError:                         # pragma: no cover
    from io import StringIO

import lwsdk

SERVER_NAME = "LW_ClaudeMCPModeler"
DEFAULT_PORT = int(os.environ.get("LWMCP_MODELER_PORT", "10102"))
BRIDGE_VERSION = __version__
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")


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


def _jsonable(v, depth=0):
    if depth > 8:
        return repr(v)
    if v is None or isinstance(v, (bool, int, float)):
        return v
    try:
        if isinstance(v, long):             # noqa: F821
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
    try:
        return [float(v[i]) for i in range(len(v))]
    except Exception:
        pass
    return repr(v)


def _state():
    out = {}
    try:
        sq = lwsdk.LWStateQueryFuncs()
        out["object"] = _jsonable(sq.object())
        out["surface"] = _jsonable(sq.surface())
        out["num_layers"] = sq.numLayers()
        try:
            fg = sq.layerList(lwsdk.OPLYR_FG, None)
            bg = sq.layerList(lwsdk.OPLYR_BG, None)
            out["fg_layers"] = _jsonable(fg)
            out["bg_layers"] = _jsonable(bg)
        except Exception:
            pass
        try:
            out["bbox_fg"] = _jsonable(sq.bbox(lwsdk.OPLYR_FG))
        except Exception:
            pass
    except Exception as e:
        out["error"] = str(e)
    return out


def _mesh_stats(mod):
    """Count points / polygons in the foreground layers."""
    try:
        op = mod.editBegin(0, 0, lwsdk.OPSEL_GLOBAL)
        if not op:
            return {}
        try:
            return {"points": op.pointCount(op.state, lwsdk.OPLYR_FG, lwsdk.EDCOUNT_ALL),
                    "polygons": op.polyCount(op.state, lwsdk.OPLYR_FG, lwsdk.EDCOUNT_ALL)}
        finally:
            op.done(op.state, lwsdk.EDERR_NONE, 0)
    except Exception as e:
        return {"stats_error": str(e)}


def handle(mod, req):
    op = req.get("op")
    args = req.get("args") or {}
    if op == "ping":
        return {"app": "Modeler", "bridge_version": BRIDGE_VERSION,
                "python": sys.version.split()[0],
                "command_port": lwsdk.LWCommandPort().currentPort(),
                "state": _state()}
    if op == "state":
        s = _state()
        s.update(_mesh_stats(mod))
        return s
    if op == "command":
        cmds = args.get("commands") or [args.get("command", "")]
        if isinstance(cmds, basestring):    # noqa: F821
            cmds = [cmds]
        results = []
        mod.undoGroupBegin()
        try:
            for c in cmds:
                c = c.strip()
                try:
                    r = mod.evaluate(mod.data, c)
                    results.append({"command": c, "result": _jsonable(r)})
                except Exception as e:
                    results.append({"command": c, "result": None, "error": "%s: %s" % (type(e).__name__, e)})
                    if args.get("stop_on_error", True):
                        break
        finally:
            mod.undoGroupEnd()
        return {"results": results}
    if op == "python":
        ns = {"lwsdk": lwsdk, "mod": mod, "result": None, "__name__": "__lwmcp__",
              "evaluate": lambda c: mod.evaluate(mod.data, c)}
        try:
            from lwsdk.pris import modeler as pris_modeler
            pris_modeler.init(mod)
            ns["pm"] = pris_modeler
        except Exception:
            pass
        buf = StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = buf
        err = None
        try:
            exec(compile(args.get("code", ""), "<claude>", "exec"), ns)
        except Exception:
            err = traceback.format_exc()
        finally:
            sys.stdout, sys.stderr = old
        return {"output": buf.getvalue()[-20000:], "result": _jsonable(ns.get("result")), "error": err}
    raise ValueError("unknown op %r" % op)


class claude_mcp_modeler(lwsdk.ICommandSequence):
    def __init__(self, context):
        super(claude_mcp_modeler, self).__init__()

    def process(self, mod_command):
        arg = (mod_command.argument or "").strip()
        d = exchange_dir()
        if _ID_RE.match(arg):
            ids = [arg]
        else:
            # Run from the keyboard shortcut (or by hand): handle every waiting
            # Modeler request. "M..." ids, or bare 16-hex ids from older servers.
            ids = []
            for name in sorted(os.listdir(d)):
                if name.endswith(".req.json"):
                    rid = name[:-len(".req.json")]
                    if rid.startswith("M") or _HEX16_RE.match(rid):
                        ids.append(rid)
            if not ids:
                cp = lwsdk.LWCommandPort()
                if cp.currentPort() == 0:
                    cp.enable(DEFAULT_PORT)
                print("Claude MCP Modeler bridge %s: no pending requests (exchange: %s)" % (BRIDGE_VERSION, d))
                return lwsdk.AFUNC_OK
        for req_id in ids:
            _process_one(mod_command, d, req_id)
        return lwsdk.AFUNC_OK


def _process_one(mod_command, d, req_id):
    req_path = os.path.join(d, req_id + ".req.json")
    resp_path = os.path.join(d, req_id + ".resp.json")
    if not os.path.exists(req_path):
        return
    t0 = time.time()
    try:
        with open(req_path, "rb") as f:
            req = json.loads(f.read().decode("utf-8"))
        resp = {"ok": True, "data": handle(mod_command, req)}
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


ServerTagInfo = [
    ("Claude MCP Modeler Bridge", lwsdk.SRVTAG_USERNAME | lwsdk.LANGID_USENGLISH),
    ("Claude MCP Modeler", lwsdk.SRVTAG_BUTTONNAME | lwsdk.LANGID_USENGLISH),
]

ServerRecord = {lwsdk.CommandSequenceFactory(SERVER_NAME, claude_mcp_modeler): ServerTagInfo}
