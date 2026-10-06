"""HD2 物理骨的Blender 集成入口与事务生命周期。"""

from __future__ import annotations

import importlib

import bpy

_MODULE_ORDER = (
    "addon_bridge",
    "blender_lifecycle",
    "pose_driver_properties",
    "properties",
    "binding",
    "ownership",
    "preview_geometry",
    "preview_nodes",
    "preview_layers",
    "preview",
    "controllers",
    "adapter_v1",
    "pose_drivers",
    "motion_preview",
    "pose_overlay",
    "operators",
    "diagnostics",
    "authoring_ui_ops",
    "collider_transform",
    "chain_profile_ops",
    "rest_pose",
    "ui_sidebar",
    "ui_object",
)

for _module_name in _MODULE_ORDER:
    _module = globals().get(_module_name)
    if _module is None:
        _module = importlib.import_module(f".{_module_name}", __name__)
    else:
        _module = importlib.reload(_module)
    globals()[_module_name] = _module
del _module_name, _module


RUNTIME_CLASSES = operators.CLASSES + authoring_ui_ops.CLASSES + collider_transform.CLASSES + chain_profile_ops.CLASSES + pose_drivers.CLASSES + motion_preview.CLASSES + ui_sidebar.CLASSES + ui_object.CLASSES
CLASSES = properties.CLASSES + RUNTIME_CLASSES

_REGISTERED_CLASSES = []
_REGISTERED = False
_DIRTY = False
_MENU_REGISTERED = False


def _draw_file_export(self, _context):
    self.layout.operator(
        "hd2pb.export_v1",
        text="HD2 物理与姿态独立配套更新 (.hd2irpack.zip)",
        icon="EXPORT",
    )


def owned_state_present():
    return bool(
        _REGISTERED_CLASSES
        or properties.owned_state_present()
        or preview.owned_state_present()
        or motion_preview.owned_state_present()
        or pose_overlay.owned_state_present()
        or controllers.owned_state_present()
        or operators.runtime_state_present()
        or _MENU_REGISTERED
        or _REGISTERED
        or _DIRTY
    )


def register():
    """按 RNA → preview runtime → classes → File Export 顺序事务注册。"""

    global _DIRTY, _MENU_REGISTERED, _REGISTERED
    if _DIRTY:
        raise RuntimeError("上次物理骨模块清理不完整，请先重试禁用或重启 Blender")
    if _REGISTERED:
        return
    operators.clear_runtime_state()
    try:
        properties.register()
        preview.register()
        motion_preview.register()
        pose_overlay.register()
        blender_lifecycle.register_classes(RUNTIME_CLASSES, _REGISTERED_CLASSES)
        bpy.types.TOPBAR_MT_file_export.append(_draw_file_export)
        _MENU_REGISTERED = True
    except Exception as exc:
        rollback_errors = []
        if _MENU_REGISTERED:
            try:
                bpy.types.TOPBAR_MT_file_export.remove(_draw_file_export)
                _MENU_REGISTERED = False
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        try:
            blender_lifecycle.unregister_classes(_REGISTERED_CLASSES)
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        try:
            pose_overlay.unregister()
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        try:
            motion_preview.unregister()
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        try:
            preview.unregister()
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        try:
            properties.unregister()
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        operators.clear_runtime_state()
        remaining = bool(
            _REGISTERED_CLASSES
            or properties.owned_state_present()
            or preview.owned_state_present()
            or motion_preview.owned_state_present()
            or pose_overlay.owned_state_present()
            or controllers.owned_state_present()
            or operators.runtime_state_present()
            or _MENU_REGISTERED
        )
        _REGISTERED = remaining
        _DIRTY = remaining
        if rollback_errors or remaining:
            details = " | ".join(rollback_errors) or "仍存在物理骨 Blender 注册状态"
            raise RuntimeError(f"物理骨模块注册失败且回滚不完整：{details}") from exc
        raise
    _REGISTERED = True
    _DIRTY = False


def unregister():
    """逆序清理菜单/类/handler/RNA，不删除用户 canonical 或生成投影。"""

    global _DIRTY, _MENU_REGISTERED, _REGISTERED
    errors = []
    operators.clear_runtime_state()
    try:
        controllers.restore_selection_visibility()
    except Exception as exc:
        errors.append(str(exc))
    if _MENU_REGISTERED:
        try:
            bpy.types.TOPBAR_MT_file_export.remove(_draw_file_export)
            _MENU_REGISTERED = False
        except Exception as exc:
            errors.append(str(exc))
    try:
        blender_lifecycle.unregister_classes(_REGISTERED_CLASSES)
    except Exception as exc:
        errors.append(str(exc))
    try:
        pose_overlay.unregister()
    except Exception as exc:
        errors.append(str(exc))
    try:
        motion_preview.unregister()
    except Exception as exc:
        errors.append(str(exc))
    try:
        preview.unregister()
    except Exception as exc:
        errors.append(str(exc))
    try:
        properties.unregister()
    except Exception as exc:
        errors.append(str(exc))
    remaining = bool(
        _REGISTERED_CLASSES
        or properties.owned_state_present()
        or preview.owned_state_present()
        or motion_preview.owned_state_present()
        or pose_overlay.owned_state_present()
        or controllers.owned_state_present()
        or operators.runtime_state_present()
        or _MENU_REGISTERED
    )
    _REGISTERED = remaining
    _DIRTY = remaining
    if errors or remaining:
        details = " | ".join(errors) or "仍存在物理骨 Blender 注册状态"
        raise RuntimeError("物理骨模块注销不完整：" + details)


__all__ = (
    "properties",
    "ownership",
    "preview_geometry",
    "preview_nodes",
    "preview",
    "controllers",
    "adapter_v1",
    "motion_preview",
    "operators",
    "ui_sidebar",
    "ui_object",
    "CLASSES",
    "RUNTIME_CLASSES",
    "owned_state_present",
    "register",
    "unregister",
)
