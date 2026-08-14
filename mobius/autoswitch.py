"""자동 전환 순수 상태머신 — AutoSwitchEngine.swift 포팅.

부작용 없음 — 호출자(daemon)가 Decision을 실행하고 note_switched()로 알려준다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .models import AccountsFile, RateLimitInfo
from .ratelimit_parser import RateLimitHit


class SwitchReason(Enum):
    ACTIVE_EXHAUSTED = "active_exhausted"    # 활성 계정 한도 소진
    PRIMARY_RECOVERED = "primary_recovered"  # primary 리셋 도래 → 복귀


class DecisionKind(Enum):
    NONE = "none"
    SWITCH_TO = "switch_to"
    ALL_EXHAUSTED = "all_exhausted"                  # 전환할 곳 없음 → 알림만
    NOTIFY_EXHAUSTED_ONLY = "notify_exhausted_only"  # 자동 전환 꺼짐 — 알림만


@dataclass
class Decision:
    kind: DecisionKind
    target_id: Optional[str] = None
    reason: Optional[SwitchReason] = None


_NONE = Decision(DecisionKind.NONE)


class AutoSwitchEngine:
    def __init__(self, cooldown: float = 120, margin: float = 60):
        self.cooldown = cooldown   # 전환 직후 재전환 금지
        self.margin = margin       # 리셋 시각 + margin 후에만 복귀
        self._last_switch_at = float("-inf")

    def note_switched(self, now: float) -> None:
        self._last_switch_at = now

    def _in_cooldown(self, now: float) -> bool:
        return now < self._last_switch_at + self.cooldown

    @staticmethod
    def _first_available(file: AccountsFile, excluding: Optional[str], now: float) -> Optional[str]:
        """후보: 순서(우선순위)대로, 한도 안 걸렸고 재인증 불필요한 계정."""
        for a in file.accounts:
            if a.id != excluding and not a.is_limited(now) and not a.needs_reauth:
                return a.id
        return None

    def on_rate_limit_hit(self, file: AccountsFile, hit: RateLimitHit, now: float) -> Decision:
        active = file.active
        if active is None or self._in_cooldown(now):
            return _NONE
        if not file.auto_switch_enabled:  # 끄면 소진 알림만
            return Decision(DecisionKind.NOTIFY_EXHAUSTED_ONLY, target_id=active.id)
        # 모델 전용 한도 + 사용자 핀 → 머문다.
        if hit.model_scoped and active.user_pinned:
            return _NONE
        marked = self._marked_file(file, active.id, hit, now)
        nxt = self._first_available(marked, excluding=active.id, now=now)
        if nxt is None:
            return Decision(DecisionKind.ALL_EXHAUSTED)
        return Decision(DecisionKind.SWITCH_TO, target_id=nxt, reason=SwitchReason.ACTIVE_EXHAUSTED)

    @staticmethod
    def _marked_file(file: AccountsFile, active_id: str, hit: RateLimitHit, now: float) -> AccountsFile:
        f = copy.deepcopy(file)
        for a in f.accounts:
            if a.id == active_id:
                a.rate_limit = RateLimitInfo(resets_at=hit.effective_resets_at(now),
                                             recorded_at=now, model_scoped=hit.model_scoped)
                break
        return f

    def on_tick(self, file: AccountsFile, now: float) -> Decision:
        if not file.auto_switch_enabled or self._in_cooldown(now):
            return _NONE
        active = file.active
        if active is None:
            return _NONE

        # (A) 자가복구: 활성이 소진/로그인만료인데 여전히 활성이면 여유 계정으로 전환
        if active.auto_switch_may_leave(now):
            nxt = self._first_available(file, excluding=active.id, now=now)
            if nxt is not None:
                return Decision(DecisionKind.SWITCH_TO, target_id=nxt,
                                reason=SwitchReason.ACTIVE_EXHAUSTED)

        # (B) primary 복귀 — 현재 fallback 활성이 "자동 전환"의 결과일 때만
        primary = file.primary
        if (not file.auto_switched_from_primary or primary is None
                or active.id == primary.id or primary.needs_reauth):
            return _NONE
        if primary.rate_limit is not None:
            if now < primary.rate_limit.resets_at + self.margin:
                return _NONE
        return Decision(DecisionKind.SWITCH_TO, target_id=primary.id,
                        reason=SwitchReason.PRIMARY_RECOVERED)
