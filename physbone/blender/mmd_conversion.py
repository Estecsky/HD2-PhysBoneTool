"""MMD RNA boundary. Preserve imported data; add only tagged unweighted tips."""
from __future__ import annotations

import json
import math
from mathutils import Vector

from ..core.mmd_conversion import ConversionError, plan_conversion
from . import ownership, properties, preview


def _root(obj):
    while obj is not None:
        if getattr(obj, "mmd_type", None) == "ROOT":
            return obj
        obj = obj.parent
    return None


def _uniform(matrix, label):
    if any(not math.isfinite(v) for row in matrix for v in row):
        raise ConversionError(f"{label} 包含非有限数值")
    basis = matrix.to_3x3()
    lengths = [c.length for c in basis.col]
    scale = sum(lengths) / 3
    if (not math.isfinite(scale) or scale < 1.e-8 or basis.determinant() <= 0 or
            max(lengths) - min(lengths) > scale * 1.e-4 or
            any(abs(basis.col[i].dot(basis.col[j])) > scale * scale * 1.e-4
                for i in range(3) for j in range(i))):
        raise ConversionError(f"{label} 包含镜像、剪切或非均匀缩放，请先在副本规范化")
    return scale


_MMD_PARENT_CONSTRAINTS = {"mmd_tools_rigid_parent", "mmd_tools_local_rigid_parent"}


def _rigid_owners(obj):
    """Use actual object links, never names or a scene-wide bone-name match."""
    owners = {c.target for c in obj.constraints
              if c.name in _MMD_PARENT_CONSTRAINTS and c.type == "CHILD_OF"
              and c.target is not None and c.target.type == "ARMATURE"}
    parent = obj.parent
    while parent is not None:
        if parent.type == "ARMATURE":
            owners.add(parent)
        parent = parent.parent
    return owners


def _source_objects(owner):
    import bpy
    root = _root(owner)
    if root is not None:
        objects = [o for o in bpy.data.objects if _root(o) == root]
        if sum(o.type == "ARMATURE" for o in objects) != 1:
            raise ConversionError("MMD 根下存在多个骨架，无法确定刚体所属骨架")
        return objects, {"layout": "MMD_ROOT", "root": root.name}
    # CATS removes ROOT but keeps both MMD metadata and CHILD_OF owner links.
    # An unrelated model with identical bone names must never enter this set.
    rigids = []
    for obj in bpy.context.scene.objects:
        if getattr(obj, "mmd_type", None) != "RIGID_BODY" or _root(obj) is not None:
            continue
        owners = _rigid_owners(obj)
        if owner not in owners:
            continue
        if owners != {owner}:
            raise ConversionError(f"刚体指向多个骨架，无法安全识别：{obj.name}")
        rigids.append(obj)
    if not rigids:
        raise ConversionError("未找到当前骨架的 MMD 刚体；CATS 模型需保留刚体及其骨架绑定，不按同名骨扫描其他模型")
    selected = set(rigids)
    joints, excluded = [], []
    for obj in bpy.context.scene.objects:
        if getattr(obj, "mmd_type", None) != "JOINT" or _root(obj) is not None:
            continue
        c = obj.rigid_body_constraint
        if c is None:
            continue
        ends = {o for o in (c.object1, c.object2) if o is not None}
        if not ends.intersection(selected):
            continue
        if ends <= selected:
            joints.append(obj)
        else:
            excluded.append(obj.name)
    groups = {o.parent for o in rigids if o.parent is not None}
    unresolved = [o.name for o in bpy.context.scene.objects
                  if getattr(o, "mmd_type", None) == "RIGID_BODY"
                  and o.parent in groups and not _rigid_owners(o) and o not in selected]
    return rigids + joints, {"layout": "CATS_LINKED", "root": None,
                            "unowned_rigids_not_imported": sorted(unresolved),
                            "cross_owner_joints_not_imported": sorted(excluded)}


def _bone_aliases(owner):
    aliases = {}
    for pose in owner.pose.bones:
        # Read the stored MMD names without invoking add-on getters or setters.
        props = pose.get("mmd_bone", {})
        for key in ("name_j", "name_e"):
            alias = props.get(key, "")
            if alias:
                aliases.setdefault(alias, set()).add(pose.name)
    return aliases


def _resolve_bone(owner, obj, source_name, aliases):
    linked = {c.subtarget for c in obj.constraints
              if c.name in _MMD_PARENT_CONSTRAINTS and c.type == "CHILD_OF"
              and c.target == owner and c.subtarget in owner.data.bones}
    if obj.parent == owner and obj.parent_type == "BONE" and obj.parent_bone in owner.data.bones:
        linked.add(obj.parent_bone)
    if len(linked) > 1:
        raise ConversionError(f"刚体包含冲突的骨绑定：{obj.name}")
    candidates = ({source_name} if source_name in owner.data.bones else aliases.get(source_name, set()))
    if linked:
        resolved = next(iter(linked))
        if candidates and resolved not in candidates:
            raise ConversionError(f"刚体骨绑定与 MMD 名称冲突：{obj.name}")
        return resolved, "object_link"
    if len(candidates) > 1:
        raise ConversionError(f"MMD 原名对应多个骨骼，不能猜测：{obj.name} / {source_name}")
    if candidates:
        return next(iter(candidates)), "bone_name" if source_name in owner.data.bones else "mmd_original_name"
    return source_name, "unresolved" if source_name else "unbound"


def snapshot_from_mmd(owner):
    if owner is None or owner.type != "ARMATURE":
        raise ConversionError("请选择导入 PMX 的源骨架")
    objects, recognition = _source_objects(owner)
    aliases = _bone_aliases(owner)
    _uniform(owner.matrix_world, "骨架世界矩阵")
    bones = [{"name": b.name, "parent": b.parent.name if b.parent else None,
              "head": list(b.head_local), "tail": list(b.tail_local),
              "rest_basis": [float(b.matrix_local[r][c]) for r in range(3) for c in range(3)]}
             for b in owner.data.bones if not b.get("HD2PB_GeneratedTip")]
    rigids, joints = [], []
    for obj in sorted(objects, key=lambda o: o.name):
        if getattr(obj, "mmd_type", None) == "RIGID_BODY":
            r, rb = obj.mmd_rigid, obj.rigid_body
            if rb is None:
                raise ConversionError(f"刚体缺少 Blender 刚体参数：{obj.name}")
            matrix = owner.matrix_world.inverted() @ obj.matrix_world
            scale = _uniform(matrix, obj.name)
            shape = r.shape
            sx, sy, sz = map(float, r.size)
            if any(not math.isfinite(s) or s < 0 for s in (sx, sy, sz)):
                raise ConversionError(f"刚体尺寸无效：{obj.name}")
            if shape == "SPHERE":
                radius, pa, pb, kind = sx, Vector(), Vector(), "SPHERE"
            elif shape == "CAPSULE":
                # mmd_tools makeCapsule: local Z, size.y is cylinder length.
                radius, pa, pb, kind = sx, Vector((0, 0, -sy / 2)), Vector((0, 0, sy / 2)), "CAPSULE"
            elif shape == "BOX":
                # Capsule encloses the BOX cross section. Exact dimensions remain
                # in the report; do not pretend the wire format supports boxes.
                sizes = (sx, sy, sz)
                axis = max(range(3), key=lambda i: sizes[i])
                radius = math.sqrt(sum(sizes[i] ** 2 for i in range(3) if i != axis))
                pa, pb = Vector(), Vector()
                pa[axis], pb[axis] = -sizes[axis], sizes[axis]
                kind = "CAPSULE"
            else:
                raise ConversionError(f"不支持的刚体形状：{shape}")
            bone_name, binding = _resolve_bone(owner, obj, r.bone, aliases)
            bone = owner.data.bones.get(bone_name)
            local = bone.matrix_local.inverted() @ matrix if bone else matrix
            rigids.append({"name": obj.name, "bone": bone_name, "source_bone": r.bone,
                           "binding_resolution": binding, "mode": r.type,
                           "shape": shape, "size": [sx, sy, sz], "kind": kind,
                           "a": list(local @ pa), "b": list(local @ pb), "radius": radius * scale,
                           "world_matrix": [list(row) for row in obj.matrix_world],
                           "group": int(r.collision_group_number), "excluded_groups": list(r.collision_group_mask),
                           "mass": float(rb.mass), "linear_damping": float(rb.linear_damping),
                           "angular_damping": float(rb.angular_damping), "friction": float(rb.friction),
                           "restitution": float(rb.restitution)})
        elif getattr(obj, "mmd_type", None) == "JOINT":
            c = obj.rigid_body_constraint
            if c is None:
                raise ConversionError(f"MMD Joint 缺少约束：{obj.name}")
            joints.append({"name": obj.name, "a": c.object1.name if c.object1 else None,
                           "b": c.object2.name if c.object2 else None,
                           "spring_linear": list(obj.mmd_joint.spring_linear),
                           "spring_angular": list(obj.mmd_joint.spring_angular),
                           "world_matrix": [list(row) for row in obj.matrix_world],
                           "constraints": {p.identifier: getattr(c, p.identifier) for p in c.bl_rna.properties
                                           if p.identifier.startswith(("limit_", "use_limit", "use_spring", "spring_"))}})
    if not rigids:
        raise ConversionError("当前 MMD 模型没有已导入的刚体；导入 PMX 时请勾选物理")
    recognition['unresolved_bone_bindings'] = [
        {"rigid": r['name'], "source_bone": r['source_bone']}
        for r in rigids if r['binding_resolution'] == 'unresolved']
    from .pruning import consumer_bones
    return {"bones": bones, "rigids": rigids, "joints": joints, "recognition": recognition,
            "consumer_bones": consumer_bones(owner)}


def _name(text, index):
    # Wire names are UTF-8, max 63 bytes. Index suffix guarantees uniqueness.
    suffix = f"_{index:03d}"
    return str(text).encode("utf-8")[:63 - len(suffix)].decode("utf-8", errors="ignore") + suffix


def _edit_helpers(owner, entries, created, *, remove=False):
    """Edit only our new bones and restore selection/active object on failure."""
    import bpy
    active = bpy.context.view_layer.objects.active
    selected = list(bpy.context.selected_objects)
    if bpy.context.mode != "OBJECT":
        raise ConversionError("请在物体模式执行 MMD 转换")
    try:
        for obj in selected: obj.select_set(False)
        owner.select_set(True)
        bpy.context.view_layer.objects.active = owner
        bpy.ops.object.mode_set(mode="EDIT")
        if remove:
            for name in created:
                bone = owner.data.edit_bones.get(name)
                if bone is not None: owner.data.edit_bones.remove(bone)
        else:
            for row in entries:
                if row['name'] in owner.data.edit_bones:
                    raise ConversionError(f"辅助骨名称已存在：{row['name']}")
                bone = owner.data.edit_bones.new(row['name'])
                created.append(bone.name)
                bone.parent = owner.data.edit_bones[row['parent']]
                bone.head, bone.tail = row['head'], row['tail']
                bone.align_roll(bone.parent.z_axis)
                bone.use_deform = False
        bpy.ops.object.mode_set(mode="OBJECT")
        if not remove:
            for name in created: owner.data.bones[name]['HD2PB_GeneratedTip'] = True
    finally:
        if owner.mode != "OBJECT": bpy.ops.object.mode_set(mode="OBJECT")
        owner.select_set(False)
        for obj in selected: obj.select_set(True)
        bpy.context.view_layer.objects.active = active


def convert_mmd(owner, *, mode="LIGHT"):
    """Create editable canonical physics + a complete report, with rollback.

    Existing authored physics is never overwritten. Explicit approximation opt-in
    belongs to the operator; this function also supports isolated automated tests.
    """
    from .operators import _snapshot_project, _restore_project
    if len(ownership.armature_objects(owner.data)) != 1:
        raise ConversionError("请先让 MMD 骨架成为单用户数据")
    project = owner.data.hd2pb_project
    if project.chains or project.colliders or project.chain_links:
        raise ConversionError("当前骨架已有物理内容，不覆盖；请在源模型副本上转换")
    source = snapshot_from_mmd(owner)
    plan = plan_conversion(source, fixed_dt=project.fixed_dt, mode=mode, complete_light=mode == "LIGHT")
    if not plan["chains"]:
        raise ConversionError("没有可转换的独立父子链；请先检查 MMD 刚体与骨绑定")
    from ..core.pruning import prune_conversion
    plan = prune_conversion(plan, source)
    if not plan['chains']:
        owner["HD2PB_MMDConversionReport"] = json.dumps(plan['report'], ensure_ascii=False, sort_keys=True)
        return plan['report']
    snapshot = _snapshot_project(project)
    created = []
    try:
        if plan.get('helpers'): _edit_helpers(owner, plan['helpers'], created)
        with preview.suspend_updates():
            if not ownership.project_initialized(project):
                project.schema_version = properties.PROJECT_SCHEMA_VERSION
                project.project_uuid = ownership.new_uuid()
                project.authoring_object = owner
            else:
                ownership.ensure_authoring_owner(owner)
            for i, entry in enumerate(plan["chains"]):
                profile = project.profiles.add()
                profile.uuid = ownership.new_uuid()
                profile.display_name = _name(("MMD轻量_" if mode == "LIGHT" else "MMD近似_") + entry["name"], i)
                profile.stiffness, profile.damping = entry["stiffness"], entry["damping"]
                profile.gravity = entry["gravity"]
                chain = project.chains.add()
                chain.uuid = ownership.new_uuid()
                chain.spring_profile_uuid = profile.uuid
                chain.display_name = _name(entry["name"], i)
                for key in ("group_mask", "collision_mask", "root_swing"):
                    setattr(chain, key, entry[key])
                chain.local_bend = entry.get('local_bend', False)
                for values in entry["joints"]:
                    node = chain.nodes.add()
                    node.uuid, node.bone_name = ownership.new_uuid(), values["bone"]
                    for key in ("radius", "stiffness_scale", "damping_scale"):
                        setattr(node, key, values[key])
                    if "limit_kind" in values:
                        node.limit_kind, node.limit_angle = values["limit_kind"], values["limit_angle"]
            for i, values in enumerate(plan["colliders"]):
                collider = project.colliders.add()
                collider.uuid, collider.display_name = ownership.new_uuid(), _name(values["name"], i)
                collider.bone_name = values["bone"]
                for key in ("kind", "a", "b", "radius", "group_mask", "collision_mask"):
                    setattr(collider, key, values[key])
            by_source_name = {entry['name']: chain for entry, chain in zip(plan['chains'], project.chains)}
            for i, values in enumerate(plan['chain_links']):
                link = project.chain_links.add()
                link.uuid, link.display_name = ownership.new_uuid(), _name(values['name'], i)
                link.chain_a_uuid = by_source_name[values['chain_a']].uuid
                link.chain_b_uuid = by_source_name[values['chain_b']].uuid
                link.coupling, link.max_correction = values['coupling'], values['max_correction']
                link.strip_constraint = bool(values['flags'] & 1)
            project.new_chain_profile_uuid = project.profiles[0].uuid
            issues = ownership.canonical_issues(owner)
            if issues:
                raise ConversionError("；".join(issues[:5]))
            plan["report"]["source"] = source
            owner["HD2PB_MMDConversionReport"] = json.dumps(plan["report"], ensure_ascii=False, sort_keys=True)
    except Exception:
        _restore_project(project, snapshot)
        if created: _edit_helpers(owner, (), created, remove=True)
        raise
    return plan["report"]
