# SPDX-License-Identifier: MIT
"""Roblox World Exporter — Blender add-on.

Exports large Blender worlds to Roblox by slicing them into an even grid of
tiles so every piece fits inside Roblox's 2048x2048x2048-stud MeshPart limit.

Outputs, per export:
  * one FBX per mesh piece (``meshes/*.fbx``) — .rbxmx cannot embed geometry,
    so meshes are uploaded through Roblox's 3D Importer and referenced by id
  * one ``.rbxmx`` scaffold placing correctly named/sized MeshParts at exact
    grid CFrames (identity rotation, studs), with CollisionFidelity and
    optional SurfaceAppearance children
  * ``manifest.json`` mapping every piece -> FBX file -> grid cell -> stud
    position/size (plus exported texture files), for pasting asset ids

Coordinate mapping (right-handed both sides):
  Blender (x, y, z) [Z-up, meters]  ->  Roblox (x, z, -y) [Y-up, studs]
  studs = meters / meters_per_stud            (default 0.28 m per stud)

The pure-math helpers in the first half of this file deliberately avoid any
Blender API at call time so they can be unit-tested under plain Python.
"""

bl_info = {
    "name": "Roblox World Exporter",
    "author": "Generated with Claude Code",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar (N) > Roblox",
    "description": "Slice large worlds into an even grid and export FBX tiles + .rbxmx for Roblox",
    "doc_url": "",
    "category": "Import-Export",
}

import json
import logging
import math
import os
import re
import shutil
import traceback
from dataclasses import dataclass, field
from itertools import product
from xml.etree import ElementTree as ET

import bpy
import bmesh
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

ROBLOX_HARD_MAX_STUDS = 2048.0      # Roblox MeshPart per-axis size limit
ROBLOX_TRI_WARN = 10000             # Roblox per-mesh triangle guidance
MAX_TILES_HARD = 4096               # refuse to generate more tiles than this
MAX_TILES_WARN = 512

COLLISION_TOKENS = {
    "DEFAULT": 0,
    "HULL": 1,
    "BOX": 2,
    "PRECISE": 3,
}
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
    """Compact float formatting for XML/JSON output."""
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
                 max_tile_studs, safety_margin_studs, slice_z=True):
    """Compute the even grid for the given world bounds.

    ``safety_margin_studs`` shrinks the effective tile limit (e.g. 2048 - 48
    = 2000) so pieces never brush against Roblox's hard cap after float
    round-off or later re-scaling.
    """
    if meters_per_stud <= _EPS:
        raise ValueError("meters_per_stud must be > 0")
    effective = max(1.0, max_tile_studs - max(0.0, safety_margin_studs))
    divisions = []
    tile_size = []
    for axis in range(3):
        extent_m = max(0.0, bounds_max[axis] - bounds_min[axis])
        extent_studs = extent_m / meters_per_stud
        if axis == 2 and not slice_z:
            n = 1
        else:
            n = grid_divisions(extent_studs, effective)
        divisions.append(n)
        tile_size.append(extent_m / n if n else extent_m)
    return GridSpec(tuple(bounds_min), tuple(bounds_max),
                    tuple(divisions), tuple(tile_size))


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

    # Outer box: 12 edges.
    corners = list(product((lo[0], hi[0]), (lo[1], hi[1]), (lo[2], hi[2])))
    for a in range(8):
        for b in range(a + 1, 8):
            if sum(1 for axis in range(3) if corners[a][axis] != corners[b][axis]) == 1:
                seg(corners[a], corners[b])

    # Interior plane outlines.
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
    """One exported mesh piece (== one MeshPart in the .rbxmx)."""

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


# --- .rbxmx writing ---------------------------------------------------------

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

    MeshId (and SurfaceAppearance maps) carry ``rbxassetid://0`` placeholders;
    upload the FBX/texture files and paste real ids (see manifest.json).
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


def build_manifest(settings_dict, grid, meters_per_stud, pieces):
    """JSON-serializable manifest mapping pieces to files and stud placement."""
    return {
        "generator": "Roblox World Exporter",
        "version": ".".join(str(v) for v in bl_info["version"]),
        "workflow": [
            "1. In Roblox Studio open the 3D Importer and import every FBX "
            "under meshes/ (keep the importer's default 'Studs' scale unit).",
            "2. Insert the .rbxmx into your place "
            "(right-click Workspace > Insert From File).",
            "3. For each MeshPart, paste the uploaded mesh's asset id into "
            "its MeshId property (names match the FBX filenames below).",
            "4. If textures were exported, upload the files under textures/ "
            "and paste their asset ids into each SurfaceAppearance map.",
        ],
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
            "tile_count": grid.tile_count,
        },
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


def slice_bmesh_to_box(bm, box_min, box_max, grid_min, grid_max, eps=1e-6):
    """Keep only geometry inside the AABB (bisecting across its faces).

    Planes coincident with the global grid bounds are skipped: nothing lies
    beyond them, and skipping avoids razor-thin float slivers.
    """
    for axis in range(3):
        planes = (
            (box_min[axis], -1.0, abs(box_min[axis] - grid_min[axis]) < eps),
            (box_max[axis], 1.0, abs(box_max[axis] - grid_max[axis]) < eps),
        )
        for co, sign, at_outer_bound in planes:
            if at_outer_bound:
                continue
            if not bm.faces:
                return
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
                clear_outer=True,
                clear_inner=False,
            )
    loose_edges = [e for e in bm.edges if not e.link_faces]
    if loose_edges:
        bmesh.ops.delete(bm, geom=loose_edges, context='EDGES')
    loose_verts = [v for v in bm.verts if not v.link_faces]
    if loose_verts:
        bmesh.ops.delete(bm, geom=loose_verts, context='VERTS')


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
        "meters_per_stud": round(settings.meters_per_stud, 6),
        "max_tile_size_studs": settings.max_tile_size,
        "safety_margin_studs": settings.safety_margin,
        "grouping_mode": settings.grouping_mode,
        "collision_fidelity": COLLISION_LABELS[settings.collision_fidelity],
        "export_textures": settings.export_textures,
        "slice_vertically": settings.slice_z,
        "selected_only": settings.selected_only,
        "anchored": settings.anchored,
        "model_name": settings.model_name,
    }


def compute_scene_grid(context, settings):
    """(objects, grid, warnings) shared by preview / dry-run / export."""
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
                        settings.slice_z)
    mps = settings.meters_per_stud
    for axis, label in enumerate("XYZ"):
        tile_studs = grid.tile_size[axis] / mps
        if tile_studs > ROBLOX_HARD_MAX_STUDS + _EPS:
            warnings.append(
                f"Tile size on Blender {label} is {tile_studs:.0f} studs — "
                f"over Roblox's {ROBLOX_HARD_MAX_STUDS:.0f} limit"
                + (" (enable 'Slice Vertically')" if axis == 2 else ""))
    if grid.tile_count > MAX_TILES_HARD:
        raise ExportError(
            f"Grid would produce {grid.tile_count} tiles (max "
            f"{MAX_TILES_HARD}). Increase Max Tile Size, adjust the stud "
            "scale, or shrink the world")
    if grid.tile_count > MAX_TILES_WARN:
        warnings.append(f"{grid.tile_count} tiles — export may take a while")
    return objects, grid, warnings


def estimate_piece_counts(objects, grid, depsgraph, grouping_mode):
    """Bounding-box based estimate for the dry run (no meshes are built)."""
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
    return len(tiles) if grouping_mode == 'MERGED' else per_object


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


def _export_piece_fbx(context, piece_ob, filepath, embed_textures):
    """Export one temp object. The mesh is pre-baked in studs and centered,
    so the exporter only handles the Z-up -> Y-up axis swap (baked into
    vertex data) and node transforms stay identity.

    With ``embed_textures`` the images are packed into the FBX itself, so
    Roblox's 3D Importer can upload them automatically alongside the mesh.
    """
    for ob in context.selected_objects:
        ob.select_set(False)
    piece_ob.select_set(True)
    context.view_layer.objects.active = piece_ob
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


def run_export(context, settings, report):
    """Full export. Returns (piece_count, tile_count, out_dir, warnings)."""
    if not settings.output_dir:
        raise ExportError("Set an output folder first")
    if settings.output_dir.startswith("//") and not bpy.data.filepath:
        raise ExportError(
            "Output folder is relative to the .blend file — save the "
            ".blend first or pick an absolute folder")
    out_dir = bpy.path.abspath(settings.output_dir)
    meshes_dir = os.path.join(out_dir, "meshes")
    textures_dir = os.path.join(out_dir, "textures")
    os.makedirs(meshes_dir, exist_ok=True)
    if settings.export_textures:
        os.makedirs(textures_dir, exist_ok=True)

    log_handler = _setup_file_logging(out_dir, settings.debug_logging)
    wm = context.window_manager
    temp_collection = None
    temp_meshes = []
    prev_selection = list(context.selected_objects)
    prev_active = context.view_layer.objects.active
    mps = settings.meters_per_stud

    try:
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        objects, grid, warnings = compute_scene_grid(context, settings)
        depsgraph = context.evaluated_depsgraph_get()
        LOG.info("Exporting %d object(s); grid %s, tile size (studs) %s",
                 len(objects), grid.divisions,
                 tuple(round(t / mps, 2) for t in grid.tile_size))

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
            temp_meshes.append(me_world)
            obounds = mesh_bounds(me_world)
            ranges = tile_index_range(grid, obounds[0], obounds[1])
            bm_master = bmesh.new()
            bm_master.from_mesh(me_world)
            if settings.debug_logging:
                nm = count_nonmanifold_edges(bm_master)
                if nm:
                    LOG.debug("'%s': %d non-manifold edge(s) — collisions "
                              "may be imprecise", ob.name, nm)
            spans_multi = any(r[1] > r[0] for r in ranges)
            for cell in product(range(ranges[0][0], ranges[0][1] + 1),
                                range(ranges[1][0], ranges[1][1] + 1),
                                range(ranges[2][0], ranges[2][1] + 1)):
                lo, hi = tile_bounds(grid, cell)
                bm = bm_master.copy()
                try:
                    if spans_multi:
                        slice_bmesh_to_box(bm, lo, hi, grid.bounds_min,
                                           grid.bounds_max)
                    if bm.faces:
                        piece_me = bmesh_to_new_mesh(
                            bm, f"rbx_tmp_{ob.name}_{cell}",
                            list(me_world.materials))
                        temp_meshes.append(piece_me)
                        tile_pieces.setdefault(cell, []).append(
                            (piece_me, ob))
                finally:
                    bm.free()
            bm_master.free()
            temp_meshes.remove(me_world)
            bpy.data.meshes.remove(me_world)
        wm.progress_end()

        if not tile_pieces:
            raise ExportError("Nothing to export — all pieces were empty")

        # -- Phase 2: build the final piece list ---------------------------
        final = []  # (name, Mesh, grid_index, collision_key)
        used_names = set()
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
                    obounds = object_world_bounds(src_ob, depsgraph)
                    ranges = tile_index_range(grid, obounds[0], obounds[1])
                    if any(r[1] > r[0] for r in ranges):
                        base += piece_grid_suffix(grid, cell)
                    name = unique_name(base, used_names)
                    final.append((name, piece_me, cell,
                                  resolve_collision(settings, src_ob)))

        # -- Phase 3: bake, export FBX, gather metadata --------------------
        temp_collection = bpy.data.collections.new("RBX_EXPORT_TMP")
        context.scene.collection.children.link(temp_collection)
        texture_cache = {}
        texture_names = set()
        pieces = []
        wm.progress_begin(0, len(final))
        for idx, (name, me, cell, collision) in enumerate(final):
            wm.progress_update(idx)
            b = mesh_bounds(me)
            if b is None:
                continue
            center = tuple((b[0][a] + b[1][a]) * 0.5 for a in range(3))
            size_m = tuple(b[1][a] - b[0][a] for a in range(3))
            me.calc_loop_triangles()
            tri_count = len(me.loop_triangles)
            if tri_count > ROBLOX_TRI_WARN:
                warnings.append(
                    f"'{name}' has {tri_count} triangles (Roblox guidance "
                    f"is ~{ROBLOX_TRI_WARN} per mesh)")

            # Bake: center at origin, scale meters -> studs (Z-up kept; the
            # FBX exporter bakes the Y-up conversion into vertices).
            me.transform(Matrix.Scale(1.0 / mps, 4)
                         @ Matrix.Translation(-Vector(center)))

            piece_ob = bpy.data.objects.new(name, me)
            temp_collection.objects.link(piece_ob)
            context.view_layer.update()
            fbx_rel = f"meshes/{name}.fbx"
            _export_piece_fbx(context, piece_ob,
                              os.path.join(meshes_dir, f"{name}.fbx"),
                              settings.export_textures)
            textures = {}
            if settings.export_textures:
                textures = collect_piece_textures(
                    list(me.materials), textures_dir,
                    texture_cache, texture_names)

            pieces.append(PieceInfo(
                name=name,
                grid_index=cell,
                fbx_rel=fbx_rel,
                position_studs=blender_to_roblox_point(center, mps),
                size_studs=blender_to_roblox_size(size_m, mps),
                triangles=tri_count,
                collision=collision,
                textures=textures,
            ))
            LOG.debug("Exported %s (%d tris) -> %s", name, tri_count, fbx_rel)

            temp_collection.objects.unlink(piece_ob)
            bpy.data.objects.remove(piece_ob)
            if me in temp_meshes:
                temp_meshes.remove(me)
            bpy.data.meshes.remove(me)
        wm.progress_end()

        # -- Phase 4: write .rbxmx + manifest -------------------------------
        rbxmx_path = os.path.join(
            out_dir, f"{sanitize_name(settings.model_name)}.rbxmx")
        with open(rbxmx_path, "w", encoding="utf-8") as fh:
            fh.write(build_rbxmx(settings.model_name, pieces,
                                 settings.anchored))
        manifest = build_manifest(settings_snapshot(settings), grid, mps,
                                  pieces)
        manifest["warnings"] = warnings
        with open(os.path.join(out_dir, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        for w in warnings:
            LOG.warning("%s", w)
            report({'WARNING'}, w)
        LOG.info("Wrote %s + manifest.json (%d piece(s), %d tile cell(s))",
                 os.path.basename(rbxmx_path), len(pieces), len(tile_pieces))
        return len(pieces), len(tile_pieces), out_dir, warnings

    finally:
        # Non-destructive guarantee: every temp datablock is removed and the
        # user's selection restored, even on failure.
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


# ---------------------------------------------------------------------------
# Grid preview overlay
# ---------------------------------------------------------------------------

_PREVIEW = {"handler": None, "coords": None, "batch": None, "shader": None}


def _draw_preview():
    coords = _PREVIEW.get("coords")
    if not coords:
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
    batch = _PREVIEW.get("batch")
    if batch is None:
        batch = batch_for_shader(shader, 'LINES', {"pos": coords})
        _PREVIEW["batch"] = batch
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(2.0)
    shader.bind()
    shader.uniform_float("color", (1.0, 0.5, 0.1, 0.9))
    batch.draw(shader)
    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')


def _remove_preview():
    if _PREVIEW["handler"] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_PREVIEW["handler"],
                                                  'WINDOW')
    _PREVIEW.update({"handler": None, "coords": None, "batch": None})


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
    meters_per_stud: bpy.props.FloatProperty(
        name="Meters per Stud",
        description="Blender meters that equal one Roblox stud "
                    "(Roblox convention: 0.28)",
        default=0.28, min=0.0001, soft_max=10.0, precision=4)
    max_tile_size: bpy.props.FloatProperty(
        name="Max Tile Size",
        description="Roblox MeshPart per-axis size limit, in studs",
        default=2048.0, min=8.0, max=2048.0)
    safety_margin: bpy.props.FloatProperty(
        name="Safety Margin",
        description="Subtracted from Max Tile Size before gridding, so "
                    "pieces never brush the hard limit (2048-48 = 2000)",
        default=48.0, min=0.0, max=1024.0)
    slice_z: bpy.props.BoolProperty(
        name="Slice Vertically",
        description="Also slice along Blender Z (world height) when the "
                    "world is taller than one tile",
        default=True)
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
        default='MERGED')
    collision_fidelity: bpy.props.EnumProperty(
        name="Collision",
        description="CollisionFidelity written to every MeshPart",
        items=_COLLISION_ENUM, default='DEFAULT')
    export_textures: bpy.props.BoolProperty(
        name="Export Textures",
        description="Embed textures in the FBX files (Roblox's 3D Importer "
                    "uploads them automatically), copy PBR maps "
                    "(color/normal/roughness/metalness) to textures/, and "
                    "add SurfaceAppearance placeholders to the .rbxmx",
        default=False)
    anchored: bpy.props.BoolProperty(
        name="Anchored",
        description="Export MeshParts with Anchored = true",
        default=True)
    selected_only: bpy.props.BoolProperty(
        name="Selected Only",
        description="Export only the selected mesh objects",
        default=False)
    model_name: bpy.props.StringProperty(
        name="Model Name",
        description="Name of the Model in the .rbxmx (and its filename)",
        default="BlenderWorld")
    output_dir: bpy.props.StringProperty(
        name="Output Folder",
        description="Folder receiving the .rbxmx, meshes/, textures/ and "
                    "manifest.json",
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
    """Toggle a viewport overlay showing how the world will be gridded"""
    bl_idname = "rbx.toggle_grid_preview"
    bl_label = "Preview Grid"

    def execute(self, context):
        if _PREVIEW["handler"] is not None:
            _remove_preview()
            _tag_redraw_3d(context)
            return {'FINISHED'}
        try:
            _, grid, warnings = compute_scene_grid(context,
                                                   context.scene.rbx_export)
        except ExportError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        _PREVIEW["coords"] = grid_line_coords(grid)
        _PREVIEW["batch"] = None
        _PREVIEW["handler"] = bpy.types.SpaceView3D.draw_handler_add(
            _draw_preview, (), 'WINDOW', 'POST_VIEW')
        for w in warnings:
            self.report({'WARNING'}, w)
        nx, ny, nz = grid.divisions
        self.report({'INFO'}, f"Grid: {nx} x {ny} x {nz} "
                              f"({grid.tile_count} tile(s))")
        _tag_redraw_3d(context)
        return {'FINISHED'}


class RBX_OT_dry_run(bpy.types.Operator):
    """Report the grid, tile sizes and piece estimate without writing files"""
    bl_idname = "rbx.dry_run"
    bl_label = "Dry Run"

    def execute(self, context):
        settings = context.scene.rbx_export
        try:
            objects, grid, warnings = compute_scene_grid(context, settings)
        except ExportError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        depsgraph = context.evaluated_depsgraph_get()
        estimate = estimate_piece_counts(objects, grid, depsgraph,
                                         settings.grouping_mode)
        mps = settings.meters_per_stud
        ext_studs = tuple(
            (grid.bounds_max[a] - grid.bounds_min[a]) / mps for a in range(3))
        tile_studs = tuple(t / mps for t in grid.tile_size)
        lines = [
            f"Objects: {len(objects)}",
            (f"World size (studs, Blender XYZ): {ext_studs[0]:.1f} x "
             f"{ext_studs[1]:.1f} x {ext_studs[2]:.1f}"),
            (f"Grid: {grid.divisions[0]} x {grid.divisions[1]} x "
             f"{grid.divisions[2]} = {grid.tile_count} tile(s)"),
            (f"Tile size (studs): {tile_studs[0]:.1f} x {tile_studs[1]:.1f} "
             f"x {tile_studs[2]:.1f}"),
            (f"Estimated pieces ({settings.grouping_mode.lower()}, "
             f"bbox-based): {estimate}"),
        ]
        lines += [f"Warning: {w}" for w in warnings]

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
    """Slice the world into tiles and export FBX + .rbxmx + manifest"""
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

        box = layout.box()
        box.label(text="Scale & Grid", icon='GRID')
        box.prop(settings, "meters_per_stud")
        box.prop(settings, "max_tile_size")
        box.prop(settings, "safety_margin")
        box.prop(settings, "slice_z")

        box = layout.box()
        box.label(text="MeshParts", icon='MESH_CUBE')
        box.prop(settings, "grouping_mode")
        box.prop(settings, "collision_fidelity")
        box.prop(settings, "anchored")
        box.prop(settings, "export_textures")

        box = layout.box()
        box.label(text="Output", icon='EXPORT')
        box.prop(settings, "model_name")
        box.prop(settings, "output_dir")
        box.prop(settings, "selected_only")
        box.prop(settings, "debug_logging")

        row = layout.row(align=True)
        row.operator(RBX_OT_toggle_grid_preview.bl_idname,
                     icon='MESH_GRID',
                     depress=_PREVIEW["handler"] is not None)
        row.operator(RBX_OT_dry_run.bl_idname, icon='CONSOLE')
        layout.operator(RBX_OT_export_world.bl_idname, icon='EXPORT')

        ob = context.object
        if ob is not None and ob.type == 'MESH':
            box = layout.box()
            box.label(text=f"Active Object: {ob.name}", icon='OBJECT_DATA')
            box.prop(ob, "rbx_exclude")
            sub = box.column()
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
    RBX_PT_exporter,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.rbx_export = bpy.props.PointerProperty(
        type=RBXExportSettings)
    bpy.types.Object.rbx_exclude = bpy.props.BoolProperty(
        name="Exclude from Export",
        description="Skip this object when exporting to Roblox",
        default=False)
    bpy.types.Object.rbx_collision_override = bpy.props.EnumProperty(
        name="Collision Override",
        description="Per-object CollisionFidelity (Per Object grouping "
                    "mode only)",
        items=[('INHERIT', "Inherit Scene Setting",
                "Use the scene-level collision fidelity")] + _COLLISION_ENUM,
        default='INHERIT')


def unregister():
    _remove_preview()
    del bpy.types.Object.rbx_collision_override
    del bpy.types.Object.rbx_exclude
    del bpy.types.Scene.rbx_export
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":  # allows running from Blender's text editor
    register()
