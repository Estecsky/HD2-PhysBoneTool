"""Assign existing project profiles to selected chains without copying parameters."""
import bpy
from bpy.props import StringProperty
from bpy.types import Operator

from . import ownership, properties

_ENUM_CACHE = {}
_ENUM_NUMBERS = {}


def clear_runtime_state():
    _ENUM_CACHE.clear()
    _ENUM_NUMBERS.clear()


def runtime_state_present():
    return bool(_ENUM_CACHE or _ENUM_NUMBERS)


def _project(context):
    owner = ownership.project_owner(context, remember=False)
    project = ownership.ensure_authoring_owner(owner)
    return owner, project


def _identity(owner, project):
    return f'{owner.as_pointer()}:{project.project_uuid}'


def profile_items(_self, context):
    """Keep enum strings alive and bind numeric tokens to project/profile identity."""
    rows = []
    try:
        owner, project = _project(context)
        identity = _identity(owner, project)
        for profile in project.profiles:
            key = (identity, profile.uuid)
            if key not in _ENUM_NUMBERS:
                if len(_ENUM_NUMBERS) >= 16777214:
                    raise ValueError('配置选择数量超出会话范围，请重新启用插件')
                _ENUM_NUMBERS[key] = len(_ENUM_NUMBERS) + 1
            rows.append((profile.uuid, profile.display_name,
                         '当前项目参数配置：' + profile.display_name, _ENUM_NUMBERS[key]))
    except (ownership.HD2PBError, AttributeError, ReferenceError):
        pass
    rows = tuple(rows) or (('__NONE__', '当前项目没有参数配置', '请先新增弹簧参数配置', 0),)
    return _ENUM_CACHE.setdefault(rows, rows)


def selection_get(self):
    rows = profile_items(self, bpy.context)
    try:
        owner, project = _project(bpy.context)
        selected = self.get('_chain_profile_uuid', '') if self.get('_chain_profile_project', '') == _identity(owner, project) else ''
    except (ownership.HD2PBError, AttributeError, ReferenceError):
        selected = ''
    return next((row[3] for row in rows if row[0] == selected), rows[0][3])


def selection_set(self, value):
    owner, project = _project(bpy.context)
    row = next((row for row in profile_items(self, bpy.context) if row[3] == value), None)
    if row is None or row[0] == '__NONE__':
        raise ValueError('所选配置已经失效，请重新选择当前项目配置')
    self['_chain_profile_project'] = _identity(owner, project)
    self['_chain_profile_uuid'] = row[0]


def selected_chains(context):
    """Resolve only explicit selection, fail closed across project boundaries."""
    if context.mode != 'OBJECT':
        raise ownership.HD2PBError('请在物体模式多选骨链')
    owner, project = _project(context)
    chains = {}
    for obj in tuple(context.selected_objects or ()):
        if not (getattr(obj, 'hd2pb_controller_schema', 0) or getattr(obj, 'hd2pb_preview_schema', 0)):
            continue
        other_owner, other_project, entity, _parent = ownership.resolve_controller(obj, require_unique=True)
        if other_owner != owner or other_project.project_uuid != project.project_uuid:
            raise ownership.HD2PBError('选区含其他物理项目的实体；整批未修改')
        if obj.hd2pb_entity_type == properties.ENTITY_CHAIN:
            chains[entity.uuid] = entity
    if not chains:
        raise ownership.HD2PBError('请多选当前项目的骨链曲线；空选区不会应用到全部骨链')
    return owner, project, list(chains.values())


def _set_chain_profile(chain, profile_uuid):
    chain.spring_profile_uuid = profile_uuid


def apply_chain_profile(context, profile_uuid, *, project_uuid=''):
    owner, project, chains = selected_chains(context)
    if project_uuid and project.project_uuid != project_uuid:
        raise ownership.HD2PBError('当前项目已切换，请重新选择配置')
    profiles = [profile for profile in project.profiles if profile.uuid == profile_uuid]
    if len(profiles) != 1:
        raise ownership.HD2PBError('所选参数配置不存在或重复；整批未修改')
    previous = [(chain, chain.spring_profile_uuid) for chain in chains]
    try:
        for chain in chains:
            _set_chain_profile(chain, profile_uuid)
        issues = ownership.canonical_issues(owner)
        if issues:
            raise ownership.HD2PBError('项目数据校验失败：' + '；'.join(issues))
    except Exception:
        for chain, old in previous:
            chain.spring_profile_uuid = old
        raise
    return len(chains), profiles[0].display_name


def draw_panel(layout, context):
    box = layout.box()
    box.label(text='骨链参数预设')
    box.label(text='使用当前项目的参数配置，不复制参数', icon='LINKED')
    state = context.window_manager.hd2pb_preset_library
    box.prop(state, 'chain_profile', text='')
    row = box.row()
    row.operator_context = 'EXEC_DEFAULT'
    try:
        _owner, project, chains = selected_chains(context)
        selected = state.chain_profile
        enabled = any(profile.uuid == selected for profile in project.profiles)
        count = len(chains)
        message = f'所选骨链：{count} 条'
    except (ownership.HD2PBError, AttributeError, ReferenceError) as exc:
        enabled, count, selected, project = False, 0, '', None
        message = str(exc)
    row.enabled = enabled
    operator = row.operator('hd2pb.apply_chain_profile', text=f'应用配置到所选骨链（{count}）', icon='CHECKMARK')
    operator.profile_uuid = selected
    operator.project_uuid = project.project_uuid if project else ''
    box.label(text=message, icon='INFO')


class HD2PB_OT_apply_chain_profile(Operator):
    """为当前项目所选骨链批量指定已有参数配置。"""
    bl_idname = 'hd2pb.apply_chain_profile'
    bl_label = '应用骨链参数配置'
    bl_description = '把所选骨链共同指向当前项目的参数配置；仅改配置引用，失败整批回滚'
    bl_options = {'REGISTER', 'UNDO'}
    profile_uuid: StringProperty(name='参数配置', description='当前项目已有配置的稳定 UUID', options={'HIDDEN'})
    project_uuid: StringProperty(name='目标项目', description='防止点击应用时项目已经切换', options={'HIDDEN'})

    def execute(self, context):
        try:
            count, label = apply_chain_profile(context, self.profile_uuid, project_uuid=self.project_uuid)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f'已将 {count} 条骨链改用“{label}”；配置内容未修改')
        return {'FINISHED'}


CLASSES = (HD2PB_OT_apply_chain_profile,)
