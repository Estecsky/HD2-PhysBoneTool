"""HD2 PhysBone P4 参数预设的安全、无 ``bpy`` 存储合同。

模块不会被写入 ``.hd2phys``。
与其他 Blender 插件的骨骼或顶点组字典是完全独立的格式和目录。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import unicodedata
from typing import Mapping

from .format_v1 import write_atomic


PRESET_SCHEMA = "HD2PB_PRESET"
PRESET_VERSION = 1
PRESET_SUFFIX = ".hd2pbpreset.json"
MAX_PRESET_BYTES = 1024 * 1024

ENTITY_PROFILE = "PROFILE"
ENTITY_NODE = "NODE"
ENTITY_COLLIDER = "COLLIDER"
ENTITY_CHAIN_LINK = "CHAIN_LINK"
ENTITY_TYPES = frozenset({ENTITY_PROFILE, ENTITY_NODE, ENTITY_COLLIDER, ENTITY_CHAIN_LINK})
NODE_DIRECTION_FIELDS = frozenset({"limit_axis", "limit_frame", "limit_frame_valid",
                                   "reference_mode", "reference_direction", "reference_frame"})

PRESET_UNITS = {
    "angle": "radian",
    "distance": "model_unit",
    "time": "second",
}

_TOP_LEVEL_KEYS = frozenset(
    {"schema", "version", "entity_type", "units", "parameters"}
)
_LIMIT_KINDS = frozenset({"NONE", "CONE", "HINGE", "SPHERICAL"})
_COLLIDER_KINDS = frozenset({"SPHERE", "CAPSULE", "PLANE"})
_RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class PresetError(ValueError):
    """参数预设不符合安全存储合同。"""


@dataclass(frozen=True)
class PresetEntry:
    """经过完整读取与校验的预设列表项。"""

    name: str
    entity_type: str
    path: Path


def _mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise PresetError(f"{where} 必须是 JSON 对象")
    if not all(isinstance(key, str) for key in value):
        raise PresetError(f"{where} 的所有键都必须是字符串")
    return value


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], where: str) -> None:
    actual = frozenset(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise PresetError(f"{where} 缺少字段：{', '.join(missing)}")
    if unknown:
        raise PresetError(f"{where} 包含未知字段：{', '.join(unknown)}")


def _number(value: object, where: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PresetError(f"{where} 必须是数字")
    result = float(value)
    if not math.isfinite(result):
        raise PresetError(f"{where} 必须是有限数字")
    if not minimum <= result <= maximum:
        raise PresetError(f"{where} 必须位于 {minimum} 到 {maximum} 之间")
    return 0.0 if result == 0.0 else result


def _integer(value: object, where: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PresetError(f"{where} 必须是整数")
    if not minimum <= value <= maximum:
        raise PresetError(f"{where} 必须位于 {minimum} 到 {maximum} 之间")
    return value


def _boolean(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise PresetError(f"{where} 必须是布尔值")
    return value


def _enum(value: object, where: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        choices = "、".join(sorted(allowed))
        raise PresetError(f"{where} 必须是以下值之一：{choices}")
    return value


def _vec3(value: object, where: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PresetError(f"{where} 必须是恰好包含 3 个数字的数组")
    return [
        _number(component, f"{where}[{index}]", -10000.0, 10000.0)
        for index, component in enumerate(value)
    ]


def _length_squared(value: list[float]) -> float:
    return sum(component * component for component in value)


def _validate_profile(parameters: Mapping[str, object]) -> dict[str, object]:
    expected = frozenset(
        {"stiffness", "damping", "gravity", "wind", "collision_mask"}
    )
    _exact_keys(parameters, expected, "parameters")
    return {
        "stiffness": _number(parameters["stiffness"], "parameters.stiffness", 0.0, 1000.0),
        "damping": _number(parameters["damping"], "parameters.damping", 0.0, 1.0),
        "gravity": _vec3(parameters["gravity"], "parameters.gravity"),
        "wind": _vec3(parameters["wind"], "parameters.wind"),
        "collision_mask": _integer(
            parameters["collision_mask"],
            "parameters.collision_mask",
            0,
            0x7FFFFFFF,
        ),
    }


def _validate_node(parameters: Mapping[str, object]) -> dict[str, object]:
    expected = frozenset(
        {
            "radius",
            "limit_kind",
            "limit_angle",
            "limit_angle2",
            "limit_axis",
            "stiffness_scale",
            "damping_scale",
        }
    )
    reference_fields = frozenset({'reference_mode','reference_direction','reference_frame'})
    has_reference = bool(reference_fields.intersection(parameters))
    frame_fields = frozenset({'limit_frame', 'limit_frame_valid'})
    has_frame = bool(frame_fields.intersection(parameters))
    collision_fields = frozenset({'override_collision_filter','group_mask','collision_mask'})
    has_collision = bool(collision_fields.intersection(parameters))
    _exact_keys(parameters, expected | (reference_fields if has_reference else frozenset())
                | (frame_fields if has_frame else frozenset())
                | (collision_fields if has_collision else frozenset()), "parameters")
    kind = _enum(parameters["limit_kind"], "parameters.limit_kind", _LIMIT_KINDS)
    axis = _vec3(parameters["limit_axis"], "parameters.limit_axis")
    if kind in {"HINGE", "SPHERICAL"} and _length_squared(axis) < 1.0e-12:
        raise PresetError(
            "parameters.limit_axis 在 HINGE 或 SPHERICAL 限制下不能是零向量"
        )
    result = {
        "radius": _number(parameters["radius"], "parameters.radius", 0.0, 10.0),
        "limit_kind": kind,
        "limit_angle": _number(
            parameters["limit_angle"], "parameters.limit_angle", 0.0, math.pi
        ),
        "limit_angle2": _number(
            parameters["limit_angle2"], "parameters.limit_angle2", 0.0, math.pi
        ),
        "limit_axis": axis,
        "stiffness_scale": _number(
            parameters["stiffness_scale"],
            "parameters.stiffness_scale",
            0.0,
            100.0,
        ),
        "damping_scale": _number(
            parameters["damping_scale"],
            "parameters.damping_scale",
            0.0,
            100.0,
        ),
    }
    if has_frame:
        valid = _boolean(parameters['limit_frame_valid'], 'parameters.limit_frame_valid')
        raw_frame = parameters['limit_frame']
        if not isinstance(raw_frame, (list, tuple)) or len(raw_frame) != 4:
            raise PresetError('parameters.limit_frame 必须是 XYZW 四元数')
        frame = [_number(v, 'parameters.limit_frame', -1.0, 1.0) for v in raw_frame]
        if valid and sum(v * v for v in frame) < 1e-12:
            raise PresetError('角限姿态四元数不能为零')
        result.update(limit_frame=frame, limit_frame_valid=valid)
    if has_reference:
        mode = _enum(parameters['reference_mode'],'parameters.reference_mode',frozenset({'ANIMATED_SEGMENT','PARENT_REST','EXPLICIT_LOCAL','EXPLICIT_FRAME'}))
        direction = _vec3(parameters['reference_direction'],'parameters.reference_direction')
        if any(abs(v) > 1.0 for v in direction):
            raise PresetError('parameters.reference_direction 各分量必须位于 -1 到 1，避免写入时被截断')
        raw_frame = parameters['reference_frame']
        if not isinstance(raw_frame,(list,tuple)) or len(raw_frame) != 4:
            raise PresetError('parameters.reference_frame 必须是 XYZW 四元数')
        frame = [_number(v,'parameters.reference_frame',-1.0,1.0) for v in raw_frame]
        if kind == 'NONE' and mode != 'ANIMATED_SEGMENT' and not result.get('limit_frame_valid'):
            raise PresetError('无角限节点不能声明自定义参考方向')
        if mode == 'EXPLICIT_LOCAL' and _length_squared(direction) < 1e-12:
            raise PresetError('显式参考方向不能为零')
        if mode == 'EXPLICIT_FRAME' and sum(v*v for v in frame) < 1e-12:
            raise PresetError('显式参考坐标架不能为零')
        result.update(reference_mode=mode,reference_direction=direction,reference_frame=frame)
    if has_collision:
        override = parameters['override_collision_filter']
        if type(override) is not bool:
            raise PresetError('parameters.override_collision_filter 必须是布尔值')
        result.update(override_collision_filter=override,
                      group_mask=_integer(parameters['group_mask'],'parameters.group_mask',1,0x7FFFFFFF),
                      collision_mask=_integer(parameters['collision_mask'],'parameters.collision_mask',0,0x7FFFFFFF))
    return result


def _validate_collider(parameters: Mapping[str, object]) -> dict[str, object]:
    expected = frozenset({"kind", "group_mask", "inside", "a", "b", "radius"})
    if 'collision_mask' in parameters:
        expected |= {'collision_mask'}
    _exact_keys(parameters, expected, "parameters")
    kind = _enum(parameters["kind"], "parameters.kind", _COLLIDER_KINDS)
    point_a = _vec3(parameters["a"], "parameters.a")
    point_b = _vec3(parameters["b"], "parameters.b")
    if kind == "PLANE" and _length_squared(point_b) < 1.0e-12:
        raise PresetError("parameters.b 在 PLANE 碰撞体中是法线，不能是零向量")
    result = {
        "kind": kind,
        "group_mask": _integer(
            parameters["group_mask"], "parameters.group_mask", 1, 0x7FFFFFFF
        ),
        "inside": _boolean(parameters["inside"], "parameters.inside"),
        "a": point_a,
        "b": point_b,
        "radius": _number(parameters["radius"], "parameters.radius", 0.0, 10.0),
    }
    if 'collision_mask' in parameters:
        result['collision_mask'] = _integer(parameters['collision_mask'], 'parameters.collision_mask', 0, 0x7FFFFFFF)
    return result


def _validate_chain_link(parameters: Mapping[str, object]) -> dict[str, object]:
    # Endpoints, strip mode and child collisions are topology, not a parameter preset.
    _exact_keys(parameters, frozenset({"coupling", "max_correction"}), "parameters")
    return {
        "coupling": _number(parameters["coupling"], "parameters.coupling", 0.000001, 1.0),
        "max_correction": _number(parameters["max_correction"], "parameters.max_correction", 0.000001, 10.0),
    }


_PARAMETER_VALIDATORS = {
    ENTITY_PROFILE: _validate_profile,
    ENTITY_NODE: _validate_node,
    ENTITY_COLLIDER: _validate_collider,
    ENTITY_CHAIN_LINK: _validate_chain_link,
}


def validate_parameters(entity_type: object, parameters: object) -> dict[str, object]:
    """校验同类型物理参数，并返回不含调用方可变引用的规范副本。"""

    if not isinstance(entity_type, str) or entity_type not in ENTITY_TYPES:
        choices = "、".join(sorted(ENTITY_TYPES))
        raise PresetError(f"entity_type 必须是以下值之一：{choices}")
    mapping = _mapping(parameters, "parameters")
    return _PARAMETER_VALIDATORS[entity_type](mapping)


def make_preset(entity_type: object, parameters: object) -> dict[str, object]:
    """由受支持参数创建 schema 1 预设文档；身份、绑定和拓扑键会被拒绝。"""

    normalized = validate_parameters(entity_type, parameters)
    return {
        "schema": PRESET_SCHEMA,
        "version": PRESET_VERSION,
        "entity_type": entity_type,
        "units": dict(PRESET_UNITS),
        "parameters": normalized,
    }


def merge_parameters(entity_type, current, incoming, *, node_mode="NUMERIC"):
    """Build a fully validated result before any RNA writes; never silently reset a frame.

    Old presets may omit reference/filter extensions; omitted values remain the target's.
    Incompatible limit-kind/frame combinations fail explicitly instead of losing hand tuning.
    """
    result = validate_parameters(entity_type, current)
    source = validate_parameters(entity_type, incoming)
    if node_mode not in {"NUMERIC", "ALL", "DIRECTION"}:
        raise PresetError("未知节点应用模式")
    if entity_type != ENTITY_NODE or node_mode == "ALL":
        result.update(source)
    else:
        result.update({key: value for key, value in source.items()
                       if (key in NODE_DIRECTION_FIELDS) == (node_mode == "DIRECTION")})
    if (entity_type == ENTITY_NODE and node_mode in {'ALL', 'DIRECTION'}
            and 'reference_mode' in source and 'limit_frame' not in source
            and 'limit_frame_valid' in result):
        # A legacy direction preset must not be shadowed by the target's newer frame.
        result['limit_frame_valid'] = False
    try:
        return validate_parameters(entity_type, result)
    except PresetError as exc:
        raise PresetError("参数与目标保留的限制类型／方向不兼容；请核对节点或明确选择包含方向：" + str(exc)) from exc


def validate_preset(
    document: object, *, expected_entity_type: str | None = None
) -> dict[str, object]:
    """完整校验预设文档，失败时不返回任何可应用的部分参数。"""

    top = _mapping(document, "预设顶层")
    _exact_keys(top, _TOP_LEVEL_KEYS, "预设顶层")
    if top["schema"] != PRESET_SCHEMA:
        raise PresetError(f"schema 必须是 {PRESET_SCHEMA}")
    version = top["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise PresetError("version 必须是整数")
    if version != PRESET_VERSION:
        if version > PRESET_VERSION:
            raise PresetError(
                f"预设版本 {version} 高于当前支持的版本 {PRESET_VERSION}，请升级插件"
            )
        raise PresetError(
            f"预设版本 {version} 不受支持；当前只支持版本 {PRESET_VERSION}"
        )
    entity_type = top["entity_type"]
    if not isinstance(entity_type, str) or entity_type not in ENTITY_TYPES:
        raise PresetError("entity_type 只能是 PROFILE、NODE、COLLIDER 或 CHAIN_LINK")
    if expected_entity_type is not None:
        if expected_entity_type not in ENTITY_TYPES:
            raise PresetError("expected_entity_type 只能是 PROFILE、NODE、COLLIDER 或 CHAIN_LINK")
        if entity_type != expected_entity_type:
            raise PresetError(
                f"预设类型是 {entity_type}，不能应用到 {expected_entity_type}"
            )
    units = _mapping(top["units"], "units")
    _exact_keys(units, frozenset(PRESET_UNITS), "units")
    if dict(units) != PRESET_UNITS:
        raise PresetError(
            "units 必须固定为 angle=radian、distance=model_unit、time=second"
        )
    return make_preset(entity_type, top["parameters"])


def serialize_preset(document: object) -> bytes:
    """生成确定性的 UTF-8 JSON；同一逻辑输入始终产生相同字节。"""

    normalized = validate_preset(document)
    text = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    )
    return (text + "\n").encode("utf-8")


def _reject_json_constant(value: str) -> object:
    raise PresetError(f"JSON 不允许非有限数字：{value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PresetError(f"JSON 对象包含重复字段：{key}")
        result[key] = value
    return result


def deserialize_preset(
    blob: bytes | bytearray | memoryview | str,
    *,
    expected_entity_type: str | None = None,
) -> dict[str, object]:
    """严格解析 JSON 字节或文本；坏 JSON、重复键和非有限数全部拒绝。"""

    if isinstance(blob, str):
        text = blob
        byte_length = len(blob.encode("utf-8"))
    elif isinstance(blob, (bytes, bytearray, memoryview)):
        raw = bytes(blob)
        byte_length = len(raw)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PresetError("预设文件不是有效的 UTF-8 文本") from exc
    else:
        raise PresetError("预设内容必须是 UTF-8 字节或字符串")
    if byte_length > MAX_PRESET_BYTES:
        raise PresetError(f"预设文件不能超过 {MAX_PRESET_BYTES} 字节")
    try:
        document = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except PresetError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PresetError(f"预设 JSON 无法解析：{exc}") from exc
    return validate_preset(document, expected_entity_type=expected_entity_type)


def normalize_preset_filename(name: object) -> str:
    """把用户显示名称转换成专属文件名，并拒绝路径穿越和设备名。"""

    if not isinstance(name, str):
        raise PresetError("预设名称必须是字符串")
    value = unicodedata.normalize("NFC", name).strip()
    if value.casefold().endswith(PRESET_SUFFIX.casefold()):
        value = value[: -len(PRESET_SUFFIX)].rstrip()
    if not value:
        raise PresetError("请输入预设名称")
    if value in {".", ".."} or value.startswith("."):
        raise PresetError("预设名称不能以句点开头")
    if value.endswith((".", " ")):
        raise PresetError("预设名称不能以句点或空格结尾")
    if any(ord(character) < 32 for character in value):
        raise PresetError("预设名称不能包含控制字符")
    if any(character in value for character in '<>:"/\\|?*'):
        raise PresetError("预设名称不能包含 Windows 文件名保留字符")
    device_stem = value.split(".", 1)[0].rstrip(" .").upper()
    if device_stem in _RESERVED_WINDOWS_NAMES:
        raise PresetError("该预设名称是 Windows 保留设备名")
    if len(value.encode("utf-8")) > 180:
        raise PresetError("预设名称的 UTF-8 长度不能超过 180 字节")
    return value + PRESET_SUFFIX


def resolve_preset_path(directory: Path | str, name: object) -> Path:
    """在指定目录内解析一个精确预设路径；绝不接受调用方提供的子目录。"""

    root = Path(directory).expanduser().resolve(strict=False)
    if root.exists() and not root.is_dir():
        raise PresetError("预设目录不是文件夹")
    filename = normalize_preset_filename(name)
    target = (root / filename).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PresetError("预设路径必须位于指定的预设目录内") from exc
    return target


def typed_preset_directory(directory: Path | str, entity_type: str) -> Path:
    """用户库仅使用固定类型子目录；不读取旧平铺预设或允许链接逃逸。"""
    if entity_type not in ENTITY_TYPES:
        raise PresetError("预设目录类型只能是 PROFILE、NODE、COLLIDER 或 CHAIN_LINK")
    root = Path(directory).expanduser().resolve(strict=False)
    target = (root / entity_type).resolve(strict=False)
    if target != root / entity_type:
        raise PresetError("类型子目录不能通过链接离开用户预设目录")
    if target.exists() and not target.is_dir():
        raise PresetError("预设类型目录不是文件夹")
    return target


def import_preset(directory: Path | str, source: Path | str, *, name: str = "",
                  overwrite: bool = False) -> Path:
    """严格读取分享 JSON，再按文档类型规范化保存；默认禁止覆盖。"""
    source = Path(source)
    document = _read_resolved_preset(source)
    if not name.strip():
        name = source.name
        if not name.casefold().endswith(PRESET_SUFFIX.casefold()):
            name = source.stem
    target_dir = typed_preset_directory(directory, document["entity_type"])
    target = resolve_preset_path(target_dir, name)
    if target.exists() and not overwrite:
        raise PresetError(f"预设已存在：{target.name}；请改名或明确确认覆盖")
    return save_preset(target_dir, name, document["entity_type"], document["parameters"])


def save_preset(
    directory: Path | str,
    name: object,
    entity_type: object,
    parameters: object,
) -> Path:
    """原子保存一个完整预设；校验失败或替换失败都不会留下部分主文件。"""

    target = resolve_preset_path(directory, name)
    document = make_preset(entity_type, parameters)
    write_atomic(target, serialize_preset(document))
    return target


def read_preset(
    directory: Path | str,
    name: object,
    *,
    expected_entity_type: str | None = None,
) -> dict[str, object]:
    """从受限目录读取一个预设并完整校验后返回规范文档。"""

    target = resolve_preset_path(directory, name)
    return _read_resolved_preset(target, expected_entity_type=expected_entity_type)


def _read_resolved_preset(
    target: Path, *, expected_entity_type: str | None = None
) -> dict[str, object]:
    """读取已经完成目录边界检查的路径。"""

    if not target.is_file():
        raise PresetError(f"预设文件不存在：{target.name}")
    try:
        size = target.stat().st_size
        if size > MAX_PRESET_BYTES:
            raise PresetError(f"预设文件不能超过 {MAX_PRESET_BYTES} 字节")
        blob = target.read_bytes()
    except PresetError:
        raise
    except OSError as exc:
        raise PresetError(f"无法读取预设文件 {target.name}：{exc}") from exc
    return deserialize_preset(blob, expected_entity_type=expected_entity_type)


def list_presets(
    directory: Path | str, *, entity_type: str | None = None
) -> tuple[PresetEntry, ...]:
    """列出目录内全部有效专属预设；任一候选损坏时整体拒绝，避免静默忽略。"""

    if entity_type is not None and entity_type not in ENTITY_TYPES:
        raise PresetError("entity_type 只能是 PROFILE、NODE、COLLIDER 或 CHAIN_LINK")
    root = Path(directory).expanduser().resolve(strict=False)
    if not root.exists():
        return ()
    if not root.is_dir():
        raise PresetError("预设目录不是文件夹")
    candidates = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_file() and path.name.casefold().endswith(PRESET_SUFFIX.casefold())
        ),
        key=lambda path: path.name.casefold(),
    )
    entries = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise PresetError(f"预设文件不能通过链接离开预设目录：{candidate.name}") from exc
        document = _read_resolved_preset(resolved)
        current_type = str(document["entity_type"])
        if entity_type is None or current_type == entity_type:
            entries.append(
                PresetEntry(
                    name=candidate.name[: -len(PRESET_SUFFIX)],
                    entity_type=current_type,
                    path=resolved,
                )
            )
    return tuple(entries)


def delete_preset(directory: Path | str, name: object) -> Path:
    """删除解析后的精确预设文件；是否确认删除由 Blender Operator/UI 负责。"""

    target = resolve_preset_path(directory, name)
    if not target.is_file():
        raise PresetError(f"预设文件不存在：{target.name}")
    try:
        target.unlink()
    except OSError as exc:
        raise PresetError(f"无法删除预设文件 {target.name}：{exc}") from exc
    return target


__all__ = (
    "ENTITY_CHAIN_LINK",
    "ENTITY_COLLIDER",
    "ENTITY_NODE",
    "ENTITY_PROFILE",
    "ENTITY_TYPES",
    "MAX_PRESET_BYTES",
    "PRESET_SCHEMA",
    "PRESET_SUFFIX",
    "PRESET_UNITS",
    "PRESET_VERSION",
    "PresetEntry",
    "PresetError",
    "delete_preset",
    "deserialize_preset",
    "list_presets",
    "import_preset",
    "typed_preset_directory",
    "make_preset",
    "merge_parameters",
    "normalize_preset_filename",
    "read_preset",
    "resolve_preset_path",
    "save_preset",
    "serialize_preset",
    "validate_parameters",
    "validate_preset",
)
