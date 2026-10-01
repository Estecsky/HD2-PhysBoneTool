"""扩展 HD2PHY1 角限参考方向的无 ``bpy`` 数学合同。

所有方向都位于当前节点父骨的局部坐标系，且方向语义固定为
``parent -> current`` 的入段。四元数使用 ``(x, y, z, w)`` 顺序，采用右手系，
主动旋转列向量；其前向轴固定为 ``+Y``。本模块不读取 Blender 预览对象，供
serializer、未来 solver 和离线 fixture 共用。
"""

from __future__ import annotations

from enum import IntEnum
import math
from collections.abc import Iterable


EPSILON = 1.0e-6
DEFAULT_FORWARD = (0.0, 1.0, 0.0)
IDENTITY_QUATERNION = (0.0, 0.0, 0.0, 1.0)


class ReferenceMode(IntEnum):
    """HD2PHY1 的参考方向来源"""

    ANIMATED_SEGMENT = 0
    PARENT_REST = 1
    EXPLICIT_LOCAL = 2
    EXPLICIT_FRAME = 3


REFERENCE_MODE_NAMES = {mode.name: int(mode) for mode in ReferenceMode}
REFERENCE_MODE_VALUES = {int(mode): mode.name for mode in ReferenceMode}


class ReferenceError(ValueError):
    """参考方向不满足 HD2PHY1 合同。"""


def _values(value: Iterable[float], count: int, where: str) -> tuple[float, ...]:
    try:
        values = tuple(float(component) for component in value)
    except (TypeError, ValueError) as exc:
        raise ReferenceError(f"{where}: 必须是有限数字序列") from exc
    if len(values) != count or not all(math.isfinite(component) for component in values):
        raise ReferenceError(f"{where}: 必须包含 {count} 个有限数字")
    return values


def normalize(value: Iterable[float], where: str = "方向") -> tuple[float, float, float]:
    values = _values(value, 3, where)
    magnitude = math.sqrt(sum(component * component for component in values))
    if not math.isfinite(magnitude) or magnitude < EPSILON:
        raise ReferenceError(f"{where}: 长度必须大于 {EPSILON}")
    return tuple(component / magnitude for component in values)


def normalize_quaternion(
    value: Iterable[float], where: str = "参考 frame"
) -> tuple[float, float, float, float]:
    values = _values(value, 4, where)
    magnitude = math.sqrt(sum(component * component for component in values))
    if not math.isfinite(magnitude) or magnitude < EPSILON:
        raise ReferenceError(f"{where}: 四元数长度必须大于 {EPSILON}")
    normalized = tuple(component / magnitude for component in values)
    # q 与 -q 表示同一个 frame；固定首个非零分量为正，避免 producer 顺序造成不同 bytes。
    for component in normalized:
        if abs(component) >= EPSILON:
            if component < 0.0:
                return tuple(-value for value in normalized)
            break
    return normalized


def quaternion_multiply(a: Iterable[float], b: Iterable[float]):
    ax, ay, az, aw = normalize_quaternion(a, "四元数 a")
    bx, by, bz, bw = normalize_quaternion(b, "四元数 b")
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quaternion_rotate(quaternion: Iterable[float], vector: Iterable[float]):
    """用右手系主动旋转把父骨局部向量变换到同一 frame 中。"""

    x, y, z, w = normalize_quaternion(quaternion)
    vx, vy, vz = _values(vector, 3, "向量")
    # 等价于 q * (v, 0) * inverse(q)，避免构造可变临时对象。
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + y * tz - z * ty,
        vy + w * ty + z * tx - x * tz,
        vz + w * tz + x * ty - y * tx,
    )


def frame_axes(quaternion: Iterable[float]):
    """返回 frame 的 right(+X)、forward(+Y)、up(+Z) 三个单位轴。"""

    quaternion = normalize_quaternion(quaternion)
    return tuple(
        normalize(quaternion_rotate(quaternion, axis), "frame 轴")
        for axis in ((1.0, 0.0, 0.0), DEFAULT_FORWARD, (0.0, 0.0, 1.0))
    )


def resolve_reference_forward(
    mode: int | str | ReferenceMode,
    *,
    animated_segment: Iterable[float] | None = None,
    parent_rest: Iterable[float] | None = None,
    explicit_local: Iterable[float] | None = None,
    explicit_frame: Iterable[float] | None = None,
) -> tuple[float, float, float]:
    """按冻结语义解析角限中心方向，结果始终在父骨局部空间。"""

    if isinstance(mode, str):
        try:
            mode = ReferenceMode[mode]
        except KeyError as exc:
            raise ReferenceError(f"未知 reference_mode：{mode}") from exc
    try:
        mode = ReferenceMode(int(mode))
    except (TypeError, ValueError) as exc:
        raise ReferenceError(f"未知 reference_mode：{mode}") from exc
    if mode is ReferenceMode.ANIMATED_SEGMENT:
        if animated_segment is None:
            raise ReferenceError("ANIMATED_SEGMENT 缺少当前动画 parent->current 方向")
        return normalize(animated_segment, "动画入段方向")
    if mode is ReferenceMode.PARENT_REST:
        if parent_rest is None:
            raise ReferenceError("PARENT_REST 缺少父骨 Rest 入段方向")
        return normalize(parent_rest, "父骨 Rest 入段方向")
    if mode is ReferenceMode.EXPLICIT_LOCAL:
        if explicit_local is None:
            raise ReferenceError("EXPLICIT_LOCAL 缺少显式父骨局部方向")
        return normalize(explicit_local, "显式局部方向")
    if explicit_frame is None:
        raise ReferenceError("EXPLICIT_FRAME 缺少参考四元数")
    return normalize(quaternion_rotate(explicit_frame, DEFAULT_FORWARD), "frame 前向轴")


def validate_fields(
    mode: int | str | ReferenceMode,
    direction: Iterable[float],
    frame: Iterable[float],
    *,
    where: str = "reference",
) -> tuple[int, tuple[float, float, float], tuple[float, float, float, float]]:
    """校验并规范化文件中的三个参考字段。"""

    if isinstance(mode, str):
        try:
            mode_value = REFERENCE_MODE_NAMES[mode]
        except KeyError as exc:
            raise ReferenceError(f"{where}.mode：未知 reference_mode") from exc
    else:
        try:
            mode_value = int(ReferenceMode(int(mode)))
        except (TypeError, ValueError) as exc:
            raise ReferenceError(f"{where}.mode：未知 reference_mode") from exc
    normalized_direction = normalize(direction, f"{where}.direction")
    normalized_frame = normalize_quaternion(frame, f"{where}.frame")
    return mode_value, normalized_direction, normalized_frame


__all__ = (
    "DEFAULT_FORWARD",
    "EPSILON",
    "IDENTITY_QUATERNION",
    "REFERENCE_MODE_NAMES",
    "REFERENCE_MODE_VALUES",
    "ReferenceError",
    "ReferenceMode",
    "frame_axes",
    "normalize",
    "normalize_quaternion",
    "quaternion_multiply",
    "quaternion_rotate",
    "resolve_reference_forward",
    "validate_fields",
)
