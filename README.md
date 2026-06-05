# Blender 3MF Repair Addon

A Blender 4.5 addon that imports BambuStudio `.3mf` files, repairs non-manifold mesh geometry, and exports back as an OrcaSlicer-compatible `.3mf` — with all filament colors preserved.

Built for multi-color FDM printing on machines like the Snapmaker U1 (11-color) using OrcaSlicer.

## What it does

- **Imports** BambuStudio `.3mf` files, including the component sub-file format (`3D/Objects/object_1.model`) used by "Image to 3D v2"
- **Decodes** `paint_color` attributes (`"0C"`, `"1C"`, ...) to preserve per-face filament assignments for up to 11 colors
- **Repairs** non-manifold mesh geometry:
  - Removes duplicate vertices
  - Deletes faces causing multi-face edges (overlapping geometry)
  - Fills open boundary holes
  - Fixes non-contiguous edges (winding inconsistencies)
  - Splits non-manifold (bowtie) vertices
- **Exports** as OrcaSlicer-compatible `.3mf` with `paint_color` attributes and `project_settings.config` filament colors

## Installation

1. Download `3mf_repair_addon.py`
2. In Blender: **Edit → Preferences → Add-ons → Install**
3. Navigate to the downloaded file and install
4. Enable the addon (search for "3MF Repair")

## Requirements

- Blender 4.5+
- Tested with BambuStudio `.3mf` files and OrcaSlicer

## Usage

### Import
**File → Import → Import 3MF for Repair**

### Repair
With the imported mesh selected, open the **N-panel → 3MF Repair tab**:
- **Check Mesh** — reports non-manifold edges, open boundary edges, and non-manifold (bowtie) vertices
- **Repair Mesh** — runs all repair steps in one click

### Export
**File → Export → Export 3MF (Repaired)**

## How the color pipeline works

OrcaSlicer ignores `<basematerials>` for multi-color display. Instead, this addon uses BambuStudio's `paint_color` attribute on each triangle (e.g. `paint_color="2C"` for filament slot 3). Filament hex colors are written to `Metadata/project_settings.config`.

Filament indices survive mesh repair via a KD-tree snapshot taken before any bmesh operations. After repair, indices are restored by nearest-vertex position lookup.

## Tested on

- BambuStudio "Image to 3D v2" cat model (11 filament colors, 164k vertices, 329k triangles)
- Blender 4.5.3 on macOS
- OrcaSlicer on macOS

## License

MIT
