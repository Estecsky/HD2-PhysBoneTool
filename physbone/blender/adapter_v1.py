from __future__ import annotations

from .. import Gate
from ..core import format_v1
from . import ownership


def _raise_issues(issues):
    if not issues:
        return
    suffix = f"（另有 {len(issues) - 1} 项问题）" if len(issues) > 1 else ""
    raise ownership.HD2PBError(issues[0] + suffix)


def _bone_rest_local(bone):
    matrix = bone.matrix_local
    if bone.parent is not None:
        matrix = bone.parent.matrix_local.inverted() @ matrix
    return [
        float(matrix[row][column])
        for row in range(3)
        for column in range(4)
    ]


def _collider_document(owner, collider):
    # Existing .blend canonical A/B stay in A's Rest-local coordinates. The
    # wire B is re-expressed in B's own Rest-local frame ONLY for dual capsules,
    # so old projects gain real endpoint following without moving their Rest.
    endpoint_b = list(collider.b)
    bone_b = str(collider.bone_name_b or "")
    if bone_b:
        from mathutils import Vector
        endpoint_b = list(owner.data.bones[bone_b].matrix_local.inverted() @
            owner.data.bones[collider.bone_name].matrix_local @ Vector(collider.b))
    result = {
        "name": collider.display_name, "kind": collider.kind, "bone": collider.bone_name,
        "group_mask": int(collider.group_mask), "collision_mask": int(collider.collision_mask),
        "dynamic": collider.attachment_mode == "DYNAMIC", "inside": bool(collider.inside),
        "a": list(collider.a), "b": endpoint_b, "radius": float(collider.radius),
    }
    if bone_b:
        result["bone_b"] = bone_b
    return result


def authoring_document_from_object(owner):
    """仅供制作检查的内存临时索引文档，不作为游戏目标导出。"""

    ownership.ensure_authoring_owner(owner)
    _raise_issues(ownership.canonical_issues(owner))
    project = owner.data.hd2pb_project

    required = {node.bone_name for chain in project.chains for node in chain.nodes}
    required.update(name for collider in project.colliders
                    for name in (collider.bone_name, collider.bone_name_b) if name)
    mapped = [bone for bone in owner.data.bones if bone.name in required]
    slots = {bone.name: index for index, bone in enumerate(mapped)}
    mapped_names = {bone.name for bone in mapped}
    bones = []
    for bone in mapped:
        parent_slot = (
            slots[bone.parent.name]
            if bone.parent is not None and bone.parent.name in mapped_names
            else -1
        )
        bones.append(
            {
                "name": bone.name,
                "palette_slot": slots[bone.name],
                "parent_slot": parent_slot,
                "rest_head": [float(value) for value in bone.matrix_local.translation],
                "rest_basis": [
                    float(bone.matrix_local[row][column])
                    for row in range(3)
                    for column in range(3)
                ],
            }
        )

    profiles = {profile.uuid: profile for profile in project.profiles}
    chains = []
    for chain in project.chains:
        profile = profiles[chain.spring_profile_uuid]
        chains.append(
            {
                "name": chain.display_name,
                "stiffness": float(profile.stiffness),
                "damping": float(profile.damping),
                "gravity": [float(value) for value in profile.gravity],
                "wind": [float(value) for value in profile.wind],
                "group_mask": int(chain.group_mask),
                "collision_mask": int(chain.collision_mask),
                "root_swing": bool(chain.root_swing),
                "local_bend": bool(chain.local_bend),
                "joints": [
                    {
                        "bone": node.bone_name,
                        "override_collision_filter": bool(node.override_collision_filter),
                        "group_mask": int(node.group_mask),
                        "collision_mask": int(node.collision_mask),
                        "radius": float(node.radius),
                        "limit_kind": node.limit_kind,
                        "limit_angle": float(node.limit_angle),
                        "limit_angle2": float(node.limit_angle2),
                        "limit_axis": [float(value) for value in node.limit_axis],
                        "stiffness_scale": float(node.stiffness_scale),
                        "damping_scale": float(node.damping_scale),
                        "reference_mode": str(getattr(node, "reference_mode", "ANIMATED_SEGMENT")),
                        "reference_direction": [
                            float(value)
                            for value in getattr(
                                node, "reference_direction", (0.0, 1.0, 0.0)
                            )
                        ],
                        "reference_frame": [
                            float(value)
                            for value in getattr(
                                node, "reference_frame", (0.0, 0.0, 0.0, 1.0)
                            )
                        ],
                    }
                    for node in chain.nodes
                ],
            }
        )

    colliders = [_collider_document(owner, collider) for collider in project.colliders]
    chain_names = {chain.uuid: chain.display_name for chain in project.chains}
    chain_links = [
        {
            "name": link.display_name,
            "chain_a": chain_names[link.chain_a_uuid],
            "chain_b": chain_names[link.chain_b_uuid],
            "coupling": float(link.coupling),
            "flags": 1 if link.strip_constraint else 0,
            "max_correction": float(link.max_correction),
            "collisions": [
                {
                    "local_joint_index": int(collision.local_joint_index),
                    "radius": float(collision.radius),
                    "group_mask": int(collision.group_mask),
                    "collision_mask": int(collision.collision_mask),
                }
                for collision in link.collisions
            ],
        }
        for link in project.chain_links
    ]
    return {
        "unit_id": 1,
        "palette_slots": len(mapped),
        "fixed_dt": float(project.fixed_dt),
        "bones": bones,
        "chains": chains,
        "colliders": colliders,
        "chain_links": chain_links,
    }


def preview_document_from_object(owner):
    """同求解器预览包含所需父链；只生成临时索引，不改作者骨架或目标槽位。"""
    document = authoring_document_from_object(owner)
    required = {bone["name"] for bone in document["bones"]}
    for name in tuple(required):
        bone = owner.data.bones[name].parent
        while bone is not None:
            required.add(bone.name)
            bone = bone.parent
    mapped = [bone for bone in owner.data.bones if bone.name in required]
    slots = {bone.name: index for index, bone in enumerate(mapped)}
    document["palette_slots"] = len(mapped)
    document["bones"] = [{
        "name": bone.name, "palette_slot": slots[bone.name],
        "parent_slot": slots[bone.parent.name] if bone.parent else -1,
        "rest_head": list(bone.matrix_local.translation),
        "rest_basis": [float(bone.matrix_local[r][c]) for r in range(3) for c in range(3)],
    } for bone in mapped]
    return document


def validate_authoring_object(owner):
    """只读验证制作参数；临时索引不写入骨架，不作为游戏目标绑定验收。"""
    try:
        return format_v1.validate(authoring_document_from_object(owner))
    except Gate as exc:
        raise _translate_gate(exc) from exc


def project_document_from_object(owner):
    """导出不绑定单个 Unit 的共享骨架物理制作数据。"""

    ownership.ensure_authoring_owner(owner)
    project = owner.data.hd2pb_project
    # Only this SDK bridge supports a chainless pose program. Direct HD2PHY1
    # export and existing spring validation retain their nonempty-chain gate.
    _raise_issues(ownership.canonical_issues(owner,
        require_chains=not any(rule.enabled for rule in project.pose_drivers)))
    profiles = {profile.uuid: profile for profile in project.profiles}
    chain_names = {chain.uuid: chain.display_name for chain in project.chains}

    document = {
        "schema": "HD2PhysBoneAuthoringProject1",
        "project_uuid": str(project.project_uuid),
        "fixed_dt": float(project.fixed_dt),
        "shared_bones": [
            {
                "name": bone.name,
                "parent": bone.parent.name if bone.parent is not None else None,
                "is_avatar_source": bool(bone.get("HD2BT_WorkRigSourceBone", False)),
                "rest_local": _bone_rest_local(bone),
            }
            for bone in owner.data.bones
        ],
        "chains": [
            {
                "name": chain.display_name,
                "stiffness": float(profiles[chain.spring_profile_uuid].stiffness),
                "damping": float(profiles[chain.spring_profile_uuid].damping),
                "gravity": [
                    float(value) for value in profiles[chain.spring_profile_uuid].gravity
                ],
                "wind": [float(value) for value in profiles[chain.spring_profile_uuid].wind],
                "group_mask": int(chain.group_mask),
                "collision_mask": int(chain.collision_mask),
                "root_swing": bool(chain.root_swing),
                "local_bend": bool(chain.local_bend),
                "joints": [
                    {
                        "bone": node.bone_name,
                        "override_collision_filter": bool(node.override_collision_filter),
                        "group_mask": int(node.group_mask),
                        "collision_mask": int(node.collision_mask),
                        "radius": float(node.radius),
                        "limit_kind": node.limit_kind,
                        "limit_angle": float(node.limit_angle),
                        "limit_angle2": float(node.limit_angle2),
                        "limit_axis": [float(value) for value in node.limit_axis],
                        "stiffness_scale": float(node.stiffness_scale),
                        "damping_scale": float(node.damping_scale),
                        "reference_mode": str(
                            getattr(node, "reference_mode", "ANIMATED_SEGMENT")
                        ),
                        "reference_direction": [
                            float(value)
                            for value in getattr(
                                node, "reference_direction", (0.0, 1.0, 0.0)
                            )
                        ],
                        "reference_frame": [
                            float(value)
                            for value in getattr(
                                node, "reference_frame", (0.0, 0.0, 0.0, 1.0)
                            )
                        ],
                    }
                    for node in chain.nodes
                ],
            }
            for chain in project.chains
        ],
        "colliders": [_collider_document(owner, collider) for collider in project.colliders],
        "chain_links": [
            {
                "name": link.display_name,
                "chain_a": chain_names[link.chain_a_uuid],
                "chain_b": chain_names[link.chain_b_uuid],
                "coupling": float(link.coupling),
                "flags": 1 if link.strip_constraint else 0,
                "max_correction": float(link.max_correction),
                "collisions": [
                    {
                        "local_joint_index": int(collision.local_joint_index),
                        "radius": float(collision.radius),
                        "group_mask": int(collision.group_mask),
                        "collision_mask": int(collision.collision_mask),
                    }
                    for collision in link.collisions
                ],
            }
            for link in project.chain_links
        ],
    }
    if project.pose_drivers:
        document["clothing_pose"] = clothing_pose_document(owner)
    return document


def clothing_pose_document(owner):
    """Read-only author bridge, never appends data to the legacy HD2PHY1."""
    import math
    from . import pose_drivers
    from ..core.pose_driver import PoseDriverError
    pose_drivers.validate(owner)
    # Armature-space channels cannot silently consume reflected/sheared or
    # non-uniform object transforms under the v1 contract.
    basis = owner.matrix_world.to_3x3()
    lengths = [basis.col[i].length for i in range(3)]
    if (not all(math.isfinite(v) and v > 1.e-8 for v in lengths)
            or max(lengths)-min(lengths) > 1.e-5*max(lengths)
            or basis.determinant() <= 0
            or any(abs(basis.col[i].dot(basis.col[j])) > 1.e-5*lengths[i]*lengths[j]
                   for i in range(3) for j in range(i))):
        raise PoseDriverError('衣物驱动导出不支持镜像、剪切或非均匀骨架对象缩放')
    project = owner.data.hd2pb_project
    if len(project.pose_drivers) > 64:
        raise PoseDriverError('衣物驱动首版最多支持64条规则（包含停用项）')
    rest = {bone.name: bone.matrix_local for bone in owner.data.bones}
    output, occupied = [], set()
    for rule in project.pose_drivers:
        if rule.target_bone not in rest or rule.target_bone in occupied:
            raise PoseDriverError('衣物驱动目标缺失或重复，停用项仍占用目标')
        occupied.add(rule.target_bone)
        row = {'name':rule.name, 'enabled':bool(rule.enabled), 'target':rule.target_bone}
        if rule.enabled:
            # Also checks stale calibration; manual author test sliders are
            # explicitly ignored and never included in the returned document.
            pose_drivers.input_vectors(owner, rule, rest, manual=False)
            def reference(source):
                parent = owner.data.bones[source].parent
                return rule.reference_bone or (parent.name if parent else '')
            def neutral(source, value):
                q = value if rule.calibrated else pose_drivers._relative(owner, rest, source, rule.reference_bone)
                return [float(v) for v in q]
            row.update(source=rule.source_bone, reference=reference(rule.source_bone),
                       neutral=neutral(rule.source_bone,rule.neutral_primary),
                       combination=rule.combination, influence=float(rule.influence))
            if rule.combination != 'SINGLE':
                row.update(secondary=rule.secondary_bone, secondary_reference=reference(rule.secondary_bone),
                           secondary_neutral=neutral(rule.secondary_bone,rule.neutral_secondary))
            row['axes'] = []
            for axis in 'XYZ':
                channel = pose_drivers.axis_curve(rule,axis)
                if not 2 <= len(channel.samples) <= 256:
                    raise PoseDriverError('衣物驱动每轴需2到256个采样点')
                row['axes'].append({'axis':axis,'invert':bool(channel.invert_input),
                    'secondary_axis':channel.secondary_axis,'invert_secondary':bool(channel.invert_secondary),
                    'points':sorted([[float(p.input_angle),list(p.output_xyz)] for p in channel.samples])})
        output.append(row)
    return {'schema':'clothing-pose-driver-v1','rules':output}


def _translate_gate(exc):
    # 保留底层首错顺序到控制台，界面只显示稳定的中文修复建议。
    print(f"HD2 PhysBone HD2PHY1 gate: {type(exc).__name__}: {exc}")
    return ownership.HD2PBError(
        "物理制作参数校验未通过。请检查骨链父子关系、"
        "参考方向、角度限制轴、碰撞体尺寸和掩码；详细开发原因已写入控制台"
    )


__all__ = (
    "authoring_document_from_object",
    "preview_document_from_object",
    "project_document_from_object",
    "clothing_pose_document",
    "validate_authoring_object",
)
