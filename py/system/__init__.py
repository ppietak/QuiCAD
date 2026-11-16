"""system -- what a model file imports to declare its geometry.

    from build123d import *
    from system import *

    show(link(), "link")                         # printed part: exported, drawn solid
    ref(screw(), "screw", at=Pos(0, 0, 5))        # reference geometry: drawn ghosted
    check_clear("link", "screw")                  # must not overlap
    check_overlap("link", "screw", why="bites")   # must overlap
    check_gap("link", "pin", min=0.2)             # minimum distance, mm
    dim("gap", (0, 0, 0), (7.35, 0, 0))           # dimension line in the 3D view
    scan("bracket.stl")                           # the scan this model reproduces

The calls only record; the build worker reads the record after the file ran.
Only the file being built declares things: a model imported from another one
(`from lid import lid`) keeps its own show() calls to itself.
`Assembly` is the older chained style (cad-khana), kept so existing files run.
"""

from __future__ import annotations

import os
import sys

__all__ = ["show", "ref", "check_clear", "check_overlap", "check_gap", "dim", "scan", "Assembly"]


class Run:
    """Everything one execution of a model file declared."""

    def __init__(self, main: str | None = None) -> None:
        self.main = main  # path of the file being built
        self.parts: list[dict] = []
        self.checks: list[dict] = []
        self.dims: list[dict] = []
        self.scans: list[dict] = []


_run = Run()


def _reset(main: str | None = None) -> Run:
    global _run
    _run = Run(main)
    return _run


def _imported() -> bool:
    """Whether the declaration being made comes from another model of the
    project, imported by the one being built."""
    if _run.main is None:
        return False
    frame = sys._getframe(1)
    while frame is not None and frame.f_globals.get("__name__") == __name__:
        frame = frame.f_back
    if frame is None:
        return False
    caller = os.path.abspath(frame.f_code.co_filename)
    return caller != _run.main and os.path.dirname(caller) == os.path.dirname(_run.main)


def _add(shape, name, kind, at):
    if _imported():
        return shape
    if shape is None:
        raise ValueError(f"{kind}(): the shape is None")
    taken = {p["name"] for p in _run.parts}
    if name is None:
        base = "part" if kind == "show" else "ref"
        name, n = base, 1
        while name in taken:
            n += 1
            name = f"{base}{n}"
    name = str(name)
    if name in taken:
        raise ValueError(f"{kind}(): a part named {name!r} already exists")
    _run.parts.append({"name": name, "shape": at * shape if at is not None else shape, "kind": kind})
    return shape


def show(shape, name: str | None = None, *, at=None):
    """A printed part: exported to STL and drawn solid."""
    return _add(shape, name, "show", at)


def ref(shape, name: str | None = None, *, at=None):
    """Reference geometry (hardware, mating parts): drawn ghosted, never exported."""
    return _add(shape, name, "ref", at)


def _check(kind, a, b, name, **extra):
    if _imported():
        return
    _run.checks.append({"kind": kind, "a": str(a), "b": str(b), "name": name or f"{a}_{kind}_{b}", **extra})


def check_clear(a: str, b: str, name: str | None = None) -> None:
    """Parts `a` and `b` must not overlap."""
    _check("clear", a, b, name)


def check_overlap(a: str, b: str, why: str = "", name: str | None = None) -> None:
    """Parts `a` and `b` must overlap (a documented, intended interference)."""
    _check("overlap", a, b, name, why=why)


def check_gap(a: str, b: str, min: float, name: str | None = None) -> None:  # noqa: A002
    """The distance between `a` and `b` must be at least `min` mm."""
    _check("gap", a, b, name, min=float(min))


def _xyz(p) -> list[float]:
    if hasattr(p, "X"):
        return [float(p.X), float(p.Y), float(p.Z)]
    x, y, z = p
    return [float(x), float(y), float(z)]


def dim(label: str, a, b, value: float | None = None) -> None:
    """A dimension line from point `a` to point `b`, labelled in the 3D view."""
    if _imported():
        return
    pa, pb = _xyz(a), _xyz(b)
    if value is None:
        value = sum((q - p) ** 2 for p, q in zip(pa, pb)) ** 0.5
    _run.dims.append({"label": str(label), "a": pa, "b": pb, "value": float(value)})


def scan(name: str, tol: float = 0.5) -> None:
    """This model reproduces the scan `name` (a file under scans/). Every build
    then measures how far the printed parts are from it; `tol` is the distance,
    in mm, within which a point counts as matching."""
    if _imported():
        return
    name = os.path.basename(str(name))
    if not name.lower().endswith(".stl"):
        name += ".stl"
    if all(entry["name"] != name for entry in _run.scans):
        _run.scans.append({"name": name, "tol": float(tol)})


class Assembly:
    """The chained style of older files: parts plus assertions between them."""

    def __init__(self) -> None:
        self.parts: list[dict] = []
        self.checks: list[dict] = []

    def _copy(self) -> "Assembly":
        other = Assembly()
        other.parts = list(self.parts)
        other.checks = list(self.checks)
        return other

    def with_part(self, name, part, location=None, color=None, material=None) -> "Assembly":
        other = self._copy()
        name = str(name)
        if any(p["name"] == name for p in other.parts):
            raise ValueError(f"with_part(): a part named {name!r} already exists")
        other.parts.append({"name": name, "shape": location * part if location is not None else part})
        return other

    def _with_check(self, kind, a, b, name, **extra) -> "Assembly":
        other = self._copy()
        other.checks.append({"kind": kind, "a": str(a), "b": str(b), "name": name or f"{a}_{kind}_{b}", **extra})
        return other

    def assert_no_interference(self, a, b, name=None) -> "Assembly":
        return self._with_check("clear", a, b, name)

    def assert_interference(self, a, b, reason="", name=None) -> "Assembly":
        return self._with_check("overlap", a, b, name, why=reason)

    def assert_clearance(self, a, b, min_mm, name=None) -> "Assembly":
        return self._with_check("gap", a, b, name, min=float(min_mm))

    def with_subassembly(self, *args, **kwargs):
        raise NotImplementedError("sub-assemblies and joints are not supported here; place parts with with_part()")

    with_joint = with_joint_angle = with_materials = with_detailed_geometry = with_subassembly
