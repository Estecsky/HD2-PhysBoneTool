"""HD2 物理骨的持久 RNA 定义。"""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from .blender_lifecycle import register_classes, unregister_classes
from . import pose_driver_properties


PROJECT_SCHEMA_VERSION = 3


def _update_pose_object_markers(self, context):
    from . import pose_overlay
    pose_overlay.tag_redraw(self, context)


def _library_items(entity_type):
    # 延迟导入，避免 RNA 定义与操作符模块循环依赖。
    from .operators import typed_preset_items
    return typed_preset_items(entity_type)


def _profile_library_items(_self, _context):
    return _library_items("PROFILE")


def _node_library_items(_self, _context):
    return _library_items("NODE")


def _collider_library_items(_self, _context):
    return _library_items("COLLIDER")


def _library_selection_get(entity_type):
    def get_selection(self):
        rows = _library_items(entity_type)
        selected = self.get("_preset_name_" + entity_type, "")
        # 目录可由文件管理器更新；默认/已删除/失效选择自动显示首项，绘制不写 RNA。
        return next((row[3] for row in rows if row[0] == selected), rows[0][3])
    return get_selection


def _library_selection_set(entity_type):
    def set_selection(self, value):
        rows = _library_items(entity_type)
        self["_preset_name_" + entity_type] = next(
            (row[0] for row in rows if row[3] == value), rows[0][0])
    return set_selection


CONTROLLER_SCHEMA_VERSION = 1
PREVIEW_SCHEMA_VERSION = 1
# Blender 4.x 的 Empty Display Size 硬下限。Node Empty 与 GN 半径球统一
# 使用此显示下限，保证两者在任何合法 canonical radius 下都完全重合。
NODE_DISPLAY_MIN_RADIUS = 1.0e-4

# Collection 使用 IDProperty 保存 owner Object 指针，而不是名称。这样 Armature/Object
# 改名不会改变投影所有权，清理时也不需要对名称做任何猜测。
COLLECTION_SCHEMA_KEY = "hd2pb_collection_schema"
COLLECTION_PROJECT_KEY = "hd2pb_project_uuid"
COLLECTION_OWNER_KEY = "hd2pb_owner_object"
COLLECTION_ROLE_KEY = "hd2pb_collection_role"
COLLECTION_ENTITY_KEY = "hd2pb_entity_uuid"
COLLECTION_DISPLAY_NAME_KEY = "hd2pb_collection_display_name"

ENTITY_NONE = "NONE"
ENTITY_PROJECT = "PROJECT"
ENTITY_PROFILE = "PROFILE"
ENTITY_CHAIN = "CHAIN"
ENTITY_NODE = "NODE"
ENTITY_COLLIDER = "COLLIDER"
ENTITY_CHAIN_LINK = "CHAIN_LINK"
ENTITY_LINK_COLLISION = "LINK_COLLISION"

PREVIEW_NONE = "NONE"
PREVIEW_CHAIN_CURVE = "CHAIN_CURVE"
PREVIEW_NODE_RADIUS = "NODE_RADIUS"
PREVIEW_NODE_LIMIT = "NODE_LIMIT"
PREVIEW_NODE_LIMIT_AXIS = "NODE_LIMIT_AXIS"
PREVIEW_COLLIDER_SHAPE = "COLLIDER_SHAPE"
PREVIEW_CHAIN_LINK = "CHAIN_LINK"
PREVIEW_LINK_COLLISION = "LINK_COLLISION"
PREVIEW_COLLIDER_ENDPOINT = "COLLIDER_ENDPOINT"

ENTITY_TYPE_ITEMS = (
    (ENTITY_NONE, "不是物理骨编辑对象", "普通 Blender 对象，不属于 HD2 物理骨编辑投影"),
    (ENTITY_PROJECT, "物理骨项目", "整套 Armature 物理骨数据的项目入口"),
    (ENTITY_PROFILE, "弹簧参数配置", "可被多条链复用的刚度、阻尼、重力和风参数"),
    (ENTITY_CHAIN, "物理骨链", "一条沿直接父子骨路径建立的 SpringBone 模拟链"),
    (ENTITY_NODE, "链节点", "绑定到骨骼、可在属性栏编辑的模拟节点"),
    (ENTITY_COLLIDER, "碰撞体", "绑定到 Unit 根或骨骼的球、胶囊或平面碰撞体"),
    (ENTITY_CHAIN_LINK, "链组连接", "让两个物理骨链组共享平均物理偏移的组级连接"),
    (ENTITY_LINK_COLLISION, "链间碰撞体", "主链组连接下同序模拟节点之间的动态胶囊碰撞体"),
)

PREVIEW_KIND_ITEMS = (
    (PREVIEW_NONE, "不是物理骨预览", "普通 Blender 对象，不属于 HD2 物理骨实体预览"),
    (PREVIEW_CHAIN_CURVE, "骨链曲线", "按骨节点顺序连接的 Chain/Group 彩色曲线"),
    (PREVIEW_NODE_RADIUS, "节点半径", "以绑定骨骼头部为中心的 Node 碰撞半径球"),
    (PREVIEW_NODE_LIMIT, "节点角限", "使用 HD2 Cone/Hinge/Spherical 数学生成的角限边界"),
    (PREVIEW_NODE_LIMIT_AXIS, "角限旋转轴", "可选的角限轴 Empty；位置与缩放锁定，仅用于旋转预览范围"),
    (PREVIEW_COLLIDER_SHAPE, "碰撞体形状", "Sphere、Capsule 或 Plane 的彩色实体预览"),
    (PREVIEW_CHAIN_LINK, "链组连接曲线", "显示两个骨链组首个模拟节点之间的主连接"),
    (PREVIEW_LINK_COLLISION, "链间碰撞胶囊", "显示主连接下同序模拟节点之间的动态胶囊"),
    (PREVIEW_COLLIDER_ENDPOINT, "碰撞体端点名称", "胶囊端点的吸附骨骼名称 Empty；不参与导出"),
)

LIMIT_KIND_ITEMS = (
    ("NONE", "无限制", "不限制摆动角度；仍保持相邻节点的静止段长度"),
    ("CONE", "圆锥限制", "围绕当前动画父子段方向，用一个半角限制任意方向摆动"),
    ("HINGE", "铰链限制", "以父骨局部限制轴作为铰链平面法线，并用主半角限制平面内摆动"),
    ("SPHERICAL", "椭圆球面限制", "把父骨局部限制轴投影为主方向，用两个半角限制主/正交摆动"),
)

REFERENCE_MODE_ITEMS = (
    (
        "ANIMATED_SEGMENT",
        "跟随当前动画骨段",
        "每个求解子步使用当前动画中的父骨到本节点方向；这是 HD2PHY1 的默认动态参考方向",
    ),
    (
        "PARENT_REST",
        "父骨 Rest 方向",
        "保存父骨 Rest 姿态中的入段方向，随父骨动画变换，但不会被本节点的模拟结果带偏",
    ),
    (
        "EXPLICIT_LOCAL",
        "显式局部方向",
        "使用下面填写的父骨局部单位方向作为角限中心；适合手动定义圆锥或铰链参考方向",
    ),
    (
        "EXPLICIT_FRAME",
        "显式参考坐标架",
        "使用下面的父骨局部四元数坐标架；坐标架的 +Y 是角限前向，+X/+Z 是稳定切向轴",
    ),
)

COLLIDER_KIND_ITEMS = (
    ("SPHERE", "球形", "由 attachment-local 中心 a 和半径定义的球体"),
    ("CAPSULE", "胶囊", "由 attachment-local 起点 a、终点 b 和半径定义的胶囊"),
    ("PLANE", "无限平面", "由 attachment-local 点 a 和法线 b 定义的无限平面；半径不参与计算"),
)

COLLIDER_ATTACHMENT_ITEMS = (
    ("STATIC", "静态吸附", "吸附 Unit 根或非模拟骨骼；运行时不接受模拟反作用"),
    ("DYNAMIC", "动态吸附", "仅吸附唯一模拟节点；每轮从当前模拟姿态重建球形或胶囊"),
)


def _queue_preview_update(item, entity_type, aspect):
    """RNA update 只排队，不在属性回调中直接重建 Blender 几何。"""

    try:
        from . import preview

        preview.mark_rna_dirty(item, entity_type, aspect)
    except Exception as exc:
        # 属性拖动不能因为可重建的 preview 故障而破坏 canonical 写入。
        print(f"HD2 物理骨预览排队失败：{type(exc).__name__}: {exc}")


def _update_project_style(item, _context):
    _queue_preview_update(item, ENTITY_PROJECT, "STYLE")


def _update_project_plane_geometry(item, _context):
    _queue_preview_update(item, ENTITY_PROJECT, "PLANE_GEOMETRY")


def _update_project_limit_preview(item, _context):
    _queue_preview_update(item, ENTITY_PROJECT, "LIMIT_PREVIEW")


def _filter_scene_project_owner(scene, obj):
    """活动项目选择器只列出当前 Scene 中已初始化的 Armature 项目。"""

    try:
        if obj is None or obj.type != "ARMATURE":
            return False
        if scene.objects.get(obj.name) != obj:
            return False
        project = getattr(obj.data, "hd2pb_project", None)
        return bool(
            project is not None
            and project.schema_version > 0
            and project.project_uuid
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def _update_scene_project_owner(scene, _context):
    """把项目 Object 指针和稳定 UUID 配对，供失效检查使用。"""

    project_uuid = ""
    try:
        owner = scene.hd2pb_active_project_owner
        project = (
            getattr(owner.data, "hd2pb_project", None)
            if owner is not None and owner.type == "ARMATURE"
            else None
        )
        if project is not None and project.schema_version > 0:
            project_uuid = project.project_uuid
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        pass
    try:
        scene.hd2pb_active_project_uuid = project_uuid
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        pass


def _update_profile_name(item, _context):
    _queue_preview_update(item, ENTITY_PROFILE, "LABEL")


def _update_chain_name(item, _context):
    _queue_preview_update(item, ENTITY_CHAIN, "LABEL")


def _update_chain_root_swing(item, _context):
    """UI 修改不允许把衣物目标锚变成模拟骨；批量恢复留给 canonical 校验。"""
    from . import ownership, preview

    # Import/restore transactions must retain the requested value so their
    # canonical gate reports invalid data instead of silently dropping a flag.
    if getattr(preview, '_SUSPEND_DEPTH', 0):
        return
    item.root_swing_warning = ''
    armature = item.id_data
    project = getattr(armature, 'hd2pb_project', None)
    if item.root_swing and item.nodes and project is not None:
        root_name = item.nodes[0].bone_name
        if root_name in ownership.pose_driver_target_bones(project, armature.bones):
            message = (f'骨骼“{root_name}”是衣物姿态目标，只能作为固定链根；'
                       '不能启用根骨摆动（禁用的姿态规则仍占用目标）')
            item.root_swing = False
            item.root_swing_warning = message
            print('HD2 物理骨：' + message)
    _queue_preview_update(item, ENTITY_CHAIN, "LIMIT")


def _update_node_radius(item, _context):
    _queue_preview_update(item, ENTITY_NODE, "RADIUS")


def _update_node_limit(item, _context):
    _queue_preview_update(item, ENTITY_NODE, "LIMIT")


def _update_node_limit_kind(item, _context):
    # A user first enabling a limit gets the same outgoing-Rest alignment as
    # the explicit Align button. Bulk restore/import must retain authored axes.
    from . import preview
    if preview._SUSPEND_DEPTH == 0:
        if item.limit_kind == 'NONE':
            item.reference_mode = 'ANIMATED_SEGMENT'
        elif item.reference_mode == 'ANIMATED_SEGMENT':
            from .operators import _auto_align_new_limit
            _auto_align_new_limit(item)
    _update_node_limit(item, _context)


def _update_node_reference(item, _context):
    _queue_preview_update(item, ENTITY_NODE, "REFERENCE")


def _update_collider_name(item, _context):
    _queue_preview_update(item, ENTITY_COLLIDER, "LABEL")


def _update_collider_geometry(item, _context):
    _queue_preview_update(item, ENTITY_COLLIDER, "GEOMETRY")


def _update_collider_points(item, _context):
    _queue_preview_update(item, ENTITY_COLLIDER, "COLLIDER_POINTS")


def _update_collider_radius(item, _context):
    _queue_preview_update(item, ENTITY_COLLIDER, "RADIUS")


def _update_chain_link(item, _context):
    _queue_preview_update(item, ENTITY_CHAIN_LINK, "GEOMETRY")


def _update_link_collision(item, _context):
    _queue_preview_update(item, ENTITY_LINK_COLLISION, "GEOMETRY")


class HD2PB_Profile(PropertyGroup):
    """一组可复用的 SpringBone 链级参数。"""

    uuid: StringProperty(
        name="配置 UUID",
        description="弹簧参数配置的稳定内部身份；复制显示名称不会改变此值",
        options={"HIDDEN"},
    )
    display_name: StringProperty(
        name="配置名称",
        default="默认弹簧参数",
        description="给制作者阅读的名称；Chain 实际通过 UUID 引用，不依赖这个名称",
        update=_update_profile_name,
    )
    stiffness: FloatProperty(
        name="刚度（Stiffness）",
        default=8.0,
        min=0.0,
        max=1000.0,
        precision=3,
        description=(
            "弹簧把节点拉向当前动画姿态给出的目标位置和父子段方向的强度。值越大跟随越快、"
            "动作越硬；值越小越柔软。"
            "这是每秒尺度的求解参数，不是 Blender 刚体质量"
        ),
    )
    damping: FloatProperty(
        name="阻尼（Damping）",
        default=0.25,
        min=0.0,
        max=1.0,
        precision=3,
        description=(
            "每个固定步长抑制已有速度的比例。0 表示不额外消散，接近 1 会很快停止摆动；"
            "过低可能持续抖动，过高会显得迟钝"
        ),
    )
    gravity: FloatVectorProperty(
        name="重力加速度（Gravity）",
        size=3,
        default=(0.0, 0.0, -9.81),
        min=-10000.0,
        max=10000.0,
        subtype="ACCELERATION",
        description=(
            "求解器世界坐标中的恒定加速度，XYZ 单位为模型长度/秒²。默认 Z=-9.81；"
            "它不会读取 Blender 场景重力"
        ),
    )
    wind: FloatVectorProperty(
        name="恒定风加速度（Wind）",
        size=3,
        default=(0.0, 0.0, 0.0),
        min=-10000.0,
        max=10000.0,
        subtype="ACCELERATION",
        description=(
            "额外施加在整条链上的恒定世界坐标加速度。HD2PHY1 当前不会自动生成阵风；"
            "三个分量都为 0 时表示无风"
        ),
    )
    collision_mask: IntProperty(
        name="新链默认允许组（Collision Mask）",
        default=0x7FFFFFFF,
        min=0,
        max=0x7FFFFFFF,
        description=(
            "仅在新建链时复制到链的碰撞掩码；修改此值不影响已有链。"
            "单组用 1/2/4/8，组合相加；0 表示新链默认关闭碰撞"
        ),
    )


class HD2PB_Node(PropertyGroup):
    """链中的一个骨绑定节点；P3 不允许存储任意位置。"""

    uuid: StringProperty(
        name="节点 UUID",
        description="链节点的稳定内部身份",
        options={"HIDDEN"},
    )
    bone_name: StringProperty(
        name="绑定骨骼",
        description=(
            "节点绑定的骨架（Armature）骨名。静止姿态（Rest Pose）只用于建立绑定、段长和退化预检；"
            "运行时目标位置与角限中心来自当前动画父子段，不能通过 3D 视图移动编辑对象修改"
        ),
    )
    radius: FloatProperty(
        name="节点碰撞半径（Radius）",
        default=0.035,
        min=0.0,
        max=10.0,
        precision=4,
        subtype="DISTANCE",
        description=(
            "当前模拟节点参与碰撞时使用的球半径，单位为模型长度；修改后球形选择代理和"
            "Curve + Geometry Nodes 半径实体会自动同步"
        ),
        update=_update_node_radius,
    )
    limit_kind: EnumProperty(
        name="角度限制类型（Limit Kind）",
        items=LIMIT_KIND_ITEMS,
        default="NONE",
        description="限制本节点相对当前动画父子段方向的摆动范围；静止姿态只用于绑定和退化预检",
        update=_update_node_limit_kind,
    )
    limit_angle: FloatProperty(
        name="主限制半角（Limit Angle）",
        default=0.5235987755982988,
        min=0.0,
        max=3.141592653589793,
        subtype="ANGLE",
        description=(
            "圆锥限制的统一半角、铰链平面内半角，或椭圆球面主方向半角。0 表示不允许偏离，"
            "180° 表示对应方向基本不限制"
        ),
        update=_update_node_limit,
    )
    limit_angle2: FloatProperty(
        name="第二限制半角（Limit Angle 2）",
        default=0.5235987755982988,
        min=0.0,
        max=3.141592653589793,
        subtype="ANGLE",
        description=(
            "仅椭圆球面限制使用的正交方向半角；主方向由限制轴运行时投影得到，其他类型忽略此值"
        ),
        update=_update_node_limit,
    )
    limit_axis: FloatVectorProperty(
        name="限制轴（Limit Axis）",
        size=3,
        default=(1.0, 0.0, 0.0),
        min=-10000.0,
        max=10000.0,
        subtype="XYZ",
        description=(
            "父骨局部空间中的方向轴。铰链把它作为运动平面法线；椭圆球面会在运行时把它投影为"
            "主方向。两种限制都要求非零；它不是编辑对象旋转，也不会从视图变换读取"
        ),
        update=_update_node_limit,
    )
    reference_mode: EnumProperty(
        name="角限参考方向模式",
        items=REFERENCE_MODE_ITEMS,
        default="ANIMATED_SEGMENT",
        description=(
            "HD2PHY1 角限中心方向来源。固定驱动节点和无限制节点只能使用“跟随当前动画骨段”；"
            "选择显式方向或坐标架后，必须确认方向位于当前节点父骨局部空间"
        ),
        update=_update_node_reference,
    )
    reference_direction: FloatVectorProperty(
        name="显式参考方向（父骨局部）",
        size=3,
        default=(0.0, 1.0, 0.0),
        min=-1.0,
        max=1.0,
        subtype="XYZ",
        description=(
            "仅用于“显式局部方向”模式。填写父骨局部坐标中的方向向量，导出时会自动单位化；"
            "长度接近零或包含非有限值会拒绝导出"
        ),
        update=_update_node_reference,
    )
    reference_frame: FloatVectorProperty(
        name="显式参考坐标架（四元数 XYZW）",
        size=4,
        default=(0.0, 0.0, 0.0, 1.0),
        min=-1.0,
        max=1.0,
        subtype="QUATERNION",
        description=(
            "仅用于“显式参考坐标架”模式。按 XYZW 填写父骨局部主动旋转四元数，+Y 为前向轴；"
            "导出时会规范化并固定 q/-q 符号"
        ),
        update=_update_node_reference,
    )
    override_collision_filter: BoolProperty(
        name="单独设置碰撞",
        description=("关闭时实时继承所属骨链的两个掩码；开启后使用下面的节点掩码。"
                     "作用于本节点碰撞球，不改变独立碰撞体或链组连接设置"),
        default=False,
    )
    group_mask: IntProperty(
        name="节点所属组（Group Mask）",
        description="我属于哪些组：单组用 1/2/4/8，组合相加，如 3=1+2；对方允许组必须包含其中一组，不能为 0",
        default=1, min=1, max=0x7FFFFFFF,
    )
    collision_mask: IntProperty(
        name="节点允许碰撞的组（Collision Mask）",
        description=("我允许碰哪些组：仅与掩码包含的对方组计算碰撞，且对方也必须允许我的所属组。"
                     "0=关闭本节点及入段的碰撞，不关闭摆动/长度约束；排除后允许穿过该碰撞体"),
        default=0x7FFFFFFF, min=0, max=0x7FFFFFFF,
    )
    stiffness_scale: FloatProperty(
        name="节点刚度倍率",
        default=1.0,
        min=0.0,
        max=100.0,
        precision=3,
        description="乘在所属弹簧参数配置刚度上的局部倍率；1 表示不改变，0 表示该节点不回正",
    )
    damping_scale: FloatProperty(
        name="节点阻尼倍率",
        default=1.0,
        min=0.0,
        max=100.0,
        precision=3,
        description="乘在所属弹簧参数配置阻尼上的局部倍率；1 表示不改变",
    )


class HD2PB_Chain(PropertyGroup):
    """一条直接父子路径及其 Profile 引用。"""

    uuid: StringProperty(
        name="链 UUID",
        description="物理骨链的稳定内部身份",
        options={"HIDDEN"},
    )
    display_name: StringProperty(
        name="链名称",
        default="物理骨链",
        description="写入 HD2PHY1 的链名称；必须唯一，UTF-8 编码后不能超过 63 字节",
        update=_update_chain_name,
    )
    spring_profile_uuid: StringProperty(
        name="弹簧参数配置 UUID",
        description="本链引用的弹簧参数配置稳定身份；请通过面板按钮选择，不要手改 UUID",
        options={"HIDDEN"},
    )
    group_mask: IntProperty(
        name="骨链所属组（Group Mask）",
        default=1,
        min=1,
        max=0x7FFFFFFF,
        description="我属于哪些组：作为未单独设置节点的默认所属组。单组用 1/2/4/8，组合相加，如 3=1+2；不能为 0",
    )
    collision_mask: IntProperty(
        name="骨链允许碰撞的组（Collision Mask）",
        default=0x7FFFFFFF,
        min=0,
        max=0x7FFFFFFF,
        description=("我允许碰哪些组：未单独设置的节点继承此值，且双方都允许对方所属组才碰撞。"
                     "0=关闭继承节点的碰撞；单独设置的节点不受此值控制。不同数字不代表一定互斥"),
    )
    root_swing: BoolProperty(
        name="根位置固定、根骨可摆动",
        default=False,
        description="首节点放在发根/衣摆根等自定义骨：位置跟随动画，旋转由下一节点带动；末端继承上一骨段摆动。不能选游戏原生头/身体骨或衣物姿态目标",
        update=_update_chain_root_swing,
    )
    root_swing_warning: StringProperty(
        name='根骨摆动检查提示',
        description='记录姿态目标与根骨摆动冲突的最近一次检查结果',
        options={'HIDDEN', 'SKIP_SAVE'},
    )
    local_bend: BoolProperty(
        name="逐节局部弯曲",
        default=False,
        description="弹性平衡和角度限制跟随上一段的模拟方向，适用于长发、飘带和衣摆；关闭则保留全局动画参考",
    )
    nodes: CollectionProperty(
        name="链节点",
        type=HD2PB_Node,
        description="按固定驱动节点到末端模拟骨顺序保存的直接父子节点列表",
    )


class HD2PB_Collider(PropertyGroup):
    """HD2PHY1 的静态骨绑定碰撞体。"""

    uuid: StringProperty(
        name="碰撞体 UUID",
        description="碰撞体的稳定内部身份",
        options={"HIDDEN"},
    )
    display_name: StringProperty(
        name="碰撞体名称",
        default="碰撞体",
        description="写入 HD2PHY1 的碰撞体名称；必须唯一，UTF-8 编码后不能超过 63 字节",
        update=_update_collider_name,
    )
    kind: EnumProperty(
        name="碰撞体形状",
        items=COLLIDER_KIND_ITEMS,
        default="SPHERE",
        description="运行时使用的基础碰撞形状；修改后实体预览会高频自动刷新",
        update=_update_collider_geometry,
    )
    attachment_mode: EnumProperty(
        name="吸附模式",
        items=COLLIDER_ATTACHMENT_ITEMS,
        default="STATIC",
        description=(
            "静态碰撞体只能吸附 Unit 根或非模拟骨；动态碰撞体只能吸附唯一模拟节点，"
            "且不支持无限平面"
        ),
        update=_update_collider_geometry,
    )
    bone_name: StringProperty(
        name="吸附骨骼",
        description=(
            "碰撞体跟随的非模拟骨骼；留空表示跟随 Unit 根。HD2PHY1 不允许碰撞体跟随"
            "正在模拟的骨骼"
        ),
    )
    bone_name_b: StringProperty(
        name="胶囊终点骨骼",
        default="",
        options={"HIDDEN"},
        description=(
            "胶囊终点跟随的第二根固定骨；预览、导出和游戏分别按两端骨骼变换。"
            "留空保持单骨胶囊。制作数据的 A/B 仍以起点骨 Rest 局部空间编辑"
        ),
    )
    group_mask: IntProperty(
        name="碰撞体所属组（Group Mask）",
        default=1,
        min=1,
        max=0x7FFFFFFF,
        description="我属于哪些组：单组用 1/2/4/8，组合相加，如 3=1+2；节点允许组需包含其中一组，不能为 0",
    )
    collision_mask: IntProperty(
        name="碰撞体允许碰撞的组（Collision Mask）",
        default=0x7FFFFFFF,
        min=0,
        max=0x7FFFFFFF,
        description="我允许碰哪些节点所属组：双方都允许对方所属组才碰撞。0=关闭此碰撞体的接触，不删除碰撞体",
    )
    inside: BoolProperty(
        name="保持在内部（Inside）",
        default=False,
        description=(
            "关闭时把节点保持在球/胶囊外侧；开启时把节点限制在球/胶囊内部。对平面则翻转"
            "允许的半空间。内部球/胶囊半径不能小于受影响节点半径"
        ),
        update=_update_collider_points,
    )
    a: FloatVectorProperty(
        name="局部点 A（Offset / Start）",
        size=3,
        default=(0.0, 0.0, 0.0),
        min=-10000.0,
        max=10000.0,
        subtype="TRANSLATION",
        description=(
            "相对吸附骨 Rest 头部或 Unit 根的局部坐标：球心、胶囊起点或平面上的一点"
        ),
        update=_update_collider_points,
    )
    b: FloatVectorProperty(
        name="局部点 B / 法线",
        size=3,
        default=(0.0, 0.2, 0.0),
        min=-10000.0,
        max=10000.0,
        subtype="XYZ",
        description=(
            "胶囊的局部终点，或平面的局部法线。球形会忽略该值；平面法线不能为零"
        ),
        update=_update_collider_points,
    )
    radius: FloatProperty(
        name="碰撞体半径（Radius）",
        default=0.1,
        min=0.0,
        max=10.0,
        precision=4,
        subtype="DISTANCE",
        description="球形或胶囊的半径，单位为模型长度；平面会忽略该值",
        update=_update_collider_radius,
    )


class HD2PB_LinkCollision(PropertyGroup):
    """主链组连接下同序模拟节点之间的动态胶囊碰撞体。"""

    uuid: StringProperty(
        name="链间碰撞体 UUID",
        description="链间碰撞体的稳定内部身份；重建完整子表时会重新生成",
        options={"HIDDEN"},
    )
    local_joint_index: IntProperty(
        name="链内 Joint 序号",
        description="canonical 链中的局部 Joint 序号；1 是第一个模拟节点，0 固定驱动永远禁止",
        min=1,
        max=63,
        default=1,
        options={"HIDDEN"},
    )
    group_mask: IntProperty(
        name="链间碰撞组掩码",
        default=1,
        min=1,
        max=0x7FFFFFFF,
        description="按位标记此动态胶囊所属碰撞组；不能为 0",
        update=_update_link_collision,
    )
    radius: FloatProperty(
        name="链间碰撞半径",
        min=0.000001,
        max=10.0,
        default=0.05,
        precision=5,
        subtype="DISTANCE",
        description="两个骨链同序模拟节点之间动态胶囊的半径；运行时端点随当前模拟姿态重建",
        update=_update_link_collision,
    )
    collision_mask: IntProperty(
        name="链间碰撞掩码",
        default=0x7FFFFFFF,
        min=1,
        max=0x7FFFFFFF,
        description="按位选择此动态胶囊允许接触的链组；不能为 0",
        update=_update_link_collision,
    )


class HD2PB_ChainLink(PropertyGroup):
    """使两个骨链组共享平均物理偏移的组级约束。"""

    uuid: StringProperty(
        name="链组连接 UUID",
        description="链组连接的稳定内部身份；不依赖显示名称或创建顺序",
        options={"HIDDEN"},
    )
    display_name: StringProperty(
        name="链组连接名称",
        default="链组连接",
        description="写入开发版 HD2PHY1 的 ChainLink 名称；同类名称必须唯一",
        update=_update_chain_link,
    )
    chain_a_uuid: StringProperty(
        name="骨链 A UUID",
        description="第一条正式物理骨链的稳定 UUID；通过选择两个骨链曲线创建",
        options={"HIDDEN"},
    )
    chain_b_uuid: StringProperty(
        name="骨链 B UUID",
        description="第二条正式物理骨链的稳定 UUID；必须与骨链 A 不同",
        options={"HIDDEN"},
    )
    strip_constraint: BoolProperty(
        name="布条宽度与剪切约束",
        default=False,
        description="等节点数两链逐排保持宽度（±5%）及对角线（±8%），允许整体弯曲；关闭则使用平均偏移耦合",
    )
    coupling: FloatProperty(
        name="共同运动耦合强度",
        min=0.000001,
        max=1.0,
        default=1.0,
        precision=4,
        description="每轮约束修正比例；范围大于 0 且不超过 1",
    )
    max_correction: FloatProperty(
        name="单轮最大修正",
        min=0.000001,
        max=10.0,
        default=0.25,
        precision=4,
        subtype="DISTANCE",
        description="每个全局求解轮次允许施加到组级偏移差的最大模型空间长度",
    )
    collisions: CollectionProperty(
        name="链间碰撞体",
        type=HD2PB_LinkCollision,
        description="两条等长骨链从 local Joint 1 开始完整生成的动态胶囊子表",
    )


class HD2PB_PresetLibrary(PropertyGroup):
    """会话预设选择和折叠状态；不进入 Armature canonical 数据或 Undo。"""
    show_chain_profile_picker: BoolProperty(
        name="切换弹簧参数配置", default=True, options={"HIDDEN", "SKIP_SAVE"},
        description="展开当前骨链可切换的配置列表；收起时仍显示当前配置",
    )
    preset_profile: EnumProperty(name="弹簧参数预设", description="用户库中 PROFILE 类型的可复用弹簧参数", options={"SKIP_SAVE"}, items=_profile_library_items,
        get=_library_selection_get("PROFILE"), set=_library_selection_set("PROFILE"))
    preset_node: EnumProperty(name="节点参数预设", description="用户库中 NODE 类型的可复用节点参数", options={"SKIP_SAVE"}, items=_node_library_items,
        get=_library_selection_get("NODE"), set=_library_selection_set("NODE"))
    preset_collider: EnumProperty(name="碰撞体参数预设", description="用户库中 COLLIDER 类型的可复用碰撞体参数", options={"SKIP_SAVE"}, items=_collider_library_items,
        get=_library_selection_get("COLLIDER"), set=_library_selection_set("COLLIDER"))


class HD2PB_Project(PropertyGroup):
    """一个 Armature datablock 上的 PhysBone 权威项目。"""

    pose_drivers: CollectionProperty(name='衣物姿态驱动', description='保存在工程中的衣物姿态规则，由 AQSDK 独立封包编译为独立姿态资源；不写入旧 HD2PHY1', type=pose_driver_properties.HD2PB_PoseDriver)
    pose_driver_index: IntProperty(name='当前衣物驱动', description='衣物姿态驱动列表的活动行', default=0, min=0)
    show_pose_drivers: BoolProperty(name='衣物姿态驱动', description='展开衣物姿态规则制作与临时动作预览', default=False)
    show_pose_driver_object_markers: BoolProperty(
        name='物体模式显示姿态目标标记', default=True,
        description='物体模式沿目标骨绘制紫色线段；遵循骨架、骨骼与骨集合隐藏及前置显示，不创建物体或参与导出',
        update=_update_pose_object_markers,
    )
    motion_preview_show_armature: BoolProperty(
        name='显示预览骨架', default=False,
        description='仅控制临时动作预览副本的骨架可见性；关闭时仍正常蒙皮与求解，不影响原骨架或导出',
    )

    schema_version: IntProperty(
        name="项目数据版本",
        default=0,
        min=0,
        description=(
            "0 表示尚未初始化；P3/P4 项目使用 schema 1/2，P6 项目使用 schema 3。"
            "旧项目只能通过“迁移项目数据”显式升级，不会在载入、注册或绘制面板时静默修改"
        ),
        options={"HIDDEN"},
    )
    project_uuid: StringProperty(
        name="项目 UUID",
        description="本 Armature 物理骨项目的稳定内部身份",
        options={"HIDDEN"},
    )
    fixed_dt: FloatProperty(
        name="固定求解步长（Fixed Step）",
        default=1.0 / 60.0,
        min=1.0 / 240.0,
        max=1.0 / 15.0,
        precision=6,
        subtype="TIME",
        description=(
            "SpringBone 每个子步使用的秒数。较小更稳定但运行开销更高；runtime 每帧子步数"
            "还有独立上限"
        ),
    )
    new_chain_profile_uuid: StringProperty(
        name="新建骨链参数配置 UUID",
        description=(
            "姿态模式以后新建物理骨链默认使用的弹簧参数配置稳定 UUID；不会修改已有骨链，"
            "也不会写入 HD2PHY1"
        ),
        options={"HIDDEN"},
    )
    pose_collider_kind: EnumProperty(
        name="快捷碰撞体形状",
        items=COLLIDER_KIND_ITEMS,
        default="SPHERE",
        description="姿态模式从活动固定骨快速创建碰撞体时使用的形状",
    )
    preview_enabled: BoolProperty(
        name="显示实体预览",
        default=True,
        description=(
            "总开关：显示由 canonical 参数自动生成的彩色 Curve + Geometry Nodes 实体。"
            "Geometry Nodes 可在求值阶段生成表面，但不会创建 Mesh Object 或 Mesh 数据块。关闭只隐藏制作辅助体，"
            "不会删除参数、controller 或影响导出"
        ),
        update=_update_project_style,
    )
    preview_show_chains: BoolProperty(
        name="显示骨链曲线",
        default=True,
        description="显示每条物理骨链按节点顺序连接的彩色 Group 曲线",
        update=_update_project_style,
    )
    preview_show_nodes: BoolProperty(
        name="显示节点半径",
        default=False,
        description="显示绑定在骨骼头部的 Node radius 实体球",
        update=_update_project_style,
    )
    preview_show_limits: BoolProperty(
        name="显示角度限制",
        default=True,
        description="显示使用 HD2 Cone、Hinge 或 Spherical 数学生成的允许方向边界",
        update=_update_project_style,
    )
    preview_show_colliders: BoolProperty(
        name="显示碰撞体",
        default=True,
        description="显示 Sphere、Capsule 和 Plane 的 attachment-local 彩色实体",
        update=_update_project_style,
    )
    preview_show_chain_links: BoolProperty(
        name="显示链组连接",
        default=True,
        description="显示两个物理骨链组首个模拟节点之间的主连接曲线",
        update=_update_project_style,
    )
    preview_show_link_collisions: BoolProperty(
        name="显示链间碰撞体",
        default=True,
        description="显示主链组连接下按 local Joint 序号生成的动态胶囊预览",
        update=_update_project_style,
    )
    preview_in_front: BoolProperty(
        name="旧版穿透设置（兼容保留）",
        default=True,
        options={"HIDDEN"},
        description="只保留旧文件字段；新预览使用六个独立穿透设置",
        update=_update_project_style,
    )
    preview_xray_nodes: BoolProperty(name="骨链节点穿透", description="节点球及选择代理使用原生前置显示；全局 X-Ray 由 Blender 处理，不挖空骨架周围", default=True, update=_update_project_style)
    preview_xray_limits: BoolProperty(name="角度限制穿透", description="圆锥及参考轴使用原生前置显示；圆锥保留线框，同层物体仍按空间深度显示", default=True, update=_update_project_style)
    preview_xray_chains: BoolProperty(name="骨链曲线穿透", description="骨链曲线使用原生前置显示；不改变骨骼、曲线位置或材质", default=True, update=_update_project_style)
    preview_xray_chain_links: BoolProperty(name="链组连接穿透", description="链组连接使用原生前置显示；不改变链接物理参数", default=True, update=_update_project_style)
    preview_xray_link_collisions: BoolProperty(name="链间碰撞体穿透", description="链间碰撞预览使用原生前置显示；不改变接触求解", default=True, update=_update_project_style)
    preview_xray_colliders: BoolProperty(name="碰撞体穿透", description="普通碰撞体及胶囊端点使用原生前置显示；默认关闭，由模型正常遮挡", default=False, update=_update_project_style)
    new_chain_auto_tail: BoolProperty(
        name="新建链自动补尾骨",
        default=True,
        description="在实际末骨骨尾补一个无权重模拟点；已有明确末端不重复补，原网格和权重不变",
    )
    preview_show_names: BoolProperty(
        name="预览名称总开关（兼容）",
        default=True,
        options={"HIDDEN"},
        description=(
            "历史兼容总开关。新界面使用“显示节点名称”和“显示碰撞体名称”两个独立开关；"
            "骨链、链组连接、链间碰撞和角限辅助体永不显示名称"
        ),
        update=_update_project_style,
    )
    preview_show_node_names: BoolProperty(
        name="显示节点名称",
        default=True,
        description="单独显示模拟节点的骨骼名称；节点半径和角限实体不会显示名称",
        update=_update_project_style,
    )
    preview_show_collider_names: BoolProperty(
        name="显示碰撞体名称",
        default=True,
        description="单独显示可选择的球形、胶囊和平面碰撞体名称；胶囊整体名称按规则隐藏",
        update=_update_project_style,
    )
    preview_allow_collider_transform: BoolProperty(
        name="实验：允许视图变换碰撞体",
        default=False,
        description=(
            "允许在物体模式直接移动、旋转、缩放可选择的 Collider Curve，用于快速试摆。"
            "这些变换只影响本次制作视图，不写入 HD2PHY1；属性栏的形状、A/B、半径或吸附设置一经修改，"
            "会按权威参数覆盖并重置视图变换。关闭本项也会立即重置视图变换"
        ),
        update=_update_project_style,
    )
    preview_chain_color: FloatVectorProperty(
        name="骨链曲线颜色",
        size=4,
        default=(0.08, 0.55, 1.0, 0.47),
        min=0.0,
        max=1.0,
        subtype="COLOR",
        description="Chain/Group 曲线的视口材质颜色；只影响制作显示，不写入 HD2PHY1",
        update=_update_project_style,
    )
    preview_node_color: FloatVectorProperty(
        name="节点半径颜色",
        size=4,
        default=(0.0, 0.06, 0.9, 0.5),
        min=0.0,
        max=1.0,
        subtype="COLOR",
        description="Node radius 球的视口材质颜色；只影响制作显示",
        update=_update_project_style,
    )
    preview_limit_color: FloatVectorProperty(
        name="角度限制颜色",
        size=4,
        default=(1.0, 0.42, 0.06, 0.62),
        min=0.0,
        max=1.0,
        subtype="COLOR",
        description="Cone、Hinge、Spherical 角限边界的视口材质颜色；只影响制作显示",
        update=_update_project_style,
    )
    preview_collider_color: FloatVectorProperty(
        name="碰撞体颜色",
        size=4,
        default=(1.0, 0.12, 0.24, 0.58),
        min=0.0,
        max=1.0,
        subtype="COLOR",
        description="Sphere、Capsule、Plane 碰撞体的视口材质颜色；只影响制作显示",
        update=_update_project_style,
    )
    preview_chain_link_color: FloatVectorProperty(
        name="链组连接颜色",
        size=4,
        default=(0.18, 0.9, 0.35, 0.5),
        min=0.0,
        max=1.0,
        subtype="COLOR",
        description="ChainLink 主曲线的视口材质颜色；只影响制作显示",
        update=_update_project_style,
    )
    preview_link_collision_color: FloatVectorProperty(
        name="链间碰撞体颜色",
        size=4,
        default=(0.95, 0.16, 0.08, 0.62),
        min=0.0,
        max=1.0,
        subtype="COLOR",
        description="LinkCollision 动态胶囊的视口材质颜色；只影响制作显示",
        update=_update_project_style,
    )
    preview_curve_radius: FloatProperty(
        name="骨链曲线粗细",
        default=0.008,
        min=0.0001,
        max=1.0,
        precision=4,
        subtype="DISTANCE",
        description="3D 视图中 Chain/Group 曲线的显示半径；不是 Node radius，也不参与导出",
        update=_update_project_style,
    )
    preview_limit_cone_height: FloatProperty(
        name="角限圆锥预览高度",
        default=0.1,
        min=0.02,
        max=100.0,
        precision=4,
        subtype="DISTANCE",
        description=(
            "角限实体圆锥沿当前角限轴的显示高度；只影响制作预览，不是节点碰撞半径，"
            "也不写入 HD2PHY1。修改节点 Radius 不会改变此高度"
        ),
        update=_update_project_limit_preview,
    )
    preview_plane_size: FloatProperty(
        name="平面预览尺寸",
        default=0.5,
        min=0.01,
        max=100.0,
        precision=3,
        subtype="DISTANCE",
        description="用于表示无限 Plane Collider 的有限方片边长；只影响显示，不改变碰撞平面",
        update=_update_project_plane_geometry,
    )
    preview_isolate_entity_uuid: StringProperty(
        name="当前隔离实体 UUID",
        description="只显示当前实体时使用的瞬时 UUID；清空表示显示全部已启用分类",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    preview_error_message: StringProperty(
        name="实体预览刷新错误",
        description=(
            "最近一次实时刷新或旧投影清理失败的中文提示；仅用于当前 Blender 会话，"
            "不会保存到 .blend，也不会写入 HD2PHY1"
        ),
        options={"HIDDEN", "SKIP_SAVE"},
    )
    profile_index: IntProperty(
        name="当前配置列表项",
        default=0,
        min=0,
        options={"HIDDEN", "SKIP_SAVE"},
        description="物体模式折叠列表的当前配置行；仅用于界面导航，不属于导出数据",
    )
    chain_index: IntProperty(
        name="当前骨链列表项",
        default=0,
        min=0,
        options={"HIDDEN", "SKIP_SAVE"},
        description="物体模式折叠列表的当前骨链行；仅用于界面导航，不属于导出数据",
    )
    collider_index: IntProperty(
        name="当前碰撞体列表项",
        default=0,
        min=0,
        options={"HIDDEN", "SKIP_SAVE"},
        description="物体模式折叠列表的当前碰撞体行；仅用于界面导航，不属于导出数据",
    )
    chain_link_index: IntProperty(
        name="当前链组连接列表项",
        default=0,
        min=0,
        options={"HIDDEN", "SKIP_SAVE"},
        description="物体模式链组连接列表的当前行；仅用于界面导航",
    )
    show_profile_list: BoolProperty(
        name="展开弹簧参数配置列表",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
        description="展开或折叠物体模式中的参数配置导航列表",
    )
    show_chain_list: BoolProperty(
        name="展开物理骨链列表",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
        description="展开或折叠物体模式中的骨链导航列表",
    )
    show_collider_list: BoolProperty(
        name="展开碰撞体列表",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
        description="展开或折叠物体模式中的碰撞体导航列表",
    )
    show_chain_link_list: BoolProperty(
        name="展开链组连接列表",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
        description="展开或折叠物体模式中的 ChainLink 导航列表",
    )
    show_preview_settings: BoolProperty(
        name="展开实体预览设置",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
        description="展开或折叠物体模式中的可见性、颜色、穿透与隔离设置",
    )
    show_edit_tools: BoolProperty(
        name="展开复制、预设与批量工具",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
        description="展开或折叠 Clipboard、JSON Preset、角限渐变和轴对齐工具",
    )
    authoring_object: PointerProperty(
        name="编辑所属骨架对象",
        type=bpy.types.Object,
        description="唯一拥有本项目 controller 的 Armature Object；共享 datablock 时必须先单用户化",
        options={"HIDDEN"},
    )
    controller_collection: PointerProperty(
        name="编辑对象根集合",
        type=bpy.types.Collection,
        description="可从 canonical 数据重建的 Outliner 投影根集合，不参与 HD2PHY1 文档",
        options={"HIDDEN"},
    )
    profiles: CollectionProperty(
        name="弹簧参数配置",
        type=HD2PB_Profile,
        description="可由多条物理骨链复用的 SpringBone 链级参数集合",
    )
    chains: CollectionProperty(
        name="物理骨链",
        type=HD2PB_Chain,
        description="当前 Armature 项目的直接父子 SpringBone 链集合",
    )
    colliders: CollectionProperty(
        name="碰撞体",
        type=HD2PB_Collider,
        description="当前 Armature 项目的 Unit 根、静态骨绑定或动态模拟节点碰撞体集合",
    )
    chain_links: CollectionProperty(
        name="链组连接",
        type=HD2PB_ChainLink,
        description="跨两个不同物理骨链、共享组级平均物理偏移的 ChainLink 集合",
    )


CLASSES = (
    HD2PB_Profile,
    HD2PB_Node,
    HD2PB_Chain,
    HD2PB_Collider,
    HD2PB_LinkCollision,
    HD2PB_ChainLink,
    *pose_driver_properties.CLASSES,
    HD2PB_Project,
    HD2PB_PresetLibrary,
)

_REGISTERED_CLASSES = []
_RNA_OWNED = []


def _add_rna(owner, name, value):
    if hasattr(owner, name):
        raise RuntimeError(f"无法注册物理骨属性：{owner.__name__}.{name} 已存在")
    setattr(owner, name, value)
    _RNA_OWNED.append((owner, name))


def _remove_rna():
    errors = []
    remaining = []
    for owner, name in reversed(tuple(_RNA_OWNED)):
        if not hasattr(owner, name):
            continue
        try:
            delattr(owner, name)
        except Exception as exc:
            errors.append(f"{owner.__name__}.{name}: {exc}")
            remaining.append((owner, name))
    _RNA_OWNED[:] = [
        item for item in _RNA_OWNED if any(item == kept for kept in remaining)
    ]
    if errors:
        raise RuntimeError("物理骨 RNA 注销不完整：" + "; ".join(errors))


def owned_state_present():
    return bool(_REGISTERED_CLASSES or _RNA_OWNED)


def register():
    """注册 canonical PropertyGroup，再挂 Armature/Bone/Object RNA。"""

    if owned_state_present():
        raise RuntimeError("物理骨属性模块仍持有 Blender 注册状态")
    register_classes(CLASSES, _REGISTERED_CLASSES)
    try:
        _add_rna(bpy.types.WindowManager, "hd2pb_preset_library",
                 PointerProperty(name="物理参数预设库界面", description="仅在当前会话保留预设选择与骨链配置折叠状态，不写入物理项目",
                                 type=HD2PB_PresetLibrary, options={"SKIP_SAVE"}))
        _add_rna(
            bpy.types.Armature,
            "hd2pb_project",
            PointerProperty(
                name="HD2 物理骨项目",
                type=HD2PB_Project,
                description="保存在 Armature datablock 上的 HD2 PhysBone canonical 数据",
            ),
        )
        _add_rna(
            bpy.types.Scene,
            "hd2pb_active_project_owner",
            PointerProperty(
                name="当前物理骨项目",
                type=bpy.types.Object,
                poll=_filter_scene_project_owner,
                update=_update_scene_project_owner,
                description=(
                    "当前 Scene 中持续编辑的 Armature 物理骨项目。投影重建删除活动对象后，"
                    "面板和项目级按钮仍使用这里记录的项目，不要求重新选择骨架"
                ),
            ),
        )
        _add_rna(
            bpy.types.Scene,
            "hd2pb_active_project_uuid",
            StringProperty(
                name="当前物理骨项目 UUID",
                description=(
                    "与当前项目骨架指针配对的稳定 UUID；指针被删除、移出 Scene 或项目身份改变时，"
                    "插件会拒绝复用过期引用"
                ),
                options={"HIDDEN"},
            ),
        )
        _add_rna(
            bpy.types.Object,
            "hd2pb_controller_schema",
            IntProperty(
                name="编辑对象版本",
                default=0,
                min=0,
                description="HD2 物理骨 controller 投影版本；普通对象为 0",
                options={"HIDDEN"},
            ),
        )
        _add_rna(
            bpy.types.Object,
            "hd2pb_project_uuid",
            StringProperty(
                name="所属项目 UUID",
                description="controller 所投影的物理骨项目 UUID",
                options={"HIDDEN"},
            ),
        )
        _add_rna(
            bpy.types.Object,
            "hd2pb_entity_type",
            EnumProperty(
                name="物理骨实体类型",
                items=ENTITY_TYPE_ITEMS,
                default=ENTITY_NONE,
                description="controller 对应的 canonical 实体类型",
                options={"HIDDEN"},
            ),
        )
        _add_rna(
            bpy.types.Object,
            "hd2pb_entity_uuid",
            StringProperty(
                name="实体 UUID",
                description="controller 所对应 canonical 实体的稳定 UUID",
                options={"HIDDEN"},
            ),
        )
        _add_rna(
            bpy.types.Object,
            "hd2pb_controller_uuid",
            StringProperty(
                name="编辑对象 UUID",
                description="本次可重建 controller 的独立 UUID",
                options={"HIDDEN"},
            ),
        )
        _add_rna(
            bpy.types.Object,
            "hd2pb_owner_object",
            PointerProperty(
                name="所属骨架对象",
                type=bpy.types.Object,
                description="controller 所属的唯一 Armature authoring Object",
                options={"HIDDEN"},
            ),
        )
        _add_rna(
            bpy.types.Object,
            "hd2pb_preview_schema",
            IntProperty(
                name="实体预览版本",
                default=0,
                min=0,
                description=(
                    "HD2 物理骨生成预览的内部版本；普通对象和可选择 controller 为 0。"
                    "预览可以安全重建，绝不作为导出参数来源"
                ),
                options={"HIDDEN"},
            ),
        )
        _add_rna(
            bpy.types.Object,
            "hd2pb_preview_kind",
            EnumProperty(
                name="实体预览类型",
                items=PREVIEW_KIND_ITEMS,
                default=PREVIEW_NONE,
                description="不可选择的生成预览所表示的几何类型",
                options={"HIDDEN"},
            ),
        )
        _add_rna(
            bpy.types.Object,
            "hd2pb_preview_uuid",
            StringProperty(
                name="实体预览 UUID",
                description="本次生成预览对象的独立 UUID，用于精确诊断和安全清理",
                options={"HIDDEN"},
            ),
        )
    except Exception:
        try:
            _remove_rna()
        finally:
            unregister_classes(_REGISTERED_CLASSES)
        raise


def unregister():
    """只移除 RNA 定义，不删除用户 `.blend` 中的项目或 controller。"""

    errors = []
    try:
        _remove_rna()
    except Exception as exc:
        errors.append(str(exc))
    try:
        unregister_classes(_REGISTERED_CLASSES)
    except Exception as exc:
        errors.append(str(exc))
    if errors:
        raise RuntimeError("物理骨属性注销不完整：" + " | ".join(errors))


__all__ = (
    "PROJECT_SCHEMA_VERSION",
    "CONTROLLER_SCHEMA_VERSION",
    "PREVIEW_SCHEMA_VERSION",
    "NODE_DISPLAY_MIN_RADIUS",
    "COLLECTION_SCHEMA_KEY",
    "COLLECTION_PROJECT_KEY",
    "COLLECTION_OWNER_KEY",
    "COLLECTION_ROLE_KEY",
    "COLLECTION_ENTITY_KEY",
    "COLLECTION_DISPLAY_NAME_KEY",
    "ENTITY_NONE",
    "ENTITY_PROJECT",
    "ENTITY_PROFILE",
    "ENTITY_CHAIN",
    "ENTITY_NODE",
    "ENTITY_COLLIDER",
    "ENTITY_CHAIN_LINK",
    "ENTITY_LINK_COLLISION",
    "ENTITY_TYPE_ITEMS",
    "PREVIEW_NONE",
    "PREVIEW_CHAIN_CURVE",
    "PREVIEW_NODE_RADIUS",
    "PREVIEW_NODE_LIMIT",
    "PREVIEW_NODE_LIMIT_AXIS",
    "PREVIEW_COLLIDER_SHAPE",
    "PREVIEW_CHAIN_LINK",
    "PREVIEW_LINK_COLLISION",
    "PREVIEW_KIND_ITEMS",
    "LIMIT_KIND_ITEMS",
    "REFERENCE_MODE_ITEMS",
    "COLLIDER_KIND_ITEMS",
    "COLLIDER_ATTACHMENT_ITEMS",
    "HD2PB_Profile",
    "HD2PB_Node",
    "HD2PB_Chain",
    "HD2PB_Collider",
    "HD2PB_LinkCollision",
    "HD2PB_ChainLink",
    "HD2PB_Project",
    "CLASSES",
    "owned_state_present",
    "register",
    "unregister",
)
