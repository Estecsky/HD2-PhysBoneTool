"""Bounded, read-only smoke test using the production native solver."""
import math
import time

from .native_preview import NativePreview
from .contact_feasibility import rest_contact_conflicts

IDENTITY = (1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0.)


def check_document(document, *, cache_root=None, library_path=None,
                   preview_factory=NativePreview, frames=120, time_budget=8.):
    """Rest and gentle root motion; no contact/limit modifications or subsetting.

    A successful smoke test is not a proof for every animation or export rig.
    Capacity/time/library failures must never masquerade as a dynamics pass.
    """
    result = dict(status='INCOMPLETE', cases=[], reason='', frames_per_case=frames)
    if len(document['bones']) > 256:
        result['reason'] = f"当前完整项目需 {len(document['bones'])} 骨，超过同求解器单次 256 骨容量；未完成动力学检查，不能据此判断物理正常"
        return result
    start = time.monotonic()
    try:
        result['rest_conflicts'] = rest_contact_conflicts(document)
        with preview_factory(document, library_path=library_path, cache_root=cache_root) as native:
            for moving in (False, True):
                if moving:
                    native.reset()
                tracing = bool(getattr(native, 'enable_diagnostics', lambda *_: False)(True))
                row = dict(name='小幅运动' if moving else '静止姿态', frames=0,
                           fallback_frames=0, partial_fallback_frames=0, rejected_steps=0,
                           first_failure=None, messages=[], diagnostics=None)
                result['cases'].append(row)
                previous_rejected = 0
                for frame in range(1, frames + 1):
                    if time.monotonic() - start > time_budget:
                        result['reason'] = '检查达到时间预算；未完成的姿态不能标记通过'
                        return result
                    seconds = frame / 60.
                    root = list(IDENTITY)
                    if moving:
                        root[3] = .03 * math.sin(seconds * 2. * math.pi)
                    # Native ABI consumes model-space matrices and a separate
                    # instance root, matching the existing motion preview.
                    _, status = native.step(root, native.rest, seconds)
                    rejected = native.counters[2]
                    # Status 2 without rejection/message can be an intentional
                    # reset fallback; it is not proof of a convergence failure.
                    failed = rejected > previous_rejected or bool(native.message) or status == 3 or (status == 2 and frame > 1)
                    previous_rejected = rejected
                    row['frames'] = frame
                    row['rejected_steps'] = rejected
                    if failed:
                        result['status'] = 'FAILED'
                        if tracing:
                            try:
                                row['diagnostics'] = native.diagnostics()
                            except Exception as exc:
                                row['diagnostic_error'] = str(exc)
                            native.enable_diagnostics(False)
                            tracing = False
                        row['fallback_frames'] += 1
                        row['partial_fallback_frames'] += int(status == 3)
                        row['first_failure'] = row['first_failure'] or frame
                        message = native.message or '物理求解退回动画'
                        if message not in row['messages']:
                            row['messages'].append(message)
            if result['status'] != 'FAILED':
                if result.get('rest_conflicts'):
                    result['status'] = 'INCOMPLETE'
                    result['reason'] = 'Rest 几何证明与原生短测结果不一致，需核对导出及求解器组件；不能标记通过'
                else:
                    result['status'] = 'PASS'
    except Exception as exc:
        result['reason'] = str(exc) or type(exc).__name__
        # A known failing frame remains a failure even if later probing stops.
    return result


def summary_lines(result):
    if result['status'] == 'FAILED':
        lines = ['动力学检查发现求解失败／动画回退，不能视为物理正常。']
        if any('did not converge' in msg for row in result['cases'] for msg in row['messages']):
            if any(row.get('partial_fallback_frames', 0) for row in result['cases']):
                lines.append('存在局部约束不收敛：相关链组退回动画，其他通过校验的链保留物理。')
            else:
                lines.append('全局约束不收敛；游戏可能退回原动画，看起来完全不动。')
        for row in result['cases']:
            if row['fallback_frames']:
                lines.append(f"{row['name']}：{row['fallback_frames']}/{row['frames']} 帧异常，首次第 {row['first_failure']} 帧。")
                if row.get('partial_fallback_frames', 0):
                    lines.append(f"  其中 {row['partial_fallback_frames']} 帧为局部回退。")
        fallback_names = sorted({name for row in result['cases']
                                 for name in (row.get('diagnostics') or {}).get('fallback_chains', [])})
        if fallback_names:
            lines.append('首次异常回退链组：' + '、'.join(fallback_names))
        seen = set()
        for row in result['cases']:
            for location in (row.get('diagnostics') or {}).get('locations', []):
                key = (location['kind'], location['where'])
                if key in seen:continue
                seen.add(key)
                lines.append(f"{location['kind']}：{location['where']}")
                lines.append(f"  {row['name']}首次异常末次投影残差 {location['residual_m']*1000:.2f} mm")
        if seen:
            lines.append('以上是失败帧剩余冲突位置，不代表唯一根因；未自动改参数。')
        else:
            lines.append('本次未取得具体冲突位置；检查详情中的组件版本／诊断错误，不能凭空定位。')
    elif result['status'] == 'PASS':
        lines = [f"短时动力学检查通过：静止／小幅运动各 {result['frames_per_case']} 帧，无求解回退。",
                 '只覆盖本次 Rest 与测试运动；不等于所有动画或最终游戏验证通过。']
    else:
        lines = ['结构检查通过，但动力学检查未完成；不能判断物理正常。']
    if result['reason']:
        lines.append(result['reason'])
    for conflict in result.get('rest_conflicts', []):
        lines.append(f"Rest 制作约束确定冲突：{conflict['chain']} / {conflict['joint']} ↔ {conflict['collider']}")
        lines.append(f"  可达距离上界 {conflict['maximum_distance_bound_m']*1000:.2f} mm，碰撞所需 {conflict['required_distance_m']*1000:.2f} mm；至少重叠 {conflict['minimum_penetration_m']*1000:.2f} mm。")
    if result.get('rest_conflicts'):
        lines.append('这是当前 Rest 的保守几何证明，不是迭代次数不足；请检查限制参考／允许角度和碰撞体制作参数。未自动改骨骼、半径或碰撞开关；其他未列出的链仍需动力学检查。')
    lines.append('修改物理后请重新检查。')
    return lines
