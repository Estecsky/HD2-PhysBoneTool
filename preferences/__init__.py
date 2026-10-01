"""Addon update preferences; identity follows the installed package root."""
import bpy
from ..updates import addon_updater_ops

ADDON_PACKAGE = __package__.rsplit(".", 1)[0]


class HD2PB_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_PACKAGE

    auto_check_update: bpy.props.BoolProperty(
        name="自动检查更新", description="打开插件偏好设置时按间隔检查；不会自动安装", default=False)
    updater_interval_months: bpy.props.IntProperty(name="月", default=0, min=0)
    updater_interval_days: bpy.props.IntProperty(name="天", default=7, min=0)
    updater_interval_hours: bpy.props.IntProperty(name="小时", default=0, min=0, max=23)
    updater_interval_minutes: bpy.props.IntProperty(name="分钟", default=0, min=0, max=59)

    def draw(self, context):
        layout = self.layout
        layout.label(text="稳定版本按版本标签检查", icon='INFO')
        layout.label(text="更新后请重启 Blender。")
        layout.label(text="本次会话用过物理预览时，请重启后先更新，再开启预览。", icon='ERROR')
        addon_updater_ops.check_for_update_background()
        addon_updater_ops.update_settings_ui(self, context)


CLASSES = (HD2PB_AddonPreferences,)
_REGISTERED_CLASSES = []


def owned_state_present():
    return bool(_REGISTERED_CLASSES)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
        _REGISTERED_CLASSES.append(cls)


def unregister():
    for cls in reversed(tuple(_REGISTERED_CLASSES)):
        if getattr(cls, "is_registered", False):
            bpy.utils.unregister_class(cls)
        _REGISTERED_CLASSES.remove(cls)
