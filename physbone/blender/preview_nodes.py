"""HD2 PhysBone 预览用 Geometry Nodes 资源。

本模块只负责建立和更新 Curve 宿主上的 Geometry Nodes modifier。GN 内部可以
生成临时网格几何，但插件不会创建任何 ``bpy.data.meshes`` 作为预览数据块。
Node Group 使用稳定名称并跨项目复用，避免拖动属性时重复构造资源。
"""

from __future__ import annotations

import bpy


GN_OWNER_KEY = "hd2pb_preview_gn_owner"
GN_OWNER_VALUE = "HD2PhysBone"
GN_SCHEMA_KEY = "hd2pb_preview_gn_schema"
GN_KIND_KEY = "hd2pb_preview_gn_kind"
GN_SIGNATURE_KEY = "hd2pb_preview_gn_signature"
GN_VERSION = 12

SOCKET_GEOMETRY = "Geometry"
SOCKET_MATERIAL = "Material"

_GROUP_NAMES = {
    "CHAIN": "HD2PB_GN_ChainCurve",
    "NODE_RADIUS": "HD2PB_GN_NodeRadius",
    "COLLIDER_SPHERE": "HD2PB_GN_ColliderSphere",
    "COLLIDER_CAPSULE": "HD2PB_GN_ColliderCapsule",
    "COLLIDER_PLANE": "HD2PB_GN_ColliderPlane",
    "LIMIT": "HD2PB_GN_AngleLimit",
    "LINK_COLLISION": "HD2PB_GN_LinkCollision",
}


def _socket(group, name, in_out="INPUT", socket_type="NodeSocketFloat"):
    """Create one interface socket, supporting Blender 4.x's interface API."""

    existing = next(
        (
            item
            for item in group.interface.items_tree
            if getattr(item, "item_type", None) == "SOCKET"
            and item.name == name
            and item.in_out == in_out
        ),
        None,
    )
    return existing or group.interface.new_socket(
        name=name,
        in_out=in_out,
        socket_type=socket_type,
    )


def _clear_group(group):
    group.nodes.clear()
    # Rebuilding an old group with a changed interface is safer than retaining
    # stale sockets from an interrupted development build.
    for item in tuple(group.interface.items_tree):
        if getattr(item, "item_type", None) == "SOCKET":
            try:
                group.interface.remove(item)
            except (RuntimeError, TypeError):
                pass


def _io(group):
    group_input = group.nodes.new("NodeGroupInput")
    group_output = group.nodes.new("NodeGroupOutput")
    group_input.location = (-700, 0)
    group_output.location = (700, 0)
    return group_input, group_output


def _link(group, output_node, output_name, input_node, input_name):
    output = (
        output_node.outputs[output_name]
        if isinstance(output_name, int)
        else output_node.outputs.get(output_name)
    )
    target = (
        input_node.inputs[input_name]
        if isinstance(input_name, int)
        else input_node.inputs.get(input_name)
    )
    if output is not None and target is not None:
        group.links.new(output, target)


def _input_alias(node, *names):
    """按 Blender 版本兼容地查找 Geometry Nodes 输入 socket。"""

    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            return socket
    return None


def _material_output(
    group,
    geometry_node,
    group_input,
    group_output,
    *,
    output_name=None,
    smooth=True,
):
    source = (
        geometry_node.outputs.get(output_name)
        if output_name is not None
        else next(
            (
                geometry_node.outputs.get(name)
                for name in ("Geometry", "Mesh", "Curve")
                if geometry_node.outputs.get(name) is not None
            ),
            None,
        )
    )
    if smooth:
        try:
            smooth_node = group.nodes.new("GeometryNodeSetShadeSmooth")
            smooth_node.location = (170, 0)
            if source is not None and smooth_node.inputs.get("Geometry") is not None:
                group.links.new(source, smooth_node.inputs["Geometry"])
                source = smooth_node.outputs.get("Geometry") or source
        except (RuntimeError, TypeError):
            # Blender 4.x supports this node; retain a usable preview if a future
            # build removes it from the Geometry Nodes registry.
            pass
    material_node = group.nodes.new("GeometryNodeSetMaterial")
    material_node.location = (350, 0)
    if source is not None:
        group.links.new(source, material_node.inputs["Geometry"])
    _link(group, group_input, SOCKET_MATERIAL, material_node, SOCKET_MATERIAL)
    _link(group, material_node, "Geometry", group_output, "Geometry")
    return material_node


def _combine_xyz(group, source, group_input, location=(-80, -250)):
    combine = group.nodes.new("ShaderNodeCombineXYZ")
    combine.location = location
    for axis in ("X", "Y", "Z"):
        _link(group, group_input, source, combine, axis)
    return combine


def _build_chain(group):
    _socket(group, SOCKET_GEOMETRY, "INPUT", "NodeSocketGeometry")
    _socket(group, SOCKET_GEOMETRY, "OUTPUT", "NodeSocketGeometry")
    _socket(group, "Radius", "INPUT", "NodeSocketFloat")
    _socket(group, SOCKET_MATERIAL, "INPUT", "NodeSocketMaterial")
    group_input, group_output = _io(group)
    set_radius = group.nodes.new("GeometryNodeSetCurveRadius")
    set_radius.location = (-350, 60)
    _link(group, group_input, SOCKET_GEOMETRY, set_radius, "Curve")
    _link(group, group_input, "Radius", set_radius, "Radius")
    profile = group.nodes.new("GeometryNodeCurvePrimitiveCircle")
    profile.location = (-350, -150)
    if profile.inputs.get("Resolution"):
        profile.inputs["Resolution"].default_value = 12
    if profile.inputs.get("Radius"):
        profile.inputs["Radius"].default_value = 1.0
    curve_mesh = group.nodes.new("GeometryNodeCurveToMesh")
    curve_mesh.location = (-80, 40)
    _link(group, set_radius, "Curve", curve_mesh, "Curve")
    _link(group, profile, "Curve", curve_mesh, "Profile Curve")
    _material_output(
        group,
        curve_mesh,
        group_input,
        group_output,
        output_name="Mesh",
    )


def _build_capsule_hull(group):
    """把两点 Hook Curve 转成无 Boolean 接缝的平滑胶囊。"""

    _socket(group, SOCKET_GEOMETRY, "INPUT", "NodeSocketGeometry")
    _socket(group, SOCKET_GEOMETRY, "OUTPUT", "NodeSocketGeometry")
    _socket(group, "Radius", "INPUT", "NodeSocketFloat")
    _socket(group, SOCKET_MATERIAL, "INPUT", "NodeSocketMaterial")
    group_input, group_output = _io(group)

    curve_points = group.nodes.new("GeometryNodeCurveToPoints")
    curve_points.location = (-480, 80)
    if hasattr(curve_points, "mode"):
        curve_points.mode = "COUNT"
    if curve_points.inputs.get("Count"):
        curve_points.inputs["Count"].default_value = 2
    _link(group, group_input, SOCKET_GEOMETRY, curve_points, "Curve")

    sphere = group.nodes.new("GeometryNodeMeshUVSphere")
    sphere.location = (-480, -220)
    if sphere.inputs.get("Segments"):
        sphere.inputs["Segments"].default_value = 24
    if sphere.inputs.get("Rings"):
        sphere.inputs["Rings"].default_value = 12
    sphere_transform = group.nodes.new("GeometryNodeTransform")
    sphere_transform.location = (-220, -220)
    scale = _combine_xyz(group, "Radius", group_input, location=(-480, -390))
    _link(group, sphere, "Mesh", sphere_transform, "Geometry")
    _link(group, scale, "Vector", sphere_transform, "Scale")
    instances = group.nodes.new("GeometryNodeInstanceOnPoints")
    instances.location = (20, 20)
    _link(group, curve_points, "Points", instances, "Points")
    _link(group, sphere_transform, "Geometry", instances, "Instance")
    realize = group.nodes.new("GeometryNodeRealizeInstances")
    realize.location = (250, 20)
    _link(group, instances, "Instances", realize, "Geometry")

    # 数学上的胶囊就是两个同半径球体的凸包。直接构造凸包可获得连续的
    # 半球和柱面拓扑，避免 Boolean 在相切圆周生成零面积面与异常平滑法向。
    hull = group.nodes.new("GeometryNodeConvexHull")
    hull.location = (470, 20)
    _link(group, realize, "Geometry", hull, "Geometry")
    _material_output(
        group,
        hull,
        group_input,
        group_output,
        output_name="Convex Hull",
    )


def _build_link_collision(group):
    """把两点 Hook Curve 转成单一连续的动态胶囊。"""

    _build_capsule_hull(group)


def _build_sphere(group):
    _socket(group, SOCKET_GEOMETRY, "INPUT", "NodeSocketGeometry")
    _socket(group, SOCKET_GEOMETRY, "OUTPUT", "NodeSocketGeometry")
    _socket(group, "Radius", "INPUT", "NodeSocketFloat")
    _socket(group, "Center", "INPUT", "NodeSocketVector")
    _socket(group, SOCKET_MATERIAL, "INPUT", "NodeSocketMaterial")
    group_input, group_output = _io(group)
    sphere = group.nodes.new("GeometryNodeMeshUVSphere")
    sphere.location = (-320, 0)
    if sphere.inputs.get("Segments"):
        sphere.inputs["Segments"].default_value = 24
    if sphere.inputs.get("Rings"):
        sphere.inputs["Rings"].default_value = 12
    transform = group.nodes.new("GeometryNodeTransform")
    transform.location = (-40, 0)
    scale = _combine_xyz(group, "Radius", group_input)
    _link(group, scale, "Vector", transform, "Scale")
    _link(group, group_input, "Center", transform, "Translation")
    _link(group, sphere, "Mesh", transform, "Geometry")
    _material_output(group, transform, group_input, group_output)


def _build_capsule(group):
    _build_capsule_hull(group)


def _build_plane(group):
    for name, socket_type in (
        (SOCKET_GEOMETRY, "NodeSocketGeometry"),
        ("Point", "NodeSocketVector"),
        ("Rotation", "NodeSocketVector"),
        ("Size", "NodeSocketFloat"),
        (SOCKET_MATERIAL, "NodeSocketMaterial"),
    ):
        _socket(group, name, "INPUT", socket_type)
    _socket(group, SOCKET_GEOMETRY, "OUTPUT", "NodeSocketGeometry")
    group_input, group_output = _io(group)
    grid = group.nodes.new("GeometryNodeMeshGrid")
    grid.location = (-350, 0)
    if grid.inputs.get("Vertices X"):
        grid.inputs["Vertices X"].default_value = 2
    if grid.inputs.get("Vertices Y"):
        grid.inputs["Vertices Y"].default_value = 2
    _link(group, group_input, "Size", grid, "Size X")
    _link(group, group_input, "Size", grid, "Size Y")
    transform = group.nodes.new("GeometryNodeTransform")
    transform.location = (-30, 0)
    _link(group, grid, "Mesh", transform, "Geometry")
    _link(group, group_input, "Point", transform, "Translation")
    _link(group, group_input, "Rotation", transform, "Rotation")
    _material_output(group, transform, group_input, group_output)


def _build_limit(group):
    for name, socket_type in (
        (SOCKET_GEOMETRY, "NodeSocketGeometry"),
        ("Solid", "NodeSocketBool"),
        ("Cone Radius", "NodeSocketFloat"),
        ("Cone Depth", "NodeSocketFloat"),
        (SOCKET_MATERIAL, "NodeSocketMaterial"),
    ):
        _socket(group, name, "INPUT", socket_type)
    _socket(group, SOCKET_GEOMETRY, "OUTPUT", "NodeSocketGeometry")
    group_input, group_output = _io(group)
    fill = group.nodes.new("GeometryNodeFillCurve")
    fill.location = (-120, 0)
    if hasattr(fill, "mode"):
        fill.mode = "TRIANGLES"
    _link(group, group_input, SOCKET_GEOMETRY, fill, "Curve")
    # Python writes one closed Curve spline for every exact HD2 triangle.
    # Fill Curve creates only the transient evaluated surface, preserving
    # non-convex Hinge/Spherical fans without a persistent Mesh datablock.
    cone = group.nodes.new("GeometryNodeMeshCone")
    cone.location = (-120, -300)
    if cone.inputs.get("Vertices"):
        cone.inputs["Vertices"].default_value = 18
    radius_1 = _input_alias(cone, "Radius1", "Radius 1", "Radius Bottom")
    radius_2 = _input_alias(cone, "Radius2", "Radius 2", "Radius Top")
    # 在 Blender Geometry Nodes 中，Radius 1 位于局部原点一端，Radius 2
    # 位于 +Z 的 Depth 端。令 Radius 1 为零、Radius 2 为底半径，锥顶就会
    # 落在当前 Node 的局部原点、圆底落在 +Z 方向；这与 Python 的精确边界方向一致。
    if radius_1 is not None:
        radius_1.default_value = 0.0
    if radius_2 is not None:
        _link(group, group_input, "Cone Radius", cone, radius_2.name)
    if cone.inputs.get("Depth"):
        _link(group, group_input, "Cone Depth", cone, "Depth")
    switch = group.nodes.new("GeometryNodeSwitch")
    switch.input_type = "GEOMETRY"
    switch.location = (300, 0)
    _link(group, group_input, "Solid", switch, "Switch")
    _link(group, fill, "Mesh", switch, "False")
    # Blender GeometryNodeMeshCone 的局部轴向范围已经是 0..Depth：顶点在
    # 原点、底面位于 +Z 的 Depth 处。不要再平移半个深度，否则锥顶会沿
    # 当前骨骼方向错开半段，无法吸附到当前节点头部。
    _link(group, cone, "Mesh", switch, "True")
    # Blender 4.0 的 Fill Curve 在 Shade Smooth 的布尔 field 被外部 socket
    # 驱动时可能返回空面。沿用已验证的 Fill -> Geometry Switch -> Shade
    # Smooth 路径，再以第二个几何 Switch 让 Cone 旁路平滑节点。
    smooth_surface = group.nodes.new("GeometryNodeSetShadeSmooth")
    smooth_surface.location = (500, 120)
    _link(group, switch, "Output", smooth_surface, "Geometry")
    shade_switch = group.nodes.new("GeometryNodeSwitch")
    shade_switch.input_type = "GEOMETRY"
    shade_switch.location = (700, 0)
    _link(group, group_input, "Solid", shade_switch, "Switch")
    _link(group, smooth_surface, "Geometry", shade_switch, "False")
    _link(group, switch, "Output", shade_switch, "True")
    _material_output(
        group,
        shade_switch,
        group_input,
        group_output,
        output_name="Output",
        smooth=False,
    )


def _builder_for(kind):
    return {
        "CHAIN": _build_chain,
        "NODE_RADIUS": _build_sphere,
        "COLLIDER_SPHERE": _build_sphere,
        "COLLIDER_CAPSULE": _build_capsule,
        "COLLIDER_PLANE": _build_plane,
        "LIMIT": _build_limit,
        "LINK_COLLISION": _build_link_collision,
    }[kind]


def _structure_signature(group):
    """Return a deterministic structural signature for one generated node tree."""

    interface = tuple(
        sorted(
            (
                item.in_out,
                item.name,
                item.socket_type,
            )
            for item in group.interface.items_tree
            if getattr(item, "item_type", None) == "SOCKET"
        )
    )
    nodes = tuple(sorted((node.name, node.bl_idname) for node in group.nodes))
    links = tuple(
        sorted(
            (
                link.from_node.name,
                link.from_socket.name,
                link.to_node.name,
                link.to_socket.name,
            )
            for link in group.links
        )
    )
    return repr((interface, nodes, links))


def _critical_defaults_valid(group, kind):
    """校验不会进入结构签名、但会直接改变预览合同的关键默认值。"""

    if str(kind or "").upper() != "LIMIT":
        return True
    cones = [
        node for node in group.nodes if node.bl_idname == "GeometryNodeMeshCone"
    ]
    if len(cones) != 1:
        return False
    vertices = cones[0].inputs.get("Vertices")
    radius_1 = _input_alias(cones[0], "Radius1", "Radius 1", "Radius Bottom")
    try:
        return bool(
            vertices is not None
            and int(vertices.default_value) == 18
            and radius_1 is not None
            and abs(float(radius_1.default_value)) <= 1.0e-9
        )
    except (TypeError, ValueError, OverflowError):
        return False


def is_owned_node_group(group, kind=None, *, current_version=True):
    """判断节点组是否由本插件明确拥有，不能用显示名称推断所有权。"""

    if (
        group is None
        or group.bl_idname != "GeometryNodeTree"
        or group.library is not None
        or group.get(GN_OWNER_KEY) != GN_OWNER_VALUE
    ):
        return False
    if kind is not None and group.get(GN_KIND_KEY) != str(kind).upper():
        return False
    if not current_version:
        return True
    return bool(
        group.get(GN_SCHEMA_KEY) == GN_VERSION
        and group.get(GN_SIGNATURE_KEY) == _structure_signature(group)
        and _critical_defaults_valid(group, group.get(GN_KIND_KEY))
    )


def is_migratable_node_group(group, kind=None):
    """判断是否为可在刷新时替换的旧版 HD2 节点组。

    只接受明确带有本插件 owner/kind 标签、且 schema 严格低于当前版本的
    资源。当前版本结构损坏或未来版本资源继续 fail-closed，避免把用户手动
    修改过的节点树静默接管。
    """

    if not is_owned_node_group(group, kind, current_version=False):
        return False
    try:
        schema = int(group.get(GN_SCHEMA_KEY, 0))
    except (TypeError, ValueError):
        return False
    return 0 < schema < GN_VERSION


def _group_is_complete(group, kind):
    return bool(
        is_owned_node_group(group, kind)
        and group.nodes.get("Group Input")
        and group.nodes.get("Group Output")
    )


def _new_owned_group(kind):
    """新建一个节点组；同名用户资源存在时让 Blender 自动添加后缀。"""

    group = bpy.data.node_groups.new(_GROUP_NAMES[kind], "GeometryNodeTree")
    try:
        _clear_group(group)
        _builder_for(kind)(group)
        group[GN_OWNER_KEY] = GN_OWNER_VALUE
        group[GN_SCHEMA_KEY] = GN_VERSION
        group[GN_KIND_KEY] = kind
        group[GN_SIGNATURE_KEY] = _structure_signature(group)
    except Exception:
        if bpy.data.node_groups.get(group.name) == group and group.users == 0:
            bpy.data.node_groups.remove(group)
        raise
    return group


def ensure_node_group(kind):
    """返回插件明确拥有的共享 GN 组，不删除或改写任何同名用户资源。"""

    kind = str(kind).upper()
    if kind not in _GROUP_NAMES:
        raise ValueError(f"未知 HD2 预览 GN 类型：{kind}")
    matches = sorted(
        (
            group
            for group in bpy.data.node_groups
            if _group_is_complete(group, kind)
        ),
        key=lambda group: group.name,
    )
    if matches:
        return matches[0]
    # 旧版本或损坏的插件组也不原位清空：modifier 可能仍在求值，写时新建
    # 能保证构建失败时旧预览继续可见。名称仅是建议，不承担所有权证明。
    return _new_owned_group(kind)


def _input_sockets(group):
    return tuple(
        item
        for item in group.interface.items_tree
        if getattr(item, "item_type", None) == "SOCKET" and item.in_out == "INPUT"
    )


def socket_identifier(group, name):
    socket = next((item for item in _input_sockets(group) if item.name == name), None)
    return socket.identifier if socket is not None else None


def set_modifier_inputs(modifier, values):
    """Set GN modifier inputs by stable interface names.

    Blender exposes modifier sockets as ``Socket_0``-style ID properties. Names
    are resolved from the interface so a future socket reorder cannot silently
    feed the wrong parameter.
    """

    group = modifier.node_group
    if group is None:
        return
    changed = False
    for name, value in values.items():
        identifier = socket_identifier(group, name)
        if not identifier:
            continue
        try:
            current = modifier.get(identifier, None)
            if current == value:
                continue
            modifier[identifier] = value
            changed = True
        except (TypeError, ValueError, AttributeError):
            # A missing optional socket should not break canonical edits.
            continue
    # IDProperty assignment alone does not invalidate the evaluated GN mesh
    # in Blender 4.0: the socket changes while a sphere keeps its old radius.
    # Tag only this modifier's owner, and only after a real value change.
    # Do not rebuild/recompile the shared node-group interface on every drag.
    if changed:
        modifier.id_data.update_tag(refresh={"DATA"})


def material_socket_values(modifier):
    """返回 modifier 中实际绑定的 Material socket 值。"""

    group = getattr(modifier, "node_group", None)
    if group is None:
        return ()
    values = []
    for socket in _input_sockets(group):
        if socket.socket_type != "NodeSocketMaterial":
            continue
        try:
            value = modifier.get(socket.identifier, None)
        except (AttributeError, TypeError, ValueError):
            value = None
        if isinstance(value, bpy.types.Material):
            values.append(value)
    return tuple(values)


def ensure_modifier(obj, kind, material=None, values=None):
    """Ensure one marked NODES modifier on a Curve object and update inputs."""

    group = ensure_node_group(kind)
    modifier = next(
        (
            current
            for current in obj.modifiers
            if current.type == "NODES"
            and current.name == "HD2PB 实体预览几何"
            and (
                is_owned_node_group(current.node_group, kind, current_version=False)
                or (
                    current.node_group is not None
                    and current.node_group.get(GN_KIND_KEY) == kind
                    and current.node_group.get(GN_SCHEMA_KEY) == 1
                )
            )
        ),
        None,
    )
    if modifier is None:
        modifier = obj.modifiers.new("HD2PB 实体预览几何", "NODES")
    modifier.node_group = group
    # 可撤销修复不能立即删除刚解除引用的旧节点组，否则 Blender Undo 可能无法
    # 恢复原 modifier 身份。旧组不设 Fake User，零用户资源不会随正常保存持久化。
    inputs = dict(values or {})
    if material is not None:
        inputs[SOCKET_MATERIAL] = material
    set_modifier_inputs(modifier, inputs)
    return modifier


def modifiers_for(obj):
    return tuple(
        modifier
        for modifier in obj.modifiers
        if modifier.type == "NODES" and modifier.node_group is not None
    )


__all__ = (
    "GN_OWNER_KEY",
    "GN_OWNER_VALUE",
    "GN_SCHEMA_KEY",
    "GN_KIND_KEY",
    "GN_SIGNATURE_KEY",
    "GN_VERSION",
    "SOCKET_GEOMETRY",
    "SOCKET_MATERIAL",
    "is_owned_node_group",
    "is_migratable_node_group",
    "ensure_node_group",
    "ensure_modifier",
    "set_modifier_inputs",
    "material_socket_values",
    "socket_identifier",
    "modifiers_for",
)
