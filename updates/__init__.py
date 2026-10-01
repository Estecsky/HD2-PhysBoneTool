"""Updater lifecycle isolated from model authoring modules."""
import importlib

for _name in ("addon_updater", "addon_updater_ops"):
    _module = globals().get(_name)
    if _module is None:
        _module = importlib.import_module(f".{_name}", __name__)
    else:
        _module = importlib.reload(_module)
    globals()[_name] = _module
del _name, _module

CLASSES = addon_updater_ops.classes


def owned_state_present():
    return addon_updater_ops.owned_state_present()


def register(bl_info):
    addon_updater_ops.register(bl_info)


def unregister():
    addon_updater_ops.unregister()
