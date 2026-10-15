"""HD2 物理骨按 Blender 模式分开的独立 N 面板。"""

from __future__ import annotations

from bpy.types import Menu, Panel, UIList, UI_UL_list

from . import ownership, properties, motion_preview, pose_drivers
import importlib

if "_version" in globals():
    _version = importlib.reload(_version)
else:
    from ... import version as _version

VERSION = _version.VERSION
VERSION_TEXT = _version.VERSION_TEXT


def _select_button(layout, text, icon, entity_type, entity_uuid, *, selected=False):
    operator = layout.operator("hd2pb.select_controller", text=text, icon=icon, depress=selected)
    operator.entity_type = entity_type
    operator.entity_uuid = entity_uuid
    return operator


_SELECTED_ROW = 1  # UIList 保留高位作过滤标记，低位用于本次绘制的真实选择状态。
_LIST_TYPES = {
    "profiles": properties.ENTITY_PROFILE,
    "chains": properties.ENTITY_CHAIN,
    "colliders": properties.ENTITY_COLLIDER,
    "chain_links": properties.ENTITY_CHAIN_LINK,
}


def _navigation_entity(obj, project):
    """只读解析选择；不跨项目、不缓存 RNA，也不把历史列表索引当作视图选择。"""
    try:
        if (obj is None or obj.hd2pb_owner_object != project.authoring_object
                or obj.hd2pb_project_uuid != project.project_uuid):
            return None
        _owner, resolved, entity, parent = ownership.resolve_controller(obj, require_unique=False)
        if resolved != project:
            return None
        kind = obj.hd2pb_entity_type
        if kind == properties.ENTITY_NODE:
            kind, entity = properties.ENTITY_CHAIN, parent
        elif kind == properties.ENTITY_LINK_COLLISION:
            kind, entity = properties.ENTITY_CHAIN_LINK, parent
        return kind, entity.uuid
    except (ownership.HD2PBError, AttributeError, ReferenceError, RuntimeError, TypeError):
        return None


class _SelectionList:
    """每次绘制只解析一次选区；各行只读标记，避免逐行扫描预览对象。"""

    def filter_items(self, context, data, propname):
        selected = {
            key for obj in context.selected_objects
            if (key := _navigation_entity(obj, data)) is not None
        }
        items = getattr(data, propname)
        flags = UI_UL_list.filter_items_by_name(
            self.filter_name, self.bitflag_filter_item, items, "display_name",
            reverse=self.use_filter_invert,
        ) if self.filter_name else [self.bitflag_filter_item] * len(items)
        kind = _LIST_TYPES[propname]
        return [flag | (_SELECTED_ROW if (kind, item.uuid) in selected else 0)
                for item, flag in zip(items, flags)], []


def _project_from_context(context):
    # Blender 禁止在 Panel.draw()/poll() 中写 Scene ID。绘制只解析明确上下文或回退
    # Scene 记录；选择器回调和用户执行的操作符负责更新记录。投影重建删除活动对象后，
    # project_owner() 仍会回退到同一个已记录项目，避免面板退回初始选择提示。
    owner = ownership.project_owner(context)
    if owner is None or not ownership.project_initialized(owner.data.hd2pb_project):
        return owner, None
    return owner, owner.data.hd2pb_project


def _current_project_from_context(context):
    """只读判断当前上下文是否允许显示编辑子面板。"""

    owner, project = _project_from_context(context)
    if owner is None or project is None:
        return owner, None
    if (
        project.schema_version != properties.PROJECT_SCHEMA_VERSION
        or project.authoring_object != owner
        or len(ownership.armature_objects(owner.data)) != 1
        or ownership.project_uuid_conflicts(owner)
    ):
        return owner, None
    return owner, project


def _profile_by_uuid(project, uuid):
    return next((item for item in project.profiles if item.uuid == uuid), None)


def _creation_profile(project):
    """只读解析新建链配置；旧 P3 文件空值时与操作符一致地回退第一项。"""

    selected = _profile_by_uuid(project, project.new_chain_profile_uuid)
    return selected or (project.profiles[0] if project.profiles else None)


def _selected_item(project, collection_name, index_name, context=None):
    collection = getattr(project, collection_name)
    # 视图/大纲选择不会写列表索引；删除按钮必须跟随活动高亮项而非旧索引。
    if context is not None and context.active_object in context.selected_objects:
        active = _navigation_entity(context.active_object, project)
        if active is not None and active[0] == _LIST_TYPES.get(collection_name):
            return next((item for item in collection if item.uuid == active[1]), None)
    index = getattr(project, index_name)
    if 0 <= index < len(collection):
        return collection[index]
    return None


class HD2PB_MT_creation_profiles(Menu):
    """姿态模式的新建链 Profile 下拉菜单。"""

    bl_label = "新建骨链参数配置"
    bl_idname = "HD2PB_MT_creation_profiles"

    def draw(self, context):
        layout = self.layout
        owner, project = _project_from_context(context)
        if owner is None or project is None:
            layout.label(text="没有可用的物理骨项目", icon="ERROR")
            return
        selected = _creation_profile(project)
        for profile in project.profiles:
            operator = layout.operator(
                "hd2pb.set_creation_profile",
                text=profile.display_name,
                icon=(
                    "RADIOBUT_ON"
                    if selected is not None and profile.uuid == selected.uuid
                    else "RADIOBUT_OFF"
                ),
            )
            operator.profile_uuid = profile.uuid


class HD2PB_UL_profiles(_SelectionList, UIList):
    """物体模式中固定行数的 Profile 列表。"""

    bl_idname = "HD2PB_UL_profiles"

    def draw_item(
        self,
        context,
        layout,
        _data,
        item,
        _icon,
        _active_data,
        _active_propname,
        _index,
        _flt_flag=0,
    ):
        _select_button(
            layout,
            item.display_name,
            "PHYSICS",
            properties.ENTITY_PROFILE,
            item.uuid,
            selected=bool(_flt_flag & _SELECTED_ROW),
        )


class HD2PB_UL_chains(_SelectionList, UIList):
    """物体模式中固定行数的 Chain 列表。"""

    bl_idname = "HD2PB_UL_chains"

    def draw_item(
        self,
        context,
        layout,
        _data,
        item,
        _icon,
        _active_data,
        _active_propname,
        _index,
        _flt_flag=0,
    ):
        _select_button(
            layout,
            f"{item.display_name}（{len(item.nodes)} 节点）",
            "CONSTRAINT_BONE",
            properties.ENTITY_CHAIN,
            item.uuid,
            selected=bool(_flt_flag & _SELECTED_ROW),
        )


class HD2PB_UL_colliders(_SelectionList, UIList):
    """物体模式中固定行数的 Collider 列表。"""

    bl_idname = "HD2PB_UL_colliders"

    def draw_item(
        self,
        context,
        layout,
        _data,
        item,
        _icon,
        _active_data,
        _active_propname,
        _index,
        _flt_flag=0,
    ):
        kind_name = dict(
            (identifier, label)
            for identifier, label, _description in properties.COLLIDER_KIND_ITEMS
        ).get(item.kind, "未知形状")
        attachment = item.bone_name or "Unit 根"
        _select_button(
            layout,
            f"{item.display_name} · {kind_name} · {attachment}",
            "MESH_UVSPHERE",
            properties.ENTITY_COLLIDER,
            item.uuid,
            selected=bool(_flt_flag & _SELECTED_ROW),
        )


def _chain_link_endpoint_text(project, chain_uuid):
    chain = next((item for item in project.chains if item.uuid == chain_uuid), None)
    if chain is None or len(chain.nodes) <= 1:
        return "失效骨链"
    return f"{chain.display_name}/{chain.nodes[1].bone_name}"


class HD2PB_UL_chain_links(_SelectionList, UIList):
    """物体模式中固定行数的 ChainLink 导航列表。"""

    bl_idname = "HD2PB_UL_chain_links"

    def draw_item(
        self,
        context,
        layout,
        _data,
        item,
        _icon,
        _active_data,
        _active_propname,
        _index,
        _flt_flag=0,
    ):
        project = item.id_data.hd2pb_project
        endpoint_a = _chain_link_endpoint_text(project, item.chain_a_uuid)
        endpoint_b = _chain_link_endpoint_text(project, item.chain_b_uuid)
        _select_button(
            layout,
            f"{item.display_name} · {endpoint_a} ↔ {endpoint_b}",
            "CON_TRACKTO",
            properties.ENTITY_CHAIN_LINK,
            item.uuid,
            selected=bool(_flt_flag & _SELECTED_ROW),
        )


def _draw_project_header(layout, owner, project, *, selectable):
    box = layout.box()
    if selectable:
        _select_button(
            box,
            f"项目：{owner.name}",
            "ARMATURE_DATA",
            properties.ENTITY_PROJECT,
            project.project_uuid,
        )
    else:
        box.label(text=f"项目：{owner.name}", icon="ARMATURE_DATA")
    box.label(text=f"数据版本：{project.schema_version}；固定步长：{project.fixed_dt:.6f} 秒")


def _draw_owner_warning(layout, context, owner):
    instances = ownership.armature_objects(owner.data)
    conflicts = ownership.project_uuid_conflicts(owner)
    if len(instances) == 1 and not conflicts:
        return False
    warning = layout.box()
    warning.alert = True
    if len(instances) != 1:
        warning.label(
            text=f"当前 Armature 数据被 {len(instances)} 个对象共享，编辑已停止",
            icon="ERROR",
        )
    else:
        warning.label(text="检测到重复项目 UUID，编辑已停止", icon="ERROR")
    if context.mode == "OBJECT" and context.object is not None and context.object.type == "ARMATURE":
        warning.operator("hd2pb.make_project_unique", icon="DUPLICATE")
    elif context.mode == "POSE":
        switch = warning.operator(
            "hd2pb.switch_mode",
            text="切换到物体模式处理所有权",
            icon="OBJECT_DATA",
        )
        switch.target_mode = "OBJECT"
    else:
        warning.label(text="请在大纲视图选择需要独立化的骨架副本")
    warning.label(text="先独立化副本，再继续编辑或重建", icon="INFO")
    return True


def _draw_collapsible_list(
    layout,
    project,
    *,
    context=None,
    title,
    collection_name,
    index_name,
    prop_name,
    list_id,
    remove_id,
    remove_label,
    empty_hint,
):
    expanded = getattr(project, prop_name)
    row = layout.row(align=True)
    row.prop(
        project,
        prop_name,
        text=f"{title}（{len(getattr(project, collection_name))}）",
        icon="TRIA_DOWN" if expanded else "TRIA_RIGHT",
        emboss=False,
    )
    if not expanded:
        return
    collection = getattr(project, collection_name)
    if not collection:
        layout.label(text=empty_hint, icon="INFO")
        return
    layout.template_list(
        list_id,
        "",
        project,
        collection_name,
        project,
        index_name,
        rows=5,
    )
    item = _selected_item(project, collection_name, index_name, context)
    if item is not None:
        operator = layout.operator(remove_id, text=remove_label, icon="REMOVE")
        operator.entity_uuid = item.uuid


class HD2PB_PT_sidebar_object(Panel):
    """物体模式只负责项目管理、列表导航、检查和修复。"""

    bl_label = f"HD2 物理骨 · 物体模式 {VERSION_TEXT}"
    bl_idname = "HD2PB_PT_sidebar_object"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "HD2 物理骨"
    bl_context = "objectmode"

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def draw(self, context):
        layout = self.layout
        owner, project = _project_from_context(context)
        layout.prop(
            context.scene,
            "hd2pb_active_project_owner",
            text="当前物理骨项目",
            icon="ARMATURE_DATA",
        )
        if owner is None:
            layout.label(text="请选择已有项目，或选择骨架进行初始化", icon="INFO")
            return
        if project is None:
            box = layout.box()
            box.label(text=f"当前骨架：{owner.name}", icon="ARMATURE_DATA")
            box.label(text="此骨架还没有 HD2 物理骨项目")
            box.operator("hd2pb.initialize_project", icon="ADD")
            box.operator("hd2pb.convert_mmd", icon="PHYSICS")
            box.operator("hd2pb.convert_vrm", icon="IMPORT")
            return
        if _draw_owner_warning(layout, context, owner):
            return
        try:
            ownership.ensure_authoring_owner(owner)
        except ownership.HD2PBError as exc:
            box = layout.box()
            box.alert = True
            box.label(text="项目所有权或数据版本无效", icon="ERROR")
            box.label(text=str(exc))
            box.operator("hd2pb.migrate_project", icon="FILE_REFRESH")
            return

        _draw_project_header(layout, owner, project, selectable=True)
        pose_drivers.draw(layout, context, owner, project)
        layout.label(text=f"制作统计：{len(project.chains)} 条链 / "
                          f"{sum(len(c.nodes) for c in project.chains)} 个节点 / {len(project.colliders)} 个碰撞体")
        layout.label(text="建议移除无用途物理；无蒙皮骨仍可能承担驱动/碰撞", icon="INFO")
        if not project.chains and not project.colliders:
            layout.operator("hd2pb.convert_mmd", icon="PHYSICS")
            layout.operator("hd2pb.convert_vrm", icon="IMPORT")
        batch = layout.box()
        batch.operator_context = "INVOKE_DEFAULT"
        batch.label(text="角限批量工具", icon="CONSTRAINT_BONE")
        entity_type = getattr(context.object, "hd2pb_entity_type", properties.ENTITY_NONE)
        ramp = batch.column()
        ramp.enabled = entity_type == properties.ENTITY_CHAIN
        ramp.operator(
            "hd2pb.apply_limit_ramp",
            text="当前 / 所选骨链角限渐变…",
            icon="IPO_EASE_IN_OUT",
        )
        align = batch.column()
        align.enabled = entity_type in {properties.ENTITY_CHAIN, properties.ENTITY_NODE}
        align.operator(
            "hd2pb.align_limit_to_next",
            text="当前 / 所选骨链对齐受限骨段…",
            icon="ORIENTATION_LOCAL",
        )
        remove_limits = batch.column()
        remove_limits.enabled = entity_type == properties.ENTITY_CHAIN
        remove_limits.operator(
            "hd2pb.remove_chain_limits",
            text="移除当前 / 所选骨链角限…",
            icon="X",
        )
        lists = layout.box()
        lists.label(text="项目内容导航（列表默认折叠）", icon="OUTLINER")
        _draw_collapsible_list(
            lists,
            project,
            context=context,
            title="弹簧参数配置",
            collection_name="profiles",
            index_name="profile_index",
            prop_name="show_profile_list",
            list_id="HD2PB_UL_profiles",
            remove_id="hd2pb.remove_profile",
            remove_label="删除当前参数配置",
            empty_hint="还没有弹簧参数配置；请先使用下方按钮新增",
        )
        row = lists.row(align=True)
        row.operator_context = "INVOKE_DEFAULT"
        row.operator("hd2pb.add_profile", text="新增弹簧参数配置…", icon="ADD")
        _draw_collapsible_list(
            lists,
            project,
            context=context,
            title="物理骨链",
            collection_name="chains",
            index_name="chain_index",
            prop_name="show_chain_list",
            list_id="HD2PB_UL_chains",
            remove_id="hd2pb.remove_chain",
            remove_label="删除当前物理骨链",
            empty_hint="还没有物理骨链；请切换到姿态模式从骨骼创建",
        )
        _draw_collapsible_list(
            lists,
            project,
            context=context,
            title="链组连接",
            collection_name="chain_links",
            index_name="chain_link_index",
            prop_name="show_chain_link_list",
            list_id="HD2PB_UL_chain_links",
            remove_id="hd2pb.remove_chain_link",
            remove_label="删除当前链组连接",
            empty_hint="还没有链组连接；请在物体模式恰好选择两个正式骨链曲线",
        )
        link_actions = lists.column(align=True)
        link_actions.operator(
            "hd2pb.add_chain_link",
            text="从两个已选骨链创建链组连接",
            icon="CON_TRACKTO",
        )
        link_actions.operator(
            "hd2pb.build_link_collisions",
            text="为活动连接创建或重建链间碰撞体",
            icon="META_CAPSULE",
        )
        _draw_collapsible_list(
            lists,
            project,
            context=context,
            title="碰撞体",
            collection_name="colliders",
            index_name="collider_index",
            prop_name="show_collider_list",
            list_id="HD2PB_UL_colliders",
            remove_id="hd2pb.remove_collider",
            remove_label="删除当前碰撞体",
            empty_hint="还没有碰撞体；可在此创建 Unit 根形状，或到姿态模式吸附固定骨",
        )
        if project.show_collider_list:
            lists.label(text="新增 Unit 根碰撞体", icon="ADD")
            root_row = lists.row(align=True)
            for kind, text, icon in (
                ("SPHERE", "球", "MESH_UVSPHERE"),
                ("CAPSULE", "胶囊", "META_CAPSULE"),
                ("PLANE", "平面", "MESH_PLANE"),
            ):
                operator = root_row.operator(
                    "hd2pb.add_collider",
                    text=text,
                    icon=icon,
                )
                operator.kind = kind

        maintenance = layout.box()
        maintenance.label(text="检查与修复", icon="TOOL_SETTINGS")
        validate = maintenance.row()
        validate.scale_y = 1.2
        validate.operator("hd2pb.validate_project", text="检查物理骨项目", icon="CHECKMARK")
        maintenance.operator("hd2pb.refresh_preview", text="修复 controller 与实体预览", icon="FILE_REFRESH")
        maintenance.operator("hd2pb.migrate_project", icon="SORTTIME")
        maintenance.operator("hd2pb.export_v1", text="更新物理与姿态独立配套包…", icon="EXPORT")
        op = layout.operator("hd2pb.switch_mode", text="切换到姿态模式", icon="POSE_HLT")
        op.target_mode = "POSE"
        layout.label(text="详细参数请在选中 Outliner 编辑对象后查看对象属性", icon="PROPERTIES")


class HD2PB_PT_sidebar_object_preview(Panel):
    """物体模式独立的实体预览可见性、颜色、隔离和修复面板。"""

    bl_label = "实体预览与可见性"
    bl_idname = "HD2PB_PT_sidebar_object_preview"
    bl_parent_id = "HD2PB_PT_sidebar_object"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "HD2 物理骨"
    bl_context = "objectmode"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        _owner, project = _current_project_from_context(context)
        return context.mode == "OBJECT" and project is not None

    def draw(self, context):
        layout = self.layout
        _owner, project = _current_project_from_context(context)
        motion = layout.box()
        motion.label(text="同求解器动作预览", icon="PHYSICS")
        motion.prop(project, "motion_preview_show_armature")
        if motion_preview.active():
            motion.operator("hd2pb.motion_preview_stop", icon="PAUSE")
        else:
            row = motion.row(align=True)
            row.operator("hd2pb.motion_preview_start", text="当前动画", icon="PLAY").test_motion = False
            row.operator("hd2pb.motion_preview_start", text="转身 / 起停测试", icon="PLAY").test_motion = True
        if motion_preview.message():
            motion.label(text=motion_preview.message())
        if project.preview_error_message:
            error = layout.box()
            error.alert = True
            error.label(text="最近一次实体预览刷新或清理失败", icon="ERROR")
            error.label(text=project.preview_error_message)
        layout.prop(project, "preview_enabled")
        focus = layout.box()
        focus.label(text="当前项目类别专注", icon="HIDE_OFF")
        row = focus.row(align=True)
        for action, label in (("NODES", "节点"), ("LIMITS", "角限"), ("COLLIDERS", "碰撞体")):
            row.operator("hd2pb.preview_focus", text="只看" + label).action = action
        row = focus.row()
        row.enabled = bool(project.preview_focus_snapshot)
        row.operator("hd2pb.preview_focus", text="恢复专注前显示", icon="LOOP_BACK").action = "RESTORE"
        if not project.preview_enabled:
            focus.label(text="预览总开关已关闭", icon="INFO")
        visibility = layout.column()
        visibility.enabled = project.preview_enabled
        visibility.label(text="分类穿透（Blender 原生前置显示）", icon="ARMATURE_DATA")
        visibility.label(text="同层实体按空间深度显示，无固定类别覆盖顺序")
        for shown, xray in (
            ("preview_show_node_spheres", "preview_xray_node_spheres"),
            ("preview_show_nodes", "preview_xray_nodes"),
            ("preview_show_limits", "preview_xray_limits"),
            ("preview_show_chains", "preview_xray_chains"),
            ("preview_show_chain_links", "preview_xray_chain_links"),
            ("preview_show_link_collisions", "preview_xray_link_collisions"),
            ("preview_show_colliders", "preview_xray_colliders"),
        ):
            row = visibility.row(align=True)
            row.prop(project, shown)
            toggle = row.row(align=True)
            toggle.enabled = getattr(project, shown) or (
                shown == "preview_show_limits" and project.preview_show_driver_limits
            )
            toggle.prop(project, xray, text="穿透")
            if shown == "preview_show_limits":
                visibility.prop(project, "preview_show_driver_limits")
            elif shown == "preview_show_chains":
                full_chain = visibility.row()
                full_chain.enabled = project.preview_show_chains
                full_chain.prop(project, "preview_show_full_chain")
        visibility.prop(project, "preview_show_node_names")
        visibility.prop(project, "preview_show_collider_names")
        visibility.prop(project, "preview_allow_collider_transform")
        layout.separator()
        layout.prop(project, "preview_chain_color")
        layout.prop(project, "preview_node_color")
        layout.prop(project, "preview_limit_color")
        layout.prop(project, "preview_collider_color")
        layout.prop(project, "preview_chain_link_color")
        layout.prop(project, "preview_link_collision_color")
        layout.prop(project, "preview_curve_radius")
        layout.prop(project, "preview_limit_cone_height")
        layout.prop(project, "preview_plane_size")
        show_all = layout.operator("hd2pb.preview_isolate", text="显示全部启用的预览", icon="HIDE_OFF")
        show_all.action = "SHOW_ALL"
        layout.operator("hd2pb.refresh_preview", text="修复损坏的实体预览", icon="FILE_REFRESH")
        layout.operator("hd2pb.show_validation_report", text="打开完整检查列表", icon="VIEWZOOM")
        layout.label(text="参数拖动会按高频固定节拍连续刷新，无需手动重建", icon="INFO")


class HD2PB_PT_sidebar_object_clipboard(Panel):
    """物体模式独立的同类型参数 Clipboard 面板。"""

    bl_label = "物理参数剪贴板"
    bl_idname = "HD2PB_PT_sidebar_object_clipboard"
    bl_parent_id = "HD2PB_PT_sidebar_object"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "HD2 物理骨"
    bl_context = "objectmode"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        _owner, project = _current_project_from_context(context)
        return context.mode == "OBJECT" and project is not None

    def draw(self, context):
        layout = self.layout
        entity_type = getattr(context.object, "hd2pb_entity_type", properties.ENTITY_NONE)
        supported = entity_type in {properties.ENTITY_PROFILE, properties.ENTITY_NODE, properties.ENTITY_COLLIDER, properties.ENTITY_CHAIN_LINK}
        column = layout.column()
        column.operator_context = "INVOKE_DEFAULT"
        column.enabled = supported
        column.operator("hd2pb.copy_entity_parameters", text="复制活动实体参数", icon="COPYDOWN")
        column.operator("hd2pb.paste_entity_parameters", text="粘贴到选中同类型实体", icon="PASTEDOWN")
        layout.label(text="只复制参数，不复制 UUID、名称、骨绑定或链拓扑", icon="INFO")
        if not supported:
            layout.label(text="请在 Outliner 选择 Profile、Node 或 Collider controller")


class HD2PB_PT_sidebar_object_presets(Panel):
    """物体模式独立的安全 JSON 参数预设面板。"""

    bl_label = "JSON 参数预设"
    bl_idname = "HD2PB_PT_sidebar_object_presets"
    bl_parent_id = "HD2PB_PT_sidebar_object"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "HD2 物理骨"
    bl_context = "objectmode"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        _owner, project = _current_project_from_context(context)
        return context.mode == "OBJECT" and project is not None

    def draw(self, context):
        layout = self.layout
        entity_type = getattr(context.object, "hd2pb_entity_type", properties.ENTITY_NONE)
        supported = entity_type in {properties.ENTITY_PROFILE, properties.ENTITY_NODE, properties.ENTITY_COLLIDER, properties.ENTITY_CHAIN_LINK}
        _owner, project = _current_project_from_context(context)
        column = layout.column()
        column.operator_context = "INVOKE_DEFAULT"
        column.enabled = supported
        column.operator("hd2pb.save_preset", text="保存当前参数为预设…", icon="FILE_TICK")
        row = layout.row(align=True)
        row.operator_context = "INVOKE_DEFAULT"
        row.operator("hd2pb.import_preset", text="导入 JSON 预设…", icon="IMPORT")
        row.operator("hd2pb.open_preset_directory", text="打开预设文件夹", icon="FILE_FOLDER")
        for kind, field, label in (("PROFILE", "preset_profile", "弹簧参数"),
                                   ("NODE", "preset_node", "节点参数"),
                                   ("COLLIDER", "preset_collider", "碰撞体参数"),
                                   ("CHAIN_LINK", "preset_chain_link", "链连接数值")):
            box = layout.box()
            box.label(text=f"{label}预设")
            state = context.window_manager.hd2pb_preset_library
            box.prop(state, field, text="")
            name = getattr(state, field)
            valid = name not in {"", "__NONE__", "__ERROR__"}
            row = box.row(align=True)
            row.operator_context = "INVOKE_DEFAULT"
            apply_row = row.row(align=True)
            apply_row.enabled = valid and entity_type == kind
            apply = apply_row.operator("hd2pb.apply_preset", text=f"应用{label}预设…", icon="CHECKMARK")
            apply.selected_name, apply.entity_type = name, kind
            delete_row = row.row(align=True)
            delete_row.enabled = valid
            remove = delete_row.operator("hd2pb.delete_preset", text="", icon="TRASH")
            remove.selected_name, remove.entity_type = name, kind
            if kind == "PROFILE":
                from . import chain_profile_ops
                chain_profile_ops.draw_panel(layout, context)
        if not supported:
            layout.label(text="JSON 预设需选对应实体；骨链配置可应用到所选骨链")


class HD2PB_PT_sidebar_pose(Panel):
    """姿态模式只放从骨骼创建拓扑的高频工具。"""

    bl_label = f"HD2 物理骨 · 姿态模式 {VERSION_TEXT}"
    bl_idname = "HD2PB_PT_sidebar_pose"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "HD2 物理骨"
    bl_context = "posemode"

    @classmethod
    def poll(cls, context):
        return context.mode == "POSE"

    def draw(self, context):
        layout = self.layout
        owner, project = _project_from_context(context)
        if owner is None or owner.type != "ARMATURE":
            layout.label(text="请选择处于姿态模式的骨架对象", icon="INFO")
            return
        if project is None:
            layout.label(text="请先在物体模式初始化 HD2 物理骨项目", icon="ERROR")
            op = layout.operator("hd2pb.switch_mode", text="切换到物体模式", icon="OBJECT_DATA")
            op.target_mode = "OBJECT"
            return
        if _draw_owner_warning(layout, context, owner):
            return
        try:
            ownership.ensure_authoring_owner(owner)
        except ownership.HD2PBError as exc:
            box = layout.box()
            box.alert = True
            box.label(text="项目所有权或数据版本无效", icon="ERROR")
            box.label(text=str(exc))
            switch = box.operator(
                "hd2pb.switch_mode",
                text="切换到物体模式检查项目",
                icon="OBJECT_DATA",
            )
            switch.target_mode = "OBJECT"
            return

        # 姿态模式中的标题必须只读；若这里选择 Project controller，Blender 会为了
        # 激活 Empty 自动退出 Pose Mode，破坏连续建链工作流。
        _draw_project_header(layout, owner, project, selectable=False)
        pose_drivers.draw(layout, context, owner, project)

        bone_box = layout.box()
        bone_box.label(text="当前活动骨", icon="BONE_DATA")
        active = getattr(context, "active_pose_bone", None)
        selected_pose_bones = tuple(
            getattr(context, "selected_pose_bones", None) or ()
        )
        if active is None:
            bone_box.label(text="请选择一根骨骼；快捷碰撞体可单选或双选", icon="INFO")
        else:
            bone_box.label(text=f"{active.name}（长度 {active.length:.4f}）")

        tools = layout.box()
        tools.label(text="骨链工具", icon="CONSTRAINT_BONE")
        profile = _creation_profile(project)
        row = tools.row(align=True)
        row.label(text="新建骨链参数配置")
        row.menu(
            "HD2PB_MT_creation_profiles",
            text=profile.display_name if profile else "请选择配置",
            icon="PHYSICS",
        )
        tools.prop(project, "new_chain_auto_tail")
        create = tools.row()
        create.scale_y = 1.25
        create.enabled = (
            active is not None
            and len(selected_pose_bones) == 1
            and active == selected_pose_bones[0]
        )
        create.operator("hd2pb.create_chain", text="从活动骨到末端创建骨链", icon="ADD")
        advanced = tools.row()
        advanced.enabled = bool(selected_pose_bones)
        advanced.operator(
            "hd2pb.create_chain_from_selection",
            text="从明确选中路径创建（高级）",
            icon="CONSTRAINT_BONE",
        )
        tools.label(
            text="主按钮：单选起始骨，沿唯一子级到末端",
            icon="INFO",
        )
        tools.label(text="遇到分叉会拒绝")
        tools.label(
            text="有父骨：父骨是锚点，选中骨开始模拟",
            icon="LOCKED",
        )
        tools.label(
            text="无父骨会拒绝创建；固定锚点必须来自起始骨父级",
            icon="ERROR",
        )

        collider_box = layout.box()
        collider_box.label(text="碰撞体工具", icon="MESH_UVSPHERE")
        row = collider_box.row(align=True)
        row.prop(project, "pose_collider_kind", text="形状")
        create_collider = collider_box.row()
        create_collider.scale_y = 1.2
        create_collider.enabled = (
            active is not None
            and 1 <= len(selected_pose_bones) <= 2
            and active in selected_pose_bones
        )
        collider_operator = create_collider.operator(
            "hd2pb.create_collider_from_bone",
            text="从选中固定骨创建碰撞体",
            icon="ADD",
        )
        collider_operator.kind = project.pose_collider_kind
        dynamic = collider_box.row()
        dynamic.scale_y = 1.2
        dynamic.enabled = bool(
            active is not None
            and len(selected_pose_bones) == 1
            and active == selected_pose_bones[0]
            and project.pose_collider_kind in {"SPHERE", "CAPSULE"}
        )
        dynamic_operator = dynamic.operator(
            "hd2pb.create_dynamic_collider_from_bone",
            text="从活动模拟骨创建动态碰撞体",
            icon="PHYSICS",
        )
        dynamic_operator.kind = (
            project.pose_collider_kind
            if project.pose_collider_kind in {"SPHERE", "CAPSULE"}
            else "SPHERE"
        )
        collider_box.label(text="静态入口支持一或两根固定骨；双选时自动创建两端胶囊", icon="INFO")
        collider_box.label(text="动态碰撞体只支持球形或胶囊")

        extra = layout.box()
        extra.label(text="额外工具", icon="TOOL_SETTINGS")
        rename = extra.row()
        rename.enabled = (
            active is not None
            and len(selected_pose_bones) == 1
            and active == selected_pose_bones[0]
        )
        rename.operator("hd2pb.rename_bone_chain", text="批量重命名子级 Bone Chain", icon="SORTALPHA")

        op = layout.operator("hd2pb.switch_mode", text="切换到物体模式", icon="OBJECT_DATA")
        op.target_mode = "OBJECT"


CLASSES = (
    HD2PB_MT_creation_profiles,
    HD2PB_UL_profiles,
    HD2PB_UL_chains,
    HD2PB_UL_colliders,
    HD2PB_UL_chain_links,
    HD2PB_PT_sidebar_object,
    HD2PB_PT_sidebar_object_preview,
    HD2PB_PT_sidebar_object_clipboard,
    HD2PB_PT_sidebar_object_presets,
    HD2PB_PT_sidebar_pose,
)


__all__ = ("CLASSES",)
