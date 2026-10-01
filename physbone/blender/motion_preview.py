"""Side-by-side skinned motion preview, backed by the game's native solver.

Only session-owned copies are posed. The source rest rig, mesh, weights,
constraints and animation are never modified; save/load/undo/unregister stop
the session and remove its temporary objects before Blender serializes them.
"""
import copy
import json
import math
import time
import uuid

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty
from bpy.types import Operator
from mathutils import Matrix, Vector

from ..core.native_preview import NativePreview, PreviewError
from . import adapter_v1, ownership, pose_drivers

if globals().get('_REGISTERED'):
    unregister()

TAG = "HD2PB_TransientMotionPreview"
_SESSION = None
_REGISTERED = False
_MESSAGE = ""


def _packed(matrix, scale=1.0):
    return tuple(float(matrix[r][c]) * (scale if c == 3 else 1.) for r in range(3) for c in range(4))


def _unpacked(values, scale=1.0):
    return Matrix(tuple(tuple(values[r * 4 + c] / (scale if c == 3 else 1.)
                              for c in range(4)) for r in range(3)) + ((0., 0., 0., 1.),))


def _object_scale(owner):
    basis = owner.matrix_world.to_3x3()
    sizes = [col.length for col in basis.col]
    scale = sum(sizes) / 3
    if (not math.isfinite(scale) or scale < 1.e-8 or basis.determinant() <= 0 or
        max(sizes) - min(sizes) > scale * 1.e-4 or
        any(abs(basis.col[i].dot(basis.col[j])) > scale * scale * 1.e-4
            for i in range(3) for j in range(i))):
        raise PreviewError("预览骨架包含镜像、剪切或非均匀缩放；请先在保存副本上规范化")
    return scale


def _document(owner):
    scale = _object_scale(owner)
    document = adapter_v1.preview_document_from_object(owner)
    # Uniform object scale is baked in the temporary physical geometry only.
    # The display copy retains source scale; output translations are unscaled.
    if abs(scale - 1.) > 1.e-7:
        document = copy.deepcopy(document)
        for bone in document['bones']:
            bone['rest_head'] = [v * scale for v in bone['rest_head']]
        for chain in document['chains']:
            for joint in chain['joints']:
                joint['radius'] *= scale
        for collider in document['colliders']:
            collider['a'] = [v * scale for v in collider['a']]
            if collider['kind'] != 'PLANE':
                collider['b'] = [v * scale for v in collider['b']]
            collider['radius'] *= scale
        for link in document['chain_links']:
            link['max_correction'] *= scale
            for collision in link['collisions']:
                collision['radius'] *= scale
    return document, scale


def _signature(owner):
    return tuple((b.name, b.parent.name if b.parent else '') for b in owner.data.bones)


class Session:
    def __init__(self, owner, *, test_motion=False, library_path=None, cache_root=None,
                 target_only=False, use_pose_drivers=False):
        self.owner = owner
        self.target_only = target_only
        self.use_pose_drivers = use_pose_drivers or target_only
        self.telemetry = {}
        self.test_motion = test_motion
        self.library_path, self.cache_root = library_path, cache_root
        self.tag = str(uuid.uuid4())
        self.objects = []
        self.mesh_sources = []
        self.armature_data = None
        self.collection = None
        self.view_layer = bpy.context.view_layer
        self.native = None
        self.elapsed = 0.
        self.last_frame = None
        self.last_wall = time.monotonic()
        self.last_config_check = -1.
        self.signature = _signature(owner)
        if self.use_pose_drivers:
            pose_drivers.validate(owner)
        self.driver_digest = pose_drivers.configuration(owner) if self.use_pose_drivers else ''
        document = None
        self.scale = _object_scale(owner)
        if not target_only:
            document, self.scale = _document(owner)
        self.digest = json.dumps(document, sort_keys=True, allow_nan=False)
        try:
            if not target_only:
                self.native = NativePreview(document, library_path=library_path, cache_root=cache_root)
            self.collection = bpy.data.collections.new("HD2 物理动作预览（临时，不导出）")
            self.collection[TAG] = self.tag
            bpy.context.scene.collection.children.link(self.collection)
            self.display = owner.copy()
            self.display[TAG] = self.tag
            self.objects.append(self.display)
            self.display.data = owner.data.copy()
            self.armature_data = self.display.data
            self.collection.objects.link(self.display)
            self.display.animation_data_clear()
            for constraint in tuple(self.display.constraints): self.display.constraints.remove(constraint)
            self.display.parent = None
            self.display.name = owner.name + (" · 衣物目标预览" if target_only else " · 同求解器预览")
            project = self.display.data.hd2pb_project
            for field in ('profiles', 'chains', 'colliders', 'chain_links', 'pose_drivers'): getattr(project, field).clear()
            project.project_uuid = ''
            project.schema_version = 0
            project.authoring_object = None
            for bone in self.display.pose.bones:
                for constraint in tuple(bone.constraints): bone.constraints.remove(constraint)
            for obj in tuple(bpy.context.scene.objects):
                if obj.type != 'MESH' or obj.get(TAG) or not obj.visible_get(view_layer=bpy.context.view_layer) or not any(
                    modifier.type == 'ARMATURE' and modifier.object == owner for modifier in obj.modifiers):
                    continue
                duplicate = obj.copy()
                duplicate[TAG] = self.tag
                self.objects.append(duplicate)
                self.collection.objects.link(duplicate)
                self.mesh_sources.append((duplicate, obj))
                duplicate.animation_data_clear()
                duplicate.parent = None
                for constraint in tuple(duplicate.constraints): duplicate.constraints.remove(constraint)
                duplicate.matrix_world = obj.matrix_world.copy()
                for modifier in duplicate.modifiers:
                    if modifier.type == 'ARMATURE' and modifier.object == owner: modifier.object = self.display
            for obj in self.objects:
                obj[TAG] = self.tag
                obj.hide_render = True
                obj.hide_select = True
                obj.hide_set(False)
            self._update_armature_visibility()
            # Respect the author's visible mesh selection (in particular hidden
            # stock reference armour). Do not reveal unrelated hidden meshes.
            points = [source.matrix_world @ Vector(corner)
                      for _, source in self.mesh_sources for corner in source.bound_box]
            width = max(p.x for p in points) - min(p.x for p in points) if points else 1.
            self.offset = Vector((max(.5, width * 1.2), 0, 0))
        except BaseException:
            self.close()
            raise

    def _update_armature_visibility(self):
        # Hide only the session-owned rig's drawing, not its dependency-graph
        # evaluation: copied mesh Armature modifiers still need this object.
        # Keep temporary objects unselectable even when the rig is displayed.
        hidden = not getattr(self.owner.data.hd2pb_project, 'motion_preview_show_armature', False)
        if self.display.hide_get(view_layer=self.view_layer) != hidden:
            self.display.hide_set(hidden, view_layer=self.view_layer)

    def step(self, seconds=None):
        owner = self.owner
        if owner.name not in bpy.data.objects or bpy.data.objects[owner.name] != owner:
            raise PreviewError("源骨架已删除")
        if _signature(owner) != self.signature:
            raise PreviewError("骨架结构已更改，请重新开始动作预览")
        self._update_armature_visibility()
        if owner.mode == 'EDIT': return
        now = time.monotonic()
        if seconds is None:
            self.elapsed += max(0., min(.1, now - self.last_wall))
        else:
            if seconds < self.elapsed and self.native: self.native.reset()
            self.elapsed = seconds
        self.last_wall = now
        if self.elapsed - self.last_config_check >= .2 or seconds is not None:
            if not self.target_only:
                document, scale = _document(owner)
                digest = json.dumps(document, sort_keys=True, allow_nan=False)
                if digest != self.digest:
                    replacement = NativePreview(document, library_path=self.library_path, cache_root=self.cache_root)
                    self.native.close()
                    self.native, self.digest, self.scale = replacement, digest, scale
            else:
                self.scale = _object_scale(owner)
            if self.use_pose_drivers:
                driver_digest = pose_drivers.configuration(owner)
                if driver_digest != self.driver_digest:
                    if self.native: self.native.reset()
                    self.driver_digest = driver_digest
            self.last_config_check = self.elapsed
        frame = bpy.context.scene.frame_current_final
        if self.last_frame is not None and (frame < self.last_frame or frame - self.last_frame > 2):
            if self.native: self.native.reset()
        self.last_frame = frame
        evaluated = owner.evaluated_get(bpy.context.evaluated_depsgraph_get())
        original = {bone.name: bone.matrix.copy() for bone in evaluated.pose.bones}
        if self.use_pose_drivers:
            original, self.telemetry = pose_drivers.evaluate(owner, original)
        root = owner.matrix_world.to_quaternion().to_matrix().to_4x4()
        root.translation = owner.matrix_world.translation
        if self.test_motion:
            root = root @ Matrix.Rotation(.12 * math.sin(self.elapsed * 2), 4, 'Z')
            root.translation += Vector((.12 * math.sin(self.elapsed * 2.4), 0, .025 * math.sin(self.elapsed * 4.8)))
        physical, status = {}, 0
        if self.native:
            # Apply target rules once, before the existing native solver. Its
            # fallback receives the same target, not the uncorrected animation.
            matrices, status = self.native.step(_packed(root),
                [_packed(original[name], self.scale) for name in self.native.names], self.elapsed)
            physical = {name: _unpacked(row, self.scale) for name, row in zip(self.native.names, matrices)}
        desired = {}
        for bone in owner.data.bones:
            name = bone.name
            if name in physical:
                desired[name] = physical[name]
            elif bone.parent:
                parent = bone.parent.name
                desired[name] = desired[parent] @ original[parent].inverted() @ original[name]
            else:
                desired[name] = original[name]
            kwargs = {'invert': True}
            if bone.parent:
                kwargs.update(parent_matrix=desired[bone.parent.name], parent_matrix_local=bone.parent.matrix_local)
            self.display.pose.bones[name].matrix_basis = bone.convert_local_to_pose(
                desired[name], bone.matrix_local, **kwargs)
        display_root = root.copy()
        display_root.translation += self.offset
        self.display.matrix_world = display_root @ Matrix.Scale(self.scale, 4)
        visual_delta = self.display.matrix_world @ owner.matrix_world.inverted()
        for duplicate, source in self.mesh_sources:
            duplicate.matrix_world = visual_delta @ source.matrix_world
        return status

    def close(self):
        if self.native:
            self.native.close()
            self.native = None
        for obj in reversed(self.objects):
            try:
                if obj.name in bpy.data.objects and obj.get(TAG) == self.tag:
                    bpy.data.objects.remove(obj, do_unlink=True)
            except ReferenceError:
                pass
        self.objects.clear()
        try:
            if self.armature_data and self.armature_data.users == 0:
                bpy.data.armatures.remove(self.armature_data)
        except ReferenceError:
            pass  # The user may have removed the transient datablock first.
        self.armature_data = None
        try:
            if self.collection and self.collection.name in bpy.data.collections:
                bpy.data.collections.remove(self.collection)
        except ReferenceError:
            pass
        self.collection = None


def active(): return _SESSION is not None
def message(): return _MESSAGE


def driver_telemetry(owner, name):
    return _SESSION.telemetry.get(name) if _SESSION and _SESSION.owner == owner else None


def stop():
    global _SESSION, _MESSAGE
    if _SESSION:
        _SESSION.close()
        _SESSION = None
    _MESSAGE = ''


def _timer():
    global _MESSAGE
    if not _REGISTERED: return None
    if _SESSION:
        try:
            status = _SESSION.step()
            _MESSAGE = ({2: "本帧全局约束冲突，全部退回动画；请检查碰撞体和角限",
                         3: "本帧局部约束冲突：相关链组退回动画，其他链保持物理"}.get(
                        status, "运行中：仅看衣物目标（无物理）" if _SESSION.target_only else
                        ("运行中：衣物目标 + 现有物理（仅预览）" if _SESSION.use_pose_drivers else
                         "运行中：参数修改自动重载；源工程未改动")))
            for screen in bpy.data.screens:
                for area in screen.areas:
                    if area.type == 'VIEW_3D': area.tag_redraw()
        except Exception as exc:
            stop()
            _MESSAGE = str(exc)
    return 1 / 60


@persistent
def _before_data_change(*_args):
    stop()


def owned_state_present(): return bool(_REGISTERED or _SESSION)


def register():
    global _REGISTERED
    if _REGISTERED: return
    for name in ('load_pre', 'save_pre', 'undo_pre', 'redo_pre'):
        handlers = getattr(bpy.app.handlers, name)
        if _before_data_change not in handlers: handlers.append(_before_data_change)
    _REGISTERED = True
    if not bpy.app.timers.is_registered(_timer): bpy.app.timers.register(_timer, first_interval=.1)


def unregister():
    global _REGISTERED
    stop()
    if bpy.app.timers.is_registered(_timer): bpy.app.timers.unregister(_timer)
    for name in ('load_pre', 'save_pre', 'undo_pre', 'redo_pre'):
        handlers = getattr(bpy.app.handlers, name)
        if _before_data_change in handlers: handlers.remove(_before_data_change)
    _REGISTERED = False


class HD2PB_OT_motion_preview_start(Operator):
    bl_idname = 'hd2pb.motion_preview_start'
    bl_label = '开始同求解器动作预览'
    bl_description = '生成临时并排预览副本；修改参数自动重载，不修改源骨架、网格或权重'
    test_motion: BoolProperty(name='内置转身/起停驱动', default=False)
    target_only: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    use_pose_drivers: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        global _SESSION, _MESSAGE
        try:
            owner = ownership.project_owner(context)
            if owner is None: raise PreviewError('请选择物理骨项目')
            ownership.ensure_authoring_owner(owner)
            stop()
            _SESSION = Session(owner, test_motion=self.test_motion,
                               target_only=self.target_only, use_pose_drivers=self.use_pose_drivers)
            _SESSION.step(0.)
            _MESSAGE = '已开始衣物姿态预览（临时副本不导出）' if self.use_pose_drivers or self.target_only else '已开始同求解器动作预览'
            return {'FINISHED'}
        except Exception as exc:
            stop()
            _MESSAGE = str(exc)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class HD2PB_OT_motion_preview_stop(Operator):
    bl_idname = 'hd2pb.motion_preview_stop'
    bl_label = '结束并清理动作预览'

    def execute(self, _context):
        stop()
        return {'FINISHED'}


CLASSES = (HD2PB_OT_motion_preview_start, HD2PB_OT_motion_preview_stop)
