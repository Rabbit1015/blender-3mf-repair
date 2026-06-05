bl_info = {
    "name": "3MF Repair for 3D Printing",
    "version": (1, 1, 0),
    "blender": (4, 5, 0),
    "location": "File > Import/Export | Sidebar > 3MF Repair",
    "description": "Import, repair non-manifold meshes, and export .3mf files with color preservation",
    "category": "Import-Export",
}

import bpy
import bmesh
import xml.etree.ElementTree as ET
import zipfile
import json
import os
from mathutils import Matrix
from mathutils.kdtree import KDTree
from bpy.props import StringProperty, BoolProperty, FloatProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper

NS_3MF  = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
NS_MAT  = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"
COLOR_ATTR    = "3mf_color"     # FLOAT_COLOR, viewport display
FILAMENT_ATTR = "3mf_filament"  # INT, per-loop filament index (0-based)
DEFAULT_COLOR = (0.8, 0.8, 0.8, 1.0)


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def parse_color(hex_str):
    """#RRGGBB[AA] → (r, g, b, a) floats."""
    h = hex_str.lstrip('#')
    try:
        if len(h) == 6:
            return (int(h[0:2],16)/255, int(h[2:4],16)/255, int(h[4:6],16)/255, 1.0)
        if len(h) == 8:
            return tuple(int(h[i:i+2],16)/255 for i in range(0,8,2))
    except ValueError:
        pass
    return DEFAULT_COLOR


def color_to_hex(r, g, b, a=1.0):
    return "#{:02X}{:02X}{:02X}{:02X}".format(
        int(r*255), int(g*255), int(b*255), int(a*255))


def decode_paint_color_index(value_str):
    """BambuStudio paint_color → integer filament index (0-based)."""
    s = value_str.strip()
    try:
        if s.upper().endswith('C'):
            prefix = s[:-1]
            return int(prefix, 16) if prefix else 0
        return int(s, 16)
    except (ValueError, IndexError):
        return 0


def decode_paint_color(value_str, filament_colors):
    idx = decode_paint_color_index(value_str)
    if 0 <= idx < len(filament_colors):
        return filament_colors[idx]
    return DEFAULT_COLOR


# ---------------------------------------------------------------------------
# 3MF matrix helper
# ---------------------------------------------------------------------------

def parse_3mf_transform(transform_str):
    """
    3MF stores a row-major 3×4 transform: A11..A33 then Tx Ty Tz.
    Convert to a Blender 4×4 column-major Matrix.
    """
    m = [float(v) for v in transform_str.split()]
    if len(m) != 12:
        return Matrix.Identity(4)
    return Matrix([
        [m[0], m[3], m[6], m[9]],
        [m[1], m[4], m[7], m[10]],
        [m[2], m[5], m[8], m[11]],
        [0,    0,    0,    1],
    ])


# ---------------------------------------------------------------------------
# 3MF parsing  (handles BambuStudio component sub-files + paint_color)
# ---------------------------------------------------------------------------

def parse_3mf(filepath):
    """
    Returns (vertices, triangles, tri_colors, tri_filaments, filament_colors, world_matrix):
      vertices         — list of (x, y, z)
      triangles        — list of (v1, v2, v3)
      tri_colors       — list of (r, g, b, a) per triangle (for viewport)
      tri_filaments    — list of int filament index per triangle (for export)
      filament_colors  — list of (r, g, b, a) indexed by filament
      world_matrix     — mathutils.Matrix
    """
    with zipfile.ZipFile(filepath, 'r') as zf:
        namelist = zf.namelist()

        # BambuStudio filament palette from project settings
        filament_colors = []
        if 'Metadata/project_settings.config' in namelist:
            try:
                with zf.open('Metadata/project_settings.config') as f:
                    cfg = json.load(f)
                for hc in cfg.get('filament_colour', []):
                    filament_colors.append(parse_color(hc))
            except Exception:
                pass

        # Parse main model file
        main_path = _find_model_path(zf, namelist)
        with zf.open(main_path) as f:
            main_root = ET.parse(f).getroot()

        ns_m    = {'m': NS_3MF}
        ns_prod = f'{{{NS_PROD}}}'

        resources = main_root.find('m:resources', ns_m)
        if resources is None:
            raise ValueError("No <resources> in main model")

        # Standard color groups in main file
        color_groups = _parse_color_groups(resources, ns_m)

        # Component references: objectid → (zip_path, transform_str)
        components = {}
        for obj_elem in resources.findall('m:object', ns_m):
            for comp in obj_elem.findall('.//m:component', ns_m):
                path = comp.get(f'{ns_prod}path')
                if path:
                    path = path.lstrip('/')
                    obj_id = comp.get('objectid', '')
                    tfm    = comp.get('transform', '1 0 0 0 1 0 0 0 1 0 0 0')
                    components[obj_id] = (path, tfm)

        # Build-item world transform (first item only)
        world_matrix = Matrix.Identity(4)
        build = main_root.find('m:build', ns_m)
        if build is not None:
            item = build.find('m:item', ns_m)
            if item is not None:
                tfm_str = item.get('transform', '')
                if tfm_str:
                    world_matrix = parse_3mf_transform(tfm_str)

        vertices      = []
        triangles     = []
        tri_colors    = []
        tri_filaments = []

        # --- Case 1: BambuStudio component sub-files ---
        if components:
            for obj_id, (comp_path, comp_tfm_str) in components.items():
                if comp_path not in namelist:
                    continue
                with zf.open(comp_path) as f:
                    sub_root = ET.parse(f).getroot()
                sub_resources = sub_root.find('m:resources', ns_m)
                if sub_resources is None:
                    continue
                local_cg = _parse_color_groups(sub_resources, ns_m)
                _parse_objects(sub_resources, ns_m, vertices, triangles,
                               tri_colors, tri_filaments, local_cg, filament_colors)

        # --- Case 2: Mesh directly in main file ---
        if not vertices:
            _parse_objects(resources, ns_m, vertices, triangles,
                           tri_colors, tri_filaments, color_groups, filament_colors)

    return vertices, triangles, tri_colors, tri_filaments, filament_colors, world_matrix


def _find_model_path(zf, namelist):
    for candidate in ('3D/3dmodel.model', '3d/3dmodel.model'):
        if candidate in namelist:
            return candidate
    for name in namelist:
        if name.endswith('.model'):
            return name
    raise ValueError("No .model file found in 3MF archive")


def _parse_color_groups(resources_elem, ns_m):
    """Parse <basematerials> and material-extension <colorgroup> elements."""
    groups = {}
    for bm in resources_elem.findall('m:basematerials', ns_m):
        gid = bm.get('id')
        groups[gid] = [parse_color(b.get('displaycolor', '#CCCCCC'))
                       for b in bm.findall('m:base', ns_m)]
    for cg in resources_elem.findall(f'{{{NS_MAT}}}colorgroup'):
        gid = cg.get('id')
        groups[gid] = [parse_color(c.get('color', '#CCCCCC'))
                       for c in cg.findall(f'{{{NS_MAT}}}color')]
    return groups


def _parse_objects(resources_elem, ns_m, vertices, triangles,
                   tri_colors, tri_filaments, color_groups, filament_colors):
    """Append mesh data from all model objects in a resources element."""
    for obj_elem in resources_elem.findall('m:object', ns_m):
        if obj_elem.get('type', 'model') not in ('model', 'solidsupport'):
            continue
        mesh_elem = obj_elem.find('m:mesh', ns_m)
        if mesh_elem is None:
            continue

        offset = len(vertices)
        obj_pid    = obj_elem.get('pid')
        obj_pindex = int(obj_elem.get('pindex', 0))

        verts_elem = mesh_elem.find('m:vertices', ns_m)
        if verts_elem is None:
            continue
        for v in verts_elem.findall('m:vertex', ns_m):
            vertices.append((float(v.get('x', 0)),
                             float(v.get('y', 0)),
                             float(v.get('z', 0))))

        tris_elem = mesh_elem.find('m:triangles', ns_m)
        if tris_elem is None:
            continue
        for tri in tris_elem.findall('m:triangle', ns_m):
            triangles.append((int(tri.get('v1')) + offset,
                              int(tri.get('v2')) + offset,
                              int(tri.get('v3')) + offset))

            # BambuStudio paint_color takes priority over standard pid/p1
            paint_color = tri.get('paint_color')
            if paint_color is not None and filament_colors:
                fidx = decode_paint_color_index(paint_color)
                tri_filaments.append(fidx)
                tri_colors.append(decode_paint_color(paint_color, filament_colors))
                continue

            pid = tri.get('pid', obj_pid)
            p1  = int(tri.get('p1', obj_pindex))
            if pid and pid in color_groups and p1 < len(color_groups[pid]):
                tri_filaments.append(p1)
                tri_colors.append(color_groups[pid][p1])
            else:
                tri_filaments.append(0)
                tri_colors.append(DEFAULT_COLOR)


# ---------------------------------------------------------------------------
# Blender mesh helpers
# ---------------------------------------------------------------------------

def make_mesh_object(name, vertices, triangles, tri_colors, tri_filaments,
                     filament_colors, world_matrix, context):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], triangles)
    mesh.update()

    has_filaments = any(f != 0 for f in tri_filaments) if tri_filaments else False
    has_colors    = any(c != DEFAULT_COLOR for c in tri_colors) if tri_colors else False

    # Store filament color palette on the mesh so export can round-trip it
    if filament_colors:
        mesh["3mf_filament_colors"] = json.dumps(
            [color_to_hex(r, g, b, a) for r, g, b, a in filament_colors])

    # INT attribute: per-loop filament index (0-based) — survives repair
    if has_filaments or tri_filaments:
        fi_attr = mesh.attributes.new(name=FILAMENT_ATTR, type='INT', domain='CORNER')
        for poly_idx, poly in enumerate(mesh.polygons):
            fidx = tri_filaments[poly_idx] if poly_idx < len(tri_filaments) else 0
            for loop_idx in poly.loop_indices:
                fi_attr.data[loop_idx].value = fidx

    # FLOAT_COLOR attribute for Blender viewport display (best-effort)
    if has_colors:
        ca = mesh.color_attributes.new(name=COLOR_ATTR, type='FLOAT_COLOR', domain='CORNER')
        for poly_idx, poly in enumerate(mesh.polygons):
            c = tri_colors[poly_idx] if poly_idx < len(tri_colors) else DEFAULT_COLOR
            for loop_idx in poly.loop_indices:
                ca.data[loop_idx].color = (c[0], c[1], c[2], c[3])

    obj = bpy.data.objects.new(name, mesh)
    obj.matrix_world = world_matrix
    context.scene.collection.objects.link(obj)
    return obj


def build_vert_filament_kd(mesh):
    """Snapshot per-vertex filament index and vertex positions into a KD-tree.
    Returns (kd, kd_filaments) where kd_filaments[i] = filament index for vertex i.
    Must be called BEFORE any bmesh operations that change the mesh.
    """
    n = len(mesh.vertices)
    kd = KDTree(n)
    kd_filaments = [-1] * n

    fi_attr = mesh.attributes.get(FILAMENT_ATTR)
    if fi_attr:
        for poly in mesh.polygons:
            for loop_idx in poly.loop_indices:
                vi = mesh.loops[loop_idx].vertex_index
                if kd_filaments[vi] == -1:
                    kd_filaments[vi] = fi_attr.data[loop_idx].value

    for i in range(n):
        if kd_filaments[i] == -1:
            kd_filaments[i] = 0

    for i, v in enumerate(mesh.vertices):
        kd.insert(v.co, i)
    kd.balance()

    non_zero = sum(1 for f in kd_filaments if f != 0)
    print(f"[3MF Filaments] captured {non_zero:,} / {n:,} non-zero filament indices")
    return kd, kd_filaments


def restore_filament_from_kd(mesh, filament_colors, kd, kd_filaments):
    """Rebuild FILAMENT_ATTR (INT) and COLOR_ATTR (FLOAT_COLOR) from KD-tree snapshot."""
    # Remove stale attributes (bm.to_mesh may have corrupted their sizes)
    for attr_name in [FILAMENT_ATTR]:
        old = mesh.attributes.get(attr_name)
        if old:
            mesh.attributes.remove(old)
    for attr_name in [COLOR_ATTR]:
        old = mesh.color_attributes.get(attr_name)
        if old:
            mesh.color_attributes.remove(old)

    fi_attr = mesh.attributes.new(name=FILAMENT_ATTR, type='INT', domain='CORNER')
    ca = mesh.color_attributes.new(name=COLOR_ATTR, type='FLOAT_COLOR', domain='CORNER')

    for poly in mesh.polygons:
        for loop_idx in poly.loop_indices:
            vi = mesh.loops[loop_idx].vertex_index
            pos = mesh.vertices[vi].co
            _, nearest_i, _ = kd.find(pos)
            fidx = kd_filaments[nearest_i]
            fi_attr.data[loop_idx].value = fidx
            if 0 <= fidx < len(filament_colors):
                c = filament_colors[fidx]
                ca.data[loop_idx].color = (c[0], c[1], c[2], c[3])

    # Verify
    sample = fi_attr.data[0].value if len(mesh.polygons) > 0 else -1
    unique = len(set(fi_attr.data[li].value
                     for p in list(mesh.polygons)[:200]
                     for li in p.loop_indices))
    print(f"[3MF Filaments] restored — first loop fidx={sample}, "
          f"unique indices in first 200 faces: {unique}")


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class IMPORT_OT_3mf_repair(bpy.types.Operator, ImportHelper):
    bl_idname  = "import_mesh.threemf_repair"
    bl_label   = "Import 3MF for Repair"
    bl_description = "Import a .3mf file with color data for mesh repair"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".3mf"
    filter_glob: StringProperty(default="*.3mf", options={'HIDDEN'})

    def execute(self, context):
        try:
            verts, tris, colors, filaments, filament_colors, matrix = parse_3mf(self.filepath)
            name = os.path.splitext(os.path.basename(self.filepath))[0]
            obj  = make_mesh_object(name, verts, tris, colors, filaments,
                                    filament_colors, matrix, context)
            context.view_layer.objects.active = obj
            obj.select_set(True)
            # Frame the view on the imported object
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    with context.temp_override(area=area):
                        bpy.ops.view3d.view_selected()
                    break
            self.report({'INFO'},
                f"Imported {len(verts):,} verts, {len(tris):,} faces — "
                f"{len(filament_colors)} filaments")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, str(e))
            import traceback; traceback.print_exc()
            return {'CANCELLED'}


class CHECK_OT_mesh(bpy.types.Operator):
    bl_idname  = "mesh.check_3mf"
    bl_label   = "Check Mesh"
    bl_description = "Report non-manifold edges and open holes"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        mesh = context.active_object.data
        bm = bmesh.new()
        bm.from_mesh(mesh)
        non_manifold = [e for e in bm.edges if not e.is_manifold]
        boundary     = [e for e in bm.edges if e.is_boundary]
        nm_verts     = [v for v in bm.verts if not v.is_manifold]
        bm.free()
        if non_manifold or nm_verts:
            self.report({'WARNING'},
                f"{len(non_manifold)} non-manifold edges, "
                f"{len(boundary)} open boundary edges, "
                f"{len(nm_verts)} non-manifold vertices "
                f"(OrcaSlicer may count ~{len(nm_verts) * 2} NM edges)")
        else:
            self.report({'INFO'}, "Mesh is fully manifold — ready for 3D printing")
        return {'FINISHED'}


class REPAIR_OT_mesh(bpy.types.Operator):
    bl_idname  = "mesh.repair_3mf"
    bl_label   = "Repair Mesh"
    bl_description = "Remove duplicate verts, fill holes, fix normals, preserve colors"
    bl_options = {'REGISTER', 'UNDO'}

    merge_dist: FloatProperty(
        name="Merge Distance",
        description="Max distance between vertices to treat as duplicates",
        default=0.001, min=0.0, max=1.0, precision=4)
    fill_holes: BoolProperty(name="Fill Holes", default=True)
    recalc_normals: BoolProperty(name="Recalculate Normals", default=True)

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj  = context.active_object
        mesh = obj.data

        # Snapshot filament indices BEFORE any mesh changes (bm.to_mesh corrupts them).
        kd, kd_filaments = build_vert_filament_kd(mesh)
        filament_colors = []
        fc_json = mesh.get("3mf_filament_colors")
        if fc_json:
            filament_colors = [parse_color(h) for h in json.loads(fc_json)]

        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        # --- Step 1: Remove duplicate vertices ---
        before_verts = len(bm.verts)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=self.merge_dist)
        removed = before_verts - len(bm.verts)

        # --- Step 2: Eliminate ALL multi-face edges ---
        # Pass A: delete faces touching 2+ multi-face edges (the dense overlaps).
        # Pass B: lower threshold to 1 for stubborn single-nm-edge faces that
        #         step A misses.  Each pass cleans wire edges immediately.
        deleted_multi = 0
        from collections import defaultdict

        def _del_multiface(bm, threshold):
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            face_nm = defaultdict(int)
            for e in bm.edges:
                if len(e.link_faces) > 2:
                    for f in e.link_faces:
                        face_nm[f] += 1
            bad = [f for f, c in face_nm.items() if c >= threshold]
            if bad:
                bmesh.ops.delete(bm, geom=bad, context='FACES_ONLY')
                # Remove wire edges (0 faces) left when all faces of an edge
                # were deleted — they break boundary loops for holes_fill.
                bm.edges.ensure_lookup_table()
                wire = [e for e in bm.edges if len(e.link_faces) == 0]
                if wire:
                    bmesh.ops.delete(bm, geom=wire, context='EDGES')
            return len(bad)

        # Pass A: threshold 2 (obvious overlaps)
        for _ in range(20):
            n = _del_multiface(bm, 2)
            deleted_multi += n
            if n == 0:
                break

        # Pass B: threshold 1 (single-edge offenders missed by pass A)
        for _ in range(20):
            n = _del_multiface(bm, 1)
            deleted_multi += n
            if n == 0:
                break

        # Clean isolated vertices left after wire-edge removal
        bm.verts.ensure_lookup_table()
        wire_v = [v for v in bm.verts if len(v.link_edges) == 0]
        if wire_v:
            bmesh.ops.delete(bm, geom=wire_v, context='VERTS')

        bm.edges.ensure_lookup_table()
        nm_a = sum(1 for e in bm.edges if not e.is_manifold)
        bd_a = sum(1 for e in bm.edges if e.is_boundary)
        print(f"[3MF] after step2: deleted {deleted_multi} faces, "
              f"{nm_a} NM remaining ({bd_a} boundary)")

        # --- Step 3: Fix any non-contiguous edges (2 faces, wrong winding) ---
        # These are distinct from multi-face; they appear only after all
        # multi-face edges are resolved.
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        nc_faces = set()
        for e in bm.edges:
            if (not e.is_manifold and not e.is_boundary
                    and len(e.link_faces) == 2):
                f1, f2 = e.link_faces
                if f1 in nc_faces or f2 in nc_faces:
                    continue
                s1 = sum(1 for ed in f1.edges if ed.is_manifold)
                s2 = sum(1 for ed in f2.edges if ed.is_manifold)
                nc_faces.add(f1 if s1 <= s2 else f2)
        deleted_nc = len(nc_faces)
        if nc_faces:
            bmesh.ops.delete(bm, geom=list(nc_faces), context='FACES_ONLY')
            bm.edges.ensure_lookup_table()
            wire_e2 = [e for e in bm.edges if len(e.link_faces) == 0]
            if wire_e2:
                bmesh.ops.delete(bm, geom=wire_e2, context='EDGES')
        print(f"[3MF] step3: deleted {deleted_nc} non-contiguous faces")

        # --- Step 4: Fill boundary holes ---
        # Two-algorithm approach: holes_fill handles most loops; triangle_fill
        # handles stubborn non-planar or complex loops that holes_fill skips.
        filled_new = 0

        def _fill_and_clean(bm):
            """Fill one round using triangle_fill only — produces triangles
            directly, avoiding n-gon faces whose export-time triangulation
            can create coincident-diagonal multi-face edges in the 3MF."""
            bm.edges.ensure_lookup_table()
            be = [e for e in bm.edges if e.is_boundary]
            if not be:
                return 0, 0

            res = bmesh.ops.triangle_fill(bm, use_beauty=True, use_dissolve=False, edges=be)
            n = len([g for g in res.get('geom', []) if isinstance(g, bmesh.types.BMFace)])

            # Remove degenerate zero-area faces
            bm.faces.ensure_lookup_table()
            degen = [f for f in bm.faces if f.calc_area() < 1e-14]
            if degen:
                bmesh.ops.delete(bm, geom=degen, context='FACES_ONLY')

            # Safety: remove multi-face edges fill may have created
            cleaned = 0
            for _ in range(5):
                c = _del_multiface(bm, 1)
                cleaned += c
                if c == 0:
                    break
            return n, cleaned

        for _fill_round in range(10):
            bm.edges.ensure_lookup_table()
            if not any(e.is_boundary for e in bm.edges):
                break
            n, cleaned = _fill_and_clean(bm)
            filled_new += n
            print(f"[3MF] fill round {_fill_round}: +{n} faces, safety -{cleaned}")
            if n == 0 and cleaned == 0:
                break

        # Last resort: delete faces whose boundary edges no fill algorithm could close.
        # After deletion the former manifold-neighbours become boundary; a second fill
        # round below handles those new (simpler) loops entirely inside bmesh so we
        # never need Edit Mode — avoiding the n-gon / duplicate-face issues that
        # Edit Mode fill() / fill_holes() introduced in exported 3MF files.
        bm.edges.ensure_lookup_table()
        stubborn = [e for e in bm.edges if e.is_boundary]
        if stubborn:
            print(f"[3MF] last resort: removing {len(stubborn)} unfillable boundary edges")
            stub_faces = list({f for e in stubborn for f in e.link_faces})
            bmesh.ops.delete(bm, geom=stub_faces, context='FACES_ONLY')
            bm.edges.ensure_lookup_table()
            wire = [e for e in bm.edges if len(e.link_faces) == 0]
            if wire:
                bmesh.ops.delete(bm, geom=wire, context='EDGES')
            bm.verts.ensure_lookup_table()
            isolated = [v for v in bm.verts if len(v.link_edges) == 0]
            if isolated:
                bmesh.ops.delete(bm, geom=isolated, context='VERTS')

            # Second fill round: the new boundary loops left by last-resort deletion
            # are geometrically simpler than the originals; bmesh can usually fill them.
            for _r2 in range(10):
                bm.edges.ensure_lookup_table()
                if not any(e.is_boundary for e in bm.edges):
                    break
                n2, c2 = _fill_and_clean(bm)
                filled_new += n2
                print(f"[3MF] fill-r2 round {_r2}: +{n2} faces, safety -{c2}")
                if n2 == 0 and c2 == 0:
                    break

            # If any boundary loops still survived, delete them too.
            bm.edges.ensure_lookup_table()
            stubborn2 = [e for e in bm.edges if e.is_boundary]
            if stubborn2:
                stub2_faces = list({f for e in stubborn2 for f in e.link_faces})
                bmesh.ops.delete(bm, geom=stub2_faces, context='FACES_ONLY')
                bm.edges.ensure_lookup_table()
                wire2 = [e for e in bm.edges if len(e.link_faces) == 0]
                if wire2:
                    bmesh.ops.delete(bm, geom=wire2, context='EDGES')

        # Duplicate-face removal: fill ops can lay a new triangle over an existing
        # one; same vertex set + same winding → non-contiguous in OrcaSlicer.
        bm.faces.ensure_lookup_table()
        seen_faces = {}
        dup_faces = []
        for f in bm.faces:
            key = frozenset(v.index for v in f.verts)
            if key in seen_faces:
                dup_faces.append(f)
            else:
                seen_faces[key] = f
        if dup_faces:
            bmesh.ops.delete(bm, geom=dup_faces, context='FACES_ONLY')
            bm.edges.ensure_lookup_table()
            wire_d = [e for e in bm.edges if len(e.link_faces) == 0]
            if wire_d:
                bmesh.ops.delete(bm, geom=wire_d, context='EDGES')
            print(f"[3MF] removed {len(dup_faces)} duplicate faces")

        # Fix non-contiguous (wrong-winding) fill faces.
        # triangle_fill doesn't guarantee its output winding matches the surrounding
        # mesh.  Now that the mesh has 0 boundary and 0 multi-face edges, the BFS
        # inside recalc_face_normals can safely propagate consistent orientation
        # across the whole mesh — the few wrong-winding fill triangles get corrected
        # without cascading side effects.  We skip it if any boundary or multi-face
        # edges remain, since those break the BFS and could flip half the mesh.
        bm.edges.ensure_lookup_table()
        pre_recalc_nm = sum(1 for e in bm.edges if not e.is_manifold)
        still_boundary  = sum(1 for e in bm.edges if e.is_boundary)
        still_multiface = sum(1 for e in bm.edges if len(e.link_faces) > 2)
        ran_recalc = False
        if still_boundary == 0 and still_multiface == 0:
            bm.faces.ensure_lookup_table()
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
            ran_recalc = True
        bm.edges.ensure_lookup_table()
        post_recalc_nm = sum(1 for e in bm.edges if not e.is_manifold)

        bm.to_mesh(mesh)
        bm.free()
        mesh.update()

        # --- Step 5b: Fix non-manifold (bowtie) vertices ---

        def _split_nm_vert(bm_local, v):
            if v.is_manifold:
                return 0
            faces = list(v.link_faces)
            if not faces:
                return 0
            fans, remaining = [], set(faces)
            while remaining:
                seed = next(iter(remaining))
                fan, q = set(), [seed]
                while q:
                    f = q.pop()
                    if f in fan:
                        continue
                    fan.add(f)
                    remaining.discard(f)
                    for e in f.edges:
                        if v not in e.verts:
                            continue
                        for nf in e.link_faces:
                            if nf in remaining:
                                q.append(nf)
                fans.append(list(fan))
            if len(fans) <= 1:
                return 0
            extra = 0
            for fan_faces in fans[1:]:
                nv = bm_local.verts.new(v.co)
                for f in fan_faces:
                    fverts = [nv if fv == v else fv for fv in f.verts]
                    mat = f.material_index
                    try:
                        bm_local.faces.remove(f)
                        bm_local.faces.new(fverts).material_index = mat
                    except Exception:
                        pass
                extra += 1
            return extra

        bm3 = bmesh.new()
        bm3.from_mesh(mesh)
        bm3.edges.ensure_lookup_table()
        bm3.verts.ensure_lookup_table()
        pre_bm3_nm   = sum(1 for e in bm3.edges if not e.is_manifold)
        pre_bm3_nmv  = sum(1 for v in bm3.verts if not v.is_manifold)
        fixed_fans   = sum(_split_nm_vert(bm3, v)
                           for v in [v for v in bm3.verts if not v.is_manifold])
        # The split leaves wire edges (0 faces) where the old bowtie vertex
        # connected to the fan that was moved to the new vertex.  Clean them.
        bm3.edges.ensure_lookup_table()
        wire3 = [e for e in bm3.edges if len(e.link_faces) == 0]
        if wire3:
            bmesh.ops.delete(bm3, geom=wire3, context='EDGES')
        bm3.verts.ensure_lookup_table()
        iso3 = [v for v in bm3.verts if len(v.link_edges) == 0]
        if iso3:
            bmesh.ops.delete(bm3, geom=iso3, context='VERTS')
        bm3.edges.ensure_lookup_table()
        bm3.verts.ensure_lookup_table()
        post_bm3_nm  = sum(1 for e in bm3.edges if not e.is_manifold)
        post_bm3_nmv = sum(1 for v in bm3.verts if not v.is_manifold)
        bm3.to_mesh(mesh)
        bm3.free()
        mesh.update()

        # --- Step 5c: Restore filament colours from the pre-repair snapshot ---
        restore_filament_from_kd(mesh, filament_colors, kd, kd_filaments)
        mesh.update()

        # Final NM count (edges + vertices)
        bm2 = bmesh.new()
        bm2.from_mesh(mesh)
        final_nm       = sum(1 for e in bm2.edges if not e.is_manifold)
        final_boundary = sum(1 for e in bm2.edges if e.is_boundary)
        final_nm_verts = sum(1 for v in bm2.verts if not v.is_manifold)
        bm2.free()

        self.report({'INFO'},
            f"Removed {removed} dup verts — deleted {deleted_multi} overlapping + "
            f"{deleted_nc} winding faces, filled {filled_new} hole faces — "
            f"{final_nm} NM edges / {final_boundary} open boundary / "
            f"{final_nm_verts} NM verts remaining")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def write_3mf(filepath, obj):
    """Export mesh with BambuStudio paint_color attributes so OrcaSlicer
    correctly assigns each face to a filament / extruder slot."""
    mesh = obj.data
    mesh.calc_loop_triangles()

    fi_attr = mesh.attributes.get(FILAMENT_ATTR)
    fc_json = mesh.get("3mf_filament_colors")
    filament_colors = [parse_color(h) for h in json.loads(fc_json)] if fc_json else []

    print(f"[3MF Export] filament attr: {fi_attr is not None}, "
          f"filament_colors: {len(filament_colors)}")

    # ---- build XML ----
    root = ET.Element('model')
    root.set('unit', 'millimeter')
    root.set('xml:lang', 'en-US')
    root.set('xmlns', NS_3MF)

    resources = ET.SubElement(root, 'resources')
    obj_elem  = ET.SubElement(resources, 'object')
    obj_elem.set('id', '2')
    obj_elem.set('type', 'model')
    mesh_elem = ET.SubElement(obj_elem, 'mesh')

    verts_elem = ET.SubElement(mesh_elem, 'vertices')
    for v in mesh.vertices:
        ve = ET.SubElement(verts_elem, 'vertex')
        ve.set('x', f'{v.co.x:.7f}')
        ve.set('y', f'{v.co.y:.7f}')
        ve.set('z', f'{v.co.z:.7f}')

    unique_fidx = set()
    tris_elem = ET.SubElement(mesh_elem, 'triangles')
    for tri in mesh.loop_triangles:
        te = ET.SubElement(tris_elem, 'triangle')
        te.set('v1', str(tri.vertices[0]))
        te.set('v2', str(tri.vertices[1]))
        te.set('v3', str(tri.vertices[2]))
        if fi_attr:
            fidx = fi_attr.data[tri.loops[0]].value
            unique_fidx.add(fidx)
            te.set('paint_color', f'{fidx:X}C')

    build = ET.SubElement(root, 'build')
    ET.SubElement(build, 'item').set('objectid', '2')

    print(f"[3MF Export] unique filament indices used: {sorted(unique_fidx)}")

    ET.indent(root, space='  ')
    model_xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 + ET.tostring(root, encoding='unicode'))

    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels"'
        ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="model"'
        ' ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship'
        ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"'
        ' Target="/3D/3dmodel.model" Id="rel0"/>\n'
        '</Relationships>'
    )

    # project_settings.config lets OrcaSlicer display the original filament colors
    project_cfg = {}
    if filament_colors:
        project_cfg['filament_colour'] = [
            color_to_hex(r, g, b, 1.0)[:7] for r, g, b, _ in filament_colors]

    with zipfile.ZipFile(filepath, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', content_types)
        zf.writestr('_rels/.rels', rels)
        zf.writestr('3D/3dmodel.model', model_xml)
        if project_cfg:
            zf.writestr('Metadata/project_settings.config', json.dumps(project_cfg))


class EXPORT_OT_3mf_repair(bpy.types.Operator, ExportHelper):
    bl_idname  = "export_mesh.threemf_repair"
    bl_label   = "Export Repaired 3MF"
    bl_description = "Export the active mesh as a .3mf file"
    bl_options = {'REGISTER'}

    filename_ext = ".3mf"
    filter_glob: StringProperty(default="*.3mf", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        try:
            write_3mf(self.filepath, context.active_object)
            self.report({'INFO'}, f"Exported to {os.path.basename(self.filepath)}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, str(e))
            import traceback; traceback.print_exc()
            return {'CANCELLED'}


# ---------------------------------------------------------------------------
# Sidebar panel
# ---------------------------------------------------------------------------

class VIEW3D_PT_3mf_repair(bpy.types.Panel):
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = '3MF Repair'
    bl_label       = "3MF Repair"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        layout.operator("import_mesh.threemf_repair", icon='IMPORT')
        layout.separator()

        if obj and obj.type == 'MESH':
            mesh = obj.data
            box  = layout.box()
            box.label(text=obj.name, icon='MESH_DATA')
            col = box.column(align=True)
            col.label(text=f"Verts:  {len(mesh.vertices):,}")
            col.label(text=f"Faces:  {len(mesh.polygons):,}")
            has_color = COLOR_ATTR in mesh.color_attributes
            col.label(text=f"Colors: {'yes' if has_color else 'none'}",
                      icon='BRUSHES_ALL' if has_color else 'X')

            layout.separator()
            layout.operator("mesh.check_3mf",   icon='VIEWZOOM')
            layout.operator("mesh.repair_3mf",  icon='TOOL_SETTINGS')
            layout.separator()
            layout.operator("export_mesh.threemf_repair", icon='EXPORT')
        else:
            layout.label(text="Select a mesh object", icon='INFO')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _menu_import(self, context):
    self.layout.operator("import_mesh.threemf_repair", text="3MF for Repair (.3mf)")

def _menu_export(self, context):
    self.layout.operator("export_mesh.threemf_repair", text="3MF Repaired (.3mf)")


classes = (
    IMPORT_OT_3mf_repair,
    CHECK_OT_mesh,
    REPAIR_OT_mesh,
    EXPORT_OT_3mf_repair,
    VIEW3D_PT_3mf_repair,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    bpy.types.TOPBAR_MT_file_export.remove(_menu_export)


if __name__ == "__main__":
    register()
