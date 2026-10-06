"""HD2PhysBoneTool 的唯一版本源。"""

VERSION = (0, 9, 2)
PRERELEASE = ()
# PRERELEASE = ("dev", 1)


def _format_version():
    base = ".".join(str(part) for part in VERSION)
    return f"{base}-{PRERELEASE[0]}.{PRERELEASE[1]}" if PRERELEASE else base


VERSION_TEXT = _format_version()
PACKAGE_NAME = "HD2PhysBoneTool"
