# -*- Mode: Python -*-
# -*- coding: ascii -*-
"""
Claude MCP Bridge for LightWave Layout (LightWave 2020, Python 2.7).

A Layout-class Master plug-in. All the work is done by lw_mcp_layout_core.py
(same folder), imported as a normal module: LightWave's Python clears a plug-in
file's globals after activation, but imported modules keep theirs, which the
bridge's timer callback needs.

Once active it:
  * makes sure Layout's Command Port is enabled (port 10101 by default),
  * polls %USERPROFILE%\\.lightwave_mcp for <id>.req.json from a Windows timer on
    Layout's main thread, runs each request and writes <id>.resp.json,
  * also listens on the Command Port ring for "{ClaudeMCP} <id>".

Install: Layout > Utilities > Add Plugins > pick this file.
Start:   Utilities > Master Plugins > add "Claude MCP Bridge".
"""

from __future__ import print_function

__author__ = "lightwave-mcp"
__version__ = "0.2.1"
__lwver__ = "2018"

import os
import sys

import lwsdk

_HERE = None
try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    pass
for _d in (_HERE, os.environ.get("LWMCP_BRIDGE_DIR"), r"C:\lightwave-mcp\lightwave_bridge"):
    if _d and os.path.isdir(_d) and _d not in sys.path:
        sys.path.insert(0, _d)

import lw_mcp_layout_core as core   # noqa: E402

SERVER_NAME = core.SERVER_NAME


class claude_mcp_bridge(lwsdk.IMaster):
    def __init__(self, context):
        super(claude_mcp_bridge, self).__init__()
        self._core = core                    # keep references: module globals
        self._lwsdk = lwsdk                  # of this file are cleared later
        self._version = __version__
        self._comring = lwsdk.LWComRing()
        self._attached = False
        self._poller = None
        self._started = False
        self._ensure_started()

    def ring_event(self, client_data, port_data, event_code, event_data):
        if event_code != 0:
            return
        try:
            data = self._comring.decodeData(("s:256",), event_data)
            if data:
                self._core.handle_ring_message(data[0])
        except Exception:
            self._core.log_exception("ring_event")

    def inst_copy(self, source):
        return None

    def inst_descln(self):
        return "Claude MCP Bridge (port %d)" % self._lwsdk.LWCommandPort().currentPort()

    def inst_acquire(self):
        self._ensure_started()

    def _ensure_started(self):
        # LightWave does not call inst_acquire() for a master restored from a
        # saved config, so this is also called from __init__, flags and event.
        if self._started:
            return
        self._started = True
        c, L = self._core, self._lwsdk
        try:
            self._start(c, L)
        except Exception:
            self._started = False
            c.log_exception("start")

    def _start(self, c, L):
        cp = L.LWCommandPort()
        if cp.currentPort() == 0:
            cp.enable(c.DEFAULT_PORT)
        c.exchange_dir()
        if not self._attached:
            try:
                self._attached = bool(self._comring.ringAttach(L.LW_PORT_COMMAND_PORT, self, self.ring_event))
            except Exception:
                c.log_exception("ringAttach")
        self._poller = c.get_poller()
        polling = self._poller.start()
        c._log("bridge %s active: port=%d ring=%s polling=%s" % (self._version, cp.currentPort(), self._attached, polling))
        print("Claude MCP Bridge active on Command Port %d (exchange: %s)" % (cp.currentPort(), c.exchange_dir()))

    def inst_release(self):
        if self._poller is not None:
            self._poller.stop()
        self._started = False
        if self._attached:
            self._comring.ringDetach(self._lwsdk.LW_PORT_COMMAND_PORT, self)
            self._attached = False

    def flags(self):
        self._ensure_started()
        # LAYOUT masters survive Clear Scene / Load Scene.
        return self._lwsdk.LWMAST_LAYOUT

    def event(self, ma):
        self._ensure_started()
        return 0.0


ServerTagInfo = [
    ("Claude MCP Bridge", lwsdk.SRVTAG_USERNAME | lwsdk.LANGID_USENGLISH),
    ("Claude MCP Bridge", lwsdk.SRVTAG_BUTTONNAME | lwsdk.LANGID_USENGLISH),
]

ServerRecord = {lwsdk.MasterFactory(SERVER_NAME, claude_mcp_bridge): ServerTagInfo}
