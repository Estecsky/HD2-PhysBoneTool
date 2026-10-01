"""扩展 HD2PHY1 adapter 推荐生成的 canonical 字典形状。

这些 ``TypedDict`` 只用于静态说明和 IDE 提示，不会复制、清洗或重排调用方数据。
它们不是旧 producer 全部宽松输入的类型穷举：数字字符串、bool-as-int 和任意 truthiness
等宽松输入行为由对应 serializer 决定；当前 Blender adapter 使用 ``format_v1.build``。
"""

from __future__ import annotations

from typing import Literal, TypedDict


Vec3 = tuple[float, float, float] | list[float]
Basis3 = tuple[float, ...] | list[float]
LimitKind = Literal["NONE", "CONE", "HINGE", "SPHERICAL"]
ColliderKind = Literal["SPHERE", "CAPSULE", "PLANE"]
ReferenceMode = Literal[
    "ANIMATED_SEGMENT", "PARENT_REST", "EXPLICIT_LOCAL", "EXPLICIT_FRAME"
]


class BoneRequired(TypedDict):
    name: str
    palette_slot: int
    parent_slot: int
    rest_head: Vec3


class BoneInput(BoneRequired, total=False):
    rest_basis: Basis3 | None


class JointRequired(TypedDict):
    bone: str


class JointInput(JointRequired, total=False):
    override_collision_filter: bool
    group_mask: int
    collision_mask: int
    radius: float
    limit_kind: LimitKind
    limit_angle: float
    limit_angle2: float
    limit_axis: Vec3
    stiffness_scale: float
    damping_scale: float
    reference_mode: ReferenceMode
    reference_direction: Vec3
    reference_frame: tuple[float, float, float, float] | list[float]


class ChainRequired(TypedDict):
    name: str
    joints: list[JointInput]


class ChainInput(ChainRequired, total=False):
    stiffness: float
    damping: float
    gravity: Vec3
    wind: Vec3
    group_mask: int
    collision_mask: int
    root_swing: bool
    local_bend: bool


class ColliderRequired(TypedDict):
    name: str


class ColliderInput(ColliderRequired, total=False):
    kind: ColliderKind
    bone: str
    # Optional second fixed bone. With it, B is in that bone's local frame.
    bone_b: str
    group_mask: int
    collision_mask: int
    inside: bool
    dynamic: bool
    a: Vec3
    b: Vec3
    radius: float


class LinkCollisionRequired(TypedDict):
    local_joint_index: int
    radius: float
    group_mask: int
    collision_mask: int


class ChainLinkRequired(TypedDict):
    name: str
    chain_a: str
    chain_b: str


class ChainLinkInput(ChainLinkRequired, total=False):
    flags: int
    coupling: float
    max_correction: float
    collisions: list[LinkCollisionRequired]


class PhysicsDocumentRequired(TypedDict):
    unit_id: int | str
    palette_slots: int
    bones: list[BoneInput]
    chains: list[ChainInput]


class PhysicsDocument(PhysicsDocumentRequired, total=False):
    fixed_dt: float
    colliders: list[ColliderInput]
    chain_links: list[ChainLinkInput]


class PhysicsSummary(TypedDict):
    unit_id: str
    skeleton_fingerprint: str
    palette_slots: int
    target_bones: int
    chains: int
    joints: int
    colliders: int
    chain_links: int
    link_collisions: int
    capabilities: int
    payload_crc32: str
    bytes: int
