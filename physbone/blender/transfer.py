"""Transfer one canonical physics project through an explicit bone-name map."""
from __future__ import annotations

from mathutils import Matrix, Vector
from . import ownership, preview
from .mmd_conversion import _uniform


def required_bones(owner, *, for_transfer=False):
    project = owner.data.hd2pb_project
    if for_transfer:
        values, _ = _pruned_snapshot(owner)
        result = {n['bone_name'] for c in values['chains'] for n in c['nodes']}
        for collider in values['colliders']:
            result.update(n for n in (collider['bone_name'], collider['bone_name_b']) if n)
        return result
    result = {n.bone_name for c in project.chains for n in c.nodes}
    for collider in project.colliders:
        result.update(n for n in (collider.bone_name, collider.bone_name_b) if n)
    return result


def _pruned_snapshot(owner, available=None, *, consumers=...):
    from .operators import _snapshot_project
    from .pruning import consumer_bones
    from ..core.pruning import prune
    parents = {b.name: b.parent.name if b.parent else None for b in owner.data.bones}
    return prune(_snapshot_project(owner.data.hd2pb_project), parents,
                 consumer_bones(owner) if consumers is ... else consumers, available)


def transfer_project(source, target, mapping, *, source_consumers=...):
    """Copy, rebind local frames/radii, re-ID; leave source canonical untouched."""
    from .operators import _snapshot_project, _restore_project, _regenerate_project_uuids
    src = source.data.hd2pb_project
    dst = target.data.hd2pb_project
    ownership.ensure_authoring_owner(source)
    if dst.schema_version or dst.chains or dst.colliders:
        raise ownership.HD2PBError("目标骨架已有物理项目；不自动覆盖")
    if len(ownership.armature_objects(target.data)) != 1:
        raise ownership.HD2PBError("目标骨架须为单用户数据")
    available = {name for name, destination in mapping.items()
                 if name in source.data.bones and destination in target.data.bones}
    frame = target.matrix_world.inverted() @ source.matrix_world
    scale = _uniform(frame, "源/目标骨架")
    old = _snapshot_project(dst)
    values, pruning = _pruned_snapshot(source, available, consumers=source_consumers)
    if not values['chains']:
        # Do not create an invalid empty HD2PHY1 project or block mesh rebinding.
        import json
        target['HD2PB_TransferPruningReport'] = json.dumps(pruning, ensure_ascii=False, sort_keys=True)
        return {'chains': 0, 'colliders': 0, 'scale': scale, 'pruning': pruning}
    values["authoring_object"], values["controller_collection"] = target, None
    def local_frame(name):
        src_rest = source.data.bones[name].matrix_local if name else Matrix.Identity(4)
        dst_rest = target.data.bones[mapping[name]].matrix_local if name else Matrix.Identity(4)
        return dst_rest.inverted() @ frame @ src_rest
    for chain in values["chains"]:
        old_nodes = [n["bone_name"] for n in chain["nodes"]]
        for i, node in enumerate(chain["nodes"]):
            node["bone_name"] = mapping[node["bone_name"]]
            node["radius"] *= scale
            if i:
                basis = local_frame(old_nodes[i - 1]).to_3x3().normalized()
                for field in ("limit_axis", "reference_direction"):
                    node[field] = tuple(basis @ Vector(node[field]))
                x, y, z, w = node["reference_frame"]
                from mathutils import Quaternion
                q = basis.to_quaternion() @ Quaternion((w, x, y, z))
                node["reference_frame"] = (q.x, q.y, q.z, q.w)
    for collider in values["colliders"]:
        transform = local_frame(collider["bone_name"])
        collider["a"] = tuple(transform @ Vector(collider["a"]))
        collider["b"] = tuple((transform.to_3x3().normalized() @ Vector(collider["b"]))
                              if collider["kind"] == "PLANE" else transform @ Vector(collider["b"]))
        collider["radius"] *= scale
        for field in ("bone_name", "bone_name_b"):
            if collider[field]:
                collider[field] = mapping[collider[field]]
    for link in values["chain_links"]:
        link["max_correction"] *= scale
        for collider in link["collisions"]:
            collider["radius"] *= scale
    try:
        with preview.suspend_updates():
            _restore_project(dst, values)
            _regenerate_project_uuids(dst)
            issues = ownership.canonical_issues(target)
            if issues:
                raise ownership.HD2PBError("转移后物理项目无效：" + "；".join(issues[:5]))
            if "HD2PB_MMDConversionReport" in source:
                target["HD2PB_MMDConversionReport"] = source["HD2PB_MMDConversionReport"]
            if "HD2PB_VRMConversionReport" in source:
                target["HD2PB_VRMConversionReport"] = source["HD2PB_VRMConversionReport"]
    except Exception:
        _restore_project(dst, old)
        raise
    import json
    target['HD2PB_TransferPruningReport'] = json.dumps(pruning, ensure_ascii=False, sort_keys=True)
    return {"chains": len(dst.chains), "colliders": len(dst.colliders), "scale": scale,
            'pruning': pruning}
