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
from enum import Enum
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


class HitKind(Enum):
    """이 hit 를 usage 스냅샷의 **어느 필드로 검증하는가.**

    로그 라인은 계정을 밝히지 않으므로 판정은 usage API 가 한다(daemon._verify_hit).
    창 소진과 지출 한도는 응답의 서로 다른 블록에 있어, 종류를 모르면 지출 한도 hit 가
    "창은 여유 있음 = 남의 hit" 로 오판돼 통째로 버려진다.
    """

    WINDOW = "window"                # 5시간/7일 창 — five_hour·seven_day 로 검증
    MONTHLY_SPEND = "monthly_spend"  # 월간 지출 한도 — spend 블록으로 검증


@dataclass
class RateLimitHit:
    resets_at: Optional[float]     # epoch 초. 없으면(월간 지출 등) None.
    # ★ 모델 전용 한도(예: Fable 주간)인가. **월간 지출 한도는 여기 해당하지 않는다** —
    #   지출 한도는 계정 전체를 막으므로 "핀이면 머문다" 예외를 주면 안 된다(구 포팅 오류).
    model_scoped: bool = False
    kind: HitKind = HitKind.WINDOW
    # --- 귀속 구간 (이슈 #19). 워처가 채운다. bound_at 이 None 이면 복원 실패 → usage 검증 ---
    # 이 요청이 자격증명에 묶였을 수 있는 **가장 이른** 시각(턴 시작). turn_start_at() 참조.
    bound_at: Optional[float] = None
    # 이 라인이 기록된 시각 = 구간의 끝. hit 자체 타임스탬프는 **요청 시각이 아니라** 429
    # 재시도가 끝난 시각이다(실측 2분 6초). 그래서 점이 아니라 구간으로 판정한다.
    observed_at: Optional[float] = None

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


def turn_start_at(obj: dict) -> Optional[float]:
    """이 라인이 **턴의 시작**(사용자 입력)이면 그 시각, 아니면 None.

    ★ 왜 필요한가 (이슈 #19 의 근본): 한도 에러 라인에는 계정이 적혀 있지 않고, 그 라인의
      **자체 타임스탬프는 요청 시각이 아니다** — 429 재시도가 끝난 뒤에 찍힌다(실측
      2026-08-15: 재시도 창 2분 6초, 쿨다운 120초보다 길다). 자격증명은 턴 시작에 묶이므로
      (upstream #20 정정: 실행 중 세션은 **다음 입력부터** 새 계정), 이 시각이 곧
      "누구의 요청이었나" 의 좌표다. 워처가 파일별로 이 값을 이어 들고 hit 에 붙인다.

    판별: type=="user" 이면서 tool_result 가 아니고(=도구 결과 주입이 아님) prompt 메타가
    붙은 라인. 실측 88/88 hit 에서 같은 파일 안에 선행 턴 시작 라인이 존재했다
    (서브에이전트 파일 61건 포함).
    """
    if obj.get("type") != "user" or obj.get("toolUseResult") is not None:
        return None
    if not (obj.get("promptSource") or obj.get("promptId")):
        return None
    return _timestamp(obj)


def scan_line(line: str, now: float) -> tuple[Optional[RateLimitHit], Optional[float]]:
    """한 줄을 **1회만** 디코드해 (hit, 턴 시작 시각) 을 함께 돌려준다.

    워처는 모든 라인을 훑으므로 hit 판정과 턴 경계 추적이 디코드를 나눠 쓰면 비용이 두 배가
    된다. 둘 중 하나만 필요한 호출자는 parse() 를 그대로 쓴다.
    """
    try:
        obj = json.loads(line)
    except ValueError:
        return (None, None)
    if not isinstance(obj, dict):
        return (None, None)
    hit = _parse_obj(obj, now)
    if hit is not None:
        hit.observed_at = _timestamp(obj)
    return (hit, turn_start_at(obj))


def parse(line: str, now: float) -> Optional[RateLimitHit]:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    return _parse_obj(obj, now)


def _parse_obj(obj: dict, now: float) -> Optional[RateLimitHit]:
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

    if _MONTHLY_SPEND.search(text):  # P3 월간 지출 — 리셋 시각 없음, spend 블록으로 검증
        return RateLimitHit(resets_at=None, kind=HitKind.MONTHLY_SPEND)

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
