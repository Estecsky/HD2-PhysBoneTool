"""canonical 身份、所有权解析和中文完整性检查。"""

from __future__ import annotations

import math
import re
import uuid

import bpy

from . import properties


UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class HD2PBError(RuntimeError):
    """可以直接向 Blender 用户显示的中文错误。"""


_ENTITY_LABELS = {
    properties.ENTITY_PROJECT: "物理骨项目",
    properties.ENTITY_PROFILE: "弹簧参数配置",
    properties.ENTITY_CHAIN: "物理骨链",
    properties.ENTITY_NODE: "链节点",
    properties.ENTITY_COLLIDER: "碰撞体",
    properties.ENTITY_CHAIN_LINK: "链组连接",
    properties.ENTITY_LINK_COLLISION: "链间碰撞体",
}

_COLLECTION_ROLE_LABELS = {
    "ROOT": "物理骨根集合",
    "CONFIG": "配置集合",
    "CHAINS": "链组集合",
    "COLLIDERS": "碰撞体集合",
    "CHAIN_LINKS": "链间连接根集合",
    "CHAIN": "单条链集合",
    "CHAIN_LINK": "单个链组连接集合",
}


def entity_label(entity_type):
    """把内部实体枚举转换为可直接显示给用户的中文名称。"""

    return _ENTITY_LABELS.get(entity_type, "物理骨实体")


def collection_role_label(role):
    """把内部集合角色枚举转换为可直接显示给用户的中文名称。"""

    return _COLLECTION_ROLE_LABELS.get(role, "物理骨管理集合")


def new_uuid():
    """生成小写 RFC 4122 UUID 文本。"""

    return str(uuid.uuid4())


def is_valid_uuid(value):
    return isinstance(value, str) and UUID_PATTERN.fullmatch(value) is not None


def armature_objects(armature):
    """返回引用同一 Armature datablock 的全部 Object。"""

    # The normal single-user project already carries its exact owner pointer.
    # Blender's live ID user count proves there cannot be another Object here;
    # shared data, fake users and broken ownership still take the full scan.
    project = getattr(armature, 'hd2pb_project', None)
    owner = project.authoring_object if project is not None else None
    if (armature.users == 1 and owner is not None and owner.type == 'ARMATURE'
            and owner.data == armature and bpy.data.objects.get(owner.name) == owner):
        return [owner]
    return [
        obj
        for obj in bpy.data.objects
        if obj.type == "ARMATURE" and obj.data == armature
    ]


def project_armatures(project_uuid, *, exclude=None):
    """返回使用同一已初始化 project UUID 的 Armature datablock。"""

    if not project_uuid:
        return []
    return [
        armature
        for armature in bpy.data.armatures
        if armature != exclude
        and hasattr(armature, "hd2pb_project")
        and project_initialized(armature.hd2pb_project)
        and armature.hd2pb_project.project_uuid == project_uuid
    ]


def project_uuid_conflicts(owner):
    """返回与当前 owner 使用相同 UUID 的其他 Armature 数据。"""

    if owner is None or owner.type != "ARMATURE":
        return []
    project = owner.data.hd2pb_project
    if not project_initialized(project):
        return []
    return project_armatures(project.project_uuid, exclude=owner.data)


def active_owner(context):
    """从活动 Armature 或 controller 找到 authoring Object。"""

    try:
        obj = getattr(context, "object", None)
        if obj is not None and obj.type == "ARMATURE":
            return obj
        if obj is not None and (
            getattr(obj, "hd2pb_controller_schema", 0)
            or getattr(obj, "hd2pb_preview_kind", properties.PREVIEW_NONE)
            in {
                properties.PREVIEW_CHAIN_CURVE,
                properties.PREVIEW_COLLIDER_SHAPE,
                properties.PREVIEW_NODE_LIMIT_AXIS,
                properties.PREVIEW_CHAIN_LINK,
                properties.PREVIEW_LINK_COLLISION,
            }
        ):
            owner = getattr(obj, "hd2pb_owner_object", None)
            if owner is not None and owner.type == "ARMATURE":
                return owner
        pose_object = getattr(context, "pose_object", None)
        if pose_object is not None and pose_object.type == "ARMATURE":
            return pose_object
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        # 投影重建可能在一次 UI 刷新中删除活动对象；解析边界必须失败关闭。
        return None
    return None


def project_initialized(project):
    return bool(
        project is not None
        and project.schema_version > 0
        and project.project_uuid
    )


def _owner_in_context(context, owner, *, require_visible):
    """验证 owner 属于当前 Scene/View Layer，且按需可见。"""

    try:
        scene = getattr(context, "scene", None)
        view_layer = getattr(context, "view_layer", None)
        if (
            scene is None
            or view_layer is None
            or owner is None
            or owner.type != "ARMATURE"
            or scene.objects.get(owner.name) != owner
            or view_layer.objects.get(owner.name) != owner
        ):
            return False
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False
    if not require_visible:
        return True
    try:
        return bool(owner.visible_get(view_layer=view_layer))
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def forget_project_owner(context):
    """清除当前 Scene 的项目导航指针与配对 UUID，不修改 canonical 数据。"""

    scene = getattr(context, "scene", None)
    if scene is None:
        return
    if hasattr(scene, "hd2pb_active_project_owner"):
        try:
            scene.hd2pb_active_project_owner = None
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
    if hasattr(scene, "hd2pb_active_project_uuid"):
        try:
            scene.hd2pb_active_project_uuid = ""
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass


def _navigation_project(context, owner):
    """验证 Scene 项目导航目标；旧 schema 和重复 UUID 仍可进入诊断界面。"""

    if not _owner_in_context(context, owner, require_visible=True):
        raise HD2PBError("物理骨项目所属骨架不在当前可见 Scene/View Layer")
    project = getattr(owner.data, "hd2pb_project", None)
    if not project_initialized(project):
        raise HD2PBError("当前骨架尚未初始化 HD2 物理骨项目")
    if not is_valid_uuid(project.project_uuid):
        raise HD2PBError("项目 UUID 缺失或格式损坏")
    return project


def _read_remembered_project_owner(context):
    """只读校验 Scene 的项目记录，并返回 ``(owner, stale)``。"""

    scene = getattr(context, "scene", None)
    if (
        scene is None
        or not hasattr(scene, "hd2pb_active_project_owner")
        or not hasattr(scene, "hd2pb_active_project_uuid")
    ):
        return None, False
    try:
        owner = scene.hd2pb_active_project_owner
        project_uuid = scene.hd2pb_active_project_uuid
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None, True
    if owner is None:
        return None, bool(project_uuid)
    try:
        project = _navigation_project(context, owner)
    except (AttributeError, ReferenceError, RuntimeError, HD2PBError):
        return None, True
    if project.project_uuid != project_uuid:
        return None, True
    return owner, False


def peek_remembered_project_owner(context):
    """只读返回有效 Scene 项目；Panel ``draw/poll`` 必须使用这条路径。"""

    owner, _stale = _read_remembered_project_owner(context)
    return owner


def remembered_project_owner(context):
    """返回有效 Scene 项目，并在显式操作期间清除失效记录。"""

    owner, stale = _read_remembered_project_owner(context)
    if stale:
        forget_project_owner(context)
    return owner


def remember_project_owner(context, owner):
    """记录当前 Scene/View Layer 的项目 Object，并以稳定 UUID 防止陈旧引用。"""

    project = _navigation_project(context, owner)
    scene = context.scene
    if not (
        hasattr(scene, "hd2pb_active_project_owner")
        and hasattr(scene, "hd2pb_active_project_uuid")
    ):
        raise HD2PBError("物理骨项目导航属性尚未注册")
    scene.hd2pb_active_project_owner = owner
    scene.hd2pb_active_project_uuid = project.project_uuid
    return owner


def _explicit_context_owner(context):
    """解析明确活动对象；损坏的物理骨投影不得回退到旧项目。"""

    try:
        obj = getattr(context, "object", None)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None, "INVALID"
    if obj is not None:
        try:
            if obj.type == "ARMATURE":
                return obj, "ARMATURE"
            tagged = bool(
                getattr(obj, "hd2pb_controller_schema", 0)
                or getattr(obj, "hd2pb_preview_schema", 0)
                or getattr(obj, "hd2pb_owner_object", None) is not None
                or getattr(obj, "hd2pb_project_uuid", "")
                or getattr(obj, "hd2pb_entity_uuid", "")
                or getattr(obj, "hd2pb_entity_type", properties.ENTITY_NONE)
                != properties.ENTITY_NONE
                or getattr(obj, "hd2pb_preview_kind", properties.PREVIEW_NONE)
                != properties.PREVIEW_NONE
            )
        except (AttributeError, ReferenceError, RuntimeError):
            return None, "INVALID"
        if tagged:
            try:
                owner, _project, _entity, _parent = resolve_controller(
                    obj,
                    require_unique=True,
                )
            except (HD2PBError, AttributeError, ReferenceError, RuntimeError, TypeError):
                # 旧版/未来版 canonical 会使严格 controller 解析失败，但只要对象仍以
                # 精确 owner 指针和 project UUID 指回同一项目，就允许主面板展示版本
                # 诊断与迁移入口。当前 schema 下的损坏投影仍然 fail closed。
                try:
                    declared_owner = getattr(obj, "hd2pb_owner_object", None)
                    declared_project = getattr(
                        getattr(declared_owner, "data", None),
                        "hd2pb_project",
                        None,
                    )
                    if (
                        declared_owner is not None
                        and _owner_in_context(
                            context,
                            declared_owner,
                            require_visible=True,
                        )
                        and project_initialized(declared_project)
                        and declared_project.schema_version
                        != properties.PROJECT_SCHEMA_VERSION
                        and is_valid_uuid(declared_project.project_uuid)
                        and getattr(obj, "hd2pb_project_uuid", "")
                        == declared_project.project_uuid
                    ):
                        return declared_owner, "VERSIONED_PROJECT"
                except (AttributeError, ReferenceError, RuntimeError, TypeError):
                    pass
                return None, "INVALID"
            return owner, "PROJECT"
        return None, "ORDINARY"
    try:
        pose_object = getattr(context, "pose_object", None)
        if pose_object is not None and pose_object.type == "ARMATURE":
            return pose_object, "ARMATURE"
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None, "INVALID"
    return None, "MISSING"


def project_owner(context, *, remember=False):
    """项目面板解析器：明确项目优先，普通/空选择回退 Scene 项目记录。"""

    owner, source = _explicit_context_owner(context)
    if source == "INVALID":
        # 损坏的已标记投影必须 fail closed；保留 Scene 记录，用户改选普通对象后
        # 仍可返回原项目执行检查或修复。
        return None
    if owner is not None:
        if remember:
            try:
                remember_project_owner(context, owner)
            except (HD2PBError, AttributeError, ReferenceError, RuntimeError, TypeError):
                # 未初始化或身份损坏的显式 Armature 仍交给面板显示初始化/诊断；
                # 它不能覆盖已有的有效项目记录。
                pass
        return owner
    if remember:
        return remembered_project_owner(context)
    return peek_remembered_project_owner(context)


def explicit_export_owner(context):
    """严格解析导出所属项目；普通/空选择可只读回退 Scene 项目记录。"""

    owner, source = _explicit_context_owner(context)
    if source == "INVALID":
        raise HD2PBError("当前活动物理骨编辑对象的身份已损坏；请先修复实体预览")
    if source == "VERSIONED_PROJECT":
        raise HD2PBError("当前物理骨项目版本过旧；请先迁移项目数据")
    if owner is None:
        if source not in {"ORDINARY", "MISSING"}:
            return None
        # 导出菜单和物体模式面板允许用户先选普通对象再继续编辑上一次项目。
        # 这里只读读取 Scene 记录；失效清理仍由显式操作的 remembered_project_owner 完成。
        owner = peek_remembered_project_owner(context)
        if owner is None:
            return None
    if not _owner_in_context(context, owner, require_visible=True):
        raise HD2PBError("导出所属骨架不在当前可见 Scene/View Layer")
    return owner


def ensure_authoring_owner(
    owner,
    *,
    require_initialized=True,
    require_unique_project_uuid=True,
):
    """验证当前 Object 是唯一 authoring owner，并返回 Project。"""

    if owner is None or owner.type != "ARMATURE":
        raise HD2PBError("请先选择用于制作物理骨的骨架对象（Armature）")
    project = owner.data.hd2pb_project
    if require_initialized and not project_initialized(project):
        raise HD2PBError("当前骨架尚未初始化 HD2 物理骨项目")
    instances = armature_objects(owner.data)
    if len(instances) != 1:
        raise HD2PBError(
            f"骨架数据（Armature）“{owner.data.name}”正被 {len(instances)} 个对象共享；"
            "请在要编辑的副本上执行“复制为独立物理骨项目”后再继续"
        )
    if project_initialized(project):
        authoring = project.authoring_object
        if authoring is None or authoring.type != "ARMATURE" or authoring.data != owner.data:
            raise HD2PBError(
                "项目记录的编辑所属骨架对象已丢失或指向其他 Armature；"
                "请先检查项目所有权，不要直接修改 UUID"
            )
        if authoring != owner:
            raise HD2PBError(
                f"当前对象不是项目记录的编辑所属对象；应选择“{authoring.name}”"
            )
        if project.schema_version != properties.PROJECT_SCHEMA_VERSION:
            if project.schema_version > properties.PROJECT_SCHEMA_VERSION:
                raise HD2PBError(
                    f"项目数据版本为 {project.schema_version}，高于当前插件支持的 "
                    f"{properties.PROJECT_SCHEMA_VERSION}；请升级插件"
                )
            raise HD2PBError(
                f"项目数据版本为 {project.schema_version}，请先执行“迁移项目数据”"
            )
        if not is_valid_uuid(project.project_uuid):
            raise HD2PBError("项目 UUID 缺失或格式损坏；请不要继续编辑或手工修改内部 UUID")
        if require_unique_project_uuid:
            conflicts = project_uuid_conflicts(owner)
            if conflicts:
                raise HD2PBError(
                    "另一个 Armature 项目使用了相同 project UUID；这通常来自普通 Shift-D "
                    "复制。为避免两个项目互相删除编辑对象，请先对当前副本执行“复制为独立物理骨项目”"
                )
    return project


def entity_entries(project):
    """按 canonical collection 顺序枚举所有可投影实体。"""

    yield properties.ENTITY_PROJECT, project.project_uuid, project, None
    for profile in project.profiles:
        yield properties.ENTITY_PROFILE, profile.uuid, profile, None
    for chain in project.chains:
        yield properties.ENTITY_CHAIN, chain.uuid, chain, None
        for node in chain.nodes:
            yield properties.ENTITY_NODE, node.uuid, node, chain
    for collider in project.colliders:
        yield properties.ENTITY_COLLIDER, collider.uuid, collider, None
    for link in project.chain_links:
        yield properties.ENTITY_CHAIN_LINK, link.uuid, link, None
        for collision in link.collisions:
            yield properties.ENTITY_LINK_COLLISION, collision.uuid, collision, link


def _controller_entries(project):
    """枚举真正面向用户的编辑投影；内部固定驱动不生成 controller。"""

    yield properties.ENTITY_PROJECT, project.project_uuid, project, None
    for profile in project.profiles:
        yield properties.ENTITY_PROFILE, profile.uuid, profile, None
    for chain in project.chains:
        yield properties.ENTITY_CHAIN, chain.uuid, chain, None
        for node in list(chain.nodes)[1:]:
            yield properties.ENTITY_NODE, node.uuid, node, chain
    for collider in project.colliders:
        yield properties.ENTITY_COLLIDER, collider.uuid, collider, None
    for link in project.chain_links:
        yield properties.ENTITY_CHAIN_LINK, link.uuid, link, None
        for collision in link.collisions:
            yield properties.ENTITY_LINK_COLLISION, collision.uuid, collision, link


def find_entity(project, entity_type, entity_uuid):
    if entity_type == properties.ENTITY_PROJECT:
        return project if entity_uuid == project.project_uuid else None
    for current_type, current_uuid, entity, parent in entity_entries(project):
        if current_type == entity_type and current_uuid == entity_uuid:
            return entity, parent
    return None


def project_controllers(owner, project_uuid=None):
    """找出声明属于此 owner 或此 project UUID 的全部 controller。"""

    if owner is None:
        return []
    project_uuid = project_uuid or owner.data.hd2pb_project.project_uuid
    result = []
    for obj in bpy.data.objects:
        if not getattr(obj, "hd2pb_controller_schema", 0):
            continue
        if (
            getattr(obj, "hd2pb_owner_object", None) == owner
            or getattr(obj, "hd2pb_project_uuid", "") == project_uuid
        ):
            result.append(obj)
    return result


def controllers_for_entity(owner, project_uuid, entity_type, entity_uuid):
    return [
        obj
        for obj in project_controllers(owner, project_uuid)
        if getattr(obj, "hd2pb_owner_object", None) == owner
        and getattr(obj, "hd2pb_project_uuid", "") == project_uuid
        and getattr(obj, "hd2pb_entity_type", properties.ENTITY_NONE) == entity_type
        and getattr(obj, "hd2pb_entity_uuid", "") == entity_uuid
    ]


def _local_controllers_for_entity(owner, project, entity_type, entity_uuid):
    """热路径只遍历当前项目 Collection 子树，不扫描 ``bpy.data.objects``。"""

    root = project.controller_collection
    if root is None or bpy.data.collections.get(root.name) != root:
        return []
    pending = [root]
    seen = set()
    matches = []
    while pending:
        collection = pending.pop()
        pointer = collection.as_pointer()
        if pointer in seen:
            continue
        seen.add(pointer)
        pending.extend(collection.children)
        for obj in collection.objects:
            if (
                getattr(obj, "hd2pb_controller_schema", 0)
                and obj.hd2pb_owner_object == owner
                and obj.hd2pb_project_uuid == project.project_uuid
                and obj.hd2pb_entity_type == entity_type
                and obj.hd2pb_entity_uuid == entity_uuid
            ):
                matches.append(obj)
    return matches


def resolve_controller(controller, expected_type=None, *, require_unique=True):
    """解析 Empty controller，或可选择的 Chain/Collider/Link Curve 代理。"""

    if controller is None:
        raise HD2PBError("当前对象不是 HD2 物理骨编辑对象")
    proxy = None
    if not getattr(controller, "hd2pb_controller_schema", 0):
        if not getattr(controller, "hd2pb_preview_schema", 0):
            raise HD2PBError("当前对象不是 HD2 物理骨编辑对象")
        if getattr(controller, "hd2pb_preview_kind", properties.PREVIEW_NONE) not in {
            properties.PREVIEW_CHAIN_CURVE,
            properties.PREVIEW_COLLIDER_SHAPE,
            properties.PREVIEW_NODE_LIMIT_AXIS,
            properties.PREVIEW_CHAIN_LINK,
            properties.PREVIEW_LINK_COLLISION,
        }:
            raise HD2PBError("当前辅助预览不可作为属性编辑入口")
        proxy = controller
        owner = getattr(proxy, "hd2pb_owner_object", None)
        if owner is None or owner.type != "ARMATURE":
            raise HD2PBError("可选择预览失去了所属 Armature；请修复实体预览")
        project = ensure_authoring_owner(owner)
        if proxy.hd2pb_project_uuid != project.project_uuid:
            raise HD2PBError("可选择预览的项目 UUID 与所属 Armature 不一致")
        matches = _local_controllers_for_entity(
            owner,
            project,
            proxy.hd2pb_entity_type,
            proxy.hd2pb_entity_uuid,
        )
        if len(matches) != 1:
            raise HD2PBError(
                f"实体存在 {len(matches)} 个内部编辑投影；请修复实体预览"
            )
        controller = matches[0]
    if controller.hd2pb_controller_schema != properties.CONTROLLER_SCHEMA_VERSION:
        raise HD2PBError(
            f"编辑对象版本为 {controller.hd2pb_controller_schema}，当前仅支持 "
            f"{properties.CONTROLLER_SCHEMA_VERSION}；请重建编辑对象"
        )
    owner = controller.hd2pb_owner_object
    if owner is None or owner.type != "ARMATURE":
        raise HD2PBError("编辑对象失去了所属 Armature；请从骨架侧边栏重建编辑对象")
    project = ensure_authoring_owner(owner)
    if controller.hd2pb_project_uuid != project.project_uuid:
        raise HD2PBError("编辑对象的项目 UUID 与所属 Armature 不一致；请重建编辑对象")
    entity_type = controller.hd2pb_entity_type
    if proxy is not None and (
        proxy.hd2pb_entity_type != entity_type
        or proxy.hd2pb_entity_uuid != controller.hd2pb_entity_uuid
    ):
        raise HD2PBError("可选择 Curve 与内部 controller 的实体身份不一致")
    if expected_type is not None and entity_type != expected_type:
        raise HD2PBError("当前编辑对象类型与此属性面板不匹配")
    result = find_entity(project, entity_type, controller.hd2pb_entity_uuid)
    if result is None:
        raise HD2PBError("编辑对象引用的 canonical 实体已不存在；请重建编辑对象")
    if entity_type == properties.ENTITY_PROJECT:
        entity, parent = project, None
    else:
        entity, parent = result
    if require_unique:
        matches = _local_controllers_for_entity(
            owner,
            project,
            entity_type,
            controller.hd2pb_entity_uuid,
        )
        if len(matches) != 1:
            raise HD2PBError(
                f"实体存在 {len(matches)} 个编辑投影；请从骨架侧边栏执行“重建编辑对象”"
            )
    return owner, project, entity, parent


def controller_problem(controller):
    try:
        resolve_controller(controller)
    except HD2PBError as exc:
        return str(exc)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return "当前物理骨编辑对象已失效；请重新选择对象或修复实体预览"
    return ""


def _name_issue(kind, name, seen_names):
    if not isinstance(name, str) or not name:
        return f"{kind}名称不能为空"
    if b"\0" in name.encode("utf-8", errors="ignore"):
        return f"{kind}“{name}”名称不能包含 NUL 字符"
    if len(name.encode("utf-8")) > 63:
        return f"{kind}“{name}”的 UTF-8 名称超过 63 字节，请缩短名称"
    if name in seen_names:
        return f"{kind}名称“{name}”重复；HD2PHY1 要求同类名称唯一"
    seen_names.add(name)
    return ""


def pose_driver_target_bones(project, bones):
    """只读衣物目标占用；禁用规则和随 Bone 重命名保留的标记也保留占用。

    此函数只检查角色，不检查输入骨、曲线或中立姿态；完整规则由
    adapter 的衣物作者桥接校验。普通 HD2PHY1 保持自己的物理校验。
    孤立标记仍保守视为占用。
    """
    targets = {
        rule.target_bone for rule in getattr(project, "pose_drivers", ())
        if rule.target_bone
    }
    targets.update(
        bone.name for bone in bones
        if bone.get("HD2PB_PoseDriverMarker", None) is not None
    )
    return targets


def simulated_bone_names(project):
    """真正由求解器写旋转的骨；固定链根是动画锚，不占模拟角色。"""
    return {
        node.bone_name for chain in project.chains
        for index, node in enumerate(chain.nodes)
        if index > 0 or chain.root_swing
    }


def canonical_issues(owner, *, require_chains=True):
    """检查物理制作数据；目标 Unit 与骨骼槽位由 AQSDK 保存后自动解析。"""

    issues = []
    if owner is None or owner.type != "ARMATURE":
        return ["没有可检查的骨架对象（Armature）"]
    project = owner.data.hd2pb_project
    if not project_initialized(project):
        return ["当前骨架尚未初始化 HD2 物理骨项目"]
    if project.schema_version != properties.PROJECT_SCHEMA_VERSION:
        issues.append(
            f"项目数据版本为 {project.schema_version}，当前支持 "
            f"{properties.PROJECT_SCHEMA_VERSION}；请先迁移或升级插件"
        )
    instances = armature_objects(owner.data)
    if len(instances) != 1:
        issues.append(
            f"骨架数据（Armature）“{owner.data.name}”被 {len(instances)} 个对象共享；"
            "必须先复制为独立物理骨项目"
        )
    if project.authoring_object != owner:
        issues.append("项目记录的编辑所属骨架对象与当前对象不一致")
    if not is_valid_uuid(project.project_uuid):
        issues.append("项目 UUID 缺失或格式损坏；请不要手工编辑内部 UUID")
    else:
        duplicate_projects = [
            armature
            for armature in bpy.data.armatures
            if armature != owner.data
            and hasattr(armature, "hd2pb_project")
            and armature.hd2pb_project.schema_version > 0
            and armature.hd2pb_project.project_uuid == project.project_uuid
        ]
        if duplicate_projects:
            issues.append(
                "另一个 Armature 项目使用了相同 project UUID；"
                "请对复制对象执行“复制为独立物理骨项目”"
            )

    all_uuids = set()
    for entity_type, entity_uuid, _entity, _parent in entity_entries(project):
        label = entity_label(entity_type)
        if not is_valid_uuid(entity_uuid):
            issues.append(f"{label}存在缺失或损坏的 UUID")
        elif entity_uuid in all_uuids:
            issues.append(f"{label} UUID {entity_uuid} 与项目内其他实体重复")
        else:
            all_uuids.add(entity_uuid)

    if not project.profiles and (require_chains or project.chains):
        issues.append("项目至少需要一个弹簧参数配置")
    profile_ids = {profile.uuid for profile in project.profiles}
    profile_names = set()
    for profile in project.profiles:
        problem = _name_issue("弹簧参数配置", profile.display_name, profile_names)
        if problem:
            issues.append(problem)

    if require_chains and not project.chains:
        issues.append("项目至少需要一条物理骨链，HD2PHY1 不接受空链列表")
    chain_names = set()
    simulated = set()
    drivers = set()
    pose_targets = pose_driver_target_bones(project, owner.data.bones)
    for chain in project.chains:
        problem = _name_issue("物理骨链", chain.display_name, chain_names)
        if problem:
            issues.append(problem)
        if int(chain.group_mask) <= 0:
            issues.append(f"物理骨链“{chain.display_name}”的链组掩码不能为 0")
        if int(chain.collision_mask) < 0:
            issues.append(f"物理骨链“{chain.display_name}”的碰撞掩码不能为负数")
        if chain.spring_profile_uuid not in profile_ids:
            issues.append(
                f"物理骨链“{chain.display_name}”引用的弹簧参数配置不存在；请重新选择配置"
            )
        if not 2 <= len(chain.nodes) <= 64:
            issues.append(
                f"物理骨链“{chain.display_name}”必须包含 2..64 个节点（第一个为固定驱动节点）"
            )
        previous_bone = None
        for index, node in enumerate(chain.nodes):
            if node.bone_name in pose_targets and (index > 0 or chain.root_swing):
                issues.append(
                    f"物理骨链“{chain.display_name}”的骨骼“{node.bone_name}”已被衣物姿态驱动目标占用；"
                    "姿态目标骨与真正模拟骨互斥（含可摆动链根及禁用的姿态驱动）；固定链根允许作为目标"
                )
            if node.override_collision_filter and index == 0:
                issues.append(f"链“{chain.display_name}”的固定位置根节点不参与接触，不能单独设置碰撞")
            if int(node.group_mask) <= 0 or int(node.collision_mask) < 0:
                issues.append(f"节点“{node.bone_name}”的所属组须大于 0，允许碰撞组不能为负数")
            bone = owner.data.bones.get(node.bone_name)
            if bone is None:
                issues.append(
                    f"物理骨链“{chain.display_name}”的节点 {index + 1} 引用不存在的骨骼"
                    f"“{node.bone_name}”"
                )
                previous_bone = None
                continue
            if previous_bone is not None:
                if bone.parent != previous_bone:
                    issues.append(
                        f"物理骨链“{chain.display_name}”在“{previous_bone.name}”到“{bone.name}”"
                        "之间不是直接父子关系"
                    )
                distance = (bone.matrix_local.translation - previous_bone.matrix_local.translation).length
                if distance < 1.0e-5:
                    issues.append(
                        f"物理骨链“{chain.display_name}”的 Rest 段“{previous_bone.name}”到"
                        f"“{bone.name}”长度接近零，请先修复骨架"
                    )
            if index == 0 and chain.root_swing and bone.get("HD2BT_WorkRigSourceBone", False):
                issues.append(f"根骨摆动不能覆盖游戏原生骨“{bone.name}”；请选择发根/衣摆根等自定义骨")
            if index == 0 and not chain.root_swing:
                drivers.add(bone.name)
            else:
                if bone.name in simulated:
                    issues.append(f"模拟骨骼“{bone.name}”同时属于多条物理骨链")
                simulated.add(bone.name)
            if node.limit_kind in {"HINGE", "SPHERICAL"}:
                axis_length = math.sqrt(sum(value * value for value in node.limit_axis))
                if axis_length < 1.0e-6:
                    issues.append(
                        f"链“{chain.display_name}”节点“{bone.name}”使用方向性角限，但限制轴为零"
                    )
            previous_bone = bone
    overlap = drivers.intersection(simulated)
    if overlap:
        issues.append(f"模拟骨骼“{sorted(overlap)[0]}”不能同时作为另一条 HD2PHY1 链的固定驱动节点")

    collider_names = set()
    for collider in project.colliders:
        problem = _name_issue("碰撞体", collider.display_name, collider_names)
        if problem:
            issues.append(problem)
        if int(collider.group_mask) <= 0 or int(collider.collision_mask) < 0:
            issues.append(f"碰撞体“{collider.display_name}”的所属组须大于 0，允许碰撞组不能为负数")
        dynamic = collider.attachment_mode == "DYNAMIC"
        if dynamic and not collider.bone_name:
            issues.append(f"动态碰撞体“{collider.display_name}”必须吸附一个模拟节点")
        if dynamic and collider.kind == "PLANE":
            issues.append(f"动态碰撞体“{collider.display_name}”只支持球形或胶囊，不能使用无限平面")
        if collider.bone_name:
            if any(c.root_swing and c.nodes and c.nodes[0].bone_name == collider.bone_name for c in project.chains):
                issues.append(f"碰撞体“{collider.display_name}”不能附着可摆动根骨；身体避让体请附着固定父骨")
            bone = owner.data.bones.get(collider.bone_name)
            if bone is None:
                issues.append(
                    f"碰撞体“{collider.display_name}”引用不存在的骨骼“{collider.bone_name}”"
                )
            if dynamic and collider.bone_name not in simulated:
                issues.append(
                    f"动态碰撞体“{collider.display_name}”只能吸附本项目唯一的模拟节点"
                )
            if not dynamic and collider.bone_name in simulated:
                issues.append(
                    f"静态碰撞体“{collider.display_name}”不能跟随模拟骨骼“{collider.bone_name}”"
                )
        bone_name_b = str(getattr(collider, "bone_name_b", "") or "")
        if bone_name_b:
            if collider.kind != "CAPSULE":
                issues.append(f"碰撞体“{collider.display_name}”只有胶囊形状可以设置第二端骨骼")
            if dynamic:
                issues.append(f"动态碰撞体“{collider.display_name}”不能同时吸附两根骨骼")
            bone_b = owner.data.bones.get(bone_name_b)
            if bone_b is None:
                issues.append(f"碰撞体“{collider.display_name}”引用不存在的终点骨骼“{bone_name_b}”")
            if bone_name_b in simulated:
                issues.append(f"静态碰撞体“{collider.display_name}”不能跟随模拟终点骨“{bone_name_b}”")
        elif collider.kind != "CAPSULE" and getattr(collider, "bone_name_b", ""):
            issues.append(f"碰撞体“{collider.display_name}”的终点骨骼字段无效")
        if collider.kind == "PLANE":
            normal_length = math.sqrt(sum(value * value for value in collider.b))
            if normal_length < 1.0e-6:
                issues.append(f"平面碰撞体“{collider.display_name}”的法线不能为零")

    chains_by_uuid = {chain.uuid: chain for chain in project.chains}
    link_names = set()
    chain_pair_keys = set()
    for link in project.chain_links:
        problem = _name_issue("链组连接", link.display_name, link_names)
        if problem:
            issues.append(problem)
        chain_a = chains_by_uuid.get(link.chain_a_uuid)
        chain_b = chains_by_uuid.get(link.chain_b_uuid)
        if chain_a is None or chain_b is None:
            issues.append(f"链组连接“{link.display_name}”引用了不存在的物理骨链")
            continue
        if chain_a == chain_b:
            issues.append(f"链组连接“{link.display_name}”必须连接两条不同物理骨链")
        pair_key = frozenset((link.chain_a_uuid, link.chain_b_uuid))
        if pair_key in chain_pair_keys:
            issues.append(f"链组连接“{link.display_name}”与现有连接使用了相同的两条骨链")
        chain_pair_keys.add(pair_key)
        coupling = float(link.coupling)
        max_correction = float(link.max_correction)
        if not math.isfinite(coupling) or not 0.0 < coupling <= 1.0:
            issues.append(f"链组连接“{link.display_name}”的耦合强度必须在 (0, 1] 范围")
        if not math.isfinite(max_correction) or not 0.0 < max_correction <= 10.0:
            issues.append(f"链组连接“{link.display_name}”的单轮最大修正必须在 (0, 10] 范围")
        simulated_a = len(chain_a.nodes) - 1
        simulated_b = len(chain_b.nodes) - 1
        if link.strip_constraint and simulated_a != simulated_b:
            issues.append(f"链组连接“{link.display_name}”的布条约束要求两链节点数相等")
        if link.collisions and simulated_a != simulated_b:
            issues.append(
                f"链组连接“{link.display_name}”的两条骨链模拟节点数不同，不能保留链间碰撞体"
            )
        expected_indices = set(range(1, len(chain_a.nodes))) if simulated_a == simulated_b else set()
        actual_indices = {int(collision.local_joint_index) for collision in link.collisions}
        if link.collisions and actual_indices != expected_indices:
            issues.append(
                f"链组连接“{link.display_name}”的链间碰撞体必须完整覆盖 local Joint 1..{simulated_a}"
            )
        if len(actual_indices) != len(link.collisions):
            issues.append(f"链组连接“{link.display_name}”存在重复的链间碰撞 Joint 序号")
        for collision in link.collisions:
            index = int(collision.local_joint_index)
            if index <= 0 or index >= len(chain_a.nodes) or index >= len(chain_b.nodes):
                issues.append(
                    f"链组连接“{link.display_name}”的链间碰撞 Joint 序号 {index} 超出有效范围"
                )
            radius = float(collision.radius)
            if not math.isfinite(radius) or not 0.0 < radius <= 10.0:
                issues.append(f"链组连接“{link.display_name}”的链间碰撞半径必须在 (0, 10] 范围")
            if int(collision.group_mask) <= 0 or int(collision.collision_mask) <= 0:
                issues.append(
                    f"链组连接“{link.display_name}”的链间碰撞组掩码和碰撞掩码都不能为 0"
                )

    return issues


def _collection_parents(target):
    return [
        collection
        for collection in bpy.data.collections
        if any(child == target for child in collection.children)
    ]


def _collection_scenes(target):
    return [
        scene
        for scene in bpy.data.scenes
        if any(child == target for child in scene.collection.children)
    ]


def _collection_owned_by(collection, owner, project_uuid):
    return bool(
        collection.get(properties.COLLECTION_SCHEMA_KEY)
        == properties.CONTROLLER_SCHEMA_VERSION
        and collection.get(properties.COLLECTION_PROJECT_KEY) == project_uuid
        and collection.get(properties.COLLECTION_OWNER_KEY) == owner
    )


def controller_issues(owner):
    """检查 controller 一一映射、精确 Outliner 层级、锁定和骨绑定。"""

    project = owner.data.hd2pb_project
    if not project_initialized(project):
        return ["当前骨架尚未初始化 HD2 物理骨项目"]
    issues = []
    expected = {
        (entity_type, entity_uuid): (entity, parent)
        for entity_type, entity_uuid, entity, parent in _controller_entries(project)
    }
    counts = {key: [] for key in expected}

    managed = [
        collection
        for collection in bpy.data.collections
        if _collection_owned_by(collection, owner, project.project_uuid)
    ]

    def unique_collection(role, entity_uuid=""):
        matches = [
            collection
            for collection in managed
            if collection.get(properties.COLLECTION_ROLE_KEY) == role
            and collection.get(properties.COLLECTION_ENTITY_KEY, "") == entity_uuid
        ]
        if len(matches) != 1:
            suffix = f"、实体 {entity_uuid}" if entity_uuid else ""
            issues.append(
                f"大纲视图（Outliner）中{collection_role_label(role)}{suffix}应有 1 个，"
                f"当前为 {len(matches)} 个"
            )
            return None
        return matches[0]

    root = unique_collection("ROOT")
    config = unique_collection("CONFIG")
    chains_root = unique_collection("CHAINS")
    colliders_root = unique_collection("COLLIDERS")
    chain_links_root = unique_collection("CHAIN_LINKS")
    chain_collections = {
        chain.uuid: unique_collection("CHAIN", chain.uuid)
        for chain in project.chains
    }
    chain_link_collections = {
        link.uuid: unique_collection("CHAIN_LINK", link.uuid)
        for link in project.chain_links
    }

    if project.controller_collection is None:
        issues.append("项目的 Outliner 根集合已丢失；请执行“重建编辑对象”")
    elif project.controller_collection != root:
        issues.append("项目记录的 Outliner 根集合不是当前 owner 的唯一 ROOT 集合")

    expected_parent = {}
    if root is not None:
        for collection in (config, chains_root, colliders_root, chain_links_root):
            if collection is not None:
                expected_parent[collection] = root
        scene_links = _collection_scenes(root)
        owner_scenes = set(owner.users_scene)
        if len(scene_links) != 1 or scene_links[0] not in owner_scenes:
            issues.append("Outliner 根集合必须只链接到一个包含所属 Armature 的 Scene")
    if chains_root is not None:
        for collection in chain_collections.values():
            if collection is not None:
                expected_parent[collection] = chains_root
    if chain_links_root is not None:
        for collection in chain_link_collections.values():
            if collection is not None:
                expected_parent[collection] = chain_links_root
    for collection, parent in expected_parent.items():
        parents = _collection_parents(collection)
        if parents != [parent]:
            issues.append(
                f"管理集合“{collection.name}”没有唯一位于预期父集合“{parent.name}”下"
            )
    for collection in managed:
        for child in collection.children:
            if child not in managed:
                issues.append(
                    f"管理集合“{collection.name}”下混入未归属本项目的子集合“{child.name}”"
                )
        for obj in collection.objects:
            if not (
                getattr(obj, "hd2pb_controller_schema", 0)
                or getattr(obj, "hd2pb_preview_schema", 0)
            ):
                issues.append(f"管理集合“{collection.name}”中混入用户对象“{obj.name}”")

    expected_collections = {}
    if config is not None:
        expected_collections[(properties.ENTITY_PROJECT, project.project_uuid)] = config
        for profile in project.profiles:
            expected_collections[(properties.ENTITY_PROFILE, profile.uuid)] = config
    if colliders_root is not None:
        for collider in project.colliders:
            expected_collections[(properties.ENTITY_COLLIDER, collider.uuid)] = colliders_root
    for chain in project.chains:
        chain_collection = chain_collections.get(chain.uuid)
        if chain_collection is None:
            continue
        expected_collections[(properties.ENTITY_CHAIN, chain.uuid)] = chain_collection
        for node in list(chain.nodes)[1:]:
            expected_collections[(properties.ENTITY_NODE, node.uuid)] = chain_collection
    for link in project.chain_links:
        link_collection = chain_link_collections.get(link.uuid)
        if link_collection is None:
            continue
        expected_collections[(properties.ENTITY_CHAIN_LINK, link.uuid)] = link_collection
        for collision in link.collisions:
            expected_collections[
                (properties.ENTITY_LINK_COLLISION, collision.uuid)
            ] = link_collection

    controller_uuids = set()
    for controller in project_controllers(owner, project.project_uuid):
        if controller.hd2pb_owner_object != owner:
            issues.append(
                f"编辑对象“{controller.name}”使用本项目 UUID，却指向其他所属骨架"
            )
            continue
        if controller.hd2pb_project_uuid != project.project_uuid:
            issues.append(f"编辑对象“{controller.name}”的项目 UUID 与所属骨架不一致")
            continue
        if controller.hd2pb_controller_schema != properties.CONTROLLER_SCHEMA_VERSION:
            issues.append(
                f"编辑对象“{controller.name}”的版本为 {controller.hd2pb_controller_schema}，"
                "请重建编辑对象"
            )
        key = (controller.hd2pb_entity_type, controller.hd2pb_entity_uuid)
        if key not in expected:
            issues.append(f"编辑对象“{controller.name}”引用了不存在的 canonical 实体")
            continue
        counts[key].append(controller)
        if not is_valid_uuid(controller.hd2pb_controller_uuid):
            issues.append(f"编辑对象“{controller.name}”的 controller UUID 损坏")
        elif controller.hd2pb_controller_uuid in controller_uuids:
            issues.append(
                f"编辑对象“{controller.name}”的 controller UUID 与本项目另一对象重复"
            )
        else:
            controller_uuids.add(controller.hd2pb_controller_uuid)
        if controller.type != "EMPTY":
            issues.append(f"编辑对象“{controller.name}”不是空对象（Empty）；请重建编辑对象")
        if tuple(controller.lock_location) != (True, True, True):
            issues.append(f"编辑对象“{controller.name}”的位置锁已损坏；请重建编辑对象")
        if tuple(controller.lock_rotation) != (True, True, True):
            issues.append(f"编辑对象“{controller.name}”的旋转锁已损坏；请重建编辑对象")
        if tuple(controller.lock_scale) != (True, True, True):
            issues.append(f"编辑对象“{controller.name}”的缩放锁已损坏；请重建编辑对象")
        if not controller.hide_render:
            issues.append(f"编辑对象“{controller.name}”不应参与渲染；请重建编辑对象")
        # show_name is a viewport preference, not part of entity ownership or
        # physics validity. Keep style defaults outside structural validation.
        if controller.hd2pb_entity_type != properties.ENTITY_NODE and controller.hide_viewport:
            issues.append(
                f"内部编辑对象“{controller.name}”不应通过隐藏标志排除显示；请重建编辑对象"
            )

        expected_collection = expected_collections.get(key)
        if expected_collection is not None and list(controller.users_collection) != [expected_collection]:
            issues.append(
                f"编辑对象“{controller.name}”没有唯一位于预期集合“{expected_collection.name}”中"
            )

        entity, _parent = expected[key]
        from . import binding
        expected_location = (0.0, 0.0, 0.0)
        if controller.hd2pb_entity_type in {
            properties.ENTITY_CHAIN,
            properties.ENTITY_COLLIDER,
            properties.ENTITY_CHAIN_LINK,
            properties.ENTITY_LINK_COLLISION,
        } and not controller.hide_select:
            issues.append(
                f"内部编辑对象“{controller.name}”不应可选；请选择对应的 Curve 实体"
            )
        if controller.hd2pb_entity_type == properties.ENTITY_NODE:
            bone = owner.data.bones.get(entity.bone_name)
            if controller.empty_display_type != "SPHERE":
                issues.append(f"节点编辑对象“{controller.name}”必须使用球形 Empty")
            if (
                abs(
                    controller.empty_display_size
                    - max(float(entity.radius), properties.NODE_DISPLAY_MIN_RADIUS)
                )
                > 1.0e-6
            ):
                issues.append(f"节点编辑对象“{controller.name}”的球半径没有同步 canonical radius")
            if not binding.matches(controller, owner, entity.bone_name):
                issues.append(
                    f"节点编辑对象“{controller.name}”没有正确吸附骨骼“{entity.bone_name}”"
                )
            if bone is not None:
                expected_location = binding.expected_location(controller, owner, entity.bone_name)
        elif (
            controller.hd2pb_entity_type == properties.ENTITY_COLLIDER
            and entity.bone_name
            and not (entity.kind == "CAPSULE" and getattr(entity, "bone_name_b", ""))
        ):
            bone = owner.data.bones.get(entity.bone_name)
            if not binding.matches(controller, owner, entity.bone_name):
                issues.append(
                    f"碰撞体编辑对象“{controller.name}”没有正确吸附骨骼“{entity.bone_name}”"
                )
            if bone is not None:
                expected_location = binding.expected_location(controller, owner, entity.bone_name)
        elif not binding.matches(controller, owner):
            issues.append(f"编辑对象“{controller.name}”没有绑定到所属骨架对象（Armature Object）")

        if any(abs(a - b) > 1.0e-6 for a, b in zip(controller.location, expected_location)):
            issues.append(f"编辑对象“{controller.name}”的局部位置偏离权威骨骼吸附点")
        if any(abs(value - 1.0) > 1.0e-6 for value in controller.scale):
            issues.append(f"编辑对象“{controller.name}”的缩放值不是 1；请重建编辑对象")
        if abs(controller.matrix_basis.to_quaternion().angle) > 1.0e-6:
            issues.append(f"编辑对象“{controller.name}”的局部旋转不是零；请重建编辑对象")

    for (entity_type, entity_uuid), matches in counts.items():
        if len(matches) == 0:
            issues.append(
                f"{entity_label(entity_type)}（{entity_uuid}）缺少编辑对象；请执行“重建编辑对象”"
            )
        elif len(matches) > 1:
            issues.append(
                f"{entity_label(entity_type)}（{entity_uuid}）有 {len(matches)} 个重复编辑对象；"
                "请执行“重建编辑对象”"
            )
    return issues


def project_issues(owner, *, include_controllers=True, include_previews=False, require_chains=True):
    issues = canonical_issues(owner, require_chains=require_chains)
    if include_controllers and project_initialized(owner.data.hd2pb_project):
        issues.extend(controller_issues(owner))
    if include_previews and project_initialized(owner.data.hd2pb_project):
        # 延迟导入避免 preview -> ownership 的模块环；preview 问题只影响制作体验，
        # export gate 必须保持 include_previews=False。
        from . import preview

        issues.extend(preview.preview_issues(owner))
    return issues


__all__ = (
    "HD2PBError",
    "new_uuid",
    "is_valid_uuid",
    "armature_objects",
    "project_armatures",
    "project_uuid_conflicts",
    "active_owner",
    "peek_remembered_project_owner",
    "remembered_project_owner",
    "remember_project_owner",
    "forget_project_owner",
    "project_owner",
    "explicit_export_owner",
    "project_initialized",
    "ensure_authoring_owner",
    "entity_entries",
    "find_entity",
    "project_controllers",
    "controllers_for_entity",
    "resolve_controller",
    "controller_problem",
    "canonical_issues",
    "controller_issues",
    "project_issues",
)
