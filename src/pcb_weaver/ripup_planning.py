"""Layer-aware bounded rip-up scopes; proposals require independent native checks."""
import math
from shapely.geometry import Point, LineString

from .board import read_board, _locked
from .repair_geometry import _read, _board, _inside
from .routing_clearance import pad_envelope
from .storage import digest


def plans(source, findings, *, maximum_area=2500, edge_clearance=.5):
    if any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0
           for v in (maximum_area,edge_clearance)):
        raise ValueError("Finite positive area and edge clearance required")
    model = read_board(source)
    copper = _board(_read(source)[0], source=True)["copper"]
    ids = {i["uuid"] for f in findings for i in f["items"]}
    targets = [p for f in model["footprints"] for p in f["pads"]
               if p["uuid"] in ids and "*.Cu" not in p["layers"]]
    seen = set()
    for pad in targets:
        shape = pad_envelope(pad)
        neighbors = {}
        for c in copper:
            g = c["geometry"]
            if not g["net"] or g["net"] == pad["net"] or _locked(c["node"]):
                continue
            span = {g["layer"]} if g["kind"] == "segment" else set(model["copper_layers"])
            if not span.intersection(pad["layers"]):
                continue
            core = Point(g["at"]) if g["kind"] == "via" else LineString([g["start"],g["end"]])
            distance = core.distance(shape)-g.get("size",g.get("width",0))/2
            if distance < 1:
                neighbors[g["net"]] = min(neighbors.get(g["net"],float("inf")),distance)
        ordered = sorted(neighbors, key=lambda n:(neighbors[n],n))
        selections = [[net] for net in ordered[:3]] + [ordered[:count] for count in (2,4,7)]
        for selected in selections:
            nets = [pad["net"], *selected]
            for margin in (2,4):
                b, outline = shape.bounds, model["outline"]["bounds"]
                region = [max(outline[0]+edge_clearance,b[0]-margin),max(outline[1]+edge_clearance,b[1]-margin),
                          min(outline[2]-edge_clearance,b[2]+margin),min(outline[3]-edge_clearance,b[3]+margin)]
                if (region[2]-region[0])*(region[3]-region[1]) > maximum_area:
                    continue
                removed = [c["id"] for c in copper if c["geometry"]["net"] in nets[1:]
                           and _inside(c["geometry"]["bounds"],region) and not _locked(c["node"])]
                key = tuple(sorted(removed))
                if not removed or len(removed)>64 or key in seen:
                    continue
                seen.add(key)
                for finding in findings:
                    if any(i["uuid"] == pad["uuid"] for i in finding["items"]):
                        for item in finding["items"]:
                            for axis,i in (("x",0),("y",1)):
                                region[i] = max(outline[i]+edge_clearance,min(region[i],item["pos"][axis]-3))
                                region[i+2] = min(outline[i+2]-edge_clearance,max(region[i+2],item["pos"][axis]+3))
                if (region[2]-region[0])*(region[3]-region[1]) > maximum_area:
                    continue
                plan = {"nets":nets,"region":region,"remove_ids":removed,"passes":3,"ripup_extent":"escape",
                       "target_pad":pad["uuid"],"target_net":pad["net"],
                       "source_sha256":digest(source),"manufacturing_authorized":False}
                yield plan
                expanded = [c["id"] for c in copper if c["geometry"]["net"] in nets[1:]
                            and _inside(c["geometry"]["bounds"],region) and not _locked(c["node"])]
                expanded_key = tuple(sorted(expanded))
                if len(removed) < len(expanded) <= 64 and expanded_key not in seen:
                    seen.add(expanded_key)
                    yield {**plan,"remove_ids":expanded,"ripup_extent":"routing_region"}
