from build123d import *
from system import *

# --- Base
BASE_L = 76.0      # base length
BASE_W = 44.0      # base width
BASE_T = 5.0       # base thickness

# --- Wall
WALL_H = 52.0      # wall height
WALL_T = 5.0       # wall thickness
SLOT_W = 6.0       # adjustment slot width
SLOT_L = 22.0      # adjustment slot length

# --- Rib
RIB_T = 5.0        # rib thickness
RIB_L = 34.0       # rib length at the base

# --- Mounting
HOLE_D = 4.5       # mounting hole diameter
HOLE_INSET = 8.0   # hole inset from the edge
EDGE_R = 4.0       # corner radius

HOLE_BIG_D = 6.0   # diameter of the picked hole (front, right)
M4_L = 16.0        # M4 screw length (preview)


def base() -> Part:
    plate = Pos(0, 0, BASE_T / 2) * Box(BASE_L, BASE_W, BASE_T)
    plate = fillet(plate.edges().filter_by(Axis.Z), EDGE_R)
    x = BASE_L / 2 - HOLE_INSET
    for sx in (0.15, 1):
        for sy in (-1, 1):
            d = HOLE_BIG_D if (sx == 1 and sy == -1) else HOLE_D
            plate -= Pos(x * sx, sy * (BASE_W / 2 - HOLE_INSET), BASE_T / 2) * Cylinder(d / 2, BASE_T)
    return plate


def wall() -> Part:
    x = -BASE_L / 2 + WALL_T / 2
    plate = Pos(x, 0, BASE_T + WALL_H / 2) * Box(WALL_T, BASE_W, WALL_H)
    top = plate.edges().filter_by(Axis.X).group_by(Axis.Z)[-1]
    plate = fillet(top, EDGE_R * 2)
    slot = Pos(x, 0, BASE_T + WALL_H * 0.58) * Rot(0, 90, 0) * extrude(SlotOverall(SLOT_L, SLOT_W), WALL_T / 2, both=True)
    return plate - slot


def rib() -> Part:
    x0 = -BASE_L / 2 + WALL_T
    profile = Polygon((0, 0), (RIB_L, 0), (0, WALL_H * 0.7), align=None)
    solid = extrude(Plane.XZ * profile, RIB_T / 2, both=True)
    return Pos(x0, 0, BASE_T) * solid


def bracket() -> Part:
    part = base() + wall()
    for y in (-1, 1):
        part += Pos(0, y * (BASE_W / 2 - RIB_T / 2), 0) * rib()
    return part


def screw() -> Part:
    return Cylinder(2.0, M4_L) + Pos(0, 0, M4_L / 2 + 1.2) * Cylinder(3.5, 2.4)


show(bracket(), "bracket")
x = BASE_L / 2 - HOLE_INSET
ref(Pos(x, BASE_W / 2 - HOLE_INSET, BASE_T - M4_L / 2 + 0.01) * screw(), "screw")
check_clear("bracket", "screw")   # the M4 screw passes through the hole without touching
