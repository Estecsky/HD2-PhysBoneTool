"""Curve + Geometry Nodes 实体预览与高频增量刷新。

实体表面预览都使用 Curve Object 作为宿主，Geometry Nodes 只在求值阶段生成显示几何；
插件不会创建 Mesh Object 或 Mesh datablock。Chain 曲线与 Collider 实体可作为选择入口，
Node 半径和角限实体不可选择；角限另有一个仅允许旋转的轴 Empty 作为选择入口。任何
预览仍不参与当前 HD2PHY1 导出。
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
import math
import time

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector

from ..core import reference as core_reference
from . import ownership, preview_geometry, preview_nodes, properties
from . import preview_layers


VISUAL_REFRESH_DELAY = 1.0 / 120.0
HIDDEN_REFRESH_DELAY = 0.25

ASPECT_ALL = "ALL"
ASPECT_GEOMETRY = "GEOMETRY"
ASPECT_RADIUS = "RADIUS"
ASPECT_COLLIDER_POINTS = "COLLIDER_POINTS"
ASPECT_LIMIT = "LIMIT"
ASPECT_REFERENCE = "REFERENCE"
ASPECT_LABEL = "LABEL"
ASPECT_STYLE = "STYLE"
ASPECT_PLANE_GEOMETRY = "PLANE_GEOMETRY"
ASPECT_LIMIT_PREVIEW = "LIMIT_PREVIEW"

DATA_SCHEMA_KEY = "hd2pb_preview_schema"
DATA_PROJECT_KEY = "hd2pb_project_uuid"
DATA_OWNER_KEY = "hd2pb_owner_object"
DATA_ENTITY_TYPE_KEY = "hd2pb_entity_type"
DATA_ENTITY_KEY = "hd2pb_entity_uuid"
DATA_KIND_KEY = "hd2pb_preview_kind"
DATA_UUID_KEY = "hd2pb_preview_uuid"
DATA_CHAIN_KEY = "hd2pb_chain_uuid"
DATA_MATERIAL_VARIANT_KEY = "hd2pb_material_variant"
DISPLAY_NAME_KEY = "hd2pb_display_name"
COLLIDER_ENDPOINT_KEY = "hd2pb_collider_endpoint"
COLLIDER_KIND_KEY = "hd2pb_collider_kind"
_BLENDER_ID_NAME_BYTES = 63

_KIND_LABELS = {
    properties.PREVIEW_CHAIN_CURVE: "骨链曲线",
    properties.PREVIEW_NODE_RADIUS: "节点半径",
    properties.PREVIEW_NODE_LIMIT: "节点角限",
    properties.PREVIEW_NODE_LIMIT_AXIS: "角限旋转轴",
    properties.PREVIEW_COLLIDER_SHAPE: "碰撞体形状",
    properties.PREVIEW_CHAIN_LINK: "链组连接曲线",
    properties.PREVIEW_LINK_COLLISION: "链间碰撞体",
    properties.PREVIEW_COLLIDER_ENDPOINT: "碰撞体端点名称",
}

_DIRTY = {}
_DEFERRED = {}
_DEADLINE = 0.0
_SUSPEND_DEPTH = 0
_REGISTERED = False
_DATA_CHANGING = False
_FLUSHING = False
_KNOWN_OWNERS = {}
_REST_SIGNATURES = {}
_REST_PENDING = {}


@dataclass(frozen=True, slots=True)
class PreviewSpec:
    """一个 Curve 宿主及其 GN 输入的不可变描述。"""

    gn_kind: str
    curve: preview_geometry.CurvePayload = preview_geometry.EMPTY_CURVE
    faces: tuple[preview_geometry.Triangle, ...] = ()
    inputs: tuple[tuple[str, object], ...] = ()
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    axis_rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    hook_bones: tuple[str, ...] = ()
    parent_bone: str = ""

    def modifier_inputs(self):
        return dict(self.inputs)


def _safe_label(value, fallback):
    value = str(value or "").strip()
    return (value or fallback)[:48]


def _trim_utf8(value, byte_limit):
    """按 Blender ID 名称的 UTF-8 字节上限截断，不切断多字节字符。"""

    text = str(value or "").strip()
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    return encoded[: max(int(byte_limit), 0)].decode("utf-8", errors="ignore").rstrip()


def _trim_utf8_tail(value, byte_limit):
    """从尾部保留 UTF-8 文本，不切断多字节字符。"""

    text = str(value or "").strip()
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    return encoded[-max(int(byte_limit), 0) :].decode("utf-8", errors="ignore").lstrip()


def _compact_utf8(value, byte_limit):
    """在小预算内同时保留名称头尾，避免左右骨常见后缀被截掉。"""

    text = str(value or "").strip()
    byte_limit = max(int(byte_limit), 0)
    if len(text.encode("utf-8")) <= byte_limit:
        return text
    ellipsis = "…"
    ellipsis_bytes = len(ellipsis.encode("utf-8"))
    if byte_limit <= ellipsis_bytes:
        return _trim_utf8(text, byte_limit)
    content_budget = byte_limit - ellipsis_bytes
    head_budget = (content_budget + 1) // 2
    tail_budget = content_budget - head_budget
    return f"{_trim_utf8(text, head_budget)}{ellipsis}{_trim_utf8_tail(text, tail_budget)}"


def _fit_id_name(label, suffix=""):
    """保留稳定后缀，并把 Object 名称限制在 Blender 的 63 字节内。"""

    suffix = _trim_utf8(suffix, _BLENDER_ID_NAME_BYTES)
    budget = _BLENDER_ID_NAME_BYTES - len(suffix.encode("utf-8"))
    prefix = _trim_utf8(label, budget)
    return f"{prefix}{suffix}" or "物理骨对象"


def _fit_paired_id_name(prefix, stem, first, second):
    """在 63 字节对象名内均衡保留胶囊两端骨名。"""

    prefix = str(prefix or "")
    stem = str(stem or "")
    first = str(first or "")
    second = str(second or "")
    full = f"{prefix}{stem}{first}↔{second}"
    if len(full.encode("utf-8")) <= _BLENDER_ID_NAME_BYTES:
        return full

    arrow = "↔"
    # 先给两端各保留最少 9 字节，再压缩可能被用户改得很长的名称前缀。
    fixed_budget = _BLENDER_ID_NAME_BYTES - len(prefix.encode("utf-8")) - len(
        arrow.encode("utf-8")
    )
    # 9 字节足以容纳“一个中文字符 + 省略号 + 一个中文字符”，确保
    # 最坏情况下也不会把纯中文双端都压成无法区分的“…↔…”。
    endpoint_floor = 9
    stem_budget = max(fixed_budget - endpoint_floor * 2, 0)
    stem = _compact_utf8(stem, stem_budget)
    endpoint_budget = max(
        _BLENDER_ID_NAME_BYTES
        - len(f"{prefix}{stem}{arrow}".encode("utf-8")),
        0,
    )
    first_size = len(first.encode("utf-8"))
    second_size = len(second.encode("utf-8"))
    first_budget = min(first_size, endpoint_budget // 2)
    second_budget = min(second_size, endpoint_budget - first_budget)
    remaining = endpoint_budget - first_budget - second_budget
    if remaining and first_budget < first_size:
        extra = min(remaining, first_size - first_budget)
        first_budget += extra
        remaining -= extra
    if remaining and second_budget < second_size:
        second_budget += min(remaining, second_size - second_budget)
    return _fit_id_name(
        f"{prefix}{stem}{_compact_utf8(first, first_budget)}"
        f"{arrow}{_compact_utf8(second, second_budget)}"
    )


def collider_display_label(collider, prefix):
    """生成 Collider 的 Outliner 名称；双骨胶囊始终兼顾显示两端。"""

    display_name = str(getattr(collider, "display_name", "") or "未命名").strip()
    first = str(getattr(collider, "bone_name", "") or "").strip()
    second = str(getattr(collider, "bone_name_b", "") or "").strip()
    endpoint_text = f"{first}↔{second}"
    if (
        getattr(collider, "kind", "") == "CAPSULE"
        and first
        and second
        and display_name.endswith(endpoint_text)
    ):
        return _fit_paired_id_name(
            prefix,
            display_name[: -len(endpoint_text)],
            first,
            second,
        )
    return _fit_id_name(f"{prefix}{display_name}")


def _generated_name_identity(obj):
    """返回可读且稳定的生成对象身份；不依赖 Blender 自动数字后缀。"""

    project = str(getattr(obj, "hd2pb_project_uuid", ""))[:8] or "project"
    if getattr(obj, "hd2pb_preview_schema", 0):
        roles = {
            properties.PREVIEW_CHAIN_CURVE: "chain",
            properties.PREVIEW_NODE_RADIUS: "radius",
            properties.PREVIEW_NODE_LIMIT: "limit",
            properties.PREVIEW_NODE_LIMIT_AXIS: "limit-axis",
            properties.PREVIEW_COLLIDER_SHAPE: "collider",
            properties.PREVIEW_CHAIN_LINK: "chain-link",
            properties.PREVIEW_LINK_COLLISION: "link-collision",
            properties.PREVIEW_COLLIDER_ENDPOINT: "collider-endpoint",
        }
        role = roles.get(getattr(obj, "hd2pb_preview_kind", ""), "preview")
        unique = str(getattr(obj, "hd2pb_preview_uuid", ""))
    else:
        roles = {
            properties.ENTITY_PROJECT: "project",
            properties.ENTITY_PROFILE: "profile",
            properties.ENTITY_CHAIN: "chain-control",
            properties.ENTITY_NODE: "node",
            properties.ENTITY_COLLIDER: "collider-control",
            properties.ENTITY_CHAIN_LINK: "chain-link-control",
            properties.ENTITY_LINK_COLLISION: "link-collision-control",
        }
        role = roles.get(getattr(obj, "hd2pb_entity_type", ""), "control")
        unique = str(getattr(obj, "hd2pb_controller_uuid", ""))
    unique = unique.replace("-", "") or str(getattr(obj, "hd2pb_entity_uuid", ""))
    return role, project, unique.replace("-", "")


def _qualified_fallback_name(desired, obj):
    """生成稳定的全局唯一名；只改当前生成对象，绝不重命名占用者。"""

    role, project, unique = _generated_name_identity(obj)
    for width in (8, 12, 20, 32):
        token = unique[:width] or "unknown"
        candidate = _fit_id_name(desired, f" · {role}#{token}@{project}")
        occupant = bpy.data.objects.get(candidate)
        if occupant is None or occupant is obj:
            return candidate
    # 只有复制了内部 UUID 的损坏投影才会走到这里。指针只用于本次修复会话，
    # 正常新建对象始终使用上面的持久 UUID 分支。
    return _fit_id_name(desired, f" · {role}#{obj.as_pointer():x}")


def _readable_fallback_name(desired, obj):
    """为会直接显示在视图中的对象分配三位可读序号。"""

    index = 2
    while True:
        suffix = f" · {index:03d}"
        budget = _BLENDER_ID_NAME_BYTES - len(suffix.encode("utf-8"))
        candidate = f"{_trim_utf8(desired, budget)}{suffix}"
        occupant = bpy.data.objects.get(candidate)
        if occupant is None or occupant is obj:
            return candidate
        index += 1


def set_display_name(obj, name, *, readable_fallback=False):
    """记录用户可读名，并用稳定规则解决 Blender 全局对象名冲突。"""

    desired = _fit_id_name(str(name or "物理骨对象").strip() or "物理骨对象")
    obj[DISPLAY_NAME_KEY] = desired
    occupant = bpy.data.objects.get(desired)
    if occupant is None or occupant is obj:
        try:
            obj.name = desired
        except (AttributeError, RuntimeError, TypeError):
            pass
        return obj.name
    try:
        obj.name = (
            _readable_fallback_name(desired, obj)
            if readable_fallback
            else _qualified_fallback_name(desired, obj)
        )
    except (AttributeError, RuntimeError, TypeError):
        pass
    return obj.name


def normalize_display_names(objects):
    """Normalize generated Object names after a staged projection swap.

    This is deliberately best-effort: a user object with the requested name is
    never renamed, and failure to normalize a cosmetic name cannot invalidate the
    canonical project or its preview geometry.
    """

    for obj in tuple(objects or ()):
        if obj is None or not _id_exists(obj, bpy.data.objects):
            continue
        desired = str(obj.get(DISPLAY_NAME_KEY, "")).strip()
        if not desired:
            continue
        set_display_name(obj, desired)


def _id_exists(value, collection):
    try:
        return value is not None and collection.get(value.name) == value
    except (ReferenceError, AttributeError):
        return False


def _live_owner(owner):
    return _id_exists(owner, bpy.data.objects) and owner.type == "ARMATURE"


def _tag_generated(
    datablock,
    owner,
    project_uuid,
    entity_type,
    entity_uuid,
    preview_kind,
    preview_uuid,
    *,
    chain_uuid="",
):
    datablock[DATA_SCHEMA_KEY] = properties.PREVIEW_SCHEMA_VERSION
    datablock[DATA_PROJECT_KEY] = project_uuid
    datablock[DATA_OWNER_KEY] = owner
    datablock[DATA_ENTITY_TYPE_KEY] = entity_type
    datablock[DATA_ENTITY_KEY] = entity_uuid
    datablock[DATA_KIND_KEY] = preview_kind
    datablock[DATA_UUID_KEY] = preview_uuid
    datablock[DATA_CHAIN_KEY] = chain_uuid


def is_preview_object(obj):
    return bool(getattr(obj, "hd2pb_preview_schema", 0))


def is_exact_preview_object(obj, owner, project_uuid):
    entity_type = getattr(obj, "hd2pb_entity_type", properties.ENTITY_NONE)
    entity_uuid = getattr(obj, "hd2pb_entity_uuid", "")
    kind = getattr(obj, "hd2pb_preview_kind", properties.PREVIEW_NONE)
    return bool(
        is_preview_object(obj)
        and obj.hd2pb_preview_schema == properties.PREVIEW_SCHEMA_VERSION
        and obj.hd2pb_owner_object == owner
        and obj.hd2pb_project_uuid == project_uuid
        and entity_type
        in {
            properties.ENTITY_CHAIN,
            properties.ENTITY_NODE,
            properties.ENTITY_COLLIDER,
            properties.ENTITY_CHAIN_LINK,
            properties.ENTITY_LINK_COLLISION,
        }
        and ownership.is_valid_uuid(entity_uuid)
        and kind in _KIND_LABELS
        and ownership.is_valid_uuid(obj.hd2pb_preview_uuid)
    )


def is_owned_preview_datablock(datablock, owner, project_uuid):
    return bool(
        datablock is not None
        and datablock.get(DATA_SCHEMA_KEY) == properties.PREVIEW_SCHEMA_VERSION
        and datablock.get(DATA_OWNER_KEY) == owner
        and datablock.get(DATA_PROJECT_KEY) == project_uuid
        and datablock.get(DATA_KIND_KEY) in _KIND_LABELS
        and ownership.is_valid_uuid(datablock.get(DATA_UUID_KEY, ""))
    )


def material_variant_is_valid(kind, variant):
    """材质变体只用于同一预览 kind 内的可视状态，不改变实体类型。"""

    return variant == ""


def _is_exact_preview_material(material, owner, project_uuid, kind, variant=""):
    return bool(
        is_owned_preview_datablock(material, owner, project_uuid)
        and material.get(DATA_ENTITY_TYPE_KEY) == properties.ENTITY_PROJECT
        and material.get(DATA_ENTITY_KEY) == project_uuid
        and material.get(DATA_KIND_KEY) == kind
        and material.get(DATA_CHAIN_KEY, "") == ""
        and material.get(DATA_MATERIAL_VARIANT_KEY, "") == variant
        and material_variant_is_valid(kind, variant)
    )


def _is_exact_preview_data(data, obj, owner, project_uuid):
    return bool(
        is_owned_preview_datablock(data, owner, project_uuid)
        and data.get(DATA_ENTITY_TYPE_KEY) == obj.hd2pb_entity_type
        and data.get(DATA_ENTITY_KEY) == obj.hd2pb_entity_uuid
        and data.get(DATA_KIND_KEY) == obj.hd2pb_preview_kind
        and data.get(DATA_CHAIN_KEY, "") == obj.get(DATA_CHAIN_KEY, "")
    )


def _iter_collection_tree(root):
    if root is None:
        return
    pending = [root]
    seen = set()
    while pending:
        collection = pending.pop()
        pointer = collection.as_pointer()
        if pointer in seen:
            continue
        seen.add(pointer)
        yield collection
        pending.extend(reversed(tuple(collection.children)))


def build_project_index(owner):
    """只遍历当前项目根集合，构造 controller/preview 局部索引。"""

    project = ownership.ensure_authoring_owner(owner)
    root = project.controller_collection
    if root is None or not _id_exists(root, bpy.data.collections):
        raise ownership.HD2PBError("项目的 Outliner 根集合缺失；请执行“修复编辑投影”")
    controllers = defaultdict(list)
    previews = defaultdict(list)
    materials = defaultdict(list)
    material_local_users = defaultdict(int)
    counted_data = set()
    for collection in _iter_collection_tree(root):
        for obj in collection.objects:
            if getattr(obj, "hd2pb_controller_schema", 0):
                controllers[(obj.hd2pb_entity_type, obj.hd2pb_entity_uuid)].append(obj)
            elif getattr(obj, "hd2pb_preview_schema", 0):
                previews[
                    (obj.hd2pb_entity_type, obj.hd2pb_entity_uuid, obj.hd2pb_preview_kind)
                ].append(obj)
                for material in getattr(obj.data, "materials", ()) if obj.data else ():
                    if material is not None:
                        variant = str(material.get(DATA_MATERIAL_VARIANT_KEY, ""))
                        materials[(obj.hd2pb_preview_kind, variant)].append(material)
                        material_local_users[material.as_pointer()] += 1
                if obj.data is not None:
                    counted_data.add(obj.data)
                for modifier in getattr(obj, "modifiers", ()):
                    for value in preview_nodes.material_socket_values(modifier):
                        material_local_users[value.as_pointer()] += 1
    # An interrupted old projection may leave an exact, tagged Curve datablock
    # without an Object user. It is still an internal material reference until
    # the next successful cleanup, so include its slots in the local count.
    for data in bpy.data.curves:
        if data in counted_data:
            continue
        if (
            data.get(DATA_SCHEMA_KEY) == properties.PREVIEW_SCHEMA_VERSION
            and data.get(DATA_OWNER_KEY) == owner
            and data.get(DATA_PROJECT_KEY) == project.project_uuid
        ):
            for material in data.materials:
                if material is not None:
                    material_local_users[material.as_pointer()] += 1
    return {
        "owner": owner,
        "project": project,
        "root": root,
        "controllers": controllers,
        "previews": previews,
        "materials": materials,
        "material_local_users": material_local_users,
    }


def material_reference_count(objects, material, datablocks=()):
    """统计项目内对材质的引用，包括暂时没有 Object 用户的孤儿数据块。

    Blender 在删除 Object 后不会自动删除其 Curve 数据块；在事务式重建或
    清理故障的短窗口内，这个孤儿 Curve 仍会持有材质槽。调用方可以把已确认
    属于本项目的额外数据块传入，避免把这种内部残留误判成外部用户。已由
    ``objects`` 使用的数据块会自动去重，防止同一个材质槽被计算两次。
    """

    count = 0
    used_data = set()
    for obj in objects:
        data = getattr(obj, "data", None)
        if data is not None:
            used_data.add(data)
            count += sum(
                1 for current in getattr(data, "materials", ()) if current == material
            )
        for modifier in getattr(obj, "modifiers", ()):
            count += sum(
                1
                for value in preview_nodes.material_socket_values(modifier)
                if value == material
            )
    for data in datablocks:
        if data is None or data in used_data:
            continue
        count += sum(1 for current in getattr(data, "materials", ()) if current == material)
    return count


def project_preview_objects(owner):
    """返回项目根集合内带 preview 标记的对象；不扫描全场景。"""

    project = owner.data.hd2pb_project
    root = project.controller_collection
    if root is None or not _id_exists(root, bpy.data.collections):
        return []
    return [
        obj
        for collection in _iter_collection_tree(root)
        for obj in collection.objects
        if getattr(obj, "hd2pb_preview_schema", 0)
    ]


def _expected_material_variant(project, entity_type, entity_uuid, kind):
    if kind != properties.PREVIEW_CHAIN_CURVE or entity_type != properties.ENTITY_CHAIN:
        return ""
    result = ownership.find_entity(project, entity_type, entity_uuid)
    if result is None:
        raise ownership.HD2PBError("骨链曲线引用的 canonical Chain 已不存在")
    return ""


def _kind_color(project, kind, variant=""):
    field = {
        properties.PREVIEW_CHAIN_CURVE: "preview_chain_color",
        properties.PREVIEW_NODE_RADIUS: "preview_node_color",
        properties.PREVIEW_NODE_LIMIT: "preview_limit_color",
        properties.PREVIEW_NODE_LIMIT_AXIS: "preview_limit_color",
        properties.PREVIEW_COLLIDER_SHAPE: "preview_collider_color",
        properties.PREVIEW_CHAIN_LINK: "preview_chain_link_color",
        properties.PREVIEW_LINK_COLLISION: "preview_link_collision_color",
        properties.PREVIEW_COLLIDER_ENDPOINT: "preview_collider_color",
    }[kind]
    return tuple(float(value) for value in getattr(project, field))


def node_display_radius(radius):
    """返回 Node Empty 与 GN 半径球共同使用的 Blender 显示半径。"""

    return max(float(radius), properties.NODE_DISPLAY_MIN_RADIUS)


def _configure_material(material, color):
    material.diffuse_color = color
    material.use_nodes = True
    nodes = material.node_tree.nodes if material.node_tree else None
    principled = nodes.get("Principled BSDF") if nodes else None
    if principled is not None:
        base = principled.inputs.get("Base Color")
        alpha = principled.inputs.get("Alpha")
        roughness = principled.inputs.get("Roughness")
        if base is not None:
            base.default_value = color
        if alpha is not None:
            alpha.default_value = color[3]
        if roughness is not None:
            roughness.default_value = 0.42
    if hasattr(material, "blend_method"):
        material.blend_method = "BLEND" if color[3] < 0.999 else "OPAQUE"
    if hasattr(material, "show_transparent_back"):
        material.show_transparent_back = True


def new_stage():
    return {"objects": [], "data": [], "materials": [], "material_by_kind": {}}


def _ensure_stage_material(stage, owner, project, kind, variant=""):
    key = (kind, variant)
    material = stage["material_by_kind"].get(key)
    if material is not None:
        return material
    preview_uuid = ownership.new_uuid()
    variant_label = ""
    material = bpy.data.materials.new(
        f"HD2PB · {_KIND_LABELS[kind]}{variant_label} · {_safe_label(owner.name, '骨架')}"
    )
    try:
        _tag_generated(
            material,
            owner,
            project.project_uuid,
            properties.ENTITY_PROJECT,
            project.project_uuid,
            kind,
            preview_uuid,
        )
        material[DATA_MATERIAL_VARIANT_KEY] = variant
        _configure_material(material, _kind_color(project, kind, variant))
    except Exception:
        if _id_exists(material, bpy.data.materials):
            bpy.data.materials.remove(material)
        raise
    stage["materials"].append(material)
    stage["material_by_kind"][key] = material
    return material


def _set_curve_points(data, payload, faces=()):
    data.splines.clear()
    if not payload.points:
        return
    if faces:
        point_count = len(payload.points)
        for face in faces:
            if len(face) != 3 or any(index < 0 or index >= point_count for index in face):
                raise ownership.HD2PBError("角限 Curve 包含无效三角面索引")
            spline = data.splines.new("POLY")
            spline.points.add(2)
            for point, index in zip(spline.points, face):
                point.co = (*payload.points[index], 1.0)
            spline.use_cyclic_u = True
        return
    spline = data.splines.new("POLY")
    spline.points.add(len(payload.points) - 1)
    for point, value in zip(spline.points, payload.points):
        point.co = (*value, 1.0)
    spline.use_cyclic_u = bool(payload.cyclic)


def _new_curve_data(name, spec, owner, project, entity_type, entity_uuid, kind, chain_uuid):
    data = bpy.data.curves.new(name, type="CURVE")
    try:
        preview_uuid = ownership.new_uuid()
        _tag_generated(
            data,
            owner,
            project.project_uuid,
            entity_type,
            entity_uuid,
            kind,
            preview_uuid,
            chain_uuid=chain_uuid,
        )
        data.dimensions = "3D"
        data.resolution_u = 1
        data.bevel_depth = 0.0
        _set_curve_points(data, spec.curve, spec.faces)
    except Exception:
        if _id_exists(data, bpy.data.curves):
            bpy.data.curves.remove(data)
        raise
    return data


def _set_preview_identity(obj, owner, project, entity_type, entity_uuid, kind, chain_uuid):
    preview_uuid = ownership.new_uuid()
    obj.hd2pb_preview_schema = properties.PREVIEW_SCHEMA_VERSION
    obj.hd2pb_project_uuid = project.project_uuid
    obj.hd2pb_owner_object = owner
    obj.hd2pb_entity_type = entity_type
    obj.hd2pb_entity_uuid = entity_uuid
    obj.hd2pb_preview_kind = kind
    obj.hd2pb_preview_uuid = preview_uuid
    obj[DATA_CHAIN_KEY] = chain_uuid


def _effective_isolation_uuid(project):
    isolated = project.preview_isolate_entity_uuid
    if not isolated:
        return ""
    for entity_type, entity_uuid, _entity, _parent in ownership.entity_entries(project):
        if (
            entity_type
            in {
                properties.ENTITY_CHAIN,
                properties.ENTITY_NODE,
                properties.ENTITY_COLLIDER,
                properties.ENTITY_CHAIN_LINK,
                properties.ENTITY_LINK_COLLISION,
            }
            and entity_uuid == isolated
        ):
            return isolated
    return ""


def _visible(project, obj):
    if not project.preview_enabled:
        return False
    category = {
        properties.PREVIEW_CHAIN_CURVE: project.preview_show_chains,
        properties.PREVIEW_NODE_RADIUS: project.preview_show_nodes,
        properties.PREVIEW_NODE_LIMIT: project.preview_show_limits,
        properties.PREVIEW_NODE_LIMIT_AXIS: project.preview_show_limits,
        properties.PREVIEW_COLLIDER_SHAPE: project.preview_show_colliders,
        properties.PREVIEW_CHAIN_LINK: project.preview_show_chain_links,
        properties.PREVIEW_LINK_COLLISION: project.preview_show_link_collisions,
        properties.PREVIEW_COLLIDER_ENDPOINT: project.preview_show_colliders,
    }.get(obj.hd2pb_preview_kind, False)
    if not category:
        return False
    isolated = _effective_isolation_uuid(project)
    return not isolated or obj.hd2pb_entity_uuid == isolated or obj.get(DATA_CHAIN_KEY, "") == isolated


def _style_object(obj, project):
    variant = _expected_material_variant(
        project,
        obj.hd2pb_entity_type,
        obj.hd2pb_entity_uuid,
        obj.hd2pb_preview_kind,
    )
    color = _kind_color(project, obj.hd2pb_preview_kind, variant)
    obj.color = color
    obj.hide_viewport = not _visible(project, obj)
    obj.hide_render = True
    obj.show_wire = obj.hd2pb_preview_kind == properties.PREVIEW_NODE_LIMIT
    obj.show_all_edges = obj.hd2pb_preview_kind == properties.PREVIEW_NODE_LIMIT
    selectable = obj.hd2pb_preview_kind in {
        properties.PREVIEW_CHAIN_CURVE,
        properties.PREVIEW_NODE_LIMIT_AXIS,
        properties.PREVIEW_COLLIDER_SHAPE,
        properties.PREVIEW_CHAIN_LINK,
        properties.PREVIEW_LINK_COLLISION,
    }
    obj.hide_select = not selectable
    preview_layers.style(obj, project)
    if obj.hd2pb_preview_kind == properties.PREVIEW_COLLIDER_SHAPE:
        # 碰撞体名称由独立开关控制；胶囊整体名称固定关闭，端点 Empty
        # 会分别显示吸附骨名称，避免长标签覆盖在实体中部。
        collider_kind = obj.get(COLLIDER_KIND_KEY, "")
        if not collider_kind:
            try:
                resolved = ownership.find_entity(
                    project, properties.ENTITY_COLLIDER, obj.hd2pb_entity_uuid
                )
                collider_kind = str(resolved[0].kind) if resolved is not None else ""
                if collider_kind:
                    obj[COLLIDER_KIND_KEY] = collider_kind
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                collider_kind = ""
        obj.show_name = bool(
            project.preview_show_collider_names
            and collider_kind != "CAPSULE"
        )
    else:
        obj.show_name = False
    if obj.hd2pb_preview_kind == properties.PREVIEW_COLLIDER_ENDPOINT:
        obj.empty_display_type = "PLAIN_AXES"
        obj.empty_display_size = 0.025
        obj.hide_select = True
        obj.lock_location = (True, True, True)
        obj.lock_rotation = (True, True, True)
        obj.lock_scale = (True, True, True)
        obj.show_name = bool(
            project.preview_show_collider_names
        )
        return
    if obj.hd2pb_preview_kind == properties.PREVIEW_NODE_LIMIT_AXIS:
        obj.show_name = False
        obj.empty_display_type = "ARROWS"
        obj.hide_select = False
        obj.lock_location = (True, True, True)
        obj.lock_rotation = (False, False, False)
        obj.lock_scale = (True, True, True)
        return
    collider_transform = bool(
        obj.hd2pb_preview_kind == properties.PREVIEW_COLLIDER_SHAPE
        and project.preview_allow_collider_transform
    )
    locked = not collider_transform
    obj.lock_location = (locked, locked, locked)
    obj.lock_rotation = (locked, locked, locked)
    obj.lock_scale = (locked, locked, locked)
    if obj.hd2pb_preview_kind == properties.PREVIEW_COLLIDER_SHAPE and locked:
        obj.location = (0.0, 0.0, 0.0)
        obj.rotation_mode = "XYZ"
        obj.rotation_euler = (0.0, 0.0, 0.0)
        obj.scale = (1.0, 1.0, 1.0)


def _sync_chain_hooks(obj, owner, spec):
    for modifier in tuple(obj.modifiers):
        if modifier.type == "HOOK" and modifier.name.startswith("HD2PB 骨钩 · "):
            obj.modifiers.remove(modifier)
    first_nodes_index = next(
        (index for index, modifier in enumerate(obj.modifiers) if modifier.type == "NODES"),
        len(obj.modifiers),
    )
    for index, bone_name in enumerate(spec.hook_bones):
        bone = owner.data.bones.get(bone_name)
        if bone is None:
            raise ownership.HD2PBError(
                f"骨链曲线“{obj.name}”引用的骨骼“{bone_name}”不存在"
            )
        modifier = obj.modifiers.new(f"HD2PB 骨钩 · {bone_name}", "HOOK")
        modifier.object = owner
        modifier.subtarget = bone_name
        modifier.vertex_indices_set([index])
        # Blender 会在设置 bone target 时按“当前姿态”自动写 matrix_inverse。
        # 完整投影重建若发生在非静置姿态，这会把当前姿态误当成新绑定姿态，
        # 使曲线立即弹回 Rest 并在后续姿态中持续滞后。必须在 subtarget 之后
        # 显式固定到 Rest bone inverse，令重建时机不影响 Hook 求值。
        try:
            modifier.matrix_inverse = bone.matrix_local.inverted()
        except (RuntimeError, ValueError):
            raise ownership.HD2PBError(
                f"骨链曲线“{obj.name}”无法为骨骼“{bone_name}”建立静置姿态 Hook 绑定"
            )
        # Hook 必须在 Geometry Nodes 之前，才能让 GN 看到姿态后的曲线。
        # Blender 的 modifier collection 支持稳定的 move API；保留一个
        # 兼容回退，便于旧文件在受限上下文中继续刷新。
        try:
            current_index = next(
                position
                for position, current in enumerate(obj.modifiers)
                if current == modifier
            )
            if current_index > first_nodes_index:
                obj.modifiers.move(current_index, first_nodes_index)
            first_nodes_index += 1
        except (AttributeError, RuntimeError, ValueError):
            raise ownership.HD2PBError(
                f"骨链曲线“{obj.name}”无法把骨骼 Hook 排在 Geometry Nodes 之前"
            )

    nodes_index = next(
        (index for index, modifier in enumerate(obj.modifiers) if modifier.type == "NODES"),
        len(obj.modifiers),
    )
    managed_hooks = [
        (index, modifier)
        for index, modifier in enumerate(obj.modifiers)
        if modifier.type == "HOOK" and modifier.name.startswith("HD2PB 骨钩 · ")
    ]
    if len(managed_hooks) != len(spec.hook_bones) or any(
        index >= nodes_index for index, _modifier in managed_hooks
    ):
        raise ownership.HD2PBError(
            f"骨链曲线“{obj.name}”的 Hook 数量或修改器顺序无效"
        )


def _sync_geometry_nodes(obj, spec, material):
    for modifier in tuple(obj.modifiers):
        group = getattr(modifier, "node_group", None)
        managed = bool(
            modifier.type == "NODES"
            and modifier.name == "HD2PB 实体预览几何"
            and group is not None
            and (
                preview_nodes.is_owned_node_group(group, current_version=False)
                or group.get(preview_nodes.GN_SCHEMA_KEY) == 1
            )
        )
        if (
            managed
            and group.get(preview_nodes.GN_KIND_KEY) != spec.gn_kind
        ):
            obj.modifiers.remove(modifier)
    return preview_nodes.ensure_modifier(
        obj,
        spec.gn_kind,
        material,
        spec.modifier_inputs(),
    )


def _expected_gn_kind(project, obj):
    kind = obj.hd2pb_preview_kind
    if kind == properties.PREVIEW_CHAIN_CURVE:
        return "CHAIN"
    if kind == properties.PREVIEW_CHAIN_LINK:
        return "CHAIN"
    if kind == properties.PREVIEW_LINK_COLLISION:
        return "LINK_COLLISION"
    if kind == properties.PREVIEW_NODE_RADIUS:
        return "NODE_RADIUS"
    if kind == properties.PREVIEW_NODE_LIMIT:
        return "LIMIT"
    if kind == properties.PREVIEW_COLLIDER_SHAPE:
        result = ownership.find_entity(
            project,
            properties.ENTITY_COLLIDER,
            obj.hd2pb_entity_uuid,
        )
        if result is None:
            raise ownership.HD2PBError("碰撞体预览引用的 canonical 实体已经不存在")
        collider, _parent = result
        if collider.kind not in {"SPHERE", "CAPSULE", "PLANE"}:
            raise ownership.HD2PBError(f"未知碰撞体类型：{collider.kind}")
        return f"COLLIDER_{collider.kind}"
    raise ownership.HD2PBError("预览对象使用了未知的 Geometry Nodes 类型")


def _apply_spec(obj, spec, owner, project, material, *, sync_hooks=False):
    if obj.type != "CURVE" or obj.data is None:
        raise ownership.HD2PBError("实体预览宿主必须是 Curve Object")
    _set_curve_points(obj.data, spec.curve, spec.faces)
    if sync_hooks or any(
        modifier.type == "HOOK" and modifier.name.startswith("HD2PB 骨钩 · ")
        for modifier in obj.modifiers
    ):
        _sync_chain_hooks(obj, owner, spec)
    _sync_geometry_nodes(obj, spec, material)
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = spec.rotation
    obj.scale = (1.0, 1.0, 1.0)
    _style_object(obj, project)


def _attach_preview_object(obj, logical_parent, owner, spec):
    """把普通 helper 挂到 controller，把角限 helper 直接挂到当前节点骨骼。"""

    obj.matrix_parent_inverse = Matrix.Identity(4)
    if spec.parent_bone:
        bone = owner.data.bones.get(spec.parent_bone)
        if bone is None:
            raise ownership.HD2PBError(
                f"角限预览引用的父骨骼“{spec.parent_bone}”不存在"
            )
        obj.parent = owner
        obj.parent_type = "BONE"
        obj.parent_bone = spec.parent_bone
        # Blender Bone Parent 的局部原点位于骨尾；移回骨头作为锥顶。
        obj.location = (0.0, -bone.length, 0.0)
        return
    obj.parent = logical_parent
    obj.parent_type = "OBJECT"
    obj.parent_bone = ""
    obj.location = (0.0, 0.0, 0.0)


def _new_preview_object(
    collection,
    parent,
    owner,
    project,
    entity_type,
    entity_uuid,
    kind,
    name,
    spec,
    stage,
    *,
    chain_uuid="",
):
    data_name = f"{name} · Curve 宿主"
    data = _new_curve_data(
        data_name, spec, owner, project, entity_type, entity_uuid, kind, chain_uuid
    )
    stage["data"].append(data)
    obj = bpy.data.objects.new(name, data)
    try:
        collection.objects.link(obj)
        _set_preview_identity(
            obj, owner, project, entity_type, entity_uuid, kind, chain_uuid
        )
        if kind == properties.PREVIEW_COLLIDER_SHAPE:
            result = ownership.find_entity(project, properties.ENTITY_COLLIDER, entity_uuid)
            if result is not None:
                obj[COLLIDER_KIND_KEY] = str(result[0].kind)
        set_display_name(obj, name)
        variant = _expected_material_variant(project, entity_type, entity_uuid, kind)
        material = _ensure_stage_material(stage, owner, project, kind, variant)
        data.materials.append(material)
        _apply_spec(
            obj,
            spec,
            owner,
            project,
            material,
            sync_hooks=bool(spec.hook_bones),
        )
        _attach_preview_object(obj, parent, owner, spec)
        if bpy.context.view_layer is not None and obj.name in bpy.context.view_layer.objects:
            obj.select_set(False)
    except Exception:
        if _id_exists(obj, bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        if data in stage["data"]:
            stage["data"].remove(data)
        if data.users == 0 and _id_exists(data, bpy.data.curves):
            bpy.data.curves.remove(data)
        raise
    stage["objects"].append(obj)
    return obj


def _new_limit_axis_object(collection, owner, project, node, spec, stage, *, chain_uuid=""):
    """创建 RE Chain 风格的可选角限旋转轴 Empty。

    这是制作期派生对象：位置和缩放由当前 Node 骨骼绑定固定，仅旋转用于
    调整视口中角限实体的朝向。当前轴旋转仍属于制作期预览，只有后续明确应用
    到 HD2PHY1 参考方向时才会改变 canonical 数据。
    """

    name = f"{_safe_label(node.bone_name, '缺失骨骼')} · 角限轴"
    obj = bpy.data.objects.new(name, None)
    try:
        collection.objects.link(obj)
        _set_preview_identity(
            obj,
            owner,
            project,
            properties.ENTITY_NODE,
            node.uuid,
            properties.PREVIEW_NODE_LIMIT_AXIS,
            chain_uuid,
        )
        obj[DISPLAY_NAME_KEY] = _safe_label(name, "角限旋转轴")
        obj.empty_display_type = "ARROWS"
        obj.empty_display_size = max(node_display_radius(node.radius), 0.04)
        obj.hide_render = True
        obj.hide_select = False
        obj.show_name = False
        obj.parent = owner
        obj.parent_type = "BONE"
        obj.parent_bone = node.bone_name
        bone = owner.data.bones.get(node.bone_name)
        if bone is None:
            raise ownership.HD2PBError(f"角限旋转轴引用的骨骼“{node.bone_name}”不存在")
        obj.location = (0.0, -bone.length, 0.0)
        obj.rotation_mode = "XYZ"
        obj.rotation_euler = spec.axis_rotation
        obj.scale = (1.0, 1.0, 1.0)
        obj.lock_location = (True, True, True)
        obj.lock_rotation = (False, False, False)
        obj.lock_scale = (True, True, True)
        obj["hd2pb_limit_axis_base_rotation"] = tuple(
            float(value) for value in spec.axis_rotation
        )
        _style_object(obj, project)
    except Exception:
        if _id_exists(obj, bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        raise
    stage["objects"].append(obj)
    return obj


def _axis_rotation_changed(obj):
    current = tuple(float(value) for value in obj.rotation_euler)
    previous = tuple(float(value) for value in obj.get("hd2pb_limit_axis_base_rotation", ()))
    if len(previous) != 3:
        return True
    return any(abs(a - b) > 1.0e-5 for a, b in zip(current, previous))


def _capture_limit_axis_reference(axis):
    """把用户旋转的角限轴立即写回当前节点的父骨局部 canonical。"""

    try:
        owner = axis.hd2pb_owner_object
        if owner is None or owner.type != "ARMATURE":
            return False
        project = ownership.ensure_authoring_owner(owner)
        if axis.hd2pb_preview_kind != properties.PREVIEW_NODE_LIMIT_AXIS:
            return False
        preview_index = build_project_index(owner)
        matches = preview_index["previews"].get(
            (
                properties.ENTITY_NODE,
                axis.hd2pb_entity_uuid,
                properties.PREVIEW_NODE_LIMIT_AXIS,
            ),
            (),
        )
        if len(matches) != 1 or matches[0] != axis:
            return False
        _validate_limit_axis_for_update(preview_index, axis)
        result = ownership.find_entity(project, properties.ENTITY_NODE, axis.hd2pb_entity_uuid)
        if result is None:
            return False
        node, chain = result
        index = next(
            (position for position, item in enumerate(chain.nodes) if item.uuid == node.uuid),
            -1,
        )
        if index <= 0 or node.limit_kind == "NONE":
            return False
        previous_bone = owner.data.bones.get(chain.nodes[index - 1].bone_name)
        current_bone = owner.data.bones.get(node.bone_name)
        if previous_bone is None or current_bone is None:
            return False
        axis.rotation_mode = "XYZ"
        direction_current = axis.rotation_euler.to_matrix() @ Vector((0.0, 0.0, 1.0))
        direction_owner = current_bone.matrix_local.to_3x3() @ direction_current
        direction_parent = previous_bone.matrix_local.to_3x3().inverted_safe() @ direction_owner
        if direction_parent.length <= 1.0e-6:
            return False
        direction_parent.normalize()
        with suspend_updates():
            node.reference_direction = tuple(float(value) for value in direction_parent)
            node.reference_mode = "EXPLICIT_LOCAL"
        axis["hd2pb_limit_axis_base_rotation"] = tuple(
            float(value) for value in axis.rotation_euler
        )
        return True
    except (ownership.HD2PBError, AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False


def _sync_limit_axis_object(obj, owner, project, node, spec):
    """更新轴 Empty 的绑定和默认姿态，保留用户已经旋转的试摆结果。"""

    if obj.type != "EMPTY":
        raise ownership.HD2PBError(f"角限旋转轴“{obj.name}”必须是 Empty")
    if not _axis_rotation_changed(obj):
        obj.rotation_mode = "XYZ"
        obj.rotation_euler = spec.axis_rotation
    bone = owner.data.bones.get(node.bone_name)
    if bone is None:
        raise ownership.HD2PBError(f"角限旋转轴引用的骨骼“{node.bone_name}”不存在")
    obj.parent = owner
    obj.parent_type = "BONE"
    obj.parent_bone = node.bone_name
    obj.location = (0.0, -bone.length, 0.0)
    obj.empty_display_type = "ARROWS"
    obj.empty_display_size = max(node_display_radius(node.radius), 0.04)
    obj.lock_location = (True, True, True)
    obj.lock_rotation = (False, False, False)
    obj.lock_scale = (True, True, True)
    obj["hd2pb_limit_axis_base_rotation"] = tuple(
        float(value) for value in spec.axis_rotation
    )
    _style_object(obj, project)
    return obj


def _rest_head(owner, bone_name):
    bone = owner.data.bones.get(bone_name)
    if bone is None:
        raise ownership.HD2PBError(f"预览引用的骨骼“{bone_name}”不存在")
    return tuple(float(value) for value in bone.matrix_local.translation)


def _chain_curve_payload(owner, chain):
    """只投影模拟节点；单节点链用该骨自身方向保留一个可选择 Curve。"""

    simulated = list(chain.nodes)[1:]
    if not simulated:
        raise ownership.HD2PBError("骨链没有可投影的模拟节点")
    points = [_rest_head(owner, node.bone_name) for node in simulated]
    hooks = [node.bone_name for node in simulated]
    if len(points) == 1:
        bone = owner.data.bones.get(simulated[0].bone_name)
        if bone is None:
            raise ownership.HD2PBError(
                f"预览引用的骨骼“{simulated[0].bone_name}”不存在"
            )
        head = Vector(points[0])
        tail = Vector(bone.tail_local)
        if (tail - head).length <= 1.0e-6:
            direction = bone.matrix_local.to_3x3() @ Vector((0.0, 1.0, 0.0))
            if direction.length <= 1.0e-6:
                direction = Vector((0.0, 1.0, 0.0))
            direction.normalize()
            tail = head + direction * 0.02
        points.append(tuple(float(value) for value in tail))
        hooks.append(simulated[0].bone_name)
    return preview_geometry.make_chain_curve(points), tuple(hooks)


def _chain_spec(owner, project, chain):
    payload, hooks = _chain_curve_payload(owner, chain)
    return PreviewSpec(
        "CHAIN",
        curve=payload,
        inputs=(("Radius", float(project.preview_curve_radius)),),
        hook_bones=hooks,
    )


def _chain_link_endpoints(project, link):
    chains = {chain.uuid: chain for chain in project.chains}
    chain_a = chains.get(link.chain_a_uuid)
    chain_b = chains.get(link.chain_b_uuid)
    if chain_a is None or chain_b is None:
        raise ownership.HD2PBError("链组连接引用的物理骨链已经不存在")
    if len(chain_a.nodes) <= 1 or len(chain_b.nodes) <= 1:
        raise ownership.HD2PBError("链组连接的两个骨链组都必须包含至少一个模拟节点")
    return chain_a, chain_a.nodes[1], chain_b, chain_b.nodes[1]


def _link_collision_endpoints(project, link, collision):
    chain_a, _top_a, chain_b, _top_b = _chain_link_endpoints(project, link)
    index = int(collision.local_joint_index)
    if index < 1 or index >= len(chain_a.nodes) or index >= len(chain_b.nodes):
        raise ownership.HD2PBError("链间碰撞体的 local Joint 序号超出模拟节点范围")
    return chain_a, chain_a.nodes[index], chain_b, chain_b.nodes[index]


def _two_point_hook_curve(owner, node_a, node_b):
    """构造允许重合端点的两点 Curve；重合时 GN 会稳定退化为球。"""

    return preview_geometry.CurvePayload(
        points=(
            _rest_head(owner, node_a.bone_name),
            _rest_head(owner, node_b.bone_name),
        ),
        cyclic=False,
    )


def _chain_link_spec(owner, project, link):
    _chain_a, node_a, _chain_b, node_b = _chain_link_endpoints(project, link)
    return PreviewSpec(
        "CHAIN",
        curve=_two_point_hook_curve(owner, node_a, node_b),
        inputs=(("Radius", max(float(project.preview_curve_radius) * 1.35, 0.0001)),),
        hook_bones=(node_a.bone_name, node_b.bone_name),
    )


def _chain_link_display_label(link):
    """隐藏自动序号噪声，同时保留用户主动填写的连接名称。"""

    label = _safe_label(link.display_name, "链组连接")
    base, separator, suffix = label.rpartition(".")
    if separator and base == "链组连接" and suffix.isdigit():
        return base
    return label


def chain_link_anchor_label(project, link):
    """返回主 Link 实际吸附的两个首模拟节点骨名。"""

    _chain_a, node_a, _chain_b, node_b = _chain_link_endpoints(project, link)
    return (
        f"{_safe_label(node_a.bone_name, '节点A')} ↔ "
        f"{_safe_label(node_b.bone_name, '节点B')}"
    )


def chain_link_label(project, link):
    return f"{_chain_link_display_label(link)} · {chain_link_anchor_label(project, link)}"


def chain_link_collection_label(project, link):
    return f"连接 · {chain_link_label(project, link)}"


def _link_collision_spec(owner, project, link, collision):
    _chain_a, node_a, _chain_b, node_b = _link_collision_endpoints(
        project, link, collision
    )
    return PreviewSpec(
        "LINK_COLLISION",
        curve=_two_point_hook_curve(owner, node_a, node_b),
        inputs=(("Radius", float(collision.radius)),),
        hook_bones=(node_a.bone_name, node_b.bone_name),
    )


def link_collision_label(project, link, collision):
    _chain_a, node_a, _chain_b, node_b = _link_collision_endpoints(
        project, link, collision
    )
    return (
        f"链间碰撞 Joint {int(collision.local_joint_index)} · "
        f"{_safe_label(node_a.bone_name, '节点A')} ↔ {_safe_label(node_b.bone_name, '节点B')}"
    )


def _limit_spec(owner, chain, node_index, preview_height=None):
    node = chain.nodes[node_index]
    if node_index == 0 or node.limit_kind == "NONE":
        return None
    previous_node = chain.nodes[node_index - 1]
    previous_bone = owner.data.bones.get(previous_node.bone_name)
    current_bone = owner.data.bones.get(node.bone_name)
    if previous_bone is None or current_bone is None:
        raise ownership.HD2PBError("角限预览引用的骨骼已经不存在")
    # 视觉轴在 Rest Pose 中冻结为当前 Node 的局部方向；随后由当前 Node 的
    # Bone Parent 完整驱动动画姿态。若用每帧 Pose 反算局部轴，当前骨自身
    # 的旋转会被逆变换抵消，正是“圆锥不跟骨骼旋转”的根因。
    delta = (
        Vector(current_bone.matrix_local.translation)
        - Vector(previous_bone.matrix_local.translation)
    )
    node_basis = current_bone.matrix_local.to_3x3()
    length = float(delta.length)
    if not math.isfinite(length) or length <= 1.0e-6:
        raise ownership.HD2PBError("角限预览所在骨段长度接近零")
    parent_rest_local = previous_bone.matrix_local.to_3x3().inverted_safe() @ delta
    try:
        reference_parent_local = Vector(
            core_reference.resolve_reference_forward(
                getattr(node, "reference_mode", "ANIMATED_SEGMENT"),
                animated_segment=parent_rest_local,
                parent_rest=parent_rest_local,
                explicit_local=getattr(node, "reference_direction", (0.0, 1.0, 0.0)),
                explicit_frame=getattr(node, "reference_frame", (0.0, 0.0, 0.0, 1.0)),
            )
        )
    except core_reference.ReferenceError as exc:
        raise ownership.HD2PBError(f"角限参考方向无效：{exc}") from exc
    desired_owner = previous_bone.matrix_local.to_3x3() @ reference_parent_local
    desired_local = node_basis.inverted_safe() @ desired_owner
    local_length = float(desired_local.length)
    if not math.isfinite(local_length) or local_length <= 1.0e-6:
        raise ownership.HD2PBError("角限预览无法转换到当前节点骨骼局部空间")
    if node.limit_kind == "CONE" and preview_height is not None:
        # 只有立体 Cone 使用项目级显示高度；Hinge/Spherical 的扇区长度
        # 继续由当前骨段比例决定，避免“圆锥高度”误改其他角限形状。
        try:
            display_length = max(float(preview_height), 0.02)
        except (TypeError, ValueError, OverflowError):
            raise ownership.HD2PBError("角限圆锥预览高度不是有限数值")
        if not math.isfinite(display_length):
            raise ownership.HD2PBError("角限圆锥预览高度不是有限数值")
    else:
        # 参考解析返回单位方向，只决定朝向，不能当作骨长；否则毫米级的
        # VRM 骨段也会画成 0.8 米。尺寸始终取真实 Rest 入段长度。
        display_length = max(length * 0.8, 0.02)
    reference_local = None
    if node.limit_kind in {"HINGE", "SPHERICAL"}:
        reference_local = Vector(tuple(node.limit_axis))
        # canonical limit_axis 位于前一骨的 Rest 局部空间；当前节点作为
        # helper 宿主时，转换到当前 Node 的 Rest 局部空间。
        reference_local = (
            current_bone.matrix_local.to_3x3().inverted_safe()
            @ (previous_bone.matrix_local.to_3x3() @ reference_local)
        )
    # Mesh Cone 的轴是局部 +Z。精确边界先在 desired 轴空间构造，再旋回 +Z，
    # 最终由 Object 的局部 rotation 对齐；当前骨骼父级负责位置和动画姿态。
    desired_unit = Vector(desired_local)
    desired_unit.normalize()
    local_rotation = Vector((0.0, 0.0, 1.0)).rotation_difference(desired_unit)
    inverse_rotation = local_rotation.inverted()
    try:
        exact = preview_geometry.make_limit(
            node.limit_kind,
            tuple(float(value) for value in desired_local),
            (
                tuple(float(value) for value in reference_local)
                if reference_local is not None
                else None
            ),
            float(node.limit_angle),
            float(node.limit_angle2),
            length=display_length,
        )
    except preview_geometry.GeometryError as exc:
        raise ownership.HD2PBError(str(exc)) from exc
    points = tuple(
        tuple(float(value) for value in (inverse_rotation @ Vector(point)))
        for point in exact.vertices
    )
    cone_radius = 0.0
    # Fixed axial-height cones diverge near 90 degrees and cannot represent
    # obtuse limits. When width would exceed four display heights, use the
    # existing finite, exact angular boundary fan instead of clamping the angle
    # and generating a kilometre-wide GN cone. Physics angles are unchanged.
    solid_cone = node.limit_kind == "CONE" and float(node.limit_angle) < math.atan(4.0)
    if solid_cone:
        cone_angle = float(node.limit_angle)
        cone_radius = max(0.0, math.tan(cone_angle) * display_length)
    return PreviewSpec(
        "LIMIT",
        curve=preview_geometry.CurvePayload(points=points, cyclic=False),
        faces=exact.faces,
        inputs=(
            ("Solid", solid_cone),
            ("Cone Radius", cone_radius),
            ("Cone Depth", display_length),
        ),
        # Curve 宿主挂在可选角限轴 Empty 下；轴 Empty 承担骨骼绑定和默认
        # Rest 姿态旋转，用户旋转该 Empty 时只改变视口角限范围。
        axis_rotation=tuple(float(value) for value in local_rotation.to_euler("XYZ")),
    )


def _collider_spec(project, collider, owner=None):
    if collider.kind == "SPHERE":
        return PreviewSpec(
            "COLLIDER_SPHERE",
            inputs=(("Center", tuple(collider.a)), ("Radius", float(collider.radius))),
        )
    if collider.kind == "CAPSULE":
        if getattr(collider, "bone_name_b", "") and owner is not None:
            first_bone = owner.data.bones.get(collider.bone_name)
            if first_bone is not None:
                # 双骨胶囊 canonical 的 A/B 位于首端骨局部空间；双端
                # controller 使用 owner object parent，因此这里转换到
                # armature-object/rest 空间，再交给各自 Bone Hook。
                rest_matrix = first_bone.matrix_local
                points = (
                    tuple(float(value) for value in (rest_matrix @ Vector(collider.a))),
                    tuple(float(value) for value in (rest_matrix @ Vector(collider.b))),
                )
            else:
                points = (
                    tuple(float(value) for value in collider.a),
                    tuple(float(value) for value in collider.b),
                )
        else:
            points = (
                tuple(float(value) for value in collider.a),
                tuple(float(value) for value in collider.b),
            )
        hooks = ()
        if getattr(collider, "bone_name_b", "") and collider.bone_name:
            hooks = (str(collider.bone_name), str(collider.bone_name_b))
        return PreviewSpec(
            "COLLIDER_CAPSULE",
            curve=preview_geometry.CurvePayload(points=points, cyclic=False),
            inputs=(
                ("Radius", float(collider.radius)),
            ),
            hook_bones=hooks,
        )
    if collider.kind == "PLANE":
        normal = Vector(tuple(collider.b))
        if normal.length <= 1.0e-6:
            raise ownership.HD2PBError("平面碰撞体法线不能为零")
        normal.normalize()
        if collider.inside:
            normal.negate()
        rotation = Vector((0.0, 0.0, 1.0)).rotation_difference(normal).to_euler("XYZ")
        return PreviewSpec(
            "COLLIDER_PLANE",
            inputs=(
                ("Point", tuple(collider.a)),
                ("Rotation", tuple(float(value) for value in rotation)),
                ("Size", float(project.preview_plane_size)),
            ),
        )
    raise ownership.HD2PBError(f"未知碰撞体类型：{collider.kind}")


def _collider_endpoint_objects(owner, project, collider_uuid):
    """返回某个胶囊的两个仅用于显示端点骨名的 Empty。"""

    root = getattr(project, "controller_collection", None)
    if root is None or not _id_exists(root, bpy.data.collections):
        return []
    return [
        obj
        for collection in _iter_collection_tree(root)
        for obj in collection.objects
        if obj.get(COLLIDER_ENDPOINT_KEY) == collider_uuid
    ]


def _style_collider_endpoint(obj, project):
    obj.hide_render = True
    obj.hide_select = True
    obj.empty_display_type = "PLAIN_AXES"
    obj.empty_display_size = 0.025
    obj.show_in_front = preview_layers.xray(project, properties.PREVIEW_COLLIDER_ENDPOINT)
    obj.hide_viewport = not bool(project.preview_enabled and project.preview_show_colliders)
    obj.show_name = bool(project.preview_show_collider_names)


def _set_collider_endpoint(obj, owner, project, bone_name, label):
    bone = owner.data.bones.get(bone_name) if bone_name else None
    if bone is None:
        return
    # 端点名称会直接绘制在视图中。多个胶囊共用同一个起点或终点骨时，
    # 使用“· 002 / · 003”消歧，内部 UUID 只保存在对象属性中。
    set_display_name(obj, label, readable_fallback=True)
    obj.parent = owner
    obj.parent_type = "BONE"
    obj.parent_bone = bone.name
    obj.location = (0.0, -bone.length, 0.0)
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)
    obj.lock_location = (True, True, True)
    obj.lock_rotation = (True, True, True)
    obj.lock_scale = (True, True, True)
    _style_collider_endpoint(obj, project)


def _stage_collider_endpoints(collection, owner, project, collider, stage):
    if collider.kind != "CAPSULE" or not collider.bone_name:
        return ()
    names = (
        ("起点", collider.bone_name),
        ("终点", getattr(collider, "bone_name_b", "") or collider.bone_name),
    )
    result = []
    for role, bone_name in names:
        obj = bpy.data.objects.new(f"{role} · {bone_name}", None)
        collection.objects.link(obj)
        _set_preview_identity(
            obj,
            owner,
            project,
            properties.ENTITY_COLLIDER,
            collider.uuid,
            properties.PREVIEW_COLLIDER_ENDPOINT,
            "",
        )
        obj[COLLIDER_ENDPOINT_KEY] = collider.uuid
        obj["hd2pb_project_uuid"] = project.project_uuid
        obj["hd2pb_owner_object"] = owner
        _set_collider_endpoint(obj, owner, project, bone_name, f"{role} · {bone_name}")
        stage["objects"].append(obj)
        result.append(obj)
    return tuple(result)


def _sync_collider_endpoints(collection, owner, project, collider):
    """原位同步胶囊端点 Empty，避免属性拖动时重建整个集合。"""

    existing = [
        obj for obj in collection.objects
        if obj.get(COLLIDER_ENDPOINT_KEY) == collider.uuid
    ]
    if collider.kind != "CAPSULE" or not collider.bone_name:
        for obj in existing:
            bpy.data.objects.remove(obj, do_unlink=True)
        return
    wanted = (
        ("起点", collider.bone_name),
        ("终点", getattr(collider, "bone_name_b", "") or collider.bone_name),
    )
    while len(existing) < 2:
        obj = bpy.data.objects.new("胶囊端点", None)
        collection.objects.link(obj)
        _set_preview_identity(
            obj,
            owner,
            project,
            properties.ENTITY_COLLIDER,
            collider.uuid,
            properties.PREVIEW_COLLIDER_ENDPOINT,
            "",
        )
        obj[COLLIDER_ENDPOINT_KEY] = collider.uuid
        obj["hd2pb_project_uuid"] = project.project_uuid
        obj["hd2pb_owner_object"] = owner
        existing.append(obj)
    for obj, (role, bone_name) in zip(existing[:2], wanted):
        _set_collider_endpoint(obj, owner, project, bone_name, f"{role} · {bone_name}")
    for obj in existing[2:]:
        bpy.data.objects.remove(obj, do_unlink=True)


def stage_chain_previews(
    collection,
    chain_controller,
    node_controllers,
    owner,
    project,
    chain,
    stage,
):
    """为一条 canonical Chain 创建曲线、Node radius 和精确角限。"""

    simulated = list(chain.nodes)[1:]
    if len(node_controllers) != len(simulated):
        raise ownership.HD2PBError("模拟节点与可选择 Node controller 数量不一致")

    _new_preview_object(
        collection,
        chain_controller,
        owner,
        project,
        properties.ENTITY_CHAIN,
        chain.uuid,
        properties.PREVIEW_CHAIN_CURVE,
        f"预览 · 链 · {_safe_label(chain.display_name, '未命名')}",
        _chain_spec(owner, project, chain),
        stage,
        chain_uuid=chain.uuid,
    )
    for index, (node, controller) in enumerate(
        zip(simulated, node_controllers),
        start=1,
    ):
        controller.name = _safe_label(node.bone_name, "缺失骨骼")
        controller.empty_display_type = "SPHERE"
        controller.empty_display_size = node_display_radius(node.radius)
        preview_layers.style_node_proxy(controller, project)
        controller.show_name = bool(
            project.preview_show_names and project.preview_show_node_names
        )
        radius_spec = PreviewSpec(
            "NODE_RADIUS",
            inputs=(
                ("Center", (0.0, 0.0, 0.0)),
                ("Radius", node_display_radius(node.radius)),
            ),
        )
        _new_preview_object(
            collection,
            controller,
            owner,
            project,
            properties.ENTITY_NODE,
            node.uuid,
            properties.PREVIEW_NODE_RADIUS,
            f"{_safe_label(node.bone_name, '缺失骨骼')} · 半径实体",
            radius_spec,
            stage,
            chain_uuid=chain.uuid,
        )
        limit_spec = _limit_spec(owner, chain, index, project.preview_limit_cone_height)
        if limit_spec is not None:
            axis = _new_limit_axis_object(
                collection,
                owner,
                project,
                node,
                limit_spec,
                stage,
                chain_uuid=chain.uuid,
            )
            _new_preview_object(
                collection,
                axis,
                owner,
                project,
                properties.ENTITY_NODE,
                node.uuid,
                properties.PREVIEW_NODE_LIMIT,
                f"{_safe_label(node.bone_name, '缺失骨骼')} · 角限实体",
                limit_spec,
                stage,
                chain_uuid=chain.uuid,
            )


def stage_collider_preview(collection, controller, owner, project, collider, stage):
    preview_obj = _new_preview_object(
        collection,
        controller,
        owner,
        project,
        properties.ENTITY_COLLIDER,
        collider.uuid,
        properties.PREVIEW_COLLIDER_SHAPE,
        collider_display_label(collider, "预览 · "),
        _collider_spec(project, collider, owner),
        stage,
    )
    _stage_collider_endpoints(collection, owner, project, collider, stage)
    return preview_obj


def stage_chain_link_preview(collection, controller, owner, project, link, stage):
    """创建可选择的主 ChainLink Curve，并用两个 Bone Hook 跟随端点姿态。"""

    return _new_preview_object(
        collection,
        controller,
        owner,
        project,
        properties.ENTITY_CHAIN_LINK,
        link.uuid,
        properties.PREVIEW_CHAIN_LINK,
        f"预览 · {chain_link_label(project, link)}",
        _chain_link_spec(owner, project, link),
        stage,
        chain_uuid=link.uuid,
    )


def stage_link_collision_preview(
    collection,
    controller,
    owner,
    project,
    link,
    collision,
    stage,
):
    """创建可选择的 LinkCollision 胶囊 Curve，并随两个模拟骨端点更新。"""

    return _new_preview_object(
        collection,
        controller,
        owner,
        project,
        properties.ENTITY_LINK_COLLISION,
        collision.uuid,
        properties.PREVIEW_LINK_COLLISION,
        f"预览 · {link_collision_label(project, link, collision)}",
        _link_collision_spec(owner, project, link, collision),
        stage,
        chain_uuid=link.uuid,
    )


def discard_stage(stage):
    """逆序清理尚未交换到项目的临时预览资源。"""

    errors = []
    for obj in reversed(tuple(stage.get("objects", ()))):
        if _id_exists(obj, bpy.data.objects):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception as exc:
                errors.append(f"预览对象 {obj.name}: {exc}")
    for data in reversed(tuple(stage.get("data", ()))):
        if data.users:
            continue
        try:
            if isinstance(data, bpy.types.Curve) and _id_exists(data, bpy.data.curves):
                bpy.data.curves.remove(data)
        except Exception as exc:
            errors.append(f"预览几何 {data.name}: {exc}")
    for material in reversed(tuple(stage.get("materials", ()))):
        if not material.users and _id_exists(material, bpy.data.materials):
            try:
                bpy.data.materials.remove(material)
            except Exception as exc:
                errors.append(f"预览材质 {material.name}: {exc}")
    if errors:
        raise RuntimeError("临时物理骨预览清理失败：" + "; ".join(errors))


def _validate_limit_axis_for_update(index, obj):
    """校验可选角限轴 Empty；它没有 Curve/GN/材质数据块。"""

    owner = index["owner"]
    project = index["project"]
    if not is_exact_preview_object(obj, owner, project.project_uuid):
        raise ownership.HD2PBError(
            f"角限旋转轴“{obj.name}”的 owner、项目、类型或 UUID 标签损坏；请修复编辑投影"
        )
    if obj.type != "EMPTY" or obj.data is not None:
        raise ownership.HD2PBError(f"角限旋转轴“{obj.name}”必须是无数据 Empty")
    if getattr(obj, "parent_type", "") != "BONE":
        raise ownership.HD2PBError(f"角限旋转轴“{obj.name}”必须使用 Bone Parent")
    if tuple(obj.lock_location) != (True, True, True):
        raise ownership.HD2PBError(f"角限旋转轴“{obj.name}”的位置必须锁定")
    if tuple(obj.lock_scale) != (True, True, True):
        raise ownership.HD2PBError(f"角限旋转轴“{obj.name}”的缩放必须锁定")
    if tuple(obj.lock_rotation) != (False, False, False):
        raise ownership.HD2PBError(f"角限旋转轴“{obj.name}”只能通过旋转调整")
    return None


def _validate_preview_for_update(index, obj, *, allow_material_variant_mismatch=False):
    owner = index["owner"]
    project = index["project"]
    if not is_exact_preview_object(obj, owner, project.project_uuid):
        raise ownership.HD2PBError(
            f"预览对象“{obj.name}”的 owner、项目、类型或 UUID 标签损坏；请修复编辑投影"
        )
    if obj.hd2pb_preview_kind == properties.PREVIEW_NODE_LIMIT_AXIS:
        return _validate_limit_axis_for_update(index, obj)
    if obj.type != "CURVE" or obj.data is None:
        raise ownership.HD2PBError(
            f"预览对象“{obj.name}”必须使用 Curve 数据；请修复编辑投影"
        )
    if not _is_exact_preview_data(obj.data, obj, owner, project.project_uuid):
        raise ownership.HD2PBError(
            f"预览对象“{obj.name}”的几何所有权或实体标签损坏；请修复编辑投影"
        )
    if obj.data.users != 1:
        raise ownership.HD2PBError(
            f"预览几何“{obj.data.name}”还被其他对象使用；为保护用户数据已拒绝刷新"
        )
    node_modifiers = preview_nodes.modifiers_for(obj)
    if len(node_modifiers) != 1:
        raise ownership.HD2PBError(
            f"预览对象“{obj.name}”必须有且只有一个 Geometry Nodes 修改器；请修复编辑投影"
        )
    node_group = node_modifiers[0].node_group
    expected_gn_kind = _expected_gn_kind(project, obj)
    if not (
        preview_nodes.is_owned_node_group(node_group, expected_gn_kind)
        or preview_nodes.is_migratable_node_group(node_group, expected_gn_kind)
    ):
        raise ownership.HD2PBError(
            f"预览对象“{obj.name}”的 Geometry Nodes 资源身份损坏；请修复编辑投影"
        )
    if len(obj.data.materials) != 1:
        raise ownership.HD2PBError(
            f"预览对象“{obj.name}”没有唯一分类材质；请修复编辑投影"
        )
    material = obj.data.materials[0]
    material_variant = (
        str(material.get(DATA_MATERIAL_VARIANT_KEY, "")) if material is not None else ""
    )
    if material is None or not _is_exact_preview_material(
        material,
        owner,
        project.project_uuid,
        obj.hd2pb_preview_kind,
        material_variant,
    ):
        raise ownership.HD2PBError(
            f"预览对象“{obj.name}”挂载了非本项目精确拥有的材质；为保护用户材质已拒绝刷新"
        )
    expected_variant = _expected_material_variant(
        project,
        obj.hd2pb_entity_type,
        obj.hd2pb_entity_uuid,
        obj.hd2pb_preview_kind,
    )
    if not allow_material_variant_mismatch and material_variant != expected_variant:
        raise ownership.HD2PBError(
            f"预览对象“{obj.name}”的骨链状态材质未同步；请执行样式刷新或修复实体预览"
        )
    local_users = index["material_local_users"].get(material.as_pointer(), 0)
    if material.users != local_users:
        raise ownership.HD2PBError(
            f"预览材质“{material.name}”还被项目外数据使用；为保护用户数据已拒绝刷新"
        )
    return material


def validate_selectable_preview(index, obj, entity_type, kind):
    """确认对象是项目根集合内该实体唯一且完整的正式可选预览。"""

    key = (entity_type, getattr(obj, "hd2pb_entity_uuid", ""), kind)
    matches = index["previews"].get(key, ())
    if len(matches) != 1 or matches[0] != obj:
        raise ownership.HD2PBError(
            "当前选择不是项目集合内该实体唯一的正式预览；请执行“修复编辑投影”"
        )
    if (
        getattr(obj, "hd2pb_entity_type", properties.ENTITY_NONE) != entity_type
        or getattr(obj, "hd2pb_preview_kind", properties.PREVIEW_NONE) != kind
    ):
        raise ownership.HD2PBError("当前选择的实体类型或预览类型不符合此操作要求")
    _validate_preview_for_update(index, obj)
    _single_controller(index, entity_type, obj.hd2pb_entity_uuid)
    return obj


def apply_project_style(owner, index=None):
    index = index or build_project_index(owner)
    project = index["project"]
    validated = []
    for matches in index["previews"].values():
        for obj in matches:
            if obj.hd2pb_preview_kind == properties.PREVIEW_COLLIDER_ENDPOINT:
                _style_object(obj, project)
                continue
            material = _validate_preview_for_update(
                index,
                obj,
                allow_material_variant_mismatch=True,
            )
            if obj.hd2pb_preview_kind == properties.PREVIEW_NODE_LIMIT_AXIS:
                _style_object(obj, project)
                continue
            validated.append((obj, material))
    required_materials = {}
    for obj, _material in validated:
        kind = obj.hd2pb_preview_kind
        variant = _expected_material_variant(
            project,
            obj.hd2pb_entity_type,
            obj.hd2pb_entity_uuid,
            kind,
        )
        key = (kind, variant)
        if key not in required_materials:
            required_materials[key] = _ensure_index_material(index, kind, variant)
    seen_materials = set()
    for obj, _old_material in validated:
        kind = obj.hd2pb_preview_kind
        variant = _expected_material_variant(
            project,
            obj.hd2pb_entity_type,
            obj.hd2pb_entity_uuid,
            kind,
        )
        material = required_materials[(kind, variant)]
        _assign_preview_material(obj, material)
        _style_object(obj, project)
        if kind in {
            properties.PREVIEW_CHAIN_CURVE,
            properties.PREVIEW_CHAIN_LINK,
        }:
            node_modifier = preview_nodes.modifiers_for(obj)[0]
            radius = float(project.preview_curve_radius)
            preview_nodes.set_modifier_inputs(
                node_modifier,
                {"Radius": radius},
            )
        if material.as_pointer() in seen_materials:
            continue
        seen_materials.add(material.as_pointer())
        _configure_material(material, _kind_color(project, kind, variant))
    for materials in tuple(index["materials"].values()):
        for material in tuple(set(materials)):
            if material.users == 0 and _id_exists(material, bpy.data.materials):
                bpy.data.materials.remove(material)
    for collider in project.colliders:
        for endpoint in _collider_endpoint_objects(owner, project, collider.uuid):
            desired = str(endpoint.get(DISPLAY_NAME_KEY, "")).strip()
            if desired:
                set_display_name(endpoint, desired, readable_fallback=True)
            _style_collider_endpoint(endpoint, project)
    for controllers in index["controllers"].values():
        for controller in controllers:
            entity_type = getattr(controller, "hd2pb_entity_type", properties.ENTITY_NONE)
            controller.show_name = bool(
                entity_type == properties.ENTITY_NODE
                and project.preview_show_node_names
            )
            controller.hide_viewport = False
            if entity_type == properties.ENTITY_NODE:
                preview_layers.style_node_proxy(controller, project)
            if entity_type != properties.ENTITY_NODE:
                controller.empty_display_size = 0.0001
    for chain in project.chains:
        for node in list(chain.nodes)[1:]:
            matches = index["controllers"].get((properties.ENTITY_NODE, node.uuid), ())
            if len(matches) == 1:
                set_display_name(
                    matches[0],
                    _safe_label(node.bone_name, "缺失骨骼"),
                )
                matches[0].empty_display_type = "SPHERE"
                matches[0].empty_display_size = node_display_radius(node.radius)
                matches[0].show_name = bool(
                project.preview_show_node_names
                )


def _single_controller(index, entity_type, entity_uuid):
    matches = index["controllers"].get((entity_type, entity_uuid), ())
    if len(matches) != 1:
        raise ownership.HD2PBError(
            f"实体存在 {len(matches)} 个 controller；请执行“修复编辑投影”"
        )
    controller = matches[0]
    if (
        controller.type != "EMPTY"
        or controller.hd2pb_controller_schema != properties.CONTROLLER_SCHEMA_VERSION
        or controller.hd2pb_owner_object != index["owner"]
        or controller.hd2pb_project_uuid != index["project"].project_uuid
        or not ownership.is_valid_uuid(controller.hd2pb_controller_uuid)
    ):
        raise ownership.HD2PBError("controller 的类型或所有权标签损坏；请修复编辑投影")
    return controller


def _single_preview(index, entity_type, entity_uuid, kind):
    matches = index["previews"].get((entity_type, entity_uuid, kind), ())
    if len(matches) != 1:
        raise ownership.HD2PBError(
            f"实体的{_KIND_LABELS.get(kind, '预览')}存在 {len(matches)} 个；请执行“修复编辑投影”"
        )
    return matches[0]


def _material_from_index(index, kind, variant=""):
    unique = []
    pointers = set()
    for material in index["materials"].get((kind, variant), ()):
        if not _is_exact_preview_material(
            material,
            index["owner"],
            index["project"].project_uuid,
            kind,
            variant,
        ):
            raise ownership.HD2PBError(
                f"{_KIND_LABELS[kind]}挂载了非本项目精确拥有的材质；请修复编辑投影"
            )
        if material.users != index["material_local_users"].get(material.as_pointer(), 0):
            raise ownership.HD2PBError(
                f"预览材质“{material.name}”还被项目外数据使用；为保护用户数据已拒绝刷新"
            )
        if material.as_pointer() not in pointers:
            pointers.add(material.as_pointer())
            unique.append(material)
    if len(unique) > 1:
        label = "普通"
        raise ownership.HD2PBError(
            f"{_KIND_LABELS[kind]}（{label}）存在重复生成材质；请修复编辑投影"
        )
    return unique[0] if unique else None


def _ensure_index_material(index, kind, variant=""):
    material = _material_from_index(index, kind, variant)
    if material is not None:
        return material
    stage = new_stage()
    material = _ensure_stage_material(
        stage,
        index["owner"],
        index["project"],
        kind,
        variant,
    )
    index["materials"][(kind, variant)].append(material)
    index["material_local_users"][material.as_pointer()] = 0
    return material


def _assign_preview_material(obj, material):
    """原位切换 Curve 槽位与 GN Material socket，不触碰其他投影身份。"""

    current = obj.data.materials[0]
    if current == material:
        return False
    modifier = preview_nodes.modifiers_for(obj)[0]
    try:
        obj.data.materials[0] = material
        preview_nodes.set_modifier_inputs(modifier, {"Material": material})
    except Exception:
        obj.data.materials[0] = current
        preview_nodes.set_modifier_inputs(modifier, {"Material": current})
        raise
    return True


def _update_spec(
    obj,
    spec,
    logical_parent,
    owner,
    project,
    chain_uuid,
    material,
    *,
    sync_hooks=False,
):
    """原位更新 Curve 点与 GN socket，不交换 Object/Data 身份。"""

    obj[DATA_CHAIN_KEY] = chain_uuid
    if obj.hd2pb_preview_kind == properties.PREVIEW_COLLIDER_SHAPE:
        resolved = ownership.find_entity(project, properties.ENTITY_COLLIDER, obj.hd2pb_entity_uuid)
        if resolved is not None:
            obj[COLLIDER_KIND_KEY] = str(resolved[0].kind)
    _apply_spec(obj, spec, owner, project, material, sync_hooks=sync_hooks)
    _attach_preview_object(obj, logical_parent, owner, spec)


def _remove_preview_object(obj, owner, project):
    endpoint_uuid = obj.get(COLLIDER_ENDPOINT_KEY) if obj is not None else None
    if (
        not endpoint_uuid
        and obj is not None
        and getattr(obj, "hd2pb_preview_kind", "") == properties.PREVIEW_COLLIDER_SHAPE
    ):
        endpoint_uuid = getattr(obj, "hd2pb_entity_uuid", "")
    if endpoint_uuid:
        for endpoint in _collider_endpoint_objects(owner, project, endpoint_uuid):
            if endpoint != obj and _id_exists(endpoint, bpy.data.objects):
                bpy.data.objects.remove(endpoint, do_unlink=True)
    data = obj.data
    materials = tuple(data.materials) if data is not None else ()
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is not None and data.users == 0:
        if isinstance(data, bpy.types.Curve) and _id_exists(data, bpy.data.curves):
            bpy.data.curves.remove(data)
    for material in materials:
        if (
            material is not None
            and material.users == 0
            and is_owned_preview_datablock(material, owner, project.project_uuid)
            and _id_exists(material, bpy.data.materials)
        ):
            bpy.data.materials.remove(material)


def _rename_preview(index, key, name):
    matches = list(index["previews"].get(key, ()))
    if len(matches) > 1:
        raise ownership.HD2PBError(
            f"{_KIND_LABELS[key[2]]}存在重复对象；请修复编辑投影"
        )
    if matches:
        set_display_name(
            matches[0],
            name,
        )


def _sync_spec(index, entity_type, entity_uuid, kind, controller, name, spec, chain_uuid=""):
    owner = index["owner"]
    project = index["project"]
    key = (entity_type, entity_uuid, kind)
    matches = list(index["previews"].get(key, ()))
    if len(matches) > 1:
        raise ownership.HD2PBError(f"{_KIND_LABELS[kind]}存在重复对象；请修复编辑投影")
    if spec is None:
        if matches:
            obj = matches[0]
            material = _validate_preview_for_update(index, obj)
            material_variant = str(material.get(DATA_MATERIAL_VARIANT_KEY, ""))
            material_pointer = material.as_pointer()
            removed_refs = material_reference_count((obj,), material)
            _remove_preview_object(obj, owner, project)
            index["previews"].pop(key, None)
            index["material_local_users"][material_pointer] = max(
                0,
                index["material_local_users"].get(material_pointer, removed_refs)
                - removed_refs,
            )
            material_key = (kind, material_variant)
            index["materials"][material_key] = [
                current
                for current in index["materials"].get(material_key, ())
                if _id_exists(current, bpy.data.materials)
            ]
        return
    variant = _expected_material_variant(project, entity_type, entity_uuid, kind)
    material = _material_from_index(index, kind, variant)
    if not matches:
        stage = new_stage()
        if material is not None:
            stage["material_by_kind"][(kind, variant)] = material
        try:
            obj = _new_preview_object(
                controller.users_collection[0],
                controller,
                owner,
                project,
                entity_type,
                entity_uuid,
                kind,
                name,
                spec,
                stage,
                chain_uuid=chain_uuid,
            )
            if entity_type == properties.ENTITY_COLLIDER:
                _stage_collider_endpoints(
                    controller.users_collection[0], owner, project, ownership.find_entity(
                        project, entity_type, entity_uuid
                    )[0], stage,
                )
        except Exception:
            discard_stage(stage)
            raise
        index["previews"][key] = [obj]
        used_material = obj.data.materials[0]
        index["material_local_users"][used_material.as_pointer()] += material_reference_count(
            (obj,),
            used_material,
        )
        if material is None:
            index["materials"][(kind, variant)].append(
                stage["material_by_kind"][(kind, variant)]
            )
        return
    obj = matches[0]
    _validate_preview_for_update(index, obj, allow_material_variant_mismatch=True)
    material = material or _ensure_index_material(index, kind, variant)
    _assign_preview_material(obj, material)
    if list(obj.users_collection) != list(controller.users_collection):
        raise ownership.HD2PBError(f"{_KIND_LABELS[kind]}集合损坏；请修复编辑投影")
    set_display_name(
        obj,
        name,
    )
    _update_spec(
        obj,
        spec,
        controller,
        owner,
        project,
        chain_uuid,
        material,
        sync_hooks=(
            kind in {
                properties.PREVIEW_CHAIN_CURVE,
                properties.PREVIEW_CHAIN_LINK,
                properties.PREVIEW_LINK_COLLISION,
            }
            or (
                kind == properties.PREVIEW_COLLIDER_SHAPE
                and spec.gn_kind == "COLLIDER_CAPSULE"
                and bool(spec.hook_bones)
            )
        ),
    )
    if entity_type == properties.ENTITY_COLLIDER:
        resolved = ownership.find_entity(project, entity_type, entity_uuid)
        if resolved is not None:
            _sync_collider_endpoints(
                controller.users_collection[0], owner, project, resolved[0]
            )


def _fast_update_collider_preview(index, collider, controller, name, aspects):
    """原位更新高频 Collider 数值，不重建 Hook、modifier 或节点组。"""

    key = (
        properties.ENTITY_COLLIDER,
        collider.uuid,
        properties.PREVIEW_COLLIDER_SHAPE,
    )
    matches = list(index["previews"].get(key, ()))
    if len(matches) != 1:
        raise ownership.HD2PBError(
            f"碰撞体预览存在 {len(matches)} 个对象；请修复编辑投影"
        )
    obj = matches[0]
    _validate_preview_for_update(index, obj)
    set_display_name(obj, name)
    spec = _collider_spec(index["project"], collider, index["owner"])
    modifier = preview_nodes.modifiers_for(obj)[0]
    if ASPECT_COLLIDER_POINTS in aspects:
        _set_curve_points(obj.data, spec.curve, spec.faces)
        preview_nodes.set_modifier_inputs(modifier, spec.modifier_inputs())
    elif ASPECT_RADIUS in aspects and collider.kind in {"SPHERE", "CAPSULE"}:
        preview_nodes.set_modifier_inputs(modifier, {"Radius": float(collider.radius)})
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = spec.rotation
    obj.scale = (1.0, 1.0, 1.0)
    _attach_preview_object(obj, controller, index["owner"], spec)
    _style_object(obj, index["project"])


def refresh_entity(owner, entity_type, entity_uuid, index=None, aspects=None):
    """只刷新一个 canonical 实体对应的几何与标签。"""

    index = index or build_project_index(owner)
    project = index["project"]
    aspects = frozenset(aspects or (ASPECT_ALL,))
    result = ownership.find_entity(project, entity_type, entity_uuid)
    if result is None and entity_type != properties.ENTITY_PROJECT:
        return
    if entity_type == properties.ENTITY_PROJECT:
        if ASPECT_ALL in aspects:
            for profile in project.profiles:
                refresh_entity(
                    owner,
                    properties.ENTITY_PROFILE,
                    profile.uuid,
                    index,
                    (ASPECT_ALL,),
                )
            for chain in project.chains:
                refresh_entity(
                    owner,
                    properties.ENTITY_CHAIN,
                    chain.uuid,
                    index,
                    (ASPECT_ALL,),
                )
                for node in list(chain.nodes)[1:]:
                    refresh_entity(
                        owner,
                        properties.ENTITY_NODE,
                        node.uuid,
                        index,
                        (ASPECT_ALL,),
                    )
            for collider in project.colliders:
                refresh_entity(
                    owner,
                    properties.ENTITY_COLLIDER,
                    collider.uuid,
                    index,
                    (ASPECT_ALL,),
                )
            for link in project.chain_links:
                refresh_entity(
                    owner,
                    properties.ENTITY_CHAIN_LINK,
                    link.uuid,
                    index,
                    (ASPECT_ALL,),
                )
                for collision in link.collisions:
                    refresh_entity(
                        owner,
                        properties.ENTITY_LINK_COLLISION,
                        collision.uuid,
                        index,
                        (ASPECT_ALL,),
                    )
        elif ASPECT_PLANE_GEOMETRY in aspects:
            for collider in project.colliders:
                if collider.kind == "PLANE":
                    refresh_entity(
                        owner,
                        properties.ENTITY_COLLIDER,
                        collider.uuid,
                        index,
                        (ASPECT_GEOMETRY,),
                    )
        elif ASPECT_LIMIT_PREVIEW in aspects:
            for chain in project.chains:
                for node in list(chain.nodes)[1:]:
                    if node.limit_kind != "NONE":
                        refresh_entity(
                            owner,
                            properties.ENTITY_NODE,
                            node.uuid,
                            index,
                            (ASPECT_LIMIT,),
                        )
        if ASPECT_ALL in aspects or ASPECT_STYLE in aspects:
            apply_project_style(owner, index)
        return
    entity, parent = result
    if entity_type == properties.ENTITY_PROFILE:
        controller = _single_controller(index, entity_type, entity_uuid)
        set_display_name(
            controller,
            f"参数 · {_safe_label(entity.display_name, '未命名')}",
        )
        return
    if entity_type == properties.ENTITY_CHAIN:
        controller = _single_controller(index, entity_type, entity_uuid)
        set_display_name(
            controller,
            f"链 · {_safe_label(entity.display_name, '未命名')}",
        )
        preview_name = f"预览 · 链 · {_safe_label(entity.display_name, '未命名')}"
        key = (entity_type, entity_uuid, properties.PREVIEW_CHAIN_CURVE)
        if ASPECT_ALL in aspects or ASPECT_GEOMETRY in aspects:
            _sync_spec(
                index,
                entity_type,
                entity_uuid,
                properties.PREVIEW_CHAIN_CURVE,
                controller,
                preview_name,
                _chain_spec(owner, project, entity),
                entity.uuid,
            )
        else:
            _rename_preview(index, key, preview_name)
        return
    if entity_type == properties.ENTITY_NODE:
        chain = parent
        node_index = next(index for index, node in enumerate(chain.nodes) if node.uuid == entity_uuid)
        if node_index == 0:
            # nodes[0] 是没有任何 3D/Outliner 投影的内部固定锚点。
            return
        controller = _single_controller(index, entity_type, entity_uuid)
        set_display_name(
            controller,
            _safe_label(entity.bone_name, "缺失骨骼"),
        )
        controller.empty_display_type = "SPHERE"
        controller.empty_display_size = node_display_radius(entity.radius)
        preview_layers.style_node_proxy(controller, project)
        controller.show_name = bool(
            project.preview_show_node_names
        )
        if ASPECT_ALL in aspects or ASPECT_GEOMETRY in aspects or ASPECT_RADIUS in aspects:
            radius_spec = PreviewSpec(
                "NODE_RADIUS",
                inputs=(
                    ("Center", (0.0, 0.0, 0.0)),
                    ("Radius", node_display_radius(entity.radius)),
                ),
            )
            _sync_spec(
                index,
                entity_type,
                entity_uuid,
                properties.PREVIEW_NODE_RADIUS,
                controller,
                f"{_safe_label(entity.bone_name, '缺失骨骼')} · 半径实体",
                radius_spec,
                chain.uuid,
            )
        if (
            ASPECT_ALL in aspects
            or ASPECT_GEOMETRY in aspects
            or ASPECT_LIMIT in aspects
            or ASPECT_REFERENCE in aspects
        ):
            limit_spec = _limit_spec(
                owner,
                chain,
                node_index,
                project.preview_limit_cone_height,
            )
            axis_key = (
                properties.ENTITY_NODE,
                entity_uuid,
                properties.PREVIEW_NODE_LIMIT_AXIS,
            )
            axis_matches = list(index["previews"].get(axis_key, ()))
            if limit_spec is None:
                if len(axis_matches) > 1:
                    raise ownership.HD2PBError(
                        f"节点“{entity.bone_name}”存在重复角限旋转轴；请修复编辑投影"
                    )
                if axis_matches:
                    axis = axis_matches[0]
                    _validate_limit_axis_for_update(index, axis)
                    _remove_preview_object(axis, owner, project)
                    index["previews"].pop(axis_key, None)
            else:
                if len(axis_matches) > 1:
                    raise ownership.HD2PBError(
                        f"节点“{entity.bone_name}”存在重复角限旋转轴；请修复编辑投影"
                    )
                if axis_matches:
                    axis = _sync_limit_axis_object(
                        axis_matches[0], owner, project, entity, limit_spec
                    )
                else:
                    stage = new_stage()
                    try:
                        axis = _new_limit_axis_object(
                            controller.users_collection[0],
                            owner,
                            project,
                            entity,
                            limit_spec,
                            stage,
                            chain_uuid=chain.uuid,
                        )
                    except Exception:
                        discard_stage(stage)
                        raise
                    index["previews"][axis_key] = [axis]
            _sync_spec(
                index,
                entity_type,
                entity_uuid,
                properties.PREVIEW_NODE_LIMIT,
                axis if limit_spec is not None else controller,
                f"{_safe_label(entity.bone_name, '缺失骨骼')} · 角限实体",
                limit_spec,
                chain.uuid,
            )
        return
    if entity_type == properties.ENTITY_COLLIDER:
        controller = _single_controller(index, entity_type, entity_uuid)
        set_display_name(
            controller,
            collider_display_label(entity, ""),
        )
        preview_name = collider_display_label(entity, "预览 · ")
        key = (entity_type, entity_uuid, properties.PREVIEW_COLLIDER_SHAPE)
        if ASPECT_ALL in aspects or ASPECT_GEOMETRY in aspects:
            _sync_spec(
                index,
                entity_type,
                entity_uuid,
                properties.PREVIEW_COLLIDER_SHAPE,
                controller,
                preview_name,
                _collider_spec(project, entity, owner),
            )
        elif ASPECT_COLLIDER_POINTS in aspects or ASPECT_RADIUS in aspects:
            _fast_update_collider_preview(
                index,
                entity,
                controller,
                preview_name,
                aspects,
            )
        else:
            _rename_preview(index, key, preview_name)
        return
    if entity_type == properties.ENTITY_CHAIN_LINK:
        controller = _single_controller(index, entity_type, entity_uuid)
        label = chain_link_label(project, entity)
        set_display_name(controller, label)
        preview_label = f"预览 · {label}"
        if ASPECT_ALL in aspects or ASPECT_GEOMETRY in aspects:
            _sync_spec(
                index,
                entity_type,
                entity_uuid,
                properties.PREVIEW_CHAIN_LINK,
                controller,
                preview_label,
                _chain_link_spec(owner, project, entity),
                entity.uuid,
            )
        else:
            _rename_preview(
                index,
                (entity_type, entity_uuid, properties.PREVIEW_CHAIN_LINK),
                preview_label,
            )
        return
    if entity_type == properties.ENTITY_LINK_COLLISION:
        link = parent
        controller = _single_controller(index, entity_type, entity_uuid)
        label = link_collision_label(project, link, entity)
        set_display_name(controller, label)
        preview_label = f"预览 · {label}"
        if ASPECT_ALL in aspects or ASPECT_GEOMETRY in aspects:
            _sync_spec(
                index,
                entity_type,
                entity_uuid,
                properties.PREVIEW_LINK_COLLISION,
                controller,
                preview_label,
                _link_collision_spec(owner, project, link, entity),
                link.uuid,
            )
        else:
            _rename_preview(
                index,
                (entity_type, entity_uuid, properties.PREVIEW_LINK_COLLISION),
                preview_label,
            )
        return
def _hook_vertex_indices(modifier):
    """安全读取 Hook 的点索引；RNA 损坏时返回 ``None``。"""

    try:
        values = tuple(modifier.vertex_indices)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None
    try:
        return tuple(int(value) for value in values)
    except (TypeError, ValueError, OverflowError):
        return None


def _curve_point_count(obj):
    """统计 Curve 中所有 spline 的控制点数量，供 Hook 索引范围检查。"""

    data = getattr(obj, "data", None)
    if data is None:
        return None
    try:
        return sum(len(spline.points) for spline in data.splines)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None


def _chain_hook_issues(obj, owner, chain=None, *, expected_bones=None):
    """检查每个 Curve 点的 Hook 绑定、索引和完整跟随参数。"""

    issues = []
    if expected_bones is None:
        try:
            _payload, expected_bones = _chain_curve_payload(owner, chain)
        except ownership.HD2PBError as exc:
            return [str(exc)]
    expected_bones = tuple(expected_bones)
    managed = [
        (position, modifier)
        for position, modifier in enumerate(obj.modifiers)
        if modifier.type == "HOOK" and modifier.name.startswith("HD2PB 骨钩 · ")
    ]
    if len(managed) != len(expected_bones):
        return [
            f"骨链曲线“{obj.name}”应有 {len(expected_bones)} 个模拟骨 Hook，当前为 {len(managed)} 个"
        ]
    nodes_positions = [
        position
        for position, modifier in enumerate(obj.modifiers)
        if modifier.type == "NODES"
    ]
    nodes_position = nodes_positions[0] if len(nodes_positions) == 1 else len(obj.modifiers)
    point_count = _curve_point_count(obj)
    if point_count is None:
        issues.append(f"骨链曲线“{obj.name}”无法读取 Curve 控制点，Hook 索引无法验证")
    elif point_count != len(expected_bones):
        issues.append(
            f"骨链曲线“{obj.name}”应有 {len(expected_bones)} 个模拟骨控制点，当前为 {point_count} 个"
        )
    for index, ((position, modifier), bone_name) in enumerate(
        zip(managed, expected_bones)
    ):
        if position >= nodes_position:
            issues.append(f"骨链曲线“{obj.name}”的 Hook 必须排在 Geometry Nodes 之前")
        try:
            bound_object = modifier.object
            bound_bone = modifier.subtarget
        except (AttributeError, ReferenceError, RuntimeError):
            bound_object = None
            bound_bone = ""
        bone = owner.data.bones.get(bone_name)
        if bound_object != owner or bound_bone != bone_name:
            issues.append(
                f"骨链曲线“{obj.name}”第 {index + 1} 个 Hook 没有绑定骨骼“{bone_name}”"
            )
        elif bone is None:
            issues.append(
                f"骨链曲线“{obj.name}”第 {index + 1} 个 Hook 引用的骨骼“{bone_name}”不存在"
            )
        else:
            try:
                actual_inverse = modifier.matrix_inverse
                expected_inverse = bone.matrix_local.inverted()
                matrix_values = tuple(
                    float(actual_inverse[row][column])
                    for row in range(4)
                    for column in range(4)
                )
                inverse_matches = all(
                    math.isfinite(value) for value in matrix_values
                ) and all(
                    abs(actual_inverse[row][column] - expected_inverse[row][column])
                    <= 1.0e-5
                    for row in range(4)
                    for column in range(4)
                )
            except (
                AttributeError,
                ReferenceError,
                RuntimeError,
                TypeError,
                ValueError,
                OverflowError,
            ):
                inverse_matches = False
            if not inverse_matches:
                issues.append(
                    f"骨链曲线“{obj.name}”第 {index + 1} 个 Hook 不是静置姿态绑定；"
                    "请执行“修复实体预览”"
                )

        vertex_indices = _hook_vertex_indices(modifier)
        if vertex_indices is None:
            issues.append(
                f"骨链曲线“{obj.name}”第 {index + 1} 个 Hook 的 vertex index 无法读取"
            )
        elif len(vertex_indices) != 1 or vertex_indices[0] != index:
            issues.append(
                f"骨链曲线“{obj.name}”第 {index + 1} 个 Hook 的 vertex index 应为 {index}，"
                f"当前为 {vertex_indices}"
            )
        elif point_count is not None and not 0 <= vertex_indices[0] < point_count:
            issues.append(
                f"骨链曲线“{obj.name}”第 {index + 1} 个 Hook 的 vertex index 超出 Curve 控制点范围"
            )

        try:
            strength = float(modifier.strength)
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError, OverflowError):
            strength = None
        if strength is None or not math.isfinite(strength) or abs(strength - 1.0) > 1.0e-6:
            issues.append(
                f"骨链曲线“{obj.name}”第 {index + 1} 个 Hook 的 strength 必须为 1.0"
            )
        if not bool(getattr(modifier, "show_viewport", True)):
            issues.append(f"骨链曲线“{obj.name}”第 {index + 1} 个 Hook 未启用视口求值")
        if str(getattr(modifier, "vertex_group", "")):
            issues.append(
                f"骨链曲线“{obj.name}”第 {index + 1} 个 Hook 不应使用 vertex group"
            )
        if bool(getattr(modifier, "invert_vertex_group", False)):
            issues.append(
                f"骨链曲线“{obj.name}”第 {index + 1} 个 Hook 不应反转 vertex group"
            )
        if bool(getattr(modifier, "use_apply_on_spline", False)):
            issues.append(
                f"骨链曲线“{obj.name}”第 {index + 1} 个 Hook 不应启用 apply on spline"
            )
    return issues


def preview_issues(owner):
    """独立诊断 derived preview；返回问题但不改变导出字节。"""

    try:
        index = build_project_index(owner)
    except ownership.HD2PBError as exc:
        return [str(exc)]
    project = index["project"]
    chains_by_uuid = {chain.uuid: chain for chain in project.chains}
    links_by_uuid = {link.uuid: link for link in project.chain_links}
    collisions_by_uuid = {
        collision.uuid: (link, collision)
        for link in project.chain_links
        for collision in link.collisions
    }
    expected = {}
    for chain in project.chains:
        expected[
            (properties.ENTITY_CHAIN, chain.uuid, properties.PREVIEW_CHAIN_CURVE)
        ] = ((properties.ENTITY_CHAIN, chain.uuid), chain.uuid, "")
        for node_index, node in enumerate(list(chain.nodes)[1:], start=1):
            expected[
                (properties.ENTITY_NODE, node.uuid, properties.PREVIEW_NODE_RADIUS)
            ] = ((properties.ENTITY_NODE, node.uuid), chain.uuid, "")
            if node.limit_kind != "NONE":
                expected[
                    (properties.ENTITY_NODE, node.uuid, properties.PREVIEW_NODE_LIMIT_AXIS)
                ] = ((properties.ENTITY_NODE, node.uuid), chain.uuid, node.bone_name)
                expected[
                    (properties.ENTITY_NODE, node.uuid, properties.PREVIEW_NODE_LIMIT)
                ] = (
                    (properties.ENTITY_NODE, node.uuid),
                    chain.uuid,
                    node.bone_name,
                )
    for collider in project.colliders:
        expected[
            (properties.ENTITY_COLLIDER, collider.uuid, properties.PREVIEW_COLLIDER_SHAPE)
        ] = ((properties.ENTITY_COLLIDER, collider.uuid), "", "")
    for link in project.chain_links:
        expected[
            (properties.ENTITY_CHAIN_LINK, link.uuid, properties.PREVIEW_CHAIN_LINK)
        ] = ((properties.ENTITY_CHAIN_LINK, link.uuid), link.uuid, "")
        for collision in link.collisions:
            expected[
                (
                    properties.ENTITY_LINK_COLLISION,
                    collision.uuid,
                    properties.PREVIEW_LINK_COLLISION,
                )
            ] = (
                (properties.ENTITY_LINK_COLLISION, collision.uuid),
                link.uuid,
                "",
            )
    issues = []
    object_uuids = set()
    data_uuids = set()
    exact_objects = []
    for key in sorted(expected):
        matches = index["previews"].get(key, ())
        if len(matches) != 1:
            issues.append(
                f"{_KIND_LABELS[key[2]]}（{key[1]}）应有 1 个，当前为 {len(matches)} 个"
            )
    for key, matches in index["previews"].items():
        if key not in expected:
            if key[2] == properties.PREVIEW_COLLIDER_ENDPOINT:
                collider_result = ownership.find_entity(
                    project, properties.ENTITY_COLLIDER, key[1]
                )
                if collider_result is not None and collider_result[0].kind == "CAPSULE":
                    if len(matches) != 2:
                        issues.append(
                            f"胶囊碰撞体“{key[1]}”应有 2 个端点名称对象，当前为 {len(matches)} 个"
                        )
                    for endpoint in matches:
                        # Name visibility is cosmetic (and style updates may be
                        # queued). Only the endpoint's structure is required.
                        if endpoint.type != "EMPTY":
                            issues.append(f"胶囊碰撞体“{key[1]}”端点名称对象必须是 Empty")
                    continue
            issues.append(f"存在不再对应 canonical 的{_KIND_LABELS.get(key[2], '未知预览')}")
        for obj in matches:
            exact_objects.append(obj)
            if not is_exact_preview_object(obj, owner, project.project_uuid):
                issues.append(f"预览对象“{obj.name}”的 owner、版本或 UUID 标签损坏")
            elif obj.hd2pb_preview_uuid in object_uuids:
                issues.append(f"预览对象“{obj.name}”的 preview UUID 与本项目另一对象重复")
            else:
                object_uuids.add(obj.hd2pb_preview_uuid)
            selectable = key[2] in {
                properties.PREVIEW_CHAIN_CURVE,
                properties.PREVIEW_NODE_LIMIT_AXIS,
                properties.PREVIEW_COLLIDER_SHAPE,
                properties.PREVIEW_CHAIN_LINK,
                properties.PREVIEW_LINK_COLLISION,
            }
            if obj.hide_select == selectable or not obj.hide_render:
                issues.append(f"预览对象“{obj.name}”的可选择或渲染合同损坏")
            unlocked_collider = bool(
                key[2] == properties.PREVIEW_COLLIDER_SHAPE
                and project.preview_allow_collider_transform
            )
            expected_lock = (not unlocked_collider,) * 3
            expected_rotation_lock = (
                (False,) * 3
                if key[2] == properties.PREVIEW_NODE_LIMIT_AXIS
                else expected_lock
            )
            expected_location_lock = (
                (True,) * 3
                if key[2] == properties.PREVIEW_NODE_LIMIT_AXIS
                else expected_lock
            )
            expected_scale_lock = (
                (True,) * 3
                if key[2] == properties.PREVIEW_NODE_LIMIT_AXIS
                else expected_lock
            )
            if (
                tuple(obj.lock_location) != expected_location_lock
                or tuple(obj.lock_rotation) != expected_rotation_lock
                or tuple(obj.lock_scale) != expected_scale_lock
            ):
                issues.append(f"预览对象“{obj.name}”的分类型变换锁合同损坏")
            if key[2] == properties.PREVIEW_COLLIDER_ENDPOINT:
                if obj.type != "EMPTY":
                    issues.append(f"碰撞体端点名称对象“{obj.name}”必须是 Empty")
                continue
            if key in expected:
                controller_key, expected_chain_uuid, parent_bone = expected[key]
                controllers = index["controllers"].get(controller_key, ())
                if len(controllers) != 1:
                    issues.append(f"预览对象“{obj.name}”缺少唯一逻辑 controller")
                else:
                    if list(obj.users_collection) != list(controllers[0].users_collection):
                        issues.append(f"预览对象“{obj.name}”没有与 controller 位于同一唯一集合")
                    if key[2] == properties.PREVIEW_NODE_LIMIT:
                        axis_matches = index["previews"].get(
                            (
                                properties.ENTITY_NODE,
                                key[1],
                                properties.PREVIEW_NODE_LIMIT_AXIS,
                            ),
                            (),
                        )
                        if len(axis_matches) != 1 or obj.parent != axis_matches[0]:
                            issues.append(f"预览对象“{obj.name}”没有挂到角限旋转轴 Empty")
                    elif parent_bone:
                        bone = owner.data.bones.get(parent_bone)
                        expected_location = (
                            (0.0, -bone.length, 0.0)
                            if bone is not None
                            else None
                        )
                        if (
                            bone is None
                            or obj.parent != owner
                            or obj.parent_type != "BONE"
                            or obj.parent_bone != parent_bone
                            or any(
                                abs(current - wanted) > 1.0e-6
                                for current, wanted in zip(
                                    obj.location,
                                    expected_location or (0.0, 0.0, 0.0),
                                )
                            )
                        ):
                            issues.append(
                                f"预览对象“{obj.name}”没有把锥顶吸附到当前节点骨骼“{parent_bone}”"
                            )
                    elif obj.parent != controllers[0] or obj.parent_type != "OBJECT":
                        issues.append(f"预览对象“{obj.name}”没有挂到预期 controller")
                if obj.get(DATA_CHAIN_KEY, "") != expected_chain_uuid:
                    issues.append(f"预览对象“{obj.name}”的骨链归属标签损坏")
            if key[2] == properties.PREVIEW_NODE_LIMIT_AXIS:
                if obj.type != "EMPTY" or obj.data is not None:
                    issues.append(f"角限旋转轴“{obj.name}”必须是无数据 Empty")
                if obj.empty_display_type != "ARROWS":
                    issues.append(f"角限旋转轴“{obj.name}”必须使用箭头显示")
                continue
            if obj.type != "CURVE":
                issues.append(f"预览对象“{obj.name}”的数据类型必须为 CURVE")
            if not is_owned_preview_datablock(obj.data, owner, project.project_uuid):
                issues.append(f"预览对象“{obj.name}”的 Curve 所有权标签损坏")
            elif (
                obj.data.get(DATA_ENTITY_TYPE_KEY) != key[0]
                or obj.data.get(DATA_ENTITY_KEY) != key[1]
                or obj.data.get(DATA_KIND_KEY) != key[2]
                or obj.data.get(DATA_CHAIN_KEY, "") != obj.get(DATA_CHAIN_KEY, "")
            ):
                issues.append(f"预览对象“{obj.name}”的 Curve 实体标签不一致")
            else:
                data_uuid = obj.data.get(DATA_UUID_KEY, "")
                if data_uuid in data_uuids:
                    issues.append(f"预览对象“{obj.name}”的几何 UUID 与本项目另一数据块重复")
                data_uuids.add(data_uuid)
            if key[2] == properties.PREVIEW_CHAIN_CURVE:
                chain = chains_by_uuid.get(key[1])
                if chain is not None:
                    issues.extend(_chain_hook_issues(obj, owner, chain))
            elif key[2] == properties.PREVIEW_CHAIN_LINK:
                link = links_by_uuid.get(key[1])
                if link is not None:
                    _chain_a, node_a, _chain_b, node_b = _chain_link_endpoints(
                        project, link
                    )
                    issues.extend(
                        _chain_hook_issues(
                            obj,
                            owner,
                            expected_bones=(node_a.bone_name, node_b.bone_name),
                        )
                    )
            elif key[2] == properties.PREVIEW_LINK_COLLISION:
                resolved = collisions_by_uuid.get(key[1])
                if resolved is not None:
                    link, collision = resolved
                    _chain_a, node_a, _chain_b, node_b = _link_collision_endpoints(
                        project, link, collision
                    )
                    issues.extend(
                        _chain_hook_issues(
                            obj,
                            owner,
                            expected_bones=(node_a.bone_name, node_b.bone_name),
                        )
                    )
            elif (
                key[2] == properties.PREVIEW_COLLIDER_SHAPE
                and key[0] == properties.ENTITY_COLLIDER
            ):
                collider_result = ownership.find_entity(
                    project, properties.ENTITY_COLLIDER, key[1]
                )
                if collider_result is not None:
                    collider = collider_result[0]
                    if collider.kind == "CAPSULE" and getattr(collider, "bone_name_b", ""):
                        issues.extend(
                            _chain_hook_issues(
                                obj,
                                owner,
                                expected_bones=(collider.bone_name, collider.bone_name_b),
                            )
                        )
            node_modifiers = preview_nodes.modifiers_for(obj)
            if len(node_modifiers) != 1:
                issues.append(f"预览对象“{obj.name}”必须有一个 Geometry Nodes 修改器")
            else:
                try:
                    expected_gn_kind = _expected_gn_kind(project, obj)
                except ownership.HD2PBError as exc:
                    issues.append(str(exc))
                else:
                    if not preview_nodes.is_owned_node_group(
                        node_modifiers[0].node_group,
                        expected_gn_kind,
                    ):
                        issues.append(
                            f"预览对象“{obj.name}”的 Geometry Nodes 所有权、版本或类型标签损坏"
                        )
            if obj.data is not None and len(obj.data.materials) != 1:
                issues.append(f"预览对象“{obj.name}”必须使用一个项目分类材质")
            elif obj.data is not None:
                material = obj.data.materials[0]
                expected_variant = _expected_material_variant(
                    project,
                    key[0],
                    key[1],
                    key[2],
                )
                if (
                    material is None
                    or not is_owned_preview_datablock(material, owner, project.project_uuid)
                    or material.get(DATA_KIND_KEY) != key[2]
                    or material.get(DATA_MATERIAL_VARIANT_KEY, "") != expected_variant
                ):
                    issues.append(f"预览对象“{obj.name}”的分类材质所有权或类型损坏")

    tagged_data = []
    for data in bpy.data.curves:
        owner_match = data.get(DATA_OWNER_KEY) == owner
        project_match = data.get(DATA_PROJECT_KEY) == project.project_uuid
        if owner_match != project_match:
            issues.append(f"预览几何“{data.name}”只有部分项目所有权标签匹配")
        elif owner_match and project_match:
            tagged_data.append(data)
    for data in bpy.data.meshes:
        if (
            data.get(DATA_OWNER_KEY) == owner
            or data.get(DATA_PROJECT_KEY) == project.project_uuid
        ):
            issues.append(f"存在旧版 Mesh 预览数据“{data.name}”；请修复实体预览")
    object_data = {obj.data for obj in exact_objects if obj.data is not None}
    for data in tagged_data:
        if data not in object_data:
            issues.append(f"存在未被预览对象使用的生成几何“{data.name}”")
        if data.users != sum(1 for obj in exact_objects if obj.data == data):
            issues.append(f"预览几何“{data.name}”还被项目外对象使用")

    tagged_materials = []
    for material in bpy.data.materials:
        owner_match = material.get(DATA_OWNER_KEY) == owner
        project_match = material.get(DATA_PROJECT_KEY) == project.project_uuid
        if owner_match != project_match:
            issues.append(f"预览材质“{material.name}”只有部分项目所有权标签匹配")
        elif owner_match and project_match:
            tagged_materials.append(material)
    expected_material_keys = {
        (
            key[2],
            _expected_material_variant(project, key[0], key[1], key[2]),
        )
        for key in expected
        if key[2] != properties.PREVIEW_NODE_LIMIT_AXIS
    }
    for kind, variant in expected_material_keys:
        matches = [
            material
            for material in tagged_materials
            if material.get(DATA_KIND_KEY) == kind
            and material.get(DATA_MATERIAL_VARIANT_KEY, "") == variant
        ]
        if len(matches) != 1:
            label = "普通"
            issues.append(
                f"{_KIND_LABELS[kind]}（{label}）分类材质应有 1 个，当前为 {len(matches)} 个"
            )
    for material in tagged_materials:
        owned_users = material_reference_count(
            exact_objects,
            material,
            datablocks=tagged_data,
        )
        material_key = (
            material.get(DATA_KIND_KEY),
            material.get(DATA_MATERIAL_VARIANT_KEY, ""),
        )
        if material_key not in expected_material_keys:
            issues.append(f"存在不再需要的生成材质“{material.name}”")
        if material.users != owned_users:
            issues.append(f"预览材质“{material.name}”还被项目外数据使用")
    return issues


def register_owner(owner):
    if _live_owner(owner) and owner.data.hd2pb_project.controller_collection is not None:
        _KNOWN_OWNERS[owner.as_pointer()] = owner


def _projection_hidden(owner):
    root = owner.data.hd2pb_project.controller_collection
    return root is not None and root.hide_viewport


def _limit_rest_signature(owner):
    """Limit geometry is Rest-local; pose/world transforms are bone-parented.

    Preview writes may also tag the owner in the depsgraph. Only a changed Rest
    binding requires regenerating the cones, not every such notification.
    """
    names = set()
    for chain in owner.data.hd2pb_project.chains:
        for index, node in enumerate(chain.nodes):
            if index > 0 and node.limit_kind != "NONE":
                names.update((chain.nodes[index - 1].bone_name, node.bone_name))
    bones = owner.data.bones
    return tuple(
        (name, tuple(value for row in bones[name].matrix_local for value in row)
         if name in bones else None)
        for name in sorted(names)
    )


def _resume_visible_updates():
    # Hidden projects keep only a cheap owner marker; defer all bone-matrix
    # scans until reveal. A data-only project has nothing to refresh at all.
    for pointer, owner in tuple(_REST_PENDING.items()):
        if not _live_owner(owner) or owner.data.hd2pb_project.controller_collection is None:
            _REST_PENDING.pop(pointer, None)
            continue
        if _projection_hidden(owner):
            continue
        _REST_PENDING.pop(pointer, None)
        signature = _limit_rest_signature(owner)
        if signature != _REST_SIGNATURES.get(pointer):
            _REST_SIGNATURES[pointer] = signature
            _queue(owner, properties.ENTITY_PROJECT, owner.data.hd2pb_project.project_uuid, ASPECT_ALL)
    for key, owner in tuple(_DEFERRED.items()):
        if not _live_owner(owner) or owner.data.hd2pb_project.project_uuid != key[1]:
            _DEFERRED.pop(key, None)
        elif not _projection_hidden(owner):
            _DIRTY[key] = owner
            _DEFERRED.pop(key, None)


def _queue(owner, entity_type, entity_uuid, aspect=ASPECT_GEOMETRY):
    global _DEADLINE
    if not _REGISTERED or _DATA_CHANGING or not _live_owner(owner):
        return
    project = owner.data.hd2pb_project
    if project.schema_version != properties.PROJECT_SCHEMA_VERSION:
        return
    if project.controller_collection is None:
        return
    register_owner(owner)
    key = (owner.as_pointer(), project.project_uuid, entity_type, entity_uuid, aspect)
    queue_was_empty = not _DIRTY
    hidden = _projection_hidden(owner)
    (_DEFERRED if hidden else _DIRTY)[key] = owner
    # 固定到本帧首次修改后的刷新点；连续拖动不能一直延后 deadline。
    if queue_was_empty:
        _DEADLINE = time.monotonic() + VISUAL_REFRESH_DELAY
    if _SUSPEND_DEPTH == 0 and not bpy.app.timers.is_registered(_timer_callback):
        bpy.app.timers.register(_timer_callback, first_interval=HIDDEN_REFRESH_DELAY if hidden else VISUAL_REFRESH_DELAY)


def mark_rna_dirty(item, entity_type, aspect=ASPECT_GEOMETRY):
    """Property update 入口：只解析 id_data/UUID 并登记，不创建 Blender 数据。"""

    armature = getattr(item, "id_data", None)
    if armature is None or not hasattr(armature, "hd2pb_project"):
        return
    project = armature.hd2pb_project
    owner = project.authoring_object
    entity_uuid = project.project_uuid if entity_type == properties.ENTITY_PROJECT else getattr(item, "uuid", "")
    if owner is not None and entity_uuid:
        _queue(owner, entity_type, entity_uuid, aspect)


@contextmanager
def suspend_updates():
    """合并批量写入/回滚产生的 RNA callback，退出后至多保留一个 timer。"""

    global _SUSPEND_DEPTH
    _SUSPEND_DEPTH += 1
    try:
        yield
    finally:
        _SUSPEND_DEPTH -= 1
        if (
            _SUSPEND_DEPTH == 0
            and _REGISTERED
            and (_DIRTY or _DEFERRED)
            and not bpy.app.timers.is_registered(_timer_callback)
        ):
            bpy.app.timers.register(_timer_callback, first_interval=VISUAL_REFRESH_DELAY)


def _tag_redraw():
    window_manager = getattr(bpy.context, "window_manager", None)
    for window in tuple(window_manager.windows) if window_manager else ():
        for area in window.screen.areas:
            if area.type in {"VIEW_3D", "PROPERTIES", "OUTLINER"}:
                area.tag_redraw()


def _flush_entries(entries):
    global _FLUSHING
    grouped = defaultdict(list)
    for key, owner in entries.items():
        if not _live_owner(owner):
            continue
        grouped[owner.as_pointer()].append((key, owner))
    _FLUSHING = True
    refreshed = False
    try:
        for rows in grouped.values():
            owner = rows[0][1]
            if not _live_owner(owner):
                continue
            project = owner.data.hd2pb_project
            if not ownership.project_initialized(project) or project.controller_collection is None:
                continue
            if _projection_hidden(owner):
                _DEFERRED.update(dict(rows))
                continue
            try:
                refreshed = True
                index = build_project_index(owner)
                requests = defaultdict(set)
                for key, _owner in rows:
                    if key[1] != project.project_uuid:
                        continue
                    requests[(key[2], key[3])].add(key[4])
                project_aspects = requests.pop(
                    (properties.ENTITY_PROJECT, project.project_uuid),
                    set(),
                )
                if project_aspects:
                    refresh_entity(
                        owner,
                        properties.ENTITY_PROJECT,
                        project.project_uuid,
                        index,
                        project_aspects,
                    )
                if ASPECT_ALL not in project_aspects:
                    for (entity_type, entity_uuid), aspects in sorted(requests.items()):
                        refresh_entity(
                            owner,
                            entity_type,
                            entity_uuid,
                            index,
                            aspects,
                        )
                project.preview_error_message = ""
                _REST_SIGNATURES[owner.as_pointer()] = _limit_rest_signature(owner)
            except Exception as exc:
                message = f"实体预览自动刷新失败：{exc}；请检查生成物归属后点击“修复实体预览”"
                project.preview_error_message = message
                print(f"HD2 物理骨实时预览刷新失败：{type(exc).__name__}: {exc}")
    finally:
        _FLUSHING = False
    if refreshed:
        _tag_redraw()


def _timer_callback():
    if not _REGISTERED or _DATA_CHANGING:
        return None
    _resume_visible_updates()
    remaining = _DEADLINE - time.monotonic()
    if remaining > 0.0:
        return max(remaining, 0.001)
    entries = dict(_DIRTY)
    _DIRTY.clear()
    if entries:
        _flush_entries(entries)
    return VISUAL_REFRESH_DELAY if _DIRTY else HIDDEN_REFRESH_DELAY if (_DEFERRED or _REST_PENDING) else None


def _register_scan_callback():
    """等 Blender 解除注册期 ``_RestrictData`` 后恢复已打开文件的 owner 索引。"""

    if not _REGISTERED:
        return None
    try:
        _rebuild_known_owners(queue_style=True)
    except AttributeError as exc:
        if type(bpy.data).__name__ == "_RestrictData":
            return 0.01
        print(f"HD2 物理骨启用后恢复 owner 索引失败：{type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"HD2 物理骨启用后恢复 owner 索引失败：{type(exc).__name__}: {exc}")
    return None


def _schedule_register_scan():
    if not bpy.app.timers.is_registered(_register_scan_callback):
        bpy.app.timers.register(_register_scan_callback, first_interval=0.0)


def _cancel_register_scan():
    if bpy.app.timers.is_registered(_register_scan_callback):
        bpy.app.timers.unregister(_register_scan_callback)


def flush_now():
    """测试/显式工具入口：立即刷新当前 dirty 集，不等待定时窗口。"""

    if bpy.app.timers.is_registered(_timer_callback):
        bpy.app.timers.unregister(_timer_callback)
    _resume_visible_updates()
    entries = dict(_DIRTY)
    _DIRTY.clear()
    if entries:
        _flush_entries(entries)
    if _REGISTERED and (_DEFERRED or _REST_PENDING) and not bpy.app.timers.is_registered(_timer_callback):
        bpy.app.timers.register(_timer_callback, first_interval=HIDDEN_REFRESH_DELAY)
    return len(entries)


def discard_owner_updates(owner, project_uuid=""):
    """完整投影已按最新 canonical 重建后，丢弃该项目的冗余 dirty 请求。"""

    owner_pointer = owner.as_pointer()
    _REST_PENDING.pop(owner_pointer, None)
    for queue in (_DIRTY, _DEFERRED):
        for key in tuple(queue):
            if key[0] != owner_pointer:
                continue
            if project_uuid and key[1] != project_uuid:
                continue
            queue.pop(key, None)
    _REST_SIGNATURES[owner_pointer] = _limit_rest_signature(owner)
    if not _DIRTY and not _DEFERRED and not _REST_PENDING and bpy.app.timers.is_registered(_timer_callback):
        bpy.app.timers.unregister(_timer_callback)


def _reset_runtime_queue():
    global _DEADLINE
    _cancel_register_scan()
    if bpy.app.timers.is_registered(_timer_callback):
        bpy.app.timers.unregister(_timer_callback)
    _DIRTY.clear()
    _DEFERRED.clear()
    _REST_SIGNATURES.clear()
    _REST_PENDING.clear()
    _DEADLINE = 0.0


@persistent
def _load_pre(_unused):
    global _DATA_CHANGING
    _DATA_CHANGING = False
    _reset_runtime_queue()
    _KNOWN_OWNERS.clear()
    try:
        from . import controllers, operators

        controllers.restore_selection_visibility()
        operators.clear_runtime_state()
    except Exception as exc:
        print(f"HD2 物理骨运行状态清理失败：{type(exc).__name__}: {exc}")


@persistent
def _load_post(_unused):
    global _DATA_CHANGING
    _DATA_CHANGING = False
    # 这里只重建 owner 注册表并恢复 derived 显示样式；不创建、删除或迁移 canonical。
    _cancel_register_scan()
    _rebuild_known_owners(queue_style=True)


def _rebuild_known_owners(*, queue_style):
    _KNOWN_OWNERS.clear()
    for armature in bpy.data.armatures:
        if not hasattr(armature, "hd2pb_project"):
            continue
        project = armature.hd2pb_project
        if ownership.project_initialized(project) and project.authoring_object is not None:
            owner = project.authoring_object
            if not _live_owner(owner):
                continue
            if project.controller_collection is None:
                continue
            if (
                project.schema_version != properties.PROJECT_SCHEMA_VERSION
                or project.authoring_object != owner
                or len(ownership.armature_objects(armature)) != 1
                or ownership.project_uuid_conflicts(owner)
            ):
                continue
            register_owner(owner)
            # Repair only plugin-owned bone display colors on enable/load scans.
            # No new timer/handler, geometry mutation, or canonical rule migration.
            from . import pose_drivers
            pose_drivers.repair_target_colors(owner)
            _REST_SIGNATURES[owner.as_pointer()] = _limit_rest_signature(owner)
            if queue_style:
                _queue(owner, properties.ENTITY_PROJECT, project.project_uuid, ASPECT_STYLE)


@persistent
def _undo_redo_pre(_unused):
    """丢弃旧数据世代的引用；可见性由撤销快照负责，不在此写入场景。"""
    global _DATA_CHANGING
    _DATA_CHANGING = True
    _reset_runtime_queue()
    _KNOWN_OWNERS.clear()
    from . import controllers
    controllers.discard_selection_visibility()


@persistent
def _undo_redo_post(_unused):
    global _DATA_CHANGING
    _reset_runtime_queue()
    _DATA_CHANGING = False
    # Undo may recreate deleted owners: scan current bpy.data, never reuse the
    # previous Object wrappers. Keep the existing deferred full-refresh policy.
    _rebuild_known_owners(queue_style=False)
    for owner in tuple(_KNOWN_OWNERS.values()):
        _queue(owner, properties.ENTITY_PROJECT,
               owner.data.hd2pb_project.project_uuid, ASPECT_ALL)


@persistent
def _depsgraph_update_post(_scene, depsgraph):
    if _FLUSHING or not _REGISTERED or _DATA_CHANGING:
        return
    # ``depsgraph.updates`` 在交互式 Pose 编辑中通常返回 evaluated ID；其
    # ``as_pointer()`` 与 ``bpy.data.objects`` 中的原始 owner 不同。统一还原到
    # ``id.original``，并同时接受 owner Object/Armature datablock 更新；后者是
    # 部分 Blender 4.x 姿态编辑实际报告的 ID。重复登记会被 dirty key 合并。
    changed = set()
    changed_axes = []
    for update in depsgraph.updates:
        updated_id = update.id
        if updated_id is None:
            continue
        original_id = getattr(updated_id, "original", None)
        candidate = original_id or updated_id
        changed.add(candidate.as_pointer())
        if (
            getattr(candidate, "type", None) == "EMPTY"
            and getattr(candidate, "hd2pb_preview_kind", properties.PREVIEW_NONE)
            == properties.PREVIEW_NODE_LIMIT_AXIS
        ):
            changed_axes.append(candidate)
    for axis in changed_axes:
        if _axis_rotation_changed(axis):
            _capture_limit_axis_reference(axis)
    for owner in tuple(_KNOWN_OWNERS.values()):
        if not _live_owner(owner):
            continue
        if owner.data.hd2pb_project.controller_collection is None:
            _KNOWN_OWNERS.pop(owner.as_pointer(), None)
            continue
        owner_ids = {owner.as_pointer(), owner.data.as_pointer()}
        if owner_ids.intersection(changed):
            project = owner.data.hd2pb_project
            if _projection_hidden(owner):
                _REST_PENDING[owner.as_pointer()] = owner
                if _SUSPEND_DEPTH == 0 and not bpy.app.timers.is_registered(_timer_callback):
                    bpy.app.timers.register(_timer_callback, first_interval=HIDDEN_REFRESH_DELAY)
                continue
            signature = _limit_rest_signature(owner)
            pointer = owner.as_pointer()
            if _REST_SIGNATURES.get(pointer) == signature:
                continue
            _REST_SIGNATURES[pointer] = signature
            for chain in project.chains:
                for node_index, node in enumerate(chain.nodes):
                    if node_index > 0 and node.limit_kind != "NONE":
                        _queue(owner, properties.ENTITY_NODE, node.uuid, ASPECT_LIMIT)


_HANDLERS = (
    (bpy.app.handlers.load_pre, _load_pre),
    (bpy.app.handlers.load_post, _load_post),
    (bpy.app.handlers.undo_pre, _undo_redo_pre),
    (bpy.app.handlers.redo_pre, _undo_redo_pre),
    (bpy.app.handlers.undo_post, _undo_redo_post),
    (bpy.app.handlers.redo_post, _undo_redo_post),
    (bpy.app.handlers.depsgraph_update_post, _depsgraph_update_post),
)


def owned_state_present():
    return bool(
        _REGISTERED
        or _DIRTY
        or _DEFERRED
        or _REST_SIGNATURES
        or _REST_PENDING
        or _KNOWN_OWNERS
        or bpy.app.timers.is_registered(_timer_callback)
        or bpy.app.timers.is_registered(_register_scan_callback)
        or any(callback in handlers for handlers, callback in _HANDLERS)
    )


def register():
    global _REGISTERED
    if owned_state_present():
        raise RuntimeError("物理骨预览运行时仍持有 Blender handler 或 timer 状态")
    appended = []
    try:
        for handlers, callback in _HANDLERS:
            handlers.append(callback)
            appended.append((handlers, callback))
    except Exception:
        for handlers, callback in reversed(appended):
            if callback in handlers:
                handlers.remove(callback)
        raise
    _REGISTERED = True
    _schedule_register_scan()


def unregister():
    global _REGISTERED, _DATA_CHANGING
    _DATA_CHANGING = False
    errors = []
    try:
        _reset_runtime_queue()
    except Exception as exc:
        errors.append(str(exc))
    for handlers, callback in reversed(_HANDLERS):
        while callback in handlers:
            try:
                handlers.remove(callback)
            except Exception as exc:
                errors.append(str(exc))
                break
    _KNOWN_OWNERS.clear()
    _REGISTERED = any(callback in handlers for handlers, callback in _HANDLERS)
    if errors or owned_state_present():
        raise RuntimeError(
            "物理骨预览运行时注销不完整：" + (" | ".join(errors) or "仍有 handler/timer")
        )


__all__ = (
    "VISUAL_REFRESH_DELAY",
    "DATA_SCHEMA_KEY",
    "DATA_PROJECT_KEY",
    "DATA_OWNER_KEY",
    "DATA_ENTITY_TYPE_KEY",
    "DATA_ENTITY_KEY",
    "DATA_KIND_KEY",
    "DATA_UUID_KEY",
    "DATA_CHAIN_KEY",
    "DISPLAY_NAME_KEY",
    "node_display_radius",
    "set_display_name",
    "normalize_display_names",
    "chain_link_anchor_label",
    "chain_link_label",
    "chain_link_collection_label",
    "link_collision_label",
    "material_reference_count",
    "is_preview_object",
    "is_exact_preview_object",
    "is_owned_preview_datablock",
    "build_project_index",
    "validate_selectable_preview",
    "project_preview_objects",
    "new_stage",
    "stage_chain_previews",
    "stage_collider_preview",
    "discard_stage",
    "apply_project_style",
    "refresh_entity",
    "preview_issues",
    "register_owner",
    "mark_rna_dirty",
    "suspend_updates",
    "flush_now",
    "discard_owner_updates",
    "owned_state_present",
    "register",
    "unregister",
)
