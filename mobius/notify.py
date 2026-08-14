"""알림 — notify-send(있으면) + stdout/로그. macOS의 DistributedNotificationCenter 대체.

앱↔CLI IPC는 Linux CLI에선 불필요하므로 삭제하고, 사용자 통지만 남긴다.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

_HAS_NOTIFY_SEND = shutil.which("notify-send") is not None


def notify(title: str, body: str = "") -> None:
    line = f"[mobius] {title}" + (f" — {body}" if body else "")
    print(line, file=sys.stderr, flush=True)
    if _HAS_NOTIFY_SEND:
        try:
            subprocess.run(["notify-send", title, body], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
