"""Codex ChatGPT 계정 사용량 조회.

Codex CLI가 사용하는 ChatGPT backend 경로를 읽는다. 응답 형식은 Codex의
rate_limit.primary_window/secondary_window 구조에 맞추되, 알 수 없는 필드는 무시한다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .codex_config import CodexAuth

ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"


class Unauthorized(Exception):
    """Codex 토큰이 거부됐다."""


@dataclass(frozen=True)
class UsageWindow:
    name: str
    used_percent: float
    resets_at: Optional[float]
    window_minutes: Optional[int]


@dataclass(frozen=True)
class CodexUsageSnapshot:
    plan_type: Optional[str]
    windows: tuple[UsageWindow, ...]
    fetched_at: float

    def account_reset_after(self, now: float) -> Optional[float]:
        resets = [w.resets_at for w in self.windows
                  if w.used_percent >= 100 and w.resets_at is not None and w.resets_at > now]
        return max(resets) if resets else None


def _window(name: str, value: object) -> Optional[UsageWindow]:
    if not isinstance(value, dict):
        return None
    used = value.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    reset = value.get("reset_at")
    if isinstance(reset, str):
        try:
            from datetime import datetime
            reset = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
        except ValueError:
            reset = None
    if not isinstance(reset, (int, float)):
        reset = None
    minutes = value.get("window_minutes")
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        seconds = value.get("limit_window_seconds")
        minutes = seconds // 60 if isinstance(seconds, int) and not isinstance(seconds, bool) else None
    return UsageWindow(name, float(used), float(reset) if reset is not None else None, minutes)


def parse(data: bytes, now: float) -> Optional[CodexUsageSnapshot]:
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    limits = obj.get("rate_limit")
    if not isinstance(limits, dict):
        return None
    windows = tuple(w for name, key in (("primary", "primary_window"), ("secondary", "secondary_window"))
                   if (w := _window(name, limits.get(key))) is not None)
    if not windows:
        return None
    plan = obj.get("plan_type")
    return CodexUsageSnapshot(plan if isinstance(plan, str) else None, windows, now)


Transport = Callable[[str, str], tuple[int, bytes]]


def _default_transport(token: str, account_id: str) -> tuple[int, bytes]:
    req = urllib.request.Request(ENDPOINT, headers={
        "Authorization": f"Bearer {token}",
        "ChatGPT-Account-Id": account_id,
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def fetch(auth: CodexAuth, now: float,
          transport: Optional[Transport] = None) -> Optional[CodexUsageSnapshot]:
    status, data = (transport or _default_transport)(auth.access_token, auth.account_id)
    if status in (401, 403):
        raise Unauthorized()
    if status != 200:
        return None
    return parse(data, now)
