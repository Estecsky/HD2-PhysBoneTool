"""Object-mode pose-target markers: drawing only, no scene data or export state.

Read current evaluated bones on every redraw. Never retain RNA objects across
undo/load, generate helper objects, or change the author's mode or selection.
"""
import math

import bpy

from . import ownership

if globals().get('_HANDLE') is not None:
    unregister()

_HANDLE = None
_SHADER = None
COLOR = (0.72, 0.28, 1.0, 1.0)


def tag_redraw(_self=None, _context=None):
    # Add-on enable/startup (and failure rollback) may run in RestrictBlend:
    # bpy.data is then _RestrictData, with no screens. Redraw is optional here;
    # Blender's next normal viewport draw will invoke the registered handler.
    # Do not retain real data or schedule a timer to bypass this restriction.
    for screen in getattr(bpy.data, 'screens', ()):
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _bone_visible(bone):
    if bone.hide:
        return False
    # 4.0 has flat collections; later versions add hierarchy/solo visibility.
    return not bone.collections or any(
        getattr(collection, 'is_visible_effectively', collection.is_visible)
        for collection in bone.collections
    )


def marker_segments(context):
    """Return world-space endpoint pairs, separated by normal/in-front drawing."""
    space = context.space_data
    normal, in_front = [], []
    if (context.mode != 'OBJECT' or space is None or space.type != 'VIEW_3D'
            or not space.overlay.show_overlays or not space.overlay.show_bones
            or not space.show_object_viewport_armature):
        return normal, in_front
    depsgraph = None
    for owner in context.view_layer.objects:
        try:
            if (owner.type != 'ARMATURE' or owner.mode != 'OBJECT'
                    or owner.get('HD2PB_TransientMotionPreview')
                    or owner.display_type == 'BOUNDS'
                    or not owner.visible_get(view_layer=context.view_layer, viewport=space)):
                continue
            project = getattr(owner.data, 'hd2pb_project', None)
            if (project is None or not ownership.project_initialized(project)
                    or project.authoring_object != owner
                    or not project.show_pose_driver_object_markers):
                continue
            targets = ownership.pose_driver_target_bones(project, owner.data.bones)
            if not targets:
                continue
            if depsgraph is None:
                depsgraph = context.evaluated_depsgraph_get()
            evaluated = owner.evaluated_get(depsgraph)
            points = in_front if owner.show_in_front else normal
            for name in sorted(targets):
                bone = owner.data.bones.get(name)
                pose = evaluated.pose.bones.get(name)
                if bone is None or pose is None or not _bone_visible(bone):
                    continue
                pair = (tuple(evaluated.matrix_world @ pose.head),
                        tuple(evaluated.matrix_world @ pose.tail))
                if all(math.isfinite(value) for point in pair for value in point):
                    points.extend(pair)
        except (ReferenceError, RuntimeError, AttributeError):
            # A draw can coincide with an undo/load or collection replacement.
            # Keep no stale RNA and retry naturally on the next viewport draw.
            continue
    return normal, in_front


def _draw():
    global _SHADER
    context = bpy.context
    if context.region is None or context.region.type != 'WINDOW':
        return
    normal, in_front = marker_segments(context)
    if not normal and not in_front:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader

    if _SHADER is None:
        _SHADER = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    state = gpu.state
    previous = (state.blend_get(), state.depth_test_get(), state.depth_mask_get())
    try:
        state.blend_set('ALPHA')
        state.depth_mask_set(False)
        _SHADER.bind()
        _SHADER.uniform_float('viewportSize', gpu.state.viewport_get()[2:])
        _SHADER.uniform_float('lineWidth', 3.0)
        _SHADER.uniform_float('color', COLOR)
        for points, depth in ((normal, 'LESS_EQUAL'), (in_front, 'NONE')):
            if points:
                state.depth_test_set(depth)
                batch_for_shader(_SHADER, 'LINES', {'pos': points}).draw(_SHADER)
    finally:
        state.blend_set(previous[0])
        state.depth_test_set(previous[1])
        state.depth_mask_set(previous[2])


def owned_state_present():
    return _HANDLE is not None


def register():
    global _HANDLE
    if _HANDLE is None and not bpy.app.background:
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_VIEW')
        tag_redraw()


def unregister():
    global _HANDLE, _SHADER
    if _HANDLE is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, 'WINDOW')
        _HANDLE = None
    _SHADER = None
    tag_redraw()
