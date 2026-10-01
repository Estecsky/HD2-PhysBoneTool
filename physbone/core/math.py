"""HD2PHY1 角限预览使用的无 ``bpy`` 数学。

这里的公式逐项对应当前 C++ consumer 的 cone/hinge/spherical 投影边界。
模块只返回单位方向，不读取 Blender controller、preview 或对象变换。
"""

from __future__ import annotations

import math as _math
from collections.abc import Iterable


_EPSILON = 1.0e-6
_PI = _math.pi


def vec3(value: Iterable[float]) -> tuple[float, float, float]:
    values = tuple(float(component) for component in value)
    if len(values) != 3:
        raise ValueError("HD2 角限向量必须恰好包含三个分量")
    return values


def add(a, b):
    a = vec3(a)
    b = vec3(b)
    return tuple(a[index] + b[index] for index in range(3))


def subtract(a, b):
    a = vec3(a)
    b = vec3(b)
    return tuple(a[index] - b[index] for index in range(3))


def multiply(value, scalar):
    value = vec3(value)
    scalar = float(scalar)
    return tuple(component * scalar for component in value)


def dot(a, b):
    a = vec3(a)
    b = vec3(b)
    return sum(a[index] * b[index] for index in range(3))


def cross(a, b):
    ax, ay, az = vec3(a)
    bx, by, bz = vec3(b)
    return (
        ay * bz - az * by,
        az * bx - ax * bz,
        ax * by - ay * bx,
    )


def length(value):
    value = vec3(value)
    return _math.sqrt(sum(component * component for component in value))


def normalize(value, fallback=(0.0, 1.0, 0.0)):
    value = vec3(value)
    magnitude = length(value)
    if magnitude >= _EPSILON and _math.isfinite(magnitude):
        return tuple(component / magnitude for component in value)
    fallback = vec3(fallback)
    fallback_length = length(fallback)
    if fallback_length < _EPSILON or not _math.isfinite(fallback_length):
        return (0.0, 1.0, 0.0)
    return tuple(component / fallback_length for component in fallback)


def orthogonal(value):
    """使用与 C++ consumer 相同的稳定正交回退选择。"""

    value = normalize(value)
    axis = (1.0, 0.0, 0.0) if abs(value[0]) < 0.8 else (0.0, 1.0, 0.0)
    return normalize(cross(value, axis), (0.0, 0.0, 1.0))


def rotate_axis(value, axis, angle):
    value = vec3(value)
    axis = normalize(axis, (1.0, 0.0, 0.0))
    angle = float(angle)
    cosine = _math.cos(angle)
    sine = _math.sin(angle)
    first = multiply(value, cosine)
    second = multiply(cross(axis, value), sine)
    third = multiply(axis, dot(axis, value) * (1.0 - cosine))
    return add(add(first, second), third)


def spherical_allowed_angle(radial_x, radial_y, primary_angle, secondary_angle):
    """返回 C++ ``spherical_limit`` 对给定切平面方向允许的最大半角。"""

    radial_x = float(radial_x)
    radial_y = float(radial_y)
    primary_angle = float(primary_angle)
    secondary_angle = float(secondary_angle)
    denominator_squared = 0.0
    if abs(radial_x) > _EPSILON:
        if primary_angle <= _EPSILON:
            return 0.0
        denominator_squared += (radial_x * radial_x) / (
            primary_angle * primary_angle
        )
    if abs(radial_y) > _EPSILON:
        if secondary_angle <= _EPSILON:
            return 0.0
        denominator_squared += (radial_y * radial_y) / (
            secondary_angle * secondary_angle
        )
    if denominator_squared <= 0.0:
        return _PI
    return min(_PI, 1.0 / _math.sqrt(denominator_squared))


def _tangent_basis(desired, reference_axis=None):
    desired = normalize(desired)
    if reference_axis is None:
        tangent_x = orthogonal(desired)
    else:
        reference_axis = vec3(reference_axis)
        tangent_x = subtract(reference_axis, multiply(desired, dot(reference_axis, desired)))
        tangent_x = normalize(tangent_x, orthogonal(desired))
    tangent_y = normalize(cross(desired, tangent_x), orthogonal(desired))
    return desired, tangent_x, tangent_y


def limit_boundary_directions(
    kind,
    desired,
    reference_axis,
    primary_angle,
    secondary_angle=None,
    *,
    steps=32,
):
    """生成 HD2 limit 的单位边界方向。

    ``CONE``/``SPHERICAL`` 返回闭合方位环；``HINGE`` 返回从负半角到正半角的
    有序开弧。``NONE`` 返回空元组。
    """

    kind = str(kind).upper()
    steps = int(steps)
    if steps < 3:
        raise ValueError("HD2 角限预览至少需要三个采样步")
    primary_angle = max(0.0, min(_PI, float(primary_angle)))
    secondary_angle = (
        primary_angle
        if secondary_angle is None
        else max(0.0, min(_PI, float(secondary_angle)))
    )
    if kind == "NONE":
        return ()
    if kind == "HINGE":
        normal = normalize(reference_axis, (1.0, 0.0, 0.0))
        desired = vec3(desired)
        center = subtract(desired, multiply(normal, dot(normal, desired)))
        center = normalize(center, orthogonal(normal))
        return tuple(
            normalize(
                rotate_axis(
                    center,
                    normal,
                    -primary_angle + (2.0 * primary_angle * index / steps),
                ),
                center,
            )
            for index in range(steps + 1)
        )

    desired, tangent_x, tangent_y = _tangent_basis(
        desired,
        reference_axis if kind == "SPHERICAL" else None,
    )
    directions = []
    for index in range(steps):
        azimuth = 2.0 * _PI * index / steps
        radial_x = _math.cos(azimuth)
        radial_y = _math.sin(azimuth)
        radial = add(multiply(tangent_x, radial_x), multiply(tangent_y, radial_y))
        if kind == "CONE":
            allowed = primary_angle
        elif kind == "SPHERICAL":
            allowed = spherical_allowed_angle(
                radial_x,
                radial_y,
                primary_angle,
                secondary_angle,
            )
        else:
            raise ValueError(f"未知 HD2 角限类型：{kind}")
        direction = add(
            multiply(desired, _math.cos(allowed)),
            multiply(radial, _math.sin(allowed)),
        )
        directions.append(normalize(direction, desired))
    return tuple(directions)


__all__ = (
    "add",
    "cross",
    "dot",
    "length",
    "limit_boundary_directions",
    "multiply",
    "normalize",
    "orthogonal",
    "rotate_axis",
    "spherical_allowed_angle",
    "subtract",
    "vec3",
)
