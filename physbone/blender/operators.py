"""HD2 物理骨操作符"""

from __future__ import annotations

import math
import os
from pathlib import Path
import traceback

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Vector

from ..core import preset_v1
from . import adapter_v1, controllers, ownership, preview, properties


_PARAMETER_CLIPBOARD = None
_LAST_SOLVER_CHECK = None
_PRESET_ENUM_ROWS = []
_PRESET_ENUM_STRING_POOL = []
_TYPED_PRESET_ENUM_CACHE = {}
_TYPED_PRESET_ENUM_NUMBERS = {}
_NODE_APPLY_MODES = (
    ("NUMERIC", "仅数值（保留目标方向）", "保留限制轴与参考方向；不兼容的限制类型会拒绝，不自动改方向"),
    ("ALL", "包含方向", "复制全部节点参数；方向属于父骨局部坐标，不做跨骨坐标换算"),
    ("DIRECTION", "仅方向", "仅复制限制轴与参考模式／方向／坐标架，其他参数保持不变"),
)
_CHAIN_SCOPES = (
    ("CURRENT", "当前骨链／节点", "只修改当前实体"),
    ("SELECTED_CHAINS", "选中的同项目骨链", "仅明确选中的骨链；不包含其他项目，不把空选区当作全部"),
)


def clear_runtime_state():
    """清空不能跨禁用、热重载或文件载入保留的内存状态。"""

    global _PARAMETER_CLIPBOARD, _LAST_SOLVER_CHECK
    _PARAMETER_CLIPBOARD = None
    _LAST_SOLVER_CHECK = None
    _PRESET_ENUM_ROWS.clear()
    _PRESET_ENUM_STRING_POOL.clear()
    _TYPED_PRESET_ENUM_CACHE.clear()
    _TYPED_PRESET_ENUM_NUMBERS.clear()
    from . import diagnostics, chain_profile_ops
    diagnostics.clear()
    chain_profile_ops.clear_runtime_state()


def runtime_state_present():
    from . import diagnostics, chain_profile_ops
    return bool(
        _PARAMETER_CLIPBOARD is not None
        or _PRESET_ENUM_ROWS
        or _PRESET_ENUM_STRING_POOL
        or _TYPED_PRESET_ENUM_CACHE
        or diagnostics.present()
        or chain_profile_ops.runtime_state_present()
    )


def _cancel(operator, exc):
    if isinstance(exc, ownership.HD2PBError):
        detail = str(exc) if str(exc) else "请检查当前骨架和项目数据"
        message = f"物理骨操作失败：{detail}"
    else:
        print("HD2 物理骨内部操作异常；以下信息仅供开发诊断：")
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        message = (
            "物理骨内部操作失败，本次操作未能完成；如界面仍显示变化，可立即撤销，"
            "并请查看系统控制台中的开发诊断信息"
        )
    operator.report({"ERROR"}, message)
    return {"CANCELLED"}


def _unique_display_name(items, base):
    used = {item.display_name for item in items}
    if base not in used:
        return base
    index = 2
    while f"{base}.{index:03d}" in used:
        index += 1
    return f"{base}.{index:03d}"


def _unique_descendant_path(start_bone, *, purpose, branch_help=None):
    """从起始骨沿唯一直接子骨走到末端；不猜分叉。"""

    ordered = [start_bone]
    current = start_bone
    while current.children:
        children = sorted(current.children, key=lambda bone: bone.name)
        if len(children) > 1:
            names = "、".join(f"“{child.name}”" for child in children)
            help_text = branch_help or "请改用明确选中路径的高级工具"
            raise ownership.HD2PBError(
                f"骨骼“{current.name}”有多个直接子骨：{names}；{purpose}不能自动猜测分支，"
                f"{help_text}"
            )
        current = children[0]
        ordered.append(current)
    return ordered


def _rename_bones_exact(armature, bones, target_names):
    """先全部换成唯一临时名，再原子化地落到目标名，禁止 Blender 自动追加后缀。"""

    if len(bones) != len(target_names):
        raise RuntimeError("骨链重命名内部长度不一致")
    temporary_names = []
    for index, _bone in enumerate(bones):
        while True:
            candidate = f"__HD2PB_RENAME_{ownership.new_uuid().replace('-', '')}_{index}"
            if armature.bones.get(candidate) is None:
                temporary_names.append(candidate)
                break
    for bone, temporary in zip(bones, temporary_names):
        bone.name = temporary
        if bone.name != temporary:
            raise RuntimeError("Blender 未能建立骨链重命名临时名称")
    for bone, target in zip(bones, target_names):
        bone.name = target
        if bone.name != target:
            raise RuntimeError(
                f"Blender 把目标骨名“{target}”改写为“{bone.name}”；为避免静默 .001 后缀已终止"
            )


def _automatic_bone_path(context, owner):
    """RE Chain 式主流程：单选起始骨，自动延伸到唯一末端。"""

    selected_pose = list(getattr(context, "selected_pose_bones", None) or ())
    active_pose_bone = getattr(context, "active_pose_bone", None)
    if (
        owner.mode != "POSE"
        or len(selected_pose) != 1
        or active_pose_bone != selected_pose[0]
    ):
        raise ownership.HD2PBError(
            "请在姿态模式（Pose Mode）只选择并激活一根起始骨；"
            "插件会自动沿唯一子级延伸到末端"
        )
    start = active_pose_bone.bone
    if start.parent is None:
        raise ownership.HD2PBError(
            f"起始骨骼“{start.name}”没有父骨，无法建立隐藏的固定驱动锚点；"
            "请选择一根具有父骨的模拟起始骨"
        )
    simulated = _unique_descendant_path(start, purpose="自动建链")
    return [start.parent, *simulated]


def _selected_explicit_bone_path(context, owner):
    """高级流程：只按用户明确选中的唯一直接父子路径建链。"""

    selected_pose = list(getattr(context, "selected_pose_bones", None) or ())
    if owner.mode != "POSE" or not selected_pose:
        raise ownership.HD2PBError(
            "请在姿态模式（Pose Mode）明确选择一条直接父子路径后再创建"
        )
    selected = {pose_bone.name: pose_bone.bone for pose_bone in selected_pose}
    roots = [
        bone
        for bone in selected.values()
        if bone.parent is None or bone.parent.name not in selected
    ]
    if len(roots) != 1:
        raise ownership.HD2PBError("选中骨必须只有一个根，不能包含断开的多段骨链")
    ordered = []
    current = roots[0]
    while current is not None:
        ordered.append(current)
        children = [child for child in current.children if child.name in selected]
        if len(children) > 1:
            raise ownership.HD2PBError(
                f"选区在骨骼“{current.name}”处分叉；一次只能创建一条直接父子链"
            )
        current = children[0] if children else None
    if len(ordered) != len(selected):
        raise ownership.HD2PBError("选区包含断开的骨骼；请只选择一条连续父子路径")
    if ordered[0].parent is None:
        raise ownership.HD2PBError(
            f"起始骨骼“{ordered[0].name}”没有父骨，无法建立隐藏的固定驱动锚点；"
            "请从具有父骨的模拟起始骨开始选择"
        )
    ordered.insert(0, ordered[0].parent)
    return ordered


def _active_profile(project):
    """解析本项目的新建链 Profile；旧文件空值时安全回退第一项。"""

    selected = next(
        (
            profile
            for profile in project.profiles
            if profile.uuid == project.new_chain_profile_uuid
        ),
        None,
    )
    return selected or (project.profiles[0] if project.profiles else None)


def _validate_new_chain(project, bones):
    # Two creation entry points share this preflight, before canonical writes or
    # automatic tail creation. New chains have root_swing=False: their first
    # bone is a permitted animation anchor. Include disabled rules and Bone tags
    # so renaming a marked bone cannot bypass target/physics mutual exclusion.
    pose_targets = ownership.pose_driver_target_bones(project, bones)
    conflicts = sorted({bone.name for bone in bones[1:]}.intersection(pose_targets))
    if conflicts:
        raise ownership.HD2PBError(
            f"骨骼“{'、'.join(conflicts)}”已被衣物姿态驱动目标占用；"
            "姿态目标骨与真正模拟骨互斥（禁用规则仍占用）；仅固定链根允许作为目标，不能创建此物理骨链"
        )
    if not 2 <= len(bones) <= 64:
        raise ownership.HD2PBError(
            f"所选路径会生成 {len(bones) - 1} 个模拟节点；HD2PHY1 要求每条链为 1..63 个"
        )
    # Authoring precedes Archive selection. Slots are validated at the actual
    # wire export boundary (AQSDK target compiler).
    for previous, current in zip(bones, bones[1:]):
        if current.parent != previous:
            raise ownership.HD2PBError(
                f"骨骼“{previous.name}”与“{current.name}”不是直接父子关系"
            )
        distance = (current.matrix_local.translation - previous.matrix_local.translation).length
        if distance < 1.0e-5:
            raise ownership.HD2PBError(
                f"骨骼“{previous.name}”到“{current.name}”的 Rest 段长度接近零，无法模拟"
            )
    existing_drivers = set()
    existing_simulated = set()
    for chain in project.chains:
        for index, node in enumerate(chain.nodes):
            (existing_drivers if index == 0 else existing_simulated).add(node.bone_name)
    driver = bones[0].name
    simulated = {bone.name for bone in bones[1:]}
    if driver in existing_simulated:
        raise ownership.HD2PBError(
            f"骨骼“{driver}”已经属于另一条物理骨链，当前起始位置不能重复使用"
        )
    duplicate = simulated.intersection(existing_simulated)
    if duplicate:
        raise ownership.HD2PBError(
            f"模拟骨骼“{sorted(duplicate)[0]}”已经属于另一条物理骨链"
        )
    conflict = simulated.intersection(existing_drivers)
    if conflict:
        raise ownership.HD2PBError(
            f"骨骼“{sorted(conflict)[0]}”已被另一条物理骨链占用，不能重复作为模拟节点"
        )
    collider_attachments = {
        collider.bone_name
        for collider in project.colliders
        if collider.bone_name and collider.attachment_mode == "STATIC"
    }
    collision_conflict = simulated.intersection(collider_attachments)
    if collision_conflict:
        bone_name = sorted(collision_conflict)[0]
        collider_name = next(
            collider.display_name
            for collider in project.colliders
            if collider.bone_name == bone_name
        )
        raise ownership.HD2PBError(
            f"骨骼“{bone_name}”正被碰撞体“{collider_name}”作为固定吸附骨；"
            "HD2PHY1 不能让碰撞体跟随模拟骨，请先改绑或删除该碰撞体"
        )


def _regenerate_project_uuids(project):
    old_to_new_profile = {}
    old_to_new_chain = {}
    previous_default_profile = project.new_chain_profile_uuid
    project.preview_isolate_entity_uuid = ""
    project.project_uuid = ownership.new_uuid()
    for profile in project.profiles:
        previous = profile.uuid
        profile.uuid = ownership.new_uuid()
        old_to_new_profile[previous] = profile.uuid
    fallback_profile = project.profiles[0].uuid if project.profiles else ""
    project.new_chain_profile_uuid = old_to_new_profile.get(
        previous_default_profile,
        fallback_profile,
    )
    for chain in project.chains:
        previous = chain.uuid
        chain.uuid = ownership.new_uuid()
        old_to_new_chain[previous] = chain.uuid
        chain.spring_profile_uuid = old_to_new_profile.get(
            chain.spring_profile_uuid, fallback_profile
        )
        for node in chain.nodes:
            node.uuid = ownership.new_uuid()
    for collider in project.colliders:
        collider.uuid = ownership.new_uuid()
    for link in project.chain_links:
        link.uuid = ownership.new_uuid()
        link.chain_a_uuid = old_to_new_chain.get(link.chain_a_uuid, "")
        link.chain_b_uuid = old_to_new_chain.get(link.chain_b_uuid, "")
        for collision in link.collisions:
            collision.uuid = ownership.new_uuid()


_PROFILE_FIELDS = (
    "uuid",
    "display_name",
    "stiffness",
    "damping",
    "gravity",
    "wind",
    "collision_mask",
)
_NODE_FIELDS = (
    "uuid",
    "bone_name",
    "override_collision_filter",
    "group_mask",
    "collision_mask",
    "radius",
    "limit_kind",
    "limit_angle",
    "limit_angle2",
    "limit_axis",
    "stiffness_scale",
    "damping_scale",
    "reference_mode",
    "reference_direction",
    "reference_frame",
)
_CHAIN_FIELDS = (
    "uuid",
    "display_name",
    "spring_profile_uuid",
    "group_mask",
    "collision_mask",
    "root_swing",
    "local_bend",
)
_COLLIDER_FIELDS = (
    "uuid",
    "display_name",
    "kind",
    "attachment_mode",
    "bone_name",
    "bone_name_b",
    "group_mask",
    "collision_mask",
    "inside",
    "a",
    "b",
    "radius",
)
_CHAIN_LINK_FIELDS = (
    "uuid",
    "display_name",
    "chain_a_uuid",
    "chain_b_uuid",
    "coupling",
    "strip_constraint",
    "max_correction",
)
_LINK_COLLISION_FIELDS = (
    "uuid",
    "local_joint_index",
    "group_mask",
    "radius",
    "collision_mask",
)
_PROJECT_AUTHORING_FIELDS = (
    "new_chain_auto_tail",
    "preview_enabled",
    "preview_show_chains",
    "preview_show_nodes",
    "preview_show_limits",
    "preview_show_colliders",
    "preview_show_chain_links",
    "preview_show_link_collisions",
    "preview_in_front",
    "preview_xray_nodes",
    "preview_xray_limits",
    "preview_xray_chains",
    "preview_xray_chain_links",
    "preview_xray_link_collisions",
    "preview_xray_colliders",
    "preview_show_names",
    "preview_show_node_names",
    "preview_show_collider_names",
    "preview_allow_collider_transform",
    "preview_chain_color",
    "preview_node_color",
    "preview_limit_color",
    "preview_collider_color",
    "preview_chain_link_color",
    "preview_link_collision_color",
    "preview_curve_radius",
    "preview_limit_cone_height",
    "preview_plane_size",
    "preview_isolate_entity_uuid",
)


def _snapshot_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bpy.types.ID):
        return value
    return tuple(value)


def _snapshot_fields(item, fields):
    return {name: _snapshot_value(getattr(item, name)) for name in fields}


def _snapshot_project(project):
    """保存一次小型 canonical 快照，供失败返回 CANCELLED 前显式回滚。"""

    snapshot = {
        "schema_version": project.schema_version,
        "project_uuid": project.project_uuid,
        "fixed_dt": project.fixed_dt,
        "new_chain_profile_uuid": project.new_chain_profile_uuid,
        "pose_collider_kind": project.pose_collider_kind,
        "authoring_object": project.authoring_object,
        "controller_collection": project.controller_collection,
        "profiles": [_snapshot_fields(item, _PROFILE_FIELDS) for item in project.profiles],
        "chains": [
            {
                **_snapshot_fields(chain, _CHAIN_FIELDS),
                "nodes": [_snapshot_fields(node, _NODE_FIELDS) for node in chain.nodes],
            }
            for chain in project.chains
        ],
        "colliders": [_snapshot_fields(item, _COLLIDER_FIELDS) for item in project.colliders],
        "chain_links": [
            {
                **_snapshot_fields(item, _CHAIN_LINK_FIELDS),
                "collisions": [
                    _snapshot_fields(collision, _LINK_COLLISION_FIELDS)
                    for collision in item.collisions
                ],
            }
            for item in project.chain_links
        ],
    }
    snapshot.update(_snapshot_fields(project, _PROJECT_AUTHORING_FIELDS))
    return snapshot


def _restore_fields(item, values):
    for name, value in values.items():
        setattr(item, name, value)


def _restore_project(project, snapshot):
    with preview.suspend_updates():
        project.profiles.clear()
        project.chains.clear()
        project.colliders.clear()
        project.chain_links.clear()
        project.schema_version = snapshot["schema_version"]
        project.project_uuid = snapshot["project_uuid"]
        project.fixed_dt = snapshot["fixed_dt"]
        project.new_chain_profile_uuid = snapshot["new_chain_profile_uuid"]
        project.pose_collider_kind = snapshot["pose_collider_kind"]
        project.authoring_object = snapshot["authoring_object"]
        project.controller_collection = snapshot["controller_collection"]
        _restore_fields(
            project,
            {name: snapshot[name] for name in _PROJECT_AUTHORING_FIELDS},
        )
        for values in snapshot["profiles"]:
            _restore_fields(project.profiles.add(), values)
        for chain_values in snapshot["chains"]:
            node_values = chain_values["nodes"]
            chain = project.chains.add()
            _restore_fields(chain, {key: value for key, value in chain_values.items() if key != "nodes"})
            for values in node_values:
                _restore_fields(chain.nodes.add(), values)
        for values in snapshot["colliders"]:
            _restore_fields(project.colliders.add(), values)
        for values in snapshot.get("chain_links", ()):
            link = project.chain_links.add()
            collision_values = values.get("collisions", ())
            _restore_fields(
                link,
                {key: value for key, value in values.items() if key != "collisions"},
            )
            for collision_values_item in collision_values:
                _restore_fields(link.collisions.add(), collision_values_item)


def _cancel_with_rollback(operator, project, snapshot, exc):
    try:
        _restore_project(project, snapshot)
    except Exception as rollback_exc:
        return _cancel(
            operator,
            RuntimeError(f"操作失败且 canonical 回滚失败：{rollback_exc}"),
        )
    return _cancel(operator, exc)


def _remember_navigation_after_commit(operator, context, owner):
    """项目事务已提交后，导航记录失败只能降级，不能反向回滚 canonical。"""

    try:
        ownership.remember_project_owner(context, owner)
    except Exception:
        print("HD2 物理骨项目已提交，但当前项目导航记录更新失败：")
        traceback.print_exc()
        operator.report(
            {"WARNING"},
            "项目数据与预览已经完成，但当前项目导航未能更新；请在“当前物理骨项目”中重新选择",
        )


class HD2PB_OT_initialize_project(Operator):
    """在单用户骨架（Armature）上建立权威项目和默认参数配置。"""

    bl_idname = "hd2pb.initialize_project"
    bl_label = "初始化物理骨项目"
    bl_description = (
        "在当前单用户骨架数据（Armature datablock）上创建物理骨项目和默认弹簧参数配置，并生成可在 "
        "大纲视图（Outliner）选择的锁定空对象（Empty）"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == "ARMATURE"

    def execute(self, context):
        owner = context.object
        project = owner.data.hd2pb_project
        snapshot = _snapshot_project(project)
        try:
            if ownership.project_initialized(project):
                raise ownership.HD2PBError("当前骨架（Armature）已经初始化物理骨项目")
            if len(ownership.armature_objects(owner.data)) != 1:
                raise ownership.HD2PBError(
                    "当前骨架数据（Armature datablock）被多个对象共享；请先让要制作的骨架成为单用户数据"
                )
            project.schema_version = properties.PROJECT_SCHEMA_VERSION
            project.project_uuid = ownership.new_uuid()
            project.authoring_object = owner
            profile = project.profiles.add()
            profile.uuid = ownership.new_uuid()
            profile.display_name = "默认弹簧参数"
            project.new_chain_profile_uuid = profile.uuid
            result = controllers.rebuild_project_controllers(
                owner, context_scene=context.scene
            )
        except Exception as exc:
            return _cancel_with_rollback(self, project, snapshot, exc)
        _remember_navigation_after_commit(self, context, owner)
        self.report(
            {"INFO"},
            f"已初始化物理骨项目并创建 {result['controllers']} 个编辑对象；"
        )
        return {"FINISHED"}


class HD2PB_OT_migrate_project(Operator):
    """显式把旧开发期 schema 1/2 升级为当前 schema 3。"""

    bl_idname = "hd2pb.migrate_project"
    bl_label = "迁移项目数据"
    bl_description = (
        "显式把旧 schema 逐版本迁移到当前版本并支持撤销；不会在文件载入、面板绘制或插件注册时"
        "静默修改项目。schema 1/2 会补入 动态碰撞与链组连接默认值，并原子重建编辑投影"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        owner = ownership.project_owner(context)
        if owner is None:
            return False
        schema_version = owner.data.hd2pb_project.schema_version
        return 0 < schema_version < properties.PROJECT_SCHEMA_VERSION

    def execute(self, context):
        owner = ownership.project_owner(context, remember=True)
        project = owner.data.hd2pb_project
        snapshot = _snapshot_project(project)
        try:
            if project.schema_version > properties.PROJECT_SCHEMA_VERSION:
                raise ownership.HD2PBError(
                    f"项目版本 {project.schema_version} 高于当前支持的 "
                    f"{properties.PROJECT_SCHEMA_VERSION}，请升级插件"
                )
            if project.schema_version < 1:
                raise ownership.HD2PBError(
                    "当前版本没有可安全自动迁移的历史 schema；请保留文件并使用对应版本插件"
                )
            if project.schema_version == properties.PROJECT_SCHEMA_VERSION:
                self.report(
                    {"INFO"},
                    f"项目已经是当前 schema {properties.PROJECT_SCHEMA_VERSION}，无需迁移，数据未改变",
                )
                return {"FINISHED"}
            if project.schema_version not in {1, 2}:
                raise ownership.HD2PBError(
                    f"项目版本 {project.schema_version} 没有定义逐步迁移路径"
                )
            # schema 1 不能通过 ensure_authoring_owner，因此迁移前直接调用同一只读清理预检。
            controllers._projection_cleanup_plan(
                owner,
                project.project_uuid,
                context_scene=context.scene,
            )
            previous_version = int(project.schema_version)
            with preview.suspend_updates():
                project.schema_version = properties.PROJECT_SCHEMA_VERSION
            result = controllers.rebuild_project_controllers(
                owner,
                context_scene=context.scene,
            )
        except Exception as exc:
            return _cancel_with_rollback(self, project, snapshot, exc)
        _remember_navigation_after_commit(self, context, owner)
        self.report(
            {"INFO"},
            f"已把项目从 schema {previous_version} 迁移到 schema "
            f"{properties.PROJECT_SCHEMA_VERSION}，并重建 "
            f"{result['controllers']} 个 controller 与 {result['previews']} 个实体预览",
        )
        return {"FINISHED"}


def _profile_source_items(_self, _context):
    return typed_preset_items("PROFILE", include_default=True)


class HD2PB_OT_add_profile(Operator):
    """新增一组可被多条 Chain 复用的弹簧参数配置。"""

    bl_idname = "hd2pb.add_profile"
    bl_label = "新增弹簧参数配置"
    bl_description = (
        "在当前 Armature 项目中新增一组刚度、阻尼、重力、风和碰撞掩码参数；"
        "不会自动改动已有物理骨链的配置引用"
    )
    bl_options = {"REGISTER", "UNDO"}

    source_preset: EnumProperty(
        name="初始参数", description="从默认参数或已保存的弹簧预设建立独立配置",
        items=_profile_source_items,
    )
    profile_name: StringProperty(name="配置名称", description="新建弹簧参数配置的显示名称；同名时自动增加编号", default="弹簧参数", maxlen=180)

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, _context):
        self.layout.prop(self, "source_preset")
        self.layout.prop(self, "profile_name")
        self.layout.label(text="新配置独立保存，不改动已有骨链引用", icon="INFO")

    def execute(self, context):
        project = None
        snapshot = None
        try:
            name = self.profile_name.strip()
            if not name:
                raise ownership.HD2PBError("请输入配置名称")
            parameters = None
            if self.source_preset != "__DEFAULT__":
                parameters = preset_v1.read_preset(
                    _preset_directory("PROFILE"), self.source_preset,
                    expected_entity_type="PROFILE",
                )["parameters"]
            owner = ownership.project_owner(context, remember=True)
            project = ownership.ensure_authoring_owner(owner)
            controllers.preflight_project_rebuild(owner, context_scene=context.scene)
            snapshot = _snapshot_project(project)
            profile = project.profiles.add()
            profile.uuid = ownership.new_uuid()
            profile.display_name = _unique_display_name(
                project.profiles[:-1], name
            )
            if parameters is not None:
                _apply_parameter_payload("PROFILE", profile, parameters)
            if not project.new_chain_profile_uuid:
                project.new_chain_profile_uuid = profile.uuid
            controllers.rebuild_project_controllers(owner, context_scene=context.scene)
            project.profile_index = len(project.profiles) - 1
            _remember_navigation_after_commit(self, context, owner)
            self.report({"INFO"}, f"已新增弹簧参数配置“{profile.display_name}”")
            return {"FINISHED"}
        except Exception as exc:
            if project is not None and snapshot is not None:
                return _cancel_with_rollback(self, project, snapshot, exc)
            return _cancel(self, exc)


class HD2PB_OT_remove_profile(Operator):
    """按 UUID 删除未被 Chain 使用的参数配置。"""

    bl_idname = "hd2pb.remove_profile"
    bl_label = "删除弹簧参数配置"
    bl_description = (
        "只删除指定 UUID 的弹簧参数配置；最后一项或仍被物理骨链引用时会拒绝。"
        "不会删除骨骼、Chain、Node、Collider 或网格，操作可一步撤销"
    )
    bl_options = {"REGISTER", "UNDO"}

    entity_uuid: StringProperty(
        name="配置 UUID",
        description="要删除的弹簧参数配置稳定 UUID",
        options={"HIDDEN"},
    )

    def execute(self, context):
        project = None
        snapshot = None
        try:
            owner = ownership.project_owner(context, remember=True)
            project = ownership.ensure_authoring_owner(owner)
            index = next(
                (i for i, item in enumerate(project.profiles) if item.uuid == self.entity_uuid),
                -1,
            )
            if index < 0:
                raise ownership.HD2PBError("要删除的弹簧参数配置 UUID 不存在")
            if len(project.profiles) <= 1:
                raise ownership.HD2PBError("项目必须保留至少一个弹簧参数配置")
            used = [
                chain.display_name
                for chain in project.chains
                if chain.spring_profile_uuid == self.entity_uuid
            ]
            if used:
                raise ownership.HD2PBError(
                    f"配置仍被物理骨链“{used[0]}”引用；请先给这些链选择其他配置"
                )
            controllers.preflight_project_rebuild(owner, context_scene=context.scene)
            snapshot = _snapshot_project(project)
            name = project.profiles[index].display_name
            removed_default = project.new_chain_profile_uuid == self.entity_uuid
            project.profiles.remove(index)
            if removed_default or not any(
                profile.uuid == project.new_chain_profile_uuid
                for profile in project.profiles
            ):
                project.new_chain_profile_uuid = project.profiles[0].uuid
            controllers.rebuild_project_controllers(owner, context_scene=context.scene)
            project.profile_index = min(project.profile_index, len(project.profiles) - 1)
            _remember_navigation_after_commit(self, context, owner)
            self.report({"INFO"}, f"已删除弹簧参数配置“{name}”")
            return {"FINISHED"}
        except Exception as exc:
            if project is not None and snapshot is not None:
                return _cancel_with_rollback(self, project, snapshot, exc)
            return _cancel(self, exc)


class HD2PB_OT_set_creation_profile(Operator):
    """选择姿态模式以后新建物理骨链所使用的弹簧参数配置。"""

    bl_idname = "hd2pb.set_creation_profile"
    bl_label = "选择新建骨链参数配置"
    bl_description = (
        "按稳定 UUID 选择以后新建物理骨链默认使用的弹簧参数配置；不会修改已有骨链，"
            "也不会改变 HD2PHY1 格式"
    )
    bl_options = {"REGISTER", "UNDO"}

    profile_uuid: StringProperty(
        name="配置 UUID",
        description="要用于以后新建物理骨链的弹簧参数配置稳定 UUID",
        options={"HIDDEN"},
    )

    def execute(self, context):
        try:
            owner = ownership.active_owner(context)
            project = ownership.ensure_authoring_owner(owner)
            profile = next(
                (item for item in project.profiles if item.uuid == self.profile_uuid),
                None,
            )
            if profile is None:
                raise ownership.HD2PBError("指定的弹簧参数配置 UUID 不存在")
            project.new_chain_profile_uuid = profile.uuid
            self.report(
                {"INFO"},
                f"以后新建的物理骨链将使用“{profile.display_name}”；已有骨链未改变",
            )
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


def _needs_tail_helper(last):
    return not (last.get('HD2PB_TailHelper') or last.get('HD2PB_GeneratedTip')
                or last.name.lower() in {'end','tip'}
                or last.name.lower().endswith(('_end','_tip')) or last.children)


def _selection_bones(owner):
    """返回当前模式下实际保存选择状态的骨集合，兼容 Blender 4.x/5.x。

    编辑模式始终使用 EditBone；Blender 5.x 将非编辑模式的 select 移到了
    PoseBone，因此按 RNA 能力选取，不依赖版本号或修改骨骼自定义属性。
    """
    if owner.mode == 'EDIT':
        return owner.data.edit_bones
    pose_bones = owner.pose.bones
    first = next(iter(pose_bones), None)
    if first is not None and hasattr(first, 'select'):
        return pose_bones
    return owner.data.bones


def _selected_bone_names(owner):
    """读取当前骨骼选择，用于临时切换模式后的恢复。"""
    return {bone.name for bone in _selection_bones(owner) if bone.select}


def _restore_bone_selection(owner, selected, active, endpoints=None):
    """恢复选择和活动骨；不改变调用方模式，不把新增辅助骨留为选中。"""
    for bone in _selection_bones(owner):
        bone.select = bone.name in selected
        if owner.mode == 'EDIT':
            bone.select_head, bone.select_tail = (endpoints or {}).get(
                bone.name, (bone.select, bone.select)
            )
    active_bones = owner.data.edit_bones if owner.mode == 'EDIT' else owner.data.bones
    active_bones.active = active_bones.get(active) if active else None


def _new_tail_helper(owner, last_name):
    """仅为缺少末端的叶骨补辅助骨，并恢复调用方的模式与选择。"""
    last = owner.data.bones[last_name]
    if not _needs_tail_helper(last):
        return None
    delta = last.tail_local - last.head_local
    if delta.length < 1e-5 or not all(math.isfinite(v) for v in delta):
        raise ownership.HD2PBError('末骨长度无效，无法自动补尾骨')
    mode = owner.mode
    selected = _selected_bone_names(owner)
    active_bones = owner.data.edit_bones if mode == 'EDIT' else owner.data.bones
    active = active_bones.active.name if active_bones.active else None
    endpoints = {
        bone.name: (bone.select_head, bone.select_tail)
        for bone in owner.data.edit_bones
    } if mode == 'EDIT' else None
    name = None
    try:
        if mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode='EDIT')
        parent = owner.data.edit_bones[last_name]
        helper = owner.data.edit_bones.new(last_name.encode('utf8')[:46].decode('utf8','ignore') + '_PB_TIP')
        name = helper.name
        helper.head = parent.tail
        helper.tail = helper.head + delta.normalized() * .025
        helper.parent = parent
        helper.use_deform = False
        helper['HD2PB_TailHelper'] = True
        helper['HD2BT_CustomBone'] = True
    except Exception:
        if name and owner.data.edit_bones.get(name):
            owner.data.edit_bones.remove(owner.data.edit_bones[name])
        raise
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        if mode != 'OBJECT': bpy.ops.object.mode_set(mode=mode)
        _restore_bone_selection(owner, selected, active, endpoints)
    return name


def _remove_new_tail_helper(owner, name):
    mode = owner.mode
    if mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        helper = owner.data.edit_bones.get(name)
        if helper is not None: owner.data.edit_bones.remove(helper)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        if mode != 'OBJECT': bpy.ops.object.mode_set(mode=mode)


def _execute_chain_creation(operator, context, bones):
    """把已解析的固定锚点 + simulated 路径原子写入 canonical。"""

    owner = context.object
    project = None
    snapshot = None
    helper_name = None
    try:
        project = ownership.ensure_authoring_owner(owner)
        ownership.remember_project_owner(context, owner)
        profile = _active_profile(project)
        if profile is None:
            raise ownership.HD2PBError("请先新增至少一个弹簧参数配置")
        _validate_new_chain(project, bones)
        controllers.preflight_project_rebuild(owner, context_scene=context.scene)
        snapshot = _snapshot_project(project)
        names = [bone.name for bone in bones]
        if project.new_chain_auto_tail:
            if len(names) >= 64 and _needs_tail_helper(bones[-1]):
                raise ownership.HD2PBError('骨链已有 64 个节点，不能再自动补尾；请缩短链或关闭自动补尾')
            helper_name = _new_tail_helper(owner, names[-1])
            if helper_name:
                names.append(helper_name)
                bones = [owner.data.bones[name] for name in names]
        chain = project.chains.add()
        chain.uuid = ownership.new_uuid()
        chain.display_name = _unique_display_name(
            project.chains[:-1], f"{bones[1].name}_弹簧链"
        )
        chain.spring_profile_uuid = profile.uuid
        chain.collision_mask = int(profile.collision_mask)
        for bone in bones:
            node = chain.nodes.add()
            node.uuid = ownership.new_uuid()
            node.bone_name = bone.name
        controllers.rebuild_project_controllers(owner, context_scene=context.scene)
        project.chain_index = len(project.chains) - 1
        _remember_navigation_after_commit(operator, context, owner)
        message = (
            f"已创建物理骨链“{chain.display_name}”：从“{bones[1].name}”到“{bones[-1].name}”"
            f"共 {len(chain.nodes) - 1} 个模拟节点，"
            f"使用“{profile.display_name}”"
        )
        if len(bones) == 2:
            message += "；当前只有一个模拟节点，建议增加末端 end 骨以获得稳定的末段旋转方向"
        if helper_name:
            message += f"；已自动补无权重尾骨“{helper_name}”"
        operator.report({"INFO"}, message)
        return {"FINISHED"}
    except Exception as exc:
        if helper_name is not None:
            _remove_new_tail_helper(owner, helper_name)
        if project is not None and snapshot is not None:
            return _cancel_with_rollback(operator, project, snapshot, exc)
        return _cancel(operator, exc)


class HD2PB_OT_create_chain(Operator):
    """从单根起始模拟骨沿唯一子级自动创建到末端的 SpringBone 物理骨链。"""

    bl_idname = "hd2pb.create_chain"
    bl_label = "从起始骨到末端创建物理骨链"
    bl_description = (
        "在姿态模式（Pose Mode）只选择一根具有父骨的模拟起始骨；插件沿唯一直接子级延伸到末端，"
        "遇到分叉会拒绝而不会猜测，并在写入前检查段长和跨链冲突；目标槽位在封包时确定"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        selected = tuple(getattr(context, "selected_pose_bones", None) or ())
        return (
            context.object is not None
            and context.object.type == "ARMATURE"
            and context.object.mode == "POSE"
            and len(selected) == 1
            and getattr(context, "active_pose_bone", None) == selected[0]
        )

    def execute(self, context):
        try:
            bones = _automatic_bone_path(context, context.object)
        except Exception as exc:
            return _cancel(self, exc)
        return _execute_chain_creation(self, context, bones)


class HD2PB_OT_create_chain_from_selection(Operator):
    """按姿态模式明确选择的一条直接父子路径创建 SpringBone 物理骨链。"""

    bl_idname = "hd2pb.create_chain_from_selection"
    bl_label = "从明确选中路径创建物理骨链（高级）"
    bl_description = (
        "高级建链方式：只使用姿态模式中明确选中的唯一连续父子路径，不自动收集未选子级；"
        "最上方选中骨必须具有父骨"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.object is not None
            and context.object.type == "ARMATURE"
            and context.object.mode == "POSE"
            and bool(getattr(context, "selected_pose_bones", None))
        )

    def execute(self, context):
        try:
            bones = _selected_explicit_bone_path(context, context.object)
        except Exception as exc:
            return _cancel(self, exc)
        return _execute_chain_creation(self, context, bones)


class HD2PB_OT_rename_bone_chain(Operator):
    """从姿态模式起始骨到末端，安全批量重命名一条单路骨链。"""

    bl_idname = "hd2pb.rename_bone_chain"
    bl_label = "批量重命名 Bone Chain"
    bl_description = (
        "从当前唯一选中的起始骨沿唯一子级走到末端并批量改名；会修改真实 Armature 骨名，"
        "同时更新本插件已有 Chain/Collider 引用和 Outliner controller。遇到分叉、重名或过长名称会整次拒绝"
    )
    bl_options = {"REGISTER", "UNDO"}

    base_name: StringProperty(
        name="骨链基础名称",
        default="physbone",
        description="所有目标骨名共用的前缀，例如 Hair_L；不能包含控制字符",
    )
    start_number: IntProperty(
        name="起始编号",
        default=0,
        min=0,
        max=9999,
        description="第一个非末端骨使用的编号",
    )
    digits: IntProperty(
        name="编号位数",
        default=2,
        min=1,
        max=4,
        description="编号左侧补零位数，例如 2 会生成 00、01、02",
    )
    use_end_suffix: BoolProperty(
        name="末端使用 _end",
        default=True,
        description="开启后最后一根骨使用“基础名称_end”，不使用数字编号",
    )

    @classmethod
    def poll(cls, context):
        selected = tuple(getattr(context, "selected_pose_bones", None) or ())
        return (
            context.object is not None
            and context.object.type == "ARMATURE"
            and context.object.mode == "POSE"
            and len(selected) == 1
            and getattr(context, "active_pose_bone", None) == selected[0]
        )

    @staticmethod
    def _validate_target_name(name):
        if not name or any(ord(character) < 32 for character in name):
            raise ownership.HD2PBError("目标骨名不能为空，也不能包含控制字符")
        if len(name.encode("utf-8")) > 63:
            raise ownership.HD2PBError(f"目标骨名“{name}”超过 Blender/HD2PHY1 的 63 字节限制")

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "base_name")
        row = layout.row(align=True)
        row.prop(self, "start_number")
        row.prop(self, "digits")
        layout.prop(self, "use_end_suffix")
        layout.label(text="会修改真实骨骼名称；必要时请同步外部脚本和映射表", icon="ERROR")

    def execute(self, context):
        owner = context.object
        project = owner.data.hd2pb_project
        initialized = ownership.project_initialized(project)
        snapshot = None
        bones = []
        old_names = []
        try:
            if len(ownership.armature_objects(owner.data)) != 1:
                raise ownership.HD2PBError(
                    "当前 Armature 数据被多个对象共享；请先复制为独立物理骨项目后再重命名"
                )
            if initialized:
                ownership.ensure_authoring_owner(owner)
                controllers.preflight_project_rebuild(owner, context_scene=context.scene)
                snapshot = _snapshot_project(project)
            active_pose_bone = getattr(context, "active_pose_bone", None)
            if active_pose_bone is None:
                raise ownership.HD2PBError("请只选择一根活动骨后再批量重命名")
            start = active_pose_bone.bone
            bones = _unique_descendant_path(
                start,
                purpose="骨链重命名",
                branch_help="请从每个分支分别重新选择起始骨",
            )
            base = self.base_name.strip()
            if not base:
                raise ownership.HD2PBError("骨链基础名称不能为空")
            target_names = []
            for index, _bone in enumerate(bones):
                if self.use_end_suffix and index == len(bones) - 1:
                    name = f"{base}_end"
                else:
                    name = f"{base}_{self.start_number + index:0{self.digits}d}"
                self._validate_target_name(name)
                target_names.append(name)
            if len(set(target_names)) != len(target_names):
                raise ownership.HD2PBError("批量生成的目标骨名重复，请调整基础名称或编号设置")
            # Blender 的 RNA Bone 包装对象在不同版本中不保证可哈希；用改名之前
            # 的名称集合判断路径外占用，避免 `set(bones)` 在 Blender 4.0 后台模式
            # 或热重载后出现不可预测的 TypeError。
            path_names = {bone.name for bone in bones}
            outside = {bone.name for bone in owner.data.bones if bone.name not in path_names}
            conflict = sorted(set(target_names).intersection(outside))
            if conflict:
                raise ownership.HD2PBError(
                    f"目标骨名已被路径外骨骼占用：“{conflict[0]}”；为避免静默 .001 后缀，本次未修改"
                )
            old_names = [bone.name for bone in bones]
            mapping = dict(zip(old_names, target_names))
            _rename_bones_exact(owner.data, bones, target_names)
            if initialized:
                for chain in project.chains:
                    for node in chain.nodes:
                        node.bone_name = mapping.get(node.bone_name, node.bone_name)
                for collider in project.colliders:
                    collider.bone_name = mapping.get(collider.bone_name, collider.bone_name)
                    collider.bone_name_b = mapping.get(
                        getattr(collider, "bone_name_b", ""),
                        getattr(collider, "bone_name_b", ""),
                    )
                controllers.rebuild_project_controllers(owner, context_scene=context.scene)
                _remember_navigation_after_commit(self, context, owner)
            self.report(
                {"INFO"},
                f"已从“{old_names[0]}”开始重命名 {len(bones)} 根骨骼；末端为“{target_names[-1]}”",
            )
            return {"FINISHED"}
        except Exception as exc:
            rollback_errors = []
            if bones and old_names:
                try:
                    _rename_bones_exact(owner.data, bones, old_names)
                except Exception as rollback_exc:
                    rollback_errors.append(f"真实骨名：{rollback_exc}")
            if initialized and snapshot is not None:
                try:
                    _restore_project(project, snapshot)
                except Exception as rollback_exc:
                    rollback_errors.append(f"canonical：{rollback_exc}")
                try:
                    controllers.rebuild_project_controllers(
                        owner,
                        context_scene=context.scene,
                    )
                except Exception as rollback_exc:
                    rollback_errors.append(f"编辑对象：{rollback_exc}")
            if rollback_errors:
                return _cancel(
                    self,
                    RuntimeError(
                        "骨链重命名失败且以下回滚步骤不完整："
                        + "；".join(rollback_errors)
                    ),
                )
            return _cancel(self, exc)


class HD2PB_OT_append_tail_helper(Operator):
    """补一个真实无权重骨尾节点，不修改已有骨骼静止矩阵和蒙皮。"""

    bl_idname = "hd2pb.append_tail_helper"
    bl_label = "补真实骨尾模拟点"
    bl_description = "在末骨骨尾创建无权重辅助骨，让末骨按实际长度摆动；已有骨骼和权重不变，可撤销。请在建立链间连接前使用"
    bl_options = {"REGISTER", "UNDO"}
    entity_uuid: StringProperty(name="骨链标识", description="要补末端的正式物理骨链 UUID", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        owner = chain = None
        helper_name = None
        added_node = False
        selected = tuple(context.selected_objects)
        active = context.view_layer.objects.active
        hidden = None
        try:
            owner = ownership.project_owner(context, remember=True)
            project = ownership.ensure_authoring_owner(owner)
            chain = next((c for c in project.chains if c.uuid == self.entity_uuid), None)
            if chain is None or not 2 <= len(chain.nodes) < 64:
                raise ownership.HD2PBError("请选择 2..63 个节点的有效骨链")
            if any(chain.uuid in (l.chain_a_uuid, l.chain_b_uuid) for l in project.chain_links):
                raise ownership.HD2PBError("该链已有链间连接；请先解除连接，补齐各链末端后再连接")
            last = owner.data.bones.get(chain.nodes[-1].bone_name)
            if last is None or last.get("HD2PB_TailHelper", False):
                raise ownership.HD2PBError("末骨缺失或已经是末端辅助点")
            delta = last.tail_local - last.head_local
            if delta.length < 1.e-5 or not all(math.isfinite(v) for v in delta):
                raise ownership.HD2PBError("末骨长度为零或非有限，不能构造实际末端")
            if owner.hide_viewport:
                raise ownership.HD2PBError("请先启用工作骨架的视口显示")
            hidden = owner.hide_get();owner.hide_set(False)
            for obj in selected: obj.select_set(False)
            owner.select_set(True);context.view_layer.objects.active = owner
            last_name = last.name
            bpy.ops.object.mode_set(mode="EDIT")
            parent = owner.data.edit_bones[last_name]
            helper = owner.data.edit_bones.new(last_name.encode("utf-8")[:46].decode("utf-8", "ignore") + "_PB_TIP")
            helper_name = helper.name
            helper.head = parent.tail
            helper.tail = helper.head + delta.normalized() * .025
            helper.parent = parent;helper.use_deform = False
            helper["HD2PB_TailHelper"] = True
            bpy.ops.object.mode_set(mode="OBJECT")
            with preview.suspend_updates():
                previous = chain.nodes[-1]
                values = {k: tuple(getattr(previous, k)) if k in ("limit_axis", "reference_direction", "reference_frame")
                          else getattr(previous, k) for k in _NODE_FIELDS if k not in ("uuid", "bone_name")}
                node = chain.nodes.add();added_node = True
                node.uuid = ownership.new_uuid();node.bone_name = helper_name
                for key, value in values.items(): setattr(node, key, value)
                issues = ownership.canonical_issues(owner)
                if issues: raise ownership.HD2PBError("；".join(issues[:3]))
        except Exception as exc:
            if owner is not None and helper_name is not None:
                if owner.mode != "OBJECT": bpy.ops.object.mode_set(mode="OBJECT")
                if added_node: chain.nodes.remove(len(chain.nodes) - 1)
                bpy.ops.object.mode_set(mode="EDIT")
                helper = owner.data.edit_bones.get(helper_name)
                if helper is not None: owner.data.edit_bones.remove(helper)
                bpy.ops.object.mode_set(mode="OBJECT")
            return _cancel(self, exc)
        finally:
            if owner is not None:
                if owner.mode != "OBJECT": bpy.ops.object.mode_set(mode="OBJECT")
                owner.select_set(False)
                if hidden is not None: owner.hide_set(hidden)
            for obj in selected: obj.select_set(True)
            context.view_layer.objects.active = active
        try:
            controllers.rebuild_project_controllers(owner, context_scene=context.scene)
        except Exception as exc:
            self.report({"WARNING"}, f"末端已补齐，预览需重建：{exc}")
        self.report({"INFO"}, f"已补无权重末端“{helper_name}”；原骨骼、网格和权重未修改")
        return {"FINISHED"}


class HD2PB_OT_remove_chain(Operator):
    """按稳定 UUID 原子删除一条 Chain 及其全部 Node。"""

    bl_idname = "hd2pb.remove_chain"
    bl_label = "删除物理骨链"
    bl_description = (
        "按稳定 UUID 删除指定 Chain、其中全部 Node 和对应编辑投影；不会删除 Armature 骨骼、"
        "权重、碰撞体或弹簧参数配置，操作可一步撤销"
    )
    bl_options = {"REGISTER", "UNDO"}

    entity_uuid: StringProperty(
        name="链 UUID",
        description="要删除的物理骨链稳定 UUID",
        options={"HIDDEN"},
    )

    def execute(self, context):
        project = None
        snapshot = None
        try:
            owner = ownership.project_owner(context, remember=True)
            project = ownership.ensure_authoring_owner(owner)
            index = next(
                (i for i, item in enumerate(project.chains) if item.uuid == self.entity_uuid),
                -1,
            )
            if index < 0:
                raise ownership.HD2PBError("要删除的物理骨链 UUID 不存在")
            references = [
                link
                for link in project.chain_links
                if self.entity_uuid in {link.chain_a_uuid, link.chain_b_uuid}
            ]
            if references:
                raise ownership.HD2PBError(
                    f"该骨链仍被 {len(references)} 个链组连接引用；"
                    "请先删除这些链组连接，再删除骨链"
                )
            controllers.preflight_project_rebuild(owner, context_scene=context.scene)
            snapshot = _snapshot_project(project)
            chain = project.chains[index]
            name = chain.display_name
            removed_uuids = {chain.uuid, *(node.uuid for node in chain.nodes)}
            if project.preview_isolate_entity_uuid in removed_uuids:
                project.preview_isolate_entity_uuid = ""
            project.chains.remove(index)
            controllers.rebuild_project_controllers(owner, context_scene=context.scene)
            project.chain_index = max(0, min(project.chain_index, len(project.chains) - 1))
            _remember_navigation_after_commit(self, context, owner)
            self.report({"INFO"}, f"已删除物理骨链“{name}”及其全部节点")
            return {"FINISHED"}
        except Exception as exc:
            if project is not None and snapshot is not None:
                return _cancel_with_rollback(self, project, snapshot, exc)
            return _cancel(self, exc)


def _selected_chain_groups(context):
    """严格解析物体模式中恰好两个正式 Chain Curve。"""

    selected = tuple(context.selected_objects or ())
    if len(selected) != 2:
        raise ownership.HD2PBError("请恰好选择两个不同骨链的彩色 Chain Curve")
    resolved = []
    for obj in selected:
        if (
            getattr(obj, "hd2pb_preview_kind", properties.PREVIEW_NONE)
            != properties.PREVIEW_CHAIN_CURVE
        ):
            raise ownership.HD2PBError("创建链组连接时只能选择两个正式骨链曲线")
        resolved.append(
            ownership.resolve_controller(
                obj,
                expected_type=properties.ENTITY_CHAIN,
                require_unique=True,
            )
        )
    owner = resolved[0][0]
    project = resolved[0][1]
    if resolved[1][0] != owner or resolved[1][1] != project:
        raise ownership.HD2PBError("两个骨链必须属于同一个物理骨项目")
    preview_index = preview.build_project_index(owner)
    for obj in selected:
        preview.validate_selectable_preview(
            preview_index,
            obj,
            properties.ENTITY_CHAIN,
            properties.PREVIEW_CHAIN_CURVE,
        )
    chains = [resolved[0][2], resolved[1][2]]
    if chains[0].uuid == chains[1].uuid:
        raise ownership.HD2PBError("链组连接必须连接两条不同物理骨链")
    order = {chain.uuid: index for index, chain in enumerate(project.chains)}
    chains.sort(key=lambda chain: order.get(chain.uuid, len(order)))
    return owner, project, chains[0], chains[1]


class HD2PB_OT_add_chain_link(Operator):
    """从物体模式选中的两条正式骨链建立组级共同运动连接。"""

    bl_idname = "hd2pb.add_chain_link"
    bl_label = "从两个已选骨链创建链组连接"
    bl_description = (
        "仅接受恰好两个不同的正式骨链曲线。创建后让两组模拟节点的平均物理偏移共同变化；"
        "主连接允许两条链的模拟节点数不同，不会创建节点距离带"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if context.mode != "OBJECT":
            return False
        try:
            _selected_chain_groups(context)
            return True
        except (ownership.HD2PBError, AttributeError, ReferenceError, RuntimeError, TypeError):
            return False

    def execute(self, context):
        project = None
        snapshot = None
        try:
            owner, project, chain_a, chain_b = _selected_chain_groups(context)
            requested = frozenset((chain_a.uuid, chain_b.uuid))
            for link in project.chain_links:
                if frozenset((link.chain_a_uuid, link.chain_b_uuid)) == requested:
                    raise ownership.HD2PBError("这两个骨链组之间已经存在链组连接")
            controllers.preflight_project_rebuild(owner, context_scene=context.scene)
            snapshot = _snapshot_project(project)
            link = project.chain_links.add()
            link.uuid = ownership.new_uuid()
            link.display_name = _unique_display_name(
                project.chain_links[:-1],
                "链组连接",
            )
            link.chain_a_uuid = chain_a.uuid
            link.chain_b_uuid = chain_b.uuid
            link.coupling = 1.0
            link.max_correction = 0.25
            controllers.rebuild_project_controllers(owner, context_scene=context.scene)
            project.chain_link_index = len(project.chain_links) - 1
            try:
                controllers.select_entity_controller(
                    context,
                    owner,
                    properties.ENTITY_CHAIN_LINK,
                    link.uuid,
                )
            except Exception as selection_exc:
                print(f"链组连接已创建，但自动选择失败：{selection_exc}")
            _remember_navigation_after_commit(self, context, owner)
            self.report(
                {"INFO"},
                f"已创建“{link.display_name}”：{chain_a.display_name} ↔ {chain_b.display_name}；"
                "主连接按组级平均物理偏移工作",
            )
            return {"FINISHED"}
        except Exception as exc:
            if project is not None and snapshot is not None:
                return _cancel_with_rollback(self, project, snapshot, exc)
            return _cancel(self, exc)


class HD2PB_OT_build_link_collisions(Operator):
    """为活动主 ChainLink 原子生成完整的同序动态胶囊子表。"""

    bl_idname = "hd2pb.build_link_collisions"
    bl_label = "创建或重建链间碰撞体"
    bl_description = (
        "仅在选中主链组连接曲线时可用。两条骨链的模拟节点数必须一致；"
        "成功后从 local Joint 1 开始完整替换全部子项，不等长时保留原有子项不变"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if context.mode != "OBJECT":
            return False
        obj = context.object
        if (
            obj is None
            or getattr(obj, "hd2pb_preview_kind", properties.PREVIEW_NONE)
            != properties.PREVIEW_CHAIN_LINK
        ):
            return False
        try:
            ownership.resolve_controller(
                obj,
                expected_type=properties.ENTITY_CHAIN_LINK,
                require_unique=True,
            )
            return True
        except (ownership.HD2PBError, AttributeError, ReferenceError, RuntimeError, TypeError):
            return False

    def execute(self, context):
        project = None
        snapshot = None
        try:
            owner, project, link, _parent = ownership.resolve_controller(
                context.object,
                expected_type=properties.ENTITY_CHAIN_LINK,
                require_unique=True,
            )
            chains = {chain.uuid: chain for chain in project.chains}
            chain_a = chains.get(link.chain_a_uuid)
            chain_b = chains.get(link.chain_b_uuid)
            if chain_a is None or chain_b is None:
                raise ownership.HD2PBError("当前链组连接引用的骨链已经不存在")
            simulated_a = len(chain_a.nodes) - 1
            simulated_b = len(chain_b.nodes) - 1
            if simulated_a != simulated_b:
                raise ownership.HD2PBError(
                    f"创建链间碰撞体要求两条骨链的模拟节点数一致；当前为 "
                    f"{simulated_a} 与 {simulated_b}。原有子项保持不变"
                )
            if simulated_a < 1:
                raise ownership.HD2PBError("每条骨链至少需要一个模拟节点")
            prepared = []
            for local_index in range(1, len(chain_a.nodes)):
                node_a = chain_a.nodes[local_index]
                node_b = chain_b.nodes[local_index]
                radius = max(min(float(node_a.radius), float(node_b.radius)), 1.0e-6)
                prepared.append((local_index, radius))

            controllers.preflight_project_rebuild(owner, context_scene=context.scene)
            snapshot = _snapshot_project(project)
            link.collisions.clear()
            for local_index, radius in prepared:
                collision = link.collisions.add()
                collision.uuid = ownership.new_uuid()
                collision.local_joint_index = local_index
                collision.radius = radius
                collision.group_mask = 1
                collision.collision_mask = 0x7FFFFFFF
            controllers.rebuild_project_controllers(owner, context_scene=context.scene)
            try:
                controllers.select_entity_controller(
                    context,
                    owner,
                    properties.ENTITY_CHAIN_LINK,
                    link.uuid,
                )
            except Exception as selection_exc:
                print(f"链间碰撞体已重建，但主连接自动选择失败：{selection_exc}")
            _remember_navigation_after_commit(self, context, owner)
            self.report(
                {"INFO"},
                f"已从 local Joint 1 开始完整生成 {len(prepared)} 个链间碰撞体",
            )
            return {"FINISHED"}
        except Exception as exc:
            if project is not None and snapshot is not None:
                return _cancel_with_rollback(self, project, snapshot, exc)
            return _cancel(self, exc)


class HD2PB_OT_remove_chain_link(Operator):
    """按稳定 UUID 删除主 ChainLink 及其全部 LinkCollision child。"""

    bl_idname = "hd2pb.remove_chain_link"
    bl_label = "删除链组连接"
    bl_description = "删除当前链组连接及其全部链间碰撞体；不会删除任何骨链、节点或骨骼"
    bl_options = {"REGISTER", "UNDO"}

    entity_uuid: StringProperty(
        name="链组连接 UUID",
        description="要删除的 ChainLink 稳定 UUID",
        options={"HIDDEN"},
    )

    def execute(self, context):
        project = None
        snapshot = None
        try:
            owner = ownership.project_owner(context, remember=True)
            project = ownership.ensure_authoring_owner(owner)
            index = next(
                (i for i, link in enumerate(project.chain_links) if link.uuid == self.entity_uuid),
                -1,
            )
            if index < 0:
                raise ownership.HD2PBError("要删除的链组连接 UUID 不存在")
            controllers.preflight_project_rebuild(owner, context_scene=context.scene)
            snapshot = _snapshot_project(project)
            removed = project.chain_links[index]
            removed_uuids = {removed.uuid, *(item.uuid for item in removed.collisions)}
            if project.preview_isolate_entity_uuid in removed_uuids:
                project.preview_isolate_entity_uuid = ""
            name = removed.display_name
            project.chain_links.remove(index)
            controllers.rebuild_project_controllers(owner, context_scene=context.scene)
            project.chain_link_index = max(
                0,
                min(project.chain_link_index, len(project.chain_links) - 1),
            )
            _remember_navigation_after_commit(self, context, owner)
            self.report({"INFO"}, f"已删除链组连接“{name}”及其全部链间碰撞体")
            return {"FINISHED"}
        except Exception as exc:
            if project is not None and snapshot is not None:
                return _cancel_with_rollback(self, project, snapshot, exc)
            return _cancel(self, exc)


def _execute_add_collider(
    operator,
    context,
    owner,
    bone_name,
    kind,
    *,
    bone_name_b="",
    dynamic=False,
):
    """用显式 attachment 原子新增一项 Collider canonical 与 controller。"""

    project = None
    snapshot = None
    try:
        project = ownership.ensure_authoring_owner(owner)
        ownership.remember_project_owner(context, owner)
        bone = owner.data.bones.get(bone_name) if bone_name else None
        bone_b = owner.data.bones.get(bone_name_b) if bone_name_b else None
        if bone_name and bone is None:
            raise ownership.HD2PBError(f"骨架中不存在碰撞体吸附骨“{bone_name}”")
        if bone_name_b and bone_b is None:
            raise ownership.HD2PBError(f"骨架中不存在胶囊终点骨“{bone_name_b}”")
        if bone_name_b and kind != "CAPSULE":
            raise ownership.HD2PBError("只有胶囊碰撞体可以使用第二端吸附骨")
        if dynamic and bone_name_b:
            raise ownership.HD2PBError("动态碰撞体暂不支持同时吸附两根骨骼")
        # Bone ownership/existence is authoritative here; palette slots do not
        # exist yet in the shared authoring workflow. Export still checks them.
        simulated = {
            node.bone_name
            for chain in project.chains
            for index, node in enumerate(chain.nodes)
            if index > 0
        }
        if dynamic and not bone_name:
            raise ownership.HD2PBError("动态碰撞体必须吸附一个模拟节点")
        if dynamic and bone_name not in simulated:
            raise ownership.HD2PBError(
                f"动态碰撞体只能吸附本项目唯一的模拟节点；“{bone_name}”不属于模拟节点"
            )
        if dynamic and kind == "PLANE":
            raise ownership.HD2PBError("动态碰撞体只支持球形或胶囊，不能创建无限平面")
        if not dynamic and (bone_name in simulated or bone_name_b in simulated):
            raise ownership.HD2PBError(
                f"静态碰撞体不能跟随模拟骨骼“{bone_name or bone_name_b}”；"
                "请选择固定骨，或使用“从活动模拟骨创建动态碰撞体”"
            )
        names = {
            "SPHERE": "球形碰撞体",
            "CAPSULE": "胶囊碰撞体",
            "PLANE": "平面碰撞体",
        }
        if kind not in names:
            raise ownership.HD2PBError("不支持的碰撞体形状")
        controllers.preflight_project_rebuild(owner, context_scene=context.scene)
        snapshot = _snapshot_project(project)
        collider = project.colliders.add()
        collider.uuid = ownership.new_uuid()
        endpoint_label = bone_name if not bone_name_b else f"{bone_name}↔{bone_name_b}"
        base_name = f"{names[kind]}_{len(project.colliders):03d}_{endpoint_label or 'Unit根'}"
        collider.display_name = _unique_display_name(project.colliders[:-1], base_name)
        collider.kind = kind
        collider.attachment_mode = "DYNAMIC" if dynamic else "STATIC"
        collider.bone_name = bone_name
        collider.bone_name_b = bone_name_b if kind == "CAPSULE" else ""
        if kind == "SPHERE":
            collider.b = (0.0, 0.0, 1.0)
        elif kind == "CAPSULE":
            if bone_b is not None and bone is not None:
                # 将第二端 Rest 头转换到首端骨骼局部空间；预览另外保留第二个
                # Bone Hook，使姿态变化时两端仍分别跟随各自骨骼。
                local_end = bone.matrix_local.inverted_safe() @ bone_b.matrix_local.translation
                collider.b = tuple(float(value) for value in local_end)
            else:
                # 单骨胶囊的两个端点重合，视觉上是球体但语义仍保留 Capsule；
                # Unit 根没有骨骼吸附，继续使用可读的局部 Y 轴默认长度。
                collider.b = (
                    tuple(float(value) for value in collider.a)
                    if bone is not None
                    else (0.0, 0.2, 0.0)
                )
        else:
            collider.b = (0.0, 0.0, 1.0)
            collider.radius = 0.0
        controllers.rebuild_project_controllers(owner, context_scene=context.scene)
        project.collider_index = len(project.colliders) - 1
        _remember_navigation_after_commit(operator, context, owner)
        attachment = (
            f"骨骼“{bone_name}”与“{bone_name_b}”"
            if bone_name_b
            else (f"骨骼“{bone_name}”" if bone_name else "Unit 根")
        )
        mode_label = "动态" if dynamic else "静态"
        operator.report(
            {"INFO"},
            f"已新增{mode_label}{names[kind]}“{collider.display_name}”，吸附到{attachment}",
        )
        return {"FINISHED"}
    except Exception as exc:
        if project is not None and snapshot is not None:
            return _cancel_with_rollback(operator, project, snapshot, exc)
        return _cancel(operator, exc)


class HD2PB_OT_add_collider(Operator):
    """在物体模式为 Unit 根新增碰撞体参数。"""

    bl_idname = "hd2pb.add_collider"
    bl_label = "新增 Unit 根碰撞体"
    bl_description = (
        "在物体模式新增一个明确绑定 Unit 根的碰撞体；需要吸附骨骼时请切换到姿态模式，"
        "只选择一根固定骨后使用快捷创建。生成的彩色 Curve + Geometry Nodes 实体可选择并打开"
        "碰撞体属性；默认锁定视图变换，实验设置开启后才允许直接试摆"
    )
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(
        name="碰撞体形状",
        items=properties.COLLIDER_KIND_ITEMS,
        default="SPHERE",
        description="要新增的碰撞体基础形状",
    )

    @classmethod
    def description(cls, _context, values):
        details = {
            "SPHERE": "新增球形碰撞体；由局部中心 A 和半径定义，参数变化会自动刷新彩色实体预览",
            "CAPSULE": "新增胶囊碰撞体；由局部起点 A、终点 B 和半径定义，参数变化会自动刷新彩色实体预览",
            "PLANE": "新增无限平面碰撞体；由局部点 A 和法线 B 定义，参数变化会自动刷新有限方片和法线箭头预览",
        }
        return details.get(values.kind, cls.bl_description)

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and ownership.project_owner(context) is not None

    def execute(self, context):
        owner = ownership.project_owner(context, remember=True)
        return _execute_add_collider(self, context, owner, "", self.kind)


class HD2PB_OT_create_collider_from_bone(Operator):
    """在姿态模式从一根或两根固定骨快速创建碰撞体。"""

    bl_idname = "hd2pb.create_collider_from_bone"
    bl_label = "从活动骨创建碰撞体"
    bl_description = (
        "在姿态模式把碰撞体吸附到当前选中的固定骨；单选创建球、胶囊或平面，"
        "同时选中两根固定骨时自动创建胶囊：先选骨作为起点，最后激活的骨作为终点。"
        "超过两根、模拟骨或无活动骨时拒绝操作"
    )
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(
        name="碰撞体形状",
        items=properties.COLLIDER_KIND_ITEMS,
        default="SPHERE",
        description="要从活动固定骨快速创建的碰撞体基础形状",
    )

    @classmethod
    def poll(cls, context):
        selected = tuple(getattr(context, "selected_pose_bones", None) or ())
        return (
            context.object is not None
            and context.object.type == "ARMATURE"
            and context.object.mode == "POSE"
            and 1 <= len(selected) <= 2
            and getattr(context, "active_pose_bone", None) in selected
        )

    def execute(self, context):
        active = getattr(context, "active_pose_bone", None)
        selected = tuple(getattr(context, "selected_pose_bones", None) or ())
        if active is None or not 1 <= len(selected) <= 2:
            return _cancel(self, ownership.HD2PBError("请选中一或两根固定骨，并确保其中一根为活动骨"))
        other = next((bone for bone in selected if bone != active), None)
        kind = "CAPSULE" if other is not None else self.kind
        # Blender 对恰好两根选骨只可靠保留“最后选择者是活动骨”的信息；
        # 因此非活动骨是先选起点，活动骨是最后选择的终点。
        start = other if other is not None else active
        end = active if other is not None else None
        return _execute_add_collider(
            self,
            context,
            context.object,
            start.name,
            kind,
            bone_name_b=end.name if end is not None else "",
        )


class HD2PB_OT_create_dynamic_collider_from_bone(Operator):
    """从姿态模式唯一活动模拟骨创建动态 Sphere/Capsule。"""

    bl_idname = "hd2pb.create_dynamic_collider_from_bone"
    bl_label = "从活动模拟骨创建动态碰撞体"
    bl_description = (
        "仅接受本项目唯一模拟节点，并创建随当前模拟姿态重建的动态球形或胶囊碰撞体；"
        "动态无限平面、固定驱动骨和未纳入物理链的骨骼都会被拒绝"
    )
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(
        name="动态碰撞体形状",
        items=(
            ("SPHERE", "动态球形", "从当前模拟节点创建动态球形碰撞体"),
            ("CAPSULE", "动态胶囊", "从当前模拟节点创建动态胶囊碰撞体"),
        ),
        default="SPHERE",
        description="动态碰撞体只支持球形和胶囊",
    )

    @classmethod
    def poll(cls, context):
        selected = tuple(getattr(context, "selected_pose_bones", None) or ())
        return (
            context.object is not None
            and context.object.type == "ARMATURE"
            and context.object.mode == "POSE"
            and len(selected) == 1
            and getattr(context, "active_pose_bone", None) == selected[0]
        )

    def execute(self, context):
        active = getattr(context, "active_pose_bone", None)
        if active is None:
            return _cancel(self, ownership.HD2PBError("请只选择一根活动模拟骨"))
        return _execute_add_collider(
            self,
            context,
            context.object,
            active.name,
            self.kind,
            dynamic=True,
        )


class HD2PB_OT_remove_collider(Operator):
    """按稳定 UUID 删除碰撞体 canonical 数据与对应投影。"""

    bl_idname = "hd2pb.remove_collider"
    bl_label = "删除碰撞体"
    bl_description = (
        "按稳定 UUID 删除指定碰撞体参数和对应空对象（Empty）；不会删除骨骼、网格、物理骨链或其他"
        "用户对象，操作可一步撤销"
    )
    bl_options = {"REGISTER", "UNDO"}

    entity_uuid: StringProperty(
        name="碰撞体 UUID",
        description="要删除的碰撞体稳定 UUID",
        options={"HIDDEN"},
    )

    def execute(self, context):
        project = None
        snapshot = None
        try:
            owner = ownership.project_owner(context, remember=True)
            project = ownership.ensure_authoring_owner(owner)
            index = next(
                (i for i, item in enumerate(project.colliders) if item.uuid == self.entity_uuid),
                -1,
            )
            if index < 0:
                raise ownership.HD2PBError("要删除的碰撞体 UUID 不存在")
            controllers.preflight_project_rebuild(owner, context_scene=context.scene)
            snapshot = _snapshot_project(project)
            name = project.colliders[index].display_name
            if project.preview_isolate_entity_uuid == project.colliders[index].uuid:
                project.preview_isolate_entity_uuid = ""
            project.colliders.remove(index)
            controllers.rebuild_project_controllers(owner, context_scene=context.scene)
            project.collider_index = max(
                0,
                min(project.collider_index, len(project.colliders) - 1),
            )
            _remember_navigation_after_commit(self, context, owner)
            self.report({"INFO"}, f"已删除碰撞体“{name}”")
            return {"FINISHED"}
        except Exception as exc:
            if project is not None and snapshot is not None:
                return _cancel_with_rollback(self, project, snapshot, exc)
            return _cancel(self, exc)


class HD2PB_OT_assign_profile(Operator):
    """用稳定 UUID 给指定 Chain 更换弹簧参数配置。"""

    bl_idname = "hd2pb.assign_profile"
    bl_label = "给物理骨链选择参数配置"
    bl_description = (
        "把指定 Chain 的弹簧参数引用切换到另一组 Profile UUID；不会复制参数，也不会修改其他链"
    )
    bl_options = {"REGISTER", "UNDO"}

    chain_uuid: StringProperty(
        name="链 UUID",
        description="需要更换参数配置的 Chain UUID",
        options={"HIDDEN"},
    )
    profile_uuid: StringProperty(
        name="配置 UUID",
        description="要分配给 Chain 的弹簧参数配置 UUID",
        options={"HIDDEN"},
    )

    def execute(self, context):
        try:
            owner = ownership.project_owner(context, remember=True)
            project = ownership.ensure_authoring_owner(owner)
            chain = next((item for item in project.chains if item.uuid == self.chain_uuid), None)
            profile = next(
                (item for item in project.profiles if item.uuid == self.profile_uuid), None
            )
            if chain is None or profile is None:
                raise ownership.HD2PBError("指定的物理骨链或弹簧参数配置 UUID 不存在")
            chain.spring_profile_uuid = profile.uuid
            self.report(
                {"INFO"},
                f"物理骨链“{chain.display_name}”现在使用“{profile.display_name}”",
            )
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


class HD2PB_OT_set_collider_bone(Operator):
    """通过专用对话框原子修改 Collider canonical 与 controller 的吸附骨。"""

    bl_idname = "hd2pb.set_collider_bone"
    bl_label = "修改碰撞体吸附骨骼"
    bl_description = (
        "修改碰撞体吸附骨并同步唯一编辑投影。静态模式只允许 Unit 根或非模拟骨；"
        "动态模式必须选择本项目唯一模拟节点，任何失败都不会留下部分修改"
    )
    bl_options = {"REGISTER", "UNDO"}

    collider_uuid: StringProperty(
        name="碰撞体 UUID",
        description="需要修改吸附骨骼的碰撞体稳定 UUID",
        options={"HIDDEN"},
    )
    bone_name: StringProperty(
        name="吸附骨骼",
        description="静态模式可清空为 Unit 根；动态模式必须填写本项目模拟节点骨名",
    )

    def invoke(self, context, _event):
        try:
            _owner, _project, collider, _parent = ownership.resolve_controller(
                context.object,
                properties.ENTITY_COLLIDER,
            )
            self.collider_uuid = collider.uuid
            self.bone_name = collider.bone_name
            return context.window_manager.invoke_props_dialog(self, width=440)
        except Exception as exc:
            return _cancel(self, exc)

    def draw(self, context):
        layout = self.layout
        owner = ownership.active_owner(context)
        if owner is None:
            layout.label(text="所属骨架（Armature）已丢失", icon="ERROR")
            return
        layout.prop_search(self, "bone_name", owner.data, "bones", text="吸附骨骼")
        layout.label(text="静态模式：清空表示 Unit 根；不能选择模拟骨", icon="INFO")
        layout.label(text="动态模式：必须选择模拟骨；只支持球形或胶囊")

    def execute(self, context):
        try:
            owner = ownership.project_owner(context, remember=True)
            project = ownership.ensure_authoring_owner(owner)
            collider = next(
                (item for item in project.colliders if item.uuid == self.collider_uuid),
                None,
            )
            if collider is None:
                raise ownership.HD2PBError("要修改的碰撞体 UUID 不存在")
            bone_name = self.bone_name.strip()
            if bone_name and owner.data.bones.get(bone_name) is None:
                raise ownership.HD2PBError(f"骨架中不存在吸附骨骼“{bone_name}”")
            simulated = {
                node.bone_name
                for chain in project.chains
                for index, node in enumerate(chain.nodes)
                if index > 0
            }
            dynamic = collider.attachment_mode == "DYNAMIC"
            if bone_name == "" and getattr(collider, "bone_name_b", ""):
                raise ownership.HD2PBError("双端胶囊不能仅清空首端吸附骨；请先在参数中改为单端胶囊")
            if dynamic and not bone_name:
                raise ownership.HD2PBError("动态碰撞体必须吸附一个模拟节点，不能改为 Unit 根")
            if dynamic and bone_name not in simulated:
                raise ownership.HD2PBError(
                    f"动态碰撞体只能吸附本项目唯一模拟节点；“{bone_name}”不是模拟节点"
                )
            if dynamic and collider.kind == "PLANE":
                raise ownership.HD2PBError("动态碰撞体不能使用无限平面")
            if not dynamic and bone_name in simulated:
                raise ownership.HD2PBError(
                    f"静态碰撞体不能跟随模拟骨骼“{bone_name}”"
                )

            previous = collider.bone_name
            collider.bone_name = bone_name
            previous_b = tuple(collider.b)
            try:
                controllers.sync_collider_controller(owner, collider, bone_name)
                if collider.kind == "CAPSULE" and getattr(collider, "bone_name_b", "") and bone_name:
                    first_bone = owner.data.bones.get(bone_name)
                    second_bone = owner.data.bones.get(collider.bone_name_b)
                    if first_bone is not None and second_bone is not None:
                        endpoint = first_bone.matrix_local.inverted_safe() @ second_bone.matrix_local.translation
                        collider.b = tuple(float(value) for value in endpoint)
            except Exception:
                collider.bone_name = previous
                collider.b = previous_b
                raise
            attachment = f"骨骼“{bone_name}”" if bone_name else "Unit 根"
            self.report({"INFO"}, f"已把碰撞体“{collider.display_name}”吸附到{attachment}")
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


class HD2PB_OT_rebuild_controllers(Operator):
    """从 canonical 数据原子重建本项目全部 controller 与实体 preview。"""

    bl_idname = "hd2pb.rebuild_controllers"
    bl_label = "修复编辑投影"
    bl_description = (
        "按所属 Armature 和项目 UUID 安全替换本项目已有 controller、彩色 preview 与管理集合。"
        "只用于修复手动删除、Shift-D 重复或损坏；日常参数调整会实时刷新，不需要手动重建"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            owner = ownership.project_owner(context, remember=True)
            ownership.ensure_authoring_owner(owner)
            result = controllers.rebuild_project_controllers(
                owner, context_scene=context.scene
            )
            _remember_navigation_after_commit(self, context, owner)
            self.report(
                {"INFO"},
                f"已修复 {result['controllers']} 个 controller、{result['previews']} 个实体预览："
                f"{result['chains']} 条链、{result['nodes']} 个节点、{result['colliders']} 个碰撞体",
            )
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


def _publish_validation(context, owner, rows, status):
    from . import diagnostics
    diagnostics.publish(owner, rows, status=status)
    for row in rows:
        print('HD2PB 检查：', row if isinstance(row, str) else row['message'])
    if not bpy.app.background:
        if status in {'PASS', '结构检查通过'}:
            # Restore the compact result menu on success. Keep the full snapshot
            # available for manual review without opening the large dialog.
            messages = tuple(row if isinstance(row, str) else row['message'] for row in rows)
            def draw_report(menu, _context):
                for message in messages:
                    menu.layout.label(text=message)
            context.window_manager.popup_menu(draw_report, title='物理骨检查结果', icon='CHECKMARK')
        else:
            bpy.ops.hd2pb.show_validation_report('INVOKE_DEFAULT')


class HD2PB_OT_show_validation_report(Operator):
    """分页显示完整检查快照，不修改制作数据。"""
    bl_idname = 'hd2pb.show_validation_report'
    bl_label = '完整物理检查列表'
    bl_description = '查看本次会话的完整检查记录；参数更改后需要重新检查'
    page: IntProperty(name='页', description='完整检查记录的当前页码', default=0, min=0, options={'SKIP_SAVE'})
    previous_page: BoolProperty(name='上一页', default=False, options={'SKIP_SAVE'})
    next_page: BoolProperty(name='下一页', default=False, options={'SKIP_SAVE'})

    def invoke(self, context, _event):
        from . import diagnostics
        if not diagnostics.present():
            self.report({'WARNING'}, '还没有检查记录，请先检查当前项目')
            return {'CANCELLED'}
        self.previous_page = self.next_page = False
        self.check(context)
        return context.window_manager.invoke_props_dialog(self, width=880)

    def check(self, _context):
        from . import diagnostics
        saved = diagnostics.report()
        pages = max(1, (len(saved['entries']) + 5) // 6) if saved else 1
        previous, following = self.previous_page, self.next_page
        old_page = self.page
        # Property buttons update this dialog's operator instance. Invoking the
        # report operator here would stack another modal dialog on every click.
        self.previous_page = self.next_page = False
        page = min(max(0, old_page), pages - 1)
        self.page = min(max(0, page + int(following) - int(previous)), pages - 1)
        return previous or following or self.page != old_page

    def draw(self, context):
        import textwrap
        from . import diagnostics
        saved = diagnostics.report()
        if not saved:
            self.layout.label(text='检查记录已清除，请重新检查')
            return
        rows = saved['entries']
        size = 6
        pages = max(1, (len(rows) + size - 1) // size)
        page = min(self.page, pages - 1)
        self.layout.label(text=f"{saved['owner_name']} — {saved['status']} — {len(rows)} 条记录")
        self.layout.label(text='此为当前检查快照；修改后请重新检查。定位不显示隐藏对象。', icon='INFO')
        try:
            diagnostics.matching_owner(context, saved)
            can_locate = True
        except Exception:
            can_locate = False
            self.layout.label(text='当前不是原检查项目，定位已禁用', icon='ERROR')
        for index in range(page * size, min((page + 1) * size, len(rows))):
            row = rows[index]
            box = self.layout.box()
            for line in str(row['message']).splitlines() or ['']:
                for wrapped in textwrap.wrap(line, width=56, replace_whitespace=False) or ['']:
                    box.label(text=wrapped)
            if row['entity_uuid']:
                buttons = box.row()
                buttons.enabled = can_locate
                op = buttons.operator('hd2pb.locate_validation_issue', text='定位可见实体', icon='VIEWZOOM')
                op.index = index
        nav = self.layout.row(align=True)
        left = nav.row(); left.enabled = page > 0
        left.prop(self, 'previous_page', text='上一页', toggle=True)
        nav.label(text=f'{page + 1} / {pages}')
        right = nav.row(); right.enabled = page + 1 < pages
        right.prop(self, 'next_page', text='下一页', toggle=True)

    def execute(self, context):
        return {'FINISHED'}


class HD2PB_OT_locate_validation_issue(Operator):
    """仅定位原项目中已经可见的唯一编辑实体。"""
    bl_idname = 'hd2pb.locate_validation_issue'
    bl_label = '定位检查实体'
    bl_description = '仅选择原项目中可见的唯一对象'
    index: IntProperty(name='检查条目', description='完整检查记录中的条目编号', default=0, min=0, options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        from . import diagnostics
        try:
            target = diagnostics.locate(context, self.index)
            self.report({'INFO'}, f'已选择“{target.name}”')
            return {'FINISHED'}
        except Exception as exc:
            return _cancel(self, exc)


class HD2PB_OT_validate_project(Operator):
    """只读结构检查及同求解器短测。"""

    bl_idname = "hd2pb.validate_project"
    bl_label = "检查物理骨项目"
    bl_description = (
        "检查结构与衣物姿态规则；有骨链时同求解器测试静止和小幅运动各 120 帧，纯姿态项目跳过弹簧短测；"
        "目标 Unit/槽位在封包时另验"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        global _LAST_SOLVER_CHECK
        _LAST_SOLVER_CHECK = None
        try:
            owner = ownership.project_owner(context, remember=True)
            ownership.ensure_authoring_owner(owner)
            project = owner.data.hd2pb_project
            enabled_pose_count = sum(rule.enabled for rule in project.pose_drivers)
            issues = ownership.project_issues(
                owner,
                include_controllers=True,
                include_previews=True,
                require_chains=not enabled_pose_count,
            )
            if project.pose_drivers:
                # Use the immutable SDK author contract: manual test controls
                # never mask invalid source bindings or stale calibration.
                from ..core.pose_driver import PoseDriverError
                try:
                    adapter_v1.clothing_pose_document(owner)
                except PoseDriverError as exc:
                    issues.append(str(exc))
            if issues:
                _publish_validation(context, owner,
                    [dict(message=message, severity='ERROR') for message in issues], '结构检查失败')
                self.report({'WARNING'}, f'检查发现 {len(issues)} 项结构／姿态问题；完整检查列表已保留，未运行求解短测')
                return {'CANCELLED'}
            if not project.chains:
                _publish_validation(context, owner, ['衣物姿态制作检查通过；无物理链，跳过弹簧短测。',
                    '最终骨架绑定由 AQSDK 封包校验；修改后请重新检查。'], '结构检查通过')
                self.report({'INFO'}, f'衣物姿态制作检查通过（启用 {enabled_pose_count} 条）；'
                            '无物理链，已跳过弹簧短测；最终骨架绑定由 AQSDK 封包校验')
                return {'FINISHED'}
            try:
                summary = adapter_v1.validate_authoring_object(owner)
            except Exception as exc:
                _publish_validation(context, owner, [dict(message=str(exc), severity='ERROR')], '格式检查失败')
                raise
            from ..core import solver_check
            from .motion_preview import _document
            try:
                document, _scale = _document(owner)
                result = solver_check.check_document(document)
            except Exception as exc:
                result = dict(status='INCOMPLETE', cases=[], reason=str(exc))
            _LAST_SOLVER_CHECK = result
            lines = solver_check.summary_lines(result)
            records = [dict(message=line, severity='INFO') for line in lines]
            print('HD2PB 物理骨检查：', owner.name, summary)
            for line in lines:
                print(line)
            for row in result['cases']:
                for message in row['messages']:
                    print('Native solver:', row['name'], message)
                    records.append(dict(message=f"{row['name']}：{message}", severity='WARNING'))
                if row.get('diagnostic_error'):
                    print('Native diagnostic error:', row['diagnostic_error'])
                    records.append(dict(message=f"{row['name']} 诊断读取失败：{row['diagnostic_error']}", severity='WARNING'))
                for location in (row.get('diagnostics') or {}).get('locations', []):
                    records.append(dict(message=f"{row['name']}／{location['kind']}：{location['where']}；残差 {location['residual_m'] * 1000:.3f} mm",
                                        where=location['where'], severity='WARNING'))
            locations = [loc for row in result['cases']
                         for loc in (row.get('diagnostics') or {}).get('locations', [])]
            brief = lines[0] + ('；'+locations[0]['where'] if locations else '；'.join(lines[1:3]))
            self.report({'INFO'} if result['status'] == 'PASS' else {'WARNING'}, brief)
            _publish_validation(context, owner, records, result['status'])
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


def _parameter_fields(entity_type):
    fields = {
        properties.ENTITY_PROFILE: ("stiffness", "damping", "gravity", "wind", "collision_mask"),
        properties.ENTITY_NODE: (
            "override_collision_filter",
            "group_mask",
            "collision_mask",
            "radius",
            "limit_kind",
            "limit_angle",
            "limit_angle2",
            "limit_axis",
            "stiffness_scale",
            "damping_scale",
            "reference_mode",
            "reference_direction",
            "reference_frame",
        ),
        properties.ENTITY_COLLIDER: ("kind", "group_mask", "collision_mask", "inside", "a", "b", "radius"),
        properties.ENTITY_CHAIN_LINK: ("coupling", "max_correction"),
    }
    return fields.get(entity_type)


def _parameter_payload(entity_type, entity):
    fields = _parameter_fields(entity_type)
    if fields is None:
        raise ownership.HD2PBError("当前实体不支持复制或保存物理参数预设")
    return {
        name: list(value) if not isinstance(value, (str, int, float, bool)) else value
        for name in fields
        for value in (getattr(entity, name),)
    }


def _apply_parameter_payload(entity_type, entity, parameters):
    normalized = preset_v1.validate_parameters(entity_type, parameters)
    for name, value in normalized.items():
        setattr(entity, name, value)
    if entity_type == properties.ENTITY_NODE and 'reference_mode' not in normalized:
        if entity.limit_kind == 'NONE':
            entity.reference_mode = 'ANIMATED_SEGMENT'
        elif entity.reference_mode == 'ANIMATED_SEGMENT':
            _auto_align_new_limit(entity)


def _current_parameter_entity(context):
    try:
        owner, project, entity, parent = ownership.resolve_controller(context.object)
    except ownership.HD2PBError as exc:
        raise ownership.HD2PBError(
            "请先选择 Profile、Node、Collider 或 ChainLink 的可选择 controller"
        ) from exc
    entity_type = context.object.hd2pb_entity_type
    if _parameter_fields(entity_type) is None:
        raise ownership.HD2PBError(
            "请选择具体 Profile、Node、Collider 或 ChainLink，而不是项目或骨链"
        )
    return owner, project, entity_type, entity, parent


def _preset_directory(entity_type=None):
    override = os.environ.get("HD2PB_PRESET_DIR", "").strip()
    if override:
        root = Path(override).expanduser()
    else:
        config = Path(bpy.utils.user_resource("CONFIG"))
        root = config / "hd2_physbone" / "presets"
    return preset_v1.typed_preset_directory(root, entity_type) if entity_type else root


def typed_preset_items(entity_type, *, include_default=False):
    """保留动态枚举字符串，并使用 UI float32 能精确表示的会话内小整数。

    不用 CRC 作枚举值：例如 hair 的旧值 485974298 会被菜单舍入为
    485974304，之后 RNA 找不到对应项。名称不变时数值不变，删除也不复用。
    """
    rows = ([("__DEFAULT__", "默认弹簧参数", "不使用预设，以默认值新建", 0)] if include_default
            else [])
    try:
        entries = preset_v1.list_presets(_preset_directory(entity_type), entity_type=entity_type)
        numbers = _TYPED_PRESET_ENUM_NUMBERS.setdefault(entity_type, {})
        for entry in entries:
            if entry.name not in numbers:
                if len(numbers) >= 16777214:
                    raise ValueError("会话预设数量超出菜单安全范围，请重启 Blender")
                numbers[entry.name] = len(numbers) + 1
            number = numbers[entry.name]
            rows.append((entry.name, entry.name, str(entry.path), number))
    except Exception as exc:
        rows = rows[:1] if include_default else []
        rows.append(("__ERROR__", "预设列表读取失败", str(exc), 16777215))
    if not rows:
        rows = [("__NONE__", "没有此类型预设", "保存参数或导入分享 JSON", 0)]
    key = (entity_type, include_default, tuple(rows))
    return _TYPED_PRESET_ENUM_CACHE.setdefault(key, rows)


def _select_library_preset(context, entity_type, name):
    """文件操作后刷新会话选择；用户库跨项目共用，不写 Armature 数据。"""
    field = {"PROFILE": "preset_profile", "NODE": "preset_node", "COLLIDER": "preset_collider",
             "CHAIN_LINK": "preset_chain_link"}[entity_type]
    rows = typed_preset_items(entity_type)
    if name not in {row[0] for row in rows}:
        name = rows[0][0]
    setattr(context.window_manager.hd2pb_preset_library, field, name)


def _preset_items(_self, context):
    try:
        entity_type = getattr(_self, "entity_type", "")
        if not entity_type:
            _owner, _project, entity_type, _entity, _parent = _current_parameter_entity(context)
        return typed_preset_items(entity_type)
    except Exception as exc:
        key = ("error", str(exc))
        return _TYPED_PRESET_ENUM_CACHE.setdefault(key, [("__ERROR__", "请先选择参数实体", str(exc), 0)])


def _operator_preset_get(self):
    rows = _preset_items(self, bpy.context)
    name = self.get("_preset_choice_name", "")
    return next((row[3] for row in rows if row[0] == name), rows[0][3])


def _operator_preset_set(self, value):
    rows = _preset_items(self, bpy.context)
    self["_preset_choice_name"] = next((row[0] for row in rows if row[3] == value), rows[0][0])


def _export_independent_update(sdk, context, owner, filepath=None):
    try:
        return sdk.export_independent_physics_update(context, owner, filepath)
    except sdk.PhysicsPackagingError as exc:
        # SDK 制作检查属于可行动错误，不能被跨插件边界吞成“内部失败”。
        raise ownership.HD2PBError(str(exc)) from exc


def _independent_package_filepath(filepath):
    """规范化完整复合后缀，同时收敛旧对话框产生的重复后缀。"""
    directory, name = os.path.split(filepath)
    if not name:
        return filepath
    if name.casefold().endswith(".zip"):
        name = name[:-4]
    elif not name.casefold().endswith(".hd2irpack"):
        name = os.path.splitext(name)[0]
    while name.casefold().endswith(".hd2irpack"):
        name = name[:-len(".hd2irpack")]
    return os.path.join(directory, name + ".hd2irpack.zip")


class HD2PB_OT_export_v1(Operator, ExportHelper):
    """由 AQSDK 从实际 Unit 解析映射并更新物理/衣物姿态独立配套包。"""

    bl_idname = "hd2pb.export_v1"
    bl_label = "导出物理与姿态 / 更新独立配套包"
    bl_description = (
        "AQSDK 独立模式按最近保存的身体/头盔目标更新物理与衣物姿态配套 ZIP；身体共享规范资源，头盔独立；"
        "模型或骨架变化需先重新保存；Unit 与骨骼槽位全部自动解析，不支持手动映射直出"
    )
    bl_options = {"REGISTER"}
    filename_ext = ".hd2irpack.zip"

    filter_glob: StringProperty(
        name="HD2 独立配套包筛选",
        description="只显示物理与衣物姿态独立配套 ZIP 文件",
        default="*.hd2irpack.zip",
        options={"HIDDEN"},
        maxlen=255,
    )
    owner_name: StringProperty(
        name="导出所属骨架",
        description="打开文件选择器时记录的物理骨所属 Armature Object；用于防止选择文件期间切换项目",
        options={"HIDDEN"},
    )
    project_uuid: StringProperty(
        name="导出项目 UUID",
        description="打开文件选择器时记录的物理骨项目身份；执行时必须仍与所属骨架一致",
        options={"HIDDEN"},
    )
    export_domain: StringProperty(name="导出身体或头盔域", description="记录打开文件选择器时的独立保存域，防止中途切换", options={"HIDDEN"})

    def check(self, _context):
        # ExportHelper.check 会先删除最后一个扩展名，导致复合扩展名无限叠加。
        # 只处理文件名，不改目录；重复回调不再改变已规范化的路径。
        filepath = _independent_package_filepath(self.filepath)
        if filepath == self.filepath:
            return False
        self.filepath = filepath
        return True

    @staticmethod
    def _independent_backend(context):
        settings = getattr(context.scene, 'Hd2ToolPanelSettings', None)
        if settings is None or not getattr(settings, 'IndependentPackagingMode', False):
            raise ownership.HD2PBError('请启用 AQSDK 独立封包模式并先保存部位；Unit 与骨骼槽位由 SDK 自动解析')
        import sys
        # 两 SDK 的 RNA/操作符名称相同，必须使用当前面板实际所属且已启用的
        # 根模块；不得导入“已安装但未启用”的另一个 SDK 并使用其全局资源表。
        enabled = getattr(getattr(context, 'preferences', None), 'addons', ())
        candidates = []
        for addon in enabled:
            module_name = addon.module
            module = sys.modules.get(module_name)
            settings_type = getattr(module, 'Hd2ToolPanelSettings', None)
            if (isinstance(settings_type, type)
                    and (settings_type.__module__ == module_name
                         or settings_type.__module__.startswith(module_name + '.'))):
                candidates.append((module_name, module, settings_type))
        if len(candidates) > 1:
            raise ownership.HD2PBError('检测到多个已启用的 AQSDK；它们共用面板与操作符，请仅保留一个版本启用后重启 Blender')
        owner_module = type(settings).__module__
        matches = [module for name, module, settings_type in candidates
                   if isinstance(settings, settings_type)
                   and (owner_module == name or owner_module.startswith(name + '.'))]
        if len(matches) != 1:
            raise ownership.HD2PBError('独立配套更新需要当前面板所属的 AQSDK 处于启用状态；请启用配套 SDK 后重启 Blender')
        sdk = matches[0]
        if not callable(getattr(sdk, 'export_independent_physics_update', None)):
            raise ownership.HD2PBError('当前 AQSDK 不支持独立配套更新，请一起安装本轮配套版本')
        return sdk

    @classmethod
    def poll(cls, context):
        try:
            owner = ownership.explicit_export_owner(context)
            ownership.ensure_authoring_owner(owner)
            return True
        except (
            ownership.HD2PBError,
            AttributeError,
            ReferenceError,
            RuntimeError,
            TypeError,
        ):
            return False

    def invoke(self, context, event):
        try:
            owner = ownership.explicit_export_owner(context)
            project = ownership.ensure_authoring_owner(owner)
            self.owner_name = owner.name
            self.project_uuid = project.project_uuid
            sdk = self._independent_backend(context)
            summary = _export_independent_update(sdk, context, owner)
            self.export_domain = summary['content_domain']
            self.filepath = summary['default_name']
            return ExportHelper.invoke(self, context, event)
        except Exception as exc:
            return _cancel(self, exc)

    def _resolve_export_owner(self):
        if not self.owner_name or not self.project_uuid:
            raise ownership.HD2PBError(
                "导出上下文缺少所属骨架或项目身份；请从“文件 > 导出”重新打开导出窗口"
            )
        owner = bpy.data.objects.get(self.owner_name)
        if owner is None or owner.type != "ARMATURE":
            raise ownership.HD2PBError(
                f"打开导出窗口时的所属骨架“{self.owner_name}”已经不存在；本次未写出文件"
            )
        project = ownership.ensure_authoring_owner(owner)
        if project.project_uuid != self.project_uuid:
            raise ownership.HD2PBError(
                "打开导出窗口后项目身份发生了变化；为避免导出错误项目，本次未写出文件"
            )
        return owner

    def execute(self, context):
        try:
            if not self.owner_name or not self.project_uuid:
                active = ownership.explicit_export_owner(context)
                project = ownership.ensure_authoring_owner(active)
                self.owner_name = active.name
                self.project_uuid = project.project_uuid
            owner = self._resolve_export_owner()
            sdk = self._independent_backend(context)
            summary = _export_independent_update(sdk, context, owner)
            if self.export_domain and self.export_domain != summary['content_domain']:
                raise ownership.HD2PBError('打开导出窗口后身体/头盔保存范围已改变，请重新打开导出窗口')
            self.check(context)
            summary = _export_independent_update(sdk, context, owner, self.filepath)
            self.report({'INFO'}, f"已导出{summary['domain_name']}物理配套更新包："
                        f"{summary['units']} 个 Unit / {summary['chains']} 条链"
                        + (f" / {summary['physics_files']} 个总物理文件" if summary.get('solver_scope') == 'shared_body_v2' else '')
                        + "；未重写模型或材质")
            return {'FINISHED'}
        except Exception as exc:
            return _cancel(self, exc)

class HD2PB_OT_refresh_preview(Operator):
    """显式修复当前项目的 controller/preview 两层投影。"""

    bl_idname = "hd2pb.refresh_preview"
    bl_label = "修复实体预览"
    bl_description = (
        "仅在预览被手动删除、复制或损坏时使用：从 canonical 原子重建球形 Node Empty、可选择的 "
        "Chain/Collider Curve 和不可选择的 Node radius/角限 Curve；所有实体由 Geometry Nodes 求值，"
        "日常拖动参数会自动实时刷新"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            owner = ownership.project_owner(context, remember=True)
            result = controllers.rebuild_project_controllers(owner, context_scene=context.scene)
            _remember_navigation_after_commit(self, context, owner)
            self.report(
                {"INFO"},
                f"已修复 {result['controllers']} 个 controller 与 {result['previews']} 个实体预览",
            )
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


class HD2PB_OT_preview_isolate(Operator):
    """隔离当前 Chain/Node/Collider 的预览，或恢复全部分类显示。"""

    bl_idname = "hd2pb.preview_isolate"
    bl_label = "隔离物理骨预览"
    bl_description = (
        "只隐藏或显示生成预览，不改变 canonical、controller、渲染或导出；隔离 Chain 时同时显示其节点和角限"
    )
    bl_options = {"REGISTER"}

    action: EnumProperty(
        name="预览隔离动作",
        description="隔离当前实体，或清除隔离并恢复所有已启用分类",
        items=(("ISOLATE", "隔离当前实体", "只显示当前实体或当前骨链的预览"),
               ("SHOW_ALL", "显示全部预览", "清除当前实体隔离状态")),
        default="ISOLATE",
    )

    def execute(self, context):
        try:
            owner = ownership.project_owner(context, remember=True)
            project = ownership.ensure_authoring_owner(owner)
            if self.action == "SHOW_ALL":
                project.preview_isolate_entity_uuid = ""
                label = "已显示全部启用的物理骨预览"
            else:
                _owner, _project, entity, parent = ownership.resolve_controller(context.object)
                entity_type = context.object.hd2pb_entity_type
                if entity_type not in {
                    properties.ENTITY_CHAIN,
                    properties.ENTITY_NODE,
                    properties.ENTITY_COLLIDER,
                    properties.ENTITY_CHAIN_LINK,
                    properties.ENTITY_LINK_COLLISION,
                }:
                    raise ownership.HD2PBError(
                        "只有 Chain、Node、Collider、ChainLink 或 LinkCollision 可以作为隔离目标"
                    )
                project.preview_isolate_entity_uuid = entity.uuid
                label = "已隔离当前物理骨实体；可随时点击“显示全部预览”恢复"
            preview.apply_project_style(owner)
            self.report({"INFO"}, label)
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


def _paste_targets(context, owner, entity_type, entity):
    targets = {entity.uuid: entity}
    for obj in tuple(context.selected_objects or ()):
        if not (getattr(obj, 'hd2pb_controller_schema', 0) or getattr(obj, 'hd2pb_preview_schema', 0)):
            continue
        current_owner, _project, current, _parent = ownership.resolve_controller(obj)
        if current_owner != owner or obj.hd2pb_entity_type != entity_type:
            raise ownership.HD2PBError('多选粘贴只支持同项目、同类型实体；请移除其他物理实体后重试')
        targets[current.uuid] = current
    return list(targets.values())


def _apply_parameter_batch(owner, entity_type, targets, incoming, *, node_mode="NUMERIC"):
    """Prepare every target before writing, then commit and canonical-check one transaction."""
    project = ownership.ensure_authoring_owner(owner)
    previous = [_parameter_payload(entity_type, target) for target in targets]
    prepared = [preset_v1.merge_parameters(entity_type, values, incoming, node_mode=node_mode)
                for values in previous]
    if entity_type == properties.ENTITY_NODE:
        for target, values in zip(targets, prepared):
            _node, chain = ownership.find_entity(project, entity_type, target.uuid)
            if (not chain.root_swing and chain.nodes[0].uuid == target.uuid
                    and (values['limit_kind'] != 'NONE' or values.get('reference_mode') != 'ANIMATED_SEGMENT'
                         or values.get('override_collision_filter'))):
                raise ownership.HD2PBError('固定驱动节点不能应用模拟角限或独立碰撞过滤；整批未修改')
    try:
        with preview.suspend_updates():
            for target, values in zip(targets, prepared):
                for key, value in values.items():
                    setattr(target, key, value)
            issues = ownership.canonical_issues(owner)
            if issues:
                raise ownership.HD2PBError('应用后物理数据无效：' + '；'.join(issues))
        preview.flush_now()
    except Exception:
        with preview.suspend_updates():
            for target, values in zip(targets, previous):
                for key, value in values.items():
                    setattr(target, key, value)
        raise


class HD2PB_OT_copy_entity_parameters(Operator):
    """把当前 Profile/Node/Collider 的物理参数复制到插件内剪贴板。"""

    bl_idname = "hd2pb.copy_entity_parameters"
    bl_label = "复制当前实体参数"
    bl_description = (
        "只复制同类型可复用物理参数；不会复制 UUID、显示名称、骨骼绑定、Profile 引用或 Chain 拓扑"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        global _PARAMETER_CLIPBOARD
        try:
            _owner, _project, entity_type, entity, _parent = _current_parameter_entity(context)
            _PARAMETER_CLIPBOARD = {
                "entity_type": entity_type,
                "parameters": _parameter_payload(entity_type, entity),
            }
            labels = {
                properties.ENTITY_PROFILE: "弹簧参数配置",
                properties.ENTITY_NODE: "链节点",
                properties.ENTITY_COLLIDER: "碰撞体",
                properties.ENTITY_CHAIN_LINK: "链组连接",
            }
            self.report(
                {"INFO"},
                f"已复制{labels[entity_type]}参数；未复制 UUID、名称和骨骼绑定。",
            )
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


class HD2PB_OT_paste_entity_parameters(Operator):
    """把同类型剪贴板参数完整校验后粘贴到当前实体。"""

    bl_idname = "hd2pb.paste_entity_parameters"
    bl_label = "粘贴到选中同类型实体"
    bl_description = (
        "复制活动实体后，多选同项目、同类型实体统一粘贴；整批校验、失败整批回滚，不复制身份和绑定"
    )
    bl_options = {"REGISTER", "UNDO"}
    node_mode: EnumProperty(name="节点参数范围", description="选择仅数值、包含方向或仅方向；默认保留目标方向", items=_NODE_APPLY_MODES, default="NUMERIC")

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        try:
            owner, _project, kind, entity, _parent = _current_parameter_entity(context)
            if kind == properties.ENTITY_NODE:
                self.layout.prop(self, "node_mode")
            self.layout.label(text=f"将修改 {len(_paste_targets(context, owner, kind, entity))} 个实体")
            self.layout.label(text="身份、绑定和拓扑保持不变；方向按父骨局部空间复制")
        except Exception as exc:
            self.layout.label(text=str(exc), icon='ERROR')

    def execute(self, context):
        try:
            if _PARAMETER_CLIPBOARD is None:
                raise ownership.HD2PBError("物理参数剪贴板为空，请先复制一个实体参数")
            owner, project, entity_type, entity, _parent = _current_parameter_entity(context)
            source_type = _PARAMETER_CLIPBOARD["entity_type"]
            if source_type != entity_type:
                source_label = {
                    properties.ENTITY_PROFILE: "弹簧参数配置",
                    properties.ENTITY_NODE: "链节点",
                    properties.ENTITY_COLLIDER: "碰撞体",
                    properties.ENTITY_CHAIN_LINK: "链组连接",
                }[source_type]
                target_label = {
                    properties.ENTITY_PROFILE: "弹簧参数配置",
                    properties.ENTITY_NODE: "链节点",
                    properties.ENTITY_COLLIDER: "碰撞体",
                    properties.ENTITY_CHAIN_LINK: "链组连接",
                }[entity_type]
                raise ownership.HD2PBError(
                    f"剪贴板保存的是{source_label}参数，不能粘贴到{target_label}"
                )
            targets = _paste_targets(context, owner, entity_type, entity)
            _apply_parameter_batch(owner, entity_type, targets, _PARAMETER_CLIPBOARD['parameters'],
                                   node_mode=self.node_mode)
            self.report({"INFO"}, f"已粘贴到 {len(targets)} 个同类型实体；UUID、名称、骨骼绑定和拓扑保持不变。")
            return {"FINISHED"}
        except preset_v1.PresetError as exc:
            return _cancel(self, ownership.HD2PBError(str(exc)))
        except Exception as exc:
            return _cancel(self, exc)


class HD2PB_OT_save_preset(Operator):
    """把当前实体参数原子保存为用户配置目录中的 JSON Preset。"""

    bl_idname = "hd2pb.save_preset"
    bl_label = "保存物理参数预设"
    bl_description = (
        "把当前 Profile、Node 或 Collider 的可复用参数保存为带 schema、类型和单位声明的 JSON；不保存身份、绑定或拓扑"
    )
    bl_options = {"REGISTER"}

    preset_name: StringProperty(
        name="预设名称",
        description="用户配置目录中的显示名称；不能包含路径、Windows 保留字符或设备名",
        default="我的物理参数",
        maxlen=180,
    )
    confirm_overwrite: BoolProperty(name="允许覆盖同名预设（不可撤销）", description="明确允许替换当前类型的同名用户 JSON 文件；此文件操作不能通过 Blender 撤销", default=False)

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        try:
            _owner, _project, entity_type, entity, _parent = _current_parameter_entity(context)
            directory = _preset_directory(entity_type)
            if preset_v1.resolve_preset_path(directory, self.preset_name).exists() and not self.confirm_overwrite:
                raise ownership.HD2PBError("同类型预设已存在；请改名或勾选允许覆盖，文件尚未改变")
            path = preset_v1.save_preset(
                directory,
                self.preset_name,
                entity_type,
                _parameter_payload(entity_type, entity),
            )
            _select_library_preset(context, entity_type, path.name[:-len(preset_v1.PRESET_SUFFIX)])
            self.report({"INFO"}, f"已原子保存物理参数预设“{path.name}”")
            return {"FINISHED"}
        except (preset_v1.PresetError, OSError) as exc:
            return _cancel(self, ownership.HD2PBError(str(exc)))
        except Exception as exc:
            return _cancel(self, exc)


def _preset_targets(context, owner, entity_type, entity, parent, scope):
    if scope == "CURRENT":
        return [entity]
    if scope in {"CHAIN_NODES", "CHAIN_TAIL"}:
        if entity_type != properties.ENTITY_NODE or parent is None:
            raise ownership.HD2PBError("“整条骨链节点”范围只适用于当前选择的 Node")
        nodes = list(parent.nodes)
        start = next((i for i, node in enumerate(nodes) if node.uuid == entity.uuid), -1)
        if start < 0:
            raise ownership.HD2PBError("当前节点不在所属链中，请重新选择")
        return nodes[max(1, start) if scope == "CHAIN_TAIL" else 1:]
    if scope != "SELECTED_TYPE":
        raise ownership.HD2PBError("未知预设应用范围")
    targets = []
    for obj in tuple(context.selected_objects or ()):
        if not (
            getattr(obj, "hd2pb_controller_schema", 0)
            or getattr(obj, "hd2pb_preview_kind", properties.PREVIEW_NONE)
            in {properties.PREVIEW_CHAIN_CURVE, properties.PREVIEW_COLLIDER_SHAPE}
        ):
            continue
        try:
            current_owner, _project, current_entity, _parent = ownership.resolve_controller(obj)
        except ownership.HD2PBError:
            continue
        if current_owner != owner:
            raise ownership.HD2PBError("选区包含另一物理项目；整批未修改")
        if obj.hd2pb_entity_type == entity_type:
            targets.append(current_entity)
    if not targets:
        raise ownership.HD2PBError("当前选区没有同项目、同类型的有效 controller")
    unique = []
    seen = set()
    for target in targets:
        key = getattr(target, "uuid", "") or target.as_pointer()
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return unique


class HD2PB_OT_apply_preset(Operator):
    """把同类型 JSON Preset 应用到当前、同类型选区或整条链节点。"""

    bl_idname = "hd2pb.apply_preset"
    bl_label = "应用物理参数预设"
    bl_description = (
        "完整读取并校验同类型 JSON Preset 后一次性应用；坏文件、未来版本或类型不符会拒绝，且不会留下部分修改"
    )
    bl_options = {"REGISTER", "UNDO"}

    selected_name: StringProperty(name="选中的预设名称", description="由分类面板传入的精确预设名称", options={"HIDDEN"})
    entity_type: StringProperty(name="预设实体类型", description="由分类面板传入的预设类型，应用时必须与活动实体一致", options={"HIDDEN"})
    node_mode: EnumProperty(name="节点参数范围", description="选择仅数值、包含方向或仅方向；默认保留目标方向", items=_NODE_APPLY_MODES, default="NUMERIC")

    preset_name: EnumProperty(
        name="参数预设",
        description="用户配置目录中与当前实体类型一致的物理参数预设",
        items=_preset_items,
        get=_operator_preset_get,
        set=_operator_preset_set,
    )
    scope: EnumProperty(
        name="应用范围",
        description="选择预设要修改的同类型实体范围",
        items=(
            ("CURRENT", "当前实体", "只修改当前选择的实体"),
            ("SELECTED_TYPE", "同类型选区", "修改当前选中的同项目、同类型 controller"),
            ("CHAIN_NODES", "整条骨链模拟节点", "当前为 Node 时修改所属 Chain 的全部可编辑模拟节点"),
            ("CHAIN_TAIL", "当前节点至链尾", "包含当前及后续节点；跳过固定驱动节点，不改变其他链"),
        ),
        default="CURRENT",
    )

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        if self.selected_name:
            self.layout.label(text=f"预设：{self.selected_name}")
        else:
            self.layout.prop(self, "preset_name")
        self.layout.prop(self, "scope")
        try:
            owner, _project, kind, entity, parent = _current_parameter_entity(context)
            if kind == properties.ENTITY_NODE:
                self.layout.prop(self, "node_mode")
            self.layout.label(text=f"将修改 {len(_preset_targets(context, owner, kind, entity, parent, self.scope))} 个实体")
        except Exception as exc:
            self.layout.label(text=str(exc), icon='ERROR')

    def execute(self, context):
        try:
            name = self.selected_name or self.preset_name
            if name in {"__NONE__", "__ERROR__", ""}:
                raise ownership.HD2PBError("没有可应用的同类型有效预设")
            owner, _project, entity_type, entity, parent = _current_parameter_entity(context)
            if self.entity_type and self.entity_type != entity_type:
                raise ownership.HD2PBError("预设类型与当前活动实体不符，请选择同类型实体")
            document = preset_v1.read_preset(
                _preset_directory(entity_type),
                name,
                expected_entity_type=entity_type,
            )
            targets = _preset_targets(context, owner, entity_type, entity, parent, self.scope)
            _apply_parameter_batch(owner, entity_type, targets, document["parameters"], node_mode=self.node_mode)
            self.report({"INFO"}, f"已向 {len(targets)} 个同类型实体应用预设；身份、绑定和拓扑未修改。")
            return {"FINISHED"}
        except preset_v1.PresetError as exc:
            return _cancel(self, ownership.HD2PBError(str(exc)))
        except Exception as exc:
            return _cancel(self, exc)


class HD2PB_OT_delete_preset(Operator):
    """确认后删除用户配置目录中的一个精确物理参数预设。"""

    bl_idname = "hd2pb.delete_preset"
    bl_label = "删除物理参数预设"
    bl_description = (
        "删除当前实体类型的一个用户 JSON Preset；这是外部文件操作，不能随 Blender 撤销，执行前会再次确认"
    )
    bl_options = {"REGISTER"}

    selected_name: StringProperty(name="选中的预设名称", description="由分类面板传入的待删除精确预设名称", options={"HIDDEN"})
    entity_type: StringProperty(name="预设实体类型", description="限定待删除预设所属的类型子目录", options={"HIDDEN"})

    preset_name: EnumProperty(
        name="要删除的参数预设",
        description="只列出与当前实体类型一致的用户预设",
        items=_preset_items,
        get=_operator_preset_get,
        set=_operator_preset_set,
    )
    confirm_delete: BoolProperty(
        name="我确认删除这个用户预设",
        description="外部 JSON 文件删除不能随 Blender 撤销；勾选后才允许执行",
        default=False,
    )

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, _context):
        layout = self.layout
        if self.selected_name:
            layout.label(text=f"删除 {self.entity_type} 预设：{self.selected_name}")
        else:
            layout.prop(self, "preset_name")
        warning = layout.box()
        warning.alert = True
        warning.label(text="删除的是用户配置目录中的 JSON 文件，不能随 Blender 撤销", icon="ERROR")
        warning.prop(self, "confirm_delete")

    def execute(self, context):
        try:
            name = self.selected_name or self.preset_name
            if name in {"__NONE__", "__ERROR__", ""}:
                raise ownership.HD2PBError("没有可删除的同类型有效预设")
            if not self.confirm_delete:
                raise ownership.HD2PBError("请先勾选确认删除；预设文件尚未改变")
            entity_type = self.entity_type or _current_parameter_entity(context)[2]
            path = preset_v1.delete_preset(_preset_directory(entity_type), name)
            _select_library_preset(context, entity_type, "__NONE__")
            self.report({"INFO"}, f"已删除用户物理参数预设“{path.name}”；此文件操作不能撤销。")
            return {"FINISHED"}
        except preset_v1.PresetError as exc:
            return _cancel(self, ownership.HD2PBError(str(exc)))
        except Exception as exc:
            return _cancel(self, exc)


class HD2PB_OT_import_preset(Operator, ImportHelper):
    """导入分享 JSON，完整校验后按类型保存；不直接改动当前实体。"""
    bl_idname = "hd2pb.import_preset"
    bl_label = "导入 JSON 参数预设"
    bl_description = "导入其他用户分享的预设到对应类型子目录；严格校验格式、类型、单位及参数"
    bl_options = {"REGISTER"}
    filename_ext = ".json"
    filter_glob: StringProperty(name="预设文件筛选", description="仅列出可导入的 JSON 参数预设文件", default="*.json", options={"HIDDEN"})
    preset_name: StringProperty(name="保存名称（留空使用文件名）", description="导入后在用户库中的预设名称；留空使用源文件名", maxlen=180)
    confirm_overwrite: BoolProperty(name="允许覆盖同名预设（不可撤销）", description="明确允许替换导入类型的同名用户 JSON 文件；此文件操作不能通过 Blender 撤销", default=False)

    def execute(self, context):
        try:
            path = preset_v1.import_preset(_preset_directory(), self.filepath,
                name=self.preset_name, overwrite=self.confirm_overwrite)
            _select_library_preset(context, path.parent.name, path.name[:-len(preset_v1.PRESET_SUFFIX)])
            self.report({"INFO"}, f"已导入 {path.parent.name} 预设“{path.name}”")
            return {"FINISHED"}
        except (preset_v1.PresetError, OSError) as exc:
            return _cancel(self, ownership.HD2PBError(str(exc)))


class HD2PB_OT_open_preset_directory(Operator):
    """打开用户预设库；三个类型子目录可供用户直接管理。"""
    bl_idname = "hd2pb.open_preset_directory"
    bl_label = "打开用户预设文件夹"
    bl_description = "打开用户配置目录下的预设库；PROFILE、NODE、COLLIDER 分别存放，不读取旧平铺目录"

    def execute(self, context):
        try:
            for entity_type in sorted(preset_v1.ENTITY_TYPES):
                _preset_directory(entity_type).mkdir(parents=True, exist_ok=True)
            return bpy.ops.wm.path_open(filepath=str(_preset_directory()))
        except (preset_v1.PresetError, OSError) as exc:
            return _cancel(self, ownership.HD2PBError(str(exc)))


def _chain_operation_targets(context, scope, *, allow_node=False):
    owner, _project, entity, parent = ownership.resolve_controller(context.object, require_unique=True)
    kind = context.object.hd2pb_entity_type
    if kind not in ({properties.ENTITY_CHAIN, properties.ENTITY_NODE} if allow_node else {properties.ENTITY_CHAIN}):
        raise ownership.HD2PBError("请先选择正式骨链" + ("或节点" if allow_node else ""))
    if scope == 'CURRENT':
        if kind == properties.ENTITY_NODE:
            index = next(i for i, node in enumerate(parent.nodes) if node.uuid == entity.uuid)
            return owner, [(parent, [(index, entity)])]
        return owner, [(entity, list(enumerate(entity.nodes)))]
    if scope != 'SELECTED_CHAINS':
        raise ownership.HD2PBError('未知骨链操作范围')
    chains = {}
    for obj in tuple(context.selected_objects or ()):
        if not (getattr(obj, 'hd2pb_controller_schema', 0) or getattr(obj, 'hd2pb_preview_schema', 0)):
            continue
        current_owner, _project, current, _parent = ownership.resolve_controller(obj, require_unique=True)
        if current_owner != owner:
            raise ownership.HD2PBError('选区包含另一物理项目；整批未修改')
        if obj.hd2pb_entity_type == properties.ENTITY_CHAIN:
            chains[current.uuid] = current
    if not chains:
        raise ownership.HD2PBError('选区没有同项目的正式骨链；空选区不会操作全部链')
    return owner, [(chain, list(enumerate(chain.nodes))) for chain in chains.values()]


def _commit_node_updates(owner, changes):
    # All directions and merged values are resolved before the first RNA write.
    previous = [(node, _parameter_payload(properties.ENTITY_NODE, node)) for node, _values in changes]
    prepared = []
    for (node, old), (_target, updates) in zip(previous, changes):
        merged = dict(old)
        merged.update(updates)
        try:
            prepared.append((node, preset_v1.validate_parameters(properties.ENTITY_NODE, merged)))
        except preset_v1.PresetError as exc:
            raise ownership.HD2PBError(str(exc)) from exc
    try:
        with preview.suspend_updates():
            for node, values in prepared:
                for key, value in values.items():
                    setattr(node, key, value)
            issues = ownership.canonical_issues(owner)
            if issues:
                raise ownership.HD2PBError('批处理后结构无效：' + '；'.join(issues))
        preview.flush_now()
    except Exception:
        with preview.suspend_updates():
            for node, values in previous:
                for key, value in values.items():
                    setattr(node, key, value)
        raise


def _draw_chain_impact(layout, context, scope, *, allow_node=False):
    try:
        _owner, rows = _chain_operation_targets(context, scope, allow_node=allow_node)
        count = sum(sum(index > 0 for index, _node in nodes) for _chain, nodes in rows)
        layout.label(text=f"范围：{len(rows)} 条链、{count} 个模拟节点（固定驱动不修改）")
    except Exception as exc:
        layout.label(text=str(exc), icon='ERROR')


class HD2PB_OT_apply_limit_ramp(Operator):
    """按 RE Chain 的最大角限/迭代规则处理活动骨链。"""

    bl_idname = "hd2pb.apply_limit_ramp"
    bl_label = "应用角限渐变"
    bl_description = (
        "对当前活动骨链的所有模拟节点启用圆锥角限，并递增写入主限制半角：第 N 个节点使用 "
        "最大角限 × min(N / 最大迭代, 1)；固定驱动节点不会被修改"
    )
    bl_options = {"REGISTER", "UNDO"}
    scope: EnumProperty(name="应用范围", description="仅当前骨链或明确选中的同项目骨链，不把空选区视为全部", items=_CHAIN_SCOPES, default="CURRENT")

    max_angle_limit: FloatProperty(
        name="最大角限（半角）",
        description=(
            "达到最大迭代后使用的主限制半角。RE Chain 的默认 60° 是全角，"
            "本工具默认使用 30° 半角（全角 60°）；最大迭代 4 时前四个模拟节点依次得到 "
            "7.5°、15°、22.5°、30°，后续节点保持 30°"
        ),
        default=math.radians(30.0),
        min=0.0,
        max=math.pi,
        subtype="ANGLE",
    )
    max_iteration: IntProperty(
        name="最大迭代",
        description="主角限递增到最大值所需的模拟节点数量；达到后不再继续增大",
        default=4,
        min=1,
        max=64,
    )
    align_to_next: BoolProperty(
        name="同时对齐实际受限骨段",
        description="角限约束上一节点到当前节点的入段，按该 Rest 入段生成方向；关闭则保留手动方向",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        obj = getattr(context, "object", None)
        if obj is None:
            return False
        try:
            ownership.resolve_controller(
                obj,
                expected_type=properties.ENTITY_CHAIN,
                require_unique=True,
            )
        except (
            ownership.HD2PBError,
            AttributeError,
            ReferenceError,
            RuntimeError,
            TypeError,
        ):
            return False
        return True

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "scope")
        layout.prop(self, "max_angle_limit")
        layout.prop(self, "max_iteration")
        layout.prop(self, "align_to_next")
        _draw_chain_impact(layout, context, self.scope)

    def execute(self, context):
        try:
            owner, rows = _chain_operation_targets(context, self.scope)
            angle_step = self.max_angle_limit / self.max_iteration
            changes = []
            for chain, nodes in rows:
                if len(nodes) < 2:
                    raise ownership.HD2PBError('骨链没有模拟节点；整批未修改')
                for index, node in nodes:
                    if index == 0:
                        continue
                    values = dict(limit_kind='CONE', limit_angle=min(angle_step * index, self.max_angle_limit))
                    if self.align_to_next:
                        values.update(reference_direction=list(_aligned_reference_direction(owner, chain, index)),
                                      reference_mode="EXPLICIT_LOCAL")
                    changes.append((node, values))
            _commit_node_updates(owner, changes)
            self.report(
                {"INFO"},
                f"已为 {len(rows)} 条链的 {len(changes)} 个模拟节点启用圆锥角限并应用渐变。"
                + ("已对齐实际受限入段；整批已校验。"
                   if self.align_to_next else "保留手动方向。"),
            )
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


def _stable_parent_local_tangent(previous_bone, first_owner):
    first_local = previous_bone.matrix_local.to_3x3().inverted_safe() @ first_owner
    if first_local.length < 1.0e-6:
        raise ownership.HD2PBError("父骨 Rest 坐标系退化，无法计算稳定限制轴")
    first_local.normalize()
    candidates = (Vector((1.0, 0.0, 0.0)), Vector((0.0, 1.0, 0.0)), Vector((0.0, 0.0, 1.0)))
    seed = min(candidates, key=lambda axis: abs(axis.dot(first_local)))
    tangent = seed - first_local * seed.dot(first_local)
    if tangent.length < 1.0e-6:
        raise ownership.HD2PBError("父骨 Rest 坐标系无法提供稳定切向回退")
    tangent.normalize()
    return tangent


def _aligned_limit_axis(owner, chain, index):
    node = chain.nodes[index]
    previous_bone = owner.data.bones.get(chain.nodes[index - 1].bone_name)
    current_bone = owner.data.bones.get(node.bone_name)
    next_bone = owner.data.bones.get(chain.nodes[index + 1].bone_name)
    if previous_bone is None or current_bone is None or next_bone is None:
        raise ownership.HD2PBError("相邻节点引用的 Rest 骨骼已经不存在")
    first = Vector(current_bone.matrix_local.translation - previous_bone.matrix_local.translation)
    second = Vector(next_bone.matrix_local.translation - current_bone.matrix_local.translation)
    if first.length < 1.0e-6 or second.length < 1.0e-6:
        raise ownership.HD2PBError("相邻 Rest 骨段长度接近零")
    first.normalize()
    second.normalize()
    if node.limit_kind == "HINGE":
        axis_owner = first.cross(second)
    else:
        axis_owner = second - first * second.dot(first)
    if axis_owner.length < 1.0e-6:
        return tuple(_stable_parent_local_tangent(previous_bone, first))
    axis_owner.normalize()
    local_axis = previous_bone.matrix_local.to_3x3().inverted_safe() @ axis_owner
    if local_axis.length < 1.0e-6:
        raise ownership.HD2PBError("父骨 Rest 坐标系退化，无法转换限制轴")
    local_axis.normalize()
    return tuple(local_axis)


def _aligned_reference_direction(owner, chain, index):
    """Match the wire/native contract: previous node -> current node.

    The historical operator id is retained for saved UI/keymap compatibility.
    Using current -> next here rotated the *incoming* cone, e.g. 136 degrees
    away from HairG1's rest segment, driving a valid hair rig into the body.
    """

    previous_bone = owner.data.bones.get(chain.nodes[index - 1].bone_name)
    current_bone = owner.data.bones.get(chain.nodes[index].bone_name)
    if previous_bone is None or current_bone is None:
        raise ownership.HD2PBError("相邻节点引用的 Rest 骨骼已经不存在")
    incoming_owner = (
        Vector(current_bone.matrix_local.translation)
        - Vector(previous_bone.matrix_local.translation)
    )
    if incoming_owner.length < 1.0e-6:
        raise ownership.HD2PBError("当前受限 Rest 入段长度接近零")
    incoming_owner.normalize()
    reference = previous_bone.matrix_local.to_3x3().inverted_safe() @ incoming_owner
    if reference.length < 1.0e-6:
        raise ownership.HD2PBError("父骨 Rest 坐标系退化，无法写入角限参考方向")
    reference.normalize()
    return tuple(float(value) for value in reference)


def _auto_align_new_limit(node):
    project = getattr(node.id_data, 'hd2pb_project', None)
    if project is None or project.authoring_object is None or not node.uuid:
        return
    for chain in project.chains:
        for index,current in enumerate(chain.nodes):
            if current.uuid != node.uuid:
                continue
            if index == 0:
                return
            try:
                direction = _aligned_reference_direction(project.authoring_object,chain,index)
                axis = None if node.limit_kind == 'CONE' or index >= len(chain.nodes)-1 else _aligned_limit_axis(project.authoring_object,chain,index)
                with preview.suspend_updates():
                    node.reference_direction = direction
                    node.reference_mode = 'EXPLICIT_LOCAL'
                    if axis is not None: node.limit_axis = axis
            except ownership.HD2PBError:
                # Incomplete/degenerate data stays editable; strict validation
                # still reports it when the user checks or exports the project.
                pass
            return


class HD2PB_OT_align_limit_to_next(Operator):
    """从 Rest Pose 相邻两段计算单节点或整条 Chain 的稳定限制轴。"""

    bl_idname = "hd2pb.align_limit_to_next"
    bl_label = "限制对齐实际受限骨段"
    bl_description = (
        "选择 Node 时处理当前入段；选择 Chain 时批量对齐角限到上一节点→当前节点的 Rest 方向。"
        "铰链/椭圆球面写入限制轴，圆锥写入显式父骨局部参考方向；共线段使用父骨局部稳定切向回退，"
        "末端圆锥也可对齐；末端铰链/椭圆轴及无限制节点跳过，缺骨或零长度段整批拒绝"
    )
    bl_options = {"REGISTER", "UNDO"}

    scope: EnumProperty(name="应用范围", description="仅当前实体或明确选中的同项目骨链，不跨项目操作", items=_CHAIN_SCOPES, default="CURRENT")

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        self.layout.prop(self, "scope")
        _draw_chain_impact(self.layout, context, self.scope, allow_node=True)
        try:
            _owner, rows = _chain_operation_targets(context, self.scope, allow_node=True)
            count = sum(sum(index > 0 and node.limit_kind != 'NONE' and
                            (index < len(chain.nodes) - 1 or node.limit_kind == 'CONE')
                            for index, node in nodes) for chain, nodes in rows)
            self.layout.label(text=f"实际可对齐 {count} 个节点；其余固定／无限制／末端方向轴跳过")
        except Exception:
            pass

    def execute(self, context):
        try:
            owner, rows = _chain_operation_targets(context, self.scope, allow_node=True)
            batch = self.scope == 'SELECTED_CHAINS' or context.object.hd2pb_entity_type == properties.ENTITY_CHAIN
            prepared = []
            skipped = 0
            for chain, candidates in rows:
                for index, node in candidates:
                    if (index == 0 or (index >= len(chain.nodes) - 1 and node.limit_kind != 'CONE')
                            or node.limit_kind == "NONE"):
                        skipped += 1
                        if not batch:
                            raise ownership.HD2PBError("当前节点是固定驱动、末端方向轴或无限制，不能自动对齐")
                        continue
                    values = dict(reference_direction=list(_aligned_reference_direction(owner, chain, index)),
                                  reference_mode='EXPLICIT_LOCAL')
                    if node.limit_kind != 'CONE':
                        values['limit_axis'] = list(_aligned_limit_axis(owner, chain, index))
                    prepared.append((node, values))
            _commit_node_updates(owner, prepared)
            processed = len(prepared)
            processed_types = [item[0].limit_kind for item in prepared]

            if batch:
                level = {"INFO"} if processed else {"WARNING"}
                self.report(
                    level,
                    f"{len(rows)} 条骨链限制轴对齐完成：处理 {processed} 个，跳过 {skipped} 个；"
                    "跳过项仅固定驱动、无限制和末端铰链/椭圆；整批已校验。",
                )
            else:
                type_name = {
                    "HINGE": "铰链（Hinge）",
                    "SPHERICAL": "椭圆球面（Spherical）",
                    "CONE": "圆锥（Cone）显式参考方向",
                }[processed_types[0]]
                self.report({"INFO"}, f"已按实际受限 Rest 入段对齐{type_name}限制轴。")
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


class HD2PB_OT_select_controller(Operator):
    """从侧边栏按实体 UUID 选择唯一 controller。"""

    bl_idname = "hd2pb.select_controller"
    bl_label = "选择物理骨编辑对象"
    bl_description = (
        "按项目 UUID、实体类型和实体 UUID 在当前 View Layer 选择唯一编辑入口；Chain、Collider、"
        "ChainLink 和 LinkCollision 会选择彩色 Curve，其他类型选择 Empty"
    )

    entity_type: EnumProperty(
        name="实体类型",
        items=properties.ENTITY_TYPE_ITEMS,
        default=properties.ENTITY_PROJECT,
        description="要选择的 canonical 实体类型",
        options={"HIDDEN"},
    )
    entity_uuid: StringProperty(
        name="实体 UUID",
        description="要选择的 canonical 实体稳定 UUID",
        options={"HIDDEN"},
    )

    def execute(self, context):
        try:
            owner = ownership.project_owner(context, remember=True)
            controller = controllers.select_entity_controller(
                context, owner, self.entity_type, self.entity_uuid
            )
            project = owner.data.hd2pb_project
            list_targets = {
                properties.ENTITY_PROFILE: ("profiles", "profile_index"),
                properties.ENTITY_CHAIN: ("chains", "chain_index"),
                properties.ENTITY_COLLIDER: ("colliders", "collider_index"),
                properties.ENTITY_CHAIN_LINK: ("chain_links", "chain_link_index"),
            }
            target = list_targets.get(self.entity_type)
            if target is not None:
                collection_name, index_name = target
                collection = getattr(project, collection_name)
                index = next(
                    (
                        index
                        for index, item in enumerate(collection)
                        if item.uuid == self.entity_uuid
                    ),
                    None,
                )
                if index is not None:
                    setattr(project, index_name, index)
            self.report({"INFO"}, f"已选择编辑对象“{controller.name}”")
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


class HD2PB_OT_switch_mode(Operator):
    """在当前物理骨项目的物体模式与姿态模式之间安全切换。"""

    bl_idname = "hd2pb.switch_mode"
    bl_label = "切换物理骨编辑模式"
    bl_description = (
        "精确激活当前项目所属的骨架对象，并在物体模式（Object Mode）与姿态模式（Pose Mode）"
        "之间切换"
    )
    bl_options = {"REGISTER"}

    target_mode: EnumProperty(
        name="目标模式",
        items=(
            ("OBJECT", "物体模式", "切换到物体模式管理项目、配置和编辑对象"),
            ("POSE", "姿态模式", "切换到姿态模式从骨骼创建链和碰撞体"),
        ),
        default="POSE",
        description="要进入的 Blender 编辑模式",
    )

    @classmethod
    def poll(cls, context):
        return ownership.project_owner(context) is not None

    def execute(self, context):
        try:
            owner = ownership.project_owner(context, remember=True)
            if owner is None:
                raise ownership.HD2PBError("没有可切换模式的所属骨架对象")
            layer_owner = context.view_layer.objects.get(owner.name)
            if layer_owner != owner:
                raise ownership.HD2PBError(
                    f"所属骨架“{owner.name}”不在当前视图层（View Layer），无法切换模式"
                )
            if owner.hide_get() or owner.hide_viewport:
                raise ownership.HD2PBError(
                    f"所属骨架“{owner.name}”当前被隐藏；请先在大纲视图取消隐藏"
                )

            active = context.view_layer.objects.active
            if active is not None and active.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            for selected in tuple(context.selected_objects or ()):
                selected.select_set(False)
            owner.select_set(True)
            context.view_layer.objects.active = owner
            if self.target_mode == "POSE":
                bpy.ops.object.mode_set(mode="POSE")
                label = "姿态模式"
            else:
                if owner.mode != "OBJECT":
                    bpy.ops.object.mode_set(mode="OBJECT")
                label = "物体模式"
            self.report({"INFO"}, f"已切换到{label}：{owner.name}")
            return {"FINISHED"}
        except Exception as exc:
            return _cancel(self, exc)


class HD2PB_OT_make_project_unique(Operator):
    """把共享副本或普通 Shift-D 独立数据副本转换成唯一 canonical 项目。"""

    bl_idname = "hd2pb.make_project_unique"
    bl_label = "复制为独立物理骨项目"
    bl_description = (
        "用于仍共享 Armature datablock 的非 owner 副本，或普通 Shift-D 产生的独立 datablock 但"
        "project UUID 重复的副本：保留物理参数，重新生成全部 UUID 与 controller；"
        "不会清理或改写另一个项目的集合和编辑对象"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        owner = context.object
        if (
            owner is None
            or owner.type != "ARMATURE"
            or owner.data.hd2pb_project.schema_version <= 0
        ):
            return False
        instances = ownership.armature_objects(owner.data)
        shared_copy = len(instances) > 1 and owner.data.hd2pb_project.authoring_object != owner
        return shared_copy or bool(ownership.project_uuid_conflicts(owner))

    def execute(self, context):
        owner = context.object
        source_armature = owner.data
        copied_armature = None
        project = None
        snapshot = None
        try:
            source_project = source_armature.hd2pb_project
            if not ownership.project_initialized(source_project):
                raise ownership.HD2PBError("当前骨架尚未初始化 HD2 物理骨项目")
            if source_project.schema_version != properties.PROJECT_SCHEMA_VERSION:
                raise ownership.HD2PBError("项目 schema 不是当前版本，不能安全复制为独立项目")

            instances = ownership.armature_objects(source_armature)
            if len(instances) > 1:
                if source_project.authoring_object == owner:
                    raise ownership.HD2PBError(
                        "当前对象是共享 datablock 的原项目 owner；请选中复制出来的非 owner 骨架"
                    )
                if source_project.authoring_object is None:
                    raise ownership.HD2PBError("原项目 owner 已损坏，不能安全判断哪一份应保留")
                copied_armature = source_armature.copy()
                owner.data = copied_armature
                project = copied_armature.hd2pb_project
            else:
                if not ownership.project_uuid_conflicts(owner):
                    raise ownership.HD2PBError(
                        "当前项目没有共享 datablock 或重复 project UUID，无需执行独立复制"
                    )
                project = source_project
                snapshot = _snapshot_project(project)

            # 普通 Shift-D 会把 Collection 指针继续指向原项目。先断开副本引用，再换 UUID；
            # 这样后续 rebuild 的清理计划绝不会命中原项目集合。
            project.controller_collection = None
            project.authoring_object = owner
            _regenerate_project_uuids(project)
            result = controllers.rebuild_project_controllers(
                owner, context_scene=context.scene
            )
        except Exception as exc:
            if copied_armature is not None:
                owner.data = source_armature
                if copied_armature.users == 0:
                    bpy.data.armatures.remove(copied_armature)
            elif project is not None and snapshot is not None:
                try:
                    _restore_project(project, snapshot)
                except Exception as rollback_exc:
                    return _cancel(
                        self,
                        RuntimeError(f"独立复制失败且 canonical 回滚失败：{rollback_exc}"),
                    )
            return _cancel(self, exc)
        _remember_navigation_after_commit(self, context, owner)
        self.report(
            {"INFO"},
            f"已建立独立物理骨项目并重建 {result['controllers']} 个编辑对象；"
            "请通过 AQSDK 保存副本对应的部位以生成目标映射",
        )
        return {"FINISHED"}


class HD2PB_OT_convert_mmd(Operator):
    """从当前 MMD 模型生成可编辑物理近似，并保留逐项转换报告。"""
    bl_idname = "hd2pb.convert_mmd"
    bl_label = "一键轻量转换 MMD 物理（有损）"
    bl_description = "保留原 MMD 数据；生成可编辑弹簧、末端辅助骨、可识别布条连接及身体避让代理；未支持项逐条报告"
    bl_options = {"REGISTER", "UNDO"}

    accept_approximation: BoolProperty(name="接受近似转换（不是完整 MMD 刚体模拟）", default=False,
                                      description="保留原始 MMD 数据；将可表达的父子链转为弹簧骨，未支持项写入报告")
    conversion_mode: EnumProperty(name="模式", description="选择明确有损的轻量弹簧或实验性碰撞近似", items=(
        ("LIGHT", "完整轻量", "可编辑弹簧、末端骨、布条连接及身体避让；不是完整 MMD 刚体模拟"),
        ("CONTACTS", "原参数碰撞近似（实验）", "保留可表达碰撞，复杂模型可能无法收敛")), default="LIGHT")

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == "ARMATURE" and context.mode == "OBJECT"

    def invoke(self, context, _event):
        if self.conversion_mode == "LIGHT":
            self.accept_approximation = True
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, _context):
        self.layout.label(text="保留 PMX 刚体、约束、骨骼和权重；不覆盖已有物理项目。")
        self.layout.prop(self, "conversion_mode")
        self.layout.label(text="支持父子链及碰撞组；盒形、动力学为近似。")
        self.layout.label(text="未支持分叉、横向关节和六自由度硬限将逐项写入报告。")
        self.layout.prop(self, "accept_approximation")

    def execute(self, context):
        if not self.accept_approximation:
            self.report({"ERROR"}, "请先确认接受近似转换；原数据不会被删除")
            return {"CANCELLED"}
        try:
            from .mmd_conversion import convert_mmd, ConversionError
            owner = context.object
            report = convert_mmd(owner, mode=self.conversion_mode)
        except (ConversionError, ownership.HD2PBError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            return _cancel(self, exc)
        if not report['counts']['chains']:
            import json
            text = bpy.data.texts.new(f"MMD物理转换报告_{owner.name}.json")
            text.from_string(json.dumps(report, ensure_ascii=False, indent=2))
            self.report({'INFO'}, '全部候选链均无有效用途，未生成物理项目；原骨骼与 MMD 数据保留，详见报告')
            return {'FINISHED'}
        ownership.remember_project_owner(context, owner)
        # Canonical commit is complete. Preview failure must not masquerade as
        # a failed conversion or encourage an accidental duplicate import.
        try:
            controllers.rebuild_project_controllers(owner, context_scene=context.scene, initially_hidden=True)
        except Exception as exc:
            self.report({"WARNING"}, f"物理数据已生成，预览需重建：{exc}")
        text = bpy.data.texts.new(f"MMD物理转换报告_{owner.name}.json")
        # Blender 4.0 Text.write is extremely slow on a giant single UTF-8 line.
        # Full MMD graphs can spend minutes here after conversion.
        # Keep the canonical JSON on the owner; load readable bounded lines.
        import json
        text.from_string(json.dumps(report, ensure_ascii=False, indent=2))
        counts = report["counts"]
        cleaned = report.get('pruning', {}).get('counts', {})
        self.report({'INFO'}, f"依赖检查清理 {cleaned.get('chains', 0)} 条无用链、"
                    f"{cleaned.get('colliders', 0)} 个碰撞体；原骨骼与 MMD 数据未删除")
        self.report({"WARNING"}, f"已生成 {counts['chains']} 条近似链、{counts['colliders']} 个碰撞体；"
                    f"{counts['unsupported_bodies']} 个刚体、{counts['unsupported_constraints']} 个约束未转换，详见文本报告；新物理预览默认关闭，可在大纲视图打开集合的显示器开关")
        return {"FINISHED"}


class HD2PB_OT_convert_vrm(Operator, ImportHelper):
    """从用户预处理 VRM 生成可编辑近似物理，保留覆盖与损失报告。"""
    bl_idname = 'hd2pb.convert_vrm'
    bl_label = '导入预处理 VRM/VRC 物理（有损）…'
    bl_description = '在已导入源骨架上读取 VRM 1 SpringBone，生成可编辑近似；前置 VRC 预处理由用户完成，不导入网格'
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = '.vrm'
    filter_glob: StringProperty(name='预处理文件筛选', description='仅显示 VRM、GLB 或 glTF JSON 文件',
                                default='*.vrm;*.glb;*.gltf;*.json', options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'ARMATURE' and context.mode == 'OBJECT'

    def draw(self, context):
        self.layout.label(text='请选择与当前源骨架对应的预处理 VRM 1 文件。')
        self.layout.label(text='支持源骨架；已重定向时须保留源骨架和原映射字典。')
        self.layout.label(text='不覆盖已有物理，不导入网格；无骨骼的物理仍会清理。')
        self.layout.label(text='动力学、重力与角限中心为近似；未支持项写入文本报告。')

    def execute(self, context):
        try:
            from .vrm_conversion import convert_vrm, ConversionError
            owner = context.object
            report = convert_vrm(owner, self.filepath)
        except (ConversionError, ownership.HD2PBError, OSError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            return _cancel(self, exc)
        if not report['counts']['chains']:
            import json
            text = bpy.data.texts.new(f'VRM物理转换报告_{owner.name}.json')
            text.from_string(json.dumps(report, ensure_ascii=False, indent=2))
            self.report({'INFO'}, '全部候选链均无有效用途，未生成物理项目；原骨骼保留，详见报告')
            return {'FINISHED'}
        ownership.remember_project_owner(context, owner)
        try:
            text = bpy.data.texts.new(f'VRM物理转换报告_{owner.name}.json')
            import json
            text.from_string(json.dumps(report, ensure_ascii=False, indent=2))
            controllers.rebuild_project_controllers(owner, context_scene=context.scene, initially_hidden=True)
        except Exception as exc:
            self.report({'WARNING'}, f'物理已生成，预览/文本需重建；完整报告仍保存在骨架属性：{exc}')
        c = report['counts']
        cleaned = report.get('pruning', {}).get('counts', {})
        self.report({'INFO'}, f"依赖检查清理 {cleaned.get('chains', 0)} 条无用链、"
                    f"{cleaned.get('colliders', 0)} 个碰撞体；原骨骼未删除")
        missing_chains = c.get('source_springs', c['chains'] + c['unsupported_springs']) - c['chains']
        missing_colliders = c.get('source_colliders', c['colliders'] + c['unsupported_colliders']) - c['colliders']
        self.report({'WARNING'}, f"已生成 {c['chains']} 条近似链、{c['colliders']} 个碰撞体；"
                    f"未生成 {missing_chains} 条链/{missing_colliders} 个碰撞体（含删骨与无有效用途项），具体原因见文本报告；预览默认关闭，可在大纲视图打开集合显示器开关")
        return {'FINISHED'}


CLASSES = (
    HD2PB_OT_convert_vrm,
    HD2PB_OT_convert_mmd,
    HD2PB_OT_initialize_project,
    HD2PB_OT_migrate_project,
    HD2PB_OT_add_profile,
    HD2PB_OT_remove_profile,
    HD2PB_OT_set_creation_profile,
    HD2PB_OT_create_chain,
    HD2PB_OT_remove_chain,
    HD2PB_OT_append_tail_helper,
    HD2PB_OT_add_chain_link,
    HD2PB_OT_build_link_collisions,
    HD2PB_OT_remove_chain_link,
    HD2PB_OT_create_chain_from_selection,
    HD2PB_OT_rename_bone_chain,
    HD2PB_OT_add_collider,
    HD2PB_OT_create_collider_from_bone,
    HD2PB_OT_create_dynamic_collider_from_bone,
    HD2PB_OT_remove_collider,
    HD2PB_OT_assign_profile,
    HD2PB_OT_set_collider_bone,
    HD2PB_OT_rebuild_controllers,
    HD2PB_OT_validate_project,
    HD2PB_OT_show_validation_report,
    HD2PB_OT_locate_validation_issue,
    HD2PB_OT_export_v1,
    HD2PB_OT_refresh_preview,
    HD2PB_OT_preview_isolate,
    HD2PB_OT_copy_entity_parameters,
    HD2PB_OT_paste_entity_parameters,
    HD2PB_OT_save_preset,
    HD2PB_OT_apply_preset,
    HD2PB_OT_delete_preset,
    HD2PB_OT_import_preset,
    HD2PB_OT_open_preset_directory,
    HD2PB_OT_apply_limit_ramp,
    HD2PB_OT_align_limit_to_next,
    HD2PB_OT_select_controller,
    HD2PB_OT_switch_mode,
    HD2PB_OT_make_project_unique,
)


__all__ = ("CLASSES", "clear_runtime_state", "runtime_state_present")
