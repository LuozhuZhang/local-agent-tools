"""Task identifiers, lookup metadata and private Task state paths."""

import os
from pathlib import Path
import re
import stat

from local_agent_tools.foundation.sanitize import Fault, redact


MAX_CHECKPOINT = 32768
INDEX_PAGE = 50
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def task_id(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise Fault("invalid_task_id", "Use 1-64 letters, digits, hyphens or underscores, beginning with a letter or digit.")
    return value


def metadata(title: str, anchors: list[str]) -> dict:
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or any(ord(c) < 32 for c in title):
        raise Fault("invalid_task", "Supply a nonempty single-line title of at most 200 characters.")
    if (not isinstance(anchors, list) or len(anchors) > 16
            or any(not isinstance(a, str) or not a or len(a) > 256 or any(ord(c) < 32 for c in a) for a in anchors)):
        raise Fault("invalid_task", "Use at most 16 explicit single-line lookup anchors of 1-256 characters.")
    if any(redact(value) != value for value in (title, *anchors)):
        raise Fault("invalid_task", "Keep credentials out of task metadata.")
    return {"title": title, "anchors": sorted(set(anchors))}


def private_path(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not kind or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Fault("corrupt_tasks", "Task state must use private, user-owned directories and regular files without links.")
    return info
