"""Validated native SpringBone preview; no second Python physics solver.

The standalone library compiles the same physics.cpp as the game addon. This
module only creates a temporary single-profile Rig, marshals matrices and owns
the native token. Temporary indices never become export mappings on the model.
"""
from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import struct
import tempfile
import zlib

from . import format_v1


class PreviewError(ValueError):
    pass


def _matrix(bone):
    basis = bone.get("rest_basis") or (1., 0., 0., 0., 1., 0., 0., 0., 1.)
    return tuple(value for row in range(3)
                 for value in (*basis[row * 3:row * 3 + 3], bone["rest_head"][row]))


def _relative(parent, child):
    # Rest transforms are validated by the native receiver before simulation.
    delta = [child[r * 4 + 3] - parent[r * 4 + 3] for r in range(3)]
    return tuple(value for r in range(3) for value in (
        *(sum(parent[k * 4 + r] * child[k * 4 + c] for k in range(3)) for c in range(3)),
        sum(parent[k * 4 + r] * delta[k] for k in range(3))))


def build_preview_rig(document):
    """Return binary plus parent-first bone names and Rest matrices."""
    format_v1.validate(document)
    rows = {bone["palette_slot"]: bone for bone in document["bones"]}
    ordered, visiting, done = [], set(), set()

    def visit(slot):
        if slot in done:
            return
        if slot in visiting:
            raise PreviewError("预览骨架父链循环")
        visiting.add(slot)
        parent = rows[slot]["parent_slot"]
        if parent >= 0:
            visit(parent)
        visiting.remove(slot)
        ordered.append(rows[slot])
        done.add(slot)

    for slot in sorted(rows):
        visit(slot)
    index = {bone["palette_slot"]: i for i, bone in enumerate(ordered)}
    matrices = [_matrix(bone) for bone in ordered]
    identity = (1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0.)
    name64 = lambda name: format_v1._name64(name, "preview bone")
    payload = struct.pack("<64siI12f", name64("__HD2PB_PREVIEW_SOURCE__"), -1, 1, *identity)
    payload += struct.pack("<QIIiII", format_v1.parse_unit_id(document["unit_id"]),
                           len(ordered), 16, 0, document["palette_slots"], 0)
    for i, bone in enumerate(ordered):
        parent = index[bone["parent_slot"]] if bone["parent_slot"] >= 0 else -1
        local = _relative(matrices[parent], matrices[i]) if parent >= 0 else matrices[i]
        payload += struct.pack("<64siiIIf12f", name64(bone["name"]), parent, -1,
                               bone["palette_slot"], 0x100 if parent >= 0 else 0, 1., *local)
    payload += bytes(16)
    header = struct.pack("<8s6I", b"HD2IRG1\0", 1, 32, 1, 1, zlib.crc32(payload), 0)
    return header + payload, [bone["name"] for bone in ordered], matrices


_LIBRARIES = {}


def _library(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise PreviewError("未安装同求解器预览组件：" + str(path))
    key = str(path)
    if key not in _LIBRARIES:
        library = ctypes.CDLL(key)
        u32, u64 = ctypes.c_uint32, ctypes.c_uint64
        float_p, char_p = ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_char)
        library.hd2pb_preview_version.argtypes = []
        library.hd2pb_preview_version.restype = u32
        if library.hd2pb_preview_version() != 1:
            raise PreviewError("预览组件 ABI 版本不匹配")
        library.hd2pb_preview_open.argtypes = [ctypes.c_char_p, char_p, u32]
        library.hd2pb_preview_open.restype = u64
        library.hd2pb_preview_close.argtypes = [u64]
        library.hd2pb_preview_close.restype = None
        library.hd2pb_preview_step.argtypes = [u64, u64, ctypes.c_double, float_p, float_p,
                                              u32, float_p, u32, ctypes.POINTER(u64), char_p, u32]
        library.hd2pb_preview_step.restype = u32
        if hasattr(library, 'hd2pb_preview_trace') and hasattr(library, 'hd2pb_preview_residual'):
            library.hd2pb_preview_trace.argtypes = [u64, u32]
            library.hd2pb_preview_trace.restype = u32
            library.hd2pb_preview_residual.argtypes = [u64, float_p, u32, ctypes.POINTER(u32), u32]
            library.hd2pb_preview_residual.restype = u32
        if hasattr(library, 'hd2pb_preview_fallback_chains'):
            library.hd2pb_preview_fallback_chains.argtypes = [u64, ctypes.POINTER(u32), u32]
            library.hd2pb_preview_fallback_chains.restype = u32
        _LIBRARIES[key] = library
    return _LIBRARIES[key]


class NativePreview:
    def __init__(self, document, *, library_path=None, cache_root=None):
        library_path = library_path or Path(__file__).resolve().parents[2] / "bin" / "HD2PhysBonePreview.dll"
        self.library = _library(library_path)
        self.token = 0
        self.frame = 0
        self.temporary = None
        self.message = ""
        self.counters = (0, 0, 0, 0)
        rig, self.names, self.rest = build_preview_rig(document)
        physical, self.metadata = format_v1.build(document)
        self.wire = format_v1.parse(physical)
        cache_root = cache_root or os.environ.get("HD2PB_PREVIEW_CACHE")
        if cache_root:
            Path(cache_root).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="hd2pb_preview_", dir=cache_root)
        directory = Path(self.temporary.name)
        try:
            (directory / "preview.rigbin").write_bytes(rig)
            (directory / "preview.hd2phys").write_bytes(physical)
            self.reset()
        except BaseException:
            self.close()
            raise

    def reset(self):
        if not self.temporary:
            raise PreviewError("预览会话已关闭")
        error = ctypes.create_string_buffer(2048)
        token = self.library.hd2pb_preview_open(str(self.temporary.name).encode("utf-8"), error, len(error))
        if not token:
            raise PreviewError(error.value.decode("utf-8", errors="replace"))
        if self.token:
            self.library.hd2pb_preview_close(self.token)
        self.token, self.frame = token, 0

    def enable_diagnostics(self, enabled=True):
        return bool(hasattr(self.library, 'hd2pb_preview_trace') and
                    self.library.hd2pb_preview_trace(self.token, int(enabled)))

    def diagnostics(self):
        if not hasattr(self.library, 'hd2pb_preview_residual'):
            return None
        values, indices = (ctypes.c_float * 4)(), (ctypes.c_uint32 * 10)()
        if not self.library.hd2pb_preview_residual(self.token, values, 4, indices, 10):
            return None
        result = dict(residuals=list(values), indices=list(indices), locations=[])
        wire = getattr(self, 'wire', None)
        if wire is None:
            result['unavailable'] = '缺少实际序列化表，未将索引猜测为名称'
            return result
        chains, joints = wire['chains'], wire['joints']
        if hasattr(self.library, 'hd2pb_preview_fallback_chains'):
            count = self.library.hd2pb_preview_fallback_chains(self.token, None, 0)
            if count > len(chains):raise PreviewError('局部回退链数量越界')
            rows = (ctypes.c_uint32 * count)()
            if count:
                if self.library.hd2pb_preview_fallback_chains(self.token, rows, count) != count:
                    raise PreviewError('局部回退诊断在读取时变更')
            result['fallback_chains'] = [chains[index]['name'] for index in rows]
        def node(chain_index, local):
            chain = chains[chain_index]
            if local >= chain['count']:raise PreviewError('诊断节点索引越界')
            return f"{chain['name']} / {joints[chain['start'] + local]['bone']}"
        static = [c for c in wire['colliders'] if not c['dynamic']]
        # Mirror build_proxies' ordering ONLY, using the round-tripped wire
        # tables. No geometry/physics is implemented here.
        proxies = []
        contact_chains = chains if (wire['link_collisions'] or any(c['dynamic'] for c in wire['colliders'])) else []
        for ci, chain in enumerate(contact_chains):
            for local in range(1, chain['count']):
                joint = joints[chain['start'] + local]
                filtered = joint if joint['override_collision_filter'] else chain
                if filtered['collision_mask'] == 0 or joint['radius'] <= 0:continue
                proxies.append(f"节点 {node(ci, local)}")
        proxies.extend('动态碰撞体 '+c['name'] for c in wire['colliders'] if c['dynamic'])
        for link in wire['chain_links']:
            for collision in link['collisions']:
                local = collision['local_joint_index']
                proxies.append(f"连接碰撞 {node(link['chain_a_index'], local)} ↔ {node(link['chain_b_index'], local)}")
        def add(kind, value, where):
            if value > 1.e-5:
                result['locations'].append(dict(kind=kind, residual_m=value, where=where))
        if values[0] > 1.e-5:add('长度／角度约束', values[0], node(indices[0], indices[1]))
        if values[1] > 1.e-5:add('节点碰撞', values[1], node(indices[2], indices[3])+' ↔ '+static[indices[4]]['name'])
        if values[2] > 1.e-5:add('节点／动态接触', values[2], proxies[indices[5]]+' ↔ '+proxies[indices[6]])
        if values[3] > 1.e-5:
            link = wire['chain_links'][indices[7]]
            add('链间连接约束', values[3], link['chain_a']+' ↔ '+link['chain_b'])
        return result

    def step(self, root, animated, seconds, *, frame=None):
        if not self.token:
            raise PreviewError("预览会话已关闭")
        if len(root) != 12 or len(animated) != len(self.names) or any(len(row) != 12 for row in animated):
            raise PreviewError("预览矩阵数量不匹配")
        if not math.isfinite(seconds) or seconds < 0:
            raise PreviewError("预览时间无效")
        next_frame = self.frame + 1 if frame is None else frame
        if not isinstance(next_frame, int) or not 1 <= next_frame <= 0xFFFFFFFFFFFFFFFF:
            raise PreviewError("预览帧号无效")
        root_buffer = (ctypes.c_float * 12)(*root)
        input_buffer = (ctypes.c_float * (12 * len(animated)))(*(v for row in animated for v in row))
        output = (ctypes.c_float * len(input_buffer))()
        counters = (ctypes.c_uint64 * 4)()
        error = ctypes.create_string_buffer(2048)
        status = self.library.hd2pb_preview_step(self.token, next_frame, seconds, root_buffer,
            input_buffer, len(animated), output, len(animated), counters, error, len(error))
        self.message = error.value.decode("utf-8", errors="replace")
        self.counters = tuple(counters)
        if not status:
            raise PreviewError(self.message or "物理预览拒绝此帧")
        self.frame = next_frame
        return [tuple(output[i:i + 12]) for i in range(0, len(output), 12)], status

    def close(self):
        if self.token:
            self.library.hd2pb_preview_close(self.token)
            self.token = 0
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
