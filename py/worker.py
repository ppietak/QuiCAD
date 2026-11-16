#!/usr/bin/env python3
"""Build worker of the editor.

Imports build123d once (about two seconds), then serves build requests. Each
build runs in a process of its own that starts with every import already warm
(a fork, or where the system has none a spare process kept ready): it cannot
leak state into the next build, and can be killed when it hangs.

Protocol: one JSON object per line on stdin; one JSON reply per line on the file
descriptor named by APP_REPLY_FD (stdout when unset). Output printed by a model
never reaches the reply channel.

    {"id": 1, "op": "ping"}
    {"id": 2, "op": "build", "source": "...", "path": "/p/src/model.py", "cwd": "/p",
     "out": "/p/.sys/cache/<key>", "tol": 0.02, "ang": 0.2, "timeout": 60}
    {"id": 3, "op": "params", "source": "..."}
    {"id": 4, "op": "scan", "what": "overview", "path": "/p/scans/a.stl", "name": "a.stl",
     "out": "/p/.sys/cache/scan-<key>"}
    {"id": 5, "op": "scan", "what": "section", "path": "/p/scans/a.stl", "axis": "z", "at": 4.0}
    {"id": 6, "op": "scan", "what": "section", "path": "/p/scans/a.stl", "normal": [1, 1, 0], "at": 12.5}
    {"id": 7, "op": "scan", "what": "probe", "path": "/p/scans/a.stl", "point": [8, 10.2, 4], "radius": 3}
    {"id": 8, "op": "scan", "what": "view", "path": "/p/scans/a.stl", "name": "a.stl", "view": "iso",
     "size": 512, "out": "/p/.sys/cache/a-iso.png"}
    {"id": 9, "op": "view", "source": "...", "path": "/p/src/model.py", "cwd": "/p", "direction": [-1, 1, -1],
     "size": 512, "out": "/p/.sys/cache/model.png", "scans": ["a.stl"]}
    {"id": 10, "op": "face_at", "source": "...", "path": "/p/src/model.py", "cwd": "/p",
     "name": "link", "point": [1.0, 2.0, 3.0]}

Every reply carries the request's "id" and "ok". Any other op is a ping.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import traceback
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import guard  # noqa: E402
import system  # noqa: E402
import build123d  # noqa: E402  (the slow import; done once, in the parent)

try:  # measuring and drawing meshes (py/scan.py, needs numpy and scipy) is optional: without it scans are only shown
    import numpy as np  # noqa: E402
    import scan as scanlib  # noqa: E402
except ImportError:
    scanlib = None
from build123d import Compound, Shape, Vector  # noqa: E402
from OCP.BRepAdaptor import BRepAdaptor_Surface  # noqa: E402
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex  # noqa: E402
from OCP.BRepExtrema import BRepExtrema_DistShapeShape  # noqa: E402
from OCP.BRepGProp import BRepGProp, BRepGProp_Face  # noqa: E402
from OCP.BRepMesh import BRepMesh_IncrementalMesh  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus  # noqa: E402
from OCP.gp import gp_Pnt, gp_Vec  # noqa: E402
from OCP.GProp import GProp_GProps  # noqa: E402
from OCP.StlAPI import StlAPI_Writer  # noqa: E402

OVERLAP_EPS = 1e-3  # mm3; below this two parts count as not overlapping
SPAN_AREA_SHARE = 0.04  # a flat face this much of the part's largest one counts as "large"
MAX_SPANS = 5  # distances between parallel faces reported per part, the weightiest first
END_FACE_AREA_SHARE = 2 * SPAN_AREA_SHARE  # an end face must be larger still to get its width shown
MAX_END_FACES = 4  # of those widths, per part
MIN_END_WIDTH = 1.0  # mm; a narrower end face is not worth a label
END_WIDTH_MARGIN = 0.5  # mm; an end face this close to the part's full width adds nothing to the overall size
MIN_FACE_AREA = 1e-6  # mm2; a flat face smaller than this is a sliver, not a face
SAME_LEVEL = 1e-3  # mm; two offsets closer than this are the same plane
MIN_VOLUME = 1e-6  # mm3: a printed part with less is a degenerate shape, not a solid
FULL_TURN = 2 * math.pi
TURN_SLACK = 0.05  # rad; cylindrical faces this short of a full turn still close a hole
PICK_REACH = 1.0  # mm; a picked point further than this from every face is on no face
STL_HEADER = 84  # bytes of a binary STL before the triangles: 80 of text and a 4-byte count
STL_TRIANGLE = 50  # bytes per triangle


class ModelError(Exception):
    """A problem with what the model declared, as opposed to a crash inside it."""


# --- compatibility: files written for cad-khana run unchanged ----------------


def install_compat() -> None:
    def module(name: str, **attrs) -> None:
        m = types.ModuleType(name)
        m.__path__ = []  # lets `import a.b.c` treat it as a package
        m.__dict__.update(attrs)
        sys.modules[name] = m

    def noop(*_args, **_kwargs) -> None:
        return None

    class FDM:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    module("cad_khana")
    module("cad_khana.mechanism")
    module("cad_khana.mechanism.assembly", Assembly=system.Assembly)
    module("cad_khana.mechanism.check", check=noop)
    module("cad_khana.printability")
    module("cad_khana.printability.inspect", inspect=noop)
    module("cad_khana.printability.methods", FDM=FDM)
    module("cad_khana.export", export_glb=noop, export_animated_glb=noop)


# --- parameters: the numbers at the top of the file --------------------------


def _char_col(line: str, byte_col: int) -> int:
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", "ignore"))


def _number_literal(node):
    """The numeric value of a literal such as `7.35` or `-2`, else None."""
    sign = 1.0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        sign = -1.0 if isinstance(node.op, ast.USub) else 1.0
        node = node.operand
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return sign * node.value
    return None


def parse_params(source: str) -> list[dict]:
    """Top-level UPPER_CASE assignments. A literal number is editable in place
    (its exact position is returned); anything else is listed as derived and
    gets its value after the file has run."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()

    trailing: dict[int, str] = {}
    with contextlib.suppress(tokenize.TokenError, IndentationError, SyntaxError):
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                trailing[tok.start[0]] = tok.string.lstrip("#").strip()

    # A section header is a comment line that follows a blank line (or opens the
    # file) -- a comment glued under code is an explanation, not a header.
    headers: dict[int, str] = {}
    for i, line in enumerate(lines, start=1):
        text = line.strip()
        if text.startswith("#") and (i == 1 or not lines[i - 2].strip()):
            title = text.lstrip("#").strip().strip("-=").strip()
            if title:
                headers[i] = title[:60]
    header_lines = sorted(headers)

    def group_of(line_no: int):
        found = None
        for h in header_lines:
            if h > line_no:
                break
            found = headers[h]
        return found

    params = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        if name.startswith("_") or name != name.upper() or not any(c.isalpha() for c in name):
            continue
        entry = {
            "name": name,
            "line": node.lineno,
            "comment": trailing.get(node.end_lineno) or None,
            "group": group_of(node.lineno),
        }
        value = _number_literal(node.value)
        if value is not None and node.value.lineno == node.value.end_lineno:
            text = lines[node.value.lineno - 1]
            start = _char_col(text, node.value.col_offset)
            end = _char_col(text, node.value.end_col_offset)
            entry.update(kind="value", value=value, raw=text[start:end], start=start, end=end)
        else:
            entry.update(kind="derived", value=None)
        params.append(entry)
    return params


# --- measuring ---------------------------------------------------------------


def _volume(topods) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(topods, props)
    return float(props.Mass())


def _bbox(shape) -> list[list[float]]:
    bb = shape.bounding_box()
    return [[bb.min.X, bb.min.Y, bb.min.Z], [bb.max.X, bb.max.Y, bb.max.Z]]


def _boxes_apart(a, b) -> bool:
    (a0, a1), (b0, b1) = a, b
    return any(a1[i] < b0[i] or b1[i] < a0[i] for i in range(3))


def overlap_volume(a, b, box_a, box_b) -> float:
    if _boxes_apart(box_a, box_b):
        return 0.0  # skips the boolean, the expensive part of a check
    common = BRepAlgoAPI_Common(a.wrapped, b.wrapped)
    common.Build()
    return _volume(common.Shape()) if common.IsDone() else 0.0


def _normal(face, u: float, v: float) -> Vector:
    """The outward normal of a face at (u, v) on its surface. Face.normal_at(point)
    gives the same, but first projects the point onto the surface to find (u, v)."""
    point, normal = gp_Pnt(), gp_Vec()
    BRepGProp_Face(face.wrapped).Normal(u, v, point, normal)
    return Vector(normal).normalized()


def face_surfaces(faces) -> list[tuple]:
    """(face, its surface adaptor) for every face: what the measurements below share."""
    return [(face, BRepAdaptor_Surface(face.wrapped)) for face in faces]


def find_holes(surfaces: list[tuple]) -> list[dict]:
    """Round holes: inward-facing cylindrical faces that together close a full turn."""
    groups: dict[tuple, dict] = {}
    for face, surface in surfaces:
        if surface.GetType() != GeomAbs_Cylinder:
            continue
        cylinder = surface.Cylinder()
        radius = cylinder.Radius()
        origin = Vector(cylinder.Axis().Location())
        along = Vector(cylinder.Axis().Direction())  # the way OCCT oriented the axis: v runs along it
        direction = along
        u1, u2, v1, v2 = BRepTools.UVBounds_s(face.wrapped)
        u, v = (u1 + u2) / 2.0, (v1 + v2) / 2.0
        point = Vector(surface.Value(u, v))
        radial = (point - origin) - direction * (point - origin).dot(direction)
        if _normal(face, u, v).dot(radial) > 0:
            continue  # the outside of a boss, not the inside of a hole
        # One key per axis line, whichever way OCCT oriented it.
        flip = next((c < 0 for c in (direction.X, direction.Y, direction.Z) if abs(c) > 1e-9), False)
        if flip:
            direction = direction * -1.0
        foot = origin - direction * origin.dot(direction)
        key = tuple(round(c, 3) for c in (direction.X, direction.Y, direction.Z, foot.X, foot.Y, foot.Z, radius))
        ends = [(origin + along * v).dot(direction) for v in (v1, v2)]
        group = groups.setdefault(key, {"turn": 0.0, "lo": min(ends), "hi": max(ends), "dir": direction, "foot": foot, "r": radius})
        group["turn"] += abs(u2 - u1)
        group["lo"] = min(group["lo"], *ends)
        group["hi"] = max(group["hi"], *ends)

    holes = []
    for g in groups.values():
        if g["turn"] < FULL_TURN - TURN_SLACK:
            continue  # an arc: a fillet or a slot end
        a = g["foot"] + g["dir"] * g["lo"]
        b = g["foot"] + g["dir"] * g["hi"]
        holes.append({
            "d": round(2.0 * g["r"], 4),
            "a": [a.X, a.Y, a.Z],
            "b": [b.X, b.Y, b.Z],
            "depth": round(g["hi"] - g["lo"], 4),
        })
    holes.sort(key=lambda h: (h["d"], h["a"]))
    return holes


def flat_faces(surfaces: list[tuple]) -> list[tuple]:
    """(face, area, centre, outward normal) of every flat face."""
    flats = []
    for face, surface in surfaces:
        if surface.GetType() != GeomAbs_Plane:
            continue
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face.wrapped, props)
        centre = props.CentreOfMass()
        u1, u2, v1, v2 = BRepTools.UVBounds_s(face.wrapped)
        normal = _normal(face, (u1 + u2) / 2.0, (v1 + v2) / 2.0)  # the same all over a plane
        flats.append((face, float(props.Mass()), Vector(centre.X(), centre.Y(), centre.Z()), normal))
    return flats


def find_spans(surfaces: list[tuple], box) -> list[dict]:
    """Distances between the larger parallel flat faces of a part: the thickness
    of a wall, the width of a slot, the height of a step. Neighbouring planes
    only, and never the overall size, which is shown anyway. The widths of
    narrow end faces (see end_faces) are ranked in among them."""
    flats = flat_faces(surfaces)
    # direction -> {(offset, sense): plane}; sense says which way the face looks.
    directions: dict[tuple, dict[tuple, dict]] = {}
    largest = 0.0
    for _face, area, centre, normal in flats:
        if area < MIN_FACE_AREA:
            continue
        flip = next((c < 0 for c in (normal.X, normal.Y, normal.Z) if abs(c) > 1e-6), False)
        axis = normal * -1.0 if flip else normal
        key = tuple(round(c, 3) + 0.0 for c in (axis.X, axis.Y, axis.Z))
        offset = round(centre.dot(axis), 3)
        plane = directions.setdefault(key, {}).setdefault((offset, -1 if flip else 1), {"area": 0.0, "face": 0.0, "at": centre})
        plane["area"] += area
        if area > plane["face"]:
            plane["face"], plane["at"] = area, centre  # dimension from the plane's biggest face
        largest = max(largest, plane["area"])

    size = [box[1][i] - box[0][i] for i in range(3)]
    found = []
    for key, planes in directions.items():
        axis = Vector(*key)
        levels = sorted(((offset, sense, plane) for (offset, sense), plane in planes.items()
                         if plane["area"] >= SPAN_AREA_SHARE * largest), key=lambda level: level[0])
        principal = next((i for i in range(3) if abs(abs(key[i]) - 1.0) < 1e-6), None)
        seen: set[tuple] = set()
        for (low, low_sense, low_plane), (high, high_sense, high_plane) in zip(levels, levels[1:]):
            distance = high - low
            if distance < SAME_LEVEL:
                continue  # two sides of the same plane
            if principal is not None and abs(distance - size[principal]) < SAME_LEVEL:
                continue  # the overall size
            kind = "wall" if (low_sense, high_sense) == (-1, 1) else "gap" if (low_sense, high_sense) == (1, -1) else "step"
            if (kind, round(distance, 3)) in seen:
                continue  # the same wall again on the other side
            seen.add((kind, round(distance, 3)))
            # Measured from the smaller of the two faces, straight across to the other plane.
            start, start_offset, end_offset = (low_plane["at"], low, high) if low_plane["area"] <= high_plane["area"] else (high_plane["at"], high, low)
            end = start + axis * (end_offset - start_offset)
            found.append({
                "kind": kind,
                "value": round(distance, 4),
                "a": [start.X, start.Y, start.Z],
                "b": [end.X, end.Y, end.Z],
                "weight": min(low_plane["area"], high_plane["area"]),
            })
    found.sort(key=lambda span: -span["weight"])
    # One label per length, whatever it measures and wherever it sits: the
    # mirrored twin of a wall, or a slot as wide as the step beside it, says
    # nothing new and only crowds the view.
    seen_all: set[tuple] = set()
    kept = []
    # Material thickness and openings read at a glance, so they go first; a step
    # (two faces looking the same way) only when there is room left.
    ranked = ([span for span in found if span["kind"] != "step"]
              + end_faces(flats, box, largest)
              + [span for span in found if span["kind"] == "step"])
    for span in ranked:
        key = round(span["value"], 2)
        if key in seen_all:
            continue
        seen_all.add(key)
        kept.append({k: v for k, v in span.items() if k != "weight"})
    return kept[:MAX_SPANS]


def end_faces(flats: list[tuple], box, largest: float) -> list[dict]:
    """A large flat face on the outside of a part (at either end along X, Y or
    Z) that is narrower than the part itself: its width, which the overall
    dimensions do not show. Measured along the face's own edge. `flats` comes
    from flat_faces(); `largest` is the area of the part's largest plane."""
    size = [box[1][i] - box[0][i] for i in range(3)]
    found = []
    for face, area, _centre, normal in flats:
        if area < END_FACE_AREA_SHARE * largest:
            continue
        axis = next((i for i in range(3) if abs(abs((normal.X, normal.Y, normal.Z)[i]) - 1.0) < 1e-6), None)
        if axis is None:
            continue
        fb = _bbox(face)
        level = fb[0][axis]
        if min(abs(level - box[0][axis]), abs(level - box[1][axis])) > SAME_LEVEL:
            continue  # not at an end of the part
        for across in (i for i in range(3) if i != axis):
            width = fb[1][across] - fb[0][across]
            if width < MIN_END_WIDTH or width > size[across] - END_WIDTH_MARGIN:
                continue  # tiny, or as wide as the part (the overall dimension shows it)
            other = 3 - axis - across
            a = [0.0, 0.0, 0.0]
            a[axis] = level
            a[other] = fb[0][other]
            a[across] = fb[0][across]
            b = list(a)
            b[across] = fb[1][across]
            found.append({"kind": "face", "value": round(width, 4), "a": a, "b": b, "weight": area})
    found.sort(key=lambda span: -span["weight"])
    seen: set[tuple] = set()
    kept = []
    for span in found:
        # One label per width and direction: a face repeated round the part needs it once.
        key = (round(span["value"], 3), tuple(round(q - p, 3) != 0 for p, q in zip(span["a"], span["b"])))
        if key in seen:
            continue
        seen.add(key)
        kept.append({k: v for k, v in span.items() if k != "weight"})
    return kept[:MAX_END_FACES]


def write_stl(shape, path: str, tol: float, ang: float) -> int:
    """Tessellate and write a binary STL; returns the triangle count."""
    BRepMesh_IncrementalMesh(shape.wrapped, tol, False, ang, True)
    writer = StlAPI_Writer()
    writer.ASCIIMode = False
    if not writer.Write(shape.wrapped, path) or not os.path.exists(path):
        raise ModelError(f"could not tessellate {os.path.basename(path)[:-4]!r} (is it a solid?)")
    return max(0, (os.path.getsize(path) - STL_HEADER) // STL_TRIANGLE)


def join_stl(paths: list[str], merged: str) -> None:
    """Several binary STLs as one file: byte for byte what tessellating the
    parts together writes, without meshing them a second time."""
    bodies = []
    for path in paths:
        with open(path, "rb") as part:
            bodies.append(part.read())
    count = sum((len(body) - STL_HEADER) // STL_TRIANGLE for body in bodies)
    with open(merged, "wb") as whole:
        whole.write(bodies[0][:STL_HEADER - 4] + count.to_bytes(4, "little"))
        whole.writelines(body[STL_HEADER:] for body in bodies)


# --- one build (runs in the child) -------------------------------------------


def as_shape(value, what: str):
    if isinstance(value, (list, tuple)):
        value = Compound(children=list(value))
    if not isinstance(value, Shape):
        raise ModelError(f"{what} is a {type(value).__name__}, not a build123d shape")
    return value


def inspected_names(source: str) -> set[str]:
    """Part names the file checks for printability: inspect(..., name="pod")."""
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "inspect":
            for keyword in node.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    names.add(keyword.value.value)
    return names


def collect(ns: dict, run, source: str) -> None:
    """What to build when the file did not call show(): the older conventions.

    Printed parts are the ones a build_printable*() function returns or the file
    inspects for printability; the rest of `assembly` is reference geometry.
    With neither hint, every part of `assembly` counts as printed.
    """
    if run.parts:
        return
    assembly = ns.get("assembly")
    printed: dict[str, object] = {}
    for key, value in list(ns.items()):
        if key.startswith("build_printable") and isinstance(value, types.FunctionType):
            if value.__code__.co_argcount > len(value.__defaults__ or ()):
                continue  # needs arguments we cannot guess
            result = value()
            if not isinstance(result, system.Assembly):
                raise ModelError(f"{key}() must return an Assembly")
            for part in result.parts:
                printed.setdefault(part["name"], part["shape"])
    if isinstance(assembly, system.Assembly):
        if not printed:
            wanted = inspected_names(source)
            printed = {p["name"]: p["shape"] for p in assembly.parts if p["name"] in wanted}
        if not printed:
            printed = {p["name"]: p["shape"] for p in assembly.parts}
        for name, shape in printed.items():
            system.show(shape, name)
        for part in assembly.parts:
            if part["name"] not in printed:
                system.ref(part["shape"], part["name"])
        run.checks.extend(assembly.checks)
        return
    if printed:
        for name, shape in printed.items():
            system.show(shape, name)
        return
    for key in ("result", "part", "model"):
        if isinstance(ns.get(key), (Shape, list, tuple)):
            system.show(ns[key], key)
            return
    raise ModelError('the model shows nothing: call show(shape, "name") at the end of the file')


def describe_error(exc: BaseException, path: str, source: str) -> dict:
    lines = source.splitlines()
    line = None
    stack: list[list] = []
    message = str(exc)
    if isinstance(exc, SyntaxError) and exc.filename == path:
        line, message = exc.lineno, exc.msg
    else:
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == path]
        if frames:
            line = frames[-1].lineno
            stack = [[f.lineno, f.name] for f in frames][-4:]
    if isinstance(exc, SystemExit):
        message = f"the file exited (code {exc.code})"
    text = lines[line - 1].strip() if line and 0 < line <= len(lines) else None
    return {
        "type": type(exc).__name__,
        "message": (message or type(exc).__name__)[:800],
        "line": line,
        "text": text,
        "stack": stack,
    }


def safe_name(name: str, taken: set[str]) -> str:
    """A file name for a part, unique among `taken` whatever the case: on a
    case-insensitive disk "Arm" and "arm" would be one file."""
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "part"
    if base.lower() == "model":
        base += "_part"  # model.stl is the merged file
    candidate, n = base, 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{base}_{n}"
    taken.add(candidate.lower())
    return candidate


def run_model(req: dict, printed: io.StringIO) -> tuple[system.Run, dict]:
    """Execute the model file from its project directory. Returns what it
    declared and its globals; whatever it printed goes to `printed`."""
    source, path = req["source"], req["path"]
    os.chdir(req.get("cwd") or os.path.dirname(path))
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))  # so a model can import its neighbours
    sys.stdin = open(os.devnull)  # input() must not read the worker's requests
    run = system._reset(os.path.abspath(path))
    ns: dict = {"__name__": "__model__", "__file__": path}
    with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
        exec(compile(source, path, "exec"), ns)
        collect(ns, run, source)
    return run, ns


def run_build(req: dict) -> dict:
    started = time.perf_counter()
    source, path, out = req["source"], req["path"], req["out"]
    tol, ang = float(req.get("tol", 0.02)), float(req.get("ang", 0.2))
    result: dict = {"ok": False, "params": parse_params(source)}
    printed = io.StringIO()
    timing: dict[str, int] = {}

    def lap(label: str, since: float) -> float:
        now = time.perf_counter()
        timing[label] = round((now - since) * 1000)
        return now

    try:
        run, ns = run_model(req, printed)
        t = lap("run", started)

        for entry in result["params"]:
            if entry["kind"] == "derived":
                value = ns.get(entry["name"])
                entry["value"] = float(value) if type(value) in (int, float) else None
        result["params"] = [p for p in result["params"] if p["value"] is not None]

        shapes: dict[str, Shape] = {}
        faces: dict[str, list] = {}
        boxes: dict[str, list] = {}
        parts = []
        for part in run.parts:
            shape = as_shape(part["shape"], f"part {part['name']!r}")
            shapes[part["name"]] = shape
            faces[part["name"]] = shape.faces()
            boxes[part["name"]] = _bbox(shape)
            valid = shape.is_valid  # a property; a method in older build123d
            parts.append({
                "name": part["name"],
                "kind": part["kind"],
                "bbox": boxes[part["name"]],
                "volume": float(shape.volume),
                "solids": len(shape.solids()),
                "faces": len(faces[part["name"]]),
                "valid": bool(valid() if callable(valid) else valid),
            })
        shown = [p for p in parts if p["kind"] == "show"]
        if not shown:
            raise ModelError('nothing to print: at least one part must be declared with show(), not ref()')
        for info in shown:
            if info["solids"] == 0 or info["volume"] < MIN_VOLUME:
                raise ModelError(f"part {info['name']!r} is not a solid (volume {info['volume']:.3g} mm3), so there is nothing to print")

        checks = []
        for info in parts:
            if not info["valid"]:
                checks.append({"name": f"{info['name']} geometry", "kind": "valid", "a": info["name"], "b": info["name"],
                               "ok": False, "detail": "the shape is not a valid solid (open or self-intersecting)"})
        for check in run.checks:
            entry = {"name": check["name"], "kind": check["kind"], "a": check["a"], "b": check["b"]}
            missing = [n for n in (check["a"], check["b"]) if n not in shapes]
            if missing:
                entry.update(ok=False, detail=f"no part named {missing[0]!r}")
            elif check["kind"] == "gap":
                gap = BRepExtrema_DistShapeShape(shapes[check["a"]].wrapped, shapes[check["b"]].wrapped).Value()
                entry.update(ok=gap >= check["min"] - 1e-9, detail=f"gap {gap:.3f} mm, needs {check['min']:g}")
            else:
                vol = overlap_volume(shapes[check["a"]], shapes[check["b"]], boxes[check["a"]], boxes[check["b"]])
                overlapping = vol > OVERLAP_EPS
                entry.update(ok=overlapping == (check["kind"] == "overlap"), detail=f"overlap {vol:.3f} mm3")
                if check.get("why"):
                    entry["why"] = check["why"]
            checks.append(entry)
        t = lap("checks", t)

        os.makedirs(out, exist_ok=True)
        taken: set[str] = set()
        for info in parts:
            info["file"] = safe_name(info["name"], taken) + ".stl"
            info["triangles"] = write_stl(shapes[info["name"]], os.path.join(out, info["file"]), tol, ang)
            if info["kind"] == "show":
                surfaces = face_surfaces(faces[info["name"]])
                info["holes"] = find_holes(surfaces)
                with contextlib.suppress(Exception):  # extra dimensions are a help, not a need
                    info["spans"] = find_spans(surfaces, info["bbox"])
        merged = os.path.join(out, "model.stl")
        join_stl([os.path.join(out, p["file"]) for p in shown], merged)
        t = lap("mesh", t)

        # A model that reproduces a scan is measured against it on every build.
        fits = []
        printed_mesh = None
        for declared in run.scans:
            name = declared["name"]
            scan_file = os.path.join(os.getcwd(), "scans", name)
            if not os.path.isfile(scan_file):
                checks.append({"name": f"scan {name}", "kind": "scan", "a": name, "b": name, "ok": False,
                               "detail": f"there is no scan named {name!r} in this project"})
                continue
            if scanlib is None:
                fits.append({"name": name})  # shown over the model, not compared
                continue
            try:
                if printed_mesh is None:
                    printed_mesh = scanlib.load(merged)
                deviation = scanlib.deviation(printed_mesh, scanlib.load(scan_file), tol=declared["tol"])
                fits.append({"name": name, "deviation": deviation, "text": scanlib.deviation_text(deviation, name)})
            except Exception as exc:  # noqa: BLE001 -- a bad mesh must not fail the build
                fits.append({"name": name, "text": f"scan {name}: could not be compared ({type(exc).__name__}: {exc})"[:300]})
        if run.scans:
            t = lap("scan", t)

        lo = [min(p["bbox"][0][i] for p in shown) for i in range(3)]
        hi = [max(p["bbox"][1][i] for p in shown) for i in range(3)]
        result.update(
            ok=True,
            parts=parts,
            checks=checks,
            dims=run.dims,
            bbox=[lo, hi],
            volume=sum(p["volume"] for p in shown),
            scans=fits,
        )
    except BaseException as exc:  # noqa: BLE001 -- a model may raise anything, SystemExit included
        result["error"] = describe_error(exc, path, source)

    timing["total"] = round((time.perf_counter() - started) * 1000)
    result["ms"] = timing
    output = printed.getvalue().strip()
    if output:
        result["output"] = output[-1500:]
    return result


# --- measuring a scan (runs in the child) --------------------------------------


def run_scan(req: dict) -> dict:
    if scanlib is None:
        return {"ok": False, "error": {"type": "Unavailable", "message": "measuring scans is not available in this version"}}
    triangles = scanlib.load(req["path"])
    name = req.get("name") or os.path.basename(req["path"])
    what = req.get("what")
    if what == "section":  # the plane is axis = at ('x' | 'y' | 'z') or normal . p = at (normal: [nx, ny, nz])
        cut = scanlib.section(triangles, req.get("axis"), float(req["at"]), normal=req.get("normal"))
        return {"ok": True, "text": scanlib.section_text(cut)}
    if what == "probe":
        found = scanlib.probe(triangles, req["point"], req.get("radius"))
        return {"ok": True, "info": found, "text": scanlib.probe_text(found)}
    if what == "view":
        return write_view(req, triangles, None, f"scan {name}")
    info = scanlib.overview(triangles, name)
    result = {"ok": True, "info": info, "text": scanlib.overview_text(info), "views": False}
    out = req.get("out")
    if out:
        os.makedirs(out, exist_ok=True)
        with contextlib.suppress(Exception):  # the picture is a help, not a need
            with open(os.path.join(out, "views.png"), "wb") as picture:
                picture.write(scanlib.render_views(triangles))
            result["views"] = True
    return result


# --- a picture from any direction (runs in the child) -------------------------


def write_view(req: dict, triangles, tones, subject: str) -> dict:
    """Render `triangles` as the request asks ("view": a name, or "direction": the vector the camera
    looks along; "size", "up", "marks") into the PNG file "out". The reply names the file, never holds it."""
    direction = req.get("direction")
    if direction is None:
        direction = scanlib.view_direction(req.get("view") or "iso")
    size = int(req.get("size") or 512)
    picture = scanlib.render(triangles, None, direction, size=size, up=req.get("up"), marks=req.get("marks"), tones=tones, clear=req.get("clear") is True)
    out = req["out"]
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as handle:
        handle.write(picture)
    named = "" if req.get("direction") is not None else f"{req.get('view') or 'iso'} view, "
    caption = scanlib.view_caption(triangles, direction, size=size, up=req.get("up"))
    return {"ok": True, "png": out, "text": f"{subject}, {named}{caption}"}


def run_view(req: dict) -> dict:
    """A picture of the model as it is now: its shown parts (reference parts only when there are no
    others), optionally with scans of the project drawn over it in a second tone."""
    if scanlib is None:
        return {"ok": False, "error": {"type": "Unavailable", "message": "pictures are not available in this version"}}
    run, _ns = run_model(req, io.StringIO())
    parts = [p for p in run.parts if p["kind"] == "show"] or list(run.parts)
    if not parts:
        raise ModelError('the model shows nothing: call show(shape, "name") at the end of the file')
    tol, ang = float(req.get("tol", 0.05)), float(req.get("ang", 0.2))
    meshes, names = [], []
    with tempfile.TemporaryDirectory() as folder:
        for index, part in enumerate(parts):
            file = os.path.join(folder, f"{index}.stl")
            write_stl(as_shape(part["shape"], f"part {part['name']!r}"), file, tol, ang)
            meshes.append(scanlib.load(file))
            names.append(part["name"])
    model_count = sum(len(mesh) for mesh in meshes)
    drawn_scans = []
    for name in req.get("scans") or []:
        scan_file = os.path.join(os.getcwd(), "scans", os.path.basename(str(name)))
        if os.path.isfile(scan_file):
            meshes.append(scanlib.load(scan_file))
            drawn_scans.append(os.path.basename(str(name)))
    triangles = np.concatenate(meshes)
    tones = np.zeros(len(triangles), dtype=np.int64)
    tones[model_count:] = 1
    subject = "model (" + ", ".join(names) + ")"
    if drawn_scans:
        subject += " in blue with scan " + ", ".join(drawn_scans) + " in orange (orange shows where the scan lies outside the model)"
    return write_view(req, triangles, tones if drawn_scans else None, subject)


# --- what is under a point the user picked (runs in the child) ---------------


def _r(v, n=2):
    return round(float(v), n) + 0.0


def _vec(v):
    return "(" + ", ".join(f"{_r(c):g}" for c in (v.X(), v.Y(), v.Z())) + ")"


def describe_face(face, point) -> str:
    """The geometry of one B-rep face in plain words, whatever its type."""
    surf = BRepAdaptor_Surface(face.wrapped)
    kind = surf.GetType()
    p = gp_Pnt(*point)
    at = face.normal_at(Vector(*point))
    out_normal = (at.X, at.Y, at.Z)
    box = face.bounding_box()
    size = f"face area {_r(face.area, 1):g} mm2, extent {_r(box.size.X):g} x {_r(box.size.Y):g} x {_r(box.size.Z):g} mm"

    def axial(ax):
        return ax.Location(), ax.Direction()

    if kind == GeomAbs_Plane:
        pl = surf.Plane()
        n = pl.Axis().Direction()
        sign = 1 if (n.X() * out_normal[0] + n.Y() * out_normal[1] + n.Z() * out_normal[2]) >= 0 else -1
        nx, ny, nz = sign * n.X(), sign * n.Y(), sign * n.Z()
        offset = nx * point[0] + ny * point[1] + nz * point[2]
        return (f"a flat face, outward normal ({_r(nx):g}, {_r(ny):g}, {_r(nz):g}), "
                f"lying at {_r(offset):g} mm along that normal from the origin; {size}")
    if kind in (GeomAbs_Cylinder, GeomAbs_Cone):
        if kind == GeomAbs_Cylinder:
            g = surf.Cylinder(); loc, d = axial(g.Axis()); head = f"a cylindrical face, diameter {_r(2 * g.Radius()):g} mm"
        else:
            g = surf.Cone(); loc, d = axial(g.Axis())
            head = f"a conical face, half-angle {_r(math.degrees(abs(g.SemiAngle())), 1):g} deg, apex {_vec(g.Apex())}"
        # From the axis to the point; an outward normal pointing back at the axis means a bore.
        t = (p.X() - loc.X()) * d.X() + (p.Y() - loc.Y()) * d.Y() + (p.Z() - loc.Z()) * d.Z()
        foot = (loc.X() + t * d.X(), loc.Y() + t * d.Y(), loc.Z() + t * d.Z())
        radial = [point[i] - foot[i] for i in range(3)]
        concave = sum(radial[i] * out_normal[i] for i in range(3)) < 0
        if kind == GeomAbs_Cone:
            r = math.sqrt(sum(c * c for c in radial)); head += f", diameter {_r(2 * r):g} mm at the point"
        span = abs(surf.LastUParameter() - surf.FirstUParameter())
        arc = "full round" if span > 2 * math.pi - 1e-3 else f"an arc of {_r(math.degrees(span), 0):g} deg"
        return (f"{head}, {arc}, axis through ({', '.join(f'{_r(c):g}' for c in foot)}) along {_vec(d)}, "
                f"{'concave (the inside of a bore, hole or inner round)' if concave else 'convex (the outside of a boss, pin or outer round)'}; {size}")
    if kind == GeomAbs_Sphere:
        g = surf.Sphere()
        return f"a spherical face, diameter {_r(2 * g.Radius()):g} mm, centre {_vec(g.Location())}; {size}"
    if kind == GeomAbs_Torus:
        g = surf.Torus(); loc, d = axial(g.Axis())
        return (f"a toroidal face (a round over a curved edge), tube radius {_r(g.MinorRadius()):g} mm, ring radius "
                f"{_r(g.MajorRadius()):g} mm, axis through {_vec(loc)} along {_vec(d)}; {size}")
    return f"a freeform (spline) face, outward normal at the point ({', '.join(f'{_r(c):g}' for c in out_normal)}); {size}"


def run_face_at(req: dict) -> dict:
    run, _ns = run_model(req, io.StringIO())
    part = next((p for p in run.parts if p["name"] == req.get("name")), None)
    if part is None:
        return {"ok": False}
    shape = as_shape(part["shape"], "part")
    point = tuple(float(c) for c in req["point"])
    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    best, best_d = None, float("inf")
    for face in shape.faces():
        d = BRepExtrema_DistShapeShape(vertex, face.wrapped).Value()
        if d < best_d:
            best, best_d = face, d
    if best is None or best_d > PICK_REACH:
        return {"ok": False}
    return {"ok": True, "text": describe_face(best, point)}


# --- the parent: run each job in a process of its own, wait, relay ------------
#
# Where the system can fork, a job runs in a fork of this warm process. Where it
# cannot (Windows), warm spare processes are kept instead: each imports
# everything, waits for one job, does it and exits; a new one is started the
# moment one is taken. APP_NO_FORK=1 chooses the spares anywhere.

WORK = {}  # op -> (function, what a failure message calls it); filled below serve()
CAN_FORK = hasattr(os, "fork") and not os.environ.get("APP_NO_FORK")
SPARES = 2          # warm processes kept ready
WARMUP_S = 60.0     # what a spare may need to import everything


def failure(kind: str, message: str) -> dict:
    return {"ok": False, "error": {"type": kind, "message": message}}


def attempt(work, req: dict) -> str:
    try:
        return json.dumps(work(req))
    except BaseException as exc:  # noqa: BLE001 -- also a result that is not JSON
        return json.dumps(failure(type(exc).__name__, str(exc)[:800]))


def in_fork(op: str, req: dict) -> dict:
    work, what = WORK[op]
    timeout = float(req.get("timeout", 60))
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            guard.install(req)  # from here on this process can only touch the job's own project
            payload = attempt(work, req)
            with os.fdopen(write_fd, "w") as pipe:
                pipe.write(payload)
        finally:
            os._exit(0)  # whatever happened: the child must never return into the parent's loop

    os.close(write_fd)
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    timed_out = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            os.kill(pid, signal.SIGKILL)
            break
        ready, _, _ = select.select([read_fd], [], [], left)
        if not ready:
            continue
        data = os.read(read_fd, 1 << 16)
        if not data:
            break
        chunks.append(data)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)

    if timed_out:
        return failure("Timeout", f"the {what} took longer than {timeout:g} s and was stopped")
    if not chunks:
        how = f"signal {os.WTERMSIG(status)}" if os.WIFSIGNALED(status) else f"exit code {os.WEXITSTATUS(status)}"
        return failure("Crash", f"the {what} crashed ({how})")
    return json.loads(b"".join(chunks))


class Spares:
    """Warm processes waiting for one job each."""

    def __init__(self) -> None:
        self.waiting: list[tuple] = []

    def start(self) -> None:
        process = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--once"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        warm = threading.Event()

        def watch() -> None:  # its first line says that everything is imported
            process.stdout.readline()
            warm.set()

        threading.Thread(target=watch, daemon=True).start()
        self.waiting.append((process, warm))

    def fill(self) -> None:
        self.waiting = [(process, warm) for process, warm in self.waiting if process.poll() is None]
        while len(self.waiting) < SPARES:
            self.start()

    def run(self, op: str, req: dict) -> dict:
        _, what = WORK[op]
        timeout = float(req.get("timeout", 60))
        self.fill()
        process, warm = self.waiting.pop(0)
        self.start()  # the next one warms up while this one works
        if not warm.wait(WARMUP_S) or process.poll() is not None:
            process.kill()
            return failure("Crash", "the CAD engine did not start")
        try:
            out, _ = process.communicate(json.dumps({**req, "op": op}).encode() + b"\n", timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return failure("Timeout", f"the {what} took longer than {timeout:g} s and was stopped")
        except OSError as exc:
            return failure("Crash", f"the {what} crashed ({exc})")
        if not out.strip():
            return failure("Crash", f"the {what} crashed (exit code {process.returncode})")
        return json.loads(out)

    def stop(self) -> None:
        for process, _ in self.waiting:
            process.kill()
        self.waiting = []


def serve_once() -> None:
    """A spare: everything is imported by now; do the one job that arrives, then end."""
    # The reply goes out on what was stdout; nothing a model prints may reach it.
    reply = os.fdopen(os.dup(1), "w")
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    install_compat()
    reply.write("ready\n")
    reply.flush()
    line = sys.stdin.buffer.readline()
    if not line.strip():
        return  # the worker went away
    req = json.loads(line)
    work, _ = WORK[req["op"]]
    guard.install(req)  # from here on this process can only touch the job's own project
    reply.write(attempt(work, req))
    reply.flush()
    os._exit(0)


def serve() -> None:
    reply_fd = os.environ.get("APP_REPLY_FD")
    if reply_fd:
        reply = os.fdopen(int(reply_fd), "w")
    else:
        # The reply goes out on what was stdout; nothing a job's process writes
        # there (a print outside a model, the CAD kernel's own messages) may reach it.
        sys.stdout.flush()
        reply = os.fdopen(os.dup(1), "w")
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 1)
        os.close(null)
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the app decides when to stop the worker
    install_compat()
    spares = None if CAN_FORK else Spares()
    run = in_fork if CAN_FORK else spares.run
    if spares:
        spares.fill()

    def send(payload: dict) -> None:
        reply.write(json.dumps(payload) + "\n")
        reply.flush()

    send({"id": 0, "ready": True, "build123d": build123d.__version__, "python": sys.version.split()[0], "scan": scanlib is not None})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(req, dict):
            continue
        try:
            if req.get("op") in WORK:
                send({"id": req.get("id"), **run(req["op"], req)})
            elif req.get("op") == "params":
                send({"id": req.get("id"), "ok": True, "params": parse_params(req.get("source", ""))})
            else:
                send({"id": req.get("id"), "ok": True})
        except Exception as exc:  # noqa: BLE001 -- the worker must survive a bad request
            send({"id": req.get("id"), "ok": False, "error": {"type": type(exc).__name__, "message": str(exc)[:800]}})
    if spares:
        spares.stop()


WORK.update({
    "build": (run_build, "build"),
    "scan": (run_scan, "measurement"),
    "view": (run_view, "picture"),
    "face_at": (run_face_at, "lookup"),
})

if __name__ == "__main__":
    if "--once" in sys.argv[1:]:
        serve_once()
    else:
        serve()
