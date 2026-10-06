"""分类映射到 Blender 原生前置显示。
与 RE Chain 相同，Curve/GN 直接参与原生材质、骨架叠加与 X-Ray。
"""

from . import properties as P


# 面板按用户期望的检查顺序排列，绝不拿名称或相机偏移伪造深度。
DISPLAY_CATEGORIES = (
    (P.PREVIEW_NODE_RADIUS, "preview_xray_nodes", True),
    (P.PREVIEW_NODE_LIMIT, "preview_xray_limits", True),
    (P.PREVIEW_CHAIN_CURVE, "preview_xray_chains", True),
    (P.PREVIEW_CHAIN_LINK, "preview_xray_chain_links", True),
    (P.PREVIEW_LINK_COLLISION, "preview_xray_link_collisions", True),
    (P.PREVIEW_COLLIDER_SHAPE, "preview_xray_colliders", False),
)
_FIELDS = {kind: (field, default) for kind, field, default in DISPLAY_CATEGORIES}


def xray(project, kind):
    """角限参考轴和胶囊端点沿用所属实体的穿透设置。"""
    if kind == P.PREVIEW_NODE_LIMIT_AXIS:
        kind = P.PREVIEW_NODE_LIMIT
    elif kind == P.PREVIEW_COLLIDER_ENDPOINT:
        kind = P.PREVIEW_COLLIDER_SHAPE
    field, default = _FIELDS.get(kind, ("", False))
    return bool(getattr(project, field, default))


def style(obj, project):
    """仅修改本插件传入的预览对象，不修改用户骨架或视口状态。"""
    kind = obj.hd2pb_preview_kind
    obj.show_in_front = xray(project, kind)
    obj.show_wire = kind == P.PREVIEW_NODE_LIMIT
    obj.show_all_edges = obj.show_wire
    if obj.type == "CURVE":
        obj.display_type = "SOLID"


def style_node_proxy(obj, project):
    """可选择的球形 Empty 独立显示/穿透，并服从实体预览总开关。"""
    obj.show_in_front = bool(project.preview_xray_node_spheres)
    obj.hide_viewport = not (project.preview_enabled and project.preview_show_node_spheres)
