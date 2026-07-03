# SPDX-License-Identifier: MIT
"""Roblox World Exporter — Blender add-on.

Exports large Blender worlds to Roblox by slicing them into an even grid of
tiles so every piece fits inside Roblox's 2048x2048x2048-stud MeshPart limit
(and under the ~20k-triangle-per-mesh import limit).

Two export modes:

* **Single FBX (default)** — every sliced piece keeps its world position in
  ONE .fbx file. Import it with Roblox Studio's 3D Importer (keep "Import as
  Single Asset" / scene-position settings on) and the whole map appears
  assembled immediately — no asset-id pasting.
* **Per-piece FBX + .rbxmx scaffold (advanced)** — one FBX per piece plus an
  .rbxmx placing invisible MeshPart placeholders (MeshId cannot embed
  geometry, so ids must be pasted after upload) and a generated
  ``place_pieces.lua`` command-bar script that snaps imported pieces into
  place automatically.

Coordinate mapping (right-handed both sides):
  Blender (x, y, z) [Z-up, meters]  ->  Roblox (x, z, -y) [Y-up, studs]
  studs = meters / meters_per_stud            (default 0.28 m per stud)

The pure-math helpers in the first half of this file deliberately avoid any
Blender API at call time so they can be unit-tested under plain Python.
"""

bl_info = {
    "name": "Roblox World Exporter",
    "author": "Generated with Claude Code",
    "version": (1, 1, 0),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar (N) > Roblox",
    "description": "Slice large worlds into an even grid and export FBX + "
                   "optional .rbxmx for Roblox",
    "doc_url": "",
    "category": "Import-Export",
}

import json
import logging
import math
import os
import re
import shutil
import time
import traceback
from dataclasses import dataclass, field
from itertools import product
from xml.etree import ElementTree as ET

import bpy
import bmesh
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector

# ---------------------------------------------------------------------------
# Constants / logging
# ---------------------------------------------------------------------------

ADDON_NAME = "roblox_world_exporter"
LOG = logging.getLogger(ADDON_NAME)
if not LOG.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(name)s] %(levelname)s: %(message)s"))
    LOG.addHandler(_h)
LOG.setLevel(logging.INFO)

ROBLOX_HARD_MAX_STUDS = 2048.0   # Roblox MeshPart per-axis size limit
ROBLOX_TRI_LIMIT = 20000         # Roblox rejects meshes above this
MAX_TILES_HARD = 4096            # refuse to generate more tiles than this
MAX_TILES_WARN = 512
TRI_SPLIT_MAX_DEPTH = 8          # 2^8 = up to 256 sub-pieces per dense piece

COLLISION_TOKENS = {"DEFAULT": 0, "HULL": 1, "BOX": 2, "PRECISE": 3}
COLLISION_LABELS = {
    "DEFAULT": "Default",
    "HULL": "Hull",
    "BOX": "Box",
    "PRECISE": "PreciseConvexDecomposition",
}

# Medium stone grey (163, 162, 165) encoded as Roblox Color3uint8.
_PLACEHOLDER_COLOR3UINT8 = (0xFF << 24) | (163 << 16) | (162 << 8) | 165

_EPS = 1e-9


class ExportError(RuntimeError):
    """Raised for user-facing, expected export failures."""


# ---------------------------------------------------------------------------
# Pure helpers (no Blender API at call time — unit-testable)
# ---------------------------------------------------------------------------


def sanitize_name(name):
    """Make a string safe for filenames and Roblox instance names."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", (name or "").strip()).strip("_")
    return cleaned or "Unnamed"


def fmt_float(v):
    """Compact float formatting for XML/JSON/Lua output."""
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def grid_divisions(extent_studs, max_tile_studs):
    """Minimum number of EVEN divisions so each tile <= max_tile_studs."""
    if extent_studs <= _EPS or max_tile_studs <= _EPS:
        return 1
    return max(1, math.ceil(extent_studs / max_tile_studs - _EPS))


@dataclass
class GridSpec:
    """Even slicing grid in Blender world space (meters, Blender axes)."""

    bounds_min: tuple  # (x, y, z) meters
    bounds_max: tuple
    divisions: tuple   # (nx, ny, nz) — ints, >= 1
    tile_size: tuple   # meters per tile per axis (extent / divisions)

    @property
    def tile_count(self):
        nx, ny, nz = self.divisions
        return nx * ny * nz


def compute_grid(bounds_min, bounds_max, meters_per_stud,
                 max_tile_studs, safety_margin_studs,
                 slice_axes=(True, True, True)):
    """Compute the even grid for the given world bounds.

    ``safety_margin_studs`` shrinks the effective tile limit (e.g. 2048 - 48
    = 2000) so pieces never brush against Roblox's hard cap after float
    round-off. ``slice_axes`` toggles slicing per Blender axis (x, y, z).
    """
    if meters_per_stud <= _EPS:
        raise ValueError("meters_per_stud must be > 0")
    effective = max(1.0, max_tile_studs - max(0.0, safety_margin_studs))
    divisions = []
    tile_size = []
    for axis in range(3):
        extent_m = max(0.0, bounds_max[axis] - bounds_min[axis])
        extent_studs = extent_m / meters_per_stud
        n = grid_divisions(extent_studs, effective) if slice_axes[axis] else 1
        divisions.append(n)
        tile_size.append(extent_m / n if n else extent_m)
    return GridSpec(tuple(bounds_min), tuple(bounds_max),
                    tuple(divisions), tuple(tile_size))


def compute_origin_offset(bounds_min, bounds_max, mode):
    """Blender-space offset subtracted from all output positions.

    'WORLD' keeps Blender's origin. 'CENTER' puts the map's footprint center
    at Roblox (0, y, 0) with the lowest point at ground level (Roblox Y=0).
    """
    if mode == 'CENTER':
        return ((bounds_min[0] + bounds_max[0]) * 0.5,
                (bounds_min[1] + bounds_max[1]) * 0.5,
                bounds_min[2])
    return (0.0, 0.0, 0.0)


def tile_bounds(grid, index):
    """(min, max) corners in meters for grid cell ``index`` = (i, j, k)."""
    lo, hi = [], []
    for axis in range(3):
        i = index[axis]
        a = grid.bounds_min[axis] + grid.tile_size[axis] * i
        # Use the true outer bound on the last tile to dodge float drift.
        b = (grid.bounds_max[axis] if i == grid.divisions[axis] - 1
             else a + grid.tile_size[axis])
        lo.append(a)
        hi.append(b)
    return tuple(lo), tuple(hi)


def tile_index_range(grid, obj_min, obj_max):
    """Inclusive (start, stop) tile indices per axis overlapped by an AABB."""
    ranges = []
    for axis in range(3):
        n = grid.divisions[axis]
        ts = grid.tile_size[axis]
        if n <= 1 or ts <= _EPS:
            ranges.append((0, 0))
            continue
        rel_lo = (obj_min[axis] - grid.bounds_min[axis]) / ts
        rel_hi = (obj_max[axis] - grid.bounds_min[axis]) / ts
        i0 = min(max(int(math.floor(rel_lo + _EPS)), 0), n - 1)
        i1 = min(max(int(math.ceil(rel_hi - _EPS)) - 1, i0), n - 1)
        ranges.append((i0, i1))
    return ranges


def blender_to_roblox_point(p, meters_per_stud):
    """Blender (x, y, z) meters -> Roblox (x, z, -y) studs."""
    s = 1.0 / meters_per_stud
    return (p[0] * s, p[2] * s, -p[1] * s)


def blender_to_roblox_size(size, meters_per_stud):
    """Blender extents (sx, sy, sz) meters -> Roblox (sx, sz, sy) studs."""
    s = 1.0 / meters_per_stud
    return (abs(size[0]) * s, abs(size[2]) * s, abs(size[1]) * s)


def grid_line_coords(grid):
    """Line-segment vertices (flat list of (x, y, z)) visualizing the grid.

    Outer bounding box wireframe + an outline rectangle for every interior
    slicing plane. Consecutive pairs form one segment.
    """
    lo, hi = grid.bounds_min, grid.bounds_max
    coords = []

    def seg(a, b):
        coords.append(tuple(a))
        coords.append(tuple(b))

    corners = list(product((lo[0], hi[0]), (lo[1], hi[1]), (lo[2], hi[2])))
    for a in range(8):
        for b in range(a + 1, 8):
            if sum(1 for axis in range(3)
                   if corners[a][axis] != corners[b][axis]) == 1:
                seg(corners[a], corners[b])

    for axis in range(3):
        b_ax, c_ax = [x for x in range(3) if x != axis]
        for d in range(1, grid.divisions[axis]):
            v = grid.bounds_min[axis] + grid.tile_size[axis] * d
            rect = (
                (lo[b_ax], lo[c_ax]), (hi[b_ax], lo[c_ax]),
                (hi[b_ax], hi[c_ax]), (lo[b_ax], hi[c_ax]),
            )
            for idx in range(4):
                pa, pb = rect[idx], rect[(idx + 1) % 4]
                a3, b3 = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
                a3[axis] = b3[axis] = v
                a3[b_ax], a3[c_ax] = pa
                b3[b_ax], b3[c_ax] = pb
                seg(a3, b3)
    return coords


@dataclass
class PieceInfo:
    """One exported mesh piece (== one MeshPart in Roblox)."""

    name: str
    grid_index: tuple            # (i, j, k) Blender-axis cell
    fbx_rel: str                 # path relative to the output dir
    position_studs: tuple        # Roblox space (x, y, z)
    size_studs: tuple            # Roblox space
    triangles: int
    collision: str               # key into COLLISION_TOKENS
    textures: dict = field(default_factory=dict)  # map key -> rel path

    def collision_token(self):
        return COLLISION_TOKENS.get(self.collision, 0)


# --- .rbxmx writing (scaffold mode) -----------------------------------------

_SURFACE_MAP_PROPS = {
    "color": "ColorMap",
    "metalness": "MetalnessMap",
    "normal": "NormalMap",
    "roughness": "RoughnessMap",
}


def _prop(parent, tag, name, text=None):
    el = ET.SubElement(parent, tag, {"name": name})
    if text is not None:
        el.text = text
    return el


def _vector3(parent, name, v):
    el = _prop(parent, "Vector3", name)
    for axis, val in zip("XYZ", v):
        ET.SubElement(el, axis).text = fmt_float(val)


def _cframe(parent, name, pos):
    el = _prop(parent, "CoordinateFrame", name)
    for axis, val in zip("XYZ", pos):
        ET.SubElement(el, axis).text = fmt_float(val)
    identity = (1, 0, 0, 0, 1, 0, 0, 0, 1)
    for idx, val in enumerate(identity):
        ET.SubElement(el, f"R{idx // 3}{idx % 3}").text = str(val)


def _content(parent, name, url):
    el = _prop(parent, "Content", name)
    ET.SubElement(el, "url").text = url


def build_rbxmx(model_name, pieces, anchored=True):
    """Serialize the placement scaffold as a Roblox XML model string.

    MeshId (and SurfaceAppearance maps) carry ``rbxassetid://0`` placeholders
    — the parts are INVISIBLE until real ids are pasted (see manifest.json /
    place_pieces.lua). Prefer Single FBX mode for one-step visible imports.
    """
    root = ET.Element("roblox", {
        "xmlns:xmime": "http://www.w3.org/2005/05/xmlmime",
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "xsi:noNamespaceSchemaLocation": "http://www.roblox.com/roblox.xsd",
        "version": "4",
    })
    ET.SubElement(root, "External").text = "null"
    ET.SubElement(root, "External").text = "nil"

    referent = [0]

    def next_ref():
        referent[0] += 1
        return f"RBX{referent[0]}"

    model = ET.SubElement(root, "Item",
                          {"class": "Model", "referent": next_ref()})
    model_props = ET.SubElement(model, "Properties")
    _prop(model_props, "string", "Name", sanitize_name(model_name))

    for piece in pieces:
        part = ET.SubElement(model, "Item",
                             {"class": "MeshPart", "referent": next_ref()})
        props = ET.SubElement(part, "Properties")
        _prop(props, "bool", "Anchored", "true" if anchored else "false")
        _cframe(props, "CFrame", piece.position_studs)
        _prop(props, "bool", "CanCollide", "true")
        _prop(props, "token", "CollisionFidelity",
              str(piece.collision_token()))
        _prop(props, "Color3uint8", "Color3uint8",
              str(_PLACEHOLDER_COLOR3UINT8))
        props.append(ET.Comment(
            f" MeshId placeholder — upload {piece.fbx_rel} "
            "and paste its asset id "))
        _content(props, "MeshId", "rbxassetid://0")
        _prop(props, "string", "Name", piece.name)
        _vector3(props, "size", piece.size_studs)

        if piece.textures:
            sa = ET.SubElement(part, "Item", {"class": "SurfaceAppearance",
                                              "referent": next_ref()})
            sa_props = ET.SubElement(sa, "Properties")
            _prop(sa_props, "string", "Name", "SurfaceAppearance")
            for key in sorted(piece.textures):
                prop_name = _SURFACE_MAP_PROPS.get(key)
                rel = piece.textures[key]
                if not prop_name or not rel:
                    continue
                sa_props.append(ET.Comment(
                    f" {prop_name} placeholder — upload {rel} "
                    "and paste its asset id "))
                _content(sa_props, prop_name, "rbxassetid://0")

    try:
        ET.indent(root)  # Python 3.9+
    except AttributeError:  # pragma: no cover - ancient interpreter
        pass
    return ET.tostring(root, encoding="unicode")


def build_place_script(model_name, pieces):
    """Luau command-bar script that snaps imported pieces into place.

    Scaffold-mode companion: after the per-piece FBX files are imported with
    the 3D Importer, running this in Studio's command bar positions every
    MeshPart by name, anchors it, and applies its collision fidelity.
    """
    lines = [
        "-- Generated by Roblox World Exporter for model "
        f"'{sanitize_name(model_name)}'",
        "-- 1. Import the FBX files under meshes/ with Studio's 3D Importer.",
        "-- 2. Select the imported model(s) (or leave nothing selected to "
        "scan Workspace).",
        "-- 3. Paste and run this whole script in the command bar.",
        "local pieces = {",
    ]
    for p in pieces:
        x, y, z = (fmt_float(v) for v in p.position_studs)
        label = COLLISION_LABELS.get(p.collision, "Default")
        lines.append(
            f'\t["{p.name}"] = {{cf = CFrame.new({x}, {y}, {z}), '
            f"collision = Enum.CollisionFidelity.{label}}},")
    lines += [
        "}",
        'local roots = game:GetService("Selection"):Get()',
        "if #roots == 0 then roots = {workspace} end",
        "local placed = 0",
        "for _, root in ipairs(roots) do",
        "\tfor _, inst in ipairs(root:GetDescendants()) do",
        "\t\tlocal info = pieces[inst.Name]",
        '\t\tif info and inst:IsA("MeshPart") then',
        "\t\t\tinst.Anchored = true",
        "\t\t\tinst.CFrame = info.cf",
        "\t\t\tpcall(function()",
        "\t\t\t\tinst.CollisionFidelity = info.collision",
        "\t\t\tend)",
        "\t\t\tpieces[inst.Name] = nil",
        "\t\t\tplaced += 1",
        "\t\tend",
        "\tend",
        "end",
        'print(("[RobloxWorldExporter] placed %d piece(s)"):format(placed))',
        "local missing = {}",
        "for name in pairs(pieces) do table.insert(missing, name) end",
        "if #missing > 0 then",
        '\twarn("[RobloxWorldExporter] not found: " '
        '.. table.concat(missing, ", "))',
        "end",
    ]
    return "\n".join(lines) + "\n"


def build_manifest(settings_dict, grid, meters_per_stud, origin_offset,
                   pieces, combined_fbx=None):
    """JSON-serializable manifest mapping pieces to files and stud placement."""
    if combined_fbx:
        workflow = [
            f"1. In Roblox Studio open the 3D Importer and import "
            f"{combined_fbx} (keep 'Import as Single Asset' and the default "
            "scene-position/scale settings). The whole map imports "
            "assembled — nothing else to do.",
            "2. Optional: bulk-select the MeshParts to set Anchored and "
            "CollisionFidelity (intended values are listed per piece below).",
        ]
    else:
        workflow = [
            "1. In Roblox Studio open the 3D Importer and import every FBX "
            "under meshes/ (keep the importer's default 'Studs' scale unit).",
            "2. Run place_pieces.lua in the command bar to snap every "
            "imported piece to its exact position (or insert the .rbxmx and "
            "paste uploaded MeshIds manually — scaffold parts are INVISIBLE "
            "until ids are pasted).",
            "3. If textures were exported, upload the files under textures/ "
            "and paste their asset ids into each SurfaceAppearance map.",
        ]
    return {
        "generator": "Roblox World Exporter",
        "version": ".".join(str(v) for v in bl_info["version"]),
        "workflow": workflow,
        "coordinate_note": (
            "grid indices [i, j, k] follow Blender axes (x, y-depth, "
            "z-height); positions/sizes are Roblox studs, Y-up"),
        "settings": settings_dict,
        "grid": {
            "divisions_blender_xyz": list(grid.divisions),
            "tile_size_studs_blender_xyz": [
                t / meters_per_stud for t in grid.tile_size],
            "bounds_min_studs_blender_xyz": [
                b / meters_per_stud for b in grid.bounds_min],
            "bounds_max_studs_blender_xyz": [
                b / meters_per_stud for b in grid.bounds_max],
            "origin_offset_studs_roblox_xyz": list(
                blender_to_roblox_point(origin_offset, meters_per_stud)),
            "tile_count": grid.tile_count,
        },
        "combined_fbx": combined_fbx,
        "pieces": [
            {
                "name": p.name,
                "grid": list(p.grid_index),
                "fbx": p.fbx_rel,
                "position_studs": [round(v, 6) for v in p.position_studs],
                "size_studs": [round(v, 6) for v in p.size_studs],
                "triangles": p.triangles,
                "collision_fidelity": COLLISION_LABELS.get(p.collision,
                                                           "Default"),
                "textures": p.textures,
            }
            for p in pieces
        ],
    }


def unique_name(base, used):
    name = base
    n = 1
    while name in used:
        n += 1
        name = f"{base}_{n}"
    used.add(name)
    return name


def piece_grid_suffix(grid, index):
    i, j, k = index
    suffix = f"_x{i}_y{j}"
    if grid.divisions[2] > 1:
        suffix += f"_z{k}"
    return suffix


# ---------------------------------------------------------------------------
# Blender-side helpers
# ---------------------------------------------------------------------------


def gather_export_objects(context, settings):
    """Visible, non-excluded mesh objects (optionally selection only)."""
    if settings.selected_only:
        pool = list(context.selected_objects)
    else:
        pool = list(context.view_layer.objects)
    return [ob for ob in pool
            if ob.type == 'MESH'
            and ob.visible_get()
            and not getattr(ob, "rbx_exclude", False)]


def object_world_bounds(ob, depsgraph):
    """World-space AABB of the evaluated (post-modifier) object."""
    ob_eval = ob.evaluated_get(depsgraph)
    mw = ob_eval.matrix_world
    corners = [mw @ Vector(c) for c in ob_eval.bound_box]
    lo = tuple(min(c[a] for c in corners) for a in range(3))
    hi = tuple(max(c[a] for c in corners) for a in range(3))
    return lo, hi


def scene_world_bounds(objects, depsgraph):
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for ob in objects:
        olo, ohi = object_world_bounds(ob, depsgraph)
        for a in range(3):
            lo[a] = min(lo[a], olo[a])
            hi[a] = max(hi[a], ohi[a])
    if any(math.isinf(v) for v in lo + hi):
        return None
    return tuple(lo), tuple(hi)


def make_world_mesh(ob, depsgraph):
    """Evaluated mesh copy in world space (originals untouched)."""
    ob_eval = ob.evaluated_get(depsgraph)
    try:
        me = bpy.data.meshes.new_from_object(
            ob_eval, preserve_all_data_layers=True, depsgraph=depsgraph)
    except RuntimeError:
        return None
    me.transform(ob_eval.matrix_world)
    return me


def mesh_bounds(me):
    n = len(me.vertices)
    if n == 0:
        return None
    buf = [0.0] * (n * 3)
    me.vertices.foreach_get("co", buf)
    xs, ys, zs = buf[0::3], buf[1::3], buf[2::3]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _bisect(bm, axis, co, sign):
    plane_co = [0.0, 0.0, 0.0]
    plane_no = [0.0, 0.0, 0.0]
    plane_co[axis] = co
    plane_no[axis] = sign
    bmesh.ops.bisect_plane(
        bm,
        geom=bm.verts[:] + bm.edges[:] + bm.faces[:],
        dist=1e-6,
        plane_co=plane_co,
        plane_no=plane_no,
        clear_outer=True,   # removes geometry on the +normal side
        clear_inner=False,
    )


def _cleanup_loose(bm):
    loose_edges = [e for e in bm.edges if not e.link_faces]
    if loose_edges:
        bmesh.ops.delete(bm, geom=loose_edges, context='EDGES')
    loose_verts = [v for v in bm.verts if not v.link_faces]
    if loose_verts:
        bmesh.ops.delete(bm, geom=loose_verts, context='VERTS')


def split_bmesh_recursive(bm, ranges, grid, leaves):
    """Recursively bisect ``bm`` into grid cells; consumes ``bm``.

    Instead of copying the whole mesh once per tile (O(tiles x mesh)), the
    mesh is halved along tile boundaries — each copy shrinks, so large maps
    slice in roughly O(mesh x log tiles). Appends (cell, bm) to ``leaves``
    for every non-empty cell.
    """
    spans = [r[1] - r[0] for r in ranges]
    widest = max(spans)
    if widest == 0:
        _cleanup_loose(bm)
        if bm.faces:
            leaves.append((tuple(r[0] for r in ranges), bm))
        else:
            bm.free()
        return
    axis = spans.index(widest)
    lo, hi = ranges[axis]
    mid = (lo + hi) // 2
    cut = grid.bounds_min[axis] + grid.tile_size[axis] * (mid + 1)

    bm_high = bm.copy()
    _bisect(bm, axis, cut, 1.0)        # low half: drop geometry above cut
    _bisect(bm_high, axis, cut, -1.0)  # high half: drop geometry below cut

    for half, half_range in ((bm, (lo, mid)), (bm_high, (mid + 1, hi))):
        if not half.faces:
            half.free()
            continue
        sub = list(ranges)
        sub[axis] = half_range
        split_bmesh_recursive(half, sub, grid, leaves)


def bmesh_to_new_mesh(bm, name, materials):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    for mat in materials:
        me.materials.append(mat)
    return me


def remap_material_indices(me, index_map):
    if not index_map:
        return
    n = len(me.polygons)
    if n == 0:
        return
    buf = [0] * n
    me.polygons.foreach_get("material_index", buf)
    me.polygons.foreach_set(
        "material_index",
        [index_map[i] if 0 <= i < len(index_map) else 0 for i in buf])


def merge_meshes(name, meshes):
    """Merge temp meshes into one, consolidating material slots.

    The inputs are disposable copies, so remapping their polygon material
    indices in place before appending is safe.
    """
    merged_mats = []
    for me in meshes:
        index_map = []
        for mat in me.materials:
            if mat not in merged_mats:
                merged_mats.append(mat)
            index_map.append(merged_mats.index(mat))
        remap_material_indices(me, index_map)

    bm = bmesh.new()
    try:
        for me in meshes:
            bm.from_mesh(me)  # repeated calls append (merge)
        return bmesh_to_new_mesh(bm, name, merged_mats)
    finally:
        bm.free()


def mesh_triangle_count(me):
    me.calc_loop_triangles()
    return len(me.loop_triangles)


def split_mesh_by_triangles(me, tri_limit, depth=TRI_SPLIT_MAX_DEPTH):
    """Split a too-dense mesh in half (longest axis) until every part fits
    Roblox's per-mesh triangle limit. Returns a list of NEW meshes and frees
    ``me`` when a split happened; returns [me] untouched otherwise."""
    if tri_limit <= 0 or mesh_triangle_count(me) <= tri_limit or depth <= 0:
        return [me]
    b = mesh_bounds(me)
    if b is None:
        return [me]
    extents = [b[1][a] - b[0][a] for a in range(3)]
    axis = extents.index(max(extents))
    if extents[axis] <= 1e-5:  # degenerate: cannot split spatially
        return [me]
    cut = (b[0][axis] + b[1][axis]) * 0.5
    materials = list(me.materials)

    bm_low = bmesh.new()
    bm_low.from_mesh(me)
    bm_high = bm_low.copy()
    _bisect(bm_low, axis, cut, 1.0)
    _bisect(bm_high, axis, cut, -1.0)

    halves = []
    for bm in (bm_low, bm_high):
        _cleanup_loose(bm)
        if bm.faces:
            halves.append(bmesh_to_new_mesh(bm, me.name, materials))
        bm.free()
    if len(halves) < 2:  # split failed to reduce (all faces on one side)
        for h in halves:
            bpy.data.meshes.remove(h)
        return [me]
    bpy.data.meshes.remove(me)
    out = []
    for half in halves:
        out.extend(split_mesh_by_triangles(half, tri_limit, depth - 1))
    return out


def count_nonmanifold_edges(bm):
    return sum(1 for e in bm.edges if len(e.link_faces) > 2)


# --- Texture / material extraction -----------------------------------------

_PBR_SOCKETS = (
    ("Base Color", "color"),
    ("Normal", "normal"),
    ("Roughness", "roughness"),
    ("Metallic", "metalness"),
)


def _upstream_image(socket, depth=0):
    if depth > 8 or not socket.is_linked:
        return None
    node = socket.links[0].from_node
    if node.type == 'TEX_IMAGE':
        return node.image
    for inp in node.inputs:
        img = _upstream_image(inp, depth + 1)
        if img:
            return img
    return None


def find_pbr_images(mat):
    """{map_key: bpy Image} pulled off the material's Principled BSDF."""
    if not mat or not mat.use_nodes or not mat.node_tree:
        return {}
    principled = next(
        (n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if principled is None:
        return {}
    found = {}
    for socket_name, key in _PBR_SOCKETS:
        socket = principled.inputs.get(socket_name)
        if socket is not None:
            img = _upstream_image(socket)
            if img is not None:
                found[key] = img
    return found


def save_image_copy(img, textures_dir, cache, used_names):
    """Copy/save an image into textures_dir; returns rel path or None."""
    if img in cache:
        return cache[img]
    src = bpy.path.abspath(img.filepath) if img.filepath else ""
    ext = os.path.splitext(src)[1].lower() if src else ""
    if ext not in (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".exr", ".tif",
                   ".tiff", ".webp"):
        ext = {"PNG": ".png", "JPEG": ".jpg", "TARGA": ".tga",
               "BMP": ".bmp", "OPEN_EXR": ".exr",
               "TIFF": ".tif"}.get(img.file_format, ".png")
    base = sanitize_name(os.path.splitext(img.name)[0]) or "texture"
    name = base + ext
    n = 1
    while name in used_names:
        n += 1
        name = f"{base}_{n}{ext}"
    dst = os.path.join(textures_dir, name)
    rel = None
    try:
        if src and os.path.isfile(src):
            shutil.copy2(src, dst)
        else:
            img.save(filepath=dst)
        used_names.add(name)
        rel = "textures/" + name
    except Exception as exc:  # noqa: BLE001 — report, don't abort export
        LOG.warning("Could not export texture %r: %s", img.name, exc)
    cache[img] = rel
    return rel


def collect_piece_textures(materials, textures_dir, cache, used_names):
    """First material with usable PBR maps wins (SurfaceAppearance is one
    set per MeshPart)."""
    for mat in materials:
        images = find_pbr_images(mat)
        if not images:
            continue
        out = {}
        for key, img in images.items():
            rel = save_image_copy(img, textures_dir, cache, used_names)
            if rel:
                out[key] = rel
        if out:
            return out
    return {}


# ---------------------------------------------------------------------------
# Validation / shared computation
# ---------------------------------------------------------------------------


def resolve_collision(settings, ob=None):
    if ob is not None:
        override = getattr(ob, "rbx_collision_override", 'INHERIT')
        if override and override != 'INHERIT':
            return override
    return settings.collision_fidelity


def settings_snapshot(settings):
    return {
        "export_mode": settings.export_mode,
        "meters_per_stud": round(settings.meters_per_stud, 6),
        "max_tile_size_studs": settings.max_tile_size,
        "safety_margin_studs": settings.safety_margin,
        "slice_axes_xyz": [settings.slice_x, settings.slice_y,
                           settings.slice_z],
        "grouping_mode": settings.grouping_mode,
        "collision_fidelity": COLLISION_LABELS[settings.collision_fidelity],
        "export_textures": settings.export_textures,
        "origin_mode": settings.origin_mode,
        "triangle_limit": settings.tri_limit,
        "auto_split_dense": settings.auto_split,
        "selected_only": settings.selected_only,
        "anchored": settings.anchored,
        "model_name": settings.model_name,
    }


def compute_scene_grid(context, settings):
    """(objects, grid, warnings) shared by preview / stats / export."""
    warnings = []
    objects = gather_export_objects(context, settings)
    if not objects:
        raise ExportError(
            "No mesh objects to export (check visibility, the 'Selected "
            "Only' toggle, and per-object 'Exclude from Export' flags)")
    depsgraph = context.evaluated_depsgraph_get()
    bounds = scene_world_bounds(objects, depsgraph)
    if bounds is None:
        raise ExportError("Could not compute scene bounds")
    grid = compute_grid(bounds[0], bounds[1],
                        settings.meters_per_stud,
                        settings.max_tile_size,
                        settings.safety_margin,
                        (settings.slice_x, settings.slice_y,
                         settings.slice_z))
    mps = settings.meters_per_stud
    for axis, label in enumerate("XYZ"):
        tile_studs = grid.tile_size[axis] / mps
        if tile_studs > ROBLOX_HARD_MAX_STUDS + _EPS:
            warnings.append(
                f"Tile size on Blender {label} is {tile_studs:.0f} studs — "
                f"over Roblox's {ROBLOX_HARD_MAX_STUDS:.0f} limit "
                f"(enable 'Slice {label}')")
    if grid.tile_count > MAX_TILES_HARD:
        raise ExportError(
            f"Grid would produce {grid.tile_count} tiles (max "
            f"{MAX_TILES_HARD}). Increase Max Tile Size, adjust the stud "
            "scale, or shrink the world")
    if grid.tile_count > MAX_TILES_WARN:
        warnings.append(f"{grid.tile_count} tiles — export may take a while")
    return objects, grid, warnings


def estimate_piece_counts(objects, grid, depsgraph):
    """(merged_estimate, per_object_estimate) — bounding-box based, cheap."""
    per_object = 0
    tiles = set()
    for ob in objects:
        lo, hi = object_world_bounds(ob, depsgraph)
        (i0, i1), (j0, j1), (k0, k1) = tile_index_range(grid, lo, hi)
        span = (i1 - i0 + 1) * (j1 - j0 + 1) * (k1 - k0 + 1)
        per_object += span
        for cell in product(range(i0, i1 + 1), range(j0, j1 + 1),
                            range(k0, k1 + 1)):
            tiles.add(cell)
    return len(tiles), per_object


# ---------------------------------------------------------------------------
# Live scene snapshot cache (stats panel + grid preview)
# ---------------------------------------------------------------------------

# The depsgraph handler marks the cache dirty on ANY scene change; whoever
# draws next (panel or viewport overlay) recomputes lazily, throttled so
# dragging a heavy scene doesn't recompute every frame.

_CACHE = {"dirty": True, "stamp": 0.0, "grid": None, "stats": None,
          "error": None}
_EXPORT_RUNNING = False
_THROTTLE_S = 0.25


def _invalidate(_self=None, _context=None):
    """Mark cached grid/stats stale (also used as a property update callback)."""
    _CACHE["dirty"] = True


def get_scene_snapshot(context):
    """Cached {grid, stats, error}; recomputed lazily when stale."""
    now = time.monotonic()
    if not _CACHE["dirty"]:
        return _CACHE
    if now - _CACHE["stamp"] < _THROTTLE_S and _CACHE["stamp"] > 0.0:
        return _CACHE  # too soon — serve stale, stay dirty
    _CACHE["stamp"] = now
    _CACHE["dirty"] = False
    settings = context.scene.rbx_export
    try:
        objects, grid, warnings = compute_scene_grid(context, settings)
        depsgraph = context.evaluated_depsgraph_get()
        merged_est, per_object_est = estimate_piece_counts(
            objects, grid, depsgraph)
        mps = settings.meters_per_stud
        _CACHE["grid"] = grid
        _CACHE["error"] = None
        _CACHE["stats"] = {
            "objects": len(objects),
            "world_studs": tuple(
                (grid.bounds_max[a] - grid.bounds_min[a]) / mps
                for a in range(3)),
            "divisions": grid.divisions,
            "tile_count": grid.tile_count,
            "tile_studs": tuple(t / mps for t in grid.tile_size),
            "merged_estimate": merged_est,
            "per_object_estimate": per_object_est,
            "warnings": warnings,
        }
    except ExportError as exc:
        _CACHE["grid"] = None
        _CACHE["stats"] = None
        _CACHE["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — never break UI drawing
        _CACHE["grid"] = None
        _CACHE["stats"] = None
        _CACHE["error"] = f"Stats unavailable: {exc}"
    return _CACHE


@persistent
def _on_depsgraph_update(scene, depsgraph=None):
    if not _EXPORT_RUNNING:
        _invalidate()


@persistent
def _on_load_post(_filepath=None):
    _remove_preview()
    _invalidate()


# ---------------------------------------------------------------------------
# Export pipeline
# ---------------------------------------------------------------------------


def _setup_file_logging(out_dir, enabled):
    if not enabled:
        return None
    handler = logging.FileHandler(
        os.path.join(out_dir, "export_log.txt"), mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG)
    return handler


def _teardown_file_logging(handler):
    if handler is not None:
        LOG.removeHandler(handler)
        handler.close()
    LOG.setLevel(logging.INFO)


def _export_fbx(context, objs, filepath, embed_textures):
    """Export the given temp objects to one FBX.

    Meshes are pre-baked in studs (centered per piece in scaffold mode, at
    world position in combined mode); the exporter bakes the Z-up -> Y-up
    conversion into vertex data so node transforms stay identity. With
    ``embed_textures`` images are packed into the FBX so Roblox's 3D
    Importer uploads them automatically.
    """
    for ob in context.selected_objects:
        ob.select_set(False)
    for ob in objs:
        ob.select_set(True)
    context.view_layer.objects.active = objs[0]
    try:
        bpy.ops.export_scene.fbx(
            filepath=filepath,
            check_existing=False,
            use_selection=True,
            object_types={'MESH'},
            use_mesh_modifiers=False,
            mesh_smooth_type='FACE',
            use_triangles=True,
            add_leaf_bones=False,
            bake_anim=False,
            axis_forward='-Z',
            axis_up='Y',
            global_scale=1.0,
            apply_unit_scale=True,
            apply_scale_options='FBX_SCALE_ALL',
            bake_space_transform=True,
            path_mode='COPY' if embed_textures else 'AUTO',
            embed_textures=embed_textures,
        )
    except AttributeError as exc:
        raise ExportError(
            "FBX exporter unavailable — enable the built-in "
            "'FBX format' add-on in Preferences") from exc
    finally:
        for ob in objs:
            ob.select_set(False)


def run_export(context, settings, report):
    """Full export. Returns (piece_count, tile_count, out_dir, warnings)."""
    global _EXPORT_RUNNING
    if not settings.output_dir:
        raise ExportError("Set an output folder first")
    if settings.output_dir.startswith("//") and not bpy.data.filepath:
        raise ExportError(
            "Output folder is relative to the .blend file — save the "
            ".blend first or pick an absolute folder")
    out_dir = bpy.path.abspath(settings.output_dir)
    combined = settings.export_mode == 'COMBINED'
    meshes_dir = os.path.join(out_dir, "meshes")
    textures_dir = os.path.join(out_dir, "textures")
    os.makedirs(out_dir, exist_ok=True)
    if not combined:
        os.makedirs(meshes_dir, exist_ok=True)
    if settings.export_textures:
        os.makedirs(textures_dir, exist_ok=True)

    log_handler = _setup_file_logging(out_dir, settings.debug_logging)
    wm = context.window_manager
    temp_collection = None
    temp_meshes = []
    temp_objects = []
    prev_selection = list(context.selected_objects)
    prev_active = context.view_layer.objects.active
    mps = settings.meters_per_stud
    _EXPORT_RUNNING = True

    try:
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        objects, grid, warnings = compute_scene_grid(context, settings)
        depsgraph = context.evaluated_depsgraph_get()
        origin_offset = compute_origin_offset(
            grid.bounds_min, grid.bounds_max, settings.origin_mode)
        LOG.info("Exporting %d object(s); grid %s, tile size (studs) %s, "
                 "mode %s", len(objects), grid.divisions,
                 tuple(round(t / mps, 2) for t in grid.tile_size),
                 settings.export_mode)

        merged = settings.grouping_mode == 'MERGED'
        if merged:
            overridden = [ob.name for ob in objects
                          if getattr(ob, "rbx_collision_override",
                                     'INHERIT') != 'INHERIT']
            if overridden:
                warnings.append(
                    "Per-object collision overrides are ignored in Merged "
                    f"mode ({len(overridden)} object(s))")

        # -- Phase 1: slice every object into per-tile piece meshes --------
        tile_pieces = {}  # (i, j, k) -> [(Mesh, source Object), ...]
        wm.progress_begin(0, len(objects) + 1)
        for ob_idx, ob in enumerate(objects):
            wm.progress_update(ob_idx)
            me_world = make_world_mesh(ob, depsgraph)
            if me_world is None or len(me_world.polygons) == 0:
                if me_world is not None:
                    bpy.data.meshes.remove(me_world)
                warnings.append(f"Skipped '{ob.name}' (no faces)")
                continue
            materials = list(me_world.materials)
            obounds = mesh_bounds(me_world)
            ranges = tile_index_range(grid, obounds[0], obounds[1])
            bm = bmesh.new()
            bm.from_mesh(me_world)
            bpy.data.meshes.remove(me_world)
            if settings.debug_logging:
                nm = count_nonmanifold_edges(bm)
                if nm:
                    LOG.debug("'%s': %d non-manifold edge(s) — collisions "
                              "may be imprecise", ob.name, nm)
            leaves = []
            split_bmesh_recursive(bm, ranges, grid, leaves)
            for cell, leaf_bm in leaves:
                piece_me = bmesh_to_new_mesh(
                    leaf_bm, f"rbx_tmp_{ob.name}_{cell}", materials)
                leaf_bm.free()
                temp_meshes.append(piece_me)
                tile_pieces.setdefault(cell, []).append((piece_me, ob))
        wm.progress_end()

        if not tile_pieces:
            raise ExportError("Nothing to export — all pieces were empty")

        # -- Phase 2: grouping ----------------------------------------------
        final = []  # (base_name, Mesh, cell, collision_key)
        used_names = set()
        # Track which objects landed in more than one tile (for suffixes).
        object_tile_counts = {}
        for cell, plist in tile_pieces.items():
            for _me, src_ob in plist:
                object_tile_counts[src_ob.name] = \
                    object_tile_counts.get(src_ob.name, 0) + 1

        for cell in sorted(tile_pieces):
            plist = tile_pieces[cell]
            if merged:
                name = unique_name("Tile" + piece_grid_suffix(grid, cell),
                                   used_names)
                if len(plist) == 1:
                    me = plist[0][0]
                    me.name = name
                else:
                    me = merge_meshes(name, [p[0] for p in plist])
                    temp_meshes.append(me)
                final.append((name, me, cell, settings.collision_fidelity))
            else:
                for piece_me, src_ob in plist:
                    base = sanitize_name(src_ob.name)
                    if object_tile_counts.get(src_ob.name, 1) > 1:
                        base += piece_grid_suffix(grid, cell)
                    name = unique_name(base, used_names)
                    final.append((name, piece_me, cell,
                                  resolve_collision(settings, src_ob)))

        # -- Phase 3: enforce Roblox's per-mesh triangle limit ---------------
        tri_limit = settings.tri_limit
        expanded = []
        for name, me, cell, collision in final:
            tris = mesh_triangle_count(me)
            if tris > tri_limit:
                if settings.auto_split:
                    if me in temp_meshes:
                        temp_meshes.remove(me)
                    parts = split_mesh_by_triangles(me, tri_limit)
                    temp_meshes.extend(parts)
                    if len(parts) > 1:
                        LOG.info("Auto-split '%s' (%d tris) into %d pieces",
                                 name, tris, len(parts))
                        for n, part in enumerate(parts, start=1):
                            sub = unique_name(f"{name}_p{n}", used_names)
                            expanded.append((sub, part, cell, collision))
                        continue
                    me = parts[0]
                    warnings.append(
                        f"'{name}' still has {mesh_triangle_count(me)} "
                        f"triangles (> {tri_limit}) — Roblox may reject it; "
                        "add a Decimate modifier")
                else:
                    warnings.append(
                        f"'{name}' has {tris} triangles (> {tri_limit}) — "
                        "Roblox may reject it (enable Auto-Split or "
                        "decimate)")
            expanded.append((name, me, cell, collision))
        final = expanded

        # -- Phase 4: bake + export FBX + gather metadata --------------------
        temp_collection = bpy.data.collections.new("RBX_EXPORT_TMP")
        context.scene.collection.children.link(temp_collection)
        texture_cache = {}
        texture_names = set()
        pieces = []
        offset_v = Vector(origin_offset)
        scale_m = Matrix.Scale(1.0 / mps, 4)

        wm.progress_begin(0, len(final) + 1)
        for idx, (name, me, cell, collision) in enumerate(final):
            wm.progress_update(idx)
            b = mesh_bounds(me)
            if b is None:
                continue
            center = tuple((b[0][a] + b[1][a]) * 0.5 for a in range(3))
            size_m = tuple(b[1][a] - b[0][a] for a in range(3))
            tris = mesh_triangle_count(me)

            if combined:
                # Keep world placement (minus origin offset), scale to studs.
                me.transform(scale_m @ Matrix.Translation(-offset_v))
            else:
                # Center each piece; the .rbxmx / place script restores it.
                me.transform(scale_m @ Matrix.Translation(-Vector(center)))

            piece_ob = bpy.data.objects.new(name, me)
            temp_collection.objects.link(piece_ob)
            temp_objects.append(piece_ob)

            rel_center = tuple(center[a] - origin_offset[a] for a in range(3))
            textures = {}
            if settings.export_textures:
                textures = collect_piece_textures(
                    list(me.materials), textures_dir,
                    texture_cache, texture_names)
            pieces.append(PieceInfo(
                name=name,
                grid_index=cell,
                fbx_rel=(f"{sanitize_name(settings.model_name)}.fbx"
                         if combined else f"meshes/{name}.fbx"),
                position_studs=blender_to_roblox_point(rel_center, mps),
                size_studs=blender_to_roblox_size(size_m, mps),
                triangles=tris,
                collision=collision,
                textures=textures,
            ))

        context.view_layer.update()
        if combined:
            fbx_name = f"{sanitize_name(settings.model_name)}.fbx"
            _export_fbx(context, temp_objects,
                        os.path.join(out_dir, fbx_name),
                        settings.export_textures)
            LOG.info("Wrote %s (%d mesh piece(s))", fbx_name, len(pieces))
        else:
            for idx, (piece_ob, piece) in enumerate(zip(temp_objects,
                                                        pieces)):
                wm.progress_update(idx)
                _export_fbx(context, [piece_ob],
                            os.path.join(meshes_dir, f"{piece.name}.fbx"),
                            settings.export_textures)
                LOG.debug("Exported %s (%d tris)", piece.name,
                          piece.triangles)
        wm.progress_end()

        # -- Phase 5: write scaffold + manifest -------------------------------
        combined_fbx = (f"{sanitize_name(settings.model_name)}.fbx"
                        if combined else None)
        if not combined:
            rbxmx_path = os.path.join(
                out_dir, f"{sanitize_name(settings.model_name)}.rbxmx")
            with open(rbxmx_path, "w", encoding="utf-8") as fh:
                fh.write(build_rbxmx(settings.model_name, pieces,
                                     settings.anchored))
            with open(os.path.join(out_dir, "place_pieces.lua"), "w",
                      encoding="utf-8") as fh:
                fh.write(build_place_script(settings.model_name, pieces))
            warnings.append(
                "Scaffold mode: .rbxmx parts are INVISIBLE until MeshIds "
                "are pasted — import the FBX pieces with the 3D Importer "
                "and run place_pieces.lua instead for instant placement")

        manifest = build_manifest(settings_snapshot(settings), grid, mps,
                                  origin_offset, pieces,
                                  combined_fbx=combined_fbx)
        manifest["warnings"] = warnings
        with open(os.path.join(out_dir, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        for w in warnings:
            LOG.warning("%s", w)
            report({'WARNING'}, w)
        LOG.info("Export complete: %d piece(s), %d tile cell(s) -> %s",
                 len(pieces), len(tile_pieces), out_dir)
        return len(pieces), len(tile_pieces), out_dir, warnings

    finally:
        # Non-destructive guarantee: every temp datablock is removed and the
        # user's selection restored, even on failure.
        _EXPORT_RUNNING = False
        try:
            wm.progress_end()
        except Exception:  # noqa: BLE001
            pass
        if temp_collection is not None:
            for ob in list(temp_collection.objects):
                temp_collection.objects.unlink(ob)
                bpy.data.objects.remove(ob)
            context.scene.collection.children.unlink(temp_collection)
            bpy.data.collections.remove(temp_collection)
        for me in temp_meshes:
            try:
                bpy.data.meshes.remove(me)
            except Exception:  # noqa: BLE001
                pass
        for ob in prev_selection:
            try:
                ob.select_set(True)
            except Exception:  # noqa: BLE001
                pass
        if prev_active is not None:
            try:
                context.view_layer.objects.active = prev_active
            except Exception:  # noqa: BLE001
                pass
        _teardown_file_logging(log_handler)
        _invalidate()


# ---------------------------------------------------------------------------
# Grid preview overlay (live: follows scene + setting changes automatically)
# ---------------------------------------------------------------------------

_PREVIEW = {"handler": None, "grid_key": None, "batch": None, "shader": None}


def _draw_preview():
    snap = get_scene_snapshot(bpy.context)
    grid = snap.get("grid")
    if grid is None:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader
    shader = _PREVIEW.get("shader")
    if shader is None:
        try:
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        except ValueError:  # Blender < 3.4 naming
            shader = gpu.shader.from_builtin('3D_UNIFORM_COLOR')
        _PREVIEW["shader"] = shader
    grid_key = (grid.bounds_min, grid.bounds_max, grid.divisions)
    if _PREVIEW.get("batch") is None or _PREVIEW.get("grid_key") != grid_key:
        _PREVIEW["batch"] = batch_for_shader(
            shader, 'LINES', {"pos": grid_line_coords(grid)})
        _PREVIEW["grid_key"] = grid_key
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(2.0)
    shader.bind()
    shader.uniform_float("color", (1.0, 0.5, 0.1, 0.9))
    _PREVIEW["batch"].draw(shader)
    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')


def _remove_preview():
    if _PREVIEW["handler"] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_PREVIEW["handler"],
                                                  'WINDOW')
    _PREVIEW.update({"handler": None, "grid_key": None, "batch": None})


def _tag_redraw_3d(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

_COLLISION_ENUM = [
    ('DEFAULT', "Default", "Roblox's standard approximate collision"),
    ('HULL', "Hull", "Single convex hull — cheap, rough"),
    ('BOX', "Box", "Bounding box collision — cheapest"),
    ('PRECISE', "Precise Convex Decomposition",
     "Most accurate, most expensive"),
]


class RBXExportSettings(bpy.types.PropertyGroup):
    export_mode: bpy.props.EnumProperty(
        name="Export Mode",
        description="How the sliced world is delivered to Roblox",
        items=[
            ('COMBINED', "Single FBX (3D Importer)",
             "One FBX with every piece at its world position — import it "
             "with Studio's 3D Importer and the whole map appears "
             "assembled instantly (recommended)"),
            ('SCAFFOLD', "Per-Piece FBX + .rbxmx",
             "One FBX per piece + an .rbxmx placeholder scaffold + a "
             "place_pieces.lua command-bar script (advanced; parts are "
             "invisible until MeshIds are pasted or the script is run)"),
        ],
        default='COMBINED')
    meters_per_stud: bpy.props.FloatProperty(
        name="Meters per Stud",
        description="Blender meters that equal one Roblox stud "
                    "(Roblox convention: 0.28)",
        default=0.28, min=0.0001, soft_max=10.0, precision=4,
        update=_invalidate)
    max_tile_size: bpy.props.FloatProperty(
        name="Max Tile Size",
        description="Roblox MeshPart per-axis size limit, in studs",
        default=2048.0, min=8.0, max=2048.0, update=_invalidate)
    safety_margin: bpy.props.FloatProperty(
        name="Safety Margin",
        description="Subtracted from Max Tile Size before gridding, so "
                    "pieces never brush the hard limit (2048-48 = 2000)",
        default=48.0, min=0.0, max=1024.0, update=_invalidate)
    slice_x: bpy.props.BoolProperty(
        name="Slice X",
        description="Slice along Blender X (sideways)",
        default=True, update=_invalidate)
    slice_y: bpy.props.BoolProperty(
        name="Slice Y",
        description="Slice along Blender Y (depth)",
        default=True, update=_invalidate)
    slice_z: bpy.props.BoolProperty(
        name="Slice Z",
        description="Slice along Blender Z (world height)",
        default=True, update=_invalidate)
    grouping_mode: bpy.props.EnumProperty(
        name="Grouping",
        description="How sliced geometry becomes MeshParts",
        items=[
            ('MERGED', "Merged per Tile",
             "All geometry inside a tile merges into one MeshPart"),
            ('PER_OBJECT', "Per Object",
             "Each Blender object stays its own MeshPart (sliced only "
             "where it straddles a grid line)"),
        ],
        default='MERGED', update=_invalidate)
    tri_limit: bpy.props.IntProperty(
        name="Triangle Limit",
        description="Roblox rejects meshes above ~20,000 triangles; pieces "
                    "denser than this are auto-split (or flagged)",
        default=ROBLOX_TRI_LIMIT, min=500, max=200000)
    auto_split: bpy.props.BoolProperty(
        name="Auto-Split Dense Pieces",
        description="Recursively halve pieces that exceed the triangle "
                    "limit so Roblox accepts them",
        default=True)
    collision_fidelity: bpy.props.EnumProperty(
        name="Collision",
        description="CollisionFidelity written to the .rbxmx / place script",
        items=_COLLISION_ENUM, default='DEFAULT')
    export_textures: bpy.props.BoolProperty(
        name="Export Textures",
        description="Embed textures in the FBX (Roblox's 3D Importer "
                    "uploads them automatically), copy PBR maps "
                    "(color/normal/roughness/metalness) to textures/, and "
                    "add SurfaceAppearance placeholders in scaffold mode",
        default=False)
    origin_mode: bpy.props.EnumProperty(
        name="Map Origin",
        description="Where the exported map sits in Roblox coordinates",
        items=[
            ('WORLD', "Keep Blender Origin",
             "Positions match Blender's world origin exactly"),
            ('CENTER', "Center at Origin",
             "Footprint centered on Roblox (0, y, 0) with the lowest point "
             "at ground level Y=0"),
        ],
        default='WORLD')
    anchored: bpy.props.BoolProperty(
        name="Anchored",
        description="Scaffold parts are exported with Anchored = true "
                    "(the place script always anchors)",
        default=True)
    selected_only: bpy.props.BoolProperty(
        name="Selected Only",
        description="Export only the selected mesh objects",
        default=False, update=_invalidate)
    model_name: bpy.props.StringProperty(
        name="Model Name",
        description="Name of the exported FBX/.rbxmx model",
        default="BlenderWorld")
    output_dir: bpy.props.StringProperty(
        name="Output Folder",
        description="Folder receiving the export "
                    "(FBX, manifest.json, textures/ ...)",
        subtype='DIR_PATH', default="//roblox_export/")
    debug_logging: bpy.props.BoolProperty(
        name="Debug Logging",
        description="Verbose logging to the console and export_log.txt in "
                    "the output folder",
        default=False)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


class RBX_OT_toggle_grid_preview(bpy.types.Operator):
    """Toggle the live grid overlay (it follows edits and setting changes)"""
    bl_idname = "rbx.toggle_grid_preview"
    bl_label = "Preview Grid"

    def execute(self, context):
        if _PREVIEW["handler"] is not None:
            _remove_preview()
            _tag_redraw_3d(context)
            return {'FINISHED'}
        _invalidate()
        _PREVIEW["handler"] = bpy.types.SpaceView3D.draw_handler_add(
            _draw_preview, (), 'WINDOW', 'POST_VIEW')
        snap = get_scene_snapshot(context)
        if snap["error"]:
            self.report({'WARNING'}, snap["error"])
        elif snap["grid"] is not None:
            nx, ny, nz = snap["grid"].divisions
            self.report({'INFO'},
                        f"Grid: {nx} x {ny} x {nz} "
                        f"({snap['grid'].tile_count} tile(s)) — live")
        _tag_redraw_3d(context)
        return {'FINISHED'}


class RBX_OT_dry_run(bpy.types.Operator):
    """Report the grid, tile sizes and piece estimate without writing files"""
    bl_idname = "rbx.dry_run"
    bl_label = "Dry Run"

    def execute(self, context):
        settings = context.scene.rbx_export
        _invalidate()
        _CACHE["stamp"] = 0.0  # bypass throttle for an explicit request
        snap = get_scene_snapshot(context)
        if snap["error"] or snap["stats"] is None:
            self.report({'ERROR'}, snap["error"] or "Stats unavailable")
            return {'CANCELLED'}
        s = snap["stats"]
        est = (s["merged_estimate"] if settings.grouping_mode == 'MERGED'
               else s["per_object_estimate"])
        lines = [
            f"Objects: {s['objects']}",
            (f"World size (studs, Blender XYZ): {s['world_studs'][0]:.1f} x "
             f"{s['world_studs'][1]:.1f} x {s['world_studs'][2]:.1f}"),
            (f"Grid: {s['divisions'][0]} x {s['divisions'][1]} x "
             f"{s['divisions'][2]} = {s['tile_count']} tile(s)"),
            (f"Tile size (studs): {s['tile_studs'][0]:.1f} x "
             f"{s['tile_studs'][1]:.1f} x {s['tile_studs'][2]:.1f}"),
            (f"Estimated pieces ({settings.grouping_mode.lower()}, "
             f"bbox-based): {est}"),
        ]
        lines += [f"Warning: {w}" for w in s["warnings"]]

        def draw(menu, _context):
            for line in lines:
                menu.layout.label(text=line)

        context.window_manager.popup_menu(
            draw, title="Roblox Export — Dry Run", icon='INFO')
        for line in lines:
            LOG.info("%s", line)
        self.report({'INFO'}, lines[2])
        return {'FINISHED'}


class RBX_OT_export_world(bpy.types.Operator):
    """Slice the world into tiles and export it for Roblox"""
    bl_idname = "rbx.export_world"
    bl_label = "Export for Roblox"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.rbx_export
        try:
            count, tiles, out_dir, warnings = run_export(
                context, settings, self.report)
        except ExportError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception:  # noqa: BLE001 — surface unexpected bugs cleanly
            LOG.error("Unexpected export failure:\n%s",
                      traceback.format_exc())
            self.report(
                {'ERROR'},
                "Unexpected export failure — see the system console"
                + (" / export_log.txt" if settings.debug_logging else "")
                + " for the traceback")
            return {'CANCELLED'}
        suffix = f" ({len(warnings)} warning(s))" if warnings else ""
        self.report({'INFO'},
                    f"Exported {count} piece(s) across {tiles} tile(s) to "
                    f"{out_dir}{suffix}")
        return {'FINISHED'}


class RBX_OT_open_output(bpy.types.Operator):
    """Open the output folder in the system file browser"""
    bl_idname = "rbx.open_output"
    bl_label = "Open Output Folder"

    def execute(self, context):
        settings = context.scene.rbx_export
        path = bpy.path.abspath(settings.output_dir)
        if not path or not os.path.isdir(path):
            self.report({'ERROR'}, "Output folder doesn't exist yet")
            return {'CANCELLED'}
        bpy.ops.wm.path_open(filepath=path)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


class RBX_PT_exporter(bpy.types.Panel):
    bl_label = "Roblox World Exporter"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Roblox"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.rbx_export

        layout.prop(settings, "export_mode", text="Mode")

        # Live stats readout.
        snap = get_scene_snapshot(context)
        box = layout.box()
        box.label(text="Live Stats", icon='INFO')
        col = box.column(align=True)
        if snap["error"]:
            col.label(text=snap["error"], icon='ERROR')
        elif snap["stats"]:
            s = snap["stats"]
            w = s["world_studs"]
            t = s["tile_studs"]
            d = s["divisions"]
            est = (s["merged_estimate"]
                   if settings.grouping_mode == 'MERGED'
                   else s["per_object_estimate"])
            col.label(text=f"Objects: {s['objects']}")
            col.label(text=f"World: {w[0]:.0f} x {w[1]:.0f} x {w[2]:.0f} "
                           "studs")
            col.label(text=f"Segments: {d[0]} x {d[1]} x {d[2]} = "
                           f"{s['tile_count']} tile(s)")
            col.label(text=f"Tile: {t[0]:.0f} x {t[1]:.0f} x {t[2]:.0f} "
                           "studs")
            col.label(text=f"Est. pieces: {est}")
            for warning in s["warnings"]:
                col.label(text=warning, icon='ERROR')
        else:
            col.label(text="Add mesh objects to see stats")

        row = layout.row(align=True)
        row.operator(RBX_OT_toggle_grid_preview.bl_idname,
                     icon='MESH_GRID',
                     depress=_PREVIEW["handler"] is not None)
        row.operator(RBX_OT_dry_run.bl_idname, icon='CONSOLE')
        layout.operator(RBX_OT_export_world.bl_idname, icon='EXPORT')


class RBX_PT_scale(bpy.types.Panel):
    bl_label = "Scale & Grid"
    bl_parent_id = "RBX_PT_exporter"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.rbx_export
        layout.prop(settings, "meters_per_stud")
        layout.prop(settings, "max_tile_size")
        layout.prop(settings, "safety_margin")
        row = layout.row(align=True, heading="Slice Axes")
        row.prop(settings, "slice_x", text="X", toggle=True)
        row.prop(settings, "slice_y", text="Y", toggle=True)
        row.prop(settings, "slice_z", text="Z", toggle=True)


class RBX_PT_slicing(bpy.types.Panel):
    bl_label = "Pieces & Density"
    bl_parent_id = "RBX_PT_exporter"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.rbx_export
        layout.prop(settings, "grouping_mode")
        layout.prop(settings, "auto_split")
        sub = layout.column()
        sub.enabled = settings.auto_split
        sub.prop(settings, "tri_limit")


class RBX_PT_roblox(bpy.types.Panel):
    bl_label = "Roblox Settings"
    bl_parent_id = "RBX_PT_exporter"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.rbx_export
        layout.prop(settings, "origin_mode")
        layout.prop(settings, "collision_fidelity")
        layout.prop(settings, "anchored")
        layout.prop(settings, "export_textures")


class RBX_PT_output(bpy.types.Panel):
    bl_label = "Output"
    bl_parent_id = "RBX_PT_exporter"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.rbx_export
        layout.prop(settings, "model_name")
        layout.prop(settings, "output_dir")
        layout.prop(settings, "selected_only")
        layout.prop(settings, "debug_logging")
        layout.operator(RBX_OT_open_output.bl_idname, icon='FILE_FOLDER')


class RBX_PT_object(bpy.types.Panel):
    bl_label = "Active Object"
    bl_parent_id = "RBX_PT_exporter"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'MESH'

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        ob = context.object
        settings = context.scene.rbx_export
        layout.label(text=ob.name, icon='OBJECT_DATA')
        layout.prop(ob, "rbx_exclude")
        sub = layout.column()
        sub.enabled = settings.grouping_mode == 'PER_OBJECT'
        sub.prop(ob, "rbx_collision_override")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_CLASSES = (
    RBXExportSettings,
    RBX_OT_toggle_grid_preview,
    RBX_OT_dry_run,
    RBX_OT_export_world,
    RBX_OT_open_output,
    RBX_PT_exporter,
    RBX_PT_scale,
    RBX_PT_slicing,
    RBX_PT_roblox,
    RBX_PT_output,
    RBX_PT_object,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.rbx_export = bpy.props.PointerProperty(
        type=RBXExportSettings)
    bpy.types.Object.rbx_exclude = bpy.props.BoolProperty(
        name="Exclude from Export",
        description="Skip this object when exporting to Roblox",
        default=False, update=_invalidate)
    bpy.types.Object.rbx_collision_override = bpy.props.EnumProperty(
        name="Collision Override",
        description="Per-object CollisionFidelity (Per Object grouping "
                    "mode only)",
        items=[('INHERIT', "Inherit Scene Setting",
                "Use the scene-level collision fidelity")] + _COLLISION_ENUM,
        default='INHERIT')
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    _remove_preview()
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    del bpy.types.Object.rbx_collision_override
    del bpy.types.Object.rbx_exclude
    del bpy.types.Scene.rbx_export
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":  # allows running from Blender's text editor
    register()
