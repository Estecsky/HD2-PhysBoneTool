"""HD2PhysBoneTool Blender 4.x 插件入口。"""

import importlib
import sys

import bpy


if "_version" in globals():
    _version = importlib.reload(_version)
else:
    from . import version as _version

VERSION = _version.VERSION
VERSION_TEXT = _version.VERSION_TEXT

bl_info = {
    "name": "Helldivers 2 PhysBone Tool",
    "author": "AQ-Echoo,WisA9",
    "version": (0, 9, 0),
    "blender": (4, 0, 0),
    "location": "3D View > 侧边栏 > HD2 物理骨",
    "description": "为《Helldivers 2》制作、预览并导出物理骨配置",
    "warning": "开发中版本",
    "category": "Import-Export",
}


def _class_is_registered(cls):
    return bool(cls is not None and getattr(cls, "is_registered", False))


# Blender 覆盖安装或显式 reload 前，先让旧模块对象清理其拥有的 RNA、类、菜单和 handler。
_old_unregister = globals().get("unregister")
_old_physbone = globals().get("physbone_blender")
_old_classes = getattr(_old_physbone, "CLASSES", ()) if _old_physbone else ()
_old_state_present = bool(
    globals().get("_REGISTERED", False)
    or globals().get("_DIRTY", False)
    or any(
        module is not None and module.owned_state_present()
        for module in (globals().get("updates"), globals().get("preferences"))
    )
    or any(_class_is_registered(cls) for cls in _old_classes)
    or (
        _old_physbone is not None
        and callable(getattr(_old_physbone, "owned_state_present", None))
        and _old_physbone.owned_state_present()
    )
)
if callable(_old_unregister) and _old_state_present:
    try:
        _old_unregister()
    except Exception:
        old_subsystem_unregister = getattr(_old_physbone, "unregister", None)
        if not callable(old_subsystem_unregister):
            raise
        old_subsystem_unregister()
del _class_is_registered, _old_unregister, _old_physbone, _old_classes, _old_state_present


_SUBMODULE_ORDER = (
    "updates",
    "preferences",
    "physbone.blender",
)
for _module_name in _SUBMODULE_ORDER:
    _binding_name = _module_name.rsplit(".", 1)[-1]
    _qualified_name = f"{__name__}.{_module_name}"
    _module = sys.modules.get(_qualified_name)
    if _module is None:
        _module = importlib.import_module(f".{_module_name}", __name__)
    else:
        _module = importlib.reload(_module)
    globals()[_binding_name] = _module
del _binding_name, _qualified_name, _module_name, _module

# 保留 SDK 和外部脚本的访问入口；不再经过重复导入/重载的转发文件。
physbone_blender = blender
sys.modules[f"{__name__}.physbone_blender"] = physbone_blender


CLASSES = ()
_REGISTERED = False
_DIRTY = False


def _owned_state_present():
    return bool(physbone_blender.owned_state_present()
                or updates.owned_state_present() or preferences.owned_state_present())


def _cleanup_steps():
    return (updates.unregister, preferences.unregister, physbone_blender.unregister)


def register():
    """注册物理骨子系统及更新偏好；失败时撤销本轮已注册状态。"""

    global _DIRTY, _REGISTERED
    if _DIRTY:
        raise RuntimeError("上次 HD2 PhysBone Tool 清理不完整，请先重试禁用或重启 Blender")
    if _REGISTERED:
        return
    try:
        physbone_blender.register()
        preferences.register()
        updates.register(bl_info)
    except Exception as exc:
        rollback_errors = []
        for cleanup in _cleanup_steps():
            try:
                cleanup()
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        remaining = _owned_state_present()
        _REGISTERED = remaining
        _DIRTY = remaining
        if rollback_errors or remaining:
            details = " | ".join(rollback_errors) or "仍存在本插件拥有的 Blender 状态"
            raise RuntimeError(
                f"HD2 PhysBone Tool 注册失败且回滚不完整：{details}"
            ) from exc
        raise
    _REGISTERED = True
    _DIRTY = False


def unregister():
    """注销插件运行状态，不删除用户保存的 canonical 项目数据。"""

    global _DIRTY, _REGISTERED
    errors = []
    for cleanup in _cleanup_steps():
        try:
            cleanup()
        except Exception as exc:
            errors.append(str(exc))
    remaining = _owned_state_present()
    _REGISTERED = remaining
    _DIRTY = remaining
    if errors or remaining:
        details = " | ".join(errors) or "仍存在本插件拥有的 Blender 状态"
        raise RuntimeError("HD2 PhysBone Tool 注销不完整：" + details)


if __name__ == "__main__":
    register()
