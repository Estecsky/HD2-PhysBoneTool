"""Transactional boundary for preprocessed VRM physics; no mesh importer."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

from ..core.vrm_conversion import ConversionError, read_document, plan_conversion
from . import ownership, properties, preview
from .mmd_conversion import _uniform, _edit_helpers, _name


def _batch_addon():
    """Resolve the active Batch instance, never a disabled copy."""
    from .addon_bridge import resolve_addon
    return resolve_addon("BATCH", error_type=ConversionError)


def snapshot_from_armature(owner):
    if owner is None or owner.type != 'ARMATURE':
        raise ConversionError('请选择已导入、尚未经过 Batch 比例重拟合的源骨架')
    _uniform(owner.matrix_world, '源骨架世界矩阵')
    rotation = owner.matrix_world.to_quaternion().to_matrix().to_4x4()
    from .pruning import consumer_bones
    return {'consumer_bones': consumer_bones(owner),
            'world_rotation': [list(row) for row in rotation], 'bones': [
        {'name': b.name, 'parent': b.parent.name if b.parent else None,
         'head': list(b.head_local), 'tail': list(b.tail_local),
         'rest_basis': [float(b.matrix_local[r][c]) for r in range(3) for c in range(3)],
         'node_index': b.get('HD2PB_VRMNodeIndex'),
         'is_native': bool(b.get('HD2BT_WorkRigSourceBone', False))}
        for b in owner.data.bones if not b.get('HD2PB_GeneratedTip')]}


def convert_vrm(owner, filepath):
    if owner.get('HD2BT_WorkingRig') and owner.get('HD2BT_SourceArmature'):
        return _convert_working_rig(owner, filepath)
    return _convert_direct(owner, filepath)


def _convert_working_rig(owner, filepath):
    """Use the preserved Batch source Rest pose, then the existing transfer API.

    Never fit a VRM against game-native replacement bones. The temporary source
    project has explicit consumers from the real target; missing-bone pruning
    remains enabled. Only generated terminal helpers may be added to the target.
    """
    import bpy
    batch = _batch_addon()
    mmd_mapping = batch.mmd_mapping
    from .pruning import consumer_bones
    from .transfer import transfer_project
    from .operators import _snapshot_project, _restore_project
    source = bpy.data.objects.get(str(owner['HD2BT_SourceArmature']))
    if source is None or source.type != 'ARMATURE' or source is owner:
        raise ConversionError('找不到 Batch 保留的源骨架；请在重定向前的源模型副本上转换')
    project = owner.data.hd2pb_project
    if project.schema_version or project.chains or project.colliders:
        raise ConversionError('当前工作骨架已有物理，不覆盖；请在未转换的工程副本上操作')
    if len(ownership.armature_objects(owner.data)) != 1:
        raise ConversionError('请先让工作骨架成为单用户数据')
    profile_path = Path(str(owner.get('HD2BT_MappingPath', '')))
    if not profile_path.is_file():
        aq_presets = batch.aq_presets
        basename = profile_path.name
        candidates = [aq_presets.user_preset_dir(create=False) / basename, aq_presets.PRESET_DIR / basename]
        if bpy.data.filepath:
            candidates.append(Path(bpy.data.filepath).parent / basename)
        profile_path = next((p for p in candidates if p.is_file()), profile_path)
    if profile_path.suffix.lower() == '.py' and profile_path.is_file():
        # Parse literal mapping tables, never execute a supplied Python file.
        import ast
        module = ast.parse(profile_path.read_text(encoding='utf-8-sig'), filename=str(profile_path))
        pairs = batch.aq_presets._literal_assignment(module, 'rename_vg_fixed_name_list', profile_path)
        profile = {'name': profile_path.stem, 'weight_map': {}}
        for source_name, target_name in pairs:
            profile['weight_map'].setdefault(target_name, []).append(source_name)
    else:
        profile = mmd_mapping.load_mapping_profile(str(profile_path) if profile_path.is_file() else None)
    if owner.get('HD2BT_MappingName') != profile['name']:
        raise ConversionError('找不到此工作骨架对应的 Batch 映射配置，不能猜测物理绑定')
    # Validate aliases actually used by this model. An unrelated legacy alias
    # collision must not prevent converting an otherwise unambiguous skeleton.
    alias_targets = {}
    for destination, aliases in profile['weight_map'].items():
        for alias in aliases:
            alias_targets.setdefault(mmd_mapping.normalize_source_name(alias), set()).add(destination)
    mapping = {}
    for bone in source.data.bones:
        destinations = set()
        for alias in mmd_mapping._mmd_bone_names(source, bone):
            destinations.update(alias_targets.get(mmd_mapping.normalize_source_name(alias), ()))
        if len(destinations) > 1:
            raise ConversionError(f'骨骼 {bone.name} 在 Batch 字典中对应多个目标，不能猜测物理绑定')
        if destinations:
            mapping[bone.name] = next(iter(destinations))
    mapping.update({b.name: b.name for b in source.data.bones
                    if b.name not in mapping and b.name in owner.data.bones})
    mapping = {a: b for a, b in mapping.items() if b in owner.data.bones}
    consumers = consumer_bones(owner)
    source_consumers = None if consumers is None else [a for a, b in mapping.items() if b in consumers]
    saved = _snapshot_project(project)
    old_reports = {key: owner.get(key) for key in ('HD2PB_VRMConversionReport', 'HD2PB_TransferPruningReport')}
    temporary = staging = None
    created = []
    target_bones_before = set(owner.data.bones.keys())
    try:
        temporary = source.copy()
        temporary.data = source.data.copy()
        bpy.context.scene.collection.objects.link(temporary)
        report = _convert_direct(temporary, filepath, consumers=source_consumers)
        helpers = [b.name for b in temporary.data.bones if b.get('HD2PB_GeneratedTip')
                   and b.parent and b.parent.name in mapping]
        destination = owner
        rigging = importlib.import_module(f'{batch.__name__}.utils.rigging')
        POSE_INITIAL = importlib.import_module(f'{batch.__name__}.utils.constants').POSE_INITIAL
        if rigging.detect_body_pose_space(owner) == POSE_INITIAL and report['counts']['chains']:
            # VRM source Rest is T-pose; the current work rig may already have
            # been baked to INITIAL. Reuse Batch's physical rest-pose transport
            # on a disposable work-rig copy. Never re-pose the user's mesh/rig.
            staging = owner.copy()
            staging.data = owner.data.copy()
            bpy.context.scene.collection.objects.link(staging)
            rigging.convert_pose_space(staging, 'TO_TPOSE')
            destination = staging
        mmd_mapping._copy_custom_bones(temporary, destination, helpers, mapping, None)
        mapping.update({name: name for name in helpers if name in destination.data.bones})
        if report['counts']['chains']:
            transferred = transfer_project(temporary, destination, mapping, source_consumers=source_consumers)
            if staging is not None:
                rigging.convert_pose_space(staging, 'TO_INITIAL')
                mmd_mapping._copy_custom_bones(staging, owner, helpers,
                                               {n: n for n in target_bones_before}, None)
                transferred = transfer_project(staging, owner,
                    {b.name: b.name for b in staging.data.bones if b.name in owner.data.bones},
                    source_consumers=consumers)
                report['pose_transport'] = 'temporary_TPOSE_to_INITIAL_preserving_original_rig_and_meshes'
            report['working_rig_transfer'] = transferred
            report['counts'].update(chains=transferred['chains'], colliders=transferred['colliders'])
        created = list(set(owner.data.bones.keys()) - target_bones_before)
        for name in created:
            owner.data.bones[name]['HD2PB_GeneratedTip'] = True
        referenced = {n.bone_name for c in project.chains for n in c.nodes}
        referenced.update(c.bone_name for c in project.colliders)
        unused = [name for name in created if name not in referenced]
        if unused:
            _edit_helpers(owner, (), unused, remove=True)
            created = [name for name in created if name in referenced]
        report['conversion_route'] = 'batch_preserved_source_then_transfer'
        report['preserved_source_armature'] = source.name
        report['counts']['helpers'] = len(created)
        owner['HD2PB_VRMConversionReport'] = json.dumps(report, ensure_ascii=False, sort_keys=True)
        return report
    except Exception:
        with preview.suspend_updates():
            _restore_project(project, saved)
        # Include helpers created before a failed Batch helper-copy returned.
        created = list(set(owner.data.bones.keys()) - target_bones_before)
        if created:
            _edit_helpers(owner, (), created, remove=True)
        for key, value in old_reports.items():
            if value is None:
                if key in owner:
                    del owner[key]
            else:
                owner[key] = value
        raise
    finally:
        for obj in (staging, temporary):
            if obj is None:
                continue
            preview.discard_owner_updates(obj)
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if data.users == 0:
                bpy.data.armatures.remove(data)


def _convert_direct(owner, filepath, *, consumers=...):
    from .operators import _snapshot_project, _restore_project
    if len(ownership.armature_objects(owner.data)) != 1:
        raise ConversionError('请先让源骨架成为单用户数据')
    project = owner.data.hd2pb_project
    if project.chains or project.colliders or project.chain_links:
        raise ConversionError('当前骨架已有物理，不覆盖；请在源模型副本上转换')
    doc = read_document(filepath)
    source = snapshot_from_armature(owner)
    if consumers is not ...:
        source['consumer_bones'] = consumers
    plan = plan_conversion(doc, source, fixed_dt=project.fixed_dt)
    if not plan['chains']:
        reasons = '; '.join(r.get('reason', '') for r in plan['report']['springs'][:3])
        raise ConversionError(f'没有可转换链：{reasons}')
    from ..core.pruning import prune_conversion
    plan = prune_conversion(plan, source)
    if not plan['chains']:
        owner['HD2PB_VRMConversionReport'] = json.dumps(plan['report'], ensure_ascii=False, sort_keys=True)
        return plan['report']
    saved, created = _snapshot_project(project), []
    try:
        if plan['helpers']:
            _edit_helpers(owner, plan['helpers'], created)
        with preview.suspend_updates():
            if not ownership.project_initialized(project):
                project.schema_version = properties.PROJECT_SCHEMA_VERSION
                project.project_uuid = ownership.new_uuid()
                project.authoring_object = owner
            else:
                ownership.ensure_authoring_owner(owner)
            for i, entry in enumerate(plan['chains']):
                profile = project.profiles.add()
                profile.uuid = ownership.new_uuid()
                profile.display_name = _name('VRM近似_' + entry['name'], i)
                for key in ('stiffness', 'damping', 'gravity'):
                    setattr(profile, key, entry[key])
                chain = project.chains.add()
                chain.uuid, chain.spring_profile_uuid = ownership.new_uuid(), profile.uuid
                chain.display_name = _name(entry['name'], i)
                for key in ('group_mask', 'collision_mask', 'root_swing', 'local_bend'):
                    setattr(chain, key, entry[key])
                for values in entry['joints']:
                    node = chain.nodes.add()
                    node.uuid, node.bone_name = ownership.new_uuid(), values['bone']
                    for key, value in values.items():
                        if key != 'bone':
                            setattr(node, key, value)
            for i, values in enumerate(plan['colliders']):
                collider = project.colliders.add()
                collider.uuid, collider.display_name = ownership.new_uuid(), _name(values['name'], i)
                collider.bone_name = values['bone']
                for key, value in values.items():
                    if key not in ('name', 'bone'):
                        setattr(collider, key, value)
            project.new_chain_profile_uuid = project.profiles[-len(plan['chains'])].uuid
            issues = ownership.canonical_issues(owner)
            if issues:
                raise ConversionError('；'.join(issues[:5]))
            # Shared authoring projects are not a single Unit wire palette.
            # AQSDK partitions by actual mesh consumers after Batch KEEP.
            from .adapter_v1 import authoring_document_from_object
            required_count = len(authoring_document_from_object(owner)['bones'])
            plan['report']['authoring_physics_bones'] = required_count
            if required_count > 256:
                plan['report']['approximations'].append(
                    f'共享项目含 {required_count} 根物理相关骨；整项目单次预览超过 256 骨上限。'
                    '需按部位整理后预览/保存，每个最终 Unit 仍须通过原有骨数与槽位检查。')
            plan['report']['source_filename'] = Path(filepath).name
            # Hash only the parsed JSON, never read a potentially huge GLB BIN.
            plan['report']['source_json_sha256'] = hashlib.sha256(
                json.dumps(doc, sort_keys=True, ensure_ascii=False).encode('utf8')).hexdigest()
            owner['HD2PB_VRMConversionReport'] = json.dumps(plan['report'], ensure_ascii=False, sort_keys=True)
    except Exception:
        with preview.suspend_updates():
            _restore_project(project, saved)
        if created:
            _edit_helpers(owner, (), created, remove=True)
        raise
    return plan['report']
