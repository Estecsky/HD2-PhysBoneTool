"""衣物姿态驱动的纯数值曲线；供制作预览及 SDK 作者合同校验使用。

输入、输出均为角度。MAX/MIN 合并的是同一输出轴的标量需求，而非
旋转平均：MAX 适合两条腿均向同一方向要求衣摆让路的情况。负角度
会原样保留；相反方向的需求应由作者选择合适的曲线或独立驱动。
"""

from __future__ import annotations

import math


class PoseDriverError(ValueError):
    """姿态映射输入无效；调用方应中止预览或导出，不静默遗漏规则。"""


def _finite(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PoseDriverError(f"{label}必须为有限数值") from exc
    if not math.isfinite(result):
        raise PoseDriverError(f"{label}必须为有限数值")
    return result


def _validated_samples(samples):
    try:
        points = []
        for sample in samples:
            x, y = sample
            points.append((_finite(x, "输入角度"), _finite(y, "输出角度")))
    except (TypeError, ValueError) as exc:
        if isinstance(exc, PoseDriverError):
            raise
        raise PoseDriverError("映射点必须为 (输入角度, 输出角度) 数值对") from exc
    if len(points) < 2:
        raise PoseDriverError("姿态映射至少需要两个点")
    points.sort(key=lambda point: point[0])
    if any(a[0] == b[0] for a, b in zip(points, points[1:])):
        raise PoseDriverError("姿态映射不能包含重复的输入角度")
    return points


def _evaluate(points, angle):
    angle = _finite(angle, "目标骨角度")
    if angle <= points[0][0]:
        return points[0][1]
    if angle >= points[-1][0]:
        return points[-1][1]
    for (left_x, left_y), (right_x, right_y) in zip(points, points[1:]):
        if angle <= right_x:
            span = right_x - left_x
            # 极大但有限的输入也不能因差值溢出而悄悄得到错误插值。
            factor = ((angle - left_x) / span if math.isfinite(span) else
                      (angle * 0.5 - left_x * 0.5) /
                      (right_x * 0.5 - left_x * 0.5))
            return _finite((1.0 - factor) * left_y + factor * right_y, "插值结果")
    raise PoseDriverError("无法找到姿态映射区间")


def evaluate_curve(samples, angle):
    """排序后线性插值，范围外夹端点；不修改调用方的映射点。"""
    return _evaluate(_validated_samples(samples), angle)


def combine_outputs(primary, secondary, mode):
    """合并已映射的标量角度；SINGLE 完全忽略第二输入。"""
    primary = _finite(primary, "主输出角度")
    if mode not in ("SINGLE", "MAX", "MIN", "ADD"):
        raise PoseDriverError(f"未知姿态合并模式：{mode}")
    if mode == "SINGLE":
        return primary
    if secondary is None:
        raise PoseDriverError("双骨合并模式需要第二目标骨角度")
    secondary = _finite(secondary, "次输出角度")
    if mode == "MAX":
        return max(primary, secondary)
    if mode == "MIN":
        return min(primary, secondary)
    return _finite(primary + secondary, "合并输出角度")


def evaluate_response(samples, primary_angle, secondary_angle=None,
                      mode="SINGLE", influence=1.0):
    """两目标分别经过同一映射再合并，最后应用 0..1 影响系数。"""
    influence = _finite(influence, "影响系数")
    if not 0.0 <= influence <= 1.0:
        raise PoseDriverError("影响系数必须在 0 到 1 之间")
    points = _validated_samples(samples)
    primary = _evaluate(points, primary_angle)
    secondary = None
    if mode != "SINGLE" and secondary_angle is not None:
        secondary = _evaluate(points, secondary_angle)
    return combine_outputs(primary, secondary, mode) * influence


def _xyz(value, label):
    try:
        x, y, z = value
    except (TypeError, ValueError) as exc:
        raise PoseDriverError(f"{label}必须包含 XYZ 三个角度") from exc
    return tuple(_finite(component, label) for component in (x, y, z))


def evaluate_vector_curve(samples, angle):
    """单个输入轴映射到 XYZ 输出；线性插值并夹端点，不改输入。"""
    try:
        points = []
        for sample in samples:
            angle_in, vector = sample
            points.append((_finite(angle_in, "输入角度"), _xyz(vector, "输出角度")))
    except (TypeError, ValueError) as exc:
        if isinstance(exc, PoseDriverError):
            raise
        raise PoseDriverError("映射点必须为 (输入角度, (X, Y, Z))") from exc
    # 复用标量校验/插值语义；即使查询端点也校验所有点与全部分量。
    return tuple(evaluate_curve([(x, vector[axis]) for x, vector in points], angle)
                 for axis in range(3))


def evaluate_axis_responses(curves, primary, secondary=None, mode="SINGLE"):
    """三个独立输入轴的 XYZ 响应相加；不是跨轴耦合/RBF 模型。

    空轴贡献为零。每轴先分别映射两个源，再逐分量组合；ADD 在这一层
    夹到 ±180 度。三个轴的贡献最后相加，不再次夹角度。零输入处的
    非零输出也会相加，因此通常应将各轴的零输入响应设为零。
    返回的是度数分量，Blender 层负责将其解释为 XYZ Euler 旋转。
    """
    # 空曲线不能掩盖无效模式或缺失的双源输入。
    combine_outputs(0, None if secondary is None else 0, mode)
    primary = _xyz(primary, "主输入角度")
    secondary = _xyz(secondary, "次输入角度") if mode != "SINGLE" else None
    try:
        curve_x, curve_y, curve_z = curves
    except (TypeError, ValueError) as exc:
        raise PoseDriverError("必须提供 X、Y、Z 三个输入轴的曲线") from exc
    total = [0.0, 0.0, 0.0]
    for axis, samples in enumerate((curve_x, curve_y, curve_z)):
        try:
            samples = tuple(samples)
        except TypeError as exc:
            raise PoseDriverError("输入轴曲线必须为采样点序列；空序列表示无贡献") from exc
        if not samples:
            continue
        first = evaluate_vector_curve(samples, primary[axis])
        second = evaluate_vector_curve(samples, secondary[axis]) if secondary is not None else None
        for component in range(3):
            value = combine_outputs(first[component],
                                    second[component] if second is not None else None, mode)
            if mode == "ADD":
                value = max(-180.0, min(180.0, value))
            total[component] = _finite(total[component] + value, "三轴合计输出角度")
    return tuple(total)
