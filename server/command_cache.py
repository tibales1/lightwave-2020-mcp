"""
Build the Layout / Modeler command lists from the user's own LightWave install.

The lists are not shipped with this project (they are derived from files that
come with LightWave). On first use the server reads:
  * <install>/bin/lwsdk/pris/layout/cs.py   -> Layout CommandSequence commands + argument names
  * <install>/support/python/lwcommandport/modeler/__init__.py -> Modeler commands
and caches the result as JSON next to this file.

Run it by hand with:  python tools/build_command_cache.py
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

# Common Modeler CommandSequence commands (names only) that the command-port
# module does not cover. Modeler accepts any CommandSequence command; this list
# just makes the usual ones discoverable.
EXTRA_MODELER = {
    "MAKEBOX": "MAKEBOX <x0 y0 z0> <x1 y1 z1> <nx ny nz>",
    "MAKEBALL": "MAKEBALL <rx ry rz> <sides> <segments> <cx cy cz>",
    "MAKECONE": "", "MAKEDISC": "", "NEW": "", "LOAD": "LOAD <filename>",
    "SAVE": "SAVE <filename>", "CLOSE": "", "CLOSE_ALL": "", "SURFACE": "SURFACE <name>",
    "SETLAYER": "SETLAYER <layer>", "SETBLAYER": "", "SETLAYERNAME": "", "SETOBJECT": "",
    "MOVE": "MOVE <dx dy dz>", "ROTATE": "ROTATE <angle> <axis X|Y|Z> <cx cy cz>",
    "SCALE": "SCALE <sx sy sz> <cx cy cz>", "SHEAR": "", "TAPER": "", "TWIST": "", "BEND": "",
    "EXTRUDE": "EXTRUDE <axis> <extent> <segments>", "LATHE": "", "BEVEL": "", "SHAPEBEVEL": "",
    "SMOOTHSHIFT": "", "MIRROR": "", "BOOLEAN": "", "SOLIDDRILL": "", "AXISDRILL": "",
    "SUBDIVIDE": "", "FRACSUBDIVIDE": "", "TRIPLE": "", "FLIP": "", "MERGEPOINTS": "",
    "MERGEPOLS": "", "WELDPOINTS": "", "UNWELD": "", "SKINPOLS": "", "JITTER": "", "QUANTIZE": "",
    "SMOOTH": "", "COPY": "", "CUT": "", "PASTE": "", "DELETE": "", "UNDO": "", "REDO": "",
    "SEL_POINT": "", "SEL_POLYGON": "", "SEL_INVERT": "", "SEL_HIDE": "", "SEL_UNHIDE": "",
    "INVERT_HIDE": "", "SETPIVOT": "", "CMDSEQ": "CMDSEQ <server> <argument>",
}


def build_layout(install: Path) -> list[dict]:
    src = install / "bin" / "lwsdk" / "pris" / "layout" / "cs.py"
    tree = ast.parse(src.read_text(encoding="utf-8", errors="ignore"))
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            doc = ast.get_docstring(node) or ""
            m = re.search(r"Layout CS function (\S+)", doc)
            if m:
                out.append({"name": m.group(1), "args": [a.arg for a in node.args.args]})
    return out


def build_modeler(install: Path) -> list[dict]:
    out: dict[str, str] = {}
    src = install / "support" / "python" / "lwcommandport" / "modeler" / "__init__.py"
    if src.exists():
        tree = ast.parse(src.read_text(encoding="utf-8", errors="ignore"))
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef):
                for f in cls.body:
                    if isinstance(f, ast.FunctionDef) and not f.name.startswith("_"):
                        doc = (ast.get_docstring(f) or "").strip().splitlines()
                        out[f.name.upper()] = doc[0] if doc else ""
    for k, v in EXTRA_MODELER.items():
        if v or k not in out:
            out[k] = v or out.get(k, "")
    return [{"name": k, "usage": out[k]} for k in sorted(out)]


def ensure(cache_dir: Path, install: Path) -> None:
    """Create the cache files if they are missing and the install is readable."""
    for name, fn in (("layout_commands.json", build_layout), ("modeler_commands.json", build_modeler)):
        target = cache_dir / name
        if target.exists():
            continue
        try:
            target.write_text(json.dumps(fn(install), indent=0), encoding="utf-8")
        except OSError:
            pass  # install not found / not readable: tools fall back to an empty list
