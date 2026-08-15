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
    THRESHOLD_ADVISORY = "threshold_advisory"  # 임계값 선제 경고 — **소진 아님**


class DecisionKind(Enum):
    NONE = "none"
    SWITCH_TO = "switch_to"
    ALL_EXHAUSTED = "all_exhausted"                  # 전환할 곳 없음 → 알림만
    NOTIFY_EXHAUSTED_ONLY = "notify_exhausted_only"  # 자동 전환 꺼짐 — 알림만
    NOTIFY_ADVISORY_ONLY = "notify_advisory_only"    # 임계값 경고, 자동 전환 꺼짐 — 알림만


class CandidateProbeAction(Enum):
    USE_STORED_TOKEN = "use_stored_token"  # 저장 토큰 유효(또는 만료 정보 없음) → 그냥 조회
    SKIP_COOLDOWN = "skip_cooldown"        # 만료 + 쿨다운 중 → 판정 없이 스킵
    ESCALATE = "escalate"                  # 만료 + 쿨다운 경과 → 네트워크 refresh 로 승격


@dataclass
class Decision:
    kind: DecisionKind
    target_id: Optional[str] = None
    reason: Optional[SwitchReason] = None


_NONE = Decision(DecisionKind.NONE)


class AutoSwitchEngine:
    def __init__(self, cooldown: float = 120, margin: float = 60,
                 candidate_probe_backoff: float = 15 * 60):
        self.cooldown = cooldown   # 전환 직후 재전환 금지
        self.margin = margin       # 리셋 시각 + margin 후에만 복귀
        # 갈 곳이 없다고 판정한 뒤 이 간격 안에는 후보를 다시 걷지 않는다.
        self.candidate_probe_backoff = candidate_probe_backoff
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

    # ---------- 임계값 선제 전환 (advisory) ----------

    def should_probe_candidates(self, last_no_candidate_at: Optional[float],
                                now: float) -> bool:
        """후보 탐색을 다시 돌려도 되는가 — 순수 판정.

        갈 곳이 없다고 판정한 뒤 백오프 창 안에는 다시 걷지 않는다(5분마다 풀 전체를
        재탐색하며 폴백들을 계속 건드리는 것을 막는다).
        """
        if last_no_candidate_at is None:
            return True
        return now >= last_no_candidate_at + self.candidate_probe_backoff

    @staticmethod
    def candidate_probe_action(expires_at: Optional[float], now: float,
                               last_refresh_attempt_at: Optional[float],
                               cooldown: float) -> "CandidateProbeAction":
        """후보 1개를 어떻게 조회할지 — 순수 함수(네트워크·IO 없음).

        건강한 폴백의 토큰을 함부로 회전시키지 않기 위한 게이트다: 저장 토큰이 아직
        유효하면 그대로 조회하고, 만료됐을 때만 refresh 로 승격하되 계정당 쿨다운을 둔다.
        """
        if expires_at is None or expires_at > now:
            return CandidateProbeAction.USE_STORED_TOKEN
        if last_refresh_attempt_at is not None and now < last_refresh_attempt_at + cooldown:
            return CandidateProbeAction.SKIP_COOLDOWN
        return CandidateProbeAction.ESCALATE

    def check_advisory(self, file: AccountsFile, active_id: str,
                       verified_candidate: Optional[str], already_advised: bool,
                       now: float) -> Decision:
        """임계값 선제 경고 판정. 후보 검증(네트워크)은 호출자가 미리 끝내고 결과만 넘긴다.

        `already_advised` 는 호출자가 "직전 advised resets_at == 이번 advisory 의 resets_at"
        으로 계산해 넣는다(단순 존재 여부가 아니다 — 창이 바뀌면 다시 알려야 한다).
        """
        active = file.active
        if active is None or active.id != active_id or active.advisory is None:
            return _NONE

        # ★ 자동 전환이 꺼져 있으면 알림만 — **쿨다운 가드보다 먼저** 평가한다.
        #   알림만 하는 결정은 전환을 실행하지 않으므로 쿨다운 보호가 애초에 필요 없다.
        #   쿨다운을 먼저 두면 무관한 전환의 쿨다운 창이 이 알림을 영구히 삼켜버린다.
        if not file.auto_switch_enabled:
            return _NONE if already_advised else Decision(
                DecisionKind.NOTIFY_ADVISORY_ONLY, target_id=active.id)

        # 여기부터는 실제로 전환을 실행할 수 있는 분기들뿐이다.
        if self._in_cooldown(now):
            return _NONE

        # 경고를 보고 나서 사용자가 **일부러 돌아온** 핀만 거부권을 갖는다. 경고 이전의 핀
        # (또는 시각 없는 구버전 핀)은 "경고를 보고 선택한 것"이 아니므로 거부권 없음.
        if (active.user_pinned and active.pinned_at is not None
                and active.pinned_at > active.advisory.detected_at):
            return _NONE

        if verified_candidate is None:
            return _NONE  # 검증된 후보가 없으면 조용히 머문다(알림도 전환도 없음)
        return Decision(DecisionKind.SWITCH_TO, target_id=verified_candidate,
                        reason=SwitchReason.THRESHOLD_ADVISORY)

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
        # ★ 두 게이트 중 **있는 것 전부**를 지나야 복귀한다. rate_limit 만 검사하면
        #   advisory 만 보고 떠난 경우(rate_limit 없음) 가드가 통째로 스킵돼, 쿨다운이
        #   풀리는 순간 primary 로 돌아갔다가 아직 임계값 위인 primary 를 다시 떠나는
        #   2분 주기 핑퐁이 창 리셋까지 계속된다.
        gates = [r + self.margin for r in (
            primary.rate_limit.resets_at if primary.rate_limit else None,
            primary.advisory.resets_at if primary.advisory else None,
        ) if r is not None]
        if gates and now < max(gates):
            return _NONE
        return Decision(DecisionKind.SWITCH_TO, target_id=primary.id,
                        reason=SwitchReason.PRIMARY_RECOVERED)
