Blender add-on that exports large worlds to Roblox by slicing them into an
**even grid** so every piece fits Roblox's 2048×2048×2048-stud MeshPart limit
— no slivers, no gaps, exact scale.

## Install

1. Download **`roblox_world_exporter.py`** from the assets below.
2. Blender → **Edit → Preferences → Add-ons → Install…** → pick the file →
   enable **Roblox World Exporter**.
3. Open the 3D Viewport sidebar (**N** key) → **Roblox** tab.

## Highlights

- Even grid computation with configurable max tile size + safety margin
- Non-destructive slicing (modifiers applied on copies, scene untouched)
- One FBX per piece, pre-scaled to studs and centered; Z-up → Y-up handled
- `.rbxmx` scaffold with exact CFrames/sizes, CollisionFidelity, MeshId and
  SurfaceAppearance placeholders + `manifest.json` for asset-id pasting
- Merged-per-tile or per-object MeshParts, per-object collision overrides
- Viewport grid preview, dry-run report, optional texture export, debug log

See `ROBLOX_EXPORTER_README.md` in the repo for the full Studio workflow.

Requires Blender 3.6+ (4.x supported). No external dependencies.
