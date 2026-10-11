# Architecture and lessons learned

Everything here was found the hard way on a real LightWave 2020.0.3 install (Windows, Python 2.7.16
embedded in LightWave).

## Components

```
MCP client (Claude) ──stdio──► server/lightwave_mcp_server.py   (Python 3)
                                 │ writes  %USERPROFILE%\.lightwave_mcp\L<id>.req.json / M<id>.req.json
                                 │ reads   <id>.resp.json
          ┌──────────────────────┴───────────────────────┐
  Layout: lw_mcp_layout_bridge.py (Master)        Modeler: lw_mcp_modeler_bridge.py (CommandSequence)
          + lw_mcp_layout_core.py                          triggered by Shift+F12 (server/win_hotkey.py)
          Win32 timer polls every 100 ms
```

## Why not the Command Port?

LightWave 2020 has a Command Port (UDP, `--command-port=<port>` or `LWCommandPort.enable()`).
On 2020.0.3 both Layout and Modeler bind the UDP port but **do not execute** what arrives —
tested with plain commands, `cmdseq`, upper/lower case, CR/LF endings and `{topic}` ring
messages, including messages sent from inside the application itself. LightWave's own
`lwcommandport` client library is fire-and-forget anyway (no replies).

## Layout bridge

- A **Layout-class Master** (`LWMAST_LAYOUT`, survives Clear/Load Scene).
- It calls Win32 `SetTimer(NULL, 0, 100, proc)` through `ctypes`. A thread timer's callback is
  dispatched by the thread's message loop — Layout's main/UI thread — so `lwsdk.command(...)`,
  `LWItemInfo`, envelopes, etc. are safe to use from it.
- **PCore clears a plug-in file's module globals after activation** (they all become `None`), so
  all logic lives in `lw_mcp_layout_core.py`, imported as a normal module, and the Master keeps
  references to what it needs.
- **`inst_acquire()` is not called for a Master restored from the saved config** at startup, so the
  bridge also starts from `__init__`, `flags()` and `event()`.

## Modeler bridge

- `lwsdk.ModCommand()` outside a CommandSequence call is not valid (`lookup` returns 0, commands do
  nothing), and LightWave's `LWTimer` global isn't usable from Python (`timerAdd` is an unwrapped
  struct member). A Win32 timer *does* fire on Modeler's main thread, but there is no valid
  ModCommand there.
- So the server triggers the CommandSequence plug-in itself: it focuses the Modeler window and
  sends the user-assigned shortcut (`keybd_event`, with an Alt tap to lift the foreground lock and
  `AttachThreadInput` as fallback), then gives focus back. The plug-in processes every waiting
  `M*` request with a valid `ModCommand` (one undo group per request).
- About 1 in 4 keypresses was missed in testing; the server re-presses up to 3 times while the
  request file is still untouched.

## Gotchas

- Plug-in files declare `coding: ascii` — a single non-ASCII character (e.g. "…" in a comment) stops
  them loading.
- `LWInterfaceInfo.selected_items()` returns plain ints. To get usable item IDs after
  `LoadObject`/`Add*`, diff the `LWItemInfo.first/next` list before and after.
- Keying: `LWItemInfo.chanGroup` → `LWChannelInfo.nextChannel` (`Position.X` ... `Scale.Z`) →
  `channelEnvelope` → `LWEnvelopeFuncs.createKey(env, seconds, value)`. New items already have a
  key at frame 0 — overwrite it (`findKey` + `keySet(..., LWKEY_VALUE, v)`) or it leaks into the shot.
- Light intensity / flare intensity have no channels reachable this way; edit the `.lws`
  (`LightIntensity (envelope)` / `{ Envelope ... Key value time shape ... }`) and reload.
- `LWSurfaceFuncs.setFlt(surf, "BaseColor"/"Luminosity"/"GlowValue", [...])` works for rendering but
  is **not** written into LWO3 objects by Save/SaveAllObjects.
- `SaveRGBServer` does not set the output image format; set `RGBImageSaver LW_PNG24(.png)` in the `.lws`.
- FBX export from Layout: `Generic_ExportFBXCommand '<path>' Unity3DMode=False` — the target folder
  must already exist ("Plug-in ExportFBXCommand failed to start" otherwise).
- `LoadScene` may ask to change the content directory or to save modified objects; `RenderFrame`
  may ask to enable the Image Viewer; `RenderScene` ends on a Render Status panel. These modal
  panels pause the bridge until someone clicks.
