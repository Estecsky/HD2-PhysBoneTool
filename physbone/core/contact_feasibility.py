"""Conservative Rest-only proofs, not a solver or an automatic parameter fix.

Distance to a capsule centerline is 1-Lipschitz. A fixed-root first joint
cannot move farther from that line than root distance + segment length.
For a supported cone its reachable points fit inside a ball around the
cone-axis endpoint with radius 2*length*sin(angle/2). If even this upper
bound is inside the required contact envelope, the constraints conflict.
Other cases are deliberately inconclusive, never reported as feasible.
"""
import math

_IDENTITY = (1., 0., 0., 0., 1., 0., 0., 0., 1.)


def _vec(values):
    values = tuple(float(x) for x in values)
    if len(values) != 3 or not all(math.isfinite(x) for x in values):
        raise ValueError('invalid point')
    return values


def _basis(bone):
    basis = tuple(float(x) for x in bone.get('rest_basis', _IDENTITY))
    if len(basis) != 9 or not all(math.isfinite(x) for x in basis):
        raise ValueError('invalid basis')
    for a in range(3):
        for b in range(3):
            if abs(sum(basis[k*3+a]*basis[k*3+b] for k in range(3)) - int(a == b)) > 1.e-5:
                raise ValueError('non-rigid basis')
    return basis


def _vector(bone, point):
    b = _basis(bone);p = _vec(point)
    return tuple(sum(b[r*3+k]*p[k] for k in range(3)) for r in range(3))


def _point(bone, point):
    return tuple(x+y for x, y in zip(_vec(bone['rest_head']), _vector(bone, point)))


def _distance(point, a, b):
    delta = tuple(y-x for x, y in zip(a, b));squared = sum(x*x for x in delta)
    t = max(0., min(1., sum((x-y)*z for x, y, z in zip(point, a, delta))/squared)) if squared else 0.
    return math.dist(point, tuple(x+t*y for x, y in zip(a, delta)))


def _collides(chain, joint, collider):
    filters = joint if joint.get('override_collision_filter', False) else chain
    return bool(int(filters.get('group_mask', 1)) & int(collider.get('collision_mask', 0xffffffff)) and
                int(filters.get('collision_mask', 0xffffffff)) & int(collider.get('group_mask', 1)))


def rest_contact_conflicts(document):
    """Return only proven first-movable-joint conflicts; never modify input.

    Unrecognized geometry remains the structural/native validators' job.
    Not listed means unknown, not safe. No rest pose or runtime constraint
    is altered and no runtime checks may be bypassed using this result.
    """
    bones = {b['name']: b for b in document.get('bones', [])}
    chains = document.get('chains', [])
    driven = {j['bone'] for c in chains for j in c.get('joints', [])[1:]}
    driven.update(c['joints'][0]['bone'] for c in chains if c.get('root_swing', False) and c.get('joints'))
    by_slot = {b.get('palette_slot'): b for b in bones.values()}
    rows = []
    for chain in chains:
        joints = chain.get('joints', [])
        if len(joints) < 2 or chain.get('root_swing', False):continue
        try:
            driver = bones[joints[0]['bone']];first = joints[1]
            cursor = driver;seen = set();nested = False
            while cursor is not None:
                if cursor['name'] in seen or cursor['name'] in driven:nested = True;break
                seen.add(cursor['name']);cursor = by_slot.get(cursor.get('parent_slot', -1))
            if nested:continue
            root = _vec(driver['rest_head']);rest = _vec(bones[first['bone']]['rest_head'])
            _basis(driver);reach = math.dist(root, rest)
            if reach <= 1.e-6:continue
            center = None;chord = None
            if first.get('limit_kind', 'NONE') == 'CONE':
                mode = first.get('reference_mode', 'ANIMATED_SEGMENT')
                direction = tuple(y-x for x, y in zip(root, rest)) if mode == 'ANIMATED_SEGMENT' else (
                    _vector(driver, first.get('reference_direction', (0, 1, 0))) if mode == 'EXPLICIT_LOCAL' else None)
                angle = float(first.get('limit_angle', 0))
                if direction is not None and math.isfinite(angle) and 0 <= angle <= math.pi:
                    norm = math.sqrt(sum(x*x for x in direction))
                    if norm > 1.e-6:
                        center = tuple(x+reach*y/norm for x, y in zip(root, direction))
                        chord = 2*reach*math.sin(angle/2)
            for collider in document.get('colliders', []):
                if collider.get('dynamic', False) or collider.get('inside', False) or collider.get('kind', 'SPHERE') not in ('SPHERE', 'CAPSULE'):continue
                local_radius = float(first.get('radius', 0)) if _collides(chain, first, collider) else -1.
                if local_radius < 0:continue
                bone = bones[collider['bone']] if collider.get('bone') else dict(rest_head=(0, 0, 0))
                bone_b = bones[collider['bone_b']] if collider.get('bone_b') else bone
                a = _point(bone, collider.get('a', (0, 0, 0)))
                b = a if collider.get('kind', 'SPHERE') == 'SPHERE' else _point(bone_b, collider.get('b', (0, 0, 1)))
                needed = float(collider['radius'])+local_radius
                bound = _distance(root, a, b)+reach;proof = 'fixed_root_reach'
                if center is not None:
                    cone_bound = _distance(center, a, b)+chord
                    if cone_bound < bound:bound = cone_bound;proof = 'cone_chord_bound'
                if all(math.isfinite(v) for v in (needed, bound)) and needed > bound+2.e-5:
                    rows.append(dict(chain=chain['name'],joint=first['bone'],collider=collider['name'],
                                     proof=proof,rest_only=True,required_distance_m=needed,
                                     maximum_distance_bound_m=bound,minimum_penetration_m=needed-bound))
        except (KeyError, ValueError, TypeError, OverflowError):
            # No verdict for malformed or unhandled inputs. Existing format
            # and native checks still run and retain their hard failures.
            continue
    return rows
