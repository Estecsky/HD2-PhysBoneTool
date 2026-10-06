"""HD2PHY1 的确定性格式与严格读取器。"""

from __future__ import annotations

import math
import os
import pathlib
import struct
import tempfile
import zlib
import warnings
from typing import Any

from . import reference


class Gate(ValueError):
    """扩展 HD2PHY1 合同的校验失败。"""


FNV_OFFSET = 14695981039346656037
FNV_PRIME = 1099511628211


def fnv1a64(data: bytes, seed: int = FNV_OFFSET) -> int:
    value = seed
    for byte in data:
        value ^= byte
        value = (value * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return value


def bone_name_hash(name: str) -> int:
    return fnv1a64(name.encode("utf-8"))


def _name64(value: object, where: str, *, allow_empty: bool = False) -> bytes:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise Gate(f"{where}: 名称必须是{'字符串' if allow_empty else '非空字符串'}")
    encoded = value.encode("utf-8")
    if len(encoded) > 63 or b"\0" in encoded:
        raise Gate(f"{where}: UTF-8 名称最多 63 字节且不能含 NUL")
    return encoded + bytes(64 - len(encoded))


def _finite(value: object, where: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise Gate(f"{where}: 必须是数字") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise Gate(f"{where}: 数值超出 {minimum}..{maximum}")
    return 0.0 if result == 0.0 else result


def _vec3(value: object, where: str, *, maximum: float = 10000.0) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise Gate(f"{where}: 必须包含三个数字")
    return tuple(_finite(component, f"{where}[{index}]", -maximum, maximum)
                 for index, component in enumerate(value))


def _basis3(value: object, where: str) -> tuple[float, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 9:
        raise Gate(f"{where}: 必须包含九个数字")
    return tuple(_finite(component, f"{where}[{index}]", -10000.0, 10000.0)
                 for index, component in enumerate(value))


def _validated_bones(document: dict, palette_slots: int) -> tuple[list[dict], dict[str, dict]]:
    raw_bones = document.get("bones")
    if not isinstance(raw_bones, list) or not 1 <= len(raw_bones) <= 256:
        raise Gate("bones: 数量必须为 1..256")
    bones: list[dict] = []
    by_name: dict[str, dict] = {}
    slots: set[int] = set()
    for index, raw in enumerate(raw_bones):
        where = f"bones[{index}]"
        if not isinstance(raw, dict):
            raise Gate(f"{where}: 必须是对象")
        name = raw.get("name")
        _name64(name, where)
        slot = raw.get("palette_slot")
        if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < palette_slots:
            raise Gate(f"{where}.palette_slot: 必须是 0..{palette_slots - 1} 的整数")
        parent_slot = raw.get("parent_slot")
        if isinstance(parent_slot, bool) or not isinstance(parent_slot, int) or not -1 <= parent_slot < palette_slots:
            raise Gate(f"{where}.parent_slot: 必须是 -1 或调色板槽位")
        rest_head = _vec3(raw.get("rest_head"), f"{where}.rest_head")
        rest_basis = _basis3(raw.get("rest_basis"), f"{where}.rest_basis")
        if name in by_name or slot in slots:
            raise Gate(f"{where}: 骨名和调色板槽位必须唯一")
        bone = {"name": name, "palette_slot": slot, "parent_slot": parent_slot,
                "rest_head": rest_head, "rest_basis": rest_basis}
        bones.append(bone)
        by_name[name] = bone
        slots.add(slot)
    for bone in bones:
        if bone["parent_slot"] >= 0 and bone["parent_slot"] not in slots:
            raise Gate(f"骨骼 {bone['name']!r}: 父骨槽位未导出")
    return bones, by_name


def skeleton_fingerprint(palette_slots: int, bones: list[dict]) -> int:
    ordered = sorted(bones, key=lambda bone: (bone["palette_slot"], bone["name"]))
    value = fnv1a64(b"HD2SKEL1\0")
    value = fnv1a64(struct.pack("<II", palette_slots, len(ordered)), value)
    for bone in ordered:
        value = fnv1a64(struct.pack("<Ii", bone["palette_slot"], bone["parent_slot"]), value)
        value = fnv1a64(bone["name"].encode("utf-8") + b"\0", value)
    return value


def parse_unit_id(value: object) -> int:
    if isinstance(value, bool):
        raise Gate("unit_id: 不接受 bool")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        text = value.strip().lower().removeprefix("0x")
        if not text:
            raise Gate("unit_id: 不能为空")
        base = 16 if len(text) == 16 or any(character in "abcdef" for character in text) else 10
        try:
            result = int(text, base)
        except ValueError as exc:
            raise Gate("unit_id: 必须是十进制或十六进制文本") from exc
    else:
        raise Gate("unit_id: 必须是整数或十六进制字符串")
    if not 1 <= result <= 0xFFFFFFFFFFFFFFFF:
        raise Gate("unit_id: 超出 uint64 范围")
    return result


def write_atomic(path, blob: bytes) -> None:
    import os
    import pathlib
    import tempfile

    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


MAGIC = b"HD2PHY1\0"
VERSION = 1

# header: magic, version, header/file sizes, crc, unit/fingerprint, seven counts,
# fixed dt, capabilities, and one reserve.  The frozen 80-byte struct has only
# one reserved uint after capabilities despite the prose contract saying [2].
HEADER = struct.Struct("<8s4I2Q7If2I")
CHAIN = struct.Struct("<64sIIff3f3f4I")
JOINT = struct.Struct("<64sQIfIff3fff3fIBBH3f4fI")
COLLIDER = struct.Struct("<64s64sQiIIII3f3ffI")
CHAIN_LINK = struct.Struct("<IIff4I")
LINK_COLLISION = struct.Struct("<IIIf4I")
# Optional tail table, one entry per Joint, guarded by a new capability.
# group=0, mask=0 means inherit; group>0 means explicit (mask=0 disables).
JOINT_COLLISION_FILTER = struct.Struct("<II")

CAP_LIMIT_REFERENCE_DIRECTION = 1 << 0
CAP_LIMIT_REFERENCE_FRAME = 1 << 1
CAP_DYNAMIC_COLLIDERS = 1 << 2
CAP_CHAIN_GROUP_LINKS = 1 << 3
CAP_LINK_COLLISIONS = 1 << 4
# Reserved legacy wire bit: readable for migration, never emitted or advertised.
CAP_SEGMENT_CONTACTS = 1 << 5
CAP_ROOT_SWING = 1 << 6
CAP_LOCAL_BEND = 1 << 7
CAP_STRIP_LINKS = 1 << 8
CAP_DUAL_ENDPOINT_CAPSULES = 1 << 9
CAP_COLLISION_FILTERS = 1 << 10
KNOWN_CAPABILITIES = (
    CAP_LIMIT_REFERENCE_DIRECTION
    | CAP_LIMIT_REFERENCE_FRAME
    | CAP_DYNAMIC_COLLIDERS
    | CAP_CHAIN_GROUP_LINKS
    | CAP_LINK_COLLISIONS
    | CAP_SEGMENT_CONTACTS
    | CAP_ROOT_SWING
    | CAP_LOCAL_BEND
    | CAP_STRIP_LINKS
    | CAP_DUAL_ENDPOINT_CAPSULES
    | CAP_COLLISION_FILTERS
)

CHAIN_FLAG_SEGMENT_CONTACTS = 1 << 0
CHAIN_FLAG_SELF_CONTACTS = 1 << 1
CHAIN_FLAG_ROOT_SWING = 1 << 2
CHAIN_FLAG_LOCAL_BEND = 1 << 3
LINK_FLAG_STRIP = 1 << 0
COLLIDER_FLAG_INSIDE = 1 << 0
COLLIDER_FLAG_DYNAMIC_ATTACHMENT = 1 << 1
COLLIDER_FLAG_DUAL_ENDPOINT = 1 << 2

LIMIT_KINDS = {"NONE": 0, "CONE": 1, "HINGE": 2, "SPHERICAL": 3}
LIMIT_KIND_NAMES = {value: key for key, value in LIMIT_KINDS.items()}
_GEOMETRY_EPSILON = 1.0e-6


def _strict_uint(value: object, where: str, maximum: int = 0xFFFFFFFF) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise Gate(f"{where}: 必须是 0..{maximum} 的整数（不接受 bool）")
    return value


def _strict_bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise Gate(f"{where}: 必须是布尔值")
    return value


def _reference_fields(raw: dict, where: str, *, limit_kind: str, local_index: int):
    mode_raw = raw.get("reference_mode", "ANIMATED_SEGMENT")
    try:
        if isinstance(mode_raw, str):
            mode = reference.REFERENCE_MODE_NAMES[mode_raw]
        else:
            mode = int(reference.ReferenceMode(int(mode_raw)))
    except (KeyError, TypeError, ValueError) as exc:
        raise Gate(f"{where}.reference_mode: 未知参考方向模式") from exc

    if local_index == 0 and mode != int(reference.ReferenceMode.ANIMATED_SEGMENT):
        raise Gate(f"{where}.reference_mode: 固定 driver 只能使用 ANIMATED_SEGMENT")
    if limit_kind == "NONE" and mode != int(reference.ReferenceMode.ANIMATED_SEGMENT):
        raise Gate(f"{where}.reference_mode: 无角限节点不能声明自定义参考方向")

    direction = raw.get("reference_direction", reference.DEFAULT_FORWARD)
    frame = raw.get("reference_frame", reference.IDENTITY_QUATERNION)
    flags = _strict_uint(raw.get("reference_flags", 0), f"{where}.reference_flags", 0xFF)
    if flags != 0:
        raise Gate(f"{where}.reference_flags: 当前版本必须为 0")
    try:
        mode, direction, frame = reference.validate_fields(
            mode, direction, frame, where=where
        )
    except reference.ReferenceError as exc:
        raise Gate(str(exc)) from exc
    if mode == int(reference.ReferenceMode.EXPLICIT_FRAME) and "reference_frame" not in raw:
        raise Gate(f"{where}.reference_frame: EXPLICIT_FRAME 必须提供四元数")
    if mode in (
        int(reference.ReferenceMode.PARENT_REST),
        int(reference.ReferenceMode.EXPLICIT_LOCAL),
    ) and "reference_direction" not in raw:
        raise Gate(f"{where}.reference_direction: 当前模式必须提供方向")
    return mode, direction, frame, flags


def _check_rest_limit(raw_joint: dict, joint_where: str, limit_kind: str,
                      limit_axis: tuple[float, float, float], previous: dict | None,
                      bone: dict) -> None:
    if previous is None or limit_kind not in {"HINGE", "SPHERICAL"}:
        return
    if previous["rest_basis"] is None:
        return
    basis = previous["rest_basis"]
    axis_model = tuple(
        sum(basis[row * 3 + column] * limit_axis[column] for column in range(3))
        for row in range(3)
    )
    segment = tuple(
        bone["rest_head"][axis] - previous["rest_head"][axis] for axis in range(3)
    )
    axis_length = math.sqrt(sum(value * value for value in axis_model))
    segment_length = math.sqrt(sum(value * value for value in segment))
    if axis_length < _GEOMETRY_EPSILON or segment_length < _GEOMETRY_EPSILON:
        raise Gate(f"{joint_where}.limit_axis: Rest basis 或入段长度退化")
    alignment = abs(sum(axis_model[axis] * segment[axis] for axis in range(3)) /
                    (axis_length * segment_length))
    if ((limit_kind == "HINGE" and alignment > 1.0e-3) or
            (limit_kind == "SPHERICAL" and alignment > 1.0 - 1.0e-4)):
        raise Gate(f"{joint_where}.limit_axis: 在 Rest 姿态中退化")


def _validate_colliders(document: dict, bones_by_name: dict[str, dict], simulated: set[str],
                        simulated_radii: list[tuple[int, int, float]]) -> list[dict]:
    swinging_roots = {c['joints'][0]['bone'] for c in document['chains'] if c.get('root_swing', False)}
    raw_colliders = document.get("colliders", [])
    if not isinstance(raw_colliders, list) or len(raw_colliders) > 1024:
        raise Gate("colliders: 数量必须为 0..1024")
    colliders: list[dict] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_colliders):
        where = f"colliders[{index}]"
        if not isinstance(raw, dict):
            raise Gate(f"{where}: expected an object")
        name = raw.get("name")
        _name64(name, where)
        if name in names:
            raise Gate(f"{where}: 碰撞体名称重复")
        names.add(name)
        kind = raw.get("kind", "SPHERE")
        if kind not in {"SPHERE", "CAPSULE", "PLANE"}:
            raise Gate(f"{where}.kind: 不支持的碰撞体类型")
        bone_name = raw.get("bone", "")
        if bone_name in swinging_roots:
            raise Gate(f"{where}: 当前根骨摆动不支持附着碰撞体；身体避让体请附着固定父骨")
        dynamic = _strict_bool(raw.get("dynamic", False), f"{where}.dynamic")
        if bone_name:
            bone = bones_by_name.get(bone_name)
            if bone is None:
                raise Gate(f"{where}.bone: 未导出的骨骼")
            is_simulated = bone_name in simulated
            if dynamic != is_simulated:
                raise Gate(f"{where}: dynamic 与附着骨类别不一致")
            palette_slot = bone["palette_slot"]
            name_hash = bone_name_hash(bone_name)
        else:
            _name64("", f"{where}.bone", allow_empty=True)
            if dynamic:
                raise Gate(f"{where}: Unit 根碰撞体不能声明 dynamic")
            palette_slot = -1
            name_hash = 0
        if dynamic and kind == "PLANE":
            raise Gate(f"{where}: 动态碰撞体只允许 SPHERE/CAPSULE")
        bone_b = raw.get("bone_b", "")
        _name64(bone_b, f"{where}.bone_b", allow_empty=True)
        slot_b = -1
        if bone_b:
            if kind != "CAPSULE" or dynamic or not bone_name:
                raise Gate(f"{where}: 双端胶囊必须绑定两根固定骨")
            if bone_b not in bones_by_name or bone_b in simulated or bone_b in swinging_roots:
                raise Gate(f"{where}.bone_b: 未导出或正在模拟的终点骨")
            slot_b = bones_by_name[bone_b]["palette_slot"]
        group_mask = _strict_uint(raw.get("group_mask", 1), f"{where}.group_mask")
        if group_mask == 0:
            raise Gate(f"{where}.group_mask: 不能为零")
        collision_mask = _strict_uint(
            raw.get("collision_mask", 0xFFFFFFFF), f"{where}.collision_mask"
        )
        inside = _strict_bool(raw.get("inside", False), f"{where}.inside")
        if dynamic and inside:
            raise Gate(f"{where}: 动态碰撞体不能使用 inside")
        a = _vec3(raw.get("a", (0.0, 0.0, 0.0)), f"{where}.a")
        b = _vec3(raw.get("b", (0.0, 0.0, 1.0)), f"{where}.b")
        if kind == "PLANE" and math.sqrt(sum(value * value for value in b)) < _GEOMETRY_EPSILON:
            raise Gate(f"{where}.b: 平面法线不能为零")
        radius = _finite(raw.get("radius", 0.1), f"{where}.radius", 0.0, 10.0)
        if kind != "PLANE" and radius <= 0.0:
            raise Gate(f"{where}.radius: 必须大于零")
        if inside and kind != "PLANE":
            if any((chain_group & collision_mask) != 0
                   and (group_mask & chain_collision) != 0
                   and joint_radius > radius
                   for chain_group, chain_collision, joint_radius in simulated_radii):
                raise Gate(f"{where}: inside 碰撞体小于受影响节点半径")
        colliders.append({
            "name": name, "bone": bone_name, "name_hash": name_hash,
            "palette_slot": palette_slot, "kind": kind, "group_mask": group_mask,
            "collision_mask": collision_mask, "inside": inside,
            "dynamic": dynamic, "a": a, "b": b, "radius": radius,
            "bone_b": bone_b, "palette_slot_b": slot_b,
        })
    return sorted(colliders, key=lambda collider: collider["name"])


def _validate_chain_links(document: dict, chain_index: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    raw_links = document.get("chain_links", [])
    if not isinstance(raw_links, list) or len(raw_links) > 1024:
        raise Gate("chain_links: 数量必须为 0..1024")
    links: list[dict] = []
    pairs: set[tuple[int, int]] = set()
    names: set[str] = set()
    for raw_index, raw in enumerate(raw_links):
        where = f"chain_links[{raw_index}]"
        if not isinstance(raw, dict):
            raise Gate(f"{where}: 必须是对象")
        name = raw.get("name")
        _name64(name, f"{where}.name")
        if name in names:
            raise Gate(f"{where}.name: 主 Link 名称重复")
        names.add(name)
        name_a = raw.get("chain_a")
        name_b = raw.get("chain_b")
        if not isinstance(name_a, str) or name_a not in chain_index:
            raise Gate(f"{where}.chain_a: 未知骨链")
        if not isinstance(name_b, str) or name_b not in chain_index:
            raise Gate(f"{where}.chain_b: 未知骨链")
        index_a = chain_index[name_a]["index"]
        index_b = chain_index[name_b]["index"]
        if index_a == index_b:
            raise Gate(f"{where}: 主 Link 的两条骨链必须不同")
        pair = min(index_a, index_b), max(index_a, index_b)
        if pair in pairs:
            raise Gate(f"{where}: 反向或同向 Chain Link 重复")
        pairs.add(pair)
        flags = _strict_uint(raw.get("flags", 0), f"{where}.flags")
        if flags & ~LINK_FLAG_STRIP:
            raise Gate(f"{where}.flags: 未知 Link 标志")
        if flags & LINK_FLAG_STRIP and chain_index[name_a]["count"] != chain_index[name_b]["count"]:
            raise Gate(f"{where}: 布条横向约束要求两链节点数相等")
        coupling = _finite(raw.get("coupling", 1.0), f"{where}.coupling", 0.0, 1.0)
        if coupling <= 0.0:
            raise Gate(f"{where}.coupling: 必须大于零")
        max_correction = _finite(
            raw.get("max_correction", 0.25), f"{where}.max_correction", 0.0, 10.0
        )
        if max_correction <= 0.0:
            raise Gate(f"{where}.max_correction: 必须大于零")
        raw_children = raw.get("collisions", [])
        if not isinstance(raw_children, list):
            raise Gate(f"{where}.collisions: 必须是数组")
        count_a = chain_index[name_a]["count"] - 1
        count_b = chain_index[name_b]["count"] - 1
        if raw_children and count_a != count_b:
            raise Gate(f"{where}.collisions: 两链模拟节点数不等")
        children: list[dict] = []
        seen_indices: set[int] = set()
        for child_index, child in enumerate(raw_children):
            child_where = f"{where}.collisions[{child_index}]"
            if not isinstance(child, dict):
                raise Gate(f"{child_where}: 必须是对象")
            local_index = _strict_uint(
                child.get("local_joint_index"), f"{child_where}.local_joint_index", 63
            )
            if local_index == 0 or local_index > count_a:
                raise Gate(f"{child_where}.local_joint_index: 必须从首模拟节点 1 开始")
            if local_index in seen_indices:
                raise Gate(f"{child_where}.local_joint_index: 重复")
            seen_indices.add(local_index)
            group_mask = _strict_uint(child.get("group_mask", 1), f"{child_where}.group_mask")
            collision_mask = _strict_uint(
                child.get("collision_mask", 0xFFFFFFFF), f"{child_where}.collision_mask"
            )
            if group_mask == 0 or collision_mask == 0:
                raise Gate(f"{child_where}: group/collision mask 不能为零")
            radius = _finite(child.get("radius", 0.01), f"{child_where}.radius", 0.0, 10.0)
            if radius <= 0.0:
                raise Gate(f"{child_where}.radius: 必须大于零")
            child_flags = _strict_uint(child.get("flags", 0), f"{child_where}.flags")
            if child_flags:
                raise Gate(f"{child_where}.flags: 当前版本必须为零")
            children.append({
                "local_joint_index": local_index,
                "group_mask": group_mask,
                "collision_mask": collision_mask,
                "radius": radius,
            })
        if children and seen_indices != set(range(1, count_a + 1)):
            raise Gate(f"{where}.collisions: 必须完整连续覆盖 1..{count_a}")
        children.sort(key=lambda item: item["local_joint_index"])
        links.append({
            "name": name,
            "chain_a_index": pair[0], "chain_b_index": pair[1],
            "coupling": coupling, "max_correction": max_correction,
            "children": children, "flags": flags,
        })
    links.sort(key=lambda item: item["name"].encode("utf-8"))
    collisions: list[dict] = []
    for link_index, link in enumerate(links):
        link["first_link_collision"] = len(collisions)
        link["link_collision_count"] = len(link["children"])
        for child in link["children"]:
            collisions.append({"chain_link_index": link_index, **child})
    if len(collisions) > 8192:
        raise Gate("link_collisions: 数量超过 8192")
    return links, collisions


_WIRE_PI = struct.unpack('<f', struct.pack('<f', math.pi))[0]


def build(document: dict) -> tuple[bytes, dict]:
    """将 canonical 文档编译为当前扩展 HD2PHY1 bytes。"""

    if not isinstance(document, dict):
        raise Gate("top level must be an object")
    if "link_pairs" in document:
        raise Gate("HD2PHY1 不再支持 link_pairs；请使用 chain_links 及其 collisions")
    if "link_collisions" in document:
        raise Gate("link_collisions: 必须内嵌在所属 chain_links[].collisions")
    raw_unit_id = document.get("unit_id")
    if isinstance(raw_unit_id, bool):
        raise Gate("unit_id: 不接受 bool")
    unit_id = parse_unit_id(raw_unit_id)
    palette_slots = _strict_uint(document.get("palette_slots"), "palette_slots", 4096)
    if palette_slots == 0:
        raise Gate("palette_slots: 至少为 1")
    fixed_dt = _finite(document.get("fixed_dt", 1.0 / 60.0), "fixed_dt",
                       1.0 / 240.0, 1.0 / 15.0)
    bones, bones_by_name = _validated_bones(document, palette_slots)

    raw_chains = document.get("chains")
    if not isinstance(raw_chains, list) or not 1 <= len(raw_chains) <= 1024:
        raise Gate("chains: 数量必须为 1..1024")
    named_chains: list[tuple[str, dict]] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_chains):
        where = f"chains[{index}]"
        if not isinstance(raw, dict):
            raise Gate(f"{where}: expected an object")
        name = raw.get("name")
        _name64(name, where)
        if name in names:
            raise Gate(f"{where}: 骨链名称重复")
        names.add(name)
        named_chains.append((name, raw))
    named_chains.sort(key=lambda item: item[0].encode("utf-8"))

    chain_records = bytearray()
    joint_records = bytearray()
    chain_index: dict[str, dict] = {}
    all_joints: list[dict] = []
    drivers: set[str] = set()
    simulated: set[str] = set()
    simulated_radii: list[tuple[int, int, float]] = []
    capabilities = 0
    for current_chain_index, (name, raw_chain) in enumerate(named_chains):
        where = f"chains[{name!r}]"
        raw_joints = raw_chain.get("joints")
        if not isinstance(raw_joints, list) or not 2 <= len(raw_joints) <= 64:
            raise Gate(f"{where}.joints: 数量必须为 2..64（含固定 driver）")
        start = len(all_joints)
        chain_index[name] = {
            "index": current_chain_index, "start": start, "count": len(raw_joints)
        }
        stiffness = _finite(raw_chain.get("stiffness", 8.0), f"{where}.stiffness", 0.0, 1000.0)
        damping = _finite(raw_chain.get("damping", 0.25), f"{where}.damping", 0.0, 1.0)
        gravity = _vec3(raw_chain.get("gravity", (0.0, 0.0, -9.81)), f"{where}.gravity")
        wind = _vec3(raw_chain.get("wind", (0.0, 0.0, 0.0)), f"{where}.wind")
        group_mask = _strict_uint(raw_chain.get("group_mask", 1), f"{where}.group_mask")
        collision_mask = _strict_uint(raw_chain.get("collision_mask", 0xFFFFFFFF),
                                      f"{where}.collision_mask")
        if group_mask == 0:
            raise Gate(f"{where}: group mask 不能为零")
        if collision_mask == 0:
            capabilities |= CAP_COLLISION_FILTERS
        retired = [key for key in ("segment_contacts", "self_contacts")
                   if _strict_bool(raw_chain.get(key, False), f"{where}.{key}")]
        if retired:
            warnings.warn(f"{where}: 已移除链段/同链自碰撞；旧字段不再导出，使用节点碰撞",
                          UserWarning, stacklevel=2)
        root_swing = _strict_bool(raw_chain.get("root_swing", False), f"{where}.root_swing")
        local_bend = _strict_bool(raw_chain.get("local_bend", False), f"{where}.local_bend")
        chain_flags = ((CHAIN_FLAG_ROOT_SWING if root_swing else 0)
                       | (CHAIN_FLAG_LOCAL_BEND if local_bend else 0))
        if local_bend:
            capabilities |= CAP_LOCAL_BEND
        if root_swing:
            capabilities |= CAP_ROOT_SWING
        chain_records += CHAIN.pack(_name64(name, where), start, len(raw_joints), stiffness,
                                    damping, *gravity, *wind, group_mask, collision_mask,
                                    chain_flags, 0)
        previous: dict | None = None
        for local_index, raw_joint in enumerate(raw_joints):
            joint_where = f"{where}.joints[{local_index}]"
            if not isinstance(raw_joint, dict):
                raise Gate(f"{joint_where}: expected an object")
            bone_name = raw_joint.get("bone")
            bone = bones_by_name.get(bone_name)
            if bone is None:
                raise Gate(f"{joint_where}.bone: 未导出的骨骼")
            if previous is not None and bone["parent_slot"] != previous["palette_slot"]:
                raise Gate(f"{joint_where}: 必须形成直接父子路径")
            if previous is not None and math.dist(bone["rest_head"], previous["rest_head"]) < _GEOMETRY_EPSILON:
                raise Gate(f"{joint_where}: Rest 段不能为零长度")
            if (local_index > 0 or root_swing) and bone_name in simulated:
                raise Gate(f"{joint_where}.bone: 模拟骨骼属于另一条骨链")
            if local_index == 0 and not root_swing:
                drivers.add(bone_name)
            else:
                simulated.add(bone_name)
            radius = _finite(raw_joint.get("radius", 0.02), f"{joint_where}.radius", 0.0, 10.0)
            limit_kind = raw_joint.get("limit_kind", "NONE")
            if limit_kind not in LIMIT_KINDS:
                raise Gate(f"{joint_where}.limit_kind: 不支持的角限类型")
            limit_angle = _finite(raw_joint.get("limit_angle", math.radians(30.0)),
                                  f"{joint_where}.limit_angle", 0.0, _WIRE_PI)
            limit_angle2 = _finite(raw_joint.get("limit_angle2", limit_angle),
                                   f"{joint_where}.limit_angle2", 0.0, _WIRE_PI)
            limit_axis = _vec3(raw_joint.get("limit_axis", (1.0, 0.0, 0.0)),
                               f"{joint_where}.limit_axis")
            if limit_kind in {"HINGE", "SPHERICAL"} and math.sqrt(sum(x * x for x in limit_axis)) < _GEOMETRY_EPSILON:
                raise Gate(f"{joint_where}.limit_axis: 角限轴不能为零")
            _check_rest_limit(raw_joint, joint_where, limit_kind, limit_axis, previous, bone)
            mode, direction, frame, flags = _reference_fields(
                raw_joint, joint_where, limit_kind=limit_kind, local_index=local_index
            )
            if mode == int(reference.ReferenceMode.PARENT_REST):
                if previous is None or previous["rest_basis"] is None:
                    raise Gate(
                        f"{joint_where}.reference_direction: PARENT_REST 缺少父骨 Rest basis"
                    )
                segment_model = tuple(
                    bone["rest_head"][axis] - previous["rest_head"][axis]
                    for axis in range(3)
                )
                basis = previous["rest_basis"]
                # Blender/文档中的 Rest basis 把父骨局部列向量旋转到模型空间；
                # 正交基的逆为转置，因此这里把 parent->current 入段转回父骨局部。
                segment_local = tuple(
                    sum(basis[row * 3 + column] * segment_model[row]
                        for row in range(3))
                    for column in range(3)
                )
                try:
                    direction = reference.normalize(
                        segment_local, f"{joint_where}.reference_direction"
                    )
                except reference.ReferenceError as exc:
                    raise Gate(str(exc)) from exc
            if mode in (int(reference.ReferenceMode.PARENT_REST), int(reference.ReferenceMode.EXPLICIT_LOCAL)):
                capabilities |= CAP_LIMIT_REFERENCE_DIRECTION
            elif mode == int(reference.ReferenceMode.EXPLICIT_FRAME):
                capabilities |= CAP_LIMIT_REFERENCE_FRAME
            stiffness_scale = _finite(raw_joint.get("stiffness_scale", 1.0),
                                      f"{joint_where}.stiffness_scale", 0.0, 100.0)
            damping_scale = _finite(raw_joint.get("damping_scale", 1.0),
                                    f"{joint_where}.damping_scale", 0.0, 100.0)
            override_filter = _strict_bool(raw_joint.get("override_collision_filter", False),
                                           f"{joint_where}.override_collision_filter")
            node_group = _strict_uint(raw_joint.get("group_mask", 1), f"{joint_where}.group_mask")
            node_mask = _strict_uint(raw_joint.get("collision_mask", 0xFFFFFFFF), f"{joint_where}.collision_mask")
            if override_filter:
                if local_index == 0 or node_group == 0:
                    raise Gate(f"{joint_where}: 固定位置根节点不能覆盖碰撞，节点所属组不能为 0")
                capabilities |= CAP_COLLISION_FILTERS
            else:
                node_group = node_mask = 0
            if local_index > 0:
                simulated_radii.append((node_group if override_filter else group_mask,
                                        node_mask if override_filter else collision_mask, radius))
            record = {
                "bone": bone_name, "bone_hash": bone_name_hash(bone_name),
                "palette_slot": bone["palette_slot"], "radius": radius,
                "limit_kind": limit_kind, "limit_angle": limit_angle,
                "limit_angle2": limit_angle2, "limit_axis": limit_axis,
                "stiffness_scale": stiffness_scale, "damping_scale": damping_scale,
                "rest_head": bone["rest_head"], "reference_mode": mode,
                "reference_direction": direction, "reference_frame": frame,
                "reference_flags": flags, "chain": name, "local_index": local_index,
                "override_collision_filter": override_filter, "group_mask": node_group, "collision_mask": node_mask,
            }
            all_joints.append(record)
            joint_records += JOINT.pack(
                _name64(bone_name, joint_where), bone_name_hash(bone_name),
                bone["palette_slot"], radius, LIMIT_KINDS[limit_kind], limit_angle,
                limit_angle2, *limit_axis, stiffness_scale, damping_scale,
                *bone["rest_head"], 0, mode, flags, 0, *direction, *frame, 0
            )
            previous = bone
    if len(all_joints) > 8192:
        raise Gate("joint count exceeds 8192")
    overlap = drivers.intersection(simulated)
    if overlap:
        raise Gate(f"模拟骨骼不能驱动另一条 HD2PHY1 骨链：{sorted(overlap)[0]}")

    colliders = _validate_colliders(document, bones_by_name, simulated, simulated_radii)
    if any(collider["dynamic"] for collider in colliders):
        capabilities |= CAP_DYNAMIC_COLLIDERS
    if any(collider["bone_b"] for collider in colliders):
        capabilities |= CAP_DUAL_ENDPOINT_CAPSULES
    if any(collider["collision_mask"] == 0 for collider in colliders):
        capabilities |= CAP_COLLISION_FILTERS
    collider_records = bytearray()
    for collider in colliders:
        collider_records += COLLIDER.pack(
            _name64(collider["name"], "collider"),
            _name64(collider["bone"], "collider.bone", allow_empty=True),
            collider["name_hash"], collider["palette_slot"],
            {"SPHERE": 0, "CAPSULE": 1, "PLANE": 2}[collider["kind"]],
            collider["group_mask"], collider["collision_mask"],
            ((COLLIDER_FLAG_INSIDE if collider["inside"] else 0)
             | (COLLIDER_FLAG_DYNAMIC_ATTACHMENT if collider["dynamic"] else 0)
             | (COLLIDER_FLAG_DUAL_ENDPOINT if collider["bone_b"] else 0)),
            *collider["a"], *collider["b"], collider["radius"], collider["palette_slot_b"] + 1
        )

    chain_links, link_collisions = _validate_chain_links(document, chain_index)
    chain_link_records = bytearray()
    for link in chain_links:
        chain_link_records += CHAIN_LINK.pack(
            link["chain_a_index"], link["chain_b_index"], link["coupling"],
            link["max_correction"], link["flags"], link["first_link_collision"],
            link["link_collision_count"], 0,
        )
    link_collision_records = bytearray()
    for collision in link_collisions:
        link_collision_records += LINK_COLLISION.pack(
            collision["chain_link_index"], collision["local_joint_index"],
            collision["group_mask"], collision["radius"],
            collision["collision_mask"], 0, 0, 0,
        )
    if chain_links:
        capabilities |= CAP_CHAIN_GROUP_LINKS
    if any(link["flags"] & LINK_FLAG_STRIP for link in chain_links):
        capabilities |= CAP_STRIP_LINKS
    if link_collisions:
        capabilities |= CAP_LINK_COLLISIONS

    payload = bytes(chain_records + joint_records + collider_records
                    + chain_link_records + link_collision_records)
    if capabilities & CAP_COLLISION_FILTERS:
        payload += b"".join(JOINT_COLLISION_FILTER.pack(j["group_mask"], j["collision_mask"])
                            for j in all_joints)
    fingerprint = skeleton_fingerprint(palette_slots, bones)
    file_size = HEADER.size + len(payload)
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    header = HEADER.pack(
        MAGIC, VERSION, HEADER.size, file_size, crc, unit_id, fingerprint,
        palette_slots, len(bones), len(named_chains), len(all_joints), len(colliders),
        len(chain_links), len(link_collisions), fixed_dt, capabilities, 0
    )
    summary = {
        "version": VERSION, "unit_id": f"{unit_id:016x}",
        "skeleton_fingerprint": f"{fingerprint:016x}", "palette_slots": palette_slots,
        "target_bones": len(bones), "chains": len(named_chains), "joints": len(all_joints),
        "colliders": len(colliders), "chain_links": len(chain_links),
        "link_collisions": len(link_collisions),
        "capabilities": capabilities, "payload_crc32": f"{crc:08x}", "bytes": file_size,
    }
    return header + payload, summary


def _decode_name(blob: bytes, where: str, *, allow_empty: bool = False) -> str:
    if len(blob) != 64:
        raise Gate(f"{where}: 名称字段长度错误")
    nul = blob.find(b"\0")
    if nul < 0:
        raise Gate(f"{where}: 名称缺少 NUL 终止")
    if any(blob[nul + 1:]):
        raise Gate(f"{where}: 名称 padding 必须为零")
    try:
        value = blob[:nul].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Gate(f"{where}: 名称不是合法 UTF-8") from exc
    _name64(value, where, allow_empty=allow_empty)
    return value


def _unpack(blob: bytes | bytearray | memoryview) -> tuple:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise Gate("HD2PHY1 输入必须是 bytes")
    data = bytes(blob)
    if len(data) < HEADER.size:
        raise Gate("HD2PHY1 文件短于 header")
    header = HEADER.unpack_from(data)
    (magic, version, header_size, file_size, crc, unit_id, fingerprint,
     palette_slots, bone_count, chain_count, joint_count, collider_count,
     chain_link_count, link_collision_count, fixed_dt, capabilities, reserved) = header
    if magic != MAGIC or version != VERSION:
        raise Gate("HD2PHY1 magic/version 不受支持")
    if header_size != HEADER.size or file_size != len(data):
        raise Gate("HD2PHY1 header 或文件长度不匹配")
    if not 1 <= unit_id <= 0xFFFFFFFFFFFFFFFF:
        raise Gate("HD2PHY1 unit_id 越界")
    if capabilities & ~KNOWN_CAPABILITIES:
        raise Gate("HD2PHY1 包含未知 capability 位")
    if reserved:
        raise Gate("HD2PHY1 reserved 字段必须为零")
    if not 1 <= palette_slots <= 4096 or not 1 <= bone_count <= 256:
        raise Gate("HD2PHY1 骨架数量越界")
    if not 1 <= chain_count <= 1024 or not 2 <= joint_count <= 8192:
        raise Gate("HD2PHY1 骨链或节点数量越界")
    if collider_count > 1024:
        raise Gate("HD2PHY1 碰撞体数量越界")
    if chain_link_count > 1024 or link_collision_count > 8192:
        raise Gate("HD2PHY1 Chain Link/Link Collision 数量越界")
    if not math.isfinite(fixed_dt) or not 1.0 / 240.0 <= fixed_dt <= 1.0 / 15.0:
        raise Gate("HD2PHY1 fixed_dt 越界")
    expected = (HEADER.size + chain_count * CHAIN.size + joint_count * JOINT.size
                + collider_count * COLLIDER.size + chain_link_count * CHAIN_LINK.size
                + link_collision_count * LINK_COLLISION.size)
    if capabilities & CAP_COLLISION_FILTERS:
        expected += joint_count * JOINT_COLLISION_FILTER.size
    if expected != len(data):
        raise Gate("HD2PHY1 表长度与文件大小不匹配")
    payload = data[HEADER.size:]
    if zlib.crc32(payload) & 0xFFFFFFFF != crc:
        raise Gate("HD2PHY1 payload CRC32 不匹配")
    return data, header


def parse(blob: bytes | bytearray | memoryview) -> dict[str, Any]:
    """严格解析扩展 v1，返回 wire inspection view。

    Chain Link 作者名称不进入 wire，因此这里不会伪造 ``name``；解析结果用于检查，
    不保证能不经 canonical authoring 层补全后直接回送 :func:`build`。
    """

    data, header = _unpack(blob)
    (_, _, _, file_size, crc, unit_id, fingerprint, palette_slots, bone_count,
     chain_count, joint_count, collider_count, chain_link_count, link_collision_count,
     fixed_dt, capabilities, _reserved) = header
    offset = HEADER.size
    chains: list[dict] = []
    chain_by_name: dict[str, dict] = {}
    chain_for_joint: list[tuple[str, int]] = [None] * joint_count  # type: ignore[list-item]
    for index in range(chain_count):
        values = CHAIN.unpack_from(data, offset)
        offset += CHAIN.size
        name = _decode_name(values[0], f"chains[{index}]")
        start, count, stiffness, damping = values[1:5]
        gravity = tuple(values[5:8])
        wind = tuple(values[8:11])
        group_mask, collision_mask, flags, reserved = values[11:15]
        if (reserved or flags & ~(CHAIN_FLAG_SEGMENT_CONTACTS | CHAIN_FLAG_SELF_CONTACTS | CHAIN_FLAG_ROOT_SWING | CHAIN_FLAG_LOCAL_BEND)
                or not group_mask or (not collision_mask and not capabilities & CAP_COLLISION_FILTERS)
                or flags & CHAIN_FLAG_SELF_CONTACTS and not flags & CHAIN_FLAG_SEGMENT_CONTACTS
                or not all(math.isfinite(value) for value in
                           (stiffness, damping, *gravity, *wind))
                or not 0.0 <= stiffness <= 1000.0 or not 0.0 <= damping <= 1.0
                or not 2 <= count <= 64
                or index and name.encode("utf-8") <= chains[-1]["name"].encode("utf-8")):
            raise Gate("HD2PHY1 骨链必须按名称严格排序且 reserved 为零")
        if start != (chains[-1]["start"] + chains[-1]["count"] if chains else 0):
            raise Gate("HD2PHY1 骨链 joint 范围不连续")
        item = {"name": name, "start": start, "count": count,
                "stiffness": stiffness, "damping": damping,
                "gravity": gravity, "wind": wind, "group_mask": group_mask,
                "collision_mask": collision_mask,
                "segment_contacts": bool(flags & CHAIN_FLAG_SEGMENT_CONTACTS),
                "self_contacts": bool(flags & CHAIN_FLAG_SELF_CONTACTS),
                "root_swing": bool(flags & CHAIN_FLAG_ROOT_SWING),
                "local_bend": bool(flags & CHAIN_FLAG_LOCAL_BEND)}
        chain_by_name[name] = item
        chains.append(item)
        for local_index in range(count):
            if start + local_index >= joint_count:
                raise Gate("HD2PHY1 骨链 joint 范围越界")
            chain_for_joint[start + local_index] = (name, local_index)
    if any(item is None for item in chain_for_joint):
        raise Gate("HD2PHY1 骨链 joint 范围没有覆盖全部节点")
    joints: list[dict] = []
    simulated_names: set[str] = set()
    driver_names: set[str] = set()
    required_direction = False
    required_frame = False
    for index in range(joint_count):
        values = JOINT.unpack_from(data, offset)
        offset += JOINT.size
        (name_blob, name_hash, palette_slot, radius, kind_value, angle, angle2,
         axis_x, axis_y, axis_z, stiffness_scale, damping_scale, head_x, head_y,
         head_z, reserved, mode, flags, reference_reserved, dir_x, dir_y, dir_z,
         qx, qy, qz, qw, extension_reserved) = values
        name = _decode_name(name_blob, f"joints[{index}]")
        if name_hash != bone_name_hash(name) or reserved or reference_reserved or extension_reserved:
            raise Gate(f"joints[{index}]: hash 或 reserved 字段错误")
        if not 0 <= palette_slot < palette_slots or not 0.0 <= radius <= 10.0:
            raise Gate(f"joints[{index}]: palette/radius 越界")
        if kind_value not in LIMIT_KIND_NAMES or not 0.0 <= angle <= _WIRE_PI or not 0.0 <= angle2 <= _WIRE_PI:
            raise Gate(f"joints[{index}]: 角限字段越界")
        if not all(math.isfinite(value) for value in (radius, angle, angle2, axis_x, axis_y, axis_z,
                                                       stiffness_scale, damping_scale,
                                                       head_x, head_y, head_z)):
            raise Gate(f"joints[{index}]: 存在非有限字段")
        if not 0.0 <= stiffness_scale <= 100.0 or not 0.0 <= damping_scale <= 100.0:
            raise Gate(f"joints[{index}]: stiffness/damping scale 越界")
        if kind_value in (LIMIT_KINDS["HINGE"], LIMIT_KINDS["SPHERICAL"]):
            if math.sqrt(axis_x * axis_x + axis_y * axis_y + axis_z * axis_z) < _GEOMETRY_EPSILON:
                raise Gate(f"joints[{index}]: 角限轴不能为零")
        if mode not in reference.REFERENCE_MODE_VALUES or flags != 0:
            raise Gate(f"joints[{index}]: reference mode/flags 不受支持")
        if kind_value == LIMIT_KINDS["NONE"] and mode != int(reference.ReferenceMode.ANIMATED_SEGMENT):
            raise Gate(f"joints[{index}]: 无角限节点不能使用显式参考方向")
        try:
            normalized_direction = reference.normalize((dir_x, dir_y, dir_z), f"joints[{index}].direction")
            normalized_frame = reference.normalize_quaternion((qx, qy, qz, qw), f"joints[{index}].frame")
        except reference.ReferenceError as exc:
            raise Gate(str(exc)) from exc
        if mode == int(reference.ReferenceMode.EXPLICIT_FRAME):
            required_frame = True
        elif mode in (int(reference.ReferenceMode.PARENT_REST), int(reference.ReferenceMode.EXPLICIT_LOCAL)):
            required_direction = True
        chain_name, local_index = chain_for_joint[index]
        if local_index == 0:
            if mode != int(reference.ReferenceMode.ANIMATED_SEGMENT):
                raise Gate(f"joints[{index}]: driver 不能使用显式参考方向")
        root_swing = chain_by_name[chain_name]["root_swing"]
        if local_index == 0 and not root_swing:
            driver_names.add(name)
        else:
            if name in simulated_names:
                raise Gate(f"joints[{index}]: 模拟骨骼重复")
            simulated_names.add(name)
        joints.append({
            "bone": name, "bone_hash": name_hash, "palette_slot": palette_slot,
            "radius": radius, "limit_kind": LIMIT_KIND_NAMES[kind_value],
            "limit_angle": angle, "limit_angle2": angle2,
            "limit_axis": (axis_x, axis_y, axis_z),
            "stiffness_scale": stiffness_scale, "damping_scale": damping_scale,
            "rest_head": (head_x, head_y, head_z), "reference_mode": reference.REFERENCE_MODE_VALUES[mode],
            "reference_direction": normalized_direction, "reference_frame": normalized_frame,
            "chain": chain_name, "local_index": local_index,
        })
    if driver_names & simulated_names:
        raise Gate("HD2PHY1 driver 与模拟骨骼集合重叠")
    if required_direction and not capabilities & CAP_LIMIT_REFERENCE_DIRECTION:
        raise Gate("HD2PHY1 缺少 reference direction capability")
    if required_frame and not capabilities & CAP_LIMIT_REFERENCE_FRAME:
        raise Gate("HD2PHY1 缺少 reference frame capability")

    extension_size = joint_count * JOINT_COLLISION_FILTER.size if capabilities & CAP_COLLISION_FILTERS else 0
    for index, joint in enumerate(joints):
        node_group, node_mask = (JOINT_COLLISION_FILTER.unpack_from(
            data, len(data) - extension_size + index * JOINT_COLLISION_FILTER.size)
            if extension_size else (0, 0))
        if (node_group == 0 and node_mask != 0) or (joint['local_index'] == 0 and node_group != 0):
            raise Gate(f"joints[{index}]: 碰撞继承标记或固定根覆盖非法")
        joint.update(override_collision_filter=node_group != 0, group_mask=node_group, collision_mask=node_mask)

    colliders: list[dict] = []
    collider_names: set[str] = set()
    collider_kind_names = {0: "SPHERE", 1: "CAPSULE", 2: "PLANE"}
    for index in range(collider_count):
        values = COLLIDER.unpack_from(data, offset)
        offset += COLLIDER.size
        (name_blob, bone_blob, name_hash, palette_slot, kind, group_mask,
         collision_mask, flags,
         ax, ay, az, bx, by, bz, radius, reserved) = values
        name = _decode_name(name_blob, f"colliders[{index}]")
        bone = _decode_name(bone_blob, f"colliders[{index}].bone", allow_empty=True)
        dual = bool(flags & COLLIDER_FLAG_DUAL_ENDPOINT)
        if name in collider_names or kind not in collider_kind_names or (reserved and not dual):
            raise Gate(f"colliders[{index}]: 名称、类型或 reserved 错误")
        if any(c['root_swing'] and joints[c['start']]['bone'] == bone for c in chains):
            raise Gate(f"colliders[{index}]: 根骨摆动不支持附着碰撞体")
        collider_names.add(name)
        if (group_mask == 0 or (collision_mask == 0 and not capabilities & CAP_COLLISION_FILTERS)
                or flags & ~(COLLIDER_FLAG_INSIDE | COLLIDER_FLAG_DYNAMIC_ATTACHMENT | COLLIDER_FLAG_DUAL_ENDPOINT)
                or not (0.0 <= radius <= 10.0 if kind == 2 else 0.0 < radius <= 10.0)):
            raise Gate(f"colliders[{index}]: mask/flags/radius 越界")
        dynamic = bool(flags & COLLIDER_FLAG_DYNAMIC_ATTACHMENT)
        inside = bool(flags & COLLIDER_FLAG_INSIDE)
        if dual:
            if kind != 1 or dynamic or not bone or not 1 <= reserved <= palette_slots:
                raise Gate(f"colliders[{index}]: 双端胶囊类型或终点槽位非法")
            forbidden = {j['palette_slot'] for j in joints if j['local_index'] > 0}
            forbidden.update(joints[c['start']]['palette_slot'] for c in chains if c['root_swing'])
            if reserved - 1 in forbidden:
                raise Gate(f"colliders[{index}]: 终点不能跟随模拟骨")
        if bone:
            if name_hash != bone_name_hash(bone) or not 0 <= palette_slot < palette_slots:
                raise Gate(f"colliders[{index}]: 附着骨骼 hash/slot 错误")
            if dynamic != (bone in simulated_names):
                raise Gate(f"colliders[{index}]: dynamic 与附着骨类别不一致")
        elif name_hash != 0 or palette_slot != -1:
            raise Gate(f"colliders[{index}]: Unit 根碰撞体必须使用空骨骼字段")
        elif dynamic:
            raise Gate(f"colliders[{index}]: Unit 根碰撞体不能为动态")
        if dynamic and (kind == 2 or inside):
            raise Gate(f"colliders[{index}]: 动态碰撞体类型/inside 非法")
        if kind == 2 and math.sqrt(bx * bx + by * by + bz * bz) < _GEOMETRY_EPSILON:
            raise Gate(f"colliders[{index}]: 平面法线不能为零")
        if not all(math.isfinite(value) for value in (ax, ay, az, bx, by, bz, radius)):
            raise Gate(f"colliders[{index}]: 存在非有限字段")
        colliders.append({
            "name": name, "bone": bone, "kind": collider_kind_names[kind],
            "group_mask": group_mask, "collision_mask": collision_mask,
            "inside": inside, "dynamic": dynamic,
            "a": (ax, ay, az), "b": (bx, by, bz), "radius": radius,
            "palette_slot_b": reserved - 1 if dual else -1,
        })
    if any(colliders[index]["name"].encode("utf-8") >= colliders[index + 1]["name"].encode("utf-8")
           for index in range(len(colliders) - 1)):
        raise Gate("HD2PHY1 碰撞体必须按名称严格排序")
    for collider_index, collider in enumerate(colliders):
        if not collider["inside"] or collider["kind"] == "PLANE":
            continue
        for joint in joints:
            if joint["local_index"] == 0:
                continue
            chain = chain_by_name[joint["chain"]]
            filtered = joint if joint['override_collision_filter'] else chain
            if ((filtered["group_mask"] & collider["collision_mask"])
                    and (collider["group_mask"] & filtered["collision_mask"])
                    and joint["radius"] > collider["radius"]):
                raise Gate(f"colliders[{collider_index}]: inside 小于受影响节点半径")

    chain_links: list[dict] = []
    link_pairs: set[tuple[int, int]] = set()
    child_cursor = 0
    for index in range(chain_link_count):
        values = CHAIN_LINK.unpack_from(data, offset)
        offset += CHAIN_LINK.size
        (chain_a_index, chain_b_index, coupling, max_correction, flags,
         first_collision, child_count, reserved) = values
        if (chain_a_index >= chain_count or chain_b_index >= chain_count
                or chain_a_index >= chain_b_index or flags & ~LINK_FLAG_STRIP or reserved
                or not math.isfinite(coupling) or not 0.0 < coupling <= 1.0
                or not math.isfinite(max_correction) or not 0.0 < max_correction <= 10.0):
            raise Gate(f"chain_links[{index}]: 索引、参数、flags 或 reserved 非法")
        pair = chain_a_index, chain_b_index
        if pair in link_pairs:
            raise Gate(f"chain_links[{index}]: 规范化 chain pair 重复")
        link_pairs.add(pair)
        if first_collision != child_cursor or child_cursor + child_count > link_collision_count:
            raise Gate(f"chain_links[{index}]: child range 不连续或越界")
        count_a = chains[chain_a_index]["count"] - 1
        count_b = chains[chain_b_index]["count"] - 1
        if flags & LINK_FLAG_STRIP and count_a != count_b:
            raise Gate(f"chain_links[{index}]: 布条横向约束要求两链节点数相等")
        if child_count and (count_a != count_b or child_count != count_a):
            raise Gate(f"chain_links[{index}]: Link Collision 必须等长完整覆盖")
        chain_links.append({
            "chain_a_index": chain_a_index, "chain_b_index": chain_b_index,
            "chain_a": chains[chain_a_index]["name"],
            "chain_b": chains[chain_b_index]["name"],
            "coupling": coupling, "max_correction": max_correction,
            "first_link_collision": first_collision,
            "link_collision_count": child_count, "collisions": [], "flags": flags,
        })
        child_cursor += child_count
    if child_cursor != link_collision_count:
        raise Gate("HD2PHY1 Chain Link child range 未覆盖 Link Collision 表")

    for index in range(link_collision_count):
        values = LINK_COLLISION.unpack_from(data, offset)
        offset += LINK_COLLISION.size
        (owner_index, local_index, group_mask, radius, collision_mask,
         flags, reserved0, reserved1) = values
        if owner_index >= chain_link_count:
            raise Gate(f"link_collisions[{index}]: owner 越界")
        owner = chain_links[owner_index]
        expected_index = owner["first_link_collision"] + len(owner["collisions"])
        max_local_index = chains[owner["chain_a_index"]]["count"] - 1
        if (index != expected_index or local_index != len(owner["collisions"]) + 1
                or local_index == 0 or local_index > max_local_index
                or group_mask == 0 or collision_mask == 0
                or not math.isfinite(radius) or not 0.0 < radius <= 10.0
                or flags or reserved0 or reserved1):
            raise Gate(f"link_collisions[{index}]: 顺序、字段或 reserved 非法")
        owner["collisions"].append({
            "local_joint_index": local_index, "radius": radius,
            "group_mask": group_mask, "collision_mask": collision_mask,
        })
    if any(len(link["collisions"]) != link["link_collision_count"] for link in chain_links):
        raise Gate("HD2PHY1 Link Collision child range 不完整")

    offset += extension_size
    expected_capabilities = 0
    if (any(j['override_collision_filter'] for j in joints)
            or any(c['collision_mask'] == 0 for c in chains + colliders)):
        expected_capabilities |= CAP_COLLISION_FILTERS
    if required_direction:
        expected_capabilities |= CAP_LIMIT_REFERENCE_DIRECTION
    if required_frame:
        expected_capabilities |= CAP_LIMIT_REFERENCE_FRAME
    if any(collider["dynamic"] for collider in colliders):
        expected_capabilities |= CAP_DYNAMIC_COLLIDERS
    if any(collider["palette_slot_b"] >= 0 for collider in colliders):
        expected_capabilities |= CAP_DUAL_ENDPOINT_CAPSULES
    if chain_links:
        expected_capabilities |= CAP_CHAIN_GROUP_LINKS
    if link_collision_count:
        expected_capabilities |= CAP_LINK_COLLISIONS
    if any(chain["segment_contacts"] for chain in chains):
        expected_capabilities |= CAP_SEGMENT_CONTACTS
    if any(chain["root_swing"] for chain in chains):
        expected_capabilities |= CAP_ROOT_SWING
    if any(chain["local_bend"] for chain in chains):
        expected_capabilities |= CAP_LOCAL_BEND
    if any(link["flags"] & LINK_FLAG_STRIP for link in chain_links):
        expected_capabilities |= CAP_STRIP_LINKS
    if capabilities != expected_capabilities:
        raise Gate("HD2PHY1 capability 位与 payload 实际内容不一致")
    if offset != len(data):
        raise Gate("HD2PHY1 存在未声明的尾部数据")
    return {
        "version": VERSION, "unit_id": f"{unit_id:016x}",
        "skeleton_fingerprint": f"{fingerprint:016x}", "palette_slots": palette_slots,
        "target_bones": bone_count, "fixed_dt": fixed_dt, "capabilities": capabilities,
        "chains": chains, "joints": joints, "colliders": colliders,
        "chain_links": chain_links, "link_collisions": link_collision_count,
        "payload_crc32": f"{crc:08x}", "bytes": file_size,
    }


def validate(document: dict) -> dict:
    """只执行扩展 HD2PHY1 serializer gate，返回摘要。"""

    return build(document)[1]


__all__ = (
    "CAP_COLLISION_FILTERS",
    "JOINT_COLLISION_FILTER",
    "CAP_CHAIN_GROUP_LINKS",
    "CAP_DYNAMIC_COLLIDERS",
    "CAP_LINK_COLLISIONS",
    "CAP_LIMIT_REFERENCE_DIRECTION",
    "CAP_LIMIT_REFERENCE_FRAME",
    "CAP_SEGMENT_CONTACTS",
    "CHAIN",
    "CHAIN_LINK",
    "COLLIDER",
    "HEADER",
    "JOINT",
    "LINK_COLLISION",
    "MAGIC",
    "VERSION",
    "build",
    "parse",
    "validate",
    "write_atomic",
)
