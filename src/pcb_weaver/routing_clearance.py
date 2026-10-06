"""Continuous copper distances without inflating straight trace or pad flats."""
import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import box

from .clearance import pad_shape


def pad_envelope(pad):
    sx, sy = pad["size"]
    bounds = affinity.translate(affinity.rotate(box(-sx/2, -sy/2, sx/2, sy/2),
        -pad["rotation"], origin=(0,0)), pad["x"], pad["y"])
    # Circumscribed rounded corners remain conservative; the flat sides are exact.
    return pad_shape(pad).intersection(bounds)


def blocked_indices(tree, radii, geometries, required):
    if not len(radii) or not len(geometries):
        return np.array([], dtype=np.int64)
    radii = np.asarray(radii)
    a, b = tree.query(geometries, predicate="dwithin", distance=required + radii.max())
    distances = shapely.distance(np.asarray(geometries)[a], tree.geometries[b])
    # Only floating-point noise is tolerated (1e-9 mm); native KiCad is authoritative.
    return np.unique(a[distances < required + radii[b] - 1e-9])
