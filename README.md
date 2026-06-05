# Blender 3MF Repair Addon

A Blender 4.5 addon that imports BambuStudio `.3mf` files, repairs non-manifold mesh geometry, and exports back as an OrcaSlicer-compatible `.3mf` — with all filament colors preserved.

Built for multi-color FDM printing on machines like the Snapmaker U1 (11-color) using OrcaSlicer.

## What it does

- **Imports** BambuStudio `.3mf` files, including the component sub-file format (`3D/Objects/object_1.model`) used by "Image to 3D v2"
- **Decodes** `paint_color` attributes (`"0C"`, `"1C"`, ...) to preserve per-face filament assignments for up to 11 colors
- **Repairs** non-manifold mesh geometry in one click:
  - Removes duplicate vertices
  - Deletes faces causing multi-face edges (overlapping geometry)
  - Fills open boundary holes using triangle fill (no n-gon artifacts)
  - Recalculates face normals for consistent winding
  - Splits non-manifold (bowtie) vertices
- **Exports** as OrcaSlicer-compatible `.3mf` with `paint_color` attributes and `project_settings.config` filament colors

### Results on a real model

BambuStudio "Image to 3D v2" cat model — 164k vertices, 329k triangles, 11 filament colors:

| | Before | After |
|---|---|---|
| Non-manifold edges | 155 | **0** |
| Open boundary edges | 70 | **0** |
| Non-manifold vertices | 133 | **0** |
| OrcaSlicer errors | 20 | **0** |
| Colors preserved | — | **11 / 11** |

## Installation

1. Download [`3mf_repair_addon.py`](3mf_repair_addon.py)
2. In Blender: **Edit → Preferences → Add-ons → Install**
3. Navigate to the downloaded file and install
4. Enable the addon (search for "3MF Repair")

## Requirements

- Blender 4.5+
- Tested with BambuStudio `.3mf` files and OrcaSlicer on macOS

## Usage

### 1. Import
**File → Import → Import 3MF for Repair**

### 2. Repair
With the imported mesh selected, open the **N-panel sidebar → 3MF Repair tab**:
- **Check Mesh** — reports non-manifold edges, open boundary edges, and non-manifold vertices
- **Repair Mesh** — runs the full repair pipeline in one click

### 3. Export
**File → Export → Export 3MF (Repaired)**

Then open the exported file in OrcaSlicer — colors and geometry should be clean.

## How the repair pipeline works

1. **Remove doubles** — merges near-duplicate vertices within 0.001 mm
2. **Multi-face deletion** — finds edges shared by 3+ faces (overlapping geometry from AI mesh generation) and removes the offending faces in two passes (threshold ≥2, then ≥1)
3. **Non-contiguous fix** — removes faces with wrong winding relative to their neighbors
4. **Hole filling** — fills open boundary loops using `triangle_fill` (produces triangles directly, avoiding n-gon artifacts on export)
5. **Winding recalc** — runs `recalc_face_normals` on the full mesh once boundary and multi-face edges are resolved, correcting any fill faces with inverted normals
6. **Bowtie vertex split** — detects vertices connecting two disconnected face fans and splits them into separate vertices; cleans up resulting wire edges

## How the color pipeline works

OrcaSlicer ignores `<basematerials>` for multi-color display. This addon uses BambuStudio's `paint_color` triangle attribute (e.g. `paint_color="2C"` for filament slot 3). Filament hex colors are written to `Metadata/project_settings.config`.

Filament indices survive mesh repair via a KD-tree snapshot taken before any bmesh operations (since `bm.to_mesh()` corrupts generic mesh attributes when face count changes in Blender 4.5). After repair, indices are restored by nearest-vertex position lookup.

## Troubleshooting

**"Imported 0 verts, 0 faces"** — The file uses the BambuStudio component sub-file format. Make sure you're using **Import 3MF for Repair** (not Blender's built-in 3MF importer).

**Colors missing after export** — Ensure the mesh has the `3mf_filament` attribute (visible in Object Data Properties → Attributes). Re-import the original file if it's missing.

**OrcaSlicer still shows errors after repair** — Run **Check Mesh** first. If it reports 0 NM edges / 0 boundary / 0 NM verts, the geometry is clean and OrcaSlicer's error is a false positive that won't affect slicing.

## License

MIT — see [LICENSE](LICENSE)
