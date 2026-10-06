"""把 Armature canonical 投影为可选择 controller 与不可选择彩色 preview。"""

from __future__ import annotations

import bpy

from . import ownership, preview, properties


COLLECTION_SCHEMA_KEY = properties.COLLECTION_SCHEMA_KEY
COLLECTION_PROJECT_KEY = properties.COLLECTION_PROJECT_KEY
COLLECTION_OWNER_KEY = properties.COLLECTION_OWNER_KEY
COLLECTION_ROLE_KEY = properties.COLLECTION_ROLE_KEY
COLLECTION_ENTITY_KEY = properties.COLLECTION_ENTITY_KEY
COLLECTION_DISPLAY_NAME_KEY = properties.COLLECTION_DISPLAY_NAME_KEY

_COLLECTION_ROLES = {
    "ROOT",
    "CONFIG",
    "CHAINS",
    "COLLIDERS",
    "CHAIN_LINKS",
    "CHAIN",
    "CHAIN_LINK",
}


# 选择导航可能需要暂时解除当前项目对象的隐藏状态（例如用户在大纲视图
# 关闭了项目根集合）。保存“原值”和我们写入的“强制值”，恢复时只在值仍然
# 未被用户再次改动的情况下回放，避免覆盖用户在导航期间做出的新设置。
_SELECTION_VISIBILITY = {}


def _safe_label(value, fallback):
    text = " ".join(str(value or fallback).split())
    return text[:48] or fallback


def _id_exists(datablock, collection):
    return datablock is not None and collection.get(datablock.name) == datablock


def _tag_collection(collection, owner, project_uuid, role, entity_uuid=""):
    collection[COLLECTION_SCHEMA_KEY] = properties.CONTROLLER_SCHEMA_VERSION
    collection[COLLECTION_PROJECT_KEY] = project_uuid
    collection[COLLECTION_OWNER_KEY] = owner
    collection[COLLECTION_ROLE_KEY] = role
    collection[COLLECTION_ENTITY_KEY] = entity_uuid


def _collection_stage_name(project_uuid, role, entity_uuid=""):
    """为暂存集合生成不会与用户名称冲突的内部名称。"""

    project_token = str(project_uuid).replace("-", "") or "project"
    entity_token = str(entity_uuid).replace("-", "") or "root"
    return f"HD2PB_STAGE_{project_token}_{role}_{entity_token}"


def _collection_name_fit(value, byte_limit=63):
    text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    return encoded[:byte_limit].decode("utf-8", errors="ignore").rstrip() or "物理骨集合"


def _qualified_collection_name(desired, collection):
    role = str(collection.get(COLLECTION_ROLE_KEY, "collection")).lower()
    project = str(collection.get(COLLECTION_PROJECT_KEY, "project")).replace("-", "")[:8]
    entity = str(collection.get(COLLECTION_ENTITY_KEY, "")).replace("-", "")[:12]
    token = f" · hd2pb-{role}-{entity or 'root'}@{project or 'project'}"
    return _collection_name_fit(f"{desired}{token}")


def normalize_collection_names(collections):
    """在旧投影清理后稳定恢复新投影集合名称，不改写外部集合。"""

    for collection in tuple(collections or ()):
        if not _id_exists(collection, bpy.data.collections):
            continue
        desired = str(collection.get(COLLECTION_DISPLAY_NAME_KEY, "")).strip()
        if not desired:
            continue
        desired = _collection_name_fit(desired)
        occupant = bpy.data.collections.get(desired)
        if occupant is None or occupant is collection:
            collection.name = desired
            continue
        fallback = _qualified_collection_name(desired, collection)
        occupant = bpy.data.collections.get(fallback)
        if occupant is None or occupant is collection:
            collection.name = fallback
            continue
        # Duplicate internal identities should already be rejected by the
        # ownership gate; keep a readable suffix while repairing one.
        collection.name = _collection_name_fit(
            f"{fallback} · {collection.as_pointer():x}"
        )


def _is_owned_collection(collection, owner, project_uuid):
    return bool(
        collection is not None
        and collection.get(COLLECTION_SCHEMA_KEY) == properties.CONTROLLER_SCHEMA_VERSION
        and collection.get(COLLECTION_PROJECT_KEY) == project_uuid
        and collection.get(COLLECTION_OWNER_KEY) == owner
    )


def _is_owner_collection(collection, owner):
    return bool(
        collection is not None
        and collection.get(COLLECTION_SCHEMA_KEY) == properties.CONTROLLER_SCHEMA_VERSION
        and collection.get(COLLECTION_OWNER_KEY) == owner
    )


def _leaf_first(collections):
    """把无环 Collection 图排序为子级优先，避免依赖名称或创建顺序。"""

    remaining = list(dict.fromkeys(collections))
    ordered = []
    while remaining:
        leaves = [
            collection
            for collection in remaining
            if not any(child in remaining for child in collection.children)
        ]
        if not leaves:
            raise ownership.HD2PBError("物理骨管理集合形成了异常循环；为保护数据已拒绝删除")
        for collection in leaves:
            remaining.remove(collection)
            ordered.append(collection)
    return ordered


def _owner_scene(owner, context_scene=None):
    if context_scene is not None and owner.name in context_scene.objects:
        return context_scene
    scenes = list(owner.users_scene)
    if scenes:
        return scenes[0]
    raise ownership.HD2PBError(
        f"Armature 对象“{owner.name}”没有链接到任何场景，无法创建 Outliner 编辑集合"
    )


def _projection_cleanup_plan(owner, project_uuid, *, context_scene=None):
    """只读预检旧投影；发现用户内容或归属不明数据时 fail closed。"""

    scene = _owner_scene(owner, context_scene)
    project = owner.data.hd2pb_project
    root = project.controller_collection
    if root is not None:
        if not _id_exists(root, bpy.data.collections):
            raise ownership.HD2PBError("项目记录的 Outliner 根集合已经失效；请保存副本后重新载入")
        if not _is_owned_collection(root, owner, project_uuid):
            raise ownership.HD2PBError(
                "项目记录的 Outliner 根集合缺少精确 owner 标签或指向其他项目；"
                "为避免误删用户集合，已拒绝重建"
            )
        if root.get(COLLECTION_ROLE_KEY) != "ROOT":
            raise ownership.HD2PBError("项目记录的 Outliner 根集合角色损坏；为保护数据已拒绝重建")

    collections = []
    for collection in bpy.data.collections:
        owner_match = collection.get(COLLECTION_OWNER_KEY) == owner
        project_match = collection.get(COLLECTION_PROJECT_KEY) == project_uuid
        if owner_match != project_match:
            raise ownership.HD2PBError(
                f"管理集合“{collection.name}”只有部分 owner/project 标签匹配；"
                "无法证明可安全删除，请先保存副本并人工检查"
            )
        if not owner_match:
            continue
        if collection.get(COLLECTION_SCHEMA_KEY) != properties.CONTROLLER_SCHEMA_VERSION:
            raise ownership.HD2PBError(
                f"管理集合“{collection.name}”的版本标签损坏；为保护数据已拒绝重建"
            )
        collections.append(collection)
    if root is not None and root not in collections:
        raise ownership.HD2PBError("项目根集合所有权不完整；为保护数据已拒绝重建")

    collection_set = set(collections)
    managed_objects = set()
    for collection in collections:
        if collection.library is not None:
            raise ownership.HD2PBError(
                f"管理集合“{collection.name}”来自外部库，不能安全重建本地投影"
            )
        role = collection.get(COLLECTION_ROLE_KEY)
        if role not in _COLLECTION_ROLES:
            raise ownership.HD2PBError(
                f"管理集合“{collection.name}”角色标签损坏；为保护数据已拒绝重建"
            )
        for child in collection.children:
            if child not in collection_set:
                raise ownership.HD2PBError(
                    f"管理集合“{collection.name}”下包含未由本项目明确拥有的子集合“{child.name}”；"
                    "请先把该集合移出 HD2 物理骨根目录，再执行重建"
                )
        for obj in collection.objects:
            managed_objects.add(obj)
            is_controller = bool(getattr(obj, "hd2pb_controller_schema", 0))
            is_generated_preview = bool(getattr(obj, "hd2pb_preview_schema", 0))
            if is_controller and is_generated_preview:
                raise ownership.HD2PBError(
                    f"管理集合“{collection.name}”中的对象“{obj.name}”同时声明为 controller 和 preview；"
                    "为避免误删已拒绝重建"
                )
            if not is_controller and not is_generated_preview:
                raise ownership.HD2PBError(
                    f"管理集合“{collection.name}”中包含用户对象“{obj.name}”；"
                    "为避免误删对象，已拒绝重建"
                )
            if (
                getattr(obj, "hd2pb_owner_object", None) != owner
                or getattr(obj, "hd2pb_project_uuid", "") != project_uuid
            ):
                raise ownership.HD2PBError(
                    f"管理集合“{collection.name}”中包含其他项目的编辑对象“{obj.name}”；"
                    "为避免交叉删除，已拒绝重建"
                )
            if is_controller:
                if (
                    obj.hd2pb_controller_schema != properties.CONTROLLER_SCHEMA_VERSION
                    or not ownership.is_valid_uuid(obj.hd2pb_controller_uuid)
                ):
                    raise ownership.HD2PBError(
                        f"controller“{obj.name}”的版本或 UUID 标签损坏；为保护数据已拒绝重建"
                    )
            elif not preview.is_exact_preview_object(obj, owner, project_uuid):
                raise ownership.HD2PBError(
                    f"预览对象“{obj.name}”的 owner、类型或 UUID 标签损坏；"
                    "为保护数据已拒绝重建"
                )
            external_collections = [
                current for current in obj.users_collection if current not in collection_set
            ]
            if external_collections:
                raise ownership.HD2PBError(
                    f"编辑对象“{obj.name}”还链接到项目外集合“{external_collections[0].name}”；"
                    "为保护外部层级已拒绝重建"
                )

    objects = list(managed_objects)
    for obj in bpy.data.objects:
        if not (
            getattr(obj, "hd2pb_controller_schema", 0)
            or getattr(obj, "hd2pb_preview_schema", 0)
        ):
            continue
        if obj in managed_objects:
            continue
        owner_match = getattr(obj, "hd2pb_owner_object", None) == owner
        project_match = getattr(obj, "hd2pb_project_uuid", "") == project_uuid
        if owner_match != project_match:
            raise ownership.HD2PBError(
                f"项目根集合外的编辑对象“{obj.name}”只有部分 owner/project 标签匹配；"
                "无法证明可安全删除，请先保存副本并人工检查"
            )
        if owner_match and project_match:
            if obj.library is not None:
                raise ownership.HD2PBError(
                    f"编辑对象“{obj.name}”来自外部库，不能安全重建本地投影"
                )
            is_controller = bool(getattr(obj, "hd2pb_controller_schema", 0))
            is_generated_preview = bool(getattr(obj, "hd2pb_preview_schema", 0))
            if is_controller == is_generated_preview:
                raise ownership.HD2PBError(
                    f"项目根集合外的编辑对象“{obj.name}”身份类型不唯一；为保护数据已拒绝重建"
                )
            if is_controller and (
                obj.hd2pb_controller_schema != properties.CONTROLLER_SCHEMA_VERSION
                or not ownership.is_valid_uuid(obj.hd2pb_controller_uuid)
            ):
                raise ownership.HD2PBError(
                    f"项目根集合外的 controller“{obj.name}”版本或 UUID 损坏；已拒绝重建"
                )
            if is_generated_preview and not preview.is_exact_preview_object(
                obj,
                owner,
                project_uuid,
            ):
                raise ownership.HD2PBError(
                    f"项目根集合外的预览对象“{obj.name}”标签损坏；已拒绝重建"
                )
            objects.append(obj)

    preview_objects = [obj for obj in objects if getattr(obj, "hd2pb_preview_schema", 0)]
    preview_data = []
    materials = []
    for obj in preview_objects:
        if obj.hd2pb_controller_schema:
            raise ownership.HD2PBError(
                f"对象“{obj.name}”同时声明为 controller 和 preview；为保护数据已拒绝重建"
            )
        data = obj.data
        is_limit_axis = (
            getattr(obj, "hd2pb_preview_kind", properties.PREVIEW_NONE)
            == properties.PREVIEW_NODE_LIMIT_AXIS
        )
        is_collider_endpoint = (
            getattr(obj, "hd2pb_preview_kind", properties.PREVIEW_NONE)
            == properties.PREVIEW_COLLIDER_ENDPOINT
        )
        if (not is_limit_axis and not is_collider_endpoint) and (data is None or obj.type not in {"MESH", "CURVE"}):
            raise ownership.HD2PBError(
                f"预览对象“{obj.name}”不是当前 Curve 或可清理的旧版 Mesh；为保护数据已拒绝重建"
            )
        if (is_limit_axis or is_collider_endpoint) and (obj.type != "EMPTY" or data is not None):
            raise ownership.HD2PBError(
                f"角限旋转轴“{obj.name}”不是可清理的 Empty；为保护数据已拒绝重建"
            )
        if is_limit_axis or is_collider_endpoint:
            continue
        if data.library is not None:
            raise ownership.HD2PBError(f"预览几何“{data.name}”来自外部库，不能安全重建")
        if (
            data.get(preview.DATA_SCHEMA_KEY) != properties.PREVIEW_SCHEMA_VERSION
            or data.get(preview.DATA_PROJECT_KEY) != project_uuid
            or data.get(preview.DATA_OWNER_KEY) != owner
            or data.get(preview.DATA_ENTITY_TYPE_KEY) != obj.hd2pb_entity_type
            or data.get(preview.DATA_ENTITY_KEY) != obj.hd2pb_entity_uuid
            or data.get(preview.DATA_KIND_KEY) != obj.hd2pb_preview_kind
            or data.get(preview.DATA_CHAIN_KEY, "") != obj.get(preview.DATA_CHAIN_KEY, "")
            or not ownership.is_valid_uuid(data.get(preview.DATA_UUID_KEY, ""))
        ):
            raise ownership.HD2PBError(
                f"预览对象“{obj.name}”的数据块所有权标签不完整；为避免误删已拒绝重建"
            )
        preview_data.append(data)
        for material in data.materials:
            if material is None:
                continue
            if material.library is not None:
                raise ownership.HD2PBError(
                    f"预览材质“{material.name}”来自外部库，不能安全重建"
                )
            if (
                material.get(preview.DATA_SCHEMA_KEY) != properties.PREVIEW_SCHEMA_VERSION
                or material.get(preview.DATA_PROJECT_KEY) != project_uuid
                or material.get(preview.DATA_OWNER_KEY) != owner
                or material.get(preview.DATA_ENTITY_TYPE_KEY) != properties.ENTITY_PROJECT
                or material.get(preview.DATA_ENTITY_KEY) != project_uuid
                or material.get(preview.DATA_KIND_KEY) != obj.hd2pb_preview_kind
                or material.get(preview.DATA_CHAIN_KEY, "") != ""
                or not preview.material_variant_is_valid(
                    obj.hd2pb_preview_kind,
                    str(material.get(preview.DATA_MATERIAL_VARIANT_KEY, "")),
                )
                or not ownership.is_valid_uuid(material.get(preview.DATA_UUID_KEY, ""))
            ):
                raise ownership.HD2PBError(
                    f"预览材质“{material.name}”缺少精确项目标签；为避免误删已拒绝重建"
                )
            materials.append(material)

    # 旧版本或中途异常可能留下没有 Object 用户的带标签 orphan。只收集精确
    # owner/project 的生成数据；部分标签匹配或外部用户会在下方 fail closed。
    known_data = set(preview_data)
    for datablocks in (bpy.data.meshes, bpy.data.curves):
        for data in datablocks:
            owner_match = data.get(preview.DATA_OWNER_KEY) == owner
            project_match = data.get(preview.DATA_PROJECT_KEY) == project_uuid
            if owner_match != project_match:
                raise ownership.HD2PBError(
                    f"预览几何“{data.name}”只有部分 owner/project 标签匹配；"
                    "为保护用户数据已拒绝重建"
                )
            if owner_match and project_match:
                if data.library is not None:
                    raise ownership.HD2PBError(
                        f"预览几何“{data.name}”来自外部库，不能安全重建"
                    )
                if (
                    data.get(preview.DATA_SCHEMA_KEY) != properties.PREVIEW_SCHEMA_VERSION
                    or data.get(preview.DATA_KIND_KEY) not in preview._KIND_LABELS
                    or data.get(preview.DATA_ENTITY_TYPE_KEY)
                    not in {
                        properties.ENTITY_CHAIN,
                        properties.ENTITY_NODE,
                        properties.ENTITY_COLLIDER,
                        properties.ENTITY_CHAIN_LINK,
                        properties.ENTITY_LINK_COLLISION,
                    }
                    or not ownership.is_valid_uuid(data.get(preview.DATA_ENTITY_KEY, ""))
                    or not ownership.is_valid_uuid(data.get(preview.DATA_UUID_KEY, ""))
                ):
                    raise ownership.HD2PBError(
                        f"预览几何“{data.name}”的生成身份标签损坏；为保护数据已拒绝重建"
                    )
                known_data.add(data)
    unique_data = list(known_data)
    for data in unique_data:
        owned_users = sum(1 for obj in objects if obj.data == data)
        if data.users > owned_users:
            raise ownership.HD2PBError(
                f"预览几何“{data.name}”还被项目外对象使用；为保护用户数据已拒绝重建"
            )
    known_materials = set(materials)
    for material in bpy.data.materials:
        owner_match = material.get(preview.DATA_OWNER_KEY) == owner
        project_match = material.get(preview.DATA_PROJECT_KEY) == project_uuid
        if owner_match != project_match:
            raise ownership.HD2PBError(
                f"预览材质“{material.name}”只有部分 owner/project 标签匹配；"
                "为保护用户数据已拒绝重建"
            )
        if owner_match and project_match:
            if material.library is not None:
                raise ownership.HD2PBError(
                    f"预览材质“{material.name}”来自外部库，不能安全重建"
                )
            if (
                material.get(preview.DATA_SCHEMA_KEY) != properties.PREVIEW_SCHEMA_VERSION
                or material.get(preview.DATA_ENTITY_TYPE_KEY) != properties.ENTITY_PROJECT
                or material.get(preview.DATA_ENTITY_KEY) != project_uuid
                or material.get(preview.DATA_KIND_KEY) not in preview._KIND_LABELS
                or material.get(preview.DATA_CHAIN_KEY, "") != ""
                or not preview.material_variant_is_valid(
                    material.get(preview.DATA_KIND_KEY),
                    str(material.get(preview.DATA_MATERIAL_VARIANT_KEY, "")),
                )
                or not ownership.is_valid_uuid(material.get(preview.DATA_UUID_KEY, ""))
            ):
                raise ownership.HD2PBError(
                    f"预览材质“{material.name}”的生成身份标签损坏；为保护数据已拒绝重建"
                )
            known_materials.add(material)
    unique_materials = list(known_materials)
    for material in unique_materials:
        preview_objects = [
            obj for obj in objects if getattr(obj, "hd2pb_preview_schema", 0)
        ]
        owned_users = preview.material_reference_count(
            preview_objects,
            material,
            datablocks=unique_data,
        )
        if material.users > owned_users:
            raise ownership.HD2PBError(
                f"预览材质“{material.name}”还被项目外数据使用；为保护用户数据已拒绝重建"
            )

    # Object parent、Constraint/Modifier/Driver、额外 Collection/Scene link 等
    # 也会形成 ID 用户。逐项猜测 Blender 的引用类型容易漏项，因此用 user_map
    # 统一拒绝删除集合外用户；旧 ROOT 的当前项目指针和唯一 Scene link 是预期引用。
    deletion_ids = set(objects) | set(unique_data) | set(unique_materials) | collection_set
    if deletion_ids:
        allowed_scene_users = {scene, scene.collection}
        for datablock, users in bpy.data.user_map(subset=deletion_ids).items():
            external = set(users) - deletion_ids - allowed_scene_users
            if datablock == root:
                external.discard(owner.data)
            if external:
                user = sorted(external, key=lambda item: (type(item).__name__, item.name))[0]
                raise ownership.HD2PBError(
                    f"生成数据“{datablock.name}”仍被项目外数据“{user.name}”引用；"
                    "为保护用户层级、约束和驱动，已拒绝重建"
                )
            if getattr(datablock, "use_fake_user", False):
                raise ownership.HD2PBError(
                    f"生成数据“{datablock.name}”启用了虚拟用户（Fake User）；"
                    "无法证明可安全删除，已拒绝重建"
                )

    return {
        "scene": scene,
        "objects": objects,
        "preview_data": unique_data,
        "materials": unique_materials,
        "collections": _leaf_first(collections),
    }


def preflight_project_rebuild(owner, *, context_scene=None):
    """在 canonical 修改前验证重建不会触及未明确拥有的用户数据。"""

    project = ownership.ensure_authoring_owner(owner)
    return _projection_cleanup_plan(
        owner,
        project.project_uuid,
        context_scene=context_scene,
    )


def _execute_cleanup(plan):
    """提交新投影后尽力回收旧生成物；单项失败不会破坏新投影。"""

    errors = []
    ordered_objects = sorted(
        tuple(plan["objects"]),
        key=lambda obj: 0 if getattr(obj, "hd2pb_preview_schema", 0) else 1,
    )
    for obj in ordered_objects:
        if not _id_exists(obj, bpy.data.objects):
            continue
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception as exc:
            errors.append(f"对象“{obj.name}”：{exc}")
    for data in tuple(plan.get("preview_data", ())):
        if data.users:
            continue
        try:
            if isinstance(data, bpy.types.Mesh) and _id_exists(data, bpy.data.meshes):
                bpy.data.meshes.remove(data)
            elif isinstance(data, bpy.types.Curve) and _id_exists(data, bpy.data.curves):
                bpy.data.curves.remove(data)
        except Exception as exc:
            errors.append(f"预览几何“{data.name}”：{exc}")
    for collection in tuple(plan["collections"]):
        if not _id_exists(collection, bpy.data.collections):
            continue
        try:
            bpy.data.collections.remove(collection)
        except Exception as exc:
            errors.append(f"集合“{collection.name}”：{exc}")
    for material in tuple(plan.get("materials", ())):
        if material.users or not _id_exists(material, bpy.data.materials):
            continue
        try:
            bpy.data.materials.remove(material)
        except Exception as exc:
            errors.append(f"预览材质“{material.name}”：{exc}")
    return errors


def remove_project_projection(owner, project_uuid, *, context_scene=None):
    """按精确 owner/UUID 标签移除本项目投影；污染时拒绝，不做名称通配。"""

    plan = _projection_cleanup_plan(owner, project_uuid, context_scene=context_scene)
    errors = _execute_cleanup(plan)
    if errors:
        raise RuntimeError("物理骨投影清理不完整：" + "；".join(errors))
    owner.data.hd2pb_project.controller_collection = None


def _new_collection(name, parent, owner, project_uuid, role, entity_uuid=""):
    collection = bpy.data.collections.new(
        _collection_stage_name(project_uuid, role, entity_uuid)
    )
    try:
        _tag_collection(collection, owner, project_uuid, role, entity_uuid)
        collection[COLLECTION_DISPLAY_NAME_KEY] = _collection_name_fit(name)
        parent.children.link(collection)
    except Exception:
        if _id_exists(collection, bpy.data.collections):
            bpy.data.collections.remove(collection)
        raise
    return collection


def _set_controller_identity(obj, owner, project_uuid, entity_type, entity_uuid):
    obj.hd2pb_controller_schema = properties.CONTROLLER_SCHEMA_VERSION
    obj.hd2pb_project_uuid = project_uuid
    obj.hd2pb_entity_type = entity_type
    obj.hd2pb_entity_uuid = entity_uuid
    obj.hd2pb_controller_uuid = ownership.new_uuid()
    obj.hd2pb_owner_object = owner


def _attach_to_owner(obj, owner, bone_name=""):
    from . import binding
    binding.attach(obj, owner, bone_name, preserve_legacy=True)
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)
    obj.lock_location = (True, True, True)
    obj.lock_rotation = (True, True, True)
    obj.lock_scale = (True, True, True)


def _new_controller(
    collection,
    owner,
    project_uuid,
    entity_type,
    entity_uuid,
    name,
    *,
    bone_name="",
    display_type="PLAIN_AXES",
    display_size=0.06,
):
    obj = bpy.data.objects.new(name, None)
    try:
        collection.objects.link(obj)
        _set_controller_identity(obj, owner, project_uuid, entity_type, entity_uuid)
        # Keep the requested user-facing label independent from Blender's global
        # object-name allocator.  A later staged swap normalizes names once the
        # previous project projection has been removed.
        obj[preview.DISPLAY_NAME_KEY] = preview._safe_label(name, "物理骨对象")
        obj.empty_display_type = display_type
        obj.empty_display_size = max(
            float(display_size),
            properties.NODE_DISPLAY_MIN_RADIUS,
        )
        obj.hide_render = True
        # 项目、参数、链组和内部 Collider controller 只作为 Outliner/属性
        # 导航入口，绝不在 3D 视图绘制名称，避免大量标签重叠。
        obj.show_name = False
        # 内部 controller 仍需能从 Outliner 选择，但不能占据 3D 视图。
        # hide_viewport 会让 Outliner 条目变暗，因此使用 Blender Empty 的
        # 最小显示尺寸代替隐藏标志，保持条目可读且不在正常视图中干扰用户。
        if entity_type != properties.ENTITY_NODE:
            obj.empty_display_size = 0.0001
        obj.hide_viewport = False
        obj.hide_select = False
        _attach_to_owner(obj, owner, bone_name)
        if bpy.context.view_layer is not None and obj.name in bpy.context.view_layer.objects:
            obj.select_set(False)
    except Exception:
        if _id_exists(obj, bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        raise
    return obj


def _discard_staged_projection(objects, collections, preview_stage=None):
    errors = []
    if preview_stage is not None:
        try:
            preview.discard_stage(preview_stage)
        except Exception as exc:
            errors.append(str(exc))
    for obj in reversed(tuple(objects)):
        if not _id_exists(obj, bpy.data.objects):
            continue
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception as exc:
            errors.append(f"对象 {obj.name}: {exc}")
    try:
        ordered = _leaf_first(
            [
                collection
                for collection in collections
                if _id_exists(collection, bpy.data.collections)
            ]
        )
    except Exception as exc:
        errors.append(str(exc))
        ordered = []
    for collection in ordered:
        try:
            bpy.data.collections.remove(collection)
        except Exception as exc:
            errors.append(f"集合 {collection.name}: {exc}")
    if errors:
        raise RuntimeError("临时物理骨投影清理失败：" + "; ".join(errors))


def _build_staged_projection(owner, project):
    """在未链接到 Scene 的集合树中构造完整新投影。"""

    project_uuid = project.project_uuid
    root_desired_name = f"HD2 物理骨 - {_safe_label(owner.name, '骨架')}"
    root = bpy.data.collections.new(
        _collection_stage_name(project.project_uuid, "ROOT")
    )
    collections = [root]
    created = []
    preview_stage = preview.new_stage()
    try:
        _tag_collection(root, owner, project_uuid, "ROOT")
        root[COLLECTION_DISPLAY_NAME_KEY] = _collection_name_fit(root_desired_name)
        config = _new_collection("配置", root, owner, project_uuid, "CONFIG")
        chains_root = _new_collection("链组", root, owner, project_uuid, "CHAINS")
        colliders_root = _new_collection("碰撞体", root, owner, project_uuid, "COLLIDERS")
        chain_links_root = _new_collection(
            "链间连接", root, owner, project_uuid, "CHAIN_LINKS"
        )
        collections.extend((config, chains_root, colliders_root, chain_links_root))

        created.append(
            _new_controller(
                config,
                owner,
                project_uuid,
                properties.ENTITY_PROJECT,
                project.project_uuid,
                f"物理骨项目 · {_safe_label(owner.name, '骨架')}",
                display_type="CUBE",
                display_size=0.08,
            )
        )
        for profile in project.profiles:
            created.append(
                _new_controller(
                    config,
                    owner,
                    project_uuid,
                    properties.ENTITY_PROFILE,
                    profile.uuid,
                    f"参数 · {_safe_label(profile.display_name, '未命名')}",
                    display_type="CIRCLE",
                    display_size=0.06,
                )
            )
        for chain in project.chains:
            chain_collection = _new_collection(
                f"链 · {_safe_label(chain.display_name, '未命名')}",
                chains_root,
                owner,
                project_uuid,
                "CHAIN",
                chain.uuid,
            )
            collections.append(chain_collection)
            chain_controller = _new_controller(
                    chain_collection,
                    owner,
                    project_uuid,
                    properties.ENTITY_CHAIN,
                    chain.uuid,
                    f"链 · {_safe_label(chain.display_name, '未命名')}",
                    display_type="PLAIN_AXES",
                    display_size=0.001,
                )
            chain_controller.hide_select = True
            created.append(chain_controller)
            node_controllers = []
            # nodes[0] 只保存运行时固定锚点，不生成 Outliner/3D 选择代理。
            for node in list(chain.nodes)[1:]:
                node_controller = _new_controller(
                        chain_collection,
                        owner,
                        project_uuid,
                        properties.ENTITY_NODE,
                        node.uuid,
                        _safe_label(node.bone_name, "缺失骨骼"),
                        bone_name=node.bone_name,
                        display_type="SPHERE",
                        display_size=preview.node_display_radius(node.radius),
                    )
                node_controller.show_name = bool(
                    project.preview_show_node_names
                )
                created.append(node_controller)
                node_controllers.append(node_controller)
            preview.stage_chain_previews(
                chain_collection,
                chain_controller,
                node_controllers,
                owner,
                project,
                chain,
                preview_stage,
            )
        for collider in project.colliders:
            collider_controller = _new_controller(
                    colliders_root,
                    owner,
                    project_uuid,
                    properties.ENTITY_COLLIDER,
                    collider.uuid,
                    preview.collider_display_label(collider, ""),
                    # 双端胶囊的 Curve 点在 armature-object/rest 空间，并由
                    # 两个 Hook 分别跟随端点；controller 不能再叠加首骨 Bone
                    # Parent，否则会把首骨变换重复应用到整条曲线。
                    bone_name=(
                        ""
                        if collider.kind == "CAPSULE" and collider.bone_name_b
                        else collider.bone_name
                    ),
                    display_type="PLAIN_AXES",
                    display_size=0.001,
                )
            collider_controller.hide_select = True
            created.append(collider_controller)
            preview.stage_collider_preview(
                colliders_root,
                collider_controller,
                owner,
                project,
                collider,
                preview_stage,
            )
        for link in project.chain_links:
            link_label = preview.chain_link_label(project, link)
            link_collection = _new_collection(
                preview.chain_link_collection_label(project, link),
                chain_links_root,
                owner,
                project_uuid,
                "CHAIN_LINK",
                link.uuid,
            )
            collections.append(link_collection)
            link_controller = _new_controller(
                link_collection,
                owner,
                project_uuid,
                properties.ENTITY_CHAIN_LINK,
                link.uuid,
                link_label,
                display_type="PLAIN_AXES",
                display_size=0.001,
            )
            link_controller.hide_select = True
            created.append(link_controller)
            preview.stage_chain_link_preview(
                link_collection,
                link_controller,
                owner,
                project,
                link,
                preview_stage,
            )
            for collision in link.collisions:
                collision_label = preview.link_collision_label(project, link, collision)
                collision_controller = _new_controller(
                    link_collection,
                    owner,
                    project_uuid,
                    properties.ENTITY_LINK_COLLISION,
                    collision.uuid,
                    collision_label,
                    display_type="PLAIN_AXES",
                    display_size=0.001,
                )
                collision_controller.hide_select = True
                created.append(collision_controller)
                preview.stage_link_collision_preview(
                    link_collection,
                    collision_controller,
                    owner,
                    project,
                    link,
                    collision,
                    preview_stage,
                )
    except Exception:
        _discard_staged_projection(created, collections, preview_stage)
        raise
    return root, created, collections, preview_stage


def rebuild_project_controllers(owner, *, context_scene=None, initially_hidden=None):
    """离线构造完整新投影；提交新根后再尽力回收旧生成物。"""

    project = ownership.ensure_authoring_owner(owner)
    cleanup_plan = _projection_cleanup_plan(
        owner,
        project.project_uuid,
        context_scene=context_scene,
    )
    scene = cleanup_plan["scene"]
    # Keep the user's viewport choice across an atomic projection replacement.
    # Conversion explicitly starts disabled: hide before scene linking so the
    # newly generated Geometry Nodes do not first evaluate in the viewport.
    old_root = project.controller_collection
    hide_viewport = (
        bool(old_root.hide_viewport) if old_root is not None else False
    ) if initially_hidden is None else bool(initially_hidden)
    root = None
    created = []
    collections = []
    preview_stage = None
    try:
        root, created, collections, preview_stage = _build_staged_projection(owner, project)
        root.hide_viewport = hide_viewport
        scene.collection.children.link(root)
    except Exception:
        if root is not None or created or collections:
            _discard_staged_projection(created, collections, preview_stage)
        raise

    try:
        project.controller_collection = root
    except Exception:
        if root is not None and root.name in scene.collection.children:
            scene.collection.children.unlink(root)
        _discard_staged_projection(created, collections, preview_stage)
        raise

    preview.register_owner(owner)
    try:
        cleanup_errors = _execute_cleanup(cleanup_plan)
    except Exception as exc:
        cleanup_errors = [f"旧生成物清理出现未预期异常：{exc}"]
    # Blender does not recycle names after deleting the old projection.  Restore
    # the requested labels for the freshly built project after cleanup; collisions
    # with another generated project receive a readable qualified fallback, while
    # arbitrary user objects are never renamed.
    normalize_collection_names(collections)
    preview.normalize_display_names(
        tuple(created) + tuple(preview_stage.get("objects", ()) if preview_stage else ()),
    )
    # 结构操作在当前主线程中已经从最新 canonical 完整重建了投影；
    # Property update 回调在构造 canonical 时登记的旧请求若继续保留，会让下一次
    # 纯样式刷新重复交换几何 datablock，并产生不必要的视口抖动。
    preview.discard_owner_updates(owner, project.project_uuid)
    if cleanup_errors:
        project.preview_error_message = (
            "新投影已启用，但旧生成资源未完全清理；请再次点击“修复实体预览”。详情："
            + "；".join(cleanup_errors[:3])
        )
        print("HD2 物理骨旧投影清理警告：" + "；".join(cleanup_errors))
    else:
        project.preview_error_message = ""
    return {
        "controllers": len(created),
        "previews": len(preview_stage["objects"] if preview_stage else ()),
        "profiles": len(project.profiles),
        "chains": len(project.chains),
        "nodes": sum(len(chain.nodes) for chain in project.chains),
        "colliders": len(project.colliders),
        "chain_links": len(project.chain_links),
        "link_collisions": sum(len(link.collisions) for link in project.chain_links),
        "cleanup_errors": tuple(cleanup_errors),
    }


def sync_collider_controller(owner, collider, bone_name):
    """把唯一 Collider controller 原子重绑到指定骨或 Unit 根。"""

    project = ownership.ensure_authoring_owner(owner)
    matches = ownership.controllers_for_entity(
        owner,
        project.project_uuid,
        properties.ENTITY_COLLIDER,
        collider.uuid,
    )
    if len(matches) != 1:
        raise ownership.HD2PBError(
            f"该碰撞体有 {len(matches)} 个编辑对象；请先执行“重建编辑对象”再修改吸附骨骼"
        )
    controller = matches[0]
    from . import binding
    old_bone = controller.parent_bone if controller.parent == owner else getattr(controller.constraints.get(binding.NAME), "subtarget", "")
    if not binding.matches(controller, owner, old_bone):
        raise ownership.HD2PBError("碰撞体跟随绑定损坏；请先重建编辑对象")
    state = {
        "follow": (controller.constraints.get("HD2PB · 骨架跟随").subtarget
                   if controller.constraints.get("HD2PB · 骨架跟随") else None),
        "parent": controller.parent,
        "parent_type": controller.parent_type,
        "parent_bone": controller.parent_bone,
        "matrix_parent_inverse": controller.matrix_parent_inverse.copy(),
        "matrix_basis": controller.matrix_basis.copy(),
        "rotation_mode": controller.rotation_mode,
        "locks": (
            tuple(controller.lock_location),
            tuple(controller.lock_rotation),
            tuple(controller.lock_scale),
        ),
    }
    try:
        _attach_to_owner(
            controller,
            owner,
            ""
            if collider.kind == "CAPSULE" and getattr(collider, "bone_name_b", "")
            else bone_name,
        )
        preview.refresh_entity(
            owner,
            properties.ENTITY_COLLIDER,
            collider.uuid,
            aspects=(preview.ASPECT_GEOMETRY,),
        )
    except Exception:
        from . import binding
        if state["follow"] is not None:
            binding.attach(controller, owner, state["follow"])
        else:
            follow = controller.constraints.get(binding.NAME)
            if follow is not None:
                controller.constraints.remove(follow)
        controller.parent = state["parent"]
        controller.parent_type = state["parent_type"]
        controller.parent_bone = state["parent_bone"]
        controller.rotation_mode = state["rotation_mode"]
        controller.matrix_parent_inverse = state["matrix_parent_inverse"]
        controller.matrix_basis = state["matrix_basis"]
        controller.lock_location, controller.lock_rotation, controller.lock_scale = state["locks"]
        raise
    return controller


def _visibility_pointer(value):
    """返回一个不会触发 RNA 比较的运行时键。"""

    try:
        return value.as_pointer()
    except (AttributeError, ReferenceError, RuntimeError):
        return id(value)


def _visibility_id(value):
    # Session UID survives renames where Blender exposes it; older versions
    # conservatively skip renamed IDs instead of touching a reused pointer.
    return (_visibility_pointer(value), getattr(value, "session_uid", value.name_full))


def _visibility_view_layer_key(view_layer):
    for scene in bpy.data.scenes:
        for candidate in scene.view_layers:
            if candidate == view_layer:
                return (_visibility_id(scene), _visibility_pointer(candidate), candidate.name)
    return None


def _resolve_visibility_view_layer(key):
    if key is not None:
        for scene in bpy.data.scenes:
            if _visibility_id(scene) == key[0]:
                return next((layer for layer in scene.view_layers
                             if _visibility_pointer(layer) == key[1] and layer.name == key[2]), None)
    return None


def _visibility_reference_key(reference):
    """缓存纯值身份，不跨操作持有 LayerCollection 等嵌套 RNA。"""
    if isinstance(reference, bpy.types.LayerCollection):
        return ("LAYER", _visibility_pointer(reference), _visibility_id(reference.collection))
    if isinstance(reference, bpy.types.Collection):
        return ("COLLECTION", _visibility_id(reference))
    if isinstance(reference, bpy.types.Object):
        return ("OBJECT", _visibility_id(reference))
    raise ownership.HD2PBError("不支持的导航可见性对象类型")


def _resolve_visibility_reference(key, view_layer):
    # Resolve each write afresh: collection rebuilds/deletions can invalidate
    # LayerCollection even without an Undo. Never dereference a cached RNA.
    if key[0] == "LAYER":
        if view_layer is None:
            return None
        pending = [view_layer.layer_collection]
        while pending:
            layer = pending.pop()
            if (_visibility_pointer(layer) == key[1]
                    and _visibility_id(layer.collection) == key[2]):
                return layer
            pending.extend(layer.children)
        return None
    ids = bpy.data.objects if key[0] == "OBJECT" else bpy.data.collections
    return next((value for value in ids if _visibility_id(value) == key[1]), None)


def _remember_visibility_change(state, reference, attribute, forced):
    """记录并写入一个普通 RNA 布尔属性；同一属性只保留最初原值。"""

    try:
        current = bool(getattr(reference, attribute))
    except (AttributeError, ReferenceError, RuntimeError) as exc:
        raise ownership.HD2PBError(
            f"无法读取目标可见性属性“{attribute}”；请检查 Blender 视图层"
        ) from exc
    forced = bool(forced)
    if current == forced:
        return
    reference_pointer = _visibility_pointer(reference)
    reference_key = _visibility_reference_key(reference)
    for change in state["changes"]:
        if (
            change["kind"] == "property"
            and change.get("reference_pointer") == reference_pointer
            and change["attribute"] == attribute
        ):
            change["forced"] = forced
            try:
                setattr(reference, attribute, forced)
            except (AttributeError, ReferenceError, RuntimeError) as exc:
                raise ownership.HD2PBError(
                    f"无法调整目标可见性属性“{attribute}”；请检查集合是否来自外部库"
                ) from exc
            return
    try:
        setattr(reference, attribute, forced)
    except (AttributeError, ReferenceError, RuntimeError) as exc:
        raise ownership.HD2PBError(
            f"无法调整目标可见性属性“{attribute}”；请检查集合是否来自外部库"
        ) from exc
    state["changes"].append(
        {
            "kind": "property",
            "reference_key": reference_key,
            "reference_pointer": reference_pointer,
            "attribute": attribute,
            "original": current,
            "forced": forced,
        }
    )


def _remember_hide_set(state, target, view_layer):
    """记录并解除当前 View Layer 的对象本地隐藏（眼睛图标）。"""

    try:
        current = bool(target.hide_get(view_layer=view_layer))
    except (AttributeError, ReferenceError, RuntimeError) as exc:
        raise ownership.HD2PBError(
            f"无法读取对象“{target.name}”在当前视图层的隐藏状态"
        ) from exc
    if not current:
        return
    target_pointer = _visibility_pointer(target)
    for change in state["changes"]:
        if (
            change["kind"] == "hide_set"
            and change.get("reference_pointer") == target_pointer
        ):
            change["forced"] = False
            try:
                target.hide_set(False, view_layer=view_layer)
            except (AttributeError, ReferenceError, RuntimeError) as exc:
                raise ownership.HD2PBError(
                    f"无法解除对象“{target.name}”在当前视图层的隐藏状态"
                ) from exc
            return
    try:
        target.hide_set(False, view_layer=view_layer)
    except (AttributeError, ReferenceError, RuntimeError) as exc:
        raise ownership.HD2PBError(
            f"无法解除对象“{target.name}”在当前视图层的隐藏状态"
        ) from exc
    state["changes"].append(
        {
            "kind": "hide_set",
            "reference_key": _visibility_reference_key(target),
            "reference_pointer": target_pointer,
            "original": True,
            "forced": False,
        }
    )


def _restore_selection_visibility(*, target=None, view_layer=None):
    """恢复导航暂时改动过的可见性；用户后来改过的值不会被覆盖。"""

    for key, state in list(_SELECTION_VISIBILITY.items()):
        if target is not None and state.get("target_pointer") == _visibility_pointer(target):
            continue
        if view_layer is not None and state.get("view_layer_key") != _visibility_view_layer_key(view_layer):
            continue
        for change in reversed(state.get("changes", ())):
            current_layer = _resolve_visibility_view_layer(state.get("view_layer_key"))
            if current_layer is None:
                continue
            reference = _resolve_visibility_reference(change["reference_key"], current_layer)
            if reference is None:
                continue
            try:
                if change["kind"] == "hide_set":
                    current = bool(reference.hide_get(view_layer=current_layer))
                    if current == bool(change["forced"]):
                        reference.hide_set(
                            bool(change["original"]),
                            view_layer=current_layer,
                        )
                else:
                    current = bool(getattr(reference, change["attribute"]))
                    if current == bool(change["forced"]):
                        setattr(reference, change["attribute"], bool(change["original"]))
            except (AttributeError, ReferenceError, RuntimeError):
                # 对象可能已被删除或视图层已关闭；不把失效的 RNA 引用留在缓存中。
                continue
        # 如果用户在导航后又主动修改了某个状态，上面不会覆盖该值，同时也必须
        # 忘记旧记录；否则以后碰巧回到 forced 值时会错误回放过期原值。
        _SELECTION_VISIBILITY.pop(key, None)


def restore_selection_visibility():
    """恢复所有由选择导航暂时解除的项目可见性。"""

    _restore_selection_visibility()


def discard_selection_visibility():
    """Undo/Redo 由 Blender 快照恢复可见性；不得回放上一数据世代的导航记录。"""
    _SELECTION_VISIBILITY.clear()


def owned_state_present():
    """选择导航是否仍有待恢复的可见性记录。"""

    return bool(_SELECTION_VISIBILITY)


def _layer_collection_paths(layer_collection, target_collection, ancestors=()):
    """返回当前 View Layer 中指向目标 Collection 的所有祖先路径。"""

    path = (*ancestors, layer_collection)
    try:
        collection = layer_collection.collection
    except (AttributeError, ReferenceError, RuntimeError):
        return
    if collection == target_collection:
        yield path
    for child in tuple(getattr(layer_collection, "children", ())):
        yield from _layer_collection_paths(child, target_collection, path)


def _owned_collection_set(root, owner, project_uuid):
    """收集本项目根下精确标记的 Collection。"""

    result = set()
    try:
        iterator = preview._iter_collection_tree(root)
        for collection in iterator:
            if _is_owned_collection(collection, owner, project_uuid):
                result.add(collection)
    except (AttributeError, ReferenceError, RuntimeError):
        return set()
    return result


def _ensure_target_selectable(context, owner, project, target):
    """只解除本项目目标的隐藏，并确保其在当前视图层可见可选。"""

    view_layer = getattr(context, "view_layer", None)
    if view_layer is None:
        raise ownership.HD2PBError("当前没有可用的 Blender 视图层，无法选择物理骨编辑对象")
    if (
        getattr(target, "hd2pb_owner_object", None) != owner
        or getattr(target, "hd2pb_project_uuid", "") != project.project_uuid
    ):
        raise ownership.HD2PBError("目标编辑对象不属于当前物理骨项目，已拒绝解除其隐藏状态")

    # 先恢复上一次导航临时改动过的其他对象；当前目标的状态保留到本次检查结束。
    _restore_selection_visibility(target=target, view_layer=view_layer)
    key = (_visibility_pointer(target), _visibility_pointer(view_layer))
    state = _SELECTION_VISIBILITY.setdefault(
        key,
        {
            "target_pointer": _visibility_pointer(target),
            "view_layer_key": _visibility_view_layer_key(view_layer),
            "changes": [],
        },
    )

    root = project.controller_collection
    owned_collections = _owned_collection_set(root, owner, project.project_uuid)
    paths = []
    for collection in tuple(getattr(target, "users_collection", ())):
        paths.extend(
            _layer_collection_paths(view_layer.layer_collection, collection)
        )

    blocked_reasons = []
    selected_path = None
    for path in paths:
        blocked = []
        for layer_collection in path:
            try:
                collection = layer_collection.collection
                owned = collection in owned_collections
                if layer_collection.exclude:
                    if owned:
                        continue
                    blocked.append(f"父集合“{collection.name}”已从当前视图层排除")
                if layer_collection.hide_viewport:
                    if owned:
                        continue
                    blocked.append(f"父集合“{collection.name}”在当前视图层中隐藏")
                if collection.hide_viewport:
                    if owned:
                        continue
                    blocked.append(f"父集合“{collection.name}”的全局视图隐藏已开启")
            except (AttributeError, ReferenceError, RuntimeError):
                blocked.append("父集合的可见性状态无法读取")
        if not blocked:
            selected_path = path
            break
        blocked_reasons.extend(blocked)

    if paths and selected_path is None:
        reason = blocked_reasons[0] if blocked_reasons else "父集合不可见"
        raise ownership.HD2PBError(
            f"编辑对象“{target.name}”无法在当前视图层显示：{reason}；"
            "请在大纲视图展开该项目集合后重试"
        )
    if not paths and getattr(view_layer.objects, "get", lambda _name: None)(target.name) is None:
        raise ownership.HD2PBError(
            f"编辑对象“{target.name}”不在当前 View Layer；请切换场景或在大纲视图启用项目集合"
        )

    if selected_path is not None:
        # Changing exclude may resync the layer tree even within this call.
        # Do not continue walking wrappers obtained before the write.
        path_keys = tuple(_visibility_reference_key(layer) for layer in selected_path)
        for layer_key in path_keys:
            layer_collection = _resolve_visibility_reference(layer_key, view_layer)
            if layer_collection is None:
                continue
            collection = layer_collection.collection
            if collection in owned_collections:
                if bool(layer_collection.exclude):
                    _remember_visibility_change(state, layer_collection, "exclude", False)
                layer_collection = _resolve_visibility_reference(layer_key, view_layer)
                if layer_collection is not None and bool(layer_collection.hide_viewport):
                    _remember_visibility_change(state, layer_collection, "hide_viewport", False)
                if bool(collection.hide_viewport):
                    _remember_visibility_change(state, collection, "hide_viewport", False)

    if bool(getattr(target, "hide_viewport", False)):
        _remember_visibility_change(state, target, "hide_viewport", False)
    if bool(getattr(target, "hide_select", False)):
        _remember_visibility_change(state, target, "hide_select", False)
    _remember_hide_set(state, target, view_layer)

    try:
        visible = bool(target.visible_get(view_layer=view_layer))
    except (AttributeError, ReferenceError, RuntimeError):
        try:
            visible = bool(target.visible_get())
        except (AttributeError, ReferenceError, RuntimeError) as exc:
            raise ownership.HD2PBError(
                f"无法确认编辑对象“{target.name}”是否可见；请检查当前视图层"
            ) from exc
    if not visible:
        _restore_selection_visibility(view_layer=view_layer)
        raise ownership.HD2PBError(
            f"编辑对象“{target.name}”仍不可见；请在大纲视图检查父集合的排除、视图隐藏和本地隐藏状态"
        )
    if not state["changes"]:
        _SELECTION_VISIBILITY.pop(key, None)


def select_entity_controller(context, owner, entity_type, entity_uuid):
    """选择实体入口：Chain/Collider 优先选择可见 Curve，其他选择 Empty。"""

    project = ownership.ensure_authoring_owner(owner)
    matches = ownership.controllers_for_entity(
        owner,
        project.project_uuid,
        entity_type,
        entity_uuid,
    )
    if len(matches) != 1:
        raise ownership.HD2PBError(
            f"该实体有 {len(matches)} 个编辑对象；请先执行“重建编辑对象”"
        )
    controller = matches[0]
    target = controller
    preferred_kind = {
        properties.ENTITY_CHAIN: properties.PREVIEW_CHAIN_CURVE,
        properties.ENTITY_COLLIDER: properties.PREVIEW_COLLIDER_SHAPE,
        properties.ENTITY_CHAIN_LINK: properties.PREVIEW_CHAIN_LINK,
        properties.ENTITY_LINK_COLLISION: properties.PREVIEW_LINK_COLLISION,
    }.get(entity_type)
    if preferred_kind is not None:
        previews = [
            obj
            for obj in preview.project_preview_objects(owner)
            if obj.hd2pb_entity_type == entity_type
            and obj.hd2pb_entity_uuid == entity_uuid
            and obj.hd2pb_preview_kind == preferred_kind
        ]
        if len(previews) != 1:
            raise ownership.HD2PBError(
                f"该实体有 {len(previews)} 个可选择 Curve；请先执行“修复实体预览”"
            )
        target = previews[0]
    if target.name not in context.view_layer.objects:
        raise ownership.HD2PBError(
            f"编辑对象“{target.name}”不在当前 View Layer；请切换场景或重建编辑对象"
        )
    _ensure_target_selectable(context, owner, project, target)
    if context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for selected in list(context.selected_objects):
        selected.select_set(False)
    target.hide_set(False)
    target.select_set(True)
    context.view_layer.objects.active = target
    ownership.remember_project_owner(context, owner)
    return target


__all__ = (
    "COLLECTION_SCHEMA_KEY",
    "COLLECTION_PROJECT_KEY",
    "COLLECTION_OWNER_KEY",
    "COLLECTION_ROLE_KEY",
    "COLLECTION_ENTITY_KEY",
    "COLLECTION_DISPLAY_NAME_KEY",
    "preflight_project_rebuild",
    "remove_project_projection",
    "rebuild_project_controllers",
    "sync_collider_controller",
    "select_entity_controller",
    "restore_selection_visibility",
    "owned_state_present",
)
