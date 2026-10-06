"""Explicit, transactional baking of representable collider preview transforms."""
import math

from bpy.types import Operator
from mathutils import Matrix, Vector

from . import ownership, preview, properties


def _target(context):
    obj = context.object
    owner, project, collider, _ = ownership.resolve_controller(obj, require_unique=True)
    if (obj.hd2pb_entity_type != properties.ENTITY_COLLIDER
            or obj.hd2pb_preview_kind != properties.PREVIEW_COLLIDER_SHAPE):
        raise ownership.HD2PBError("请选择碰撞体形状 Curve")
    if not project.preview_allow_collider_transform:
        raise ownership.HD2PBError("请先启用碰撞体试摆")
    issues = ownership.controller_issues(owner) + preview.preview_issues(owner)
    if issues:
        raise ownership.HD2PBError(issues[0])
    if obj.animation_data is not None or any(abs(obj.matrix_parent_inverse[i][j] - float(i == j)) > 1e-6 for i in range(4) for j in range(4)):
        raise ownership.HD2PBError("不支持带动画/驱动器或父级逆偏移的碰撞体试摆")
    return obj, owner, project, collider


def transformed_parameters(collider, matrix):
    """matrix is the preview delta in its canonical bone-local frame."""
    if any(not math.isfinite(v) for row in matrix for v in row):
        raise ownership.HD2PBError("试摆矩阵包含非有限值")
    linear = matrix.to_3x3()
    columns = [linear.col[i] for i in range(3)]
    scales = [v.length for v in columns]
    if min(scales) < 1e-8 or linear.determinant() <= 0:
        raise ownership.HD2PBError("不支持零缩放或镜像试摆")
    if collider.kind != "PLANE" and (max(scales) - min(scales) > 1e-5 * max(scales)
            or any(abs(columns[i].dot(columns[j])) > 1e-5 * scales[i] * scales[j]
                   for i in range(3) for j in range(i))):
        raise ownership.HD2PBError("球/胶囊只能应用等比缩放，椭球或剪切无法由现有参数表达")
    if collider.kind == "CAPSULE" and collider.bone_name_b:
        # Both centerline points are fully driven by bone Hooks. Object-space
        # translation/rotation is not a canonical endpoint delta.
        if matrix.translation.length > 1e-6 or matrix.to_quaternion().angle > 1e-6:
            raise ownership.HD2PBError("双骨胶囊端点由各自骨骼驱动；请编辑局部 A/B，整体试摆仅支持等比缩放半径")
        values = dict(a=tuple(collider.a), b=tuple(collider.b), radius=collider.radius * scales[0])
    else:
        values = dict(a=tuple(matrix @ Vector(collider.a)), b=tuple(collider.b), radius=collider.radius)
        if collider.kind == "CAPSULE":
            values['b'] = tuple(matrix @ Vector(collider.b))
        if collider.kind == "PLANE":
            normal = linear.inverted().transposed() @ Vector(collider.b)
            if normal.length < 1e-8:
                raise ownership.HD2PBError("平面法线不能为零")
            values['b'] = tuple(normal.normalized())
        else:
            values['radius'] *= scales[0]
    if not 0 <= values['radius'] <= 10 or any(abs(v) > 10000 for f in ('a', 'b') for v in values[f]):
        raise ownership.HD2PBError("应用结果超出碰撞体参数范围，未修改任何参数")
    return values


def apply_transform(context):
    obj, owner, _project, collider = _target(context)
    if obj.constraints or obj.delta_location.length > 1e-8 or any(abs(v) > 1e-8 for v in obj.delta_rotation_euler) or any(abs(v-1) > 1e-8 for v in obj.delta_scale):
        raise ownership.HD2PBError("不支持碰撞体额外约束或 Delta Transform，请先还原这些变换")
    matrix = obj.matrix_basis.copy()
    values = transformed_parameters(collider, matrix)
    old = dict(a=tuple(collider.a), b=tuple(collider.b), radius=collider.radius)
    try:
        with preview.suspend_updates():
            for field, value in values.items(): setattr(collider, field, value)
        obj.matrix_basis = Matrix.Identity(4)
        preview.refresh_entity(owner, properties.ENTITY_COLLIDER, collider.uuid, aspects=(preview.ASPECT_GEOMETRY,))
        preview.consume_entity_updates(owner, properties.ENTITY_COLLIDER, collider.uuid)
    except Exception as original:
        with preview.suspend_updates():
            for field, value in old.items(): setattr(collider, field, value)
        try:
            preview.refresh_entity(owner, properties.ENTITY_COLLIDER, collider.uuid, aspects=(preview.ASPECT_GEOMETRY,))
        except Exception as rollback:
            raise RuntimeError(f"应用失败（{original}）；参数已还原，但预览恢复失败（{rollback}），请重建编辑对象") from original
        finally:
            preview.consume_entity_updates(owner, properties.ENTITY_COLLIDER, collider.uuid)
            obj.matrix_basis = matrix
        raise


class HD2PB_OT_apply_collider_transform(Operator):
    bl_idname = 'hd2pb.apply_collider_transform'
    bl_label = '应用碰撞体试摆'
    bl_description = '显式写回局部点/法线与半径；拒绝不可表示的缩放，双骨胶囊仅支持等比缩放半径'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try: apply_transform(context)
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        self.report({'INFO'}, '已将试摆写入碰撞参数并清零预览变换')
        return {'FINISHED'}


class HD2PB_OT_reset_collider_transform(Operator):
    bl_idname = 'hd2pb.reset_collider_transform'
    bl_label = '还原碰撞体试摆'
    bl_description = '清零试摆变换，不修改权威碰撞参数'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            obj, _owner, _project, _collider = _target(context)
            obj.matrix_basis = Matrix.Identity(4)
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        return {'FINISHED'}


CLASSES = (HD2PB_OT_apply_collider_transform, HD2PB_OT_reset_collider_transform)
