"""按 controller 类型显示的 Object Properties，以及骨骼调色板属性面板。"""

from __future__ import annotations

from bpy.types import Panel

from . import ownership, properties
from .authoring_ui_ops import draw_mask_field


def _resolved(context, entity_type=None):
    try:
        return ownership.resolve_controller(
            context.object,
            expected_type=entity_type,
            require_unique=True,
        )
    except (
        ownership.HD2PBError,
        AttributeError,
        ReferenceError,
        RuntimeError,
        TypeError,
    ):
        return None


def _resolved_simulated_node(context):
    result = _resolved(context, properties.ENTITY_NODE)
    if result is None:
        return None
    try:
        _owner, _project, node, chain = result
        index = next(
            (current for current, item in enumerate(chain.nodes) if item.uuid == node.uuid),
            -1,
        )
        return result if index > 0 else None
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None


def _draw_parameter_tools(layout):
    box = layout.box()
    box.operator_context = "INVOKE_DEFAULT"
    box.label(text="参数复用工具", icon="PASTEDOWN")
    box.operator("hd2pb.copy_entity_parameters", text="复制活动实体参数", icon="COPYDOWN")
    box.operator("hd2pb.paste_entity_parameters", text="粘贴到选中同类型实体", icon="PASTEDOWN")
    box.operator("hd2pb.save_preset", text="保存为 JSON 参数预设", icon="FILE_TICK")
    box.operator("hd2pb.apply_preset", text="应用同类型参数预设", icon="IMPORT")
    box.operator("hd2pb.delete_preset", text="删除用户参数预设…", icon="TRASH")


class HD2PB_PT_object_identity(Panel):
    """有效 controller 或可选择 Curve 代理的身份说明。"""

    bl_label = "HD2 物理骨编辑对象"
    bl_idname = "HD2PB_PT_object_identity"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        return _resolved(context) is not None

    def draw(self, context):
        layout = self.layout
        owner, project, _entity, _parent = ownership.resolve_controller(context.object)
        type_name = dict(
            (identifier, label)
            for identifier, label, _description in properties.ENTITY_TYPE_ITEMS
        ).get(context.object.hd2pb_entity_type, "未知实体")
        layout.label(text=f"类型：{type_name}")
        layout.label(text=f"所属骨架：{owner.name}", icon="ARMATURE_DATA")
        layout.label(text=f"项目数据版本：{project.schema_version}")
        if project.preview_error_message:
            error = layout.box()
            error.alert = True
            error.label(text="实体预览需要修复", icon="ERROR")
            error.label(text=project.preview_error_message)
        row = layout.row(align=True)
        row.operator("hd2pb.validate_project", icon="CHECKMARK")
        row.operator("hd2pb.show_validation_report", text="完整检查列表", icon="VIEWZOOM")
        row.operator("hd2pb.refresh_preview", icon="FILE_REFRESH")


class HD2PB_PT_object_project(Panel):
    """Project controller 对应的物理制作求解步长。"""

    bl_label = "项目求解设置"
    bl_idname = "HD2PB_PT_object_project"
    bl_parent_id = "HD2PB_PT_object_identity"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return _resolved(context, properties.ENTITY_PROJECT) is not None

    def draw(self, context):
        layout = self.layout
        _owner, project, _entity, _parent = ownership.resolve_controller(
            context.object, properties.ENTITY_PROJECT
        )
        layout.prop(project, "fixed_dt")


class HD2PB_PT_object_profile(Panel):
    """Profile controller 对应的完整 SpringBone 链级参数。"""

    bl_label = "弹簧参数配置"
    bl_idname = "HD2PB_PT_object_profile"
    bl_parent_id = "HD2PB_PT_object_identity"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        return _resolved(context, properties.ENTITY_PROFILE) is not None

    def draw(self, context):
        layout = self.layout
        _owner, _project, profile, _parent = ownership.resolve_controller(
            context.object, properties.ENTITY_PROFILE
        )
        layout.prop(profile, "display_name")
        layout.prop(profile, "stiffness")
        layout.prop(profile, "damping")
        layout.prop(profile, "gravity")
        layout.prop(profile, "wind")
        draw_mask_field(layout, profile, "collision_mask")
        layout.label(text="此掩码只用于新链；已有链请在骨链属性修改", icon="INFO")
        layout.label(text="Chain 引用此配置 UUID；改名称不会断开引用", icon="INFO")
        _draw_parameter_tools(layout)
        remove = layout.operator("hd2pb.remove_profile", text="删除当前参数配置", icon="TRASH")
        remove.entity_uuid = profile.uuid


class HD2PB_PT_object_chain(Panel):
    """Chain controller 对应的名称、Profile 引用和节点摘要。"""

    bl_label = "物理骨链"
    bl_idname = "HD2PB_PT_object_chain"
    bl_parent_id = "HD2PB_PT_object_identity"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        return _resolved(context, properties.ENTITY_CHAIN) is not None

    def draw(self, context):
        layout = self.layout
        _owner, project, chain, _parent = ownership.resolve_controller(
            context.object, properties.ENTITY_CHAIN
        )
        layout.prop(chain, "display_name")
        draw_mask_field(layout, chain, "group_mask")
        draw_mask_field(layout, chain, "collision_mask")
        layout.label(text="所属组=我是谁；允许组=我能碰谁（双方都须允许）", icon="INFO")
        layout.label(text="支持组 1–31；按钮编号不等于掩码；普通允许组 0=关闭")
        layout.label(text="节点默认继承；单独设置的节点可以覆盖骨链规则")
        root_is_target = bool(chain.nodes and chain.nodes[0].bone_name in
                              ownership.pose_driver_target_bones(project, _owner.data.bones))
        row = layout.row()
        row.enabled = not root_is_target or chain.root_swing
        row.prop(chain, "root_swing")
        if root_is_target:
            layout.label(text='链根已是姿态驱动目标：只允许固定根，不能开启根骨摆动', icon='INFO')
        elif chain.root_swing_warning:
            layout.label(text=chain.root_swing_warning, icon='ERROR')
        layout.prop(chain, "local_bend")
        layout.operator("hd2pb.append_tail_helper", icon="BONE_DATA").entity_uuid = chain.uuid
        current = next(
            (profile for profile in project.profiles if profile.uuid == chain.spring_profile_uuid),
            None,
        )
        box = layout.box()
        header = box.row()
        state = context.window_manager.hd2pb_preset_library
        header.prop(state, "show_chain_profile_picker", text="", emboss=False,
                    icon="TRIA_DOWN" if state.show_chain_profile_picker else "TRIA_RIGHT")
        header.label(
            text=(
                f"当前参数配置：{current.display_name}"
                if current is not None
                else "当前参数配置引用已损坏"
            ),
            icon="SETTINGS" if current is not None else "ERROR",
        )
        for profile in project.profiles if state.show_chain_profile_picker else ():
            row = box.row()
            row.enabled = profile.uuid != chain.spring_profile_uuid
            operator = row.operator(
                "hd2pb.assign_profile",
                text=f"改用：{profile.display_name}",
                icon="RADIOBUT_OFF" if row.enabled else "RADIOBUT_ON",
            )
            operator.chain_uuid = chain.uuid
            operator.profile_uuid = profile.uuid
        simulated = list(chain.nodes)[1:]
        layout.label(text=f"模拟节点数：{len(simulated)}")
        if simulated:
            layout.label(
                text=f"模拟路径：{simulated[0].bone_name} → {simulated[-1].bone_name}",
                icon="BONE_DATA",
            )
        layout.label(text="完整节点层级请在 Outliner 展开此链；这里不铺开长列表", icon="INFO")
        layout.operator("hd2pb.preview_isolate", text="隔离此骨链预览", icon="HIDE_OFF").action = "ISOLATE"
        layout.operator_context = "INVOKE_DEFAULT"
        layout.operator(
            "hd2pb.apply_limit_ramp",
            text="按最大角限/迭代应用渐变…",
            icon="IPO_EASE_IN_OUT",
        )
        layout.operator("hd2pb.remove_chain_limits", text="移除当前 / 所选骨链角限…", icon="X")
        remove = layout.operator("hd2pb.remove_chain", text="删除当前物理骨链", icon="TRASH")
        remove.entity_uuid = chain.uuid


class HD2PB_PT_object_node(Panel):
    """Node controller 对应的半径、角限和局部倍率；没有位置字段。"""

    bl_label = "链节点参数"
    bl_idname = "HD2PB_PT_object_node"
    bl_parent_id = "HD2PB_PT_object_identity"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        return _resolved_simulated_node(context) is not None

    def draw(self, context):
        layout = self.layout
        _owner, _project, node, chain = ownership.resolve_controller(
            context.object, properties.ENTITY_NODE
        )
        index = next((i for i, item in enumerate(chain.nodes) if item.uuid == node.uuid), -1)
        layout.label(text=f"所属链：{chain.display_name}", icon="CONSTRAINT_BONE")
        layout.label(text=f"绑定骨骼：{node.bone_name}（模拟节点 {index}）", icon="BONE_DATA")
        layout.label(text="弹簧目标跟随动画；角限默认对齐静止骨段，并随起点骨骼运动", icon="LOCKED")
        layout.prop(node, "radius")
        collision = layout.box()
        collision.label(text="碰撞过滤（节点球）", icon="MOD_PHYSICS")
        collision.prop(node, "override_collision_filter")
        draw_mask_field(collision, node, "group_mask", parent=chain)
        draw_mask_field(collision, node, "collision_mask", parent=chain)
        collision.label(text="所属组=我是谁；允许组=我能碰谁；双方都须允许")
        collision.label(text="31 个独立组；允许组 0=关闭碰撞；继承不表示未选组")
        collision.label(text="排除后允许穿过碰撞体；摆动与链约束仍保留", icon="INFO")
        layout.prop(node, "limit_kind")
        if node.limit_kind != "NONE":
            angle_labels = {
                "CONE": "圆锥角限半角",
                "HINGE": "铰链角限半角",
                "SPHERICAL": "主方向角限半角",
            }
            layout.prop(node, "limit_angle", text=angle_labels[node.limit_kind])
            if node.limit_kind == "SPHERICAL":
                layout.prop(node, "limit_angle2", text="正交方向角限半角")
            if node.limit_kind in {"HINGE", "SPHERICAL"}:
                layout.prop(node, "limit_axis")
            layout.separator()
            if index > 0:
                layout.label(text=f"受限骨段：{chain.nodes[index - 1].bone_name} → {node.bone_name}", icon="ORIENTATION_LOCAL")
                layout.label(text="若调整时未在视图发生变化，则调整的是驱动骨到首模拟节点的角限", icon="INFO")
                layout.label(text="需在可见性中勾选显示完整骨链与显示驱动骨角度限制", icon="INFO")
            layout.label(text="锥顶在骨段起点；旋转角限轴即可调整朝向，完整旋转会保存", icon="INFO")
        align = layout.column()
        align.operator_context = "INVOKE_DEFAULT"
        align.enabled = index > 0 and node.limit_kind != "NONE" and (
            index < len(chain.nodes) - 1 or node.limit_kind == "CONE"
        )
        align.operator("hd2pb.align_limit_to_next", text="限制对齐实际受限 Rest 骨段", icon="ORIENTATION_LOCAL")
        layout.prop(node, "stiffness_scale")
        layout.prop(node, "damping_scale")
        layout.operator("hd2pb.preview_isolate", text="隔离此节点预览", icon="HIDE_OFF").action = "ISOLATE"
        _draw_parameter_tools(layout)


class HD2PB_PT_object_collider(Panel):
    """Collider controller 对应的 attachment-local 物理参数。"""

    bl_label = "碰撞体参数"
    bl_idname = "HD2PB_PT_object_collider"
    bl_parent_id = "HD2PB_PT_object_identity"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        return _resolved(context, properties.ENTITY_COLLIDER) is not None

    def draw(self, context):
        layout = self.layout
        owner, project, collider, _parent = ownership.resolve_controller(
            context.object, properties.ENTITY_COLLIDER
        )
        layout.prop(collider, "display_name")
        layout.prop(collider, "kind")
        layout.prop(collider, "attachment_mode")
        attachment = f"骨骼：{collider.bone_name}" if collider.bone_name else "Unit 根"
        row = layout.row(align=True)
        row.label(text=f"当前吸附：{attachment}", icon="BONE_DATA")
        if collider.kind == "CAPSULE" and getattr(collider, "bone_name_b", ""):
            layout.label(
                text=f"终点跟随：{collider.bone_name_b}（制作预览与运行时一致）",
                icon="CONSTRAINT_BONE",
            )
        operator = row.operator(
            "hd2pb.set_collider_bone",
            text="修改…",
            icon="EYEDROPPER",
        )
        operator.collider_uuid = collider.uuid
        if collider.attachment_mode == "DYNAMIC":
            layout.label(text="动态模式必须吸附唯一模拟节点，且只支持球形或胶囊", icon="PHYSICS")
        else:
            layout.label(text="静态模式可清空为 Unit 根，但不能吸附模拟骨", icon="INFO")
        draw_mask_field(layout, collider, "group_mask")
        draw_mask_field(layout, collider, "collision_mask")
        layout.label(text="所属组=我是谁；允许组=我能碰谁；双方都须允许", icon="INFO")
        layout.label(text="31 个独立组；允许组 0=关闭此碰撞体")
        layout.prop(collider, "inside")
        layout.prop(collider, "a")
        if collider.kind != "SPHERE":
            layout.prop(collider, "b")
        radius = layout.column()
        radius.enabled = collider.kind != "PLANE"
        radius.prop(collider, "radius")
        if project.preview_allow_collider_transform:
            layout.label(text="试摆须显式应用才写入 A/B/半径；导出不会自动提交", icon="INFO")
            layout.label(text="修改任一权威形状参数会覆盖并重置视图变换", icon="INFO")
            row = layout.row(align=True)
            row.operator_context = "INVOKE_DEFAULT"
            row.operator("hd2pb.apply_collider_transform", text="应用试摆到物理参数", icon="CHECKMARK")
            row.operator("hd2pb.reset_collider_transform", text="还原试摆", icon="LOOP_BACK")
        else:
            layout.label(text="A/B/半径是权威物理参数；碰撞体视图变换当前锁定", icon="LOCKED")
        layout.operator("hd2pb.preview_isolate", text="隔离此碰撞体预览", icon="HIDE_OFF").action = "ISOLATE"
        _draw_parameter_tools(layout)
        remove = layout.operator("hd2pb.remove_collider", text="删除当前碰撞体", icon="TRASH")
        remove.entity_uuid = collider.uuid


def _chain_link_endpoint(project, chain_uuid):
    chain = next((item for item in project.chains if item.uuid == chain_uuid), None)
    if chain is None or len(chain.nodes) <= 1:
        return "失效骨链"
    return f"{chain.display_name} / {chain.nodes[1].bone_name}"


class HD2PB_PT_object_chain_link(Panel):
    """ChainLink Curve 的组级共同运动参数和 child 管理入口。"""

    bl_label = "链组连接参数"
    bl_idname = "HD2PB_PT_object_chain_link"
    bl_parent_id = "HD2PB_PT_object_identity"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        return _resolved(context, properties.ENTITY_CHAIN_LINK) is not None

    def draw(self, context):
        layout = self.layout
        _owner, project, link, _parent = ownership.resolve_controller(
            context.object,
            properties.ENTITY_CHAIN_LINK,
        )
        layout.prop(link, "display_name")
        endpoints = layout.box()
        endpoints.label(text="骨链 A：" + _chain_link_endpoint(project, link.chain_a_uuid))
        endpoints.label(text="骨链 B：" + _chain_link_endpoint(project, link.chain_b_uuid))
        endpoints.label(text="主连接约束两组平均物理偏移；不保存任意节点距离", icon="INFO")
        layout.prop(link, "coupling")
        layout.prop(link, "strip_constraint")
        layout.prop(link, "max_correction")
        layout.label(text=f"链间碰撞体：{len(link.collisions)} 个（从 local Joint 1 开始）")
        layout.operator(
            "hd2pb.build_link_collisions",
            text="创建或重建完整链间碰撞体",
            icon="META_CAPSULE",
        )
        layout.operator("hd2pb.preview_isolate", text="隔离此链组连接预览", icon="HIDE_OFF").action = "ISOLATE"
        _draw_parameter_tools(layout)
        remove = layout.operator("hd2pb.remove_chain_link", text="删除当前链组连接", icon="TRASH")
        remove.entity_uuid = link.uuid


class HD2PB_PT_object_link_collision(Panel):
    """LinkCollision Curve 的动态胶囊过滤与半径参数。"""

    bl_label = "链间碰撞体参数"
    bl_idname = "HD2PB_PT_object_link_collision"
    bl_parent_id = "HD2PB_PT_object_identity"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        return _resolved(context, properties.ENTITY_LINK_COLLISION) is not None

    def draw(self, context):
        layout = self.layout
        _owner, project, collision, link = ownership.resolve_controller(
            context.object,
            properties.ENTITY_LINK_COLLISION,
        )
        endpoints = layout.box()
        endpoints.label(text="骨链 A：" + _chain_link_endpoint(project, link.chain_a_uuid))
        endpoints.label(text="骨链 B：" + _chain_link_endpoint(project, link.chain_b_uuid))
        endpoints.label(
            text=f"local Joint：{int(collision.local_joint_index)}（0 是隐藏 driver，永不允许）",
            icon="BONE_DATA",
        )
        layout.prop(collision, "radius")
        draw_mask_field(layout, collision, "group_mask")
        draw_mask_field(layout, collision, "collision_mask")
        layout.label(text="链间碰撞所属组和允许组均不可为 0", icon="INFO")
        layout.label(text="胶囊两个端点每轮从两条链的当前模拟位置重建", icon="INFO")
        layout.operator("hd2pb.preview_isolate", text="隔离此链间碰撞体", icon="HIDE_OFF").action = "ISOLATE"


class HD2PB_PT_object_diagnostics(Panel):
    """损坏 controller 仍可显示中文诊断，但不能进入类型参数面板。"""

    bl_label = "HD2 物理骨编辑对象诊断"
    bl_idname = "HD2PB_PT_object_diagnostics"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        try:
            obj = context.object
            return bool(
                obj is not None
                and (
                    getattr(obj, "hd2pb_controller_schema", 0)
                    or getattr(obj, "hd2pb_preview_kind", properties.PREVIEW_NONE)
                    in {
                        properties.PREVIEW_CHAIN_CURVE,
                        properties.PREVIEW_COLLIDER_SHAPE,
                        properties.PREVIEW_CHAIN_LINK,
                        properties.PREVIEW_LINK_COLLISION,
                    }
                )
                and ownership.controller_problem(obj)
            )
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            return False

    def draw(self, context):
        layout = self.layout
        try:
            obj = context.object
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            obj = None
        problem = ownership.controller_problem(obj) if obj is not None else "当前编辑对象已经失效"
        box = layout.box()
        box.alert = True
        box.label(text="此编辑对象的身份或所有权无效", icon="ERROR")
        box.label(text=problem)
        try:
            owner = getattr(obj, "hd2pb_owner_object", None)
            owner_valid = owner is not None and owner.type == "ARMATURE"
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            owner_valid = False
        if owner_valid:
            layout.operator("hd2pb.refresh_preview", icon="FILE_REFRESH")
        layout.label(text="不要手工编辑内部 UUID；canonical 数据通常仍保存在 Armature")


CLASSES = (
    HD2PB_PT_object_identity,
    HD2PB_PT_object_project,
    HD2PB_PT_object_profile,
    HD2PB_PT_object_chain,
    HD2PB_PT_object_node,
    HD2PB_PT_object_collider,
    HD2PB_PT_object_chain_link,
    HD2PB_PT_object_link_collision,
    HD2PB_PT_object_diagnostics,
)


__all__ = ("CLASSES",)
