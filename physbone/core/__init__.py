"""HD2 物理骨的稳定、无 ``bpy`` 公共入口。

当前开发合同唯一使用扩展后的 ``HD2PHY1``。旧版 80/128 字节 v1 文件不再
兼容，但保留 ``build_v1`` 等原始 Python 名称，避免 Blender 适配层和离线工具
因为接口名称变化而失效。
"""

from . import contact
from .format_v1 import (
    Gate,
    build as build_v1,
    parse as parse_v1,
    validate as validate_v1,
    write_atomic,
)

__all__ = (
    "Gate",
    "build_v1",
    "parse_v1",
    "validate_v1",
    "write_atomic",
    "contact",
)
