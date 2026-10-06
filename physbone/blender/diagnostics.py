"""Session-only, read-only validation report. Never keeps live RNA references."""
from __future__ import annotations

import re
from copy import deepcopy

_REPORT = None
CLASSES = ()  # UI operators are registered exactly once through operators.CLASSES.


def clear():
    global _REPORT
    _REPORT = None


def present():
    return _REPORT is not None


def report():
    return deepcopy(_REPORT)


def _location(project, text):
    """Conservative exact-token resolution; ambiguity is deliberately not clickable."""
    quoted = set(re.findall(r'[“「"]([^”」"]+)[”」"]', text))
    tokens = set(re.split(r'\s*(?: / | ↔ |：|: )\s*', text)) | quoted
    matches = []
    for chain in project.chains:
        for node in list(chain.nodes)[1:]:
            if node.bone_name in tokens:
                matches.append(('NODE', node.uuid))
    if len(set(matches)) == 1:
        return matches[0]
    if matches:
        return '', ''
    for kind, items in (('CHAIN', project.chains), ('COLLIDER', project.colliders),
                        ('CHAIN_LINK', project.chain_links), ('PROFILE', project.profiles)):
        for item in items:
            if item.display_name in tokens:
                matches.append((kind, item.uuid))
    return matches[0] if len(set(matches)) == 1 else ('', '')


def publish(owner, rows, *, status):
    global _REPORT
    project = owner.data.hd2pb_project
    entries = []
    for row in rows:
        if isinstance(row, str):
            row = {'message': row, 'severity': 'INFO'}
        text = str(row['message'])
        kind, uuid = _location(project, str(row.get('where', text)))
        entries.append(dict(message=text, severity=row.get('severity', 'INFO'),
                            entity_type=kind, entity_uuid=uuid))
    _REPORT = dict(owner_pointer=owner.as_pointer(), owner_name=owner.name,
                   project_uuid=project.project_uuid, status=status, entries=entries)
    return report()


def matching_owner(context, saved):
    """Do not switch projects to make a stale or foreign diagnostic clickable."""
    from . import ownership
    owner = ownership.project_owner(context, remember=False)
    if (owner.as_pointer() != saved['owner_pointer'] or
            owner.data.hd2pb_project.project_uuid != saved['project_uuid'] or
            context.scene.objects.get(owner.name) != owner):
        raise ownership.HD2PBError('检查记录属于另一项目或已失效；请切回原项目并重新检查')
    ownership.ensure_authoring_owner(owner)
    return owner


def locate(context, index):
    """Select only an already visible, current-project controller; never reveal collections."""
    from . import ownership, preview, properties
    saved = report()
    if saved is None or not 0 <= index < len(saved['entries']):
        raise ownership.HD2PBError('检查记录已失效，请重新检查')
    owner = matching_owner(context, saved)
    row = saved['entries'][index]
    if not row['entity_uuid']:
        raise ownership.HD2PBError('此条目不能唯一定位；请按完整说明检查，未猜测目标')
    # Re-resolve UUID now; deletion/rebuild since the check cannot select an unrelated object.
    ownership.find_entity(owner.data.hd2pb_project, row['entity_type'], row['entity_uuid'])
    matches = ownership.controllers_for_entity(owner, saved['project_uuid'], row['entity_type'], row['entity_uuid'])
    if len(matches) != 1:
        raise ownership.HD2PBError('目标编辑对象不存在或重复，请先处理结构问题后重新检查')
    target = matches[0]
    preferred = {
        'CHAIN': properties.PREVIEW_CHAIN_CURVE,
        'COLLIDER': properties.PREVIEW_COLLIDER_SHAPE,
        'CHAIN_LINK': properties.PREVIEW_CHAIN_LINK,
        'LINK_COLLISION': properties.PREVIEW_LINK_COLLISION,
    }.get(row['entity_type'])
    if preferred is not None:
        visible_entries = [obj for obj in preview.project_preview_objects(owner)
                           if obj.hd2pb_entity_type == row['entity_type']
                           and obj.hd2pb_entity_uuid == row['entity_uuid']
                           and obj.hd2pb_preview_kind == preferred]
        if len(visible_entries) != 1:
            raise ownership.HD2PBError('实体可选预览不存在或重复，请先处理结构问题')
        target = visible_entries[0]
    ownership.resolve_controller(target, expected_type=row['entity_type'], require_unique=True)
    if context.mode != 'OBJECT':
        raise ownership.HD2PBError('请先切换到物体模式再定位；本操作不切换模式')
    if (context.view_layer.objects.get(target.name) != target or target.hide_select or
            target.hide_get() or not target.visible_get(view_layer=context.view_layer)):
        raise ownership.HD2PBError('目标当前隐藏、不可选或不在视图层；请手动显示，本操作不解除任何集合隐藏')
    for selected in tuple(context.selected_objects):
        selected.select_set(False)
    target.select_set(True)
    context.view_layer.objects.active = target
    return target
