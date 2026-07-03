# Roblox World Exporter (Blender add-on)

Export large Blender worlds to Roblox. Roblox MeshParts max out at
**2048 × 2048 × 2048 studs** and mesh imports are rejected above
**~20,000 triangles** — this add-on measures your world, slices it into an
**even grid** of tiles (equal-size tiles, never leftover slivers),
auto-splits over-dense pieces, and exports everything so the map reassembles
in Roblox Studio at exact scale with no gaps or overlaps.

**File:** `roblox_world_exporter.py` · **Blender:** 3.6+ (incl. 4.x) · no
external dependencies.

## Install

1. Blender → **Edit → Preferences → Add-ons → Install…**
2. Pick `roblox_world_exporter.py`, enable **Roblox World Exporter**.
3. The panel appears in the 3D Viewport sidebar (**N** key) → **Roblox** tab.

## Quick start (Single FBX mode — recommended)

1. Leave **Mode** on *Single FBX (3D Importer)*.
2. Check the **Live Stats** box: world size in studs, segment counts, tile
   size, piece estimate — it updates as you edit the scene.
3. Optional: click **Preview Grid** — a live overlay that follows your edits
   and setting changes automatically (no re-toggling needed).
4. Set the **Output Folder** (save your .blend first if you keep the default
   relative path) and click **Export for Roblox**.
5. In Roblox Studio open the **3D Importer**, import the single
   `<ModelName>.fbx`, and keep the importer's defaults (*Import as Single
   Asset*, scene position/origin, *Studs* scale unit).

**The whole map appears in Roblox immediately, fully assembled** — every
piece keeps its world position inside the FBX, so there are no asset ids to
paste. With *Export Textures* on, textures are embedded and upload
automatically too.

Your scene is never modified — all slicing happens on evaluated copies
(modifiers applied), which are deleted afterwards.

## What an export produces

```
<output folder>/
├── BlenderWorld.fbx       Single FBX mode: the whole sliced map, assembled
├── manifest.json          piece → grid cell → stud position/size/triangles
├── textures/*.png         PBR maps (only with Export Textures on)
└── export_log.txt         only with Debug Logging on

# Per-Piece FBX + .rbxmx mode (advanced) adds:
├── meshes/*.fbx           one FBX per piece (centered, baked in studs)
├── BlenderWorld.rbxmx     MeshPart placeholder scaffold (see warning below)
└── place_pieces.lua       command-bar script that snaps pieces into place
```

### ⚠️ About the `.rbxmx` scaffold (advanced mode)

`.rbxmx` files **cannot embed mesh geometry** — a MeshPart's `MeshId` must
point at an uploaded asset, so the scaffold ships with `rbxassetid://0`
placeholders and its parts are **invisible** until real ids are pasted.
That's a Roblox format limitation, not a bug. Two ways to use the mode:

- **Easy:** import the FBX pieces with the 3D Importer, then paste and run
  `place_pieces.lua` in Studio's command bar — it finds every imported
  MeshPart by name, snaps it to its exact CFrame, anchors it, and applies
  the intended CollisionFidelity. No id pasting.
- **Manual:** insert the `.rbxmx`, upload each FBX, and paste each MeshId
  (names match; `manifest.json` maps everything).

If you just want the map in Roblox, use **Single FBX mode**.

## Panel reference

The panel is organized into collapsible tabs:

**Live Stats (always visible)** — object count, world size in studs, segment
grid (`3 × 1 × 1 = 3 tiles`), tile size in studs, estimated piece count, and
inline warnings. Updates live as you move/resize/delete objects.

**Scale & Grid** — *Meters per Stud* (default `0.28`; use `1.0` if you model
at 1 unit = 1 stud), *Max Tile Size* (2048), *Safety Margin* (48 → effective
2000), and **Slice Axes X/Y/Z** toggles so you can choose which directions
(sideways and/or vertically) get gridded.

**Pieces & Density** — *Grouping* (**Merged per Tile** = one MeshPart per
tile, or **Per Object** = each object stays its own MeshPart, sliced only
where it straddles a grid line), *Auto-Split Dense Pieces* + *Triangle
Limit* (default 20,000 — pieces over the limit are recursively halved so
Roblox accepts them).

**Roblox Settings** — *Map Origin* (**Keep Blender Origin** or **Center at
Origin**, which centers the footprint at Roblox `(0, y, 0)` with the lowest
point at ground level Y=0), *Collision*, *Anchored*, *Export Textures*.

**Output** — model name, output folder, *Selected Only*, *Debug Logging*,
and an **Open Output Folder** button.

**Active Object** — per-object *Exclude from Export* and *Collision
Override* (Per Object grouping mode).

## Coordinates & math (for the curious)

- Blender `(x, y, z)` Z-up meters → Roblox `(x, z, −y)` Y-up studs.
- Grid: per axis, divisions = ⌈extent ÷ effective tile size⌉, tile size =
  extent ÷ divisions — even by construction. A 5000-stud map becomes 3 tiles
  of ~1666.7 studs.
- Slicing uses recursive bisection (each cut halves the remaining tile
  range), so big maps slice in roughly O(mesh × log tiles) instead of
  O(mesh × tiles).
- Meshes are exported with vertices pre-scaled to studs, so sizes match
  exactly after import regardless of importer unit settings.

## Limitations / notes

- Roblox rejects meshes above ~20k triangles — Auto-Split handles this, but
  a piece that can't be reduced (e.g. a single gigantic dense blob after 8
  split levels) is flagged in the warnings; add a Decimate modifier.
- Slice cuts are open (not capped) — invisible from outside for solid
  worlds; collision works with Default fidelity.
- Custom split normals are recomputed after slicing (smooth/flat shading is
  preserved).
- SurfaceAppearance supports one texture set per MeshPart; the first
  material with usable Principled BSDF maps wins (all listed in
  `manifest.json`).
- Collision fidelity and Anchored can't travel inside an FBX — in Single
  FBX mode set them after import (bulk-select the MeshParts), or use
  scaffold mode's `place_pieces.lua` which applies both automatically.
- Exports with hundreds of tiles block the UI while running; watch the
  progress cursor. A warning appears above 512 tiles, hard stop at 4096.

## Development

Pure math/XML/Lua helpers (grid computation, coordinate conversion,
`.rbxmx` and `place_pieces.lua` generation) are importable without Blender
and covered by a unittest suite that stubs `bpy` — run it with plain
`python3`.
