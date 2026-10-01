"""Bounded transactional installer for one Blender addon directory."""
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile


class InstallError(RuntimeError):
    pass


def paths(root, stage):
    root, stage = Path(root).absolute(), Path(stage).absolute()
    if root.resolve() != root or stage.resolve() != stage or root.is_symlink() or stage.is_symlink():
        raise InstallError('更新路径不能经过链接')
    if not all((root / name).is_file() for name in ('__init__.py', 'version.py')):
        raise InstallError('更新目标不是插件目录')
    if stage.parent != root:
        raise InstallError('暂存目录必须是插件的直接子目录')
    return root, stage


def relative(name):
    if not name or any(char in name for char in ('\\', ':', '\x00')):
        raise InstallError('压缩包包含非法路径')
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in ('', '.', '..') for p in name.rstrip('/').split('/')):
        raise InstallError('压缩包路径越界')
    for part in path.parts:
        stem = part.split('.')[0].upper()
        if part.rstrip(' .') != part or stem in {'CON', 'PRN', 'AUX', 'NUL'} or (
                len(stem) == 4 and stem[:3] in {'COM', 'LPT'} and stem[3] in '123456789'):
            raise InstallError('压缩包包含不安全文件名')
    return path


def extract_package(archive, destination, stage_name):
    destination = Path(destination)
    seen, entries, roots, total = set(), [], set(), 0
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.infolist()) > 100000:
            raise InstallError('压缩包文件数量超限')
        for info in bundle.infolist():
            path = relative(info.filename)
            roots.add(path.parts[0])
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise InstallError('压缩包含链接或特殊文件')
            if len(path.parts) == 1:
                if not info.is_dir():
                    raise InstallError('压缩包需要一个顶层插件目录')
                continue
            rel = Path(*path.parts[1:])
            if rel.parts[0].casefold() in {stage_name.casefold(), '.git'}:
                raise InstallError('压缩包不能覆盖暂存目录')
            key = rel.as_posix().casefold()
            if key in seen:
                raise InstallError('压缩包路径重复')
            seen.add(key)
            total += info.file_size
            if total > 1024 ** 3:
                raise InstallError('更新包超过 1 GiB')
            entries.append((info, rel))
        files = {p.as_posix() for info, p in entries if not info.is_dir()}
        if len(roots) != 1 or not {'__init__.py', 'version.py'} <= files:
            raise InstallError('压缩包必须包含唯一插件根和版本文件')
        for info, rel in entries:
            target = destination / rel
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
    return destination


def files_in(directory, exclude=()):
    directory = Path(directory)
    result = {}
    for base, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if name not in exclude]
        for name in dirs + files:
            path = Path(base) / name
            if path.is_symlink() or path.resolve() != path.absolute():
                raise InstallError('插件文件树含链接')
        for name in files:
            path = Path(base) / name
            result[path.relative_to(directory).as_posix()] = path
    return result


def make_backup(root, stage):
    root, stage = paths(root, stage)
    stage.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='backup-build-', dir=stage) as temp:
        candidate, prior = Path(temp) / 'backup', Path(temp) / 'previous'
        candidate.mkdir()
        for name, source in files_in(root, (stage.name, '.git', '__pycache__')).items():
            dest = candidate / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        backup = stage / 'backup'
        if backup.is_symlink() or backup.resolve() != backup:
            raise InstallError('备份不能是链接')
        if backup.exists():
            os.replace(backup, prior)
        try:
            os.replace(candidate, backup)
        except BaseException:
            if prior.exists():
                os.replace(prior, backup)
            raise
    return backup


def transaction(root, stage, incoming, *, deletes=(), status_path=None, status=None):
    root, stage = paths(root, stage)
    incoming = dict(incoming)
    names = set(incoming) | set(deletes)
    status_name = None
    if status_path is not None:
        status_path = Path(status_path).absolute()
        if status_path.parent != stage:
            raise InstallError('状态文件路径不属于暂存目录')
        status_name = status_path.relative_to(root).as_posix()
        names.add(status_name)
    targets = {}
    for name in names:
        target = root / relative(name).as_posix()
        if target.resolve() != target or not target.is_relative_to(root):
            raise InstallError('安装目标经过链接或越界')
        if target.exists() and not target.is_file():
            raise InstallError('安装目标与现有目录冲突')
        targets[name] = target
    stage.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='transaction-', dir=stage) as temp:
        temp = Path(temp)
        snapshots, prepared, made_dirs, changed = {}, {}, [], []
        for index, name in enumerate(sorted(names)):
            target = targets[name]
            old = temp / ('old-' + str(index)) if target.exists() else None
            if old is not None:
                shutil.copy2(target, old)
            snapshots[name] = old
            if name in incoming or name == status_name:
                new = temp / ('new-' + str(index))
                if name == status_name:
                    new.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
                else:
                    shutil.copy2(incoming[name], new)
                prepared[name] = new
        order = sorted(names - ({status_name} if status_name else set()))
        if status_name:
            order.append(status_name)
        try:
            for name in order:
                target = targets[name]
                missing, parent = [], target.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for parent in reversed(missing):
                    parent.mkdir()
                    made_dirs.append(parent)
                if name in prepared:
                    os.replace(prepared[name], target)
                    changed.append(name)
                elif target.exists():
                    target.unlink()
                    changed.append(name)
        except BaseException as failure:
            errors = []
            for name in reversed(changed):
                try:
                    if snapshots[name] is None:
                        targets[name].unlink(missing_ok=True)
                    else:
                        os.replace(snapshots[name], targets[name])
                except OSError as error:
                    errors.append(str(error))
            for parent in reversed(made_dirs):
                try:
                    parent.rmdir()
                except OSError:
                    pass
            if errors:
                raise InstallError('回滚不完整，请从备份恢复：' + '; '.join(errors)) from failure
            raise InstallError('安装失败，全部文件已回滚：' + str(failure)) from failure
    return [name for name in incoming if snapshots[name] is None]

