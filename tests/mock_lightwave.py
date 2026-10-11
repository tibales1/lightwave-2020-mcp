"""
Mock LightWave for testing the MCP server without LightWave.

Provides a fake `lwsdk` module, loads the REAL bridge plug-ins against it, and
listens on the Layout (10101) and Modeler (10102) UDP command ports the way
LightWave does, routing "{ClaudeMCP} id" to the Layout master's ring_event and
"cmdseq LW_ClaudeMCPModeler id" to the Modeler CommandSequence.
"""
import builtins
import os
import re
import socket
import sys
import threading
import types
from pathlib import Path

builtins.basestring = str  # the plug-ins are Python 2.7 code

ROOT = Path(__file__).resolve().parents[1]
LOG = []


class Item(int):
    pass


NULL = Item(0)
SCENE = {
    Item(0x10000000): dict(name="Cow", type=0, parent=NULL, pos=[0, 0, 0]),
    Item(0x10000001): dict(name="Ground", type=0, parent=NULL, pos=[0, -1, 0]),
    Item(0x20000000): dict(name="Light", type=1, parent=NULL, pos=[1, 2, -3]),
    Item(0x30000000): dict(name="Camera", type=2, parent=NULL, pos=[0, 1, -5]),
}
STATE = {"selected": None, "time": 0.0}

lw = types.ModuleType("lwsdk")
lw.LWITEM_NULL = NULL
lw.LWI_OBJECT, lw.LWI_LIGHT, lw.LWI_CAMERA, lw.LWI_BONE = 0, 1, 2, 3
lw.LWIP_POSITION, lw.LWIP_ROTATION, lw.LWIP_SCALING, lw.LWIP_W_POSITION = 1, 2, 3, 4
lw.LWMAST_LAYOUT = 3
lw.LW_PORT_COMMAND_PORT = "CommandPort"
lw.SRVTAG_USERNAME = lw.SRVTAG_BUTTONNAME = lw.LANGID_USENGLISH = 0
lw.AFUNC_OK = 0
lw.OPLYR_FG, lw.OPLYR_BG = 1, 2


def command(cmd):
    LOG.append(cmd)
    name, _, rest = cmd.partition(" ")
    if name == "AddNull":
        i = Item(0x10000000 + len(SCENE) + 10)
        SCENE[i] = dict(name=rest, type=0, parent=NULL, pos=[0, 0, 0])
        STATE["selected"] = i
    elif name == "SelectItem":
        STATE["selected"] = Item(int(rest, 16))
    elif name == "Position" and STATE["selected"]:
        SCENE[STATE["selected"]]["pos"] = [float(v) for v in rest.split()]
    elif name == "Explode":
        raise RuntimeError("boom")
    return 1


lw.command = command


class IMaster(object):
    def __init__(self):
        pass


class ICommandSequence(object):
    def __init__(self):
        pass


lw.IMaster, lw.ICommandSequence = IMaster, ICommandSequence
lw.MasterFactory = lambda name, k: ("master", name, k)
lw.CommandSequenceFactory = lambda name, k: ("cs", name, k)


class LWComRing(object):
    handlers = []

    def ringAttach(self, topic, inst, fn):
        LWComRing.handlers.append(fn)
        return 1

    def ringDetach(self, topic, inst):
        pass

    def decodeData(self, fmt, data):
        return (data,)


class LWCommandPort(object):
    port = 10101

    def currentPort(self):
        return LWCommandPort.port

    def enable(self, p):
        LWCommandPort.port = p
        return 1


class LWSceneInfo(object):
    filename = "C:/content/Scenes/test.lws"
    name = "test"
    frameStart, frameEnd, frameStep = 1, 60, 1
    framesPerSecond = 30.0
    frameWidth, frameHeight = 1280, 720
    numPoints, numPolygons = 100, 96


class LWInterfaceInfo(object):
    previewStart, previewEnd = 1, 60

    @property
    def curTime(self):
        return STATE["time"]

    def selected_items(self):
        return [STATE["selected"]] if STATE["selected"] else []


class LWItemInfo(object):
    def first(self, t, parent):
        ids = sorted(i for i, d in SCENE.items() if d["type"] == t)
        return ids[0] if ids else NULL

    def next(self, item):
        t = SCENE[item]["type"]
        ids = sorted(i for i, d in SCENE.items() if d["type"] == t)
        k = ids.index(item)
        return ids[k + 1] if k + 1 < len(ids) else NULL

    def name(self, i):
        return SCENE[i]["name"]

    def type(self, i):
        return SCENE[i]["type"]

    def parent(self, i):
        return SCENE[i]["parent"]

    def param(self, i, p, t):
        return SCENE[i]["pos"] if p in (1, 4) else ([0, 0, 0] if p == 2 else [1, 1, 1])

    def firstChild(self, i):
        return NULL

    def nextChild(self, i, c):
        return NULL

    def getTag(self, i, n):
        return None


class LWObjectInfo(object):
    def filename(self, i):
        return "C:/content/Objects/%s.lwo" % SCENE[i]["name"]

    def numPoints(self, i):
        return 8

    def numPolygons(self, i):
        return 6


class LWLightInfo(object):
    def type(self, i):
        return 0

    def intensity(self, i, t):
        return 1.0

    def color(self, i, t):
        return (1.0, 1.0, 1.0)


class LWCameraInfo(object):
    def zoomFactor(self, i, t):
        return 3.2

    def focalLength(self, i, t):
        return 30.0

    def resolution(self, i):
        return (1280, 720)


class LWStateQueryFuncs(object):
    def object(self):
        return "box.lwo"

    def surface(self):
        return "Default"

    def numLayers(self):
        return 1

    def layerList(self, l, o):
        return "1"

    def bbox(self, l):
        return (8, [-0.5, 0.5, -0.5, 0.5, -0.5, 0.5])


for _c in (LWComRing, LWCommandPort, LWSceneInfo, LWInterfaceInfo, LWItemInfo,
           LWObjectInfo, LWLightInfo, LWCameraInfo, LWStateQueryFuncs):
    setattr(lw, _c.__name__, _c)
sys.modules["lwsdk"] = lw

sys.path.insert(0, str(ROOT / "lightwave_bridge"))
import lw_mcp_layout_bridge as layout_bridge  # noqa: E402
import lw_mcp_layout_core as layout_core  # noqa: E402
import lw_mcp_modeler_bridge as modeler_bridge  # noqa: E402


class FakeMod(object):
    data = "moddata"

    def __init__(self, arg):
        self.argument = arg

    def evaluate(self, data, cmd):
        LOG.append("MOD " + cmd)
        return 0

    def undoGroupBegin(self):
        pass

    def undoGroupEnd(self):
        pass

    def editBegin(self, a, b, c):
        return None


def serve(port, handler):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))
    while True:
        msg = s.recvfrom(65535)[0].decode()
        try:
            handler(msg)
        except Exception as e:  # keep going
            print("mock error", e, file=sys.stderr)


def start():
    master = layout_bridge.claude_mcp_bridge(None)
    state = {"active": False}

    def layout_handler(msg):
        LOG.append("UDP " + msg)
        if msg.startswith("ActivateMasterUnique"):
            if not state["active"]:
                master.inst_acquire()
                state["active"] = True
            return
        if msg.startswith("{") and state["active"]:
            for fn in LWComRing.handlers:
                fn(None, None, 0, msg)
        elif not msg.startswith("{"):
            command(msg)

    def modeler_handler(msg):
        m = re.match(r"cmdseq (\S+) (\S+)", msg)
        if m and m.group(1) == modeler_bridge.SERVER_NAME:
            modeler_bridge.claude_mcp_modeler(None).process(FakeMod(m.group(2)))

    if os.environ.get("MOCK_ACTIVE", "1") == "1":
        master.inst_acquire()
        state["active"] = True
    for port, h in ((10101, layout_handler), (10102, modeler_handler)):
        threading.Thread(target=serve, args=(port, h), daemon=True).start()


if __name__ == "__main__":
    start()
    print("mock LightWave listening", flush=True)
    threading.Event().wait()
