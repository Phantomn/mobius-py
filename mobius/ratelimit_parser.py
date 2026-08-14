"""세션 로그(JSONL) 한 줄에서 rate-limit 이벤트를 찾는다 — RateLimitParser.swift 포팅.

1. 라인을 JSON으로 디코드 (실패 시 스킵).
2. error=="rate_limit" (또는 isApiErrorMessage&&apiErrorStatus==429) 인 라인만 후보.
   레거시 pipe-epoch(P4)만 예외적으로 구조화 필드 없이 인정.
3. message.content[].text 를 이어붙여 패턴 P1~P5로 분류.
   ★ "not your usage limit" 포함 시 반드시 제외 — 서버측 제한은 계정 한도가 아니다(최우선).
4. 리셋 시각은 괄호 안 IANA TZ 기준 해석, 라인 timestamp와 비교해 과거면 익일/익년으로 굴린다.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FLAGS = re.IGNORECASE

# P1/P5 시각형: "resets 7:30pm (Asia/Seoul)"
_TIME_ONLY = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", _FLAGS)
# P2 날짜형: "resets Jul 13 at 8am (Asia/Seoul)"
_DATE_AND_TIME = re.compile(
    r"resets?\s+(?:at\s+)?([A-Za-z]{3})\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)",
    _FLAGS)
# P3 월간 지출 한도 — 리셋 시각 없음
_MONTHLY_SPEND = re.compile(r"hit your monthly spend limit", _FLAGS)
# P4 레거시 "usage limit reached|<epoch>"
_PIPE_EPOCH = re.compile(r"usage limit reached\|(\d{10,13})", _FLAGS)
# P5 미래 문구 변형 대비
_LENIENT = re.compile(r"hit your (?:usage|session|weekly)\s+limit\b.*\bresets?\b", _FLAGS)

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


@dataclass
class RateLimitHit:
    resets_at: Optional[float]     # epoch 초. 없으면(월간 지출 등) None.
    model_scoped: bool = False

    def effective_resets_at(self, now: float) -> float:
        """리셋 시각 없는 이벤트의 보수적 폴백: now + 24시간."""
        return self.resets_at if self.resets_at is not None else now + 24 * 3600


def _event_text(obj: dict) -> str:
    msg = obj.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            joined = "\n".join(c["text"] for c in content
                               if isinstance(c, dict) and isinstance(c.get("text"), str))
            if joined:
                return joined
    t = obj.get("text")
    return t if isinstance(t, str) else ""


def _timestamp(obj: dict) -> Optional[float]:
    s = obj.get("timestamp")
    if not isinstance(s, str):
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_candidate(obj: dict) -> bool:
    if obj.get("error") == "rate_limit":
        return True
    return obj.get("isApiErrorMessage") is True and obj.get("apiErrorStatus") == 429


def _rebuild(dt: datetime.datetime, hour24: int, minute: int) -> datetime.datetime:
    """같은 날짜에 지정 시/분(초=0)으로 벽시계 기준 재구성."""
    return dt.replace(hour=hour24, minute=minute, second=0, microsecond=0)


def _resolve(month_abbr: Optional[str], day: Optional[str], hour: Optional[str],
             minute: Optional[str], meridiem: Optional[str], tz_name: Optional[str],
             reference: float) -> Optional[float]:
    if hour is None or meridiem is None or tz_name is None:
        return None
    try:
        hour12 = int(hour)
    except ValueError:
        return None
    if not (1 <= hour12 <= 12):
        return None
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None
    hour24 = (hour12 % 12) + (12 if meridiem.lower() == "pm" else 0)
    minute_n = int(minute) if minute else 0

    ref_dt = datetime.datetime.fromtimestamp(reference, tz)

    if month_abbr and day:
        month = _MONTHS.get(month_abbr.lower())
        if month is None:
            return None
        try:
            cand = datetime.datetime(ref_dt.year, month, int(day),
                                     hour24, minute_n, 0, tzinfo=tz)
        except ValueError:
            return None
        if cand.timestamp() < reference:  # 이미 지난 날짜 → 익년
            try:
                cand = cand.replace(year=cand.year + 1)
            except ValueError:  # 2/29 등
                cand = cand.replace(year=cand.year + 1, day=28)
        return cand.timestamp()

    cand = _rebuild(ref_dt, hour24, minute_n)
    if cand.timestamp() <= reference:  # 이미 지난 시각 → 익일 (벽시계 보존)
        next_day = (cand + datetime.timedelta(days=1)).date()
        cand = datetime.datetime(next_day.year, next_day.month, next_day.day,
                                 hour24, minute_n, 0, tzinfo=tz)
    return cand.timestamp()


def _legacy_epoch_hit(text: str, reference: float) -> Optional[RateLimitHit]:
    m = _PIPE_EPOCH.search(text)
    if not m:
        return None
    raw = m.group(1)
    epoch = float(raw)
    if len(raw) == 13:
        epoch /= 1000.0
    if reference - 86_400 < epoch < reference + 7 * 86_400:
        return RateLimitHit(resets_at=epoch)
    return None


def parse(line: str, now: float) -> Optional[RateLimitHit]:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None

    text = _event_text(obj)
    if not text:
        return None

    # 제외 규칙 — 서버측 rate limit은 계정 한도가 아니다 (최우선)
    if re.search("not your usage limit", text, _FLAGS):
        return None

    reference = _timestamp(obj)
    if reference is None:
        reference = now

    if not _is_candidate(obj):
        return _legacy_epoch_hit(text, reference)  # 구조화 필드 없으면 P4만 인정

    if _MONTHLY_SPEND.search(text):  # P3 월간 지출 = 모델 전용
        return RateLimitHit(resets_at=None, model_scoped=True)

    m = _DATE_AND_TIME.search(text)  # P2 날짜+시각(주간)
    if m:
        d = _resolve(m.group(1), m.group(2), m.group(3), m.group(4),
                     m.group(5), m.group(6), reference)
        if d is not None:
            return RateLimitHit(resets_at=d)

    m = _TIME_ONLY.search(text)  # P1 시각만(세션)
    if m:
        d = _resolve(None, None, m.group(1), m.group(2), m.group(3), m.group(4), reference)
        if d is not None:
            return RateLimitHit(resets_at=d)

    hit = _legacy_epoch_hit(text, reference)  # P4
    if hit:
        return hit

    if _LENIENT.search(text):  # P5 시각 못 읽는 미래 변형 → 시각 없이 알림
        return RateLimitHit(resets_at=None)
    return None
