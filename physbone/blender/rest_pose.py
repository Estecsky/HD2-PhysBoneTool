"""Rebase authored physics after a caller bakes a new skeleton Rest pose.

The caller supplies old-local -> posed-armature matrices, excluding axis-only
Rest edits. No preview is generated and no source project or weights are changed.
"""
import math

from mathutils import Matrix, Quaternion, Vector

from . import ownership, preview, properties
from .transfer import required_bones


def _rigid(matrix, label):
    if not all(math.isfinite(v) for row in matrix for v in row):
        raise ownership.HD2PBError(f"{label}：非有限物理姿态矩阵")
    basis = matrix.to_3x3()
    if (abs(basis.determinant() - 1.0) > 1e-4
            or any(abs(basis.col[i].length - 1.0) > 1e-4 for i in range(3))
            or any(abs(basis.col[i].dot(basis.col[j])) > 1e-4
                   for i in range(3) for j in range(i))
            or any(abs(matrix[3][i] - float(i == 3)) > 1e-5 for i in range(4))):
        raise ownership.HD2PBError(f"{label}：物理姿态烘焙暂不支持缩放、镜像或剪切")
    return matrix


class RestPoseEdit:
    """Narrow Batch/PhysBone interface; caller owns skeleton/mesh rollback."""

    def __init__(self, owner):
        self.owner = owner
        self.project = ownership.ensure_authoring_owner(owner)
        issues = ownership.canonical_issues(owner)
        if issues:
            raise ownership.HD2PBError("姿态转换前物理数据无效：" + "；".join(issues[:5]))
        self.required_bones = frozenset(n for n in required_bones(owner) if n)
        self.old_rest = {name: _rigid(owner.data.bones[name].matrix_local.copy(), name)
                         for name in self.required_bones}
        self.original = []
        for chain in self.project.chains:
            for node in chain.nodes:
                for field in ('limit_axis', 'reference_direction', 'reference_frame'):
                    self.original.append((node, field, tuple(getattr(node, field))))
        for collider in self.project.colliders:
            for field in ('a', 'b'):
                self.original.append((collider, field, tuple(getattr(collider, field))))

    @staticmethod
    def suspend_updates():
        return preview.suspend_updates()

    def restore(self):
        with preview.suspend_updates():
            for item, field, value in self.original:
                setattr(item, field, value)

    def apply(self, posed_rest):
        # Construct every write before modifying RNA. Missing/non-rigid inputs
        # must never leave part of a physics project in the new coordinate space.
        poses, final = {}, {}
        for name in self.required_bones:
            if name not in posed_rest or name not in self.owner.data.bones:
                raise ownership.HD2PBError(f"姿态转换缺少物理骨：{name}")
            poses[name] = _rigid(posed_rest[name], name)
            final[name] = _rigid(self.owner.data.bones[name].matrix_local, name)
        if all(final[name] == self.old_rest[name] and poses[name] == self.old_rest[name]
               for name in self.required_bones):
            return
        writes = []
        for chain in self.project.chains:
            for index, node in enumerate(chain.nodes):
                if not index:
                    continue
                name = chain.nodes[index - 1].bone_name
                basis = (final[name].inverted() @ poses[name]).to_3x3()
                for field in ('limit_axis', 'reference_direction'):
                    writes.append((node, field, tuple(basis @ Vector(getattr(node, field)))))
                x, y, z, w = node.reference_frame
                q = basis.to_quaternion() @ Quaternion((w, x, y, z))
                q.normalize()
                writes.append((node, 'reference_frame', (q.x, q.y, q.z, q.w)))
        for collider in self.project.colliders:
            name = collider.bone_name
            # Unbound colliders live in armature-object space.
            old = self.old_rest[name] if name else Matrix.Identity(4)
            posed = poses[name] if name else Matrix.Identity(4)
            inverse = final[name].inverted() if name else Matrix.Identity(4)
            writes.append((collider, 'a', tuple(inverse @ posed @ Vector(collider.a))))
            if collider.kind == 'PLANE':
                b = (inverse @ posed).to_3x3() @ Vector(collider.b)
            elif collider.kind == 'CAPSULE' and collider.bone_name_b:
                # Canonical B is stored in first-bone space, but physically
                # follows the second bone. Bake its motion independently.
                second = collider.bone_name_b
                b = (inverse @ poses[second] @ self.old_rest[second].inverted()
                     @ old @ Vector(collider.b))
            else:
                b = inverse @ posed @ Vector(collider.b)
            writes.append((collider, 'b', tuple(b)))
        if not all(math.isfinite(v) for _, _, values in writes for v in values):
            raise ownership.HD2PBError("姿态转换产生非有限物理坐标")
        try:
            with preview.suspend_updates():
                self._write(writes)
                issues = ownership.canonical_issues(self.owner)
                if issues:
                    raise ownership.HD2PBError("姿态转换后物理无效：" + "；".join(issues[:5]))
                # Existing hidden previews stay hidden; absent previews stay absent.
                preview.mark_rna_dirty(self.project, properties.ENTITY_PROJECT, preview.ASPECT_ALL)
        except Exception:
            self.restore()
            raise

    @staticmethod
    def _write(writes):
        for item, field, value in writes:
            setattr(item, field, value)
