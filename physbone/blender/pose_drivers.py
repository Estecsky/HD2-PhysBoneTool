"""Clothing target authoring/evaluation shared by preview and the SDK bridge.

Evaluation reads one immutable animation snapshot without writing source pose,
Rest transforms, constraints or weights. The adapter freezes author rules for SDK
packaging; temporary preview poses and manual inputs are not exported. Authoring adds/removes persistent
target-role markers and reversible Bone/PoseBone colors in the user's project.
"""
import json
import math
import uuid

import bpy
from bpy.props import EnumProperty
from bpy.types import Operator, UIList
from mathutils import Euler, Matrix, Quaternion

from ..core.pose_driver import PoseDriverError, evaluate_vector_curve, evaluate_axis_responses
from . import ownership

MARKER = 'HD2PB_PoseDriverMarker'
COLOR_SETTING = 'HD2PB_PoseDriverColorSetting'
DRIVER_COLORS = {'normal': (.55, .12, .8), 'select': (.8, .35, 1.), 'active': (1., .65, 1.)}
DRIVER_PALETTE = 'THEME06'  # Default Blender purple, with initialized opaque RGBA.


def _color_snapshot(color):
    return {'palette': color.palette, 'show_colored_constraints': color.custom.show_colored_constraints,
            **{key: list(getattr(color.custom, key)) for key in DRIVER_COLORS}}


def _is_driver_color(color):
    return color.palette == DRIVER_PALETTE or _is_old_driver_color(color)


def _is_old_driver_color(color):
    return color.palette == 'CUSTOM' and all(
        # Bone colors are stored as 8-bit channels in Blender, not exact floats.
        round(a * 255) == round(b * 255) for key, value in DRIVER_COLORS.items()
        for a, b in zip(getattr(color.custom, key), value)) and not color.custom.show_colored_constraints


def _restore_color(color, value):
    # Do not overwrite colors the author explicitly changed after tagging.
    if not _is_driver_color(color):
        return
    color.palette = 'CUSTOM'
    for key in DRIVER_COLORS:
        setattr(color.custom, key, value[key])
    color.custom.show_colored_constraints = value['show_colored_constraints']
    color.palette = value['palette']


def mark_target(owner, rule):
    """Persistent authoring colors; Blender undo/save handles the RNA transaction."""
    bone = owner.data.bones[rule.target_bone]
    pose = owner.pose.bones[bone.name]
    rule.marker_id = str(uuid.uuid4())
    rule.color_backup = json.dumps({'bone': _color_snapshot(bone.color), 'pose': _color_snapshot(pose.color)})
    if COLOR_SETTING not in owner.data:
        owner.data[COLOR_SETTING] = bool(owner.data.show_bone_colors)
    bone[MARKER] = rule.marker_id
    for color in (bone.color, pose.color):
        # Blender 4.0 RGB RNA setters leave custom RGBA's hidden alpha at zero.
        # Stick rendering consumes that alpha. Use a supported opaque theme set,
        # not ctypes/DNA writes, custom shapes, or a viewport overlay workaround.
        color.palette = DRIVER_PALETTE
    owner.data.show_bone_colors = True


def repair_target_colors(owner):
    """Repair only our old tagged RGB values; leave artist overrides untouched."""
    rules = {rule.marker_id for rule in owner.data.hd2pb_project.pose_drivers if rule.marker_id}
    count = 0
    for bone in owner.data.bones:
        if bone.get(MARKER) not in rules:
            continue
        for color in (bone.color, owner.pose.bones[bone.name].color):
            if _is_old_driver_color(color):
                color.palette = DRIVER_PALETTE
                count += 1
    return count


def unmark_target(owner, rule):
    """Resolve by persistent marker even if the original bone was renamed."""
    backup = json.loads(rule.color_backup) if rule.color_backup else None
    for bone in owner.data.bones:
        if not rule.marker_id or bone.get(MARKER) != rule.marker_id:
            continue
        if backup:
            _restore_color(bone.color, backup['bone'])
            _restore_color(owner.pose.bones[bone.name].color, backup['pose'])
        del bone[MARKER]
    if not any(bone.get(MARKER) for bone in owner.data.bones) and COLOR_SETTING in owner.data:
        if owner.data.show_bone_colors:
            owner.data.show_bone_colors = bool(owner.data[COLOR_SETTING])
        del owner.data[COLOR_SETTING]


def creation_target(context, owner):
    if context.object != owner or owner.mode != 'POSE':
        raise PoseDriverError('请在姿态模式选择一根衣物目标骨后创建驱动')
    selected = tuple(getattr(context, 'selected_pose_bones', None) or ())
    active = getattr(context, 'active_pose_bone', None)
    if len(selected) != 1 or active != selected[0] or active.name not in owner.data.bones:
        raise PoseDriverError('请只选中一根活动衣物骨；该骨将成为姿态驱动目标')
    project = owner.data.hd2pb_project
    for chain in project.chains:
        if any(node.bone_name == active.name and (index > 0 or chain.root_swing)
               for index, node in enumerate(chain.nodes)):
            raise PoseDriverError(f'骨骼“{active.name}”已是物理骨链“{chain.display_name}”的模拟骨（含开启摆动的链根），不可创建姿态驱动')
    if active.name in ownership.pose_driver_target_bones(project, owner.data.bones):
        raise PoseDriverError(f'骨骼“{active.name}”已有衣物姿态驱动，不能重复创建（停用条目也占用）')
    return active


def snapshot(owner):
    evaluated = owner.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return {bone.name: bone.matrix.copy() for bone in evaluated.pose.bones}


def _relative(owner, matrices, source, reference):
    if source not in matrices:
        raise PoseDriverError(f'源骨不存在：{source or "（未指定）"}')
    parent = owner.data.bones[source].parent
    reference = reference or (parent.name if parent else '')
    if reference and reference not in matrices:
        raise PoseDriverError(f'参考骨不存在：{reference}')
    value = matrices[source]
    if reference:
        value = matrices[reference].inverted() @ value
    return value.to_quaternion().normalized()


def binding(rule):
    return json.dumps([rule.source_bone, rule.secondary_bone if rule.combination != 'SINGLE' else '',
                       rule.reference_bone], ensure_ascii=False)


def axis_curve(rule, axis=None):
    channel = rule.axis_curves.get(axis or rule.input_axis)
    if channel is None:
        raise PoseDriverError('缺少输入轴曲线，请重新创建驱动')
    return channel


def initialize_axes(rule):
    """Initialize a new authoring entry; no legacy schema or automatic migration."""
    rule.axis_curves.clear()
    for axis in 'XYZ':
        channel = rule.axis_curves.add()
        channel.name, channel.secondary_axis = axis, axis
        for angle in (-90, 0, 90):
            point = channel.samples.add()
            point.name, point.input_angle = f'{angle}°', angle


def input_vectors(owner, rule, matrices, *, manual=True):
    """Calibrated local XYZ Euler channels, not three cross-coupled twist angles."""
    if rule.calibrated and rule.calibration_binding != binding(rule):
        raise PoseDriverError(f'{rule.name}：骨绑定已改变，请恢复 Rest 中立或重新记录中立')
    rest = {bone.name: bone.matrix_local for bone in owner.data.bones}

    def read(source, neutral):
        current = _relative(owner, matrices, source, rule.reference_bone)
        zero = Quaternion(neutral) if rule.calibrated else _relative(owner, rest, source, rule.reference_bone)
        if zero.magnitude < 1.e-8:
            raise PoseDriverError('中立旋转无效，请重新校准')
        return tuple(math.degrees(v) for v in (zero.normalized().inverted() @ current).to_euler('XYZ'))

    primary = read(rule.source_bone, rule.neutral_primary)
    secondary = read(rule.secondary_bone, rule.neutral_secondary) if rule.combination != 'SINGLE' else None
    if manual and rule.use_manual_input:
        primary = tuple(rule.test_xyz)
        if secondary is not None:
            secondary = tuple(rule.test_secondary_xyz)
    channels = [axis_curve(rule, axis) for axis in 'XYZ']
    primary = tuple(-v if channel.invert_input else v for v, channel in zip(primary, channels))
    if secondary is not None:
        secondary = tuple(secondary['XYZ'.index(c.secondary_axis)] * (-1 if c.invert_secondary else 1)
                          for c in channels)
    return primary, secondary


def _ancestors(owner, name):
    result = set()
    bone = owner.data.bones.get(name)
    while bone:
        result.add(bone.name)
        bone = bone.parent
    return result


def validate(owner):
    project = owner.data.hd2pb_project
    chain_bones = ownership.simulated_bone_names(project)
    occupied = ownership.pose_driver_target_bones(project, owner.data.bones)
    conflict = chain_bones & occupied
    if conflict:
        raise PoseDriverError(f'骨骼“{sorted(conflict)[0]}”同时属于姿态驱动与模拟骨（含开启摆动的链根），请关闭根骨摆动或删除冲突配置')
    rules = [rule for rule in project.pose_drivers if rule.enabled]
    targets = set()
    for rule in rules:
        if rule.target_bone not in owner.data.bones:
            raise PoseDriverError(f'{rule.name}：衣物目标骨不存在：{rule.target_bone or "（未指定）"}')
        if rule.marker_id and owner.data.bones[rule.target_bone].get(MARKER) != rule.marker_id:
            raise PoseDriverError(f'{rule.name}：目标绑定与骨骼标记不一致，请删除条目后重新创建')
        if rule.target_bone in targets:
            raise PoseDriverError(f'{rule.name}：同一目标骨不能由两个启用的驱动写入')
        targets.add(rule.target_bone)
        target = owner.data.bones[rule.target_bone]
        for affected in (target, *target.children_recursive):
            if not affected.use_inherit_rotation or affected.inherit_scale != 'FULL':
                raise PoseDriverError(f'{rule.name}：目标分支 {affected.name} 需要继承旋转及完整缩放')
        for axis in 'XYZ':
            try:
                evaluate_vector_curve([(p.input_angle, tuple(p.output_xyz))
                                       for p in axis_curve(rule, axis).samples], 0)
            except PoseDriverError as exc:
                raise PoseDriverError(f'{rule.name} / 输入 {axis}：{exc}') from exc
    simulated = {node.bone_name for chain in owner.data.hd2pb_project.chains
                 for i, node in enumerate(chain.nodes) if i > 0 or chain.root_swing}
    for rule in rules:
        sources = [rule.source_bone]
        if rule.combination != 'SINGLE':
            sources.append(rule.secondary_bone)
        if rule.reference_bone:
            sources.append(rule.reference_bone)
        for name in sources:
            if name not in owner.data.bones:
                raise PoseDriverError(f'{rule.name}：输入/参考骨不存在：{name or "（未指定）"}')
            if _ancestors(owner, name) & targets:
                raise PoseDriverError(f'{rule.name}：输入不能是衣物目标骨或其子级，避免循环驱动')
            if _ancestors(owner, name) & simulated:
                raise PoseDriverError(f'{rule.name}：首版输入/参考骨不能属于模拟骨分支')
    return rules


def configuration(owner):
    """Session cache key; no persistent RNA writes or export changes."""
    def values(group):
        result = {}
        for prop in group.bl_rna.properties:
            key = prop.identifier
            if key in {'rna_type', 'sample_index', 'input_axis', 'marker_id', 'color_backup'}:
                continue
            value = getattr(group, key)
            if prop.type == 'COLLECTION':
                result[key] = [values(row) for row in value]
            elif getattr(prop, 'is_array', False):
                result[key] = list(value)
            else:
                result[key] = value
        return result
    return json.dumps([values(rule) for rule in owner.data.hd2pb_project.pose_drivers], sort_keys=True)


def evaluate(owner, original):
    """Return new armature-space target matrices and read-only degree telemetry."""
    rules = validate(owner)
    target_rules = {rule.target_bone: rule for rule in rules}
    responses, telemetry = {}, {}
    for rule in rules:
        primary, secondary = input_vectors(owner, rule, original)
        curves = [[(p.input_angle, tuple(p.output_xyz)) for p in axis_curve(rule, axis).samples]
                  for axis in 'XYZ']
        output = evaluate_axis_responses(curves, primary, secondary, rule.combination)
        responses[rule.target_bone] = output
        telemetry[rule.target_bone] = (primary, secondary, output)
    result = {}

    def visit(bone):
        if bone.name in result:
            return result[bone.name]
        local = original[bone.name].copy()
        parent = visit(bone.parent) if bone.parent else None
        if bone.parent:
            local = original[bone.parent.name].inverted() @ local
        rule = target_rules.get(bone.name)
        if rule and rule.influence > 0:
            rest = bone.matrix_local
            if bone.parent:
                rest = bone.parent.matrix_local.inverted() @ rest
            position, rotation, scale = local.decompose()
            response = responses[bone.name]
            offset = Euler(tuple(math.radians(v) for v in response), 'XYZ').to_quaternion()
            goal = rest.to_quaternion() @ offset
            rotation = rotation.slerp(goal, rule.influence)
            local = Matrix.LocRotScale(position, rotation, scale)
        result[bone.name] = parent @ local if parent is not None else local
        return result[bone.name]

    for bone in owner.data.bones:
        visit(bone)
    return result, telemetry


def _current(context):
    owner = ownership.project_owner(context)
    if owner is None:
        raise PoseDriverError('请选择已初始化的物理骨项目')
    ownership.ensure_authoring_owner(owner)
    return owner, owner.data.hd2pb_project


def _rule(project):
    if not 0 <= project.pose_driver_index < len(project.pose_drivers):
        raise PoseDriverError('请先添加并选择衣物姿态驱动')
    return project.pose_drivers[project.pose_driver_index]


class HD2PB_OT_pose_driver_edit(Operator):
    bl_idname = 'hd2pb.pose_driver_edit'
    bl_label = '编辑衣物姿态驱动'
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = '姿态模式选中单根衣物骨创建并标色；物体模式可管理和删除；规则由 AQSDK 独立封包整合'
    action: EnumProperty(items=tuple((key, label, '') for key, label in (
        ('ADD', '新增驱动'), ('REMOVE', '删除驱动'), ('ADD_SAMPLE', '添加采样点'),
        ('REMOVE_SAMPLE', '删除采样点'), ('NEUTRAL', '记录当前输入中立'),
        ('REST', '恢复 Rest 中立'), ('CAPTURE_INPUT', '记录当前主输入角度'),
        ('CAPTURE_OUTPUT', '记录当前目标角度'), ('CAPTURE_BOTH', '记录输入与目标 XYZ'),
        ('SORT', '按输入角度排序'))))

    def execute(self, context):
        try:
            owner, project = _current(context)
            if self.action == 'ADD':
                active = creation_target(context, owner)
                names = {r.name for r in project.pose_drivers}
                base = f'{active.name}_衣物姿态驱动'
                name, number = base, 2
                while name in names:
                    name = f'{base}_{number}'
                    number += 1
                rule = project.pose_drivers.add()
                try:
                    rule.name, rule.target_bone = name, active.name
                    initialize_axes(rule)
                    mark_target(owner, rule)
                except Exception:
                    unmark_target(owner, rule)
                    project.pose_drivers.remove(len(project.pose_drivers) - 1)
                    raise
                project.pose_driver_index = len(project.pose_drivers) - 1
                project.show_pose_drivers = True
                return {'FINISHED'}
            rule = _rule(project)
            if self.action == 'REMOVE':
                unmark_target(owner, rule)
                project.pose_drivers.remove(project.pose_driver_index)
                project.pose_driver_index = max(0, min(project.pose_driver_index, len(project.pose_drivers) - 1))
                return {'FINISHED'}
            channel = axis_curve(rule)
            if self.action == 'ADD_SAMPLE':
                taken = sorted({-180., 180., *(p.input_angle for p in channel.samples)})
                left, right = max(zip(taken, taken[1:]), key=lambda pair: pair[1] - pair[0])
                sample = channel.samples.add()
                sample.input_angle = (left + right) / 2
                sample.name = f'{sample.input_angle:g}°'
                channel.sample_index = len(channel.samples) - 1
            elif self.action == 'REMOVE_SAMPLE':
                if len(channel.samples) <= 2:
                    raise PoseDriverError('响应曲线至少保留两个采样点')
                channel.samples.remove(channel.sample_index)
                channel.sample_index = max(0, min(channel.sample_index, len(channel.samples) - 1))
            elif self.action == 'SORT':
                points = sorted(channel.samples, key=lambda p: p.input_angle)
                payload = [(p.name, p.input_angle, tuple(p.output_xyz)) for p in points]
                for sample, (name, x, xyz) in zip(channel.samples, payload):
                    sample.name, sample.input_angle, sample.output_xyz = name, x, xyz
                channel.sample_index = 0
            elif self.action == 'REST':
                rule.calibrated = False
            elif self.action == 'NEUTRAL':
                matrices = snapshot(owner)
                primary = _relative(owner, matrices, rule.source_bone, rule.reference_bone)
                secondary = (_relative(owner, matrices, rule.secondary_bone, rule.reference_bone)
                             if rule.combination != 'SINGLE' else Quaternion())
                rule.neutral_primary, rule.neutral_secondary = primary, secondary
                rule.calibration_binding = binding(rule)
                rule.calibrated = True
            else:
                if not 0 <= channel.sample_index < len(channel.samples):
                    raise PoseDriverError('请选择采样点')
                sample = channel.samples[channel.sample_index]
                matrices = snapshot(owner)
                input_value, output_value = None, None
                if self.action in {'CAPTURE_INPUT', 'CAPTURE_BOTH'}:
                    primary, _ = input_vectors(owner, rule, matrices, manual=False)
                    input_value = primary['XYZ'.index(rule.input_axis)]
                    if self.action == 'CAPTURE_BOTH' and any(abs(v) > .05 for i, v in enumerate(primary)
                                                           if i != 'XYZ'.index(rule.input_axis)):
                        raise PoseDriverError('单轴采样时请将源骨其他两轴恢复输入中立，再记录输入与目标')
                    if any(i != channel.sample_index and abs(p.input_angle - input_value) < 1.e-5
                           for i, p in enumerate(channel.samples)):
                        raise PoseDriverError('已有该输入角度的采样点，请选择该点编辑')
                if self.action in {'CAPTURE_OUTPUT', 'CAPTURE_BOTH'}:
                    bone = owner.data.bones.get(rule.target_bone)
                    if bone is None:
                        raise PoseDriverError('请先指定衣物目标骨')
                    current, rest = matrices[bone.name], bone.matrix_local
                    if bone.parent:
                        current = matrices[bone.parent.name].inverted() @ current
                        rest = bone.parent.matrix_local.inverted() @ rest
                    delta = rest.to_quaternion().inverted() @ current.to_quaternion()
                    output_value = tuple(math.degrees(v) for v in delta.to_euler('XYZ'))
                # Commit only after both captures validate; an error never half-updates a sample.
                if input_value is not None:
                    sample.input_angle = input_value
                if output_value is not None:
                    sample.output_xyz = output_value
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class HD2PB_UL_pose_drivers(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, 'enabled', text='')
        row.prop(item, 'name', text='', emboss=False, icon='CONSTRAINT_BONE')


class HD2PB_UL_pose_samples(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, 'name', text='', emboss=False)
        row.prop(item, 'input_angle', text='输入')
        for i, axis in enumerate('XYZ'):
            row.prop(item, 'output_xyz', index=i, text=axis)


def _button(layout, action, text, icon='NONE'):
    layout.operator('hd2pb.pose_driver_edit', text=text, icon=icon).action = action


def draw(layout, context, owner, project):
    from . import motion_preview
    box = layout.box()
    box.prop(project, 'show_pose_drivers', text=f'衣物姿态驱动（{len(project.pose_drivers)}）',
             emboss=False, icon='TRIA_DOWN' if project.show_pose_drivers else 'TRIA_RIGHT')
    if not project.show_pose_drivers:
        return
    box.prop(project, 'show_pose_driver_object_markers')
    row = box.row(align=True)
    if context.object == owner and owner.mode == 'POSE':
        add = row.row(align=True)
        try:
            creation_target(context, owner)
        except PoseDriverError:
            add.enabled = False
        _button(add, 'ADD', '为选中衣物骨创建驱动', 'ADD')
    else:
        box.label(text='创建驱动：切换姿态模式并只选一根衣物骨', icon='INFO')
    if project.pose_drivers:
        _button(row, 'REMOVE', '删除当前驱动', 'REMOVE')
    if not project.pose_drivers:
        box.label(text='使用已有衣物骨；不会自动加骨或改权重')
        if motion_preview.active():
            box.operator('hd2pb.motion_preview_stop', icon='PAUSE')
        return
    box.template_list('HD2PB_UL_pose_drivers', '', project, 'pose_drivers', project, 'pose_driver_index', rows=3)
    if project.pose_driver_index >= len(project.pose_drivers):
        return
    rule = _rule(project)
    if any(rule.axis_curves.get(axis) is None for axis in 'XYZ'):
        box.label(text='缺少三轴采样数据，请删除此条目后重新新增', icon='ERROR')
        return
    channel = axis_curve(rule)
    box.prop_search(rule, 'source_bone', owner.data, 'bones')
    box.prop(rule, 'combination')
    if rule.combination != 'SINGLE':
        box.prop_search(rule, 'secondary_bone', owner.data, 'bones')
        box.label(text='双源分别映射；MAX / MIN 按输出 XYZ 分量比较')
    box.prop_search(rule, 'reference_bone', owner.data, 'bones')
    box.label(text='参考骨留空 = 各自父骨；输入为中立相对 XYZ 欧拉角')
    row = box.row(align=True)
    _button(row, 'NEUTRAL', '记录当前输入中立')
    _button(row, 'REST', '恢复 Rest 中立')
    box.label(text='输入中立：' + ('已记录姿态' if rule.calibrated else '骨架 Rest'))
    box.label(text=f'衣物目标骨：{rule.target_bone}（紫色标记）', icon='BONE_DATA')
    box.prop(rule, 'influence')
    box.label(text='三轴曲线输出逐分量相加 → Rest + XYZ 旋转')
    box.label(text='0° 通常设零输出，避免中立偏置重复相加')
    box.prop(rule, 'input_axis', text='编辑输入轴', expand=True)
    box.prop(channel, 'invert_input')
    if rule.combination != 'SINGLE':
        row = box.row(align=True)
        row.prop(channel, 'secondary_axis')
        row.prop(channel, 'invert_secondary')
    box.label(text=f'{rule.input_axis} 轴独立采样 → 目标局部 X / Y / Z（度）')
    box.template_list('HD2PB_UL_pose_samples', rule.input_axis, channel, 'samples', channel, 'sample_index', rows=5)
    row = box.row(align=True)
    _button(row, 'ADD_SAMPLE', '添加点', 'ADD')
    _button(row, 'REMOVE_SAMPLE', '删除点', 'REMOVE')
    _button(row, 'SORT', '按角度排序', 'SORTALPHA')
    row = box.row(align=True)
    _button(row, 'CAPTURE_INPUT', '记录主输入角度')
    _button(row, 'CAPTURE_OUTPUT', '记录目标 XYZ')
    _button(box, 'CAPTURE_BOTH', '记录当前轴输入 + 目标 XYZ', 'KEY_HLT')
    box.label(text='采样请调整原骨架，其他输入轴保持中立；预览副本只读')
    box.label(text='线性插值，越界夹端点；采样角度不得重复')
    box.prop(rule, 'use_manual_input')
    if rule.use_manual_input:
        box.prop(rule, 'test_xyz')
        if rule.combination != 'SINGLE':
            box.prop(rule, 'test_secondary_xyz')
    box.prop(project, 'motion_preview_show_armature')
    if motion_preview.active():
        box.operator('hd2pb.motion_preview_stop', icon='PAUSE')
    else:
        row = box.row(align=True)
        op = row.operator('hd2pb.motion_preview_start', text='仅看目标姿态', icon='PLAY')
        op.target_only, op.use_pose_drivers = True, True
        col = row.column(align=True)
        col.enabled = bool(project.chains)
        op = col.operator('hd2pb.motion_preview_start', text='目标 + 现有物理', icon='PHYSICS')
        op.target_only, op.use_pose_drivers = False, True
    if motion_preview.message():
        box.label(text=motion_preview.message())
    info = motion_preview.driver_telemetry(owner, rule.target_bone)
    if info:
        a, b, output = info
        fmt = lambda values: ' / '.join(f'{v:.1f}°' for v in values)
        box.label(text=f'输入 XYZ：{fmt(a)}')
        if b is not None:
            box.label(text=f'第二输入（按通道映射）：{fmt(b)}')
        box.label(text=f'目标 XYZ：{fmt(output)}')


CLASSES = (HD2PB_OT_pose_driver_edit, HD2PB_UL_pose_drivers, HD2PB_UL_pose_samples)
