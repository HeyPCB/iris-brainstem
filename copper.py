"""Iris Brainstem's power copper, measured from the routed board, rated by IPC-2221.

Adapted from boards/quack_core/copper.py (same method, this board's nets and links).

Run under KiCad's Python (it carries pcbnew), after the pipeline:

    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 \
        boards/iris_brainstem/copper.py [build/iris_brainstem/iris_brainstem.kicad_pcb]

It writes build/iris_brainstem/copper.json. For every net on the board's current path it lists the
routed tracks by layer (width, length), the vias, and the poured copper per layer (plan pours and
board.zones together), then rates the
narrowest track with heypcb_kicad.route.ipc2221_amps (I = k x dT^0.44 x A^0.725, A in mil^2,
k = 0.048 outer / 0.024 inner) for 1 oz and 2 oz outer copper and 0.5 oz / 1 oz inner copper, at
a 10 C and a 20 C rise. IPC-2221 is a steady-state rule for long conductors; for the
millimetre-long links between neighbouring power pads it is conservative, and the README says
which links those are. Nothing here is a measured temperature.
"""
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

import pcbnew  # noqa: E402

from heypcb_kicad.route import ipc2221_amps  # noqa: E402

PATH_NETS = ["VBUS_PD", "VIN_F", "VIN", "SW_6V0", "6V0_SERVO", "GND", "SW_5V2", "5V2", "5V_SYS", "VBUS_DATA"]
MM = pcbnew.ToMM


def rate(width_mm):
    out = {}
    for name, oz, ext in (("outer_1oz", 1.0, True), ("outer_2oz", 2.0, True), ("inner_0.5oz", 0.5, False),
                          ("inner_1oz", 1.0, False)):
        out[name] = {"10C": round(ipc2221_amps(width_mm, oz, ext, 10.0), 1),
                     "20C": round(ipc2221_amps(width_mm, oz, ext, 20.0), 1)}
    return out


def main(pcb_path):
    brd = pcbnew.LoadBoard(str(pcb_path))
    tracks = defaultdict(list)
    vias = defaultdict(list)
    for t in brd.GetTracks():
        net = t.GetNetname()
        if net not in PATH_NETS:
            continue
        if t.GetClass() == "PCB_VIA":
            vias[net].append({"at": [round(MM(t.GetPosition().x) - 100, 2), round(MM(t.GetPosition().y) - 100, 2)],
                              "d": round(MM(t.GetWidth(pcbnew.F_Cu)), 2), "drill": round(MM(t.GetDrillValue()), 2)})
        else:
            tracks[net].append({"layer": brd.GetLayerName(t.GetLayer()), "w": round(MM(t.GetWidth()), 3),
                                "len": MM(t.GetLength()),
                                "a": [round(MM(t.GetStart().x) - 100, 2), round(MM(t.GetStart().y) - 100, 2)],
                                "b": [round(MM(t.GetEnd().x) - 100, 2), round(MM(t.GetEnd().y) - 100, 2)]})
    zones = defaultdict(dict)
    for z in brd.Zones():
        if z.GetIsRuleArea():
            continue
        net = z.GetNetname()
        if net not in PATH_NETS:
            continue
        for lid in z.GetLayerSet().Seq():
            poly = z.GetFilledPolysList(lid)
            area = poly.Area() / 1e12 if poly is not None else 0.0
            row = zones[net].setdefault(brd.GetLayerName(lid), {"filled_mm2": 0.0, "islands": 0, "zones": 0})
            # board.zones put several zones of one net on a layer: sum them (they may overlap a plan pour)
            row["filled_mm2"] = round(row["filled_mm2"] + area, 1)
            row["islands"] += poly.OutlineCount() if poly is not None else 0
            row["zones"] += 1
    pads = defaultdict(list)
    for fp in brd.GetFootprints():
        for p in fp.Pads():
            if p.GetNetname() in PATH_NETS:
                pads[p.GetNetname()].append("%s.%s" % (fp.GetReference(), p.GetNumber()))
    report = {"board": os.path.relpath(pcb_path, REPO), "rule": "IPC-2221 via heypcb_kicad.route.ipc2221_amps", "nets": {}}
    for net in PATH_NETS:
        ts = tracks.get(net, [])
        by_layer = defaultdict(lambda: {"length_mm": 0.0, "min_w": None, "max_w": None, "segments": 0})
        for t in ts:
            row = by_layer[t["layer"]]
            row["length_mm"] += t["len"]
            row["segments"] += 1
            row["min_w"] = t["w"] if row["min_w"] is None else min(row["min_w"], t["w"])
            row["max_w"] = t["w"] if row["max_w"] is None else max(row["max_w"], t["w"])
        for row in by_layer.values():
            row["length_mm"] = round(row["length_mm"], 2)
        widths = sorted({t["w"] for t in ts})
        entry = {"pads": sorted(set(pads.get(net, []))), "tracks_by_layer": dict(by_layer), "widths_mm": widths,
                 "vias": len(vias.get(net, [])), "via_sizes": sorted({(v["d"], v["drill"]) for v in vias.get(net, [])}),
                 "pour": zones.get(net, {})}
        if widths:
            wmin = widths[0]
            narrow = [t for t in ts if t["w"] == wmin]
            entry["narrowest"] = {"width_mm": wmin, "segments": len(narrow),
                                  "length_mm": round(sum(t["len"] for t in narrow), 2),
                                  "layers": sorted({t["layer"] for t in narrow}),
                                  "longest_segment_mm": round(max(t["len"] for t in narrow), 2),
                                  "ipc2221_amps": rate(wmin)}
            wide = widths[-1]
            entry["widest_track"] = {"width_mm": wide, "ipc2221_amps": rate(wide)}
        report["nets"][net] = entry
    export_geometry(brd, Path(pcb_path).parent / "copper_geom.json")
    out = Path(pcb_path).parent / "copper.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2))
    os.replace(str(tmp), str(out))
    for net, e in report["nets"].items():
        n = e.get("narrowest") or {}
        print("%-9s widths %-28s vias %3d  narrowest %s mm x %s mm  1oz/10C %s A  pour %s" % (
            net, e["widths_mm"][:6], e["vias"], n.get("width_mm"), n.get("length_mm"),
            (n.get("ipc2221_amps") or {}).get("outer_1oz", {}).get("10C"),
            {k: v["filled_mm2"] for k, v in e["pour"].items()}))
    print("wrote", out)
    sys.stdout.flush()


# the current path, link by link: (net, source pads, sink pads). copper_cut.py (in the robots CAD
# environment, it needs scipy) finds the narrowest cross-section of each link over every layer.
LINKS = [
    ("VBUS_PD", ["J1.A4", "J1.A9", "J1.B4", "J1.B9"], ["F1.1"]),
    ("VIN_F", ["F1.2"], ["R1.1"]),
    ("VIN", ["R1.2"], ["U3.3", "C7.1", "C8.1", "U4.3", "C14.1", "C15.1"]),
    ("VIN", ["R1.2"], ["U3.3", "C7.1", "C8.1"]),
    ("SW_6V0", ["U3.2"], ["L1.1"]),
    ("6V0_SERVO", ["L1.2"], ["J%d.2" % k for k in range(10, 20)]),
    ("6V0_SERVO", ["L1.2"], ["J%d.2" % k for k in range(10, 15)]),
    ("6V0_SERVO", ["L1.2"], ["J%d.2" % k for k in range(15, 20)]),
    ("6V0_SERVO", ["L1.2", "C11.1", "C12.1", "C13.1", "C1.1"], ["J%d.2" % k for k in range(15, 20)]),
    ("6V0_SERVO", ["L1.2", "C11.1", "C12.1", "C13.1", "C1.1"], ["J%d.2" % k for k in range(10, 15)]),
    ("6V0_SERVO", ["J%d.2" % k for k in range(10, 15)], ["J%d.2" % k for k in range(15, 20)]),
    ("GND", ["J%d.3" % k for k in range(10, 20)], ["U3.1", "C11.2", "C12.2", "C13.2"]),
    ("GND", ["U3.1", "C7.2", "C8.2"], ["J1.A1", "J1.A12", "J1.B1", "J1.B12"]),
    ("5V2", ["L2.2"], ["D2.2"]),
    ("5V_SYS", ["D2.1", "D3.1"], ["J5.1", "J5.2"]),
]


def _rings(polyset):
    out = []
    polyset.Simplify()
    for oi in range(polyset.OutlineCount()):
        ring = polyset.Outline(oi)
        holes = [polyset.Hole(oi, hi) for hi in range(polyset.HoleCount(oi))]
        conv = lambda r: [[round(MM(r.CPoint(k).x) - 100, 4), round(MM(r.CPoint(k).y) - 100, 4)] for k in range(r.PointCount())]
        out.append({"outline": conv(ring), "holes": [conv(h) for h in holes]})
    return out


def export_geometry(brd, out_path):
    """Every copper shape of each link's net, per layer, as polygons (mm from the board's corner),
    plus the plated holes that join the layers, and the terminal pads of each link."""
    nets = sorted({l[0] for l in LINKS})
    layers = [brd.GetLayerName(l) for l in brd.GetEnabledLayers().CuStack()]
    geo = {"layers": layers, "nets": {}, "links": [{"net": n, "src": a, "dst": b} for n, a, b in LINKS]}
    err = pcbnew.FromMM(0.005)
    for net in nets:
        per = {}
        holes = []
        terminals = {}
        for ln in layers:
            lid = brd.GetLayerID(ln)
            ps = pcbnew.SHAPE_POLY_SET()
            for z in brd.Zones():
                if not z.GetIsRuleArea() and z.GetNetname() == net and z.IsOnLayer(lid):
                    ps.BooleanAdd(z.GetFilledPolysList(lid))
            for t in brd.GetTracks():
                if t.GetNetname() == net and t.IsOnLayer(lid):
                    t.TransformShapeToPolygon(ps, lid, 0, err, pcbnew.ERROR_INSIDE)
            for fp in brd.GetFootprints():
                for pad in fp.Pads():
                    if pad.GetNetname() == net and pad.IsOnLayer(lid):
                        pad.TransformShapeToPolygon(ps, lid, 0, err, pcbnew.ERROR_INSIDE)
            per[ln] = _rings(ps)
        for t in brd.GetTracks():
            if t.GetClass() == "PCB_VIA" and t.GetNetname() == net:
                holes.append({"at": [MM(t.GetPosition().x) - 100, MM(t.GetPosition().y) - 100], "drill": MM(t.GetDrillValue()),
                              "kind": "via"})
        for fp in brd.GetFootprints():
            for pad in fp.Pads():
                key = "%s.%s" % (fp.GetReference(), pad.GetNumber())
                if pad.GetNetname() == net:
                    if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
                        holes.append({"at": [MM(pad.GetPosition().x) - 100, MM(pad.GetPosition().y) - 100],
                                      "drill": MM(min(pad.GetDrillSize().x, pad.GetDrillSize().y)), "kind": "pad " + key})
                    ps = pcbnew.SHAPE_POLY_SET()
                    pad.TransformShapeToPolygon(ps, pad.GetLayerSet().CuStack()[0], 0, err, pcbnew.ERROR_INSIDE)
                    terminals.setdefault(key, []).append({"layers": [brd.GetLayerName(l) for l in pad.GetLayerSet().CuStack()],
                                                          "poly": _rings(ps)})
        geo["nets"][net] = {"copper": per, "plated_holes": holes, "terminals": terminals}
    tmp = Path(str(out_path) + ".tmp")
    tmp.write_text(json.dumps(geo))
    os.replace(str(tmp), str(out_path))
    return out_path


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else REPO / "build" / "iris_brainstem" / "iris_brainstem.kicad_pcb")
    os._exit(0)
