"""Local, bounded native material versions; never dereference network locations."""
import hashlib
import os
from pathlib import Path
import re
import stat


def versions(directory, references):
    result = {}
    for ref in references:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", ref):
            result[ref] = ref[7:]
        elif ref.startswith("file:"):
            result[ref] = file_hash(Path(directory) / "evidence", ref[5:])
    if not result:
        raise ValueError("原生材料必须含file:相对文件或sha256:不可变版本引用")
    return result


def file_hash(root, name):
    relative = Path(name)
    if (relative.is_absolute() or not relative.parts or any(p in {".", ".."} or p.startswith(".") for p in relative.parts)
            or relative.suffix not in {".txt", ".md", ".json", ".log"}):
        raise ValueError("材料仅接受evidence目录中的非隐藏文本文件")
    # Walk by directory descriptors: no symlink traversal or check/open race.
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 1024 * 1024:
                raise ValueError("材料须为不超过1MiB的独立普通文件")
            content = source.read(1024 * 1024 + 1)
            after = os.fstat(source.fileno())
            if len(content) > 1024 * 1024 or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("材料读取中发生变化")
            return hashlib.sha256(content).hexdigest()
    finally:
        os.close(descriptor)
