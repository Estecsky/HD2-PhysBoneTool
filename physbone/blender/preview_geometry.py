"""HD2 物理骨 实体预览的无 ``bpy`` 几何计算层。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Iterable

from ..core import math as hd2_math


Vec3 = tuple[float, float, float]
Edge = tuple[int, int]
Triangle = tuple[int, int, int]

_EPSILON = 1.0e-6
_PI = math.pi


class GeometryError(ValueError):
    """输入不能安全地转换为 HD2 预览几何。"""


@dataclass(frozen=True, slots=True)
class MeshPayload:
    """供 Curve spline 与 GN Fill Curve 消费的不可变三角表面采样。"""

    vertices: tuple[Vec3, ...] = ()
    edges: tuple[Edge, ...] = ()
    faces: tuple[Triangle, ...] = ()

    def __post_init__(self) -> None:
        # 即使测试或未来调用方直接传入 list，也不能把可变容器留在 frozen payload 内。
        object.__setattr__(
            self,
            "vertices",
            tuple(
                tuple(float(component) for component in vertex)
                for vertex in self.vertices
            ),
        )
        object.__setattr__(
            self,
            "edges",
            tuple(tuple(int(index) for index in edge) for edge in self.edges),
        )
        object.__setattr__(
            self,
            "faces",
            tuple(tuple(int(index) for index in face) for face in self.faces),
        )

    @property
    def triangles(self) -> tuple[Triangle, ...]:
        """强调所有 face 都是三角面的语义别名。"""

        return self.faces

    @property
    def is_empty(self) -> bool:
        return not self.vertices and not self.edges and not self.faces


@dataclass(frozen=True, slots=True)
class CurvePayload:
    """一条按给定顺序连接的不可变 POLY spline。"""

    points: tuple[Vec3, ...] = ()
    cyclic: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "points",
            tuple(tuple(float(component) for component in point) for point in self.points),
        )
        object.__setattr__(self, "cyclic", bool(self.cyclic))

    @property
    def is_empty(self) -> bool:
        return not self.points


EMPTY_MESH = MeshPayload()
EMPTY_CURVE = CurvePayload()


def _finite_scalar(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeometryError(f"{label} 必须是有限数") from exc
    if not math.isfinite(result):
        raise GeometryError(f"{label} 必须是有限数")
    return result


def _positive_scalar(value, label: str) -> float:
    result = _finite_scalar(value, label)
    if result <= _EPSILON:
        raise GeometryError(f"{label} 必须大于 {_EPSILON:g}")
    return result


def _finite_vec3(value: Iterable[float], label: str) -> Vec3:
    try:
        values = tuple(value)
    except TypeError as exc:
        raise GeometryError(f"{label} 必须恰好包含三个有限分量") from exc
    if len(values) != 3:
        raise GeometryError(f"{label} 必须恰好包含三个有限分量")
    result = tuple(
        _finite_scalar(component, f"{label}[{index}]")
        for index, component in enumerate(values)
    )
    return result  # type: ignore[return-value]


def _strict_unit(value: Iterable[float], label: str) -> Vec3:
    vector = _finite_vec3(value, label)
    magnitude = hd2_math.length(vector)
    if not math.isfinite(magnitude) or magnitude <= _EPSILON:
        raise GeometryError(f"{label} 不能为零长度向量")
    return hd2_math.multiply(vector, 1.0 / magnitude)


def _count(value, label: str, minimum: int) -> int:
    if isinstance(value, bool):
        raise GeometryError(f"{label} 必须是大于等于 {minimum} 的整数")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeometryError(f"{label} 必须是大于等于 {minimum} 的整数") from exc
    try:
        exact = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeometryError(f"{label} 必须是大于等于 {minimum} 的整数") from exc
    if not math.isfinite(exact) or exact != result or result < minimum:
        raise GeometryError(f"{label} 必须是大于等于 {minimum} 的整数")
    return result


def _angle(value, label: str) -> float:
    result = _finite_scalar(value, label)
    if result < 0.0 or result > _PI:
        raise GeometryError(f"{label} 必须位于 0 到 π 弧度之间")
    return result


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return hd2_math.subtract(a, b)


def _mul(value: Vec3, scalar: float) -> Vec3:
    return hd2_math.multiply(value, scalar)


def _distance(a: Vec3, b: Vec3) -> float:
    return hd2_math.length(_sub(a, b))


def _edges_from_triangles(
    triangles: Iterable[Triangle],
    loose_edges: Iterable[Edge] = (),
) -> tuple[Edge, ...]:
    result: list[Edge] = []
    seen: set[Edge] = set()

    def append(first: int, second: int) -> None:
        if first == second:
            return
        edge = (first, second) if first < second else (second, first)
        if edge not in seen:
            seen.add(edge)
            result.append(edge)

    for triangle in triangles:
        append(triangle[0], triangle[1])
        append(triangle[1], triangle[2])
        append(triangle[2], triangle[0])
    for first, second in loose_edges:
        append(first, second)
    return tuple(result)


def _payload(
    vertices: Iterable[Vec3],
    triangles: Iterable[Triangle],
    loose_edges: Iterable[Edge] = (),
) -> MeshPayload:
    frozen_vertices = tuple(_finite_vec3(vertex, "预览顶点") for vertex in vertices)
    frozen_triangles = tuple(tuple(int(index) for index in face) for face in triangles)
    vertex_count = len(frozen_vertices)
    for face in frozen_triangles:
        if len(face) != 3 or len(set(face)) != 3:
            raise GeometryError("预览三角面必须引用三个不同顶点")
        if min(face) < 0 or max(face) >= vertex_count:
            raise GeometryError("预览三角面引用了范围外顶点")
    frozen_edges = _edges_from_triangles(frozen_triangles, loose_edges)
    for first, second in frozen_edges:
        if first < 0 or second >= vertex_count:
            raise GeometryError("预览边引用了范围外顶点")
    return MeshPayload(
        vertices=frozen_vertices,
        edges=frozen_edges,
        faces=frozen_triangles,  # type: ignore[arg-type]
    )


def _strict_limit_inputs(
    kind: str,
    desired: Iterable[float],
    reference_axis: Iterable[float] | None,
) -> tuple[Vec3, Vec3 | None]:
    desired_unit = _strict_unit(desired, "角限中心方向")
    if kind == "CONE":
        return desired_unit, None
    if reference_axis is None:
        raise GeometryError(f"{kind} 角限必须提供非零参考轴")
    reference_unit = _strict_unit(reference_axis, "角限参考轴")
    if kind == "HINGE":
        projected = _sub(
            desired_unit,
            _mul(reference_unit, hd2_math.dot(reference_unit, desired_unit)),
        )
        if hd2_math.length(projected) <= _EPSILON:
            raise GeometryError("铰链参考轴与中心方向平行，无法形成运动平面")
    elif kind == "SPHERICAL":
        projected = _sub(
            reference_unit,
            _mul(desired_unit, hd2_math.dot(reference_unit, desired_unit)),
        )
        if hd2_math.length(projected) <= _EPSILON:
            raise GeometryError("球面参考轴与中心方向平行，无法形成主切向轴")
    return desired_unit, reference_unit


def make_limit(
    kind,
    desired: Iterable[float],
    reference_axis: Iterable[float] | None,
    primary_angle,
    secondary_angle=None,
    *,
    length=1.0,
    steps: int = 32,
) -> MeshPayload:
    """生成 HD2 Cone/Hinge/Spherical 的精确边界三角扇面。

    ``desired``、``reference_axis`` 和 ``length`` 应已处于角限 preview 的局部坐标。
    ``NONE`` 返回同一个空 payload。Spherical 两个半角相等时使用 Cone 的采样相位，
    从而不仅连续角域等价，离散预览网格也逐项等价。
    """

    kind = str(kind).upper()
    if kind == "NONE":
        return EMPTY_MESH
    if kind not in {"CONE", "HINGE", "SPHERICAL"}:
        raise GeometryError(f"未知 HD2 角限类型：{kind}")
    steps = _count(steps, "角限采样步数", 3)
    length = _positive_scalar(length, "角限显示长度")
    primary_angle = _angle(primary_angle, "主限制半角")
    secondary_angle = (
        primary_angle
        if secondary_angle is None
        else _angle(secondary_angle, "第二限制半角")
    )
    desired_unit, reference_unit = _strict_limit_inputs(
        kind,
        desired,
        reference_axis,
    )

    boundary_kind = kind
    boundary_reference = reference_unit
    if kind == "SPHERICAL" and abs(primary_angle - secondary_angle) <= _EPSILON:
        boundary_kind = "CONE"
        boundary_reference = None
    directions = hd2_math.limit_boundary_directions(
        boundary_kind,
        desired_unit,
        boundary_reference,
        primary_angle,
        secondary_angle,
        steps=steps,
    )
    for index, direction in enumerate(directions):
        direction = _finite_vec3(direction, f"角限边界方向[{index}]")
        if abs(hd2_math.length(direction) - 1.0) > 1.0e-5:
            raise GeometryError("角限数学返回了非单位边界方向")

    vertices: list[Vec3] = [(0.0, 0.0, 0.0)]
    vertices.extend(_mul(direction, length) for direction in directions)
    triangles: list[Triangle] = []
    if kind == "HINGE":
        triangles.extend(
            (0, index + 1, index + 2)
            for index in range(len(directions) - 1)
        )
    else:
        triangles.extend(
            (0, index + 1, ((index + 1) % len(directions)) + 1)
            for index in range(len(directions))
        )
    # 0°/180° 是合法 canonical 值，会把多个采样点压到同一精确方向。
    # 不人为增厚或偏移它们，否则显示会越过 runtime 边界。
    return _payload(vertices, triangles)


def make_chain_curve(points: Iterable[Iterable[float]]) -> CurvePayload:
    """生成按 Chain node 顺序连接的非闭合曲线点。"""

    try:
        frozen = tuple(
            _finite_vec3(point, f"骨链曲线点[{index}]")
            for index, point in enumerate(points)
        )
    except TypeError as exc:
        raise GeometryError("骨链曲线点必须是可迭代序列") from exc
    if len(frozen) < 2:
        raise GeometryError("骨链曲线至少需要两个节点")
    for index, (first, second) in enumerate(zip(frozen, frozen[1:])):
        if _distance(first, second) <= _EPSILON:
            raise GeometryError(f"骨链曲线第 {index + 1} 段为零长度")
    return CurvePayload(points=frozen, cyclic=False)


__all__ = (
    "Vec3",
    "Edge",
    "Triangle",
    "GeometryError",
    "MeshPayload",
    "CurvePayload",
    "EMPTY_MESH",
    "EMPTY_CURVE",
    "make_limit",
    "make_chain_curve",
)
