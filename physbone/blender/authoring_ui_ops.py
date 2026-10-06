"""Project-scoped mask editing and reversible display focus. No physics format changes."""
import json
import re

from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Operator

from . import ownership, preview, properties

MASK_MAX = 0x7fffffff
SHOW_FIELDS = ('preview_show_node_spheres', 'preview_show_nodes', 'preview_show_limits', 'preview_show_chains',
               'preview_show_chain_links', 'preview_show_link_collisions', 'preview_show_colliders')
FOCUS_FIELDS = {'NODES': 'preview_show_nodes', 'LIMITS': 'preview_show_limits',
                'COLLIDERS': 'preview_show_colliders'}
MASK_TYPES = {'PROFILE', 'CHAIN', 'NODE', 'COLLIDER', 'LINK_COLLISION'}


def parse_mask(text):
    """Strict decimal/hex input; never clamp, wrap, or discard unsupported bits."""
    text = str(text).strip()
    if not re.fullmatch(r'(?:[0-9]+|0[xX][0-9a-fA-F]+)', text):
        raise ValueError('请输入非负十进制整数或 0x 开头的十六进制掩码')
    value = int(text, 16 if text.lower().startswith('0x') else 10)
    if value > MASK_MAX:
        raise ValueError('目前仅支持组 1–31，最大值为 2147483647 / 0x7FFFFFFF')
    return value


def mask_result(original, operand, operation, minimum=0):
    if type(original) is not int or not 0 <= original <= MASK_MAX:
        raise ValueError('原掩码不在 31 位范围内')
    if type(operand) is not int or not 0 <= operand <= MASK_MAX:
        raise ValueError('待应用掩码不在 31 位范围内')
    if operation == 'REPLACE': result = operand
    elif operation == 'ADD': result = original | operand
    elif operation == 'REMOVE': result = original & (MASK_MAX ^ operand)
    else: raise ValueError('未知掩码编辑方式')
    if result < minimum:
        raise ValueError('该字段不能为 0；所属组和链间碰撞允许组必须至少选一组')
    return result


def mask_summary(value):
    groups = [str(i + 1) for i in range(31) if value & (1 << i)]
    if not groups: return '0（关闭碰撞）'
    return f'{len(groups)} 组：' + ', '.join(groups[:6]) + ('…' if len(groups) > 6 else '')


def _effective(entity_type, entity, parent, field):
    if entity_type == 'NODE' and not entity.override_collision_filter:
        return int(getattr(parent, field))
    return int(getattr(entity, field))


def _state(entity_type, entity, parent):
    fields = ('collision_mask',) if entity_type == 'PROFILE' else ('group_mask', 'collision_mask')
    return dict(uuid=entity.uuid, values={f: int(getattr(entity, f)) for f in fields},
                effective={f: _effective(entity_type, entity, parent, f) for f in fields},
                override=bool(entity.override_collision_filter) if entity_type == 'NODE' else None)


def _capture(context):
    owner, project, entity, parent = ownership.resolve_controller(context.object, require_unique=True)
    kind = context.object.hd2pb_entity_type
    if kind not in MASK_TYPES:
        raise ownership.HD2PBError('请选择有碰撞过滤参数的编辑实体')
    if kind == 'NODE' and (parent is None or not parent.nodes or parent.nodes[0].uuid == entity.uuid):
        raise ownership.HD2PBError('固定驱动节点不能编辑碰撞掩码')
    rows = {entity.uuid: _state(kind, entity, parent)}
    for obj in tuple(context.selected_objects or ()):
        if getattr(obj, 'hd2pb_entity_type', '') != kind:
            continue
        other_owner, other_project, other, other_parent = ownership.resolve_controller(obj, require_unique=True)
        if other_owner != owner or other_project.project_uuid != project.project_uuid:
            raise ownership.HD2PBError('批量掩码编辑不能跨物理项目')
        if kind == 'NODE' and (other_parent is None or other_parent.nodes[0].uuid == other.uuid):
            raise ownership.HD2PBError('选区含固定驱动节点，不能批量修改')
        rows[other.uuid] = _state(kind, other, other_parent)
    return dict(owner_name=owner.name, project_uuid=project.project_uuid, kind=kind,
                active=entity.uuid, rows=list(rows.values()))


def _rows(snapshot, scope):
    if scope == 'CURRENT': return [r for r in snapshot['rows'] if r['uuid'] == snapshot['active']]
    if scope == 'SELECTED': return snapshot['rows']
    raise ValueError('未知编辑范围')


def _minimum(kind, field):
    return 1 if field == 'group_mask' or kind == 'LINK_COLLISION' else 0


def _planned(snapshot, scope, field, operand, operation, enable_override):
    kind = snapshot['kind']
    if field not in {'group_mask', 'collision_mask'} or (kind == 'PROFILE' and field != 'collision_mask'):
        raise ValueError('当前实体没有这个碰撞过滤字段')
    result = []
    for row in _rows(snapshot, scope):
        inherited = kind == 'NODE' and not row['override']
        if inherited and not enable_override:
            raise ValueError('包含继承骨链过滤的节点；请明确勾选“为继承节点启用独立过滤”')
        values = dict(row['effective'] if inherited else row['values'])
        values[field] = mask_result(values[field], operand, operation, _minimum(kind, field))
        result.append((row, values, inherited))
    if not result: raise ValueError('没有可编辑的目标')
    return result


def _apply_mask_plan(resolved, kind):
    """All targets are prevalidated; best-effort rollback attempts every field."""
    try:
        for entity, _row, values, inherited in resolved:
            for field, value in values.items(): setattr(entity, field, value)
            if inherited: entity.override_collision_filter = True
    except Exception as error:
        failures = []
        for entity, row, _values, _inherited in resolved:
            restored = dict(row['values'])
            if kind == 'NODE': restored['override_collision_filter'] = row['override']
            for field, value in restored.items():
                try: setattr(entity, field, value)
                except Exception as rollback_error: failures.append(f'{row["uuid"]}/{field}: {rollback_error}')
        if failures:
            raise RuntimeError('掩码批量写入失败且回滚不完整：' + '；'.join(failures)) from error
        raise


def _bit_get(index):
    def get(self):
        try: return bool(parse_mask(self.mask_text) & (1 << index))
        except ValueError: return False
    return get


def _bit_set(index):
    def set_value(self, enabled):
        try: value = parse_mask(self.mask_text)
        except ValueError: return
        self.mask_text = str(value | (1 << index) if enabled else value & (MASK_MAX ^ (1 << index)))
    return set_value


def _quick_set(action):
    def set_value(self, enabled):
        if not enabled: return
        try: value = parse_mask(self.mask_text)
        except ValueError:
            if action == 'INVERT': return
            value = 0
        self.mask_text = str(MASK_MAX if action == 'ALL' else 0 if action == 'CLEAR' else MASK_MAX ^ value)
    return set_value


class HD2PB_OT_edit_collision_mask(Operator):
    bl_idname = 'hd2pb.edit_collision_mask'
    bl_label = '编辑 31 组碰撞掩码'
    bl_description = '按组编号编辑当前或同项目同类型选区；增减指定组不会覆盖其他位'
    bl_options = {'REGISTER', 'UNDO'}

    field_name: StringProperty(default='collision_mask', options={'HIDDEN'})
    snapshot_json: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    mask_text: StringProperty(name='高级数值（十进制 / 0x 十六进制）', default='0',
                              description='与组按钮共享同一编辑值；确认前不修改项目，超出31位会拒绝')
    scope: EnumProperty(name='范围', items=(('CURRENT', '当前实体', ''), ('SELECTED', '同项目同类型选区', '固定为打开弹窗时的选区')), default='CURRENT')
    operation: EnumProperty(name='应用方式', items=(('REPLACE', '替换完整掩码', '所有目标使用下列完整值'),
        ('ADD', '仅添加勾选组', '各目标保留其余组'), ('REMOVE', '仅移除勾选组', '各目标保留其余组')), default='REPLACE')
    enable_override: BoolProperty(name='为继承节点启用独立过滤', default=False,
        description='先复制该节点的有效链过滤，再仅修改选定字段；不改其他节点')

    def invoke(self, context, _event):
        try:
            snapshot = _capture(context)
            self.snapshot_json = json.dumps(snapshot, ensure_ascii=False)
            if self.field_name not in snapshot['rows'][0]['values']:
                raise ValueError('当前实体没有这个掩码字段')
            multiple = len(snapshot['rows']) > 1
            self.scope = 'SELECTED' if multiple else 'CURRENT'
            self.operation = 'ADD' if multiple else 'REPLACE'
            self.mask_text = '0' if multiple else str(snapshot['rows'][0]['effective'][self.field_name])
            return context.window_manager.invoke_props_dialog(self, width=600)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

    def draw(self, _context):
        layout = self.layout
        try:
            snapshot = json.loads(self.snapshot_json)
            rows = _rows(snapshot, self.scope)
            initial = [r['effective'][self.field_name] for r in rows]
            layout.prop(self, 'scope')
            layout.label(text=f'将编辑 {len(rows)} 个实体；当前值' + ('混合（每个目标保留独立基值）' if len(set(initial)) > 1 else f' {initial[0]}'))
            layout.prop(self, 'operation', expand=True)
            if snapshot['kind'] == 'NODE':
                layout.prop(self, 'enable_override')
            layout.label(text='按钮数字是组编号；悬停查看位值。增减模式中勾选的是操作位。')
            for start in range(0, 31, 8):
                row = layout.row(align=True)
                for index in range(start, min(start + 8, 31)):
                    mixed = any(v & (1 << index) for v in initial) and not all(v & (1 << index) for v in initial)
                    row.prop(self, f'group_{index + 1}', text=f'{index + 1}' + (' ·' if mixed else ''), toggle=True)
            layout.label(text='· 表示打开时所选目标在该组上不一致；不是已全部选中')
            row = layout.row(align=True)
            row.prop(self, 'all_groups', toggle=True)
            row.prop(self, 'invert_groups', toggle=True)
            row.prop(self, 'clear_groups', toggle=True)
            layout.prop(self, 'mask_text')
            value = parse_mask(self.mask_text)
            layout.label(text=f'0x{value:08X}  /  {value:031b}')
            plan = _planned(snapshot, self.scope, self.field_name, value, self.operation, self.enable_override)
            layout.label(text=f'确认后原子更新 {len(plan)} 个实体；取消不修改参数')
            if _minimum(snapshot['kind'], self.field_name):
                layout.label(text='此字段最终值不可为 0', icon='INFO')
            else:
                layout.label(text='允许组为 0 只关闭相应碰撞；弹簧、重力及角限仍运行', icon='INFO')
        except (ValueError, KeyError, TypeError) as exc:
            box = layout.box(); box.alert = True
            box.label(text=str(exc), icon='ERROR')

    def execute(self, context):
        try:
            snapshot = json.loads(self.snapshot_json) if self.snapshot_json else _capture(context)
            owner = ownership.project_owner(context)
            project = ownership.ensure_authoring_owner(owner)
            if owner.name != snapshot['owner_name'] or project.project_uuid != snapshot['project_uuid']:
                raise ValueError('活动项目已变化，请重新打开掩码弹窗')
            plan = _planned(snapshot, self.scope, self.field_name, parse_mask(self.mask_text), self.operation, self.enable_override)
            resolved = []
            for row, values, inherited in plan:
                found = ownership.find_entity(project, snapshot['kind'], row['uuid'])
                if found is None: raise ValueError('目标已被删除，请重新打开弹窗')
                entity, parent = found
                if _state(snapshot['kind'], entity, parent) != row:
                    raise ValueError('目标或继承掩码已变化，请重新打开弹窗')
                for field, value in values.items():
                    prop = entity.bl_rna.properties[field]
                    if not prop.hard_min <= value <= prop.hard_max:
                        raise ValueError('结果超出该字段的实际允许范围')
                resolved.append((entity, row, values, inherited))
            with preview.suspend_updates():
                _apply_mask_plan(resolved, snapshot['kind'])
            self.report({'INFO'}, f'已更新 {len(resolved)} 个实体的碰撞过滤')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


for _index in range(31):
    HD2PB_OT_edit_collision_mask.__annotations__[f'group_{_index + 1}'] = BoolProperty(
        name=f'组 {_index + 1}', description=f'组 {_index + 1}；位索引 {_index}；掩码 {1 << _index} / 0x{1 << _index:08X}',
        get=_bit_get(_index), set=_bit_set(_index))
for _field, _label, _action in (('all_groups', '全部31组', 'ALL'), ('invert_groups', '反选31组', 'INVERT'), ('clear_groups', '清空操作位', 'CLEAR')):
    HD2PB_OT_edit_collision_mask.__annotations__[_field] = BoolProperty(name=_label, get=lambda _self: False, set=_quick_set(_action))


def _display_snapshot(project):
    return dict(project_uuid=project.project_uuid, fields={f: bool(getattr(project, f)) for f in SHOW_FIELDS},
                isolation=project.preview_isolate_entity_uuid)


class HD2PB_OT_preview_focus(Operator):
    bl_idname = 'hd2pb.preview_focus'
    bl_label = '项目类别专注'
    bl_description = '仅改变当前物理项目分类显示及隔离；保留外部对象、集合和视口设置'
    bl_options = {'REGISTER', 'UNDO'}
    action: EnumProperty(items=(('NODES', '只看节点', ''), ('LIMITS', '只看角限', ''),
                               ('COLLIDERS', '只看碰撞体', ''), ('RESTORE', '恢复上次显示', '')), default='NODES')

    def execute(self, context):
        try:
            owner = ownership.project_owner(context)
            project = ownership.ensure_authoring_owner(owner)
            before, saved = _display_snapshot(project), project.preview_focus_snapshot
            if self.action == 'RESTORE':
                if not saved: raise ValueError('当前项目没有待恢复的专注显示快照')
                target = json.loads(saved)
                if set(target.get('fields', {})) == set(SHOW_FIELDS) - {'preview_show_node_spheres'}:
                    # Older snapshots predate the independent Empty display switch.
                    target['fields']['preview_show_node_spheres'] = True
                if target.get('project_uuid') != project.project_uuid or set(target.get('fields', {})) != set(SHOW_FIELDS):
                    raise ValueError('专注显示快照不属于当前项目或字段不完整')
                if any(type(v) is not bool for v in target['fields'].values()) or not isinstance(target.get('isolation'), str):
                    raise ValueError('专注显示快照格式无效')
                live = {uuid for _kind, uuid, _entity, _parent in ownership.entity_entries(project)}
                if target['isolation'] not in live: target['isolation'] = ''
                next_saved = ''
            else:
                if self.action not in FOCUS_FIELDS: raise ValueError('未知专注类别')
                focused = {FOCUS_FIELDS[self.action]}
                if self.action == 'NODES': focused.add('preview_show_node_spheres')
                target = dict(fields={f: f in focused for f in SHOW_FIELDS}, isolation='')
                next_saved = saved or json.dumps(before, ensure_ascii=False)
            with preview.suspend_updates():
                try:
                    for field, value in target['fields'].items(): setattr(project, field, value)
                    project.preview_isolate_entity_uuid = target['isolation']
                    project.preview_focus_snapshot = next_saved
                    preview.apply_project_style(owner)
                except Exception:
                    for field, value in before['fields'].items(): setattr(project, field, value)
                    project.preview_isolate_entity_uuid = before['isolation']
                    project.preview_focus_snapshot = saved
                    preview.apply_project_style(owner)
                    raise
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def draw_mask_field(layout, entity, field, *, parent=None):
    """Compact panel entry; decimal/hex editor and all 31 bits live in the dialog."""
    inherited = hasattr(entity, 'override_collision_filter') and not entity.override_collision_filter
    value = int(getattr(parent if inherited and parent is not None else entity, field))
    row = layout.row(align=True)
    row.operator_context = 'INVOKE_DEFAULT'
    title = '所属组' if field == 'group_mask' else '允许组'
    op = row.operator('hd2pb.edit_collision_mask', text=title + '：' + mask_summary(value), icon='GROUP')
    op.field_name = field
    if inherited: layout.label(text='继承骨链（编辑时需明确启用节点覆盖）', icon='LINKED')


CLASSES = (HD2PB_OT_edit_collision_mask, HD2PB_OT_preview_focus)
