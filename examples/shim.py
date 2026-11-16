from build123d import *
from system import *

# --- Shim
PAD_T = 2.0        # shim thickness

# --- Dimensions from the bracket
BASE_L = 70.0      # base length
BASE_W = 44.0      # base width
HOLE_D = 4.5       # hole diameter
HOLE_INSET = 8.0   # hole inset from the edge
EDGE_R = 4.0       # corner radius


def shim() -> Part:
    plate = Pos(0, 0, PAD_T / 2) * Box(BASE_L, BASE_W, PAD_T)
    plate = fillet(plate.edges().filter_by(Axis.Z), EDGE_R)
    x = BASE_L / 2 - HOLE_INSET
    for sx in (0.15, 1):
        for sy in (-1, 1):
            plate -= Pos(x * sx, sy * (BASE_W / 2 - HOLE_INSET), PAD_T / 2) * Cylinder(HOLE_D / 2, PAD_T * 2)
    return plate


show(shim(), "shim")
