"""deterministic capsule contacts and Chain Group Link projection.

This module deliberately has no Blender dependency.  Runtime consumers can map
their scratch positions to integer owner ids, build immutable proxies for one
global pass, and apply the returned position corrections atomically.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, MutableMapping, MutableSequence, Sequence


Vec3 = tuple[float, float, float]

DISTANCE_EPSILON = 1.0e-6
DISTANCE_EPSILON_SQUARED = DISTANCE_EPSILON * DISTANCE_EPSILON
GRID_CELL_SIZE = 0.25
MAX_PROXY_CELLS = 4096
MAX_CELL_ENTRIES = 262144
MAX_CANDIDATE_PAIRS = 65536
MAX_CONTACTS = 32768
MAX_GLOBAL_PASSES = 12

PROXY_NODE = "NODE"
PROXY_DYNAMIC_COLLIDER = "DYNAMIC_COLLIDER"
PROXY_LINK_COLLISION = "LINK_COLLISION"
_PROXY_KINDS = {PROXY_NODE, PROXY_DYNAMIC_COLLIDER, PROXY_LINK_COLLISION}


class ContactError(ValueError):
    """geometry, filtering, or hard-limit failure."""


def _vec3(value: Iterable[float], where: str) -> Vec3:
    try:
        result = tuple(float(component) for component in value)
    except (TypeError, ValueError) as exc:
        raise ContactError(f"{where}: expected three finite numbers") from exc
    if len(result) != 3 or not all(math.isfinite(component) for component in result):
        raise ContactError(f"{where}: expected three finite numbers")
    return result  # type: ignore[return-value]


def _add(a: Vec3, b: Vec3) -> Vec3:
    return a[0] + b[0], a[1] + b[1], a[2] + b[2]


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def _scale(value: Vec3, factor: float) -> Vec3:
    return value[0] * factor, value[1] * factor, value[2] * factor


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _length_squared(value: Vec3) -> float:
    return _dot(value, value)


def _length(value: Vec3) -> float:
    return math.sqrt(_length_squared(value))


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class ClosestPoints:
    point_a: Vec3
    point_b: Vec3
    parameter_a: float
    parameter_b: float
    distance: float


def closest_points_segments(a0: Iterable[float], a1: Iterable[float],
                            b0: Iterable[float], b1: Iterable[float]) -> ClosestPoints:
    """Return closest points on two finite segments using fixed degenerate branches."""

    p0 = _vec3(a0, "a0")
    p1 = _vec3(a1, "a1")
    q0 = _vec3(b0, "b0")
    q1 = _vec3(b1, "b1")
    da = _sub(p1, p0)
    db = _sub(q1, q0)
    r = _sub(p0, q0)
    aa = _dot(da, da)
    ee = _dot(db, db)

    if aa <= DISTANCE_EPSILON_SQUARED and ee <= DISTANCE_EPSILON_SQUARED:
        s = 0.0
        t = 0.0
    elif aa <= DISTANCE_EPSILON_SQUARED:
        s = 0.0
        t = _clamp01(_dot(db, r) / ee)
    elif ee <= DISTANCE_EPSILON_SQUARED:
        t = 0.0
        s = _clamp01(-_dot(da, r) / aa)
    else:
        bb = _dot(da, db)
        cc = _dot(da, r)
        ff = _dot(db, r)
        denominator = aa * ee - bb * bb
        parallel_limit = DISTANCE_EPSILON_SQUARED * aa * ee
        if denominator > parallel_limit:
            s = _clamp01((bb * ff - cc * ee) / denominator)
        else:
            s = 0.0
        t = (bb * s + ff) / ee
        if t < 0.0:
            t = 0.0
            s = _clamp01(-cc / aa)
        elif t > 1.0:
            t = 1.0
            s = _clamp01((bb - cc) / aa)

    point_a = _add(p0, _scale(da, s))
    point_b = _add(q0, _scale(db, t))
    return ClosestPoints(point_a, point_b, s, t, _length(_sub(point_b, point_a)))


@dataclass(frozen=True)
class ProxyEndpoint:
    position: Vec3
    owner: int | None = None
    inverse_mass: float = 0.0
    owner_position: Vec3 | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "position", _vec3(self.position, "endpoint.position"))
        if self.owner is not None and (isinstance(self.owner, bool) or not isinstance(self.owner, int)
                                       or self.owner < 0):
            raise ContactError("endpoint.owner: expected a non-negative integer or None")
        mass = float(self.inverse_mass)
        if not math.isfinite(mass) or mass < 0.0:
            raise ContactError("endpoint.inverse_mass: expected a finite non-negative value")
        if self.owner is None and mass != 0.0:
            raise ContactError("static endpoint without owner must have zero inverse mass")
        object.__setattr__(self, "inverse_mass", mass)
        if self.owner_position is not None:
            if self.owner is None:
                raise ContactError("endpoint.owner_position requires an owner")
            object.__setattr__(
                self, "owner_position", _vec3(self.owner_position, "endpoint.owner_position")
            )


@dataclass(frozen=True)
class CapsuleProxy:
    proxy_id: int
    kind: str
    a: ProxyEndpoint
    b: ProxyEndpoint
    radius: float
    group_mask: int
    collision_mask: int

    def __post_init__(self) -> None:
        if isinstance(self.proxy_id, bool) or not isinstance(self.proxy_id, int) or self.proxy_id < 0:
            raise ContactError("proxy_id: expected a non-negative integer")
        if self.kind not in _PROXY_KINDS:
            raise ContactError(f"kind: unsupported proxy kind {self.kind!r}")
        radius = float(self.radius)
        if not math.isfinite(radius) or not 0.0 < radius <= 10.0:
            raise ContactError("radius: expected a finite value in (0, 10]")
        object.__setattr__(self, "radius", radius)
        for name in ("group_mask", "collision_mask"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 0xFFFFFFFF:
                raise ContactError(f"{name}: expected a non-zero uint32")
        if self.kind == PROXY_NODE and (
                self.a.position != self.b.position or self.a.owner != self.b.owner):
            raise ContactError("node proxy endpoints must describe one node sphere")
        if (self.a.owner is not None and self.a.owner == self.b.owner
                and self.a.inverse_mass != self.b.inverse_mass):
            raise ContactError("one owner cannot use two inverse masses")

    @property
    def dynamic_owners(self) -> frozenset[int]:
        return frozenset(
            endpoint.owner for endpoint in (self.a, self.b)
            if endpoint.owner is not None and endpoint.inverse_mass > 0.0
        )


def proxy_pair_allowed(first: CapsuleProxy, second: CapsuleProxy) -> bool:
    """Apply the frozen bidirectional filter and canonical exclusion rules."""

    if first.proxy_id == second.proxy_id:
        return False
    if not (first.group_mask & second.collision_mask):
        return False
    if not (second.group_mask & first.collision_mask):
        return False
    if first.dynamic_owners & second.dynamic_owners:
        return False
    return (first.kind == PROXY_NODE) != (second.kind == PROXY_NODE)


def _endpoint_position(endpoint: ProxyEndpoint,
                       positions: MutableMapping[int, Vec3] | None) -> Vec3:
    if endpoint.owner is None or positions is None:
        return endpoint.position
    try:
        owner_position = _vec3(positions[endpoint.owner], f"positions[{endpoint.owner}]")
    except KeyError as exc:
        raise ContactError(f"positions: missing owner {endpoint.owner}") from exc
    reference = endpoint.owner_position or endpoint.position
    return _add(endpoint.position, _sub(owner_position, reference))


def _proxy_points(proxy: CapsuleProxy,
                  positions: MutableMapping[int, Vec3] | None) -> tuple[Vec3, Vec3]:
    return _endpoint_position(proxy.a, positions), _endpoint_position(proxy.b, positions)


def proxy_points(proxy: CapsuleProxy,
                 positions: MutableMapping[int, Vec3] | None = None) -> tuple[Vec3, Vec3]:
    """Resolve proxy endpoints after applying each owner's scratch translation."""

    return _proxy_points(proxy, positions)


def _fallback_normal(first: CapsuleProxy, second: CapsuleProxy,
                     a0: Vec3, a1: Vec3, b0: Vec3, b1: Vec3) -> Vec3:
    cross = _cross(_sub(a1, a0), _sub(b1, b0))
    cross_length = _length(cross)
    if cross_length > DISTANCE_EPSILON:
        return _scale(cross, 1.0 / cross_length)
    midpoint_delta = _sub(_scale(_add(b0, b1), 0.5), _scale(_add(a0, a1), 0.5))
    midpoint_length = _length(midpoint_delta)
    if midpoint_length > DISTANCE_EPSILON:
        return _scale(midpoint_delta, 1.0 / midpoint_length)
    low, high = sorted((first.proxy_id, second.proxy_id))
    axis = (low * 31 + high) % 3
    return (1.0, 0.0, 0.0) if axis == 0 else ((0.0, 1.0, 0.0) if axis == 1 else (0.0, 0.0, 1.0))


@dataclass(frozen=True)
class Contact:
    proxy_a: int
    proxy_b: int
    parameter_a: float
    parameter_b: float
    normal: Vec3
    penetration: float


def contact_for_pair(first: CapsuleProxy, second: CapsuleProxy,
                     positions: MutableMapping[int, Vec3] | None = None) -> Contact | None:
    if first.proxy_id > second.proxy_id:
        first, second = second, first
    if not proxy_pair_allowed(first, second):
        return None
    a0, a1 = _proxy_points(first, positions)
    b0, b1 = _proxy_points(second, positions)
    closest = closest_points_segments(a0, a1, b0, b1)
    first_endpoints = ((first.a,) if closest.parameter_a <= DISTANCE_EPSILON else
                       ((first.b,) if closest.parameter_a >= 1.0 - DISTANCE_EPSILON else ()))
    second_endpoints = ((second.a,) if closest.parameter_b <= DISTANCE_EPSILON else
                        ((second.b,) if closest.parameter_b >= 1.0 - DISTANCE_EPSILON else ()))
    if any(a.owner is not None and a.owner == b.owner
           and a.inverse_mass == 0.0 and b.inverse_mass == 0.0
           for a in first_endpoints for b in second_endpoints):
        return None
    penetration = first.radius + second.radius - closest.distance
    if penetration <= DISTANCE_EPSILON:
        return None
    delta = _sub(closest.point_b, closest.point_a)
    if closest.distance > DISTANCE_EPSILON:
        normal = _scale(delta, 1.0 / closest.distance)
    else:
        normal = _fallback_normal(first, second, a0, a1, b0, b1)
    return Contact(first.proxy_id, second.proxy_id, closest.parameter_a,
                   closest.parameter_b, normal, penetration)


@dataclass(frozen=True)
class ContactProjection:
    contact: Contact
    denominator: float
    applied: bool


def project_contact(first: CapsuleProxy, second: CapsuleProxy,
                    positions: MutableMapping[int, Vec3]) -> ContactProjection | None:
    """Project one penetrating contact into mutable owner positions."""

    if first.proxy_id > second.proxy_id:
        first, second = second, first
    contact = contact_for_pair(first, second, positions)
    if contact is None:
        return None
    weights: dict[int, tuple[float, float]] = {}
    endpoint_terms = (
        (first.a, -(1.0 - contact.parameter_a)),
        (first.b, -contact.parameter_a),
        (second.a, 1.0 - contact.parameter_b),
        (second.b, contact.parameter_b),
    )
    for endpoint, gradient in endpoint_terms:
        if endpoint.owner is None or endpoint.inverse_mass == 0.0:
            continue
        existing_gradient, existing_mass = weights.get(endpoint.owner, (0.0, endpoint.inverse_mass))
        if existing_mass != endpoint.inverse_mass:
            raise ContactError(f"owner {endpoint.owner}: inconsistent inverse mass")
        weights[endpoint.owner] = existing_gradient + gradient, existing_mass
    denominator = sum(mass * gradient * gradient for gradient, mass in weights.values())
    if denominator <= DISTANCE_EPSILON_SQUARED:
        return ContactProjection(contact, denominator, False)
    multiplier = contact.penetration / denominator
    updates: dict[int, Vec3] = {}
    for owner, (gradient, mass) in sorted(weights.items()):
        current = _vec3(positions[owner], f"positions[{owner}]")
        updates[owner] = _add(current, _scale(contact.normal, mass * gradient * multiplier))
    positions.update(updates)
    return ContactProjection(contact, denominator, True)


def uniform_grid_candidates(proxies: Sequence[CapsuleProxy], *,
                            cell_size: float = GRID_CELL_SIZE,
                            max_proxy_cells: int = MAX_PROXY_CELLS,
                            max_cell_entries: int = MAX_CELL_ENTRIES,
                            max_candidates: int = MAX_CANDIDATE_PAIRS) -> list[tuple[int, int]]:
    """Build deterministic canonical candidate pairs with fail-closed limits."""

    if not math.isfinite(cell_size) or cell_size <= 0.0:
        raise ContactError("cell_size must be finite and positive")
    ordered = sorted(proxies, key=lambda proxy: proxy.proxy_id)
    if any(first.proxy_id == second.proxy_id for first, second in zip(ordered, ordered[1:])):
        raise ContactError("proxy ids must be unique")
    cells: dict[tuple[int, int, int], list[int]] = {}
    entries = 0
    by_id = {proxy.proxy_id: proxy for proxy in ordered}
    for proxy in ordered:
        a, b = _proxy_points(proxy, None)
        minimum = tuple(math.floor((min(a[axis], b[axis]) - proxy.radius) / cell_size)
                        for axis in range(3))
        maximum = tuple(math.floor((max(a[axis], b[axis]) + proxy.radius) / cell_size)
                        for axis in range(3))
        counts = tuple(maximum[axis] - minimum[axis] + 1 for axis in range(3))
        covered = counts[0] * counts[1] * counts[2]
        if covered > max_proxy_cells:
            raise ContactError(f"proxy {proxy.proxy_id} covers {covered} cells; limit is {max_proxy_cells}")
        entries += covered
        if entries > max_cell_entries:
            raise ContactError(f"uniform grid has {entries} entries; limit is {max_cell_entries}")
        for x in range(minimum[0], maximum[0] + 1):
            for y in range(minimum[1], maximum[1] + 1):
                for z in range(minimum[2], maximum[2] + 1):
                    cells.setdefault((x, y, z), []).append(proxy.proxy_id)

    collected: set[tuple[int, int]] = set()
    for key in sorted(cells):
        ids = sorted(cells[key])
        for left in range(len(ids)):
            for right in range(left + 1, len(ids)):
                pair = ids[left], ids[right]
                if proxy_pair_allowed(by_id[pair[0]], by_id[pair[1]]):
                    collected.add(pair)
                    if len(collected) > max_candidates:
                        raise ContactError(
                            f"uniform grid has more than {max_candidates} candidates"
                        )
    pairs = sorted(collected)
    return pairs


def penetrating_contacts(proxies: Sequence[CapsuleProxy], pairs: Sequence[tuple[int, int]],
                         positions: MutableMapping[int, Vec3] | None = None, *,
                         max_contacts: int = MAX_CONTACTS) -> list[Contact]:
    by_id = {proxy.proxy_id: proxy for proxy in proxies}
    if len(by_id) != len(proxies):
        raise ContactError("proxy ids must be unique")
    canonical_pairs = sorted(set((min(a, b), max(a, b)) for a, b in pairs))
    contacts: list[Contact] = []
    for first_id, second_id in canonical_pairs:
        try:
            contact = contact_for_pair(by_id[first_id], by_id[second_id], positions)
        except KeyError as exc:
            raise ContactError(f"candidate references unknown proxy {exc.args[0]}") from exc
        if contact is not None:
            contacts.append(contact)
            if len(contacts) > max_contacts:
                raise ContactError(f"contact count exceeds limit {max_contacts}")
    return contacts


def _mean_displacement(positions: Sequence[Vec3], animated: Sequence[Vec3], where: str) -> Vec3:
    if not positions or len(positions) != len(animated):
        raise ContactError(f"{where}: positions and animated targets must have equal non-zero length")
    total = (0.0, 0.0, 0.0)
    for index, (position, target) in enumerate(zip(positions, animated)):
        total = _add(total, _sub(_vec3(position, f"{where}.positions[{index}]"),
                                 _vec3(target, f"{where}.animated[{index}]")))
    return _scale(total, 1.0 / len(positions))


def group_link_error(positions_a: Sequence[Vec3], animated_a: Sequence[Vec3],
                     positions_b: Sequence[Vec3], animated_b: Sequence[Vec3]) -> Vec3:
    return _sub(_mean_displacement(positions_b, animated_b, "group_b"),
                _mean_displacement(positions_a, animated_a, "group_a"))


def group_link_residual(positions_a: Sequence[Vec3], animated_a: Sequence[Vec3],
                        positions_b: Sequence[Vec3], animated_b: Sequence[Vec3]) -> float:
    return _length(group_link_error(positions_a, animated_a, positions_b, animated_b))


def group_link_tolerance(average_animated_segment_length: float) -> float:
    length = float(average_animated_segment_length)
    if not math.isfinite(length) or length < 0.0:
        raise ContactError("average animated segment length must be finite and non-negative")
    return 1.0e-5 * max(1.0, length)


@dataclass(frozen=True)
class GroupLinkProjection:
    residual_before: float
    residual_after: float
    correction: Vec3


def project_group_link(positions_a: MutableSequence[Vec3], animated_a: Sequence[Vec3],
                       positions_b: MutableSequence[Vec3], animated_b: Sequence[Vec3], *,
                       coupling: float = 1.0, max_correction: float = 0.25) -> GroupLinkProjection:
    """Project the frozen group-average displacement constraint in place."""

    coupling_value = float(coupling)
    correction_limit = float(max_correction)
    if not math.isfinite(coupling_value) or not 0.0 < coupling_value <= 1.0:
        raise ContactError("coupling must be in (0, 1]")
    if not math.isfinite(correction_limit) or not 0.0 < correction_limit <= 10.0:
        raise ContactError("max_correction must be in (0, 10]")
    error = group_link_error(positions_a, animated_a, positions_b, animated_b)
    residual_before = _length(error)
    requested = _scale(error, coupling_value)
    requested_length = _length(requested)
    correction = (_scale(requested, correction_limit / requested_length)
                  if requested_length > correction_limit else requested)
    count_a = len(positions_a)
    count_b = len(positions_b)
    alpha = count_b / (count_a + count_b)
    beta = count_a / (count_a + count_b)
    updated_a = [_add(_vec3(value, f"positions_a[{index}]"), _scale(correction, alpha))
                 for index, value in enumerate(positions_a)]
    updated_b = [_sub(_vec3(value, f"positions_b[{index}]"), _scale(correction, beta))
                 for index, value in enumerate(positions_b)]
    positions_a[:] = updated_a
    positions_b[:] = updated_b
    residual_after = group_link_residual(positions_a, animated_a, positions_b, animated_b)
    return GroupLinkProjection(residual_before, residual_after, correction)


__all__ = (
    "CapsuleProxy",
    "ClosestPoints",
    "Contact",
    "ContactError",
    "ContactProjection",
    "DISTANCE_EPSILON",
    "GRID_CELL_SIZE",
    "GroupLinkProjection",
    "MAX_CANDIDATE_PAIRS",
    "MAX_CELL_ENTRIES",
    "MAX_CONTACTS",
    "MAX_GLOBAL_PASSES",
    "MAX_PROXY_CELLS",
    "PROXY_DYNAMIC_COLLIDER",
    "PROXY_LINK_COLLISION",
    "PROXY_NODE",
    "ProxyEndpoint",
    "closest_points_segments",
    "contact_for_pair",
    "group_link_error",
    "group_link_residual",
    "group_link_tolerance",
    "penetrating_contacts",
    "project_contact",
    "project_group_link",
    "proxy_points",
    "proxy_pair_allowed",
    "uniform_grid_candidates",
)
