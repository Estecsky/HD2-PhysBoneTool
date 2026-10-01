"""Blender 集成层的 class 注册与注销事务辅助。"""

import bpy


def register_classes(classes, owned_classes):
    """按顺序注册；中途失败时只回滚本轮已经注册的类。"""

    if owned_classes:
        raise RuntimeError("当前模块仍持有已注册的 Blender 类，必须先完成注销")
    try:
        for cls in classes:
            bpy.utils.register_class(cls)
            owned_classes.append(cls)
    except Exception as exc:
        try:
            unregister_classes(owned_classes)
        except Exception as rollback_exc:
            raise RuntimeError(
                f"Blender 类注册失败，且回滚不完整：{rollback_exc}"
            ) from exc
        raise


def unregister_classes(owned_classes):
    """只注销本模块实际注册的类；失败项保留所有权以便重试。"""

    errors = []
    remaining = []
    for cls in reversed(tuple(owned_classes)):
        try:
            bpy.utils.unregister_class(cls)
        except Exception as exc:
            errors.append(f"{cls.__name__}: {exc}")
            remaining.append(cls)
    owned_classes[:] = [
        cls for cls in owned_classes if any(cls is item for item in remaining)
    ]
    if errors:
        raise RuntimeError("Blender 类注销不完整：" + "; ".join(errors))
