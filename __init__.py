import bpy
import bmesh
from mathutils import Vector, Matrix


# ------------------------------------------------------------------ #
# Core helpers
# ------------------------------------------------------------------ #
def _get_active_element_data(obj):
    """Return (world_center, world_normal) for the active vertex/edge/face, or None.

    Works in any of the three edit-mode select modes. Picks the active element
    if available, otherwise falls back to the most recently selected element of
    the appropriate type.
    """
    if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
        return None

    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    mw = obj.matrix_world
    normal_matrix = mw.to_3x3().inverted_safe().transposed()

    # Try the active element first (last-clicked element of any type)
    elem = bm.select_history.active if bm.select_history else None

    # Helper: pick the most recently selected element of a given bmesh type
    def _latest_selected(seq):
        sel = [x for x in seq if x.select]
        return sel[-1] if sel else None

    # If no usable active element, fall back based on current select mode
    select_mode = bm.select_mode  # set: {'VERT'}, {'EDGE'}, or {'FACE'}

    if elem is None or not getattr(elem, "select", False):
        if 'FACE' in select_mode:
            elem = _latest_selected(bm.faces)
        elif 'EDGE' in select_mode:
            elem = _latest_selected(bm.edges)
        elif 'VERT' in select_mode:
            elem = _latest_selected(bm.verts)

    if elem is None:
        return None

    # --- FACE ---
    if isinstance(elem, bmesh.types.BMFace):
        center_local = elem.calc_center_median()
        normal_local = elem.normal

    # --- EDGE ---
    elif isinstance(elem, bmesh.types.BMEdge):
        v1, v2 = elem.verts
        center_local = (v1.co + v2.co) * 0.5
        # Average the normals of the connected faces; this gives a stable
        # "outward" direction even for boundary edges (1 face) or wire
        # edges (0 faces, fall back to combined vertex normals).
        if elem.link_faces:
            n = Vector((0.0, 0.0, 0.0))
            for f in elem.link_faces:
                n += f.normal
            if n.length < 1e-6:
                n = (v1.normal + v2.normal)
            normal_local = n
        else:
            normal_local = (v1.normal + v2.normal)

        if normal_local.length < 1e-6:
            # Last-resort fallback: world Z
            normal_local = Vector((0.0, 0.0, 1.0))
        normal_local = normal_local.normalized()

    # --- VERT ---
    elif isinstance(elem, bmesh.types.BMVert):
        center_local = elem.co.copy()
        normal_local = elem.normal.copy()
        if normal_local.length < 1e-6:
            normal_local = Vector((0.0, 0.0, 1.0))
        normal_local = normal_local.normalized()

    else:
        return None

    center_world = mw @ center_local
    normal_world = (normal_matrix @ normal_local).normalized()
    return center_world, normal_world


def _align_view_to_face(context, center_world, normal_world):
    """Rotate and position the 3D viewport so it looks along -normal at the face."""
    area = context.area if (context.area and context.area.type == 'VIEW_3D') else None
    if area is None:
        for a in context.screen.areas:
            if a.type == 'VIEW_3D':
                area = a
                break
    if area is None:
        return False

    rv3d = area.spaces.active.region_3d
    if rv3d is None:
        return False

    view_z = normal_world.normalized()
    world_up = Vector((0.0, 0.0, 1.0))
    if abs(view_z.dot(world_up)) > 0.999:
        world_up = Vector((0.0, 1.0, 0.0))

    view_x = world_up.cross(view_z).normalized()
    view_y = view_z.cross(view_x).normalized()

    rot_matrix = Matrix((
        (view_x.x, view_y.x, view_z.x),
        (view_x.y, view_y.y, view_z.y),
        (view_x.z, view_y.z, view_z.z),
    ))

    rv3d.view_perspective = 'PERSP'
    rv3d.view_rotation = rot_matrix.to_quaternion()
    rv3d.view_location = center_world.copy()

    if rv3d.view_distance < 0.01:
        rv3d.view_distance = 5.0

    area.tag_redraw()
    return True


# ------------------------------------------------------------------ #
# Operator
# ------------------------------------------------------------------ #
class VIEW3D_OT_view_from_selected_face(bpy.types.Operator):
    """View from the selected face (align viewport to face normal)"""
    bl_idname = "view3d.view_from_selected_face"
    bl_label = "View From Selected"
    bl_description = "Align the 3D viewport to look along the active face's normal"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode == 'EDIT'

    def execute(self, context):
        obj = context.active_object
        data = _get_active_element_data(obj)
        if data is None:
            self.report({'WARNING'}, "No vertex, edge, or face selected.")
            return {'CANCELLED'}

        center_world, normal_world = data
        if not _align_view_to_face(context, center_world, normal_world):
            self.report({'WARNING'}, "Could not find a 3D viewport to align.")
            return {'CANCELLED'}

        return {'FINISHED'}


# ------------------------------------------------------------------ #
# Menu integration
# ------------------------------------------------------------------ #
def _is_mesh_edit_mode(context):
    """True only when we are in Edit Mode on a mesh object."""
    obj = context.active_object
    return obj is not None and obj.type == 'MESH' and obj.mode == 'EDIT'


def _menu_draw_view(self, context):
    """Append into the View3D > View menu — only in mesh Edit Mode."""
    if not _is_mesh_edit_mode(context):
        return
    layout = self.layout
    layout.separator()
    layout.operator(
        VIEW3D_OT_view_from_selected_face.bl_idname,
        text="View From Selected",
        icon='FACESEL',
    )


def _pie_menu_draw(self, context):
    """Fallback pie entry used only if the pie draw swap fails."""
    if not _is_mesh_edit_mode(context):
        return
    pie = self.layout.menu_pie()
    pie.operator(
        VIEW3D_OT_view_from_selected_face.bl_idname,
        text="View From Selected",
        icon='FACESEL',
    )


# --- Pie menu override: swap "View Selected" for "View From Selected" in Edit Mode ---
_original_pie_draw = None


def _custom_full_pie_draw(self, context):
    """Replacement draw method for VIEW3D_MT_view_pie.

    Replicates the default layout, but in mesh Edit Mode the "View Selected"
    slot is replaced with our "View From Selected" operator.
    """
    layout = self.layout
    pie = layout.menu_pie()
    pie.operator_enum("view3d.view_axis", "type")
    pie.operator("view3d.view_camera", text="View Camera", icon='CAMERA_DATA')

    if _is_mesh_edit_mode(context):
        pie.operator(
            VIEW3D_OT_view_from_selected_face.bl_idname,
            text="View From Selected",
            icon='FACESEL',
        )
    else:
        pie.operator("view3d.view_selected", text="View Selected", icon='ZOOM_SELECTED')


# ------------------------------------------------------------------ #
# Register
# ------------------------------------------------------------------ #
_classes = (
    VIEW3D_OT_view_from_selected_face,
)


def _noop_draw(self, context):
    """No-op draw used only to force Blender to wrap the menu's draw method
    so that `draw._draw_funcs` exists. Removed immediately after."""
    pass


def register():
    global _original_pie_draw

    for c in _classes:
        bpy.utils.register_class(c)

    bpy.types.VIEW3D_MT_view.append(_menu_draw_view)

    pie_cls = bpy.types.VIEW3D_MT_view_pie

    # On a fresh Blender start, nothing has appended to this menu yet, so
    # `draw` is still a plain method without a `_draw_funcs` list. Appending
    # any function (even a no-op) wraps the method and creates that list.
    if not hasattr(pie_cls.draw, '_draw_funcs'):
        pie_cls.append(_noop_draw)
        # Now `_draw_funcs` exists. Remove the no-op — the wrapper persists.
        try:
            pie_cls.draw._draw_funcs.remove(_noop_draw)
        except (ValueError, AttributeError):
            pass

    # Save the original built-in draw and substitute our custom one.
    try:
        funcs = pie_cls.draw._draw_funcs
        if _original_pie_draw is None:
            _original_pie_draw = funcs[0]
        funcs[0] = _custom_full_pie_draw
    except (AttributeError, IndexError):
        # Fallback: append (will show both default and ours, but still works).
        pie_cls.append(_pie_menu_draw)


def _purge_handlers(menu_cls, names):
    """Strip every handler in this menu whose function name matches.
    Defensive cleanup against stale handlers from previous reloads."""
    draw = getattr(menu_cls, 'draw', None)
    if not draw or not hasattr(draw, '_draw_funcs'):
        return
    stale = [f for f in draw._draw_funcs if getattr(f, '__name__', '') in names]
    for f in stale:
        try:
            draw._draw_funcs.remove(f)
        except ValueError:
            pass


def unregister():
    global _original_pie_draw

    # Restore the original pie draw method
    try:
        funcs = bpy.types.VIEW3D_MT_view_pie.draw._draw_funcs
        for i, f in enumerate(funcs):
            if f is _custom_full_pie_draw and _original_pie_draw is not None:
                funcs[i] = _original_pie_draw
                break
    except Exception:
        pass
    _original_pie_draw = None

    # Purge any stale appended handlers (defensive against past versions)
    _purge_handlers(bpy.types.VIEW3D_MT_view_pie, {"_pie_menu_draw"})
    _purge_handlers(bpy.types.VIEW3D_MT_view, {"_menu_draw_view"})

    for c in reversed(_classes):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            pass
