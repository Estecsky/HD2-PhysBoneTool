"""Preprocessed VRM 1 SpringBone -> existing editable HD2 physics (lossy).

No meshes, external buffers, VRChat SDK, or foreign runtime are imported. The
source document is never changed. All geometry is in source node world space
then fitted to the already imported, not-yet-refitted authoring skeleton.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path
import struct

from .math import add, subtract, multiply, dot, cross, length, normalize, orthogonal
from .reference import quaternion_rotate


class ConversionError(ValueError):
    pass


IDENTITY = ((1., 0., 0., 0.), (0., 1., 0., 0.), (0., 0., 1., 0.), (0., 0., 0., 1.))
MAX_JSON = 32 * 1024 * 1024


def number(value, where, lo=-1.e10, hi=1.e10):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConversionError(f"{where} 必须是数字")
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ConversionError(f"{where} 非有限或超出范围 [{lo}, {hi}]")
    return float(value)


def vector(value, where, count=3):
    if not isinstance(value, (tuple, list)) or len(value) != count:
        raise ConversionError(f"{where} 必须有 {count} 个分量")
    return tuple(number(v, where) for v in value)


def index(value, rows, where):
    if type(value) is not int or not 0 <= value < len(rows):
        raise ConversionError(f"{where} 索引无效：{value}")
    return value


def matmul(a, b):
    return tuple(tuple(sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)) for r in range(4))


def point(m, p):
    return tuple(sum(m[r][c] * p[c] for c in range(3)) + m[r][3] for r in range(3))


def direction(m, v):
    return tuple(sum(m[r][c] * v[c] for c in range(3)) for r in range(3))


def uniform_scale(m, where):
    axes = [tuple(m[r][c] for r in range(3)) for c in range(3)]
    sizes = list(map(length, axes))
    s = sum(sizes) / 3
    if (s < 1.e-8 or not math.isfinite(s) or max(sizes) - min(sizes) > s * 1.e-4 or
            dot(cross(axes[0], axes[1]), axes[2]) <= 0 or
            any(abs(dot(axes[i], axes[j])) > s * s * 1.e-4 for i in range(3) for j in range(i))):
        raise ConversionError(f"{where} 有镜像、剪切、非均匀缩放或退化变换")
    return s


def inverse_point(bone, p):
    # Blender rest_basis is orthonormal; adapters validate this boundary.
    v = subtract(p, bone['head'])
    b = bone['rest_basis']
    return tuple(sum(b[r * 3 + c] * v[r] for r in range(3)) for c in range(3))


def inverse_direction(bone, v):
    b = bone['rest_basis']
    return tuple(sum(b[r * 3 + c] * v[r] for r in range(3)) for c in range(3))


def read_document(path):
    """Bounded JSON read, including GLB JSON chunk; never load BIN or URIs."""
    with Path(path).open('rb') as f:
        header = f.read(12)
        if header[:4] == b'glTF':
            if len(header) != 12:
                raise ConversionError('GLB 文件头不完整')
            _, version, total = struct.unpack('<4sII', header)
            f.seek(0, 2)
            if version != 2 or total != f.tell():
                raise ConversionError('GLB 版本或文件长度无效')
            f.seek(12)
            chunk = f.read(8)
            if len(chunk) != 8:
                raise ConversionError('GLB 缺少 JSON 块')
            size, kind = struct.unpack('<II', chunk)
            if kind != 0x4e4f534a or size % 4 or size > MAX_JSON or size > total - 20:
                raise ConversionError('GLB JSON 块类型或长度无效')
            raw = f.read(size)
        else:
            f.seek(0)
            raw = f.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise ConversionError('物理 JSON 超过 32 MiB')
    try:
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ConversionError(f'JSON 重复字段：{key}')
                out[key] = value
            return out
        doc = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique,
                         parse_constant=lambda s: (_ for _ in ()).throw(ConversionError(f'非法 JSON 数字：{s}')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ConversionError(f'无法读取预处理 VRM JSON：{exc}') from exc
    if not isinstance(doc, dict):
        raise ConversionError('需要完整 glTF/VRM JSON 对象')
    return doc


def node_worlds(doc):
    nodes = doc.get('nodes', [])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 8192:
        raise ConversionError('VRM 节点数量须为 1..8192')
    parent = [None] * len(nodes)
    local = []
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            raise ConversionError(f'节点 {i} 无效')
        if 'matrix' in n:
            if any(k in n for k in ('translation', 'rotation', 'scale')):
                raise ConversionError(f'节点 {i} 同时定义 matrix 与 TRS')
            v = vector(n['matrix'], f'node {i}.matrix', 16)
            m = tuple(tuple(v[c * 4 + r] for c in range(4)) for r in range(4))
            if any(abs(m[3][c] - IDENTITY[3][c]) > 1.e-7 for c in range(4)):
                raise ConversionError(f'节点 {i} 不是仿射矩阵')
        else:
            t = vector(n.get('translation', [0, 0, 0]), 'translation')
            s = vector(n.get('scale', [1, 1, 1]), 'scale')
            q = vector(n.get('rotation', [0, 0, 0, 1]), 'rotation', 4)
            if sum(v * v for v in q) < 1.e-12:
                raise ConversionError('节点四元数退化')
            axes = [quaternion_rotate(q, a) for a in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
            m = tuple(tuple(axes[c][r] * s[c] for c in range(3)) + (t[r],) for r in range(3)) + (IDENTITY[3],)
        # Nonuniform unrelated mesh nodes are allowed. Physics use checks later.
        local.append(m)
        children = n.get('children', [])
        if not isinstance(children, list):
            raise ConversionError(f'节点 {i} children 无效')
        for child in children:
            index(child, nodes, 'children')
            if parent[child] is not None or child == i:
                raise ConversionError('节点树有重复父节点或自环')
            parent[child] = i
    worlds = [None] * len(nodes)
    pending = [i for i, p in enumerate(parent) if p is None]
    for i in pending:
        worlds[i] = matmul(worlds[parent[i]], local[i]) if parent[i] is not None else local[i]
        if any(not math.isfinite(v) or abs(v) > 1.e12 for row in worlds[i] for v in row):
            raise ConversionError('节点世界矩阵非有限或过大')
        pending.extend(nodes[i].get('children', []))
    if len(pending) != len(nodes):
        raise ConversionError('节点树存在环')
    return nodes, parent, worlds


def _key(name):
    name = str(name).replace('.', '_').replace(' ', '_')
    return '_' + name if name[:1].isdigit() else name


def match_bones(nodes, bones, *, preferred_nodes=()):
    targets = {b['name']: b for b in bones}
    if len(targets) != len(bones):
        raise ConversionError('目标骨名重复')
    explicit = {}
    for b in bones:
        if b.get('node_index') is not None:
            i = index(b['node_index'], nodes, 'HD2PB_VRMNodeIndex')
            if i in explicit:
                raise ConversionError('多个目标骨声明同一个 VRM 节点索引')
            explicit[i] = b
    result, used = dict(explicit), {b['name'] for b in explicit.values()}
    preferred_nodes = set(preferred_nodes)
    # Exact names win before importer sanitization. Collider helper names such
    # as Chest$uuid must not make the real Chest bone ambiguous. Only try the
    # stripped legacy suffix after both stronger passes, still requiring unique
    # source and target keys; duplicate names are never guessed by order.
    for key in (str, _key, lambda name: _key(str(name).split('$', 1)[0])):
        source_keys, target_keys = defaultdict(list), defaultdict(list)
        for i, n in enumerate(nodes):
            if n.get('name'):
                source_keys[key(n['name'])].append(i)
        for b in bones:
            if b.get('node_index') is None:
                target_keys[key(b['name'])].append(b)
        for name, ids in source_keys.items():
            candidates = target_keys[name]
            preferred = [i for i in ids if i in preferred_nodes]
            if len(ids) > 1 and len(preferred) == 1:
                ids = preferred
            if len(ids) != 1 or len(candidates) != 1:
                continue
            i, bone = ids[0], candidates[0]
            if i not in result and bone['name'] not in used:
                result[i] = bone
                used.add(bone['name'])
    return result


def fit_skeleton(worlds, matched, tolerance=.002):
    """Positive uniform similarity from stable triangle, validate EVERY match."""
    pairs = [(point(worlds[i], (0, 0, 0)), vector(b['head'], b['name'])) for i, b in matched.items()]
    if len(pairs) < 3:
        raise ConversionError('至少需要三个不共线的唯一同名骨；重复名可用 HD2PB_VRMNodeIndex 明确绑定')
    a = pairs[0]
    b = max(pairs, key=lambda p: length(subtract(p[0], a[0])))
    c = max(pairs, key=lambda p: length(cross(subtract(b[0], a[0]), subtract(p[0], a[0]))))
    def frame(k):
        x = subtract(b[k], a[k])
        z = cross(x, subtract(c[k], a[k]))
        if length(x) < 1.e-6 or length(z) < 1.e-8:
            raise ConversionError('匹配骨位置共线或退化，无法确定坐标变换')
        x, z = normalize(x), normalize(z)
        return (x, cross(z, x), z)
    src, dst = frame(0), frame(1)
    r = tuple(tuple(sum(dst[k][i] * src[k][j] for k in range(3)) for j in range(3)) for i in range(3))
    s = length(subtract(b[1], a[1])) / length(subtract(b[0], a[0]))
    rs = tuple(tuple(v * s for v in row) for row in r)
    offset = subtract(a[1], direction(rs, a[0]))
    m = tuple(rs[i] + (offset[i],) for i in range(3)) + (IDENTITY[3],)
    errors = [length(subtract(point(m, p), q)) for p, q in pairs]
    if max(errors) > tolerance:
        raise ConversionError(f'骨架对应偏差 {max(errors):.6f} m；请在 Batch 比例重拟合前的源骨架副本上转换，不能猜测变形映射')
    return m, s, max(errors)


def plan_conversion(doc, target, *, fixed_dt=1 / 60):
    fixed_dt = number(fixed_dt, 'fixed_dt', .001, .1)
    nodes, parents, worlds = node_worlds(doc)
    spring = doc.get('extensions', {}).get('VRMC_springBone')
    if not isinstance(spring, dict):
        raise ConversionError('缺少 VRMC_springBone；请先自行完成 VRC→VRM 1 预处理，不直接读取 Unity/FBX/VRM 0')
    springs, cols, groups = (spring.get(k, []) for k in ('springs', 'colliders', 'colliderGroups'))
    if any(not isinstance(v, list) or len(v) > 4096 for v in (springs, cols, groups)):
        raise ConversionError('SpringBone 表必须是长度不超过 4096 的数组')
    human = doc.get('extensions', {}).get('VRMC_vrm', {}).get('humanoid', {}).get('humanBones', {})
    # The standard Batch route maps these semantic channels to animation-owned
    # HD2 bones. Catch this before a later transfer rejects the entire project.
    native_roles = {'hips', 'spine', 'chest', 'upperChest', 'neck', 'head'}
    for side in ('left', 'right'):
        native_roles.update(side + part for part in (
            'Shoulder', 'UpperArm', 'LowerArm', 'Hand', 'UpperLeg', 'LowerLeg', 'Foot', 'Toes'))
        native_roles.update(side + finger + segment for finger in ('Index', 'Middle', 'Ring', 'Little')
                            for segment in ('Proximal', 'Intermediate', 'Distal'))
        native_roles.update(side + 'Thumb' + segment for segment in ('Metacarpal', 'Proximal', 'Distal'))
    native_nodes = {index(row['node'], nodes, 'humanoid bone'): role
                    for role, row in human.items() if role in native_roles}
    bones = target['bones']
    for b in bones:
        vector(b['head'], '骨 head'); vector(b['tail'], '骨 tail')
        basis = vector(b['rest_basis'], '骨 rest_basis', 9)
        m = tuple(tuple(basis[r * 3 + c] for c in range(3)) + (0.,) for r in range(3)) + (IDENTITY[3],)
        if abs(uniform_scale(m, b['name']) - 1) > 1.e-4:
            raise ConversionError('目标骨 Rest 基必须正交单位化')
    skin_nodes = {index(ni, nodes, 'skin joint') for skin in doc.get('skins', [])
                  for ni in skin.get('joints', [])}
    matched = match_bones(nodes, bones, preferred_nodes=skin_nodes | set(native_nodes))
    fit, scale, residual = fit_skeleton(worlds, matched)
    owner_rotation = target.get('world_rotation', IDENTITY)
    if abs(uniform_scale(owner_rotation, '世界旋转') - 1) > 1.e-4:
        raise ConversionError('世界旋转必须单位化')
    report = {'schema': 'HD2VRMConversionReport1', 'lossless': False,
              'source_format': 'VRMC_springBone', 'matched_bones': len(matched),
              'scale': scale, 'fit_max_error_m': residual, 'springs': [], 'colliders': [],
              'approximations': [
                  '不是 VRChat PhysBone 或参考程序的同一求解器；不保留抓取、触摸、参数驱动或扭转动力学。',
                  'VRM 出段参数移到 HD2 入段节点；最后节点参数无出段，不用于模拟。',
                  '刚度按 stiffness/源段长估算响应率，阻尼按 60 Hz 换算；HD2 阻尼相对动画，不是世界阻尼。',
                  '重力按参考 60 Hz 位移估算加速度，再取每链向量均值；需制作预览调节，不承诺等效运动。',
                  '角限中心重置到实际 Rest 入段，保留来源轴的切向朝向；椭圆 pitch/yaw 按参考径向半角近似。',
                  '不启用整段、自身或链间碰撞；只保留明确的链→碰撞体关系。',
                  '平面随所绑定的骨或模型根，不是游戏场景世界地面。',
                  '来源 spring.center 的惯性参照由现有模型根/动画相对参照替代。'],
              'source_physics': spring}
    report['approximations'].append(
        '已删无用骨只沿目标现有直接父子链简化；跨过已删节点时采用保留起点的出段参数。'
        '不恢复已删骨的自由度，不改已有骨骼、权重或父子关系；整条缺失或关系冲突仍跳过。')
    chains, helpers, used, signatures = [], [], set(), {}
    by_name = {b['name']: b for b in bones}
    for si, sp in enumerate(springs):
        if not isinstance(sp, dict):
            report['springs'].append({'index': si, 'status': 'unsupported', 'reason': 'Spring 条目不是对象'})
            continue
        record = {'index': si, 'name': sp.get('name', f'Spring_{si}'), 'status': 'unsupported'}
        report['springs'].append(record)
        try:
            joints = sp.get('joints', [])
            if not 2 <= len(joints) <= 64:
                raise ConversionError('仅支持 2..64 个显式节点的父子链；单节点需预处理补出段端点')
            ids = [index(j['node'], nodes, 'spring joint') for j in joints]
            if any(ni in native_nodes for ni in ids):
                raise ConversionError('链包含需保留游戏动画的人体映射骨：' + ', '.join(native_nodes[ni] for ni in ids if ni in native_nodes))
            if len(set(ids)) != len(ids) or any(parents[ids[k]] != ids[k-1] for k in range(1, len(ids))):
                raise ConversionError('节点重复或不是直接父子链')
            original_ids = ids
            # A preparation pass may remove unused roots/intermediate joints.
            # Follow only surviving bones, retaining the source terminal as a
            # helper when needed. The strict target parent check below still
            # rejects broken branches/reparenting to unrelated simulated bones.
            retained = [k for k, ni in enumerate(ids) if ni in matched]
            if not retained:
                raise ConversionError('整条链在目标骨架中均无唯一对应骨')
            if retained[-1] != len(ids) - 1 and not nodes[ids[-1]].get('children'):
                retained.append(len(ids) - 1)
            omitted = [ni for k, ni in enumerate(ids) if k not in retained]
            target_keys = {_key(b['name']) for b in bones}
            if any(_key(nodes[ni].get('name', '')) in target_keys for ni in omitted):
                raise ConversionError('被跳过节点存在同名目标骨但映射不唯一，不能当作已删除骨')
            joints = [joints[k] for k in retained]
            ids = [ids[k] for k in retained]
            if len(ids) < 2:
                raise ConversionError('删骨后没有可模拟的有效出段')
            bs, hs = [], []
            for k, ni in enumerate(ids):
                if ni in matched:
                    bs.append(matched[ni])
                elif k == len(ids)-1 and bs and not nodes[ni].get('children'):
                    if _key(nodes[ni].get('name', '')) in target_keys:
                        raise ConversionError('末端存在同名目标骨但映射不唯一，不能重复生成辅助骨')
                    head = point(fit, point(worlds[ni], (0, 0, 0)))
                    tip = {'name': f'HD2PB_VRM_Tip_{si:04d}_{ni}', 'parent': bs[-1]['name'], 'head': head,
                           'tail': add(head, multiply(normalize(subtract(head, bs[-1]['head'])), .01)),
                           'rest_basis': bs[-1]['rest_basis']}
                    if tip['name'] in by_name:
                        raise ConversionError('辅助骨名称已存在')
                    hs.append(tip); bs.append(tip)
                else:
                    raise ConversionError(f'缺少唯一对应骨：node {ni} {nodes[ni].get("name", "")}')
            if any(bs[k].get('parent') != bs[k-1]['name'] for k in range(1, len(bs))):
                raise ConversionError('导入骨架的父子关系与预处理源不一致')
            if any(b.get('is_native') for b in bs) or any(b['name'] in used for b in bs):
                raise ConversionError('原生游戏骨或其他模拟链已占用这些节点')
            colset = set()
            for gi in sp.get('colliderGroups', []):
                group = groups[index(gi, groups, 'colliderGroup')]
                for ci in group.get('colliders', []):
                    colset.add(index(ci, cols, 'collider'))
            signature = tuple(sorted(colset))
            if signature and signature not in signatures and len(signatures) >= 29:
                raise ConversionError('超过 29 种独立碰撞集合，不合并互不兼容的碰撞关系')
            values, gravities, ks, ds = [], [], [], []
            values.append({'bone': bs[0]['name'], 'radius': 0., 'stiffness_scale': 0., 'damping_scale': 0., 'limit_kind': 'NONE'})
            for k in range(1, len(bs)):
                j, ni, b = joints[k-1], ids[k-1], bs[k-1]
                source_length = length(subtract(point(worlds[ids[k]], (0, 0, 0)), point(worlds[ni], (0, 0, 0))))
                dest_length = length(subtract(bs[k]['head'], b['head']))
                if min(source_length, dest_length) < 1.e-6:
                    raise ConversionError('零长或退化物理段')
                ws = uniform_scale(worlds[ni], f'物理节点 {ni}')
                stiffness = number(j.get('stiffness', 1.), 'stiffness', 0)
                rate = stiffness / source_length
                if rate > 100000:
                    raise ConversionError('估算刚度超出既有参数域，需预处理/手工调节')
                drag = number(j.get('dragForce', .5), 'dragForce', 0, 1)
                damping = min(.999, 1 - (1 - drag) ** (fixed_dt * 60))
                g = multiply(direction(owner_rotation, direction(fit, vector(j.get('gravityDir', [0, -1, 0]), 'gravityDir'))),
                             number(j.get('gravityPower', 0.), 'gravityPower', 0) * 60)
                if max(map(abs, g)) > 10000:
                    raise ConversionError('估算重力超出既有参数域')
                gravities.append(g); ks.append(rate); ds.append(damping)
                v = {'bone': bs[k]['name'], 'radius': number(j.get('hitRadius', 0.), 'hitRadius', 0) * ws * scale,
                     'limit_kind': 'NONE', 'stiffness_scale': rate, 'damping_scale': damping}
                number(v['radius'], '转换后节点半径', 0, 10)
                lim = j.get('extensions', {}).get('VRMC_springBone_limit', {}).get('limit', {})
                if lim:
                    if len(lim) != 1 or next(iter(lim)) not in ('cone', 'hinge', 'spherical'):
                        raise ConversionError('未知角限类型')
                    kind, data = next(iter(lim.items()))
                    q = vector(data.get('rotation', [0, 0, 0, 1]), 'limit rotation', 4)
                    if sum(x*x for x in q) < 1.e-12:
                        raise ConversionError('角限坐标架四元数退化')
                    y = normalize(subtract(bs[k]['head'], b['head']))
                    x = direction(fit, direction(worlds[ni], quaternion_rotate(q, (1, 0, 0))))
                    x = normalize(subtract(x, multiply(y, dot(x, y))), orthogonal(y))
                    v.update(limit_kind=kind.upper(),
                             limit_angle=number(data.get('yaw' if kind == 'spherical' else 'angle', 0), '角限', 0, math.pi + 1.e-6),
                             limit_angle2=number(data.get('pitch', data.get('angle', 0)), '第二角限', 0, math.pi + 1.e-6),
                             limit_axis=inverse_direction(b, x), reference_mode='EXPLICIT_LOCAL',
                             reference_direction=inverse_direction(b, y), reference_frame=(0., 0., 0., 1.))
                    v['limit_angle'] = min(math.pi, v['limit_angle'])
                    v['limit_angle2'] = min(math.pi, v['limit_angle2'])
                values.append(v)
            base_k, base_d = min(1000., max(ks)), max(ds)
            for v in values:
                v['stiffness_scale'] /= base_k or 1.
                v['damping_scale'] /= base_d or 1.
            if signature and signature not in signatures:
                signatures[signature] = 1 << len(signatures)
            chain = {'name': record['name'], 'source_index': si, 'joints': values,
                     'stiffness': base_k, 'damping': base_d,
                     'gravity': tuple(sum(g[k] for g in gravities) / len(gravities) for k in range(3)),
                     'group_mask': 1 << 30, 'collision_mask': signatures.get(signature, 1 << 29),
                     'root_swing': True, 'local_bend': True,
                     'source_colliders': signature}
            chains.append(chain); helpers.extend(hs); used.update(b['name'] for b in bs)
            record.update(status='converted', nodes=len(values), gravity_averaged=len(set(gravities)) > 1,
                          terminal_parameters_ignored=True, helpers=[b['name'] for b in hs],
                          source_nodes=len(original_ids), omitted_source_nodes=omitted,
                          simplified_after_pruning=bool(omitted))
        except (ConversionError, KeyError, TypeError, ValueError) as exc:
            record['reason'] = str(exc)
    # The current shared native solver supports disjoint nested branches and
    # transports each child root with its reconstructed parent frame. Keep the
    # real hierarchy; unique simulated ownership was checked above. Still reject
    # malformed target ancestry, including cycles through non-physics helpers.
    for c in chains:
        p = by_name[c['joints'][0]['bone']].get('parent')
        visited = set()
        nested = False
        while p:
            if p in visited:
                raise ConversionError('目标骨父子关系存在环')
            visited.add(p)
            if p in used:
                nested = True
            p = by_name.get(p, {}).get('parent')
        report['springs'][c['source_index']]['nested_branch'] = nested
    needed_helpers = {v['bone'] for c in chains for v in c['joints']}
    helpers = [h for h in helpers if h['name'] in needed_helpers]
    used = {v['bone'] for c in chains for v in c['joints']}
    root_names = {c['joints'][0]['bone'] for c in chains}
    # Importers commonly omit non-skinned collider transform nodes. Their local
    # transforms can be baked into the nearest retained ancestor, but missing
    # simulated/animated joints cannot be silently treated as static. Some VRM
    # exporters put even unused collider transforms in skin.joints, so mere
    # membership in that table is not evidence of independent motion.
    moving_nodes = set(native_nodes)
    for sp in springs:
        if isinstance(sp, dict):
            moving_nodes.update(j.get('node') for j in sp.get('joints', [])
                                if isinstance(j, dict) and type(j.get('node')) is int)
    for animation in doc.get('animations', []):
        for channel in animation.get('channels', []):
            node_id = channel.get('target', {}).get('node')
            if node_id is not None:
                moving_nodes.add(index(node_id, nodes, 'animation target'))
    colliders = []
    for ci, col in enumerate(cols):
        r = {'index': ci, 'status': 'unsupported'}
        report['colliders'].append(r)
        try:
            mask = 0
            for c in chains:
                if ci in c['source_colliders']:
                    mask |= c['collision_mask']
            if not mask:
                raise ConversionError('没有已转换链引用此碰撞体')
            ni = index(col['node'], nodes, 'collider node')
            # A glTF scene-root collider belongs to model space, not to a
            # fabricated parentless custom bone in the HD2 work rig.
            attachment, skipped = ni, []
            while attachment not in matched and parents[attachment] is not None:
                if attachment in moving_nodes:
                    raise ConversionError('碰撞体依赖缺失的动画/物理骨，不能改绑到静态父骨')
                skipped.append(attachment)
                attachment = parents[attachment]
            bone = matched.get(attachment) if parents[attachment] is not None else None
            if bone is None and attachment in moving_nodes:
                raise ConversionError('碰撞体依赖缺失的动画/物理骨，不能改绑到模型根')
            ws = uniform_scale(worlds[ni], f'碰撞节点 {ni}')
            ext = col.get('extensions', {}).get('VRMC_springBone_extended_collider')
            shape = ext.get('shape', {}) if ext is not None else col.get('shape', {})
            if len(shape) != 1 or next(iter(shape)) not in ('sphere', 'capsule', 'plane'):
                raise ConversionError('未知扩展碰撞形状，不使用兼容球体回退')
            kind, data = next(iter(shape.items()))
            name = bone['name'] if bone else ''
            dynamic = name in used
            if name in root_names or dynamic and kind == 'plane':
                raise ConversionError('摆动根碰撞体或动态平面不在现有合同内')
            a = point(fit, point(worlds[ni], vector(data.get('offset', [0, 0, 0]), 'offset')))
            b = a
            if kind == 'capsule':
                b = point(fit, point(worlds[ni], vector(data['tail'], 'capsule tail')))
            elif kind == 'plane':
                b = direction(fit, direction(worlds[ni], vector(data['normal'], 'plane normal')))
                if length(b) < 1.e-6:
                    raise ConversionError('平面法线退化')
                b = normalize(b)
            if bone:
                a = inverse_point(bone, a)
                b = inverse_direction(bone, b) if kind == 'plane' else inverse_point(bone, b)
            for v in (*a, *b):
                number(v, '转换后碰撞体坐标', -10000, 10000)
            radius = 0. if kind == 'plane' else number(data['radius'], 'radius', 0) * ws * scale
            number(radius, '转换后碰撞体半径', 0, 10)
            inside = data.get('inside', False)
            if type(inside) is not bool:
                raise ConversionError('inside 必须为布尔值')
            if dynamic and inside:
                raise ConversionError('动态内碰撞体不在现有合同内')
            if kind != 'plane' and radius <= 0:
                raise ConversionError('球/胶囊半径必须大于零')
            if inside and kind != 'plane' and any(
                    j['radius'] > radius for c in chains if ci in c['source_colliders'] for j in c['joints'][1:]):
                raise ConversionError('内碰撞体小于受影响节点半径')
            colliders.append({'name': f'VRM_{ci:04d}_{nodes[ni].get("name", "root")}', 'bone': name,
                              'kind': kind.upper(), 'a': a, 'b': b,
                              'radius': radius,
                              'inside': inside, 'attachment_mode': 'DYNAMIC' if dynamic else 'STATIC',
                              'group_mask': mask, 'collision_mask': 1 << 30})
            r.update(status='converted', source='extended' if ext is not None else 'base', kind=kind,
                     attachment_bone=name, baked_transform_nodes=skipped)
        except (ConversionError, KeyError, TypeError, ValueError) as exc:
            r['reason'] = str(exc)
    report['counts'] = {'chains': len(chains), 'colliders': len(colliders), 'helpers': len(helpers),
                        'source_springs': len(springs), 'source_colliders': len(cols),
                        'absent_springs': sum(r.get('reason') == '整条链在目标骨架中均无唯一对应骨' for r in report['springs']),
                        'simplified_springs': sum(bool(r.get('simplified_after_pruning')) for r in report['springs']),
                        'unreferenced_colliders': sum(r.get('reason') == '没有已转换链引用此碰撞体' for r in report['colliders']),
                        'unsupported_springs': sum(r['status'] != 'converted' for r in report['springs']),
                        'unsupported_colliders': sum(r['status'] != 'converted' for r in report['colliders'])}
    return {'chains': chains, 'colliders': colliders, 'helpers': helpers, 'report': report}
