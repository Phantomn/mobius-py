"""파일 원자 쓰기 헬퍼 — temp 파일에 쓰고 os.replace 로 교체(부분 쓰기 방지)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_atomic(data: bytes, path: Path, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mobius-", suffix=".tmp")
    try:
        os.write(fd, data)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, str(path))
    os.chmod(str(path), mode)
