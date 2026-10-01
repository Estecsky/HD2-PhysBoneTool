"""Complete LIGHT's existing editable chain representation, not Bullet emulation.

Adds only expressible tail points, long-chain local bending, well-evidenced
transverse strip links and optional Rest-clear body proxies. All decisions are
recorded; the input PMX graph and imported data remain untouched.
"""
from __future__ import annotations
import copy
import math
from collections import defaultdict


def _add(a, b): return tuple(x + y for x, y in zip(a, b))
def _sub(a, b): return tuple(x - y for x, y in zip(a, b))
def _mul(a, s): return tuple(x * s for x in a)
def _dot(a, b): return sum(x * y for x, y in zip(a, b))
def _length(a): return math.sqrt(_dot(a, a))


def segment_distance(p, q, a, b):
    """Exact closest distance of finite line segments, including zero lengths."""
    d1, d2, r = _sub(q, p), _sub(b, a), _sub(p, a)
    aa, ee, ff = _dot(d1, d1), _dot(d2, d2), _dot(d2, r)
    clamp = lambda value: max(0., min(1., value))
    if aa <= 1.e-16 and ee <= 1.e-16: return _length(r)
    if aa <= 1.e-16:
        s, t = 0., clamp(ff / ee)
    else:
        cc = _dot(d1, r)
        if ee <= 1.e-16:
            s, t = clamp(-cc / aa), 0.
        else:
            bb = _dot(d1, d2)
            denominator = aa * ee - bb * bb
            s = clamp((bb * ff - cc * ee) / denominator) if denominator > 1.e-16 else 0.
            t = (bb * s + ff) / ee
            if t < 0.: s, t = clamp(-cc / aa), 0.
            elif t > 1.: s, t = clamp((bb - cc) / aa), 1.
    return _length(_sub(_add(p, _mul(d1, s)), _add(a, _mul(d2, t))))


def _point(bone, local):
    basis = bone.get('rest_basis') or (1., 0., 0., 0., 1., 0., 0., 0., 1.)
    return tuple(bone['head'][r] + sum(basis[r * 3 + c] * local[c] for c in range(3)) for r in range(3))


def complete_light_plan(snapshot, plan, *, body_contacts=True):
    plan = copy.deepcopy(plan)
    bones = {bone['name']: bone for bone in snapshot['bones']}
    helpers, adaptations = [], []
    chains = plan['chains']
    chain_by_bone = {}
    for ci, chain in enumerate(chains):
        nodes = chain['joints']
        if not chain['root_swing']:
            # Legacy singleton used its parent as driver. Pin/swing the actual
            # weighted bone and add an unweighted endpoint so it can rotate.
            nodes.pop(0)
        last = bones[nodes[-1]['bone']]
        head = tuple(last.get('tail') or ())
        fallback = False
        if len(head) != 3 or not all(math.isfinite(v) for v in head) or math.dist(head, last['head']) < 1.e-5:
            parent = bones.get(last.get('parent'))
            direction = _sub(last['head'], parent['head']) if parent else (0., .02, 0.)
            length = _length(direction)
            direction = _mul(direction, min(.03, max(.003, length)) / length) if length > 1.e-8 else (0., .02, 0.)
            head, fallback = _add(last['head'], direction), True
        suffix = '_HD2PB_Tip'
        stem = last['name'].encode('utf-8')[:63 - len(suffix)].decode('utf-8', errors='ignore')
        name = stem + suffix
        serial = 0
        while name in bones:
            serial += 1
            end = suffix + str(serial)
            name = stem.encode('utf-8')[:63 - len(end)].decode('utf-8', errors='ignore') + end
        direction = _sub(head, last['head'])
        helper = dict(name=name, parent=last['name'], head=head,
                      tail=_add(head, _mul(direction, .5)), rest_basis=last.get('rest_basis'),
                      fallback_endpoint=fallback)
        helpers.append(helper)
        bones[name] = helper
        chain['root_swing'] = True
        chain['local_bend'] = len(nodes) >= 8
        if chain['local_bend']:
            chain['stiffness'], chain['damping'] = 12., .8
        for index, node in enumerate(nodes): chain_by_bone[node['bone']] = (ci, index)
        tail_node = copy.deepcopy(nodes[-1])
        tail_node['bone'] = name
        nodes.append(tail_node)
        for index, node in enumerate(nodes):
            progress = index / max(1, len(nodes) - 1)
            node.update(stiffness_scale=0. if index == 0 else 1.5 - progress,
                        damping_scale=0. if index == 0 else 1.,
                        limit_kind='NONE' if index == 0 else 'CONE',
                        limit_angle=math.radians(12 + 6 * progress if chain['local_bend'] else 30))
            segment_length = math.dist(bones[node['bone']]['head'], bones[nodes[index - 1]['bone']]['head']) if index else 0.
            # A collision proxy must fit the represented segment, not the
            # enclosing PMX rigid's unrelated offset centre/box dimensions.
            node['radius'] = 0. if index == 0 else min(node['radius'], .006, segment_length * .2)
        adaptations.append(dict(chain=chain['name'], tail_helper=name, root_swing=True,
                                local_bend=chain['local_bend'], fallback_endpoint=fallback))

    rigids = {body['name']: body for body in snapshot['rigids']}
    transverse = defaultdict(dict)
    for joint in snapshot['joints']:
        a, b = rigids.get(joint.get('a')), rigids.get(joint.get('b'))
        if not a or not b: continue
        ca, cb = chain_by_bone.get(a['bone']), chain_by_bone.get(b['bone'])
        if not ca or not cb or ca[0] == cb[0] or ca[1] != cb[1]: continue
        pair = tuple(sorted((ca[0], cb[0])))
        transverse[pair].setdefault(ca[1], []).append(joint['name'])
    represented = set()
    for (ia, ib), rows in sorted(transverse.items()):
        a, b = chains[ia], chains[ib]
        count = len(a['joints']) - 1  # Source bones, excluding generated tail.
        # Require every authored corresponding row, not a stray cross joint.
        if count < 3 or len(b['joints']) != len(a['joints']) or set(rows) != set(range(count)):
            continue
        a['local_bend'] = b['local_bend'] = True
        for chain in (a, b):
            chain['stiffness'], chain['damping'] = 12., .8
        plan['chain_links'].append(dict(name=a['name'] + '__' + b['name'], chain_a=a['name'], chain_b=b['name'],
            coupling=1., max_correction=.05, flags=1, collisions=[]))
        represented.update(name for names in rows.values() for name in names)

    body_rows = {row['name']: row for row in plan['report']['bodies']}
    proxy_report = []
    if body_contacts:
        segments = [(bones[c['joints'][i - 1]['bone']]['head'], bones[c['joints'][i]['bone']]['head'],
                     max(c['joints'][i - 1]['radius'], c['joints'][i]['radius']))
                    for c in chains for i in range(1, len(c['joints']))]
        simulated = {node['bone'] for c in chains for node in c['joints']}
        for name, body in sorted(rigids.items()):
            if str(body['mode']) != '0' or body['bone'] in simulated: continue
            bone = bones.get(body['bone']) if body['bone'] else None
            if body['bone'] and not bone: continue
            a = _point(bone, body['a']) if bone else tuple(body['a'])
            b = _point(bone, body['b']) if bone else tuple(body['b'])
            if body['kind'] == 'SPHERE': b = a
            clearance = min(segment_distance(p, q, a, b) - radius for p, q, radius in segments)
            radius = min(body['radius'], clearance - .002)
            if radius < .001:
                proxy_report.append(dict(body=name, status='omitted_rest_overlap', clearance=clearance))
                body_rows[name].update(status='omitted_light_collider', reason='静止链已与代理重叠，无法收缩出安全身体避让体')
                continue
            plan['colliders'].append(dict(name=name, bone=body['bone'], kind=body['kind'], a=body['a'], b=body['b'],
                radius=radius, group_mask=2, collision_mask=1))
            proxy_report.append(dict(body=name, status='rest_clear_body_proxy', radius=radius,
                                     original_radius=body['radius'], clearance=clearance))
            body_rows[name].update(status='light_body_proxy', reason='统一轻量身体避让；半径按静止骨段净空收缩，不继承刚体反作用')
        for chain in chains:
            chain['group_mask'], chain['collision_mask'] = 1, 2 if plan['colliders'] else 1 << 30

    report = plan['report']
    for row in report['constraints']:
        if row['name'] in represented:
            row['status'] = 'strip_link_approximation'
    report.update(complete_light=True, generated_helpers=helpers, adaptations=adaptations,
                  body_proxy_adaptation=proxy_report,
                  parameter_semantics='Canonical LIGHT parameters are shared unchanged by editor, preview, Batch transfer and export; not equivalent to Bullet.')
    report['approximations'] = [text for text in report['approximations'] if not any(
        marker in text for marker in ('轻量模式不生成碰撞体', '无末端辅助骨', '横向约束保留在报告'))]
    report['approximations'].extend([
        '完整轻量转换补真实末端点；原骨骼、网格、权重和 MMD 刚体/约束不改写。',
        '长链使用局部弯曲；只有横向关节完整覆盖等长列时才近似为布条宽度/剪切约束，其余仍报告未支持。',
        '身体避让使用静止净空收缩后的固定代理与统一轻量碰撞组；不保留 MMD 的刚体反作用和逐刚体碰撞表。',
    ])
    report['counts'].update(helpers=len(helpers), strip_links=len(plan['chain_links']), colliders=len(plan['colliders']),
        omitted_colliders=sum(row['status'] == 'omitted_light_collider' for row in report['bodies']),
        unsupported_constraints=sum(row['status'].startswith('unsupported') for row in report['constraints']))
    plan['helpers'] = helpers
    return plan
