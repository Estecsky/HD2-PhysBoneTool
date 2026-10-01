"""Read-only consumer inventory. Ambiguous dependencies disable dead-chain removal."""
from __future__ import annotations


def consumer_bones(owner):
    import bpy
    result = set()
    for data in (owner, owner.data):
        animation = getattr(data, 'animation_data', None)
        if animation and animation.drivers:
            return None  # Arbitrary driver expressions are not statically provable.
    for pose in owner.pose.bones:
        if pose.constraints:
            result.add(pose.name)
    for obj in bpy.data.objects:
        # Imported MMD rigid bodies describe the input simulation, not consumers
        # of the newly generated spring output. Leave those objects untouched.
        if getattr(obj, 'mmd_type', None) in {'RIGID_BODY', 'JOINT'}:
            continue
        animation = getattr(obj, 'animation_data', None)
        if animation and animation.drivers:
            return None
        if obj.parent == owner and obj.parent_type == 'BONE':
            result.add(obj.parent_bone)
        for constraint in obj.constraints:
            if constraint.type == 'ARMATURE':
                return None
            if getattr(constraint, 'target', None) == owner:
                name = getattr(constraint, 'subtarget', '')
                if not name:
                    return None
                result.add(name)
        # Pose constraints may target this owner from a different rig.
        if obj.type == 'ARMATURE':
            for pose in obj.pose.bones:
                for constraint in pose.constraints:
                    if constraint.type == 'ARMATURE':
                        return None
                    if getattr(constraint, 'target', None) == owner:
                        name = getattr(constraint, 'subtarget', '')
                        if not name:
                            return None
                        result.add(name)
        if obj.type != 'MESH':
            continue
        modifiers = [m for m in obj.modifiers if m.type == 'ARMATURE' and m.object == owner]
        if not modifiers and obj.parent != owner:
            continue
        if any(m.use_bone_envelopes for m in modifiers):
            return None
        groups = {g.index: g.name for g in obj.vertex_groups}
        for vertex in obj.data.vertices:
            for membership in vertex.groups:
                if membership.weight > 0 and membership.group in groups:
                    name = groups[membership.group]
                    if name not in owner.data.bones:
                        return None  # Alias/control-group semantics are ambiguous.
                    result.add(name)
    return sorted(result)
