# Roblox World Exporter (Blender add-on)

Export large Blender worlds to Roblox. Roblox MeshParts max out at
**2048 × 2048 × 2048 studs** — this add-on measures your world, slices it into
an **even grid** of tiles (equal-size tiles, never leftover slivers), and
exports everything so the map reassembles in Roblox Studio at exact scale with
no gaps or overlaps.

**File:** `roblox_world_exporter.py` · **Blender:** 3.6+ (incl. 4.x) · no
external dependencies.

## What an export produces

```
<output folder>/
├── BlenderWorld.rbxmx     placement scaffold: one MeshPart per piece, exact
│                          CFrame + size in studs, CollisionFidelity set,
│                          MeshId placeholders, optional SurfaceAppearance
├── manifest.json          piece → FBX file → grid cell → stud position/size
├── meshes/*.fbx           one FBX per piece (baked in studs, centered)
├── textures/*.png         PBR maps (only with Export Textures on)
└── export_log.txt         only with Debug Logging on
```

`.rbxmx` files cannot embed mesh geometry — MeshParts reference uploaded
assets. That's why meshes ship as FBX plus a scaffold that already knows where
everything goes.

## Install

1. Blender → **Edit → Preferences → Add-ons → Install…**
2. Pick `roblox_world_exporter.py`, enable **Roblox World Exporter**.
3. The panel appears in the 3D Viewport sidebar (**N** key) → **Roblox** tab.

## Usage

1. Set **Meters per Stud** (default `0.28`, the Roblox human-scale
   convention; use `1.0` if you model at 1 unit = 1 stud).
2. Set the **Output Folder** (save your .blend first if you keep the default
   relative path).
3. Click **Preview Grid** to see the slicing overlay in the viewport, and
   **Dry Run** for a popup with world size, grid, tile size, and piece count —
   nothing is written.
4. Click **Export for Roblox**.

Your scene is never modified — all slicing happens on evaluated copies
(modifiers applied), which are deleted afterwards.

### Getting it into Roblox Studio

1. Open the **3D Importer** in Studio and import every FBX under `meshes/`
   (keep the importer's default **Studs** scale unit). With *Export Textures*
   on, textures are embedded in each FBX and upload automatically.
2. Right-click **Workspace → Insert From File…** and insert the `.rbxmx`.
3. For each MeshPart, paste the uploaded mesh's asset id into its **MeshId**
   property — names match the FBX filenames (see `manifest.json`). The part
   snaps to the correct position and size automatically, because each mesh is
   exported centered and pre-scaled in studs.
4. (Textures) Upload the files under `textures/` and paste their asset ids
   into each **SurfaceAppearance** map (placeholders are pre-created).

## Panel options

| Option | Meaning |
| --- | --- |
| Meters per Stud | Unit scale; studs = meters ÷ this value |
| Max Tile Size | Roblox's per-axis limit, default 2048 studs |
| Safety Margin | Subtracted before gridding (2048 − 48 = 2000 effective) so pieces never brush the hard cap |
| Slice Vertically | Also slice along world height for very tall maps |
| Grouping | **Merged per Tile** (one MeshPart per tile) or **Per Object** (each object stays its own MeshPart, sliced only where it straddles a grid line) |
| Collision | CollisionFidelity for every MeshPart: Default / Hull / Box / Precise |
| Anchored | Export parts anchored (recommended for maps) |
| Export Textures | Embed textures in FBX + copy PBR maps + SurfaceAppearance placeholders |
| Selected Only | Export only selected mesh objects |
| Debug Logging | Verbose console output + `export_log.txt` |

Per-object controls (active object box at the bottom of the panel):
**Exclude from Export** and a **Collision Override** (Per Object mode).

## Coordinates & math (for the curious)

- Blender `(x, y, z)` Z-up meters → Roblox `(x, z, −y)` Y-up studs.
- Grid: per axis, divisions = ⌈extent ÷ effective tile size⌉, tile size =
  extent ÷ divisions — even by construction. A 5000-stud map becomes 3 tiles
  of ~1666.7 studs.
- Meshes are exported centered at their bounds center with vertices pre-scaled
  to studs, so when Roblox recenters an imported mesh its size and pivot match
  the scaffold's CFrame/size exactly.

## Limitations / notes

- Roblox guidance is ~10k triangles per mesh; the exporter warns per piece
  but does not decimate. Very dense worlds may need a Decimate modifier first.
- Slice cuts are open (not capped) — invisible from outside for solid worlds,
  and collision works fine with Default fidelity.
- Custom split normals are recomputed after slicing (smooth/flat shading is
  preserved).
- SurfaceAppearance supports one texture set per MeshPart; the first material
  with usable Principled BSDF maps wins (all are listed in `manifest.json`).
- Exports with hundreds of tiles work but block the UI while running; watch
  the progress cursor. A warning appears above 512 tiles, hard stop at 4096.

## Development

Pure math/XML helpers (grid computation, coordinate conversion, `.rbxmx`
generation) are importable without Blender and covered by a unittest suite
that stubs `bpy` — run it with plain `python3`.
