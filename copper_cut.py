"""The narrowest copper cross-section of each link on Iris Brainstem's current path, over every layer.

Adapted from boards/quack_core/copper_cut.py (same method; the links are listed in copper.py).

copper.py (KiCad's Python) writes build/iris_brainstem/copper_geom.json: each power net's copper as
polygons per layer (tracks, pads and poured fill), the plated holes that join the layers, and the
pads where each link starts and ends. This script (robots CAD environment, it needs scipy)
rasterises that copper on a 0.1 mm grid and runs a max-flow / min-cut from the link's source pads
to its sink pads. The cut is the narrowest cross-section the link's whole current has to pass,
whatever mix of tracks, pours and vias the router made.

Model:
  * 8-neighbour grid with Cauchy-Crofton weights (orthogonal delta*pi/8, diagonal delta*pi/(8*sqrt 2)),
    so a cut across a band of copper costs its Euclidean width;
  * each layer's width is weighted by its copper thickness, so the cut minimises cross-section:
    35 um outer, 17.5 um inner (JLCPCB's default 4-layer build: 1 oz outer, 0.5 oz inner);
  * a plated hole joins the layers through its barrel: pi x drill of 25 um plating, expressed as
    an equivalent width of 35 um copper, per layer crossing;
  * terminal pads are infinite sources and sinks.
The rating is IPC-2221 (heypcb_kicad.route.ipc2221_amps) per layer of the cut, summed: outer
layers external, inner layers internal, barrels as external copper of their equivalent width.

Each link is cut twice. The first cut is the true narrowest section, which on short links is the
copper touching a part's own pads (a SOT-23-6 pin, the receptacle's 0.6 mm VBUS pads).
The second ("beyond_pads") counts the copper within 1 mm of each terminal pad as part of the pad,
so it rates the board's own copper between the parts; when two terminals are within 2 mm of each
other there is no copper between them to rate, and the row says so. All cuts run in parallel
processes (the GND net covers most of the board on three layers).

    python boards/iris_brainstem/copper_cut.py   # a Python with numpy and scipy
"""
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
from matplotlib.path import Path as MPath
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import maximum_flow, breadth_first_order

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def _load_ipc2221():
    """heypcb_kicad.route.ipc2221_amps itself, taken from route.py's source: the module imports pcbnew,
    which this environment does not have."""
    import ast
    src = (REPO / "heypcb_kicad" / "route.py").read_text()
    fn = [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "ipc2221_amps"][0]
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "route.py", "exec"), ns)
    return ns["ipc2221_amps"]


ipc2221_amps = _load_ipc2221()

DELTA = 0.1          # mm raster
UNIT = 1e-4          # capacity unit, mm of 35 um copper
T_OUT, T_IN, T_BARREL = 35.0, 17.5, 25.0
INF = 2 ** 30


def raster(rings, xs, ys):
    """Boolean mask of the union of polygons-with-holes over the grid centres."""
    gx, gy = np.meshgrid(xs, ys)
    pts = np.c_[gx.ravel(), gy.ravel()]
    m = np.zeros(len(pts), bool)
    for r in rings:
        o = np.array(r["outline"])
        if len(o) < 3:
            continue
        lo, hi = o.min(axis=0), o.max(axis=0)
        sel = np.all((pts >= lo) & (pts <= hi), axis=1)
        if not sel.any():
            continue
        inside = MPath(o).contains_points(pts[sel])
        for h in r["holes"]:
            h = np.array(h)
            if len(h) >= 3:
                inside &= ~MPath(h).contains_points(pts[sel])
        m[np.where(sel)[0][inside]] = True
    return m.reshape(gy.shape)


def cut(net_geo, layers, src, dst, grow=0.0):
    allxy = [p for ln in layers for r in net_geo["copper"][ln] for p in r["outline"]]
    a = np.array(allxy)
    x0, y0 = a.min(axis=0) - 0.5
    x1, y1 = a.max(axis=0) + 0.5
    xs = np.arange(x0, x1, DELTA) + DELTA / 2
    ys = np.arange(y0, y1, DELTA) + DELTA / 2
    H, W = len(ys), len(xs)
    thick = {ln: (T_OUT if ln in ("F.Cu", "B.Cu") else T_IN) for ln in layers}
    masks = {ln: raster(net_geo["copper"][ln], xs, ys) for ln in layers}
    index = {}
    n = 0
    for ln in layers:
        idx = np.full((H, W), -1, np.int64)
        k = masks[ln].sum()
        idx[masks[ln]] = np.arange(n, n + k)
        index[ln] = idx
        n += k
    rows, cols, caps, tags = [], [], [], []
    w_o = DELTA * math.pi / 8
    w_d = DELTA * math.pi / (8 * math.sqrt(2))
    for li, ln in enumerate(layers):
        idx = index[ln]
        scale = thick[ln] / T_OUT
        for dy, dx, w in ((0, 1, w_o), (1, 0, w_o), (1, 1, w_d), (1, -1, w_d)):
            ya, yb = slice(0, H - dy), slice(dy, H)
            xa, xb = (slice(0, W - dx), slice(dx, W)) if dx >= 0 else (slice(-dx, W), slice(0, W + dx))
            a_, b_ = idx[ya, xa], idx[yb, xb]
            ok = (a_ >= 0) & (b_ >= 0)
            c = int(round(w * scale / UNIT))
            rows.append(a_[ok]); cols.append(b_[ok]); caps.append(np.full(ok.sum(), c)); tags.append(np.full(ok.sum(), li))
    # plated holes: a hub per hole, joined to each layer's copper inside the barrel's ring
    hub_tag = len(layers)
    for h in net_geo["plated_holes"]:
        cx, cy = h["at"]
        rr = h["drill"] / 2 + 0.15
        # only the cells around the hole: a window, not the whole grid
        i0, i1 = max(0, int((cy - rr - y0) / DELTA) - 1), min(H, int((cy + rr - y0) / DELTA) + 2)
        j0, j1 = max(0, int((cx - rr - x0) / DELTA) - 1), min(W, int((cx + rr - x0) / DELTA) + 2)
        wy, wx = np.meshgrid(ys[i0:i1], xs[j0:j1], indexing="ij")
        ring = (wx - cx) ** 2 + (wy - cy) ** 2 <= rr ** 2
        barrel = math.pi * h["drill"] * T_BARREL / T_OUT
        hub = n
        n += 1
        for ln in layers:
            cells = index[ln][i0:i1, j0:j1][ring & masks[ln][i0:i1, j0:j1]]
            cells = cells[cells >= 0]
            if not len(cells):
                continue
            node = n
            n += 1
            rows.append(cells); cols.append(np.full(len(cells), node)); caps.append(np.full(len(cells), INF)); tags.append(np.full(len(cells), -1))
            rows.append(np.array([node])); cols.append(np.array([hub])); caps.append(np.array([int(round(barrel / UNIT))])); tags.append(np.array([hub_tag]))
    S, T = n, n + 1
    n += 2
    used = {"src": [], "dst": []}
    if grow > 0:
        from scipy.ndimage import binary_dilation
        k = int(round(grow / DELTA))
        yy, xx = np.mgrid[-k:k + 1, -k:k + 1]
        disk = (xx * xx + yy * yy) <= k * k
    side_cells = {"src": set(), "dst": set()}
    for key, term, side in ((src, S, "src"), (dst, T, "dst")):
        for pad in key:
            for t in net_geo["terminals"].get(pad, []):
                pm = raster(t["poly"], xs, ys)
                if grow > 0:
                    pm = binary_dilation(pm, structure=disk)
                for ln in t["layers"]:
                    if ln not in index:
                        continue
                    cells = index[ln][pm & masks[ln]]
                    cells = cells[cells >= 0]
                    if not len(cells):
                        continue
                    used[side].append(pad)
                    side_cells[side].update(cells.tolist())
                    if side == "src":
                        rows.append(np.full(len(cells), S)); cols.append(cells)
                    else:
                        rows.append(cells); cols.append(np.full(len(cells), T))
                    caps.append(np.full(len(cells), INF)); tags.append(np.full(len(cells), -1))
    if side_cells["src"] & side_cells["dst"]:
        return None, {}, sorted(set(used["src"])), sorted(set(used["dst"]))
    r = np.concatenate(rows); c = np.concatenate(cols); cap = np.concatenate(caps).astype(np.int64)
    tg = np.concatenate(tags)
    # undirected: both directions carry the capacity (terminal and hub links too)
    R = np.concatenate([r, c]); C = np.concatenate([c, r]); K = np.concatenate([cap, cap])
    # sum duplicate edges in int64 and only then clip to INF: two terminal pads at one spot (a USB-C
    # receptacle's A4 and B9) each add an INF edge, and summed in int32 they wrapped negative, which
    # read as a flow of 0 (found on this board, 2026-10-03; quack_core's copy has the same line)
    G = coo_matrix((K.astype(np.int64), (R, C)), shape=(n, n)).tocsr()
    G.sum_duplicates()
    G.data = np.minimum(G.data, INF)
    G = G.astype(np.int32)
    res = maximum_flow(G, S, T)
    flow = res.flow.tocsr()
    resid = G - flow
    resid.data = np.maximum(resid.data, 0)
    resid.eliminate_zeros()
    reach = np.zeros(n, bool)
    reach[breadth_first_order(resid, S, directed=True, return_predecessors=False)] = True
    per = {}
    crossing = reach[r] & ~reach[c] | reach[c] & ~reach[r]
    for li in np.unique(tg[crossing]):
        if li < 0:
            continue
        w = cap[crossing & (tg == li)].sum() * UNIT
        name = layers[li] if li < len(layers) else "barrels"
        per[name] = round(float(w), 2)
    return res.flow_value * UNIT, per, sorted(set(used["src"])), sorted(set(used["dst"]))


def amps(per, rise):
    tot = 0.0
    for ln, w in per.items():
        if ln == "barrels":
            tot += ipc2221_amps(w, 1.0, True, rise)          # already 35 um-equivalent width
        elif ln in ("F.Cu", "B.Cu"):
            tot += ipc2221_amps(w, 1.0, True, rise)
        else:
            tot += ipc2221_amps(w * T_OUT / T_IN, 0.5, False, rise)   # undo the weighting: real width, 0.5 oz inner
    return tot


def rate(per):
    return {"ipc2221_amps_10C": round(amps(per, 10.0), 1), "ipc2221_amps_20C": round(amps(per, 20.0), 1),
            "ipc2221_amps_10C_2oz_outer": round(sum((ipc2221_amps(w, 2.0, True, 10.0) if ln in ("F.Cu", "B.Cu") else
                                                    ipc2221_amps(w * T_OUT / T_IN, 0.5, False, 10.0) if ln != "barrels" else
                                                    ipc2221_amps(w, 1.0, True, 10.0)) for ln, w in per.items()), 1)}


def _job(args):
    return cut(*args[:4], grow=args[4])


GROW = 1.0   # mm: the "beyond the pads" cut treats the copper within this distance of a terminal pad as the pad


def main():
    geo_path = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "build" / "iris_brainstem" / "copper_geom.json"
    geo = json.loads(geo_path.read_text())
    out = {"method": __doc__.split("\n\n")[1].strip(), "raster_mm": DELTA, "copper_um": {"outer": T_OUT, "inner": T_IN, "barrel_plating": T_BARREL},
           "beyond_pads_mm": GROW, "links": []}
    from concurrent.futures import ProcessPoolExecutor
    jobs = [(geo["nets"][l["net"]], geo["layers"], l["src"], l["dst"], g) for l in geo["links"] for g in (0.0, GROW)]
    with ProcessPoolExecutor(max_workers=min(len(jobs), max(1, (os.cpu_count() or 2) - 1))) as ex:
        results = list(ex.map(_job, jobs))
    for k, link in enumerate(geo["links"]):
        net = link["net"]
        total, per, s_used, d_used = results[2 * k]
        row = {"net": net, "from": link["src"], "to": link["dst"], "terminals_found": {"from": s_used, "to": d_used},
               "cut_mm_35um_equivalent": round(total, 2), "cut_by_layer_mm": per}
        row.update(rate(per))
        # the same link with the copper within GROW mm of each terminal pad counted as the pad: the board's
        # own copper between the parts, without the pads' edges (a part's land is fixed by its footprint)
        g_total, g_per, _, _ = results[2 * k + 1]
        if g_total is None:
            row["beyond_pads"] = {"note": "the terminals lie within %.1f mm of each other: the link is the pads' own gap" % (2 * GROW)}
        else:
            row["beyond_pads"] = dict({"cut_mm_35um_equivalent": round(g_total, 2), "cut_by_layer_mm": g_per}, **rate(g_per))
        out["links"].append(row)
        bp = row["beyond_pads"]
        print("%-7s %-24s -> %-26s cut %6.2f mm %5.1f A (10C) %5.1f A (20C) | beyond pads %s" % (
            net, ",".join(link["src"])[:24], ",".join(link["dst"])[:26], total, row["ipc2221_amps_10C"],
            row["ipc2221_amps_20C"],
            ("%.2f mm %.1f A / %.1f A" % (bp["cut_mm_35um_equivalent"], bp["ipc2221_amps_10C"], bp["ipc2221_amps_20C"]))
            if "cut_mm_35um_equivalent" in bp else "pads adjacent"), flush=True)
        print("        by layer %s | beyond %s" % (per, bp.get("cut_by_layer_mm")), flush=True)
    dst = geo_path.parent / "copper_cut.json"
    tmp = dst.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, dst)
    print("wrote", dst)


if __name__ == "__main__":
    main()
