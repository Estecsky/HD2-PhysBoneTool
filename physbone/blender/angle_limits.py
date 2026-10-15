"""One authoring frame: +Z forward, in the preceding node bone's local space.

Legacy reference fields are read-only fallbacks, not authoring modes. Resolving
or exporting a frame never migrates RNA; only explicit edits persist it.
"""
from mathutils import Quaternion, Vector

from ..core import reference
from . import ownership


def _quaternion(values):
    try:
        x, y, z, w = reference.normalize_quaternion(values)
    except reference.ReferenceError as exc:
        raise ownership.HD2PBError(f"角限姿态无效：{exc}") from exc
    return Quaternion((w, x, y, z))


def frame_from_direction(direction):
    try:
        direction = Vector(reference.normalize(direction))
    except reference.ReferenceError as exc:
        raise ownership.HD2PBError(f"角限方向无效：{exc}") from exc
    return Vector((0, 0, 1)).rotation_difference(direction)


def node_context(node):
    project = getattr(node.id_data, "hd2pb_project", None)
    owner = getattr(project, "authoring_object", None)
    if owner is not None:
        for chain in project.chains:
            for index, item in enumerate(chain.nodes):
                if item.uuid == node.uuid:
                    return owner, chain, index
    raise ownership.HD2PBError("角限节点没有唯一的制作骨架与骨链")


def resolve_frame(owner, chain, index):
    node = chain.nodes[index]
    if getattr(node, "limit_frame_valid", False):
        return _quaternion(node.limit_frame)
    mode = getattr(node, "reference_mode", "ANIMATED_SEGMENT")
    if mode == "EXPLICIT_FRAME":
        # Legacy wire forward is +Y; authoring/geometry forward is +Z.
        return _quaternion(node.reference_frame) @ frame_from_direction((0, 1, 0))
    if mode == "EXPLICIT_LOCAL":
        return frame_from_direction(node.reference_direction)
    if mode not in {"ANIMATED_SEGMENT", "PARENT_REST"}:
        raise ownership.HD2PBError(f"旧角限参考类型无法识别：{mode}")
    if index <= 0:
        return Quaternion((1, 0, 0, 0))
    previous = owner.data.bones.get(chain.nodes[index - 1].bone_name)
    current = owner.data.bones.get(node.bone_name)
    if previous is None or current is None:
        raise ownership.HD2PBError("角限引用的 Rest 骨骼不存在")
    delta = current.matrix_local.translation - previous.matrix_local.translation
    return frame_from_direction(previous.matrix_local.to_3x3().inverted_safe() @ delta)


def resolve_axis(node):
    axis = Vector(node.limit_axis)
    if not getattr(node, "limit_frame_valid", False) and getattr(node, "reference_mode", "") == "EXPLICIT_FRAME":
        axis = _quaternion(node.reference_frame) @ axis
    return axis


def frame_values(q):
    q = _quaternion((q.x, q.y, q.z, q.w))
    return dict(limit_frame=(q.x, q.y, q.z, q.w), limit_frame_valid=True,
                reference_mode="EXPLICIT_LOCAL", reference_direction=tuple(q @ Vector((0, 0, 1))),
                reference_frame=(0, 0, 0, 1))


def reference_fields(owner, chain, index):
    node = chain.nodes[index]
    if index == 0 or node.limit_kind == "NONE":
        return dict(reference_mode="ANIMATED_SEGMENT", reference_direction=[0, 1, 0],
                    reference_frame=[0, 0, 0, 1], limit_axis=list(node.limit_axis))
    q = resolve_frame(owner, chain, index)
    return dict(reference_mode="EXPLICIT_LOCAL", reference_direction=list(q @ Vector((0, 0, 1))),
                reference_frame=[0, 0, 0, 1], limit_axis=list(resolve_axis(node)))
