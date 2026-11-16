"""Measure a triangle mesh (an uploaded STL) and describe it in words.

A scan is a read-only reference: the modelling agent never sees the mesh, only
the text this module writes about it. So the job is that of a machinist
describing a part over the phone -- how big it is, which faces are flat and how
far apart, where the holes and pins are and what their diameters are -- and
being honest about what could not be measured.

How features are found
----------------------
1. The triangles are welded into an indexed mesh with face adjacency and the
   dihedral angle across every edge (`_index`).
2. The mesh is judged *clean* (a CAD export: flat faces are exactly flat) or
   *noisy* (a real scan); its noise level sets every tolerance (`_tolerances`).
3. Faces are grouped into regions that are each one surface:
   - clean: coplanar triangles form patches; two patches belong to the same
     curved strip when they meet at a shallow angle and are about equally wide
     across their shared edge, the way neighbouring facets of a tessellated
     cylinder are. A wide flat face next to a fillet fails that test and stays
     a face of its own (`_clean_regions`).
   - noisy: regions are bounded by sharp creases; a region that is not one
     surface gets its flat parts peeled off by normal and offset, and what
     remains is split into connected pieces (`_peel_planes`).
4. Every region is fitted: plane (PCA), cylinder (axis = direction the normals
   do not vary in, then a circle through the *vertices* -- on a faceted
   cylinder they lie on the true circle, the face centres do not -- refined by
   least squares), sphere. A fit is kept only when its residual is within
   tolerance; anything else that is large is reported as freeform, never
   guessed (`_fit_cylinder`, `_fit_sphere`).
5. Fragments of one cylinder (a bore crossing a slot, both walls of a cross
   hole) are merged, features get stable ids (P1.. planes, C1.. cylinders,
   S1.. spheres, F1.. freeform; largest first), and equal holes are grouped
   into patterns.

`section` cuts the mesh with a plane and describes the closed outlines,
`probe` says what is at a point, `deviation` compares a model with a scan,
`render` draws a shaded picture of any mesh from any direction and
`render_views` a sheet of four -- as PNG, without any imaging library.

Everything is deterministic: fixed seeds, stable ordering. Lengths are mm.
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np
from scipy import sparse
from scipy.optimize import least_squares
from scipy.sparse.csgraph import connected_components
from scipy.spatial import ConvexHull, cKDTree

__all__ = [
    "load", "features", "overview", "overview_text", "section", "section_text",
    "probe", "probe_text", "deviation", "deviation_text", "render", "render_views", "view_direction", "view_caption",
]

# --- tolerances ----------------------------------------------------------------
# Lengths are relative to the bounding-box diagonal unless a name says otherwise.

WELD_REL = 1e-6  # vertices closer than this are one vertex; must stay above 2**-21 (key packing)
EXACT_DEG = 0.05  # float32 coordinates leave about this much angle between truly coplanar triangles
COPLANAR_DEG = 0.5  # clean meshes: triangles of one flat face; far below any facet step of a cylinder
CREASE_DEG = 35.0  # a sharper edge separates two surfaces; a 12-sided "circle" (30 deg) is still round
CLEAN_SHARE = 0.15  # share of faces with an exactly coplanar neighbour above which a mesh is a CAD export
# (CAD exports measure 0.4 .. 1.0, scans 0.0; a scan closed with a few big flat triangles stays far below)
FACET_WIDTH_RATIO = 2.5  # neighbouring facets of one cylinder are about equally wide; a flat face is wider
FIT_REL = 3e-6  # clean meshes: fit tolerance relative to the largest coordinate, about 50 float32 roundings.
# Tessellators put vertices exactly on the surface, so anything looser lets pieces of swept or lofted
# surfaces pass as cylinders.
NOISE_FACTOR = 2.5  # fit tolerance in units of the scan noise (rms of a good fit is about 1)
RADIUS_REL = 0.002  # noisy meshes: extra fit tolerance per mm of radius (large scanned arcs are less true)
MAX_RADIUS_REL = 20.0  # a "cylinder" with a radius beyond this many diagonals is a flat face
MIN_ARC_DEG = 15.0  # shorter arcs do not determine a radius
MIN_FACET_ANGLES = 4  # three points always lie on a circle; four positions make the fit mean something
MIN_REGION_FACES = 4
NOISY_MIN_FACES = 30  # noisy meshes: smaller regions are speckle
NOISY_MIN_RADIUS = 8.0  # noisy meshes: a radius below this many times the noise cannot be measured
NOISY_PLANE_MAX_DEG = 60.0  # noisy meshes: beyond this a face is a wall crossing the plane, not part of it
MIN_INLIER_SHARE = 0.6  # a robust fit must still explain most of its region
MIN_RADIAL_AGREEMENT = 0.5  # mean cosine between normals and the radial direction: all point to or all away from the axis
PEEL_MIN_AREA = 0.004  # noisy meshes: a flat part of a mixed region must be this share of the total area
PEEL_MAX_PLANES = 24
CURVED_RMS_GAIN = 1.15  # a patch that a quadric fits this much better than a plane is curved, not flat
AXIS_SPLIT_DEG = 3.0  # clean meshes: ruling edges this parallel belong to cylinders of one direction
AXIS_SPLIT_MAX = 8  # directions tried in one fillet network
MAX_CURVED_REGIONS = 3000  # regions examined one by one; the rest of a huge mesh is left unexplained
MAX_PLANE_PATCHES = 400  # flat patches considered for merging into planes
PLANE_KEEP_AREA = 0.001  # planes smaller than this share of the area are not features
MERGE_ANGLE_DEG = 2.0  # planes / cylinder axes this parallel may be the same feature

# --- what the overview lists -----------------------------------------------------

PLANE_LIST_AREA = 0.02  # flat faces from 2 % of the total area
PLANE_LIST_MAX = 12
PAIR_LIST_MAX = 10
CYLINDER_LIST_MAX = 24
FREEFORM_LIST_AREA = 0.015
FREEFORM_LIST_MAX = 6
PATTERN_LIST_MAX = 6

# --- sections --------------------------------------------------------------------

SECTION_LOOPS_MAX = 14  # loops described one per line
CIRCLE_REL = 0.01  # a loop within 1 % of its radius from a circle is a circle
CIRCLE_MIN_CORNERS = 10  # a regular hexagon or octagon is a polygon, not a coarse circle
CIRCLE_MAX_CHORD = 0.75  # in radii; a 10-sided circle has chords of 0.62 radius, a straight side is longer
POLYGON_MAX_VERTICES = 12
COLLINEAR_DEG = 1.0  # section points on a facet (not on a ruling) turn less than this

# --- deviation ---------------------------------------------------------------------

DEVIATION_QUERIES = 20000  # points sampled on the measured surface
DEVIATION_SAMPLES_MAX = 250_000  # points spread over the surface measured against
DEVIATION_NEIGHBOURS = 16  # nearest samples whose triangles are measured exactly
DEVIATION_SEED = 20240501

# --- pictures ----------------------------------------------------------------------

VIEW_PIXELS = 450  # one of the four views of the sheet; the sheet is twice that
RENDER_MAX_TRIANGLES = 250_000  # beyond this the mesh is decimated by vertex clustering first
RENDER_CLUSTER_CELLS = 500  # ... on a grid this fine along the diagonal (about a pixel)
BACKGROUND = (250, 250, 250)
SURFACE = (150, 176, 208)
SECOND_TONE = (222, 160, 96)  # e.g. a scan drawn over the model
MARK_RADIUS = 5  # pixels

_AXES = {"x": np.array([1.0, 0.0, 0.0]), "y": np.array([0.0, 1.0, 0.0]), "z": np.array([0.0, 0.0, 1.0])}


# --- reading STL -----------------------------------------------------------------


def load(path: str) -> np.ndarray:
    """The triangles of a binary or ASCII STL as an (n, 3, 3) float64 array, without degenerate ones.

    The format is decided by the content: a binary file is one whose length
    matches its triangle count, whatever its header says ("solid ..." headers
    in binary files are common).
    """
    with open(path, "rb") as handle:
        data = handle.read()
    triangles = _parse_binary(data, strict=True)
    if triangles is None and b"vertex" in data:
        triangles = _parse_ascii(data)
    if triangles is None:
        triangles = _parse_binary(data, strict=False)
    if triangles is None:
        raise ValueError("not an STL file")
    return _without_degenerate(triangles)


def _parse_binary(data: bytes, strict: bool) -> np.ndarray | None:
    if len(data) < 84:
        return None
    declared = struct.unpack_from("<I", data, 80)[0]
    available = (len(data) - 84) // 50
    if strict and 84 + 50 * declared != len(data):
        return None
    count = min(declared, available)  # a truncated or padded file still gives what it holds
    if count == 0:
        return None
    record = np.dtype([("normal", "<f4", 3), ("corners", "<f4", (3, 3)), ("attribute", "<u2")])
    return np.frombuffer(data, dtype=record, count=count, offset=84)["corners"].astype(np.float64)


def _parse_ascii(data: bytes) -> np.ndarray | None:
    tokens = np.array(data.split())
    at = np.flatnonzero(tokens == b"vertex")
    at = at[at + 3 < len(tokens)]
    at = at[: len(at) // 3 * 3]
    if len(at) == 0:
        return None
    try:
        numbers = np.stack([tokens[at + 1], tokens[at + 2], tokens[at + 3]], axis=1).astype(np.float64)
    except ValueError:
        return None
    return numbers.reshape(-1, 3, 3)


def _without_degenerate(triangles: np.ndarray) -> np.ndarray:
    triangles = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
    triangles = triangles[np.isfinite(triangles).all(axis=(1, 2))]
    if len(triangles) == 0:
        return triangles
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    return triangles[np.linalg.norm(cross, axis=1) > 0.0]


# --- small helpers ---------------------------------------------------------------


def _unit(vectors: np.ndarray) -> np.ndarray:
    length = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.where(length > 0.0, length, 1.0)


def _basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two unit vectors spanning the plane across `normal`; the first is horizontal when it can be."""
    helper = _AXES["z"] if abs(normal[2]) < 0.9 else _AXES["x"]
    u = _unit(np.cross(helper, normal))
    return u, np.cross(normal, u)


def _canonical(direction: np.ndarray) -> np.ndarray:
    """The same line direction with its largest component positive, so output is stable."""
    direction = _unit(np.asarray(direction, dtype=np.float64))
    return -direction if direction[int(np.argmax(np.abs(direction)))] < 0.0 else direction


def _sum_by(labels: np.ndarray, values: np.ndarray, count: int) -> np.ndarray:
    """Per-label sums of `values` (n,) or (n, k)."""
    if values.ndim == 1:
        return np.bincount(labels, weights=values, minlength=count)
    return np.stack([np.bincount(labels, weights=values[:, k], minlength=count) for k in range(values.shape[1])], axis=1)


def _extent_by(labels: np.ndarray, values: np.ndarray, count: int) -> np.ndarray:
    """Per-label max - min of `values`."""
    high = np.full(count, -np.inf)
    low = np.full(count, np.inf)
    np.maximum.at(high, labels, values)
    np.minimum.at(low, labels, values)
    return np.where(np.isfinite(high), high - low, 0.0)


def _scatter_by(labels: np.ndarray, points: np.ndarray, count: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-label mean (count, 3) and covariance (count, 3, 3) of 3D points."""
    members = np.maximum(np.bincount(labels, minlength=count), 1)[:, None]
    mean = _sum_by(labels, points, count) / members
    moved = points - mean[labels]
    products = np.stack([moved[:, i] * moved[:, j] for i, j in ((0, 0), (0, 1), (0, 2), (1, 1), (1, 2), (2, 2))], axis=1)
    sums = _sum_by(labels, products, count) / members
    return mean, sums[:, [0, 1, 2, 1, 3, 4, 2, 4, 5]].reshape(-1, 3, 3)


def _components(count: int, pairs: np.ndarray) -> tuple[int, np.ndarray]:
    """Connected components of `count` nodes joined by `pairs` (k, 2)."""
    graph = sparse.coo_matrix((np.ones(len(pairs), dtype=np.int8), (pairs[:, 0], pairs[:, 1])), shape=(count, count))
    return connected_components(graph, directed=False)


def _groups(labels: np.ndarray, count: int) -> list[np.ndarray]:
    """The member indices of every label, as views into one sorted array."""
    order = np.argsort(labels, kind="stable")
    ends = np.cumsum(np.bincount(labels, minlength=count))
    return np.split(order, ends[:-1])


def _largest_first(area: float, position: np.ndarray) -> tuple:
    """Sort key: by area, equal areas by position, so ids do not depend on the order of triangles in the file."""
    return (-round(area, 3), *(round(float(c), 3) for c in position))


def _round(values: Iterable[float], digits: int = 4) -> list[float]:
    return [round(float(value), digits) for value in values]


# --- indexed mesh -----------------------------------------------------------------


@dataclass
class _Mesh:
    """Welded triangles with what every measurement needs."""

    verts: np.ndarray  # (m, 3)
    faces: np.ndarray  # (n, 3) vertex indices
    tri: np.ndarray  # (n, 3, 3) = verts[faces]
    normals: np.ndarray  # (n, 3) unit, by winding
    areas: np.ndarray  # (n,)
    centroids: np.ndarray  # (n, 3)
    edge_faces: np.ndarray  # (k, 2) the two faces of every edge shared by exactly two
    edge_verts: np.ndarray  # (k, 2)
    edge_angle: np.ndarray  # (k,) radians between the two face normals
    all_edges: np.ndarray  # (e, 2) every distinct edge
    open_edges: int  # edges with one face
    odd_edges: int  # edges with more than two faces
    flipped_edges: int  # edges whose two faces wind the same way round
    diag: float
    lo: np.ndarray
    hi: np.ndarray

    @property
    def watertight(self) -> bool:
        return self.open_edges == 0 and self.odd_edges == 0 and len(self.faces) > 0


def _index(triangles: np.ndarray) -> _Mesh:
    triangles = _without_degenerate(triangles)
    points = triangles.reshape(-1, 3)
    if len(points) == 0:
        empty = np.zeros((0, 3))
        none = np.zeros((0, 2), dtype=np.int64)
        return _Mesh(empty, np.zeros((0, 3), dtype=np.int64), np.zeros((0, 3, 3)), empty, np.zeros(0), empty,
                     none, none, np.zeros(0), none, 0, 0, 0, 0.0, np.zeros(3), np.zeros(3))
    lo, hi = points.min(axis=0), points.max(axis=0)
    diag = float(np.linalg.norm(hi - lo)) or 1.0

    # Weld: equal coordinates always fall into the same cell of a fine grid.
    cells = np.floor((points - lo) / (diag * WELD_REL) + 0.5).astype(np.int64)
    keys = cells[:, 0] | (cells[:, 1] << 21) | (cells[:, 2] << 42)
    _, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    faces = inverse.reshape(-1, 3)
    faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])]
    verts = points[first]
    tri = verts[faces]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    double_area = np.linalg.norm(cross, axis=1)
    keep = double_area > 0.0
    faces, tri, cross, double_area = faces[keep], tri[keep], cross[keep], double_area[keep]
    normals = cross / double_area[:, None]
    count = len(faces)

    # Edges: every face contributes three; equal keys are the same edge.
    start = np.concatenate([faces[:, 0], faces[:, 1], faces[:, 2]])
    end = np.concatenate([faces[:, 1], faces[:, 2], faces[:, 0]])
    low, high = np.minimum(start, end), np.maximum(start, end)
    edge_key = low * np.int64(len(verts)) + high
    order = np.argsort(edge_key)
    sorted_key = edge_key[order]
    run = np.flatnonzero(np.r_[True, sorted_key[1:] != sorted_key[:-1]])
    uses = np.diff(np.r_[run, len(sorted_key)])
    pair = run[uses == 2]
    one, two = order[pair], order[pair + 1]
    edge_faces = np.stack([one % count, two % count], axis=1)
    forward = start < end
    cosine = np.clip(np.einsum("ij,ij->i", normals[edge_faces[:, 0]], normals[edge_faces[:, 1]]), -1.0, 1.0)
    return _Mesh(
        verts=verts, faces=faces, tri=tri, normals=normals, areas=0.5 * double_area, centroids=tri.mean(axis=1),
        edge_faces=edge_faces, edge_verts=np.stack([low[one], high[one]], axis=1), edge_angle=np.arccos(cosine),
        all_edges=np.stack([low[order[run]], high[order[run]]], axis=1),
        open_edges=int(np.count_nonzero(uses == 1)), odd_edges=int(np.count_nonzero(uses > 2)),
        flipped_edges=int(np.count_nonzero(forward[one] == forward[two])),
        diag=diag, lo=lo, hi=hi,
    )


def _signed_volume(mesh: _Mesh) -> float:
    return float(np.einsum("ij,ij->i", mesh.tri[:, 0], np.cross(mesh.tri[:, 1], mesh.tri[:, 2])).sum() / 6.0)


def _centre_of_mass(mesh: _Mesh) -> np.ndarray:
    """Of the solid when the mesh is closed, otherwise of its surface."""
    if mesh.watertight:
        origin = 0.5 * (mesh.lo + mesh.hi)  # tetrahedra from the middle: better conditioned
        tri = mesh.tri - origin
        volumes = np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2]))
        total = volumes.sum()
        if abs(total) > 0.0:
            return origin + (volumes[:, None] * tri.sum(axis=1)).sum(axis=0) / (4.0 * total)
    return (mesh.areas[:, None] * mesh.centroids).sum(axis=0) / mesh.areas.sum()


# --- clean or noisy, and how noisy ---------------------------------------------------


@dataclass(frozen=True)
class _Tolerances:
    clean: bool  # a CAD export: flat faces are exactly flat, vertices lie on the true surfaces
    noise: float  # rms distance of vertices from the surface they sample (0 for clean meshes)
    fit: float  # largest rms residual of an accepted plane fit
    normal_var: float  # variance of face normals from noise, rad^2
    plane_angle: float  # noisy meshes: how far a face normal of a flat region may be off, rad
    creases: bool = True  # sharp edges can be told from noise, so surfaces can be separated at them
    min_faces: int = MIN_REGION_FACES  # fewer faces are not fitted

    def radial(self, radius: float) -> float:
        """Largest rms residual of an accepted cylinder or sphere of this radius."""
        return self.fit if self.clean else self.fit + RADIUS_REL * radius


def _tolerances(mesh: _Mesh) -> _Tolerances:
    count = len(mesh.faces)
    # Clean: most triangles have a neighbour in exactly the same plane (both halves
    # of a quad, the fan of a flat face). Scanner noise never leaves two coplanar.
    exact = mesh.edge_angle < math.radians(EXACT_DEG)
    has_coplanar = np.zeros(count, dtype=bool)
    has_coplanar[mesh.edge_faces[exact].ravel()] = True
    clean = bool(has_coplanar.mean() >= CLEAN_SHARE) if count else True
    floor = FIT_REL * max(mesh.diag, float(np.abs(mesh.lo).max()), float(np.abs(mesh.hi).max()))
    if clean:
        return _Tolerances(True, 0.0, floor, 0.0, math.radians(COPLANAR_DEG))

    # Noise: how far a vertex is from the mean of its neighbours, along its normal
    # (the umbrella operator). On flat and gently curved surfaces that is noise;
    # the median ignores edges and small radii.
    vertex_count = len(mesh.verts)
    a, b = mesh.all_edges[:, 0], mesh.all_edges[:, 1]
    degree = np.bincount(a, minlength=vertex_count) + np.bincount(b, minlength=vertex_count)
    neighbours = _sum_by(a, mesh.verts[b], vertex_count) + _sum_by(b, mesh.verts[a], vertex_count)
    weighted = mesh.normals * mesh.areas[:, None]
    vertex_normal = _unit(sum(_sum_by(mesh.faces[:, k], weighted, vertex_count) for k in range(3)))
    offset = mesh.verts - neighbours / np.maximum(degree, 1)[:, None]
    noise = 1.4826 * float(np.median(np.abs(np.einsum("ij,ij->i", offset, vertex_normal))))

    # The angle between two noisy normals has a median of about 1.67 sigma (true creases are too few to move it).
    sigma = float(np.median(mesh.edge_angle)) / 1.67 if len(mesh.edge_angle) else 0.0
    spread = 3.0 * sigma  # nearly every normal of a flat face is within this of the true one
    return _Tolerances(False, noise, max(floor, NOISE_FACTOR * noise), sigma * sigma,
                       float(np.clip(spread, math.radians(8.0), math.radians(NOISY_PLANE_MAX_DEG))),
                       creases=spread < math.radians(CREASE_DEG - 10.0), min_faces=NOISY_MIN_FACES)


# --- fitting surfaces ---------------------------------------------------------------


def _fit_plane(points: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Least-squares plane: unit normal, offset (normal . p), rms distance."""
    centre = points.mean(axis=0)
    moved = points - centre
    values, vectors = np.linalg.eigh(moved.T @ moved / len(points))
    normal = vectors[:, 0]
    return normal, float(normal @ centre), math.sqrt(max(float(values[0]), 0.0))


def _fit_circle(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Algebraic (Kasa) circle through 2D points: centre and radius. Exact for points on a circle."""
    design = np.stack([x, y, np.ones_like(x)], axis=1)
    solution, *_ = np.linalg.lstsq(design, x * x + y * y, rcond=None)
    cx, cy = 0.5 * solution[0], 0.5 * solution[1]
    return float(cx), float(cy), math.sqrt(max(float(solution[2] + cx * cx + cy * cy), 0.0))


def _axis_distance(points: np.ndarray, origin: np.ndarray, axis: np.ndarray) -> np.ndarray:
    moved = points - origin
    return np.linalg.norm(moved - np.outer(moved @ axis, axis), axis=1)


def _refine_cylinder(points: np.ndarray, axis: np.ndarray, origin: np.ndarray, radius: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Geometric least squares over axis tilt, centre and radius, from a good start."""
    u, v = _basis(axis)
    sample = points[:: max(1, len(points) // 4000)]

    def residual(p: np.ndarray) -> np.ndarray:
        direction = _unit(axis + p[3] * u + p[4] * v)
        return _axis_distance(sample, origin + p[0] * u + p[1] * v, direction) - p[2]

    limit = 0.2  # the start is within a few degrees; this keeps short arcs from flipping over
    start = np.array([0.0, 0.0, radius, 0.0, 0.0])
    bounds = ([-np.inf, -np.inf, 0.0, -limit, -limit], [np.inf, np.inf, np.inf, limit, limit])
    best = least_squares(residual, start, bounds=bounds, x_scale=[radius, radius, radius, 0.1, 0.1]).x
    direction = _unit(axis + best[3] * u + best[4] * v)
    origin = origin + best[0] * u + best[1] * v
    return direction, origin - (origin @ direction) * direction, float(best[2])


def _arc_degrees(angles: np.ndarray) -> float:
    """How much of the circle the faces cover; `angles` (k, 3) are those of each face's corners."""
    bins = 720
    middle = np.arctan2(np.sin(angles).mean(axis=1), np.cos(angles).mean(axis=1))
    spread = np.angle(np.exp(1j * (angles - middle[:, None])))  # corners relative to the face, wrapped
    scale = bins / (2 * math.pi)
    slack = 0.02  # of a bin: float32 corners sit a hair either side of a bin edge
    first = np.floor((middle + spread.min(axis=1)) * scale + slack).astype(np.int64)
    last = np.ceil((middle + spread.max(axis=1)) * scale - slack).astype(np.int64)
    span = np.clip(last - first, 1, bins)
    change = np.zeros(2 * bins + 1)  # the circle unrolled twice, so no interval has to wrap
    np.add.at(change, first % bins, 1)
    np.add.at(change, first % bins + span, -1)
    covered = np.cumsum(change[:-1])
    return 360.0 * float(np.count_nonzero((covered[:bins] + covered[bins:]) > 0)) / bins


def _fit_cylinder(mesh: _Mesh, faces: np.ndarray, tol: _Tolerances, robust: bool) -> dict | None:
    """A cylinder through the faces, or None when they are not one.

    With `robust`, vertices that do not fit (the blend at a rim, a neighbouring
    surface) are left out and only the faces that fit are claimed.
    """
    normals, weights = mesh.normals[faces], mesh.areas[faces]
    spread = (normals * weights[:, None]).T @ normals / weights.sum()
    values, vectors = np.linalg.eigh(spread)
    # Normals of a cylinder lie in one plane (no component along the axis) and fan out in it.
    if values[0] > 0.004 + 3.0 * tol.normal_var or values[1] < 0.01 + tol.normal_var:
        return None
    axis = vectors[:, 0]
    u, v = _basis(axis)
    vertex_ids = np.unique(mesh.faces[faces])
    points = mesh.verts[vertex_ids]
    used = np.ones(len(points), dtype=bool)
    origin, radius, error = np.zeros(3), 0.0, np.zeros(len(points))
    for _ in range(3 if robust else 1):
        chosen = points[used]
        if len(chosen) < 6:
            return None
        cx, cy, radius = _fit_circle(chosen @ u, chosen @ v)
        origin = cx * u + cy * v
        if not 0.0 < radius < MAX_RADIUS_REL * mesh.diag:
            return None
        start_rms = float(np.sqrt(np.mean((_axis_distance(chosen, origin, axis) - radius) ** 2)))
        if start_rms > 0.1 * tol.radial(radius):  # exact already on clean facets; noise needs the refinement
            axis, origin, radius = _refine_cylinder(chosen, axis, origin, radius)
            u, v = _basis(axis)
        error = np.abs(_axis_distance(points, origin, axis) - radius)
        if not robust:
            break
        allowed = max(tol.radial(radius), 2.5 * 1.4826 * float(np.median(error[used])))
        keep = error <= allowed
        if keep.mean() < MIN_INLIER_SHARE:
            return None
        if (keep == used).all():
            break
        used = keep
    if float(np.sqrt(np.mean(error[used] ** 2))) > tol.radial(radius):
        return None
    if robust and not used.all():
        fits = np.zeros(len(mesh.verts), dtype=bool)
        fits[vertex_ids[used]] = True
        faces = faces[fits[mesh.faces[faces]].all(axis=1)]
        if len(faces) < MIN_REGION_FACES:
            return None
    return _describe_cylinder(mesh, faces, _canonical(axis), origin, radius, tol)


def _describe_cylinder(mesh: _Mesh, faces: np.ndarray, axis: np.ndarray, origin: np.ndarray, radius: float,
                       tol: _Tolerances) -> dict | None:
    """Everything reported about a fitted cylinder, measured on its faces; None when they contradict it."""
    weights = mesh.areas[faces]
    moved = mesh.centroids[faces] - origin
    outward = _unit(moved - np.outer(moved @ axis, axis))
    agreement = float((weights * np.einsum("ij,ij->i", mesh.normals[faces], outward)).sum() / weights.sum())
    if abs(agreement) < MIN_RADIAL_AGREEMENT:
        return None
    u, v = _basis(axis)
    corners = mesh.tri[faces] - origin
    angles = np.arctan2(corners @ v, corners @ u)
    arc = _arc_degrees(angles)
    positions = np.unique(np.round(np.degrees(angles) * 2.0).astype(np.int64) % 720)
    if arc < MIN_ARC_DEG or len(positions) < MIN_FACET_ANGLES:
        return None
    points = mesh.verts[np.unique(mesh.faces[faces])]
    along = (points - origin) @ axis
    error = np.abs(_axis_distance(points, origin, axis) - radius)
    return {
        "kind": "cylinder", "shape": "boss" if agreement > 0 else "bore", "full": arc >= 359.0, "arc": round(arc, 1),
        "diameter": 2.0 * radius, "radius": radius, "axis": axis,
        "start": origin + along.min() * axis, "end": origin + along.max() * axis,
        "length": float(along.max() - along.min()), "rms": float(np.sqrt(np.mean(error ** 2))), "max_dev": float(error.max()),
        "area": float(weights.sum()), "facing": _unit((weights[:, None] * mesh.normals[faces]).sum(axis=0)),
        "faces": faces,
    }


def _fit_sphere(mesh: _Mesh, faces: np.ndarray, tol: _Tolerances) -> dict | None:
    normals, weights = mesh.normals[faces], mesh.areas[faces]
    spread = (normals * weights[:, None]).T @ normals / weights.sum()
    if np.linalg.eigvalsh(spread)[0] < 0.01 + 3.0 * tol.normal_var:  # normals must fan out in every direction
        return None
    points = mesh.verts[np.unique(mesh.faces[faces])]
    design = np.concatenate([2.0 * points, np.ones((len(points), 1))], axis=1)
    solution, *_ = np.linalg.lstsq(design, (points * points).sum(axis=1), rcond=None)
    centre = solution[:3]
    radius = math.sqrt(max(float(solution[3] + centre @ centre), 0.0))
    if not 0.0 < radius < MAX_RADIUS_REL * mesh.diag:
        return None
    error = np.abs(np.linalg.norm(points - centre, axis=1) - radius)
    rms = float(np.sqrt(np.mean(error ** 2)))
    outward = _unit(mesh.centroids[faces] - centre)
    agreement = float((weights * np.einsum("ij,ij->i", normals, outward)).sum() / weights.sum())
    if rms > tol.radial(radius) or abs(agreement) < MIN_RADIAL_AGREEMENT:
        return None
    return {"kind": "sphere", "shape": "ball" if agreement > 0 else "socket", "diameter": 2.0 * radius, "radius": radius,
            "centre": centre, "rms": rms, "max_dev": float(error.max()), "area": float(weights.sum()), "faces": faces}


def _fit_surface(mesh: _Mesh, faces: np.ndarray, tol: _Tolerances, robust: bool) -> dict | None:
    if len(faces) < tol.min_faces:
        return None
    fitted = _fit_cylinder(mesh, faces, tol, robust) or _fit_sphere(mesh, faces, tol)
    if fitted is not None and (fitted["radius"] < NOISY_MIN_RADIUS * tol.noise or len(fitted["faces"]) < tol.min_faces):
        return None
    return fitted


# --- grouping faces into surfaces ---------------------------------------------------


def _clean_regions(mesh: _Mesh) -> tuple[int, np.ndarray]:
    """Clean meshes: flat faces and curved strips, by the facet-width test (see the module docstring)."""
    count = len(mesh.faces)
    coplanar = mesh.edge_angle < math.radians(COPLANAR_DEG)
    patch_count, patch = _components(count, mesh.edge_faces[coplanar])

    # Every patch gets a frame: its two in-plane principal directions and its size along them.
    owner = np.repeat(patch, 3)
    points = mesh.tri.reshape(-1, 3)
    centre, scatter = _scatter_by(owner, points, patch_count)
    _, vectors = np.linalg.eigh(scatter)
    normal, minor, major = vectors[:, :, 0], vectors[:, :, 1], vectors[:, :, 2]
    moved = points - centre[owner]
    size_major = _extent_by(owner, np.einsum("ij,ij->i", moved, major[owner]), patch_count)
    size_minor = _extent_by(owner, np.einsum("ij,ij->i", moved, minor[owner]), patch_count)

    def width(patches: np.ndarray, edge_direction: np.ndarray) -> np.ndarray:
        across = np.cross(normal[patches], edge_direction)
        return (np.abs(np.einsum("ij,ij->i", across, major[patches])) * size_major[patches]
                + np.abs(np.einsum("ij,ij->i", across, minor[patches])) * size_minor[patches])

    bend = ~coplanar & (mesh.edge_angle < math.radians(CREASE_DEG))
    pairs = mesh.edge_faces[bend]
    direction = _unit(mesh.verts[mesh.edge_verts[bend, 1]] - mesh.verts[mesh.edge_verts[bend, 0]])
    one, two = width(patch[pairs[:, 0]], direction), width(patch[pairs[:, 1]], direction)
    ruling = np.maximum(one, two) <= FACET_WIDTH_RATIO * np.minimum(one, two)
    return _components(count, np.concatenate([mesh.edge_faces[coplanar], pairs[ruling]]))


def _smooth_edges(mesh: _Mesh, tol: _Tolerances) -> np.ndarray:
    """The edges two faces of one surface may share: all but sharp creases, where creases can be trusted."""
    if not tol.creases:
        return np.ones(len(mesh.edge_angle), dtype=bool)
    return mesh.edge_angle < math.radians(CREASE_DEG)


def _noisy_regions(mesh: _Mesh, tol: _Tolerances) -> tuple[int, np.ndarray]:
    """Noisy meshes: surfaces end at sharp creases -- or, when noise hides the creases, one region for all."""
    return _components(len(mesh.faces), mesh.edge_faces[_smooth_edges(mesh, tol)])


def _connected_parts(mesh: _Mesh, faces: np.ndarray, edges: np.ndarray) -> list[np.ndarray]:
    """`faces` split into groups connected through `edges` (a mask over the mesh's edges), largest area first."""
    local = np.full(len(mesh.faces), -1, dtype=np.int64)
    local[faces] = np.arange(len(faces))
    pairs = local[mesh.edge_faces[edges]]
    pairs = pairs[(pairs >= 0).all(axis=1)]
    count, labels = _components(len(faces), pairs)
    parts = [faces[members] for members in _groups(labels, count)]
    return sorted(parts, key=lambda part: -float(mesh.areas[part].sum()))


def _is_flat(points: np.ndarray, normal: np.ndarray) -> bool:
    """Whether points near a plane are a flat face rather than a strip of a curved one.

    A band of a large cylinder is "flat within tolerance" too; a quadric fits it
    clearly better than a plane, which it does not for a noisy flat face.
    """
    points = points[:: max(1, len(points) // 5000)]
    if len(points) < 12:
        return True
    u, v = _basis(normal)
    moved = points - points.mean(axis=0)
    x, y, height = moved @ u, moved @ v, moved @ normal
    linear = np.stack([np.ones_like(x), x, y], axis=1)
    quadric = np.concatenate([linear, np.stack([x * x, x * y, y * y], axis=1)], axis=1)

    def rms(design: np.ndarray) -> float:
        solution, *_ = np.linalg.lstsq(design, height, rcond=None)
        return float(np.sqrt(np.mean((height - design @ solution) ** 2)))

    return rms(linear) <= CURVED_RMS_GAIN * rms(quadric) + 1e-12


def _peel_planes(mesh: _Mesh, faces: np.ndarray, tol: _Tolerances, min_area: float) -> list[np.ndarray]:
    """Noisy meshes: the flat parts of a region that is several surfaces, largest direction first.

    Repeatedly: take the most common normal direction, then the most common
    offset along it, fit a plane to those faces and claim every face that lies
    on it. Candidates that turn out small or curved are not tried again.
    Claimed faces leave the working set, so later rounds are cheaper.
    """
    normals, areas, centres, corners = mesh.normals[faces], mesh.areas[faces], mesh.centroids[faces], mesh.tri[faces]
    cells = np.floor((normals + 1.0) * 4.0).astype(np.int64).clip(0, 7)
    cell = cells[:, 0] * 64 + cells[:, 1] * 8 + cells[:, 2]
    seeds = np.ones(len(faces), dtype=bool)  # may still start a candidate
    everything = np.ones(len(mesh.edge_angle), dtype=bool)
    cosine = math.cos(tol.plane_angle)
    band = 2.0 * tol.fit
    found: list[np.ndarray] = []
    for _ in range(PEEL_MAX_PLANES * 3):
        if len(found) >= PEEL_MAX_PLANES or not seeds.any():
            break
        weight = np.bincount(cell[seeds], weights=areas[seeds], minlength=512)
        best = int(np.argmax(weight))
        if weight[best] < 0.1 * min_area:
            break
        start = seeds & (cell == best)
        direction = _unit((areas[start, None] * normals[start]).sum(axis=0))
        for _ in range(2):
            aligned = normals @ direction >= cosine
            direction = _unit((areas[aligned, None] * normals[aligned]).sum(axis=0))
        aligned = np.flatnonzero(normals @ direction >= cosine)
        if len(aligned) < 3:
            seeds[start] = False
            continue
        offsets = centres[aligned] @ direction
        slot = np.floor((offsets - offsets.min()) / band).astype(np.int64)
        peak = int(np.argmax(np.bincount(slot, weights=areas[aligned])))
        near = aligned[np.abs(slot - peak) <= 1]
        normal, offset, _ = _fit_plane(corners[near].reshape(-1, 3))
        normal, offset = (normal, offset) if normal @ direction > 0 else (-normal, -offset)
        # On the plane: facing its way with the centre on it -- or, however noise tilted a small face,
        # with all three corners on it (a wall crossing the plane has corners off it).
        close = np.flatnonzero(np.abs(centres @ normal - offset) <= 1.3 * tol.fit)
        centred = np.abs(centres[close] @ normal - offset) <= tol.fit
        all_corners = np.abs(corners[close] @ normal - offset).max(axis=1) <= 1.3 * tol.fit
        on_plane = close[((normals[close] @ normal >= cosine) & centred) | all_corners]
        # A plane tangent to two holes collects a strip of each; real flat faces are connected patches.
        patches = _connected_parts(mesh, faces[on_plane], everything) if areas[on_plane].sum() >= min_area else []
        patches = [patch for patch in patches if mesh.areas[patch].sum() >= 0.5 * min_area]
        claimed = np.concatenate(patches) if patches else np.zeros(0, dtype=np.int64)
        if mesh.areas[claimed].sum() >= min_area and _is_flat(mesh.tri[claimed].reshape(-1, 3), normal):
            found.append(claimed)
            keep = ~np.isin(faces, claimed)
            faces, normals, areas, centres, corners = faces[keep], normals[keep], areas[keep], centres[keep], corners[keep]
            cell, seeds = cell[keep], seeds[keep]
        else:
            seeds[near] = False
            seeds[start] = False
    return found


def _split_by_axis(mesh: _Mesh, faces: np.ndarray) -> list[np.ndarray]:
    """Clean meshes: a network of fillets split into strips whose ruling edges are parallel.

    Edges between facets of a cylinder run along its axis. The doubly curved
    corner patches that join fillets have no common edge direction and fall out.
    Strips are also cut where the surface turns from convex to concave: a round
    end running into a concave neck is two cylinders.
    """
    inside = np.zeros(len(mesh.faces), dtype=bool)
    inside[faces] = True
    within = inside[mesh.edge_faces].all(axis=1)
    coplanar = within & (mesh.edge_angle < math.radians(COPLANAR_DEG))
    bent = np.flatnonzero(within & ~coplanar & (mesh.edge_angle < math.radians(CREASE_DEG)))
    vector = mesh.verts[mesh.edge_verts[bent, 1]] - mesh.verts[mesh.edge_verts[bent, 0]]
    length = np.linalg.norm(vector, axis=1)
    direction = vector / length[:, None]
    flip = direction[np.arange(len(bent)), np.argmax(np.abs(direction), axis=1)] < 0
    direction[flip] *= -1.0
    one, two = mesh.edge_faces[bent, 0], mesh.edge_faces[bent, 1]
    convex = np.einsum("ij,ij->i", mesh.centroids[two] - mesh.centroids[one], mesh.normals[one]) < 0
    cells = np.floor((direction + 1.0) * 10.0).astype(np.int64).clip(0, 19)
    cell = cells[:, 0] * 400 + cells[:, 1] * 20 + cells[:, 2]
    free = np.ones(len(bent), dtype=bool)
    parts: list[np.ndarray] = []
    for _ in range(AXIS_SPLIT_MAX):
        if np.count_nonzero(free) < MIN_REGION_FACES:
            break
        weight = np.bincount(cell[free], weights=length[free], minlength=8000)
        seed = free & (cell == int(np.argmax(weight)))
        axis = _unit((length[seed, None] * direction[seed]).sum(axis=0))
        parallel = free & (np.abs(direction @ axis) >= math.cos(math.radians(AXIS_SPLIT_DEG)))
        free &= ~parallel
        for rulings in (bent[parallel & convex], bent[parallel & ~convex]):
            edges = coplanar.copy()
            edges[rulings] = True
            on_strip = np.zeros(len(mesh.faces), dtype=bool)
            on_strip[mesh.edge_faces[rulings].ravel()] = True
            strip = faces[on_strip[faces]]
            parts += [part for part in _connected_parts(mesh, strip, edges) if len(part) >= MIN_REGION_FACES]
    return sorted(parts, key=lambda part: -float(mesh.areas[part].sum()))


def _unclaimed(mesh: _Mesh, faces: np.ndarray, found: Sequence[dict]) -> list[np.ndarray]:
    """The faces no feature in `found` took, as a list of at most one array."""
    taken = np.zeros(len(mesh.faces), dtype=bool)
    for feature in found:
        taken[feature["faces"]] = True
    rest = faces[~taken[faces]]
    return [rest] if len(rest) else []


def _explain(mesh: _Mesh, faces: np.ndarray, tol: _Tolerances, total_area: float) -> tuple[list[dict], list[np.ndarray]]:
    """Features of one region that is not flat, and the faces left unexplained."""
    # Strictly first; then leaving out what does not fit (a blend at the rim, a neighbour the region ran into).
    whole = _fit_surface(mesh, faces, tol, robust=not tol.clean) or (_fit_surface(mesh, faces, tol, robust=True) if tol.clean else None)
    if whole is not None:
        return [whole], _unclaimed(mesh, faces, [whole])
    found: list[dict] = []
    if tol.clean:
        parts = _split_by_axis(mesh, faces)
    else:
        found = [{"kind": "plane", "faces": flat} for flat in _peel_planes(mesh, faces, tol, PEEL_MIN_AREA * total_area)]
        rest = _unclaimed(mesh, faces, found)
        parts = _connected_parts(mesh, rest[0], _smooth_edges(mesh, tol)) if rest else []
    taken = np.zeros(len(mesh.faces), dtype=bool)
    for part in parts[:MAX_CURVED_REGIONS]:
        part = part[~taken[part]]
        fitted = _fit_surface(mesh, part, tol, robust=True)
        if fitted is not None:
            found.append(fitted)
            taken[fitted["faces"]] = True
    return found, _unclaimed(mesh, faces, found)


# --- features ------------------------------------------------------------------------


@dataclass
class _Analysis:
    mesh: _Mesh
    tol: _Tolerances
    features: list[dict]  # planes, cylinders, spheres, freeform; each with "id" and "faces"
    face_feature: np.ndarray  # index into `features` per face, -1 where nothing was recognised
    warnings: list[str] = field(default_factory=list)


def _analyse(mesh: _Mesh) -> _Analysis:
    tol = _tolerances(mesh)
    count = len(mesh.faces)
    if count == 0:
        return _Analysis(mesh, tol, [], np.zeros(0, dtype=np.int64), ["the mesh has no triangles"])
    total_area = float(mesh.areas.sum())
    region_count, region = _clean_regions(mesh) if tol.clean else _noisy_regions(mesh, tol)

    # Flat regions, all at once: rms distance of a region's corners from its best plane.
    _, scatter = _scatter_by(np.repeat(region, 3), mesh.tri.reshape(-1, 3), region_count)
    flat = np.sqrt(np.maximum(np.linalg.eigvalsh(scatter)[:, 0], 0.0)) <= tol.fit
    area = np.bincount(region, weights=mesh.areas, minlength=region_count)
    size = np.bincount(region, minlength=region_count)
    members = _groups(region, region_count)
    by_area = np.argsort(-area, kind="stable")

    found: list[dict] = []
    leftover: list[np.ndarray] = []
    warnings: list[str] = []
    flat_ids = [r for r in by_area if flat[r] and area[r] >= 0.25 * PLANE_KEEP_AREA * total_area][:MAX_PLANE_PATCHES]
    found += [{"kind": "plane", "faces": members[r]} for r in flat_ids]
    curved_ids = [r for r in by_area if not flat[r] and size[r] >= tol.min_faces]
    if len(curved_ids) > MAX_CURVED_REGIONS:
        warnings.append(f"{len(curved_ids) - MAX_CURVED_REGIONS} small curved regions were not examined")
    for r in curved_ids[:MAX_CURVED_REGIONS]:
        features_here, rest = _explain(mesh, members[r], tol, total_area)
        found += features_here
        leftover += rest

    # A facet cut off from its strip (or, on a mildly noisy mesh, wide enough to pass for a flat patch) is
    # flat on its own, so flat patches are offered to the cylinders too: only a facet has all its corners
    # on the cylinder and its normal along the radius.
    patches = [f["faces"] for f in found if f["kind"] == "plane"]
    on_round = np.zeros(count, dtype=bool)
    for feature in found:
        on_round[feature["faces"]] = feature["kind"] != "plane"
    pool = np.flatnonzero(~on_round)
    cylinders = _merge_cylinders(mesh, [f for f in found if f["kind"] == "cylinder"], pool, tol)
    on_cylinder = np.zeros(count, dtype=bool)
    for cylinder in cylinders:
        on_cylinder[cylinder["faces"]] = True
    patches = [faces[~on_cylinder[faces]] for faces in patches]
    planes = _merge_planes(mesh, [faces for faces in patches if len(faces)], tol, total_area)
    spheres = sorted((f for f in found if f["kind"] == "sphere"), key=lambda f: _largest_first(f["area"], f["centre"]))
    claimed = np.zeros(count, dtype=bool)
    for feature in planes + cylinders + spheres:
        claimed[feature["faces"]] = True
    freeform = _freeform(mesh, leftover, claimed, total_area)

    features_all = planes + cylinders + spheres + freeform
    face_feature = np.full(count, -1, dtype=np.int64)
    for prefix, group in (("P", planes), ("C", cylinders), ("S", spheres), ("F", freeform)):
        for number, feature in enumerate(group, start=1):
            feature["id"] = f"{prefix}{number}"
    for index, feature in enumerate(features_all):
        face_feature[feature["faces"]] = index
    for cylinder in cylinders:
        cylinder["ends"] = _bore_ends(mesh, cylinder, tol)
    return _Analysis(mesh, tol, features_all, face_feature, warnings)


def _oriented_plane(mesh: _Mesh, faces: np.ndarray) -> tuple[np.ndarray, float, float]:
    """The plane through the faces with its normal pointing the way theirs do."""
    normal, offset, rms = _fit_plane(mesh.tri[faces].reshape(-1, 3))
    if normal @ (mesh.areas[faces, None] * mesh.normals[faces]).sum(axis=0) < 0:
        return -normal, -offset, rms
    return normal, offset, rms


def _tight_rectangle(points: np.ndarray) -> list[float]:
    """Sides of the smallest rectangle around 2D points, longer first (one side lies on the convex hull)."""
    try:
        hull = points[ConvexHull(points).vertices]
    except Exception:  # noqa: BLE001 -- collinear or too few points: fall back to the axis-aligned box
        return sorted((float(side) for side in np.ptp(points, axis=0)), reverse=True)
    edges = _unit(np.roll(hull, -1, axis=0) - hull)
    along = np.ptp(hull @ edges.T, axis=0)
    across = np.ptp(hull @ np.stack([-edges[:, 1], edges[:, 0]], axis=0), axis=0)
    best = int(np.argmin(along * across))
    return sorted([float(along[best]), float(across[best])], reverse=True)


def _merge_planes(mesh: _Mesh, patches: list[np.ndarray], tol: _Tolerances, total_area: float) -> list[dict]:
    """Flat patches in the same plane become one plane feature; largest area first."""
    patches = sorted(patches, key=lambda faces: -float(mesh.areas[faces].sum()))
    merged: list[np.ndarray] = []
    normals = np.zeros((0, 3))
    offsets = np.zeros(0)
    cosine = math.cos(math.radians(MERGE_ANGLE_DEG) + (0.0 if tol.clean else tol.plane_angle / 6.0))
    for faces in patches:
        normal, offset, _ = _oriented_plane(mesh, faces)
        same = np.flatnonzero((normals @ normal >= cosine) & (np.abs(offsets - offset) <= 2.0 * tol.fit))
        if len(same):
            merged[int(same[0])] = np.concatenate([merged[int(same[0])], faces])
        else:
            merged.append(faces)
            normals = np.vstack([normals, normal])
            offsets = np.append(offsets, offset)
    planes = []
    for faces in merged:
        weights = mesh.areas[faces]
        if weights.sum() < PLANE_KEEP_AREA * total_area:
            continue
        corners = mesh.tri[faces].reshape(-1, 3)
        normal, offset, rms = _oriented_plane(mesh, faces)
        u, v = _basis(normal)
        planes.append({
            "kind": "plane", "normal": normal, "offset": offset, "area": float(weights.sum()), "rms": rms,
            "centre": (weights[:, None] * mesh.centroids[faces]).sum(axis=0) / weights.sum(),
            "bbox": [corners.min(axis=0), corners.max(axis=0)], "size": _tight_rectangle(np.stack([corners @ u, corners @ v], axis=1)),
            "faces": faces,
        })
    return sorted(planes, key=lambda plane: _largest_first(plane["area"], plane["centre"]))


def _same_cylinder(a: dict, b: dict, tol: _Tolerances, diag: float) -> bool:
    slack = 2.0 * tol.radial(max(a["radius"], b["radius"])) + FIT_REL * diag
    if a["shape"] != b["shape"] or abs(a["radius"] - b["radius"]) > slack:
        return False
    angle = math.radians(MERGE_ANGLE_DEG) + (0.0 if tol.clean else 2.0 * math.sqrt(tol.normal_var))
    if abs(float(a["axis"] @ b["axis"])) < math.cos(angle):
        return False
    middle = 0.5 * (b["start"] + b["end"]) - a["start"]
    off_axis = float(np.linalg.norm(middle - (middle @ a["axis"]) * a["axis"]))
    return off_axis <= slack + abs(float(middle @ a["axis"])) * math.sin(angle)


def _merge_cylinders(mesh: _Mesh, cylinders: list[dict], pool: np.ndarray, tol: _Tolerances) -> list[dict]:
    """Fragments of one cylinder joined, stray facets from `pool` that lie on a cylinder claimed; largest area first."""
    merged: list[dict] = []
    for cylinder in sorted(cylinders, key=lambda c: -c["area"]):
        for index, other in enumerate(merged):
            if _same_cylinder(other, cylinder, tol, mesh.diag):
                joined = _fit_cylinder(mesh, np.concatenate([other["faces"], cylinder["faces"]]), tol, robust=False)
                if joined is not None and joined["shape"] == other["shape"]:
                    joined["segments"] = other.get("segments", 1) + 1
                    merged[index] = joined
                    break
        else:
            merged.append(cylinder)

    # Facets cut off from their strip (slivers beside a crossing hole) still lie on the cylinder.
    for index, cylinder in enumerate(merged):
        if len(pool) == 0:
            break
        axis, origin, radius = cylinder["axis"], cylinder["start"], cylinder["radius"]
        moved = mesh.centroids[pool] - origin
        along = moved @ axis
        near = np.flatnonzero((along > -tol.fit) & (along < cylinder["length"] + tol.fit)
                              & ((moved * moved).sum(axis=1) - along * along < (1.01 * radius + tol.fit) ** 2))
        candidates = pool[near]
        error = np.abs(_axis_distance(mesh.tri[candidates].reshape(-1, 3), origin, axis) - radius).reshape(-1, 3).max(axis=1)
        outward = _unit(moved[near] - np.outer(along[near], axis)) * (1.0 if cylinder["shape"] == "boss" else -1.0)
        fits = (error <= tol.radial(radius)) & (np.einsum("ij,ij->i", mesh.normals[candidates], outward) > 0.9)
        if fits.any():
            grown = _describe_cylinder(mesh, np.union1d(cylinder["faces"], candidates[fits]), axis, origin, radius, tol)
            if grown is not None:
                grown["segments"] = cylinder.get("segments", 1)
                merged[index] = grown
                pool = np.delete(pool, near[fits])
    return sorted(merged, key=lambda c: _largest_first(c["area"], c["start"]))


def _freeform(mesh: _Mesh, leftover: list[np.ndarray], claimed: np.ndarray, total_area: float) -> list[dict]:
    """Large curved regions nothing was fitted to."""
    regions = []
    for faces in leftover:
        faces = faces[~claimed[faces]]
        area = float(mesh.areas[faces].sum()) if len(faces) else 0.0
        if area < FREEFORM_LIST_AREA * total_area:
            continue
        corners = mesh.tri[faces].reshape(-1, 3)
        regions.append({"kind": "freeform", "area": area, "bbox": [corners.min(axis=0), corners.max(axis=0)], "faces": faces})
    return sorted(regions, key=lambda r: -r["area"])


def _bore_ends(mesh: _Mesh, cylinder: dict, tol: _Tolerances) -> str | None:
    """"through" or "blind" for a full bore: is there a floor across either end?"""
    if cylinder["shape"] != "bore" or not cylinder["full"]:
        return None
    axis, radius = cylinder["axis"], cylinder["radius"]
    reach = tol.fit + 0.1 * radius
    closed = 0
    for end in (cylinder["start"], cylinder["end"]):
        moved = mesh.centroids - end
        along = moved @ axis
        near = np.flatnonzero(np.abs(along) <= reach)
        radial = np.linalg.norm(moved[near] - np.outer(along[near], axis), axis=1)
        floor = (radial < 0.6 * radius) & (np.abs(mesh.normals[near] @ axis) > 0.9)
        closed += bool(floor.any())
    return "through" if closed == 0 else "blind"


def _public(feature: dict) -> dict:
    """A feature as JSON: no face lists, plain rounded numbers."""
    out: dict = {}
    for key, value in feature.items():
        if key == "faces":
            continue
        if isinstance(value, np.ndarray):
            out[key] = _round(value)
        elif isinstance(value, list) and value and isinstance(value[0], np.ndarray):
            out[key] = [_round(item) for item in value]
        elif isinstance(value, list) and value and isinstance(value[0], float):
            out[key] = _round(value)
        elif isinstance(value, float):
            out[key] = round(value, 4)
        else:
            out[key] = value
    return out


def features(triangles: np.ndarray) -> list[dict]:
    """Planes, cylinders, spheres and unexplained curved regions of a mesh, each with a stable id."""
    return [_public(feature) for feature in _analyse(_index(triangles)).features]


# --- patterns and pairs ----------------------------------------------------------------


def _plane_pairs(planes: list[dict], tol: _Tolerances) -> list[dict]:
    """Parallel flat faces and how far apart they are: thicknesses, gaps and steps."""
    cosine = math.cos(math.radians(MERGE_ANGLE_DEG) + (0.0 if tol.clean else tol.plane_angle / 6.0))
    pairs = []
    for i, a in enumerate(planes):
        for b in planes[i + 1:]:
            dot = float(a["normal"] @ b["normal"])
            if abs(dot) < cosine:
                continue
            if dot < 0:  # facing away from each other: material between them; facing each other: a gap
                between = a["offset"] + b["offset"]
                kind, distance = ("thickness" if between > 0 else "gap"), abs(between)
            else:
                kind, distance = "step", abs(a["offset"] - b["offset"])
            if kind != "step" and not _overlap(a, b):
                continue  # a thickness or a gap is between faces that lie over each other
            pairs.append({"a": a["id"], "b": b["id"], "kind": kind, "distance": round(distance, 4),
                          "area": min(a["area"], b["area"])})
    pairs.sort(key=lambda pair: (pair["kind"] == "step", -pair["area"]))  # thicknesses and gaps first
    return [{k: v for k, v in pair.items() if k != "area"} for pair in pairs[:PAIR_LIST_MAX]]


def _overlap(a: dict, b: dict) -> bool:
    """Whether two parallel planes' bounding boxes overlap when seen along their normal."""
    u, v = _basis(a["normal"])

    def box(plane: dict) -> tuple[np.ndarray, np.ndarray]:
        low, high = plane["bbox"]
        corners = np.array([[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])])
        flat = np.stack([corners @ u, corners @ v], axis=1)
        return flat.min(axis=0), flat.max(axis=0)

    (a_low, a_high), (b_low, b_high) = box(a), box(b)
    return bool((a_low < b_high).all() and (b_low < a_high).all())


def _layout(points: np.ndarray, slack: float) -> dict:
    """How equal holes are arranged, seen along their axis: spacing, rectangle, bolt circle or row."""
    count = len(points)
    if count == 2:
        return {"layout": "pair", "spacing": float(np.linalg.norm(points[1] - points[0]))}
    moved = points - points.mean(axis=0)
    values, vectors = np.linalg.eigh(moved.T @ moved)
    if values[0] <= slack * slack * count:  # all on one line
        steps = np.diff(np.sort(moved @ vectors[:, 1]))
        return {"layout": "row", "pitch": float(steps.mean())} if np.ptp(steps) <= 2.0 * slack else {"layout": "row"}
    cx, cy, radius = _fit_circle(moved[:, 0], moved[:, 1])
    if np.abs(np.hypot(moved[:, 0] - cx, moved[:, 1] - cy) - radius).max() <= 2.0 * slack:
        angles = np.sort(np.arctan2(moved[:, 1] - cy, moved[:, 0] - cx))
        gaps = np.diff(np.r_[angles, angles[0] + 2 * math.pi])
        if np.ptp(gaps) * radius <= 4.0 * slack:
            return {"layout": "bolt circle", "circle_diameter": 2.0 * radius, "equally_spaced": True}
        if count == 4:  # four points on a circle, not equally spaced: the corners of a rectangle
            ring = moved[np.argsort(np.arctan2(moved[:, 1], moved[:, 0]))]
            sides = np.linalg.norm(np.roll(ring, -1, axis=0) - ring, axis=1)
            return {"layout": "rectangle", "sides": sorted([float(sides[0]), float(sides[1])], reverse=True)}
        return {"layout": "bolt circle", "circle_diameter": 2.0 * radius, "equally_spaced": False}
    return {"layout": "scattered"}


def _patterns(cylinders: list[dict], tol: _Tolerances, diag: float) -> list[dict]:
    """Groups of full bores (or bosses) with the same diameter and parallel axes."""
    groups: list[list[dict]] = []
    cosine = math.cos(math.radians(MERGE_ANGLE_DEG) + (0.0 if tol.clean else 2.0 * math.sqrt(tol.normal_var)))
    for cylinder in (c for c in cylinders if c["full"]):
        slack = 2.0 * tol.radial(cylinder["radius"]) + FIT_REL * diag
        for group in groups:
            first = group[0]
            if (first["shape"] == cylinder["shape"] and abs(first["radius"] - cylinder["radius"]) <= slack
                    and abs(float(first["axis"] @ cylinder["axis"])) >= cosine):
                group.append(cylinder)
                break
        else:
            groups.append([cylinder])
    patterns = []
    for group in groups:
        if len(group) < 2:
            continue
        axis = group[0]["axis"]
        u, v = _basis(axis)
        middles = np.array([0.5 * (c["start"] + c["end"]) for c in group])
        slack = 4.0 * tol.radial(group[0]["radius"]) + 5.0 * FIT_REL * diag
        layout = _layout(np.stack([middles @ u, middles @ v], axis=1), slack)
        patterns.append(_public({"count": len(group), "shape": group[0]["shape"], "ids": [c["id"] for c in group],
                                 "diameter": float(np.mean([c["diameter"] for c in group])), "axis": axis,
                                 "centre": middles.mean(axis=0), **layout}))
    patterns.sort(key=lambda p: (-p["count"], -p["diameter"]))
    return patterns[:PATTERN_LIST_MAX]


# --- writing numbers ----------------------------------------------------------------------


def _n(value: float, digits: int = 2) -> str:
    text = f"{round(float(value), digits):.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _p(point: Sequence[float], digits: int = 2) -> str:
    return "(" + ",".join(_n(c, digits) for c in point) + ")"


def _cylinder_line(c: dict) -> str:
    where = f"axis {_p(c['axis'], 3)} from {_p(c['start'])} to {_p(c['end'])}"
    trust = f"fit rms {_n(c['rms'], 3)}"
    pieces = f", in {c['segments']} pieces" if c.get("segments", 1) > 1 else ""
    if c["full"]:
        ends = f" {c['ends']}" if c.get("ends") else ""
        return f"{c['id']} {c['shape']} ⌀{_n(c['diameter'])}{ends}, length {_n(c['length'])}, {where}{pieces}, {trust}"
    side = "convex" if c["shape"] == "boss" else "concave"
    return (f"{c['id']} arc R{_n(c['radius'])} over {_n(c['arc'], 0)}° {side}, length {_n(c['length'])}, "
            f"{where}, facing {_p(c['facing'])}{pieces}, {trust}")


def _plane_line(plane: dict) -> str:
    low, high = plane["bbox"]
    return (f"{plane['id']} normal {_p(plane['normal'], 3)} offset {_n(plane['offset'])}, area {_n(plane['area'], 1)}, "
            f"size {_n(plane['size'][0])} × {_n(plane['size'][1])}, centre {_p(plane['centre'])}, within {_p(low)}..{_p(high)}")


def _sphere_line(s: dict) -> str:
    return f"{s['id']} {s['shape']} ⌀{_n(s['diameter'])} centre {_p(s['centre'])}, fit rms {_n(s['rms'], 3)}"


def _feature_line(feature: dict) -> str:
    kind = feature["kind"]
    if kind == "plane":
        return _plane_line(feature)
    if kind == "cylinder":
        return _cylinder_line(feature)
    if kind == "sphere":
        return _sphere_line(feature)
    low, high = feature["bbox"]
    return f"{feature['id']} freeform surface, area {_n(feature['area'], 1)}, within {_p(low)}..{_p(high)}"


# --- overview -----------------------------------------------------------------------------


def overview(triangles: np.ndarray, name: str) -> dict:
    """Everything worth saying about a mesh, as a JSON-serialisable dict. Never raises on odd meshes."""
    info: dict = {"name": name, "triangles": 0, "warnings": []}
    try:
        mesh = _index(triangles)
    except Exception as exc:  # noqa: BLE001 -- an unreadable array is reported, not raised
        info["warnings"].append(f"the mesh could not be read ({type(exc).__name__}: {exc})")
        return info
    info["triangles"] = len(mesh.faces)
    if len(mesh.faces) == 0:
        info["warnings"].append("the mesh has no triangles")
        return info
    info["bbox"] = {"min": _round(mesh.lo), "max": _round(mesh.hi)}
    info["size"] = _round(mesh.hi - mesh.lo)
    info["area"] = round(float(mesh.areas.sum()), 3)
    info["watertight"] = mesh.watertight
    if mesh.watertight:
        info["volume"] = round(abs(_signed_volume(mesh)), 3)
        if mesh.flipped_edges:
            info["warnings"].append(f"{mesh.flipped_edges} edges have inconsistent winding; bore/boss may be confused there")
        elif _signed_volume(mesh) < 0:
            info["warnings"].append("the mesh is inside out (normals point inward); bore and boss are swapped")
    else:
        info["warnings"].append(f"not watertight ({mesh.open_edges} open edges, {mesh.odd_edges} edges shared by more "
                                "than two faces): no volume, centre of mass is that of the surface")
    centre = _centre_of_mass(mesh)
    info["centre_of_mass"] = _round(centre)

    for key in ("planes", "pairs", "cylinders", "spheres", "freeform", "patterns", "sections"):
        info[key] = []
    found = None
    try:
        found = _analyse(mesh)
        info["mesh"] = {"kind": "clean" if found.tol.clean else "noisy", "noise": round(found.tol.noise, 4),
                        "fit_tolerance": round(found.tol.fit, 4)}
        info["warnings"] += found.warnings
        by_kind = {kind: [f for f in found.features if f["kind"] == kind] for kind in ("plane", "cylinder", "sphere", "freeform")}
        info["planes"] = [_public(f) for f in by_kind["plane"]]
        info["cylinders"] = [_public(f) for f in by_kind["cylinder"]]
        info["spheres"] = [_public(f) for f in by_kind["sphere"]]
        info["freeform"] = [_public(f) for f in by_kind["freeform"]]
        listed = [p for p in by_kind["plane"] if p["area"] >= PLANE_LIST_AREA * info["area"]][:PLANE_LIST_MAX]
        info["pairs"] = _plane_pairs(listed, found.tol)
        info["patterns"] = _patterns(by_kind["cylinder"], found.tol, mesh.diag)
    except Exception as exc:  # noqa: BLE001 -- the basics above are still worth returning
        info["warnings"].append(f"features could not be measured ({type(exc).__name__}: {exc})")
    try:
        tol = found.tol if found else _tolerances(mesh)
        for axis in "xyz":
            at = float(centre["xyz".index(axis)])
            cut = _section(mesh, tol, _plane_frame(axis, None), at)
            info["sections"].append({"axis": axis, "at": round(at, 3), "summary": _section_summary(cut)})
    except Exception as exc:  # noqa: BLE001
        info["warnings"].append(f"sections could not be cut ({type(exc).__name__}: {exc})")
    return info


def overview_text(info: dict) -> str:
    """The overview as compact text for a language model."""
    name = info.get("name", "scan")
    if not info.get("triangles"):
        return f"scan {name}: " + "; ".join(info.get("warnings") or ["nothing to measure"])
    low, high = info["bbox"]["min"], info["bbox"]["max"]
    kind = info.get("mesh", {}).get("kind")
    quality = {"clean": "clean CAD-like mesh", "noisy": f"noisy mesh (noise about {_n(info.get('mesh', {}).get('noise', 0), 3)} mm)"}.get(kind, "mesh")
    lines = [
        f"scan {name}: {info['triangles']} triangles, {quality}, {'watertight' if info['watertight'] else 'NOT watertight'}",
        "bbox " + ", ".join(f"{a} {_n(low[i])}..{_n(high[i])}" for i, a in enumerate("xyz"))
        + "; size " + " × ".join(_n(s) for s in info["size"]) + " mm",
        (f"volume {_n(info['volume'], 1)} mm3, " if "volume" in info else "")
        + f"area {_n(info['area'], 1)} mm2, centre of mass {_p(info['centre_of_mass'])}",
    ]
    total = info["area"]
    planes = [p for p in info["planes"] if p["area"] >= PLANE_LIST_AREA * total][:PLANE_LIST_MAX]
    if planes:
        lines.append(f"flat faces (outward normal; offset = normal·point), {len(planes)} of {len(info['planes'])}, largest first:")
        lines += [" " + _plane_line(p) for p in planes]
    if info["pairs"]:
        lines.append("parallel faces: " + "; ".join(f"{p['a']}|{p['b']} {p['kind']} {_n(p['distance'])}" for p in info["pairs"]))
    cylinders = info["cylinders"]
    if cylinders:
        lines.append(f"cylinders (bore = hole, boss = pin or outer round, arc = part of a circle such as a fillet or a rounded "
                     f"end; sizes are of the true circle), {len(cylinders)}:")
        lines += [" " + _cylinder_line(c) for c in cylinders[:CYLINDER_LIST_MAX]]
        if len(cylinders) > CYLINDER_LIST_MAX:
            rest = cylinders[CYLINDER_LIST_MAX:]
            lines.append(f" ... and {len(rest)} smaller ones (⌀{_n(min(c['diameter'] for c in rest))}"
                         f"..{_n(max(c['diameter'] for c in rest))}); probe a point to identify one")
    else:
        lines.append("cylinders: none found")
    if info["spheres"]:
        lines.append("spheres: " + "; ".join(_sphere_line(s) for s in info["spheres"][:4]))
    lines += ["pattern: " + _pattern_text(pattern) for pattern in info["patterns"]]
    freeform = info["freeform"][:FREEFORM_LIST_MAX]
    if freeform:
        lines.append("curved regions that are neither flat nor round (measure them with sections):")
        lines += [" " + _feature_line(f) for f in freeform]
    if info["sections"]:
        lines.append("sections through the centre of mass:")
        lines += [f" {s['axis']}={_n(s['at'])}: {s['summary']}" for s in info["sections"]]
    lines += [f"warning: {warning}" for warning in info["warnings"]]
    return "\n".join(lines)


def _pattern_text(pattern: dict) -> str:
    text = (f"{pattern['count']} × {pattern['shape']} ⌀{_n(pattern['diameter'])} ({' '.join(pattern['ids'])}), "
            f"axes along {_p(pattern['axis'], 3)}, ")
    layout = pattern["layout"]
    if layout == "pair":
        text += f"{_n(pattern['spacing'])} apart"
    elif layout == "rectangle":
        text += f"on a {_n(pattern['sides'][0])} × {_n(pattern['sides'][1])} rectangle"
    elif layout == "bolt circle":
        text += f"on a ⌀{_n(pattern['circle_diameter'])} circle" + (", equally spaced" if pattern["equally_spaced"] else "")
    elif layout == "row":
        text += "in a row" + (f", pitch {_n(pattern['pitch'])}" if "pitch" in pattern else "")
    else:
        text += "scattered"
    return text + f", centre {_p(pattern['centre'])}"


# --- sections -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Frame:
    """A cutting plane's direction and the 2D coordinates used in it."""

    normal: np.ndarray
    u: np.ndarray
    v: np.ndarray
    label: str  # "z" for an axis section, "" for a general plane
    names: tuple[str, str]  # what the two coordinates are called


def _plane_frame(axis: str | Sequence[float] | None, normal: Sequence[float] | None) -> _Frame:
    """The frame of a section along 'x'|'y'|'z' or across an arbitrary normal."""
    if normal is None and axis is not None and not isinstance(axis, str):
        normal = axis
    if normal is not None:
        direction = np.asarray(normal, dtype=np.float64).reshape(-1)
        if direction.shape != (3,) or not np.isfinite(direction).all() or np.linalg.norm(direction) == 0.0:
            raise ValueError("the plane normal must be a non-zero vector [nx, ny, nz]")
        direction = _unit(direction)
        for name, unit in _AXES.items():
            if direction @ unit > 1.0 - 1e-9:  # exactly an axis: use the plain coordinates
                return _plane_frame(name, None)
        u, v = _basis(direction)
        return _Frame(direction, u, v, "", ("u", "v"))
    name = str(axis).lower()
    if name not in _AXES:
        raise ValueError("axis must be 'x', 'y' or 'z' (or give normal: [nx, ny, nz])")
    first, second = [a for a in "xyz" if a != name]
    return _Frame(_AXES[name], _AXES[first], _AXES[second], name, (first, second))


def _cut_loops(mesh: _Mesh, frame: _Frame, at: float) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Closed loops and open chains (3D point arrays) where the plane cuts the mesh.

    Every cut point lies on a mesh edge, so segments are joined exactly by the
    edge they share -- no distance tolerance. Vertices on the plane are treated
    as just above it, which keeps every triangle's cut a single segment.
    """
    height = mesh.verts @ frame.normal - at
    height = np.where(np.abs(height) < 1e-9 * mesh.diag, 1e-9 * mesh.diag, height)
    corner_above = (height > 0)[mesh.faces]
    crossing = np.flatnonzero(corner_above.any(axis=1) & ~corner_above.all(axis=1))
    if len(crossing) == 0:
        return [], []
    corner_above = corner_above[crossing]
    single_above = corner_above.sum(axis=1) == 1
    lone = np.argmax(corner_above == single_above[:, None], axis=1)  # the corner alone on its side
    rows = np.arange(len(crossing))
    faces = mesh.faces[crossing]
    apex, left, right = faces[rows, lone], faces[rows, (lone + 1) % 3], faces[rows, (lone + 2) % 3]

    def on_edge(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        t = height[a] / (height[a] - height[b])
        point = mesh.verts[a] + t[:, None] * (mesh.verts[b] - mesh.verts[a])
        return point, np.minimum(a, b) * np.int64(len(mesh.verts)) + np.maximum(a, b)

    point_left, key_left = on_edge(apex, left)
    point_right, key_right = on_edge(apex, right)
    # One direction round the solid for every segment, whichever side the lone corner is on.
    start_point = np.where(single_above[:, None], point_left, point_right)
    start_key = np.where(single_above, key_left, key_right)
    end_point = np.where(single_above[:, None], point_right, point_left)
    end_key = np.where(single_above, key_right, key_left)

    order = np.argsort(start_key, kind="stable")
    slot = np.minimum(np.searchsorted(start_key[order], end_key), len(order) - 1)
    follower = np.where(start_key[order][slot] == end_key, order[slot], -1)
    has_previous = np.zeros(len(crossing), dtype=bool)
    has_previous[follower[follower >= 0]] = True

    follower_list = follower.tolist()
    visited = [False] * len(follower_list)
    loops: list[np.ndarray] = []
    chains: list[np.ndarray] = []
    for first in np.flatnonzero(~has_previous).tolist() + list(range(len(follower_list))):
        if visited[first]:
            continue
        walk = []
        current = first
        while current >= 0 and not visited[current]:
            visited[current] = True
            walk.append(current)
            current = follower_list[current]
        if current == first:
            loops.append(start_point[walk])
        else:
            chains.append(np.vstack([start_point[walk], end_point[walk[-1]]]))
    return loops, chains


def _corners(points: np.ndarray) -> np.ndarray:
    """A closed 2D loop without the points that lie on a straight run."""
    before = points - np.roll(points, 1, axis=0)
    after = np.roll(points, -1, axis=0) - points
    cross = before[:, 0] * after[:, 1] - before[:, 1] * after[:, 0]
    dot = (before * after).sum(axis=1)
    return points[np.abs(np.arctan2(cross, dot)) > math.radians(COLLINEAR_DEG)]


def _simplify(points: np.ndarray, limit: int) -> np.ndarray:
    """A closed 2D loop as at most `limit` vertices (Douglas-Peucker with a growing tolerance)."""
    if len(points) <= limit:
        return points
    far = int(np.argmax(np.linalg.norm(points - points[0], axis=1)))
    ring = np.vstack([points, points[:1]])
    tolerance = 0.002 * float(np.linalg.norm(np.ptp(points, axis=0)))
    while True:
        keep = np.zeros(len(ring), dtype=bool)
        keep[[0, far, len(ring) - 1]] = True
        stack = [(0, far), (far, len(ring) - 1)]
        while stack:
            a, b = stack.pop()
            if b - a < 2:
                continue
            chord = ring[b] - ring[a]
            length = float(np.linalg.norm(chord)) or 1.0
            inner = ring[a + 1:b] - ring[a]
            distance = np.abs(inner[:, 0] * chord[1] - inner[:, 1] * chord[0]) / length
            worst = int(np.argmax(distance))
            if distance[worst] > tolerance:
                keep[a + 1 + worst] = True
                stack += [(a, a + 1 + worst), (a + 1 + worst, b)]
        if np.count_nonzero(keep) - 1 <= limit:
            return ring[keep][:-1]
        tolerance *= 2.0


def _inside(point: np.ndarray, polygon: np.ndarray) -> bool:
    x, y = polygon[:, 0], polygon[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    straddles = (y > point[1]) != (y2 > point[1])
    crossing_x = x + (point[1] - y) / np.where(straddles, y2 - y, 1.0) * (x2 - x)
    return bool(np.count_nonzero(straddles & (crossing_x > point[0])) % 2)


def _section(mesh: _Mesh, tol: _Tolerances, frame: _Frame, at: float) -> dict:
    origin = at * frame.normal  # on an axis section u and v are world axes, so 2D coordinates are world coordinates
    result: dict = {
        "plane": {"axis": frame.label or None, "normal": _round(frame.normal, 6), "at": round(float(at), 4),
                  "origin": _round(origin), "u": _round(frame.u, 6), "v": _round(frame.v, 6), "coords": list(frame.names)},
        "loops": [], "loop_count": 0, "hole_count": 0, "open_chains": 0, "area": 0.0, "warnings": [],
    }
    if len(mesh.faces) == 0:
        result["warnings"].append("the mesh has no triangles")
        return result
    loops3d, chains = _cut_loops(mesh, frame, at)
    if chains:
        result["open_chains"] = len(chains)
        length = sum(float(np.linalg.norm(np.diff(chain, axis=0), axis=1).sum()) for chain in chains)
        result["warnings"].append(f"{len(chains)} open chains (total length {_n(length)}): the mesh is not closed there")
    loops = []
    for points3d in loops3d:
        if len(points3d) < 3:
            continue
        flat = np.stack([(points3d - origin) @ frame.u, (points3d - origin) @ frame.v], axis=1)
        x, y = flat[:, 0], flat[:, 1]
        signed = 0.5 * float((x * np.roll(y, -1) - np.roll(x, -1) * y).sum())
        loops.append({"flat": flat, "points": points3d, "area": abs(signed)})
    loops.sort(key=lambda loop: -loop["area"])

    # Nesting: a loop inside an odd number of larger loops bounds a hole.
    for index, loop in enumerate(loops):
        holders = [j for j in range(min(index, 60)) if _inside(loop["flat"][0], loops[j]["flat"])]
        loop["hole"] = len(holders) % 2 == 1
        loop["parent"] = holders[-1] + 1 if holders else None
    result["area"] = round(sum(-loop["area"] if loop["hole"] else loop["area"] for loop in loops), 3)
    result["loop_count"] = len(loops)
    result["hole_count"] = sum(loop["hole"] for loop in loops)

    for index, loop in enumerate(loops[:SECTION_LOOPS_MAX * 3]):
        flat, points3d = loop["flat"], loop["points"]
        low, high = flat.min(axis=0), flat.max(axis=0)
        closed = np.vstack([flat, flat[:1]])
        entry = {
            "index": index + 1, "hole": loop["hole"], "parent": loop["parent"], "area": round(loop["area"], 3),
            "perimeter": round(float(np.linalg.norm(np.diff(closed, axis=0), axis=1).sum()), 3),
            "size": _round(high - low), "bbox2d": [_round(low), _round(high)],
            "bbox3d": [_round(points3d.min(axis=0)), _round(points3d.max(axis=0))], "circle": None,
        }
        # On a faceted cylinder only the corners of the outline lie on the true circle.
        corners = _corners(flat) if tol.clean else flat
        if len(corners) >= CIRCLE_MIN_CORNERS:
            cx, cy, radius = _fit_circle(corners[:, 0], corners[:, 1])
            deviation = float(np.abs(np.hypot(corners[:, 0] - cx, corners[:, 1] - cy) - radius).max())
            # ... and they go all the way round: the corners of a rounded rectangle lie on a circle too.
            longest = float(np.linalg.norm(np.roll(corners, -1, axis=0) - corners, axis=1).max())
            if deviation <= max(CIRCLE_REL * radius, 1.5 * tol.fit) and longest <= CIRCLE_MAX_CHORD * radius:
                entry["circle"] = {"centre": _round(origin + cx * frame.u + cy * frame.v), "centre2d": _round([cx, cy]),
                                   "diameter": round(2.0 * radius, 4), "max_dev": round(deviation, 4)}
        if entry["circle"] is None and index < SECTION_LOOPS_MAX:
            entry["polygon"] = [_round(point, 3) for point in _simplify(_corners(flat), POLYGON_MAX_VERTICES)]
        result["loops"].append(entry)
    return result


def section(triangles: np.ndarray, axis: str | Sequence[float] | None, at: float,
            normal: Sequence[float] | None = None) -> dict:
    """The outlines where a plane cuts the mesh.

    The plane is `axis` = at for 'x' | 'y' | 'z', or normal . p = at for an
    arbitrary `normal` [nx, ny, nz] (normalised here; `at` is in mm along it).
    """
    try:
        frame = _plane_frame(axis, normal)
    except ValueError as exc:
        return {"plane": None, "loops": [], "loop_count": 0, "hole_count": 0, "open_chains": 0, "area": 0.0, "warnings": [str(exc)]}
    mesh = _index(triangles)
    result = _section(mesh, _tolerances(mesh), frame, float(at))
    if len(mesh.faces) and not result["loops"] and not result["warnings"]:
        heights = mesh.verts @ frame.normal
        result["warnings"].append(f"the plane misses the mesh, which spans {_n(heights.min())}..{_n(heights.max())} along the normal")
    if any(loop["circle"] for loop in result["loops"]):
        try:  # name the feature a circular loop is a cut through, as the overview does
            cylinders = [f for f in _analyse(mesh).features if f["kind"] == "cylinder"]
            for loop in result["loops"]:
                if loop["circle"]:
                    loop["feature"] = _cylinder_at(cylinders, np.array(loop["circle"]["centre"]), loop["circle"]["diameter"], frame.normal)
        except Exception as exc:  # noqa: BLE001 -- the ids are a convenience, the outlines stand without them
            result["warnings"].append(f"features could not be matched ({type(exc).__name__})")
    return result


def _cylinder_at(cylinders: list[dict], centre: np.ndarray, diameter: float, normal: np.ndarray) -> str | None:
    """The id of the cylinder a circular section loop is a cut through, if any."""
    for cylinder in cylinders:
        if abs(float(cylinder["axis"] @ normal)) < 0.999 or abs(cylinder["diameter"] - diameter) > 0.02 * diameter + 0.01:
            continue
        moved = centre - cylinder["start"]
        if np.linalg.norm(moved - (moved @ cylinder["axis"]) * cylinder["axis"]) <= 0.02 * diameter + 0.01:
            return cylinder["id"]
    return None


def _section_summary(cut: dict) -> str:
    """One line: outer outlines and what is inside them."""
    loops = cut["loops"]
    if not loops:
        return "nothing cut"

    def shape(loop: dict) -> str:
        if loop["circle"]:
            return f"circle ⌀{_n(loop['circle']['diameter'])}"
        return f"{_n(loop['size'][0])} × {_n(loop['size'][1])}"

    outers = [loop for loop in loops if not loop["hole"]]
    outer_count = cut["loop_count"] - cut["hole_count"]
    text = f"{outer_count} outline" + ("s" if outer_count != 1 else "")
    text += " (" + ", ".join(shape(loop) for loop in outers[:3]) + (", ..." if outer_count > 3 else "") + ")"
    if cut["hole_count"]:
        circles = [loop["circle"]["diameter"] for loop in loops if loop["hole"] and loop["circle"]]
        text += f", {cut['hole_count']} hole" + ("s" if cut["hole_count"] != 1 else "")
        if circles:
            text += " (round: " + ", ".join(f"⌀{_n(d)}" for d in sorted(circles, reverse=True)[:6]) + ")"
    return text + f", material area {_n(cut['area'], 1)}"


def section_text(cut: dict) -> str:
    """A section as compact text for a language model."""
    plane = cut.get("plane")
    if plane is None:
        return "section: " + "; ".join(cut.get("warnings") or ["no plane"])
    if plane["axis"]:
        head = f"section {plane['axis']}={_n(plane['at'])} (2D coordinates are {plane['coords'][0]},{plane['coords'][1]})"
    else:
        head = (f"section across normal {_p(plane['normal'], 3)} at offset {_n(plane['at'])} (2D coordinates u,v: "
                f"origin {_p(plane['origin'])}, u {_p(plane['u'], 3)}, v {_p(plane['v'], 3)})")
    loops = cut["loops"]
    if not loops:
        return head + ": " + "; ".join(cut["warnings"] or ["nothing cut"])
    lines = [f"{head}: {cut['loop_count']} closed loop{'s' if cut['loop_count'] != 1 else ''}, {cut['hole_count']} of them "
             f"holes, material area {_n(cut['area'], 1)} mm2"]
    a, b = plane["coords"]
    for loop in loops[:SECTION_LOOPS_MAX]:
        role = f"hole in {loop['parent']}" if loop["hole"] else (f"island in {loop['parent']}" if loop["parent"] else "outer")
        if loop["circle"]:
            circle = loop["circle"]
            name = f" = {loop['feature']}" if loop.get("feature") else ""
            lines.append(f" {loop['index']} {role}: circle ⌀{_n(circle['diameter'])}{name}, centre {_p(circle['centre'])}, "
                         f"max deviation {_n(circle['max_dev'], 3)}")
        else:
            low, high = loop["bbox2d"]
            polygon = " ".join(_p(point) for point in loop["polygon"])
            lines.append(f" {loop['index']} {role}: {_n(loop['size'][0])} × {_n(loop['size'][1])}, {a} {_n(low[0])}..{_n(high[0])}, "
                         f"{b} {_n(low[1])}..{_n(high[1])}, area {_n(loop['area'], 1)}, perimeter {_n(loop['perimeter'], 1)}; "
                         f"polygon {polygon}")
    if cut["loop_count"] > SECTION_LOOPS_MAX:
        circles = sorted({_n(loop["circle"]["diameter"]) for loop in loops[SECTION_LOOPS_MAX:] if loop["circle"]})
        lines.append(f" ... and {cut['loop_count'] - SECTION_LOOPS_MAX} smaller loops"
                     + (f" (circles ⌀{', ⌀'.join(circles[:8])})" if circles else ""))
    lines += [f"warning: {warning}" for warning in cut["warnings"]]
    return "\n".join(lines)


# --- probing a point ---------------------------------------------------------------------------


def _closest_on_triangles(point: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """The closest point of each triangle (a, b, c) to `point`; arrays broadcast over leading axes."""
    ab, ac, ap = b - a, c - a, point - a
    d00, d01, d11 = (ab * ab).sum(-1), (ab * ac).sum(-1), (ac * ac).sum(-1)
    d0p, d1p = (ab * ap).sum(-1), (ac * ap).sum(-1)
    denominator = d00 * d11 - d01 * d01
    safe = np.where(denominator > 0, denominator, 1.0)
    s = (d11 * d0p - d01 * d1p) / safe
    t = (d00 * d1p - d01 * d0p) / safe
    inside = (denominator > 0) & (s >= 0) & (t >= 0) & (s + t <= 1)
    best = a + s[..., None] * ab + t[..., None] * ac
    best_distance = np.where(inside, 0.0, np.inf)  # inside the triangle the projection wins outright
    for start, end in ((a, b), (b, c), (c, a)):
        edge = end - start
        along = np.clip(((point - start) * edge).sum(-1) / np.maximum((edge * edge).sum(-1), 1e-300), 0.0, 1.0)
        candidate = start + along[..., None] * edge
        distance = ((point - candidate) ** 2).sum(-1)
        closer = distance < best_distance
        best = np.where(closer[..., None], candidate, best)
        best_distance = np.where(closer, distance, best_distance)
    return best


def _first_hit(mesh: _Mesh, origin: np.ndarray, direction: np.ndarray) -> float | None:
    """Distance along a ray to the first triangle it meets (Moller-Trumbore over all faces)."""
    a = mesh.tri[:, 0]
    edge1, edge2 = mesh.tri[:, 1] - a, mesh.tri[:, 2] - a
    p = np.cross(direction, edge2)
    det = (edge1 * p).sum(axis=1)
    safe = np.where(np.abs(det) > 1e-14, det, 1.0)
    offset = origin - a
    u = (offset * p).sum(axis=1) / safe
    q = np.cross(offset, edge1)
    v = (q @ direction) / safe
    distance = (edge2 * q).sum(axis=1) / safe
    slack = 1e-9
    hit = (np.abs(det) > 1e-14) & (u >= -slack) & (v >= -slack) & (u + v <= 1 + slack) & (distance > 1e-5 * mesh.diag)
    return float(distance[hit].min()) if hit.any() else None


def _local_shape(mesh: _Mesh, tol: _Tolerances, face: int, centre: np.ndarray, radius: float) -> dict:
    """What the surface around a face looks like within `radius`: plane, cylinder, sphere or just curved."""
    near = np.union1d(np.flatnonzero(np.linalg.norm(mesh.centroids - centre, axis=1) <= radius), [face])
    patch = next(part for part in _connected_parts(mesh, near, _smooth_edges(mesh, tol)) if face in part)
    normal, offset, rms = _oriented_plane(mesh, patch)
    if rms <= tol.fit:
        return {"kind": "plane", "normal": _round(normal), "offset": round(offset, 4), "rms": round(rms, 4), "faces_used": len(patch)}
    fitted = _fit_surface(mesh, patch, tol, robust=not tol.clean)
    if fitted is not None:
        return {**_public(fitted), "faces_used": len(patch)}
    return {"kind": "curved", "faces_used": len(patch)}


def probe(triangles: np.ndarray, point: Sequence[float], radius: float | None = None) -> dict:
    """What is at a 3D point: nearest surface point and normal, the feature there, wall thickness.

    The feature is the one the overview lists (same id) when the point lands on
    one; otherwise, or when `radius` is given, the surface within `radius` of
    the hit is fitted on its own.
    """
    result: dict = {"point": [], "warnings": []}
    try:
        target = np.asarray(point, dtype=np.float64).reshape(3)
        result["point"] = _round(target)
        mesh = _index(triangles)
        if len(mesh.faces) == 0 or not np.isfinite(target).all():
            raise ValueError("the mesh has no triangles" if len(mesh.faces) == 0 else "the point must be three numbers")
    except Exception as exc:  # noqa: BLE001 -- odd input is reported, not raised
        result["warnings"].append(f"cannot probe: {exc}")
        return result
    closest = _closest_on_triangles(target, mesh.tri[:, 0], mesh.tri[:, 1], mesh.tri[:, 2])
    distance = np.linalg.norm(closest - target, axis=1)
    face = int(np.argmin(distance))
    hit, normal = closest[face], mesh.normals[face]
    result["nearest"] = {"point": _round(hit), "distance": round(float(distance[face]), 4), "normal": _round(normal)}

    found = _analyse(mesh)
    index = int(found.face_feature[face])
    feature = found.features[index] if index >= 0 else None
    if feature is not None:
        result["feature"] = _public(feature)
        if feature["kind"] == "plane":
            normal = feature["normal"]
        elif feature["kind"] == "cylinder":  # the wall's true normal is radial, whatever the facet says
            moved = hit - feature["start"]
            radial = _unit(moved - (moved @ feature["axis"]) * feature["axis"])
            normal = radial if feature["shape"] == "boss" else -radial
    if feature is None or feature["kind"] == "freeform" or radius is not None:
        reach = float(radius) if radius is not None else max(0.05 * mesh.diag, 3.0 * float(np.sqrt(mesh.areas.mean())))
        try:
            result["local"] = {**_local_shape(mesh, found.tol, face, hit, reach), "within": round(reach, 3)}
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"the surface around the point could not be fitted ({type(exc).__name__})")
    behind = _first_hit(mesh, hit, -normal)
    ahead = _first_hit(mesh, hit, normal)
    result["thickness"] = round(behind, 4) if behind is not None else None
    result["clearance"] = round(ahead, 4) if ahead is not None else None
    if behind is None:
        result["warnings"].append("no surface behind this point: the mesh is open or inside out here")
    return result


def probe_text(found: dict) -> str:
    """A probe as compact text for a language model."""
    head = f"probe {_p(found['point'])}"
    if "nearest" not in found:
        return head + ": " + "; ".join(found["warnings"])
    nearest = found["nearest"]
    lines = [f"{head}: surface {_n(nearest['distance'], 3)} away at {_p(nearest['point'])}, outward normal {_p(nearest['normal'], 3)}"]
    if "feature" in found:
        lines.append("on " + _feature_line(found["feature"]))
    local = found.get("local")
    if local:
        around = f"within {_n(local['within'])} mm ({local['faces_used']} triangles) the surface is "
        if local["kind"] == "plane":
            lines.append(around + f"flat: normal {_p(local['normal'], 3)} offset {_n(local['offset'])}, fit rms {_n(local['rms'], 3)}")
        elif local["kind"] == "cylinder":
            lines.append(around + "cylindrical: " + _cylinder_line({"id": "local", **local}))
        elif local["kind"] == "sphere":
            lines.append(around + "spherical: " + _sphere_line({"id": "local", **local}))
        else:
            lines.append(around + "curved, neither flat nor round")
    if found.get("thickness") is not None:
        text = f"wall thickness {_n(found['thickness'])} (straight in from the surface to the opposite side)"
        if found.get("clearance") is not None:
            text += f"; open space in front {_n(found['clearance'])} to the facing surface"
        lines.append(text)
    lines += [f"warning: {warning}" for warning in found["warnings"]]
    return "\n".join(lines)


# --- comparing a model with a scan ---------------------------------------------------------------


def _sample_surface(triangles: np.ndarray, count: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """`count` points spread evenly over the surface (area-weighted), and the triangle of each."""
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    cumulative = np.cumsum(np.linalg.norm(cross, axis=1))
    face = np.minimum(np.searchsorted(cumulative, rng.random(count) * cumulative[-1]), len(triangles) - 1)
    r1, r2 = np.sqrt(rng.random(count)), rng.random(count)
    weights = np.stack([1.0 - r1, r1 * (1.0 - r2), r1 * r2], axis=1)
    return np.einsum("ij,ijk->ik", weights, triangles[face]), face


def _spread_samples(triangles: np.ndarray, spacing: float) -> tuple[np.ndarray, np.ndarray]:
    """Points about `spacing` apart on every triangle, and the triangle of each.

    Stratified, not random: rows parallel to each triangle's shortest edge, from
    that edge to the opposite corner, and points along each row; at least one
    point per triangle. So every spot of a triangle -- of a long needle too --
    has one of that triangle's own samples within about `spacing`.
    """
    lengths = np.linalg.norm(triangles[:, [1, 2, 0]] - triangles, axis=2)  # edges 0-1, 1-2, 2-0
    shortest = np.argmin(lengths, axis=1)
    rows_of = np.arange(len(triangles))
    a, b, c = (triangles[rows_of, (shortest + k) % 3] for k in range(3))  # a-b is the shortest edge, c the apex
    base = lengths[rows_of, shortest]
    height = np.linalg.norm(np.cross(b - a, c - a), axis=1) / np.maximum(base, 1e-300)
    rows = np.maximum(np.ceil(height / spacing), 1).astype(np.int64)
    row_owner = np.repeat(rows_of, rows)
    towards_apex = (np.arange(len(row_owner)) - np.repeat(np.cumsum(rows) - rows, rows) + 0.5) / rows[row_owner]
    points_in_row = np.maximum(np.ceil(base[row_owner] * (1.0 - towards_apex) / spacing), 1).astype(np.int64)
    point_row = np.repeat(np.arange(len(row_owner)), points_in_row)
    along = (np.arange(len(point_row)) - np.repeat(np.cumsum(points_in_row) - points_in_row, points_in_row) + 0.5) / points_in_row[point_row]
    face = row_owner[point_row]
    v = towards_apex[point_row][:, None]
    return a[face] + v * (c[face] - a[face]) + along[:, None] * (1.0 - v) * (b[face] - a[face]), face


def _one_way(source: np.ndarray, target: np.ndarray, tol: float, rng: np.random.Generator) -> dict:
    """How far points on `source` are from the surface of `target`.

    Approximation: each query point is measured exactly (point to triangle)
    against the triangles of its DEVIATION_NEIGHBOURS nearest target samples,
    not against every triangle. The target is sampled about tol / 2 apart
    (coarser when that would exceed DEVIATION_SAMPLES_MAX points). The result
    is never below the true distance, and above it by less than the sample
    spacing -- in practice by nothing, or by the width of a sliver triangle.
    """
    queries, _ = _sample_surface(source, DEVIATION_QUERIES, rng)
    cross = np.linalg.norm(np.cross(target[:, 1] - target[:, 0], target[:, 2] - target[:, 0]), axis=1)
    longest = np.linalg.norm(target[:, [1, 2, 0]] - target, axis=2).max(axis=1)
    spacing = max(0.5 * tol, 1e-9)
    for _ in range(8):  # the count is about area / spacing^2 for big triangles, edge / spacing for slivers
        estimate = float(np.maximum(np.maximum(0.5 * cross / spacing ** 2, longest / spacing), 1.0).sum())
        if estimate <= DEVIATION_SAMPLES_MAX:
            break
        spacing *= 1.3
    samples, sample_face = _spread_samples(target, spacing)
    _, nearest = cKDTree(samples).query(queries, k=min(DEVIATION_NEIGHBOURS, len(samples)))
    candidates = target[sample_face[nearest.reshape(len(queries), -1)]]  # (queries, neighbours, 3, 3)
    closest = _closest_on_triangles(queries[:, None, :], candidates[:, :, 0], candidates[:, :, 1], candidates[:, :, 2])
    distances = np.linalg.norm(closest - queries[:, None, :], axis=2)
    best = np.argmin(distances, axis=1)
    rows = np.arange(len(queries))
    distance = distances[rows, best]
    winner = candidates[rows, best]
    outward = np.cross(winner[:, 1] - winner[:, 0], winner[:, 2] - winner[:, 0])
    side = np.sign(np.einsum("ij,ij->i", queries - closest[rows, best], outward))

    # The worst places, kept apart so they point at different problems.
    worst: list[list[float]] = []
    apart = 0.1 * float(np.linalg.norm(np.ptp(source.reshape(-1, 3), axis=0)))
    for index in np.argsort(-distance, kind="stable")[:2000]:
        if all(np.linalg.norm(queries[index] - np.array(w[:3])) > apart for w in worst):
            worst.append(_round([*queries[index], distance[index]], 3))
            if len(worst) == 3:
                break
    return {
        "within": round(float(np.mean(distance <= tol)), 4), "mean": round(float(distance.mean()), 4),
        "p95": round(float(np.percentile(distance, 95)), 4), "max": round(float(distance.max()), 4),
        "bias": round(float((side * distance).mean()), 4),  # > 0: the source lies outside the target on average
        "worst": worst,
    }


def deviation(model_triangles: np.ndarray, scan_triangles: np.ndarray, tol: float) -> dict:
    """How well a model reproduces a scan, both ways, on points sampled from each surface."""
    model = _without_degenerate(model_triangles)
    scan = _without_degenerate(scan_triangles)
    if len(model) == 0 or len(scan) == 0:
        raise ValueError("the model or the scan has no triangles")
    rng = np.random.default_rng(DEVIATION_SEED)
    return {
        "tol": float(tol),
        "scan_to_model": _one_way(scan, model, float(tol), rng),
        "model_to_scan": _one_way(model, scan, float(tol), rng),
    }


def deviation_text(result: dict, name: str) -> str:
    """A deviation as compact text for a language model."""
    lines = [f"scan {name} vs the model (tolerance {_n(result['tol'])} mm):"]
    for key, source, target, hint in (
        ("scan_to_model", "scan", "model", "the scan has surface the model lacks or misplaces"),
        ("model_to_scan", "model", "scan", "the model has surface the scan does not"),
    ):
        way = result[key]
        side = "outside" if way["bias"] > 0 else "inside"
        text = (f" {source}→{target}: {_n(100 * way['within'], 1)}% within tolerance, mean {_n(way['mean'], 3)}, "
                f"p95 {_n(way['p95'], 3)}, max {_n(way['max'], 3)}")
        if abs(way["bias"]) > 0.25 * result["tol"]:
            text += f"; the {source} lies {_n(abs(way['bias']), 3)} {side} the {target} on average"
        if way["max"] > result["tol"] and way["worst"]:
            text += "; worst at " + ", ".join(f"{_p(w[:3])} {_n(w[3])}" for w in way["worst"]) + f" ({hint})"
        lines.append(text)
    return "\n".join(lines)


# --- pictures --------------------------------------------------------------------------------------

# Named views: the direction the camera looks ALONG (from the eye towards the object).
_VIEW_DIRECTIONS = {
    "iso": (-1.0, 1.0, -1.0), "top": (0.0, 0.0, -1.0), "bottom": (0.0, 0.0, 1.0), "front": (0.0, 1.0, 0.0),
    "back": (0.0, -1.0, 0.0), "left": (1.0, 0.0, 0.0), "right": (-1.0, 0.0, 0.0),
}
_SHEET = (("ISO", "iso"), ("TOP X-Y", "top"), ("FRONT X-Z", "front"), ("RIGHT Y-Z", "right"))
# 3 x 5 glyphs, rows top to bottom, for the labels of the four-view sheet only.
_GLYPHS = {
    "I": "111010010010111", "S": "111100111001111", "O": "111101101101111", "T": "111010010010010",
    "P": "111101111100100", "X": "101101010101101", "Y": "101101010010010", "Z": "111001010100111",
    "F": "111100111100100", "R": "110101110101101", "N": "110101101101101", "G": "111100101101111",
    "H": "101101111101101", "-": "000000111000000", " ": "000000000000000",
}


def view_direction(name: str) -> np.ndarray:
    """The unit direction the camera looks along for "iso", "top", "bottom", "front", "back", "left", "right".

    "front" is seen from -Y (X to the right, Z up), "right" from +X, "top" from
    +Z with Y up, "iso" from the +X, -Y, +Z corner.
    """
    try:
        return _unit(np.array(_VIEW_DIRECTIONS[str(name).lower()]))
    except KeyError:
        raise ValueError(f"unknown view {name!r}; use one of {', '.join(_VIEW_DIRECTIONS)} or a direction") from None


@dataclass(frozen=True)
class _Camera:
    """An orthographic camera: world directions of the picture's axes, and the fit."""

    right: np.ndarray
    up: np.ndarray
    eye: np.ndarray  # towards the viewer, = -direction
    centre: np.ndarray
    scale: float  # pixels per mm
    size: int

    def project(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Pixel x, pixel y (down) and depth (larger = nearer) of world points."""
        moved = points - self.centre
        return (moved @ self.right) * self.scale + 0.5 * self.size, 0.5 * self.size - (moved @ self.up) * self.scale, moved @ self.eye


def _camera(points: np.ndarray, direction: Sequence[float], size: int, up: Sequence[float] | None = None,
            scale: float | None = None) -> _Camera:
    """A camera looking along `direction`, fitted to `points` with a margin unless `scale` is given."""
    look = np.asarray(direction, dtype=np.float64).reshape(3)
    if not np.isfinite(look).all() or np.linalg.norm(look) == 0.0:
        raise ValueError("the view direction must be a non-zero vector [x, y, z]")
    eye = -_unit(look)
    hint = _AXES["z"] if up is None else _unit(np.asarray(up, dtype=np.float64).reshape(3))
    if np.linalg.norm(np.cross(hint, eye)) < 1e-6:  # looking along the up hint: Y is up instead
        hint = _AXES["y"] if abs(eye[1]) < 0.9 else _AXES["x"]
    right = _unit(np.cross(hint, eye))
    upward = np.cross(eye, right)
    if len(points) == 0:
        return _Camera(right, upward, eye, np.zeros(3), scale or 1.0, size)
    across, along = points @ right, points @ upward
    low, high = points.min(axis=0), points.max(axis=0)
    centre = 0.5 * (across.min() + across.max()) * right + 0.5 * (along.min() + along.max()) * upward + (0.5 * (low + high) @ eye) * eye
    if scale is None:
        margin = max(8.0, 0.05 * size)
        scale = (size - 2.0 * margin) / (max(float(np.ptp(across)), float(np.ptp(along))) or 1.0)
    return _Camera(right, upward, eye, centre, scale, size)


def _decimate(triangles: np.ndarray) -> np.ndarray:
    """Vertex clustering: snap corners to a grid about a pixel wide and drop collapsed triangles."""
    points = triangles.reshape(-1, 3)
    low = points.min(axis=0)
    cell = float(np.linalg.norm(points.max(axis=0) - low)) / RENDER_CLUSTER_CELLS or 1.0
    cells = np.floor((points - low) / cell).astype(np.int64)
    keys = cells[:, 0] | (cells[:, 1] << 21) | (cells[:, 2] << 42)
    _, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    faces = inverse.reshape(-1, 3)
    faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])]
    return points[first][faces]


def _rasterise(px: np.ndarray, py: np.ndarray, depth: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray]:
    """Z-buffer of triangles given in pixel coordinates (n, 3): the nearest triangle and its depth per pixel.

    Vectorised scanlines: every triangle is expanded into its rows, every row
    into its pixels; the nearest fragment per pixel wins by sorting on depth.
    """
    count = len(px)
    row_low = np.clip(np.ceil(py.min(axis=1) - 0.5), 0, size).astype(np.int64)
    row_high = np.clip(np.floor(py.max(axis=1) - 0.5), -1, size - 1).astype(np.int64)
    rows = np.maximum(row_high - row_low + 1, 0)
    owner = np.repeat(np.arange(count), rows)
    y = row_low[owner] + np.arange(len(owner)) - np.repeat(np.cumsum(rows) - rows, rows)
    middle = y + 0.5
    left = np.full(len(owner), np.inf)
    right = np.full(len(owner), -np.inf)
    for i, j in ((0, 1), (1, 2), (2, 0)):
        y0, y1, x0, x1 = py[owner, i], py[owner, j], px[owner, i], px[owner, j]
        crosses = (np.minimum(y0, y1) <= middle) & (middle <= np.maximum(y0, y1)) & (y0 != y1)
        x = x0 + (middle - y0) / np.where(crosses, y1 - y0, 1.0) * (x1 - x0)
        left = np.where(crosses, np.minimum(left, x), left)
        right = np.where(crosses, np.maximum(right, x), right)
    valid = np.isfinite(left)
    col_low = np.clip(np.ceil(np.where(valid, left, 0.0) - 0.5), 0, size).astype(np.int64)
    col_high = np.clip(np.floor(np.where(valid, right, -1.0) - 0.5), -1, size - 1).astype(np.int64)
    cols = np.where(valid, np.maximum(col_high - col_low + 1, 0), 0)
    span = np.repeat(np.arange(len(owner)), cols)
    x = col_low[span] + np.arange(len(span)) - np.repeat(np.cumsum(cols) - cols, cols)
    triangle, y = owner[span], y[span]

    # Depth is linear over a triangle: its gradient in pixel space.
    ax, ay = px[:, 1] - px[:, 0], py[:, 1] - py[:, 0]
    bx, by = px[:, 2] - px[:, 0], py[:, 2] - py[:, 0]
    az, bz = depth[:, 1] - depth[:, 0], depth[:, 2] - depth[:, 0]
    det = ax * by - bx * ay
    safe = np.where(np.abs(det) > 1e-9, det, np.inf)  # edge-on triangles: constant depth
    slope_x, slope_y = (az * by - bz * ay) / safe, (bz * ax - az * bx) / safe
    z = depth[triangle, 0] + slope_x[triangle] * (x + 0.5 - px[triangle, 0]) + slope_y[triangle] * (y + 0.5 - py[triangle, 0])
    z = np.clip(z, depth.min(axis=1)[triangle], depth.max(axis=1)[triangle])

    nearest_last = np.argsort(z, kind="stable")  # on repeated indices the last assignment wins
    pixel = (y * size + x)[nearest_last]
    ids = np.full(size * size, -1, dtype=np.int64)
    zbuffer = np.full(size * size, -np.inf)
    ids[pixel] = triangle[nearest_last]
    zbuffer[pixel] = z[nearest_last]
    return ids.reshape(size, size), zbuffer.reshape(size, size)


def _shaded(triangles: np.ndarray, camera: _Camera, tones: np.ndarray | None = None) -> np.ndarray:
    """One shaded orthographic view as a (size, size, 3) uint8 image.

    Lambert shading from the upper left, plus dark outlines wherever
    neighbouring pixels are not on one plane (creases, steps, silhouettes), so
    holes and steps read even where two faces have the same brightness.
    """
    size = camera.size
    image = np.empty((size, size, 3), dtype=np.uint8)
    image[:] = BACKGROUND
    if len(triangles) == 0:
        return image
    normals = _unit(np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]))
    px, py, depth = camera.project(triangles)
    if tones is not None:  # where two tones coincide the main one shows: the second is drawn a hair behind
        depth = depth - (np.asarray(tones) > 0)[:, None] * (0.25 / camera.scale)
    ids, zbuffer = _rasterise(px, py, depth, size)
    drawn = ids >= 0
    facing = normals[np.maximum(ids, 0)]
    facing = np.where((facing @ camera.eye)[..., None] < 0, -facing, facing)  # both sides of an open mesh are lit
    light = _unit(0.75 * camera.eye + 0.55 * camera.up - 0.4 * camera.right)
    shade = 0.3 + 0.7 * np.clip(facing @ light, 0.0, 1.0)

    n_right, n_up, n_eye = facing @ camera.right, facing @ camera.up, facing @ camera.eye
    edge = np.zeros((size, size), dtype=bool)
    step = 1.0 / camera.scale
    with np.errstate(invalid="ignore"):
        off_x = np.abs(n_right[:, :-1] * step + n_eye[:, :-1] * (zbuffer[:, 1:] - zbuffer[:, :-1]))
        back_x = np.abs(-n_right[:, 1:] * step + n_eye[:, 1:] * (zbuffer[:, :-1] - zbuffer[:, 1:]))
        off_y = np.abs(-n_up[:-1] * step + n_eye[:-1] * (zbuffer[1:] - zbuffer[:-1]))
        back_y = np.abs(n_up[1:] * step + n_eye[1:] * (zbuffer[:-1] - zbuffer[1:]))
        across, down = np.maximum(off_x, back_x), np.maximum(off_y, back_y)
        pair_x, pair_y = drawn[:, :-1] & drawn[:, 1:], drawn[:-1] & drawn[1:]
        # A rough scan is off-plane everywhere by a little: only what stands out from that is an outline.
        usual = float(np.percentile(across[pair_x], 75)) if pair_x.any() else 0.0
        limit = max(0.6 * step, 4.0 * usual)
        edge[:, :-1] |= (across > limit) & pair_x
        edge[:-1] |= (down > limit) & pair_y
    edge[:, :-1] |= drawn[:, :-1] != drawn[:, 1:]
    edge[:-1] |= drawn[:-1] != drawn[1:]
    shade = np.where(edge, 0.3 * shade, shade)

    palette = np.array([SURFACE, SECOND_TONE], dtype=np.float64)
    tone = np.zeros((size, size), dtype=np.int64) if tones is None else np.asarray(tones)[np.maximum(ids, 0)] % len(palette)
    colour = np.clip(shade[..., None] * palette[tone] * 1.25, 0, 255).astype(np.uint8)
    visible = drawn | edge
    image[visible] = colour[visible]
    return image


def _draw_marks(image: np.ndarray, camera: _Camera, marks: Sequence[Sequence[float]]) -> None:
    """A red dot with a white rim at each 3D point, on top of everything."""
    size = camera.size
    points = np.asarray(marks, dtype=np.float64).reshape(-1, 3)
    px, py, _ = camera.project(points)
    rows, cols = np.mgrid[0:size, 0:size]
    for x, y in zip(px, py):
        distance = np.hypot(cols + 0.5 - x, rows + 0.5 - y)
        image[distance <= MARK_RADIUS + 2] = (255, 255, 255)
        image[distance <= MARK_RADIUS] = (220, 30, 30)


def _draw_label(image: np.ndarray, text: str, left: int, top: int, zoom: int = 3) -> None:
    for position, char in enumerate(text):
        bits = np.array([bit == "1" for bit in _GLYPHS.get(char, _GLYPHS[" "])]).reshape(5, 3)
        glyph = np.kron(bits, np.ones((zoom, zoom), dtype=bool))
        x = left + position * 4 * zoom
        image[top:top + 5 * zoom, x:x + 3 * zoom][glyph] = (40, 40, 40)


def _png(image: np.ndarray) -> bytes:
    """An (h, w, 3) uint8 image, or (h, w, 4) with transparency, as a PNG file: unfiltered rows, deflate, CRC per chunk."""
    height, width, channels = image.shape
    rows = np.zeros((height, 1 + channels * width), dtype=np.uint8)  # each row starts with filter type 0
    rows[:, 1:] = image.reshape(height, -1)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    header = struct.pack(">IIBBBBB", width, height, 8, 6 if channels == 4 else 2, 0, 0, 0)  # 8-bit RGB or RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)) + chunk(b"IEND", b"")


def _corner_array(vertices: np.ndarray, faces: np.ndarray | None) -> np.ndarray:
    """(n, 3, 3) triangle corners from an indexed mesh, or from a triangle array when `faces` is None."""
    vertices = np.asarray(vertices, dtype=np.float64)
    if faces is None:
        return vertices.reshape(-1, 3, 3)
    return vertices.reshape(-1, 3)[np.asarray(faces, dtype=np.int64).reshape(-1, 3)]


def render(vertices: np.ndarray, faces: np.ndarray | None, direction: Sequence[float], size: int = 512,
           up: Sequence[float] | None = None, marks: Sequence[Sequence[float]] | None = None,
           tones: np.ndarray | None = None, clear: bool = False) -> bytes:
    """A shaded orthographic picture of any mesh as PNG bytes, fitted to the mesh with a margin.
    With `clear` the ground around the mesh is transparent.

    `vertices` (m, 3) with `faces` (n, 3) indices -- or an (n, 3, 3) triangle
    array with `faces` None. `direction` is the vector the camera looks ALONG
    (from the eye towards the object; see `view_direction` for named views).
    `up` is the world direction that should point up in the picture (default
    +Z, or +Y when looking along Z). `marks` are 3D points drawn as red dots.
    `tones` gives a palette index per face (0 = main colour, 1 = second tone),
    e.g. to draw a scan over a model; where both coincide the main one shows.
    """
    size = int(np.clip(size, 64, 2048))
    triangles = _corner_array(vertices, faces)
    finite = np.isfinite(triangles).all(axis=(1, 2))
    triangles = triangles[finite]
    tones = None if tones is None else np.asarray(tones)[finite]
    camera = _camera(triangles.reshape(-1, 3), direction, size, up)
    if len(triangles) > RENDER_MAX_TRIANGLES and tones is None:
        triangles = _decimate(triangles)
    image = _shaded(triangles, camera, tones)
    if marks is not None and len(marks):
        _draw_marks(image, camera, marks)
    if clear:
        drawn = (image != np.array(BACKGROUND, dtype=np.uint8)).any(axis=2)
        image = np.dstack([image, np.where(drawn, 255, 0).astype(np.uint8)])
    return _png(image)


def view_caption(vertices: np.ndarray, direction: Sequence[float], size: int = 512, up: Sequence[float] | None = None) -> str:
    """One line saying how the picture `render` makes with the same arguments is oriented and scaled."""
    points = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    camera = _camera(points[np.isfinite(points).all(axis=1)], direction, int(np.clip(size, 64, 2048)), up)

    def named(vector: np.ndarray) -> str:
        for name, unit in _AXES.items():
            if abs(vector @ unit) > 1.0 - 1e-6:
                return ("+" if vector @ unit > 0 else "-") + name.upper()
        return _p(vector, 2)

    return (f"looking along {named(-camera.eye)}: picture right is {named(camera.right)}, picture up is {named(camera.up)}, "
            f"towards the viewer is {named(camera.eye)}; {_n(1.0 / camera.scale, 3)} mm per pixel, "
            f"{camera.size} × {camera.size} px, centred on {_p(camera.centre)}")


def render_views(triangles: np.ndarray) -> bytes:
    """One PNG with four shaded views: isometric, top, front and right, all at the same scale."""
    size = VIEW_PIXELS
    canvas = np.empty((2 * size, 2 * size, 3), dtype=np.uint8)
    canvas[:] = BACKGROUND
    triangles = _without_degenerate(triangles)
    if len(triangles) > RENDER_MAX_TRIANGLES:
        triangles = _decimate(triangles)
    if len(triangles):
        points = triangles.reshape(-1, 3)
        scale = (size - 50) / (float(np.linalg.norm(np.ptp(points, axis=0))) or 1.0)  # the diagonal fits in every view
        for index, (label, name) in enumerate(_SHEET):
            top, left = (index // 2) * size, (index % 2) * size
            camera = _camera(points, view_direction(name), size, scale=scale)
            canvas[top:top + size, left:left + size] = _shaded(triangles, camera)
            _draw_label(canvas, label, left + 8, top + 8)
    canvas[size - 1:size + 1, :] = (200, 200, 200)
    canvas[:, size - 1:size + 1] = (200, 200, 200)
    return _png(canvas)
