import pytest
import shapely
from shapely.geometry import LineString, Point

from pcb_weaver.routing_clearance import pad_envelope, blocked_indices


def test_roundrect_flat_has_exact_native_width():
    pad = {"size":[1.6,.3],"shape":"roundrect","rotation":0,"x":0,"y":0,"roundrect_rratio":.25}
    shape = pad_envelope(pad)
    assert shape.bounds == pytest.approx([-.8,-.15,.8,.15])
    tree = shapely.STRtree([shape])
    points = shapely.points([[0,.5],[0,.49999]])
    assert blocked_indices(tree, [0], points, .2+.15).tolist() == [1]


def test_capsule_and_via_clearance_uses_exact_radii():
    tree = shapely.STRtree([LineString([(0,0),(2,0)]), Point(4,0)])
    probes = shapely.points([[1,.5],[1,.4999],[4,.75],[4,.7499]])
    assert blocked_indices(tree, [.15,.4], probes, .35).tolist() == [1,3]


def test_edge_interior_is_checked_not_only_vertices():
    tree = shapely.STRtree([Point(1,0)])
    lines = shapely.linestrings([[[0,-1],[2,1]], [[0,1],[2,1]]])
    assert blocked_indices(tree, [.2], lines, .3).tolist() == [0]
