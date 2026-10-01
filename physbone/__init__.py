"""HD2 PhysBone 的 Blender 无关核心。

这里只公开扩展 HD2PHY1 的数据合同、校验和确定性序列化；不注册 Blender 类型、
操作符、面板或菜单。旧版 v1 字节布局已经移除，不提供兼容读取。
"""

from .core import Gate, build_v1, parse_v1, validate_v1, write_atomic

__all__ = (
    "Gate",
    "build_v1",
    "parse_v1",
    "validate_v1",
    "write_atomic",
)
