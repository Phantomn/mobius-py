"""Claude OAuth usage 엔드포인트 조회 — UsageFetcher.swift 포팅.

게이지 표시용. 저빈도(캐시 만료 시)로만 호출한다 — 상시 폴링 없음(계정 리스크 최소화).
needsReauth 판정에도 쓰인다: 401/403 은 계정별 토큰으로 조회하므로 오귀인 불가.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Optional

from . import credentials
from .models import ScopedUsageLimit, UsageSnapshot

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"


class Unauthorized(Exception):
    """401/403 — 토큰이 거부됨."""


def _iso_to_epoch(s: str) -> Optional[float]:
    if not isinstance(s, str):
        return None
    txt = s.replace("Z", "+00:00")
    try:
        import datetime
        return datetime.datetime.fromisoformat(txt).timestamp()
    except ValueError:
        return None


def _pct(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def parse(data: bytes, now: float) -> Optional[UsageSnapshot]:
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None

    def block(key: str):
        b = obj.get(key)
        if not isinstance(b, dict):
            return (None, None)
        pct = _pct(b.get("utilization"))
        rs = b.get("resets_at")
        return (pct, _iso_to_epoch(rs) if isinstance(rs, str) else None)

    five_pct, five_reset = block("five_hour")
    week_pct, week_reset = block("seven_day")

    scoped: list[ScopedUsageLimit] = []
    for limit in (obj.get("limits") or []):
        if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
            continue
        scope = limit.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        if not isinstance(name, str) or not name:
            continue
        pct = _pct(limit.get("percent")) or 0.0
        rs = limit.get("resets_at")
        scoped.append(ScopedUsageLimit(label=name, percent=pct,
                                       resets_at=_iso_to_epoch(rs) if isinstance(rs, str) else None))

    # 월간 지출 한도 — 계정 창과 **다른 블록**에 있다. 이걸 안 읽으면 P3(월간 지출) 로그
    # hit 를 창으로만 검증하게 되고, 창이 여유면 "남의 hit" 로 판정돼 진짜 소진이 버려진다.
    spend = obj.get("spend")
    spend_enabled: Optional[bool] = None
    spend_percent: Optional[float] = None
    if isinstance(spend, dict):
        en = spend.get("enabled")
        if isinstance(en, bool):
            spend_enabled = en
        spend_percent = _pct(spend.get("percent"))

    if five_pct is None and week_pct is None and not scoped and spend_enabled is None:
        return None
    return UsageSnapshot(five_hour_percent=five_pct, five_hour_resets_at=five_reset,
                         seven_day_percent=week_pct, seven_day_resets_at=week_reset,
                         fetched_at=now, scoped_limits=scoped,
                         spend_enabled=spend_enabled, spend_percent=spend_percent)


Transport = Callable[[str], tuple]


def _default_transport(token: str) -> tuple:
    req = urllib.request.Request(ENDPOINT, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return (resp.status, resp.read())
    except urllib.error.HTTPError as e:
        return (e.code, e.read())


def fetch(credentials_blob: bytes, now: float,
          transport: Optional[Transport] = None) -> Optional[UsageSnapshot]:
    token = credentials.access_token(credentials_blob)
    if not token:
        return None
    status, data = (transport or _default_transport)(token)
    if status in (401, 403):
        raise Unauthorized()
    if status != 200:
        return None
    return parse(data, now)
