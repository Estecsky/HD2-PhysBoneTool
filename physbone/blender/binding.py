"""Project-local organization with full-matrix owner/bone following.

Child Of includes scale deliberately: disabling it changes HD2's rest/pose
contract under nonuniform armature and bone transforms. No inverse offset is
needed because bone targets use the head, unlike Blender's legacy Bone Parent.
"""
from mathutils import Matrix

NAME = "HD2PB · 骨架跟随"


def attach(obj, owner, bone_name="", *, preserve_legacy=False):
    if bone_name and owner.data.bones.get(bone_name) is None:
        raise ValueError(f"跟随骨骼不存在：{bone_name}")
    constraint = obj.constraints.get(NAME)
    if any(c != constraint for c in obj.constraints) or (constraint is not None and constraint.type != "CHILD_OF"):
        raise ValueError("物理骨跟随约束损坏或含额外约束，请重建编辑对象")
    if preserve_legacy and obj.parent == owner:
        obj.parent_type = "BONE" if bone_name else "OBJECT"
        obj.parent_bone = bone_name
        obj.location = (0, -owner.data.bones[bone_name].length, 0) if bone_name else (0, 0, 0)
        return
    obj.parent = None
    obj.parent_type = "OBJECT"
    obj.parent_bone = ""
    obj.matrix_parent_inverse = Matrix.Identity(4)
    obj.location = (0, 0, 0)
    constraint = obj.constraints.get(NAME)
    if constraint is not None and constraint.type != "CHILD_OF":
        raise ValueError("物理骨跟随约束类型损坏，请重建编辑对象")
    if constraint is None:
        constraint = obj.constraints.new("CHILD_OF")
        constraint.name = NAME
    constraint.target = owner
    constraint.subtarget = bone_name
    constraint.inverse_matrix = Matrix.Identity(4)
    constraint.influence = 1.0
    constraint.mute = False
    constraint.owner_space = "WORLD"
    constraint.target_space = "WORLD"
    constraint.set_inverse_pending = False
    for channel in ("location", "rotation", "scale"):
        for axis in "xyz":
            setattr(constraint, f"use_{channel}_{axis}", True)


def matches(obj, owner, bone_name="", *, allow_legacy=True):
    if allow_legacy and obj.parent == owner:
        return (not obj.constraints and obj.parent_type == ("BONE" if bone_name else "OBJECT")
                and obj.parent_bone == bone_name)
    constraints = list(obj.constraints)
    if obj.parent is not None or len(constraints) != 1:
        return False
    c = constraints[0]
    return (c.name == NAME and c.type == "CHILD_OF" and c.target == owner
            and c.subtarget == bone_name and not c.mute and abs(c.influence - 1) < 1e-6
            and c.owner_space == "WORLD" and c.target_space == "WORLD" and not c.set_inverse_pending
            and all(getattr(c, f"use_{channel}_{axis}") for channel in ("location", "rotation", "scale") for axis in "xyz")
            and all(abs(c.inverse_matrix[i][j] - float(i == j)) < 1e-6 for i in range(4) for j in range(4)))


def expected_location(obj, owner, bone_name=""):
    if obj.parent == owner and bone_name and owner.data.bones.get(bone_name):
        return (0, -owner.data.bones[bone_name].length, 0)
    return (0, 0, 0)
