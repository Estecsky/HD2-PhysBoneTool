"""Non-destructive MMD rigid-body graph -> SpringBone *approximation* plan.

This is deliberately not a Bullet/6-DoF serializer. The report accounts for every
input body and constraint; unsupported components never silently become physics.
Coordinates and collider geometry are normalized by the Blender adapter first.
"""
from __future__ import annotations

import math
from collections import defaultdict


class ConversionError(ValueError):
    pass


def collision_mask(excluded):
    if len(excluded) != 16 or any(type(x) is not bool for x in excluded):
        raise ConversionError("MMD 碰撞排除表必须是 16 个布尔值")
    mask = sum(1 << i for i, value in enumerate(excluded) if not value)
    # Wire v1 forbids zero. No converted object uses group 30, so this represents
    # an empty set without enabling any source collision pair.
    return mask or (1 << 30)


def _finite(value, label, minimum=0.0):
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise ConversionError(f"{label} 必须是有限且 >= {minimum} 的数字")
    return value


def _unique(rows, label):
    result = {}
    for row in rows:
        if not row.get("name") or row["name"] in result:
            raise ConversionError(f"{label}名称缺失或重复：{row.get('name')}")
        result[row["name"]] = row
    return result


def plan_conversion(snapshot, *, fixed_dt=1.0 / 60.0, mode="CONTACTS", complete_light=False, body_contacts=True):
    """Return a deterministic editable plan; input is never modified.

    Complete LIGHT can partition direct bone-parent trees into disjoint paths.
    Each dynamic bone has one owner; generated endpoints allow branch roots to
    swing while following their actually solved parent frame. Legacy/contact
    modes still require a non-branching chain with uniform collision policy.
    Cross links/loops are reported, not replaced with unrelated group coupling.
    """
    if mode not in {"LIGHT", "CONTACTS"}:
        raise ConversionError(f"未知 MMD 转换模式：{mode}")
    light = mode == "LIGHT"
    fixed_dt = _finite(fixed_dt, "固定步长", 1.0e-6)
    bones = _unique(snapshot["bones"], "骨骼")
    for bone in bones.values():
        if len(bone["head"]) != 3 or not all(math.isfinite(float(v)) for v in bone["head"]):
            raise ConversionError(f"骨骼位置不是有限三维向量：{bone['name']}")
    rigids = _unique(snapshot["rigids"], "刚体")
    joints = _unique(snapshot["joints"], "约束")
    report = {"schema": "HD2MMDConversionReport1", "lossless": False, "mode": mode,
              "bodies": [], "constraints": [], "approximations": [
                  "弹簧骨不是 MMD 六自由度刚体；不保留质量反作用、摩擦、恢复系数和扭转自由度。",
                  "动态刚体近似为骨节点/骨段碰撞代理；刚体中心偏置及盒形不能精确复现。",
                  "阻尼按每秒速度损失率换算到固定步长；刚度按转动惯量估计响应率，需预览调节。",
                  "MMD 的平移/旋转硬限及横向约束保留在报告中，不伪装成 HD2 ChainLink。",
                  "轻量链固定首动态骨的位置并允许摆动；无末端辅助骨时末骨继承上一骨段摆动。单动态骨仍保留旧式位置近似，不能复现自转。",
              ]}
    if light:
        report["approximations"].extend([
            "轻量模式不生成碰撞体，不启用链间/自身碰撞；可能穿过身体或衣物。",
            "不使用原刚体质量、弹簧和阻尼：采用可编辑的温和弹簧、沿链软化、相对运动阻尼、弱重力和 45° 角限。",
            "不同碰撞组不再阻止父子链转换；原碰撞表仍完整保存在来源报告中。",
        ])
    dynamic = defaultdict(list)
    for body in rigids.values():
        if str(body["mode"]) not in {"0", "1", "2"}:
            raise ConversionError(f"不支持的 MMD 刚体模式：{body['name']}")
        group = body["group"]
        if type(group) is not int or not 0 <= group < 16:
            raise ConversionError(f"无效碰撞组：{body['name']}")
        collision_mask(body["excluded_groups"])
        for key in ("mass", "angular_damping", "linear_damping", "radius"):
            _finite(body[key], f"{body['name']}.{key}")
        if max(body["linear_damping"], body["angular_damping"]) > 1:
            raise ConversionError(f"阻尼不在 0..1：{body['name']}")
        if str(body["mode"]) != "0" and body["bone"] in bones:
            dynamic[body["bone"]].append(body)
    # Reject cycles/missing parents globally; never hang walking malformed input.
    for name in bones:
        seen, current = set(), name
        while current:
            if current in seen or current not in bones:
                raise ConversionError(f"骨架父链循环或缺失：{name}")
            seen.add(current)
            current = bones[current].get("parent")
    children = defaultdict(list)
    for name in dynamic:
        parent = bones[name].get("parent")
        if parent in dynamic:
            children[parent].append(name)
    roots = sorted(name for name in dynamic if bones[name].get("parent") not in dynamic)
    chains, colliders, accepted = [], [], set()
    rejected = {}
    components = []
    for root in roots:
        component, pending = [], [root]
        while pending:
            name = pending.pop()
            component.append(name)
            pending.extend(sorted(children[name], reverse=True))
        if (light and complete_light and bones[root].get('parent')
                and all(len(dynamic[name]) == 1 for name in component)):
            # Stop at the branch point instead of putting that same simulated
            # bone in each child chain. Complete LIGHT adds a unique tail to
            # every path, including a one-bone path, then uses root_swing.
            members = set(component)
            starts = sorted(name for name in component
                            if bones[name].get('parent') not in members
                            or len(children[bones[name]['parent']]) != 1)
            for start in starts:
                path = [start]
                while len(children[path[-1]]) == 1:
                    path.append(children[path[-1]][0])
                components.append((start, path))
        else:
            components.append((root, component))
    for root, component in components:
        reason = None
        driver = bones[root].get("parent")
        if not driver:
            reason = "模拟根没有固定父骨"
        elif any(len(dynamic[name]) != 1 for name in component):
            reason = "同一骨绑定多个动态刚体，不能猜测控制者"
        elif any(len(children[name]) > 1 for name in component) and not (light and complete_light):
            reason = "此模式不支持分叉动态骨链，请使用带末端辅助骨的完整轻量转换"
        elif len(component) > 63:
            reason = "超过单链 63 个模拟节点上限"
        else:
            policies = {(dynamic[name][0]["group"], collision_mask(dynamic[name][0]["excluded_groups"]))
                        for name in component}
            if not light and len(policies) != 1:
                reason = "链内碰撞组/排除表不同，当前链级掩码不能无损表达"
            elif any(math.dist(bones[name]["head"], bones[bones[name]["parent"]]["head"]) < 1.e-5
                     for name in component if not (light and complete_light and name == root)):
                reason = "存在零长度父子段"
        if reason:
            rejected.update((name, reason) for name in component)
            continue
        group, mask = (0, 1 << 30) if light else next(iter(policies))
        nodes = [{"bone": driver, "radius": 0.0, "stiffness_scale": 0.0,
                  "damping_scale": 0.0}]
        for name in component:
            body = dynamic[name][0]
            related = [j for j in joints.values() if body["name"] in (j.get("a"), j.get("b"))]
            spring = max((_finite(k, "角弹簧") for j in related for k in j.get("spring_angular", ())), default=0.0)
            # First-order SpringBone response approximates a rigid's angular response.
            inertia = max(body["mass"] * max(body["radius"], .001) ** 2, 1.e-8)
            response = min(1000.0, math.sqrt(spring / inertia))
            d = max(body["angular_damping"], body["linear_damping"])
            nodes.append({"bone": name, "radius": body["radius"],
                          "stiffness_scale": 1.0 if light else response / 10.0,
                          "damping_scale": 1.0 if light else (1.0 - (1.0 - d) ** fixed_dt) / .1,
                          "limit_kind": "CONE" if light else "NONE",
                          "limit_angle": math.pi / 4})
            accepted.add(body["name"])
        root_swing = light and len(component) >= 2
        if root_swing:
            nodes.pop(0)
            nodes[0].update(radius=0.0, stiffness_scale=0.0, damping_scale=0.0, limit_kind="NONE")
            for index, node in enumerate(nodes[1:], 1):
                progress = (index - 1) / max(1, len(nodes) - 2)
                node["stiffness_scale"] = 1.5 - .75 * progress
                node["damping_scale"] = 1.0 - .15 * progress
        chains.append({"name": root, "stiffness": 12.0 if light else 10.0,
                       "damping": 1.0 - .4 ** (fixed_dt * 60) if light else .1,
                       "gravity": (0, 0, -1.0 if light else -9.81),
                       "group_mask": 1 << group, "collision_mask": mask,
                       "root_swing": root_swing,
                       "joints": nodes})
    for name, body in sorted(rigids.items()):
        mode = str(body["mode"])
        if light and mode == "0":
            state, reason = "omitted_light_collider", "轻量模式明确不生成身体碰撞体"
        elif mode == "0" and (not body["bone"] or body["bone"] in bones) and body["bone"] not in dynamic:
            colliders.append({"name": name, "bone": body["bone"],
                              "kind": body["kind"], "a": body["a"], "b": body["b"],
                              "radius": body["radius"], "group_mask": 1 << body["group"],
                              "collision_mask": collision_mask(body["excluded_groups"])})
            state = "static_collider_approximation" if body["shape"] == "BOX" else "static_collider"
            reason = "盒形外接胶囊近似" if body["shape"] == "BOX" else "球/胶囊几何与碰撞掩码"
        elif name in accepted:
            state, reason = "spring_chain_approximation", "模式 2 的平移混合未等价保留" if mode == "2" else "位置弹簧链近似"
        else:
            state, reason = "unsupported", rejected.get(body["bone"], "骨绑定缺失，或固定/动态控制同骨冲突")
        report["bodies"].append({"name": name, "bone": body["bone"], "status": state, "reason": reason})
    for name, joint in sorted(joints.items()):
        a, b = joint.get("a"), joint.get("b")
        if a not in rigids or b not in rigids:
            state = "unsupported_missing_body"
        else:
            ba, bb = rigids[a]["bone"], rigids[b]["bone"]
            direct = ba in bones and bb in bones and (bones[ba].get("parent") == bb or bones[bb].get("parent") == ba)
            state = "parent_chain_approximation" if direct and (a in accepted or b in accepted) else "unsupported_cross_link"
        report["constraints"].append({"name": name, "status": state, "source": joint})
    report["counts"] = {"source_bodies": len(rigids), "source_constraints": len(joints),
                        "chains": len(chains), "simulated_bodies": len(accepted),
                        "colliders": len(colliders),
                        "omitted_colliders": sum(r["status"] == "omitted_light_collider" for r in report["bodies"]),
                        "unsupported_bodies": sum(r["status"] == "unsupported" for r in report["bodies"]),
                        "unsupported_constraints": sum(r["status"].startswith("unsupported") for r in report["constraints"])}
    plan = {"chains": chains, "colliders": colliders, "chain_links": [], "report": report}
    if light and complete_light and chains:
        from .light_adaptation import complete_light_plan
        return complete_light_plan(snapshot, plan, body_contacts=body_contacts)
    return plan
