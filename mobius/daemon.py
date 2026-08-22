"""자동 전환 데몬 — AppState.tick/apply/preflight/proactiveRefresh 포팅.

macOS 앱의 3초 타이머 루프를 foreground 프로세스로 옮긴 것. 게이트별 상수는 실측값.
매 틱: (외부 CLI 변경 반영 위해) store 리로드 → reconcile/adopt(15s) → 활성 스냅샷 동기화(5m)
→ 만료임박 폴백 선제 refresh(1h) → 세션 로그 스캔 → 엔진 결정 실행.
"""

from __future__ import annotations

import signal
import time
from enum import Enum
from typing import Optional

from . import credentials, usage
from .autoswitch import (AutoSwitchEngine, CandidateProbeAction, Decision, DecisionKind,
                         SwitchReason)
from .claude_config import ClaudeConfigIO
from .env import MobiusEnvironment
from .fallback_check import FallbackAuthChecker, FallbackCheckResult
from .log_watcher import SessionLogWatcher
from .models import AdvisoryRecord, RateLimitInfo, UsageSnapshot
from .ratelimit_parser import HitKind, RateLimitHit
from .notify import notify
from .store import AccountStore
from .switcher import Switcher

TICK_INTERVAL = 3.0
RECONCILE_INTERVAL = 15.0
ACTIVE_SNAPSHOT_SYNC_INTERVAL = 5 * 60.0
PROACTIVE_SWEEP_INTERVAL = 3600.0
PROACTIVE_RENEW_WINDOW = 3 * 24 * 3600.0
PROACTIVE_PER_ACCOUNT_GATE = 6 * 3600.0
# needsReauth 딱지가 붙은 계정의 회복(사용자가 `claude /login` 으로 직접 복구)을 확인하는 주기.
# upstream(AppState.refreshUsageIfStale)은 팝오버 열 때마다 돌지만, 데몬엔 UI 이벤트가 없으므로
# 시간 게이트로 대신한다. 딱지 붙은 계정만 대상이라 평시 네트워크는 0이다.
REAUTH_RECHECK_INTERVAL = 30 * 60.0
# 로그 hit 검증용 usage 조회의 최소 간격(계정당). 한 번도 본 적 없는 큰 세션 파일이
# 과거 hit 를 무더기로 쏟아낼 때 조회가 hit 수만큼 늘어나는 것을 막는다.
# ★ 이 창 안에 도착한 hit 는 **버리지 않는다** — 판정 보류(UNKNOWN)로 두고 다시 시도한다.
#   짧게 잡는 이유: 이 값이 곧 진짜 소진을 확인하기까지의 최대 지연이다.
USAGE_VERIFY_MIN_INTERVAL = 15.0
# 판정 못 한 트리거를 보관하는 한도. 지나면 조용히 버린다 — 그 이상 붙들어 봐야
# "확인 못 했는데 기록" = 오귀인 날조라, #19 를 우리 손으로 재현하는 셈이다.
PENDING_HIT_TTL = 15 * 60.0
# 후보 탐색에서 만료된 폴백 토큰을 refresh 로 승격할 때의 계정당 재시도 쿨다운.
# transient 실패가 5분마다 회전 시도로 반복되지 않게 한다.
PROBE_REFRESH_COOLDOWN = 30 * 60.0
# 활성 계정 타임라인 보관 길이. 가장 오래된 항목보다 이른 hit 는 "모름" 으로 떨어진다 —
# 실측 최장 턴이 56분이므로 전환이 잦아도 그 지평을 덮는다.
ACTIVE_TIMELINE_MAX = 64

class Verdict(Enum):
    """로그 hit 판정 — **3-값이다.**

    ★ 왜 Optional 이 아닌가 (근본 원인, 실패기록): 검증을 도입하면서 판정을
      `Optional[RateLimitInfo]`(있음/없음) 이진으로 접었더니, 실제 도메인의 네 결과
      ①확인됨 ②확인 결과 아님 ③아직 모름 ④이 증거로는 판정 불가 중 ③④가 전부 ②로
      흡수됐다. 증거 스트림이 **일회성**이라(워처 오프셋은 전진하고, 사용자는 한도
      에러를 보면 타이핑을 멈춘다) ②는 파괴적이다 — 조회가 한 번 실패한 것만으로
      진짜 소진이 영영 기록되지 않는다(수정 전보다 나쁨: 예전엔 무조건 기록했다).
    """

    CONFIRMED = "confirmed"   # 이 계정이 실제로 소진 — 기록한다
    REFUTED = "refuted"       # 이 계정은 여유 — 그 hit 는 다른 계정 것이다. 버린다.
    UNKNOWN = "unknown"       # 판정 못 함 — 트리거를 보관해 다시 시도한다


def verify_verdict(snap: UsageSnapshot, hit: RateLimitHit,
                   now: float) -> tuple[Verdict, Optional[float]]:
    """usage 스냅샷으로 hit 를 판정한다 — 순수 함수(네트워크·IO 없음).

    돌려주는 리셋 시각은 CONFIRMED 일 때만 의미가 있고, None 이면 호출자가
    hit 의 폴백(24시간)을 쓴다.
    """
    # ★ 월간 지출 한도(P3)도 **계정 창으로 교차 확인한다** — 지출 한도 블록으로 판정하지
    #   않는다. upstream 실측(2026-07-13): 이 메시지는 표시 우선순위(override)라 플랜 창이
    #   여유인 상태에서도 뜨고 세션은 정상 동작한다. 즉 P3 자체는 "계정을 못 쓴다"의 증거가
    #   아니다 — 그대로 기록하면 24h 동안 멀쩡한 계정이 막힌다. 반대로 진짜 창 소진과 겹치면
    #   이 메시지가 창 소진을 가리므로, 무시하지 말고 창을 확인해야 한다.
    #   (kind 는 그래서 판정 분기가 아니라 "지출 한도는 모델 전용 한도가 아니다"의 표식이다.)
    if not snap.exhausted_account_window():
        return (Verdict.REFUTED, None)
    reset = snap.account_reset_after(now)
    if reset is None:
        # 소진인데 아직 안 지난 리셋 시각이 없다 = 응답이 낡았다. "여유"와 구분해 보류한다.
        return (Verdict.UNKNOWN, None)
    return (Verdict.CONFIRMED, reset)


_DEAD_RESULTS = {
    FallbackCheckResult.DEAD, FallbackCheckResult.LOCALLY_DEAD,
    FallbackCheckResult.NO_REFRESH_TOKEN, FallbackCheckResult.STORE_FAILED,
}


class Daemon:
    def __init__(self, env: Optional[MobiusEnvironment] = None):
        self.env = env or MobiusEnvironment.live()
        self.store = AccountStore(self.env)
        self.io = ClaudeConfigIO(self.env)
        self.switcher = Switcher(self.store, self.io)
        self.engine = AutoSwitchEngine()
        self.watcher = SessionLogWatcher(self.env)
        self.fallback_checker = FallbackAuthChecker(self.store)
        self._last_reconcile = float("-inf")
        self._last_active_sync = float("-inf")
        self._last_proactive_sweep = float("-inf")
        self._last_proactive_refresh: dict[str, float] = {}
        self._last_reauth_recheck: dict[str, float] = {}
        self._last_usage_verify: dict[str, float] = {}
        self._last_usage_refresh_attempt: dict[str, float] = {}
        self._last_advised_resets_at: dict[str, float] = {}
        self._last_no_candidate_at: Optional[float] = None
        # 판정 못 한 트리거 (account_id, hit, 최초 도착 시각). 슬롯 하나면 충분하다 —
        # 같은 계정의 여러 hit 는 구분 불가능한 같은 증거("뭔가 한도라고 했다")다.
        self._pending_hit: Optional[tuple[str, RateLimitHit, float]] = None
        # 활성 계정 타임라인 (관찰 시각, 계정 id) — hit 의 귀속 시각을 계정으로 되돌린다.
        # 자기 전환은 즉시, 외부 전환은 다음 틱(≤3초)에 기록된다. _owner_at 참조.
        self._active_timeline: list[tuple[float, float, Optional[str]]] = []
        self._last_tick_at: Optional[float] = None
        # usage 조회 transport — 테스트가 네트워크 없이 주입한다(None = 실제 HTTP).
        self.usage_transport = None
        self._stop = False

    # ---------- 루프 ----------

    def run(self) -> None:
        def _handle(_signum, _frame):
            self._stop = True
        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
        notify("자동 전환 데몬 시작", "세션 로그를 감시합니다.")
        while not self._stop:
            try:
                self.tick(time.time())
            except Exception as e:  # 한 틱 실패가 데몬을 죽이지 않도록
                print(f"[mobius] tick 오류: {e}", flush=True)
            # 짧은 슬립을 여러 번 나눠 종료 신호에 빠르게 반응
            slept = 0.0
            while slept < TICK_INTERVAL and not self._stop:
                time.sleep(0.5)
                slept += 0.5
        print("[mobius] 데몬 종료", flush=True)

    def tick(self, now: float) -> None:
        self.store.reload()  # 외부 CLI(switch/auto)의 변경을 반영

        if now - self._last_reconcile >= RECONCILE_INTERVAL:
            self._last_reconcile = now
            try:
                self.switcher.adopt_live_account_if_unregistered()
                self.switcher.reconcile()
            except Exception:
                pass

        if now - self._last_active_sync >= ACTIVE_SNAPSHOT_SYNC_INTERVAL:
            self._last_active_sync = now
            try:
                synced = self.switcher.refresh_active_snapshot_if_stable()
            except Exception:
                synced = False
            # 임계값 폴은 **싱크 성사 뒤에만** — 저장 secret 이 방금 갱신됐으므로 라이브를
            # 한 번 더 읽지 않는다(같은 5분 블록이 라이브 자격증명 1회를 나눠 쓴다).
            if synced and self.store.file.advisory_enabled:
                try:
                    self._poll_threshold(now)
                except Exception as e:
                    print(f"[mobius] 임계값 폴 오류: {e}", flush=True)

        # 전환(자기·외부 무관)을 타임라인에 남긴다 — 스캔보다 **먼저**. 이번 틱에 읽을 hit 의
        # 귀속 좌표가 이 시각 이전이어야 하므로, 늦게 기록하면 방금 전환을 못 본 채 판정한다.
        self._note_active(now)

        self._clear_expired_rate_limits(now)
        self._recheck_flagged_accounts(now)

        if now - self._last_proactive_sweep >= PROACTIVE_SWEEP_INTERVAL:
            self._last_proactive_sweep = now
            self._proactive_refresh_expiring_fallbacks(now)

        # 세션 로그의 hit 는 **증거가 아니라 트리거**다 — 라인에 계정이 적혀 있지 않으므로
        # 활성 계정에 귀속시키면 오귀인이 난다(_verify_hit 참조). 판정은 usage API 가 한다.
        active_id = self.store.file.active_account_id
        # ★ 스캔 **앞**에서 재시도한다 — 여기서 소진이 확정되면 기록이 남아, 같은 틱의
        #   새 hit 들은 is_limited 게이트에 걸려 조회 없이 끝난다.
        self._retry_pending_hit(active_id, now)
        for hit in self.watcher.scan(now):
            # ★ 먼저 **귀속 시각으로 주인을 찾는다** (이슈 #19 근본 수정). 성공하면 네트워크 0.
            #   월간 지출 hit(P3)은 제외 — 주인은 알아도 "그게 창 소진인가" 는 여전히
            #   교차 확인이 필요하다(그대로 기록하면 24h 오탐). 귀속과 판정은 다른 질문이다.
            # 리셋 시각 없는 변형(P5)은 귀속돼도 기록하지 않는다 — 24시간 폴백을 박으면
            # 실제로 2시간 뒤 풀릴 계정을 하루 막는다. 그 시각은 API 만 안다.
            owner = self._owner_for(hit, now) if hit.resets_at is not None else None
            if owner is not None and hit.kind is HitKind.WINDOW:
                self._record_attributed(owner, hit, now)
                continue
            if active_id is None:
                continue
            verdict, info = self._verify_hit(active_id, hit, now)
            if verdict == Verdict.UNKNOWN:
                # 트리거를 보관한다. 버리면 안 되는 이유는 Verdict 주석 참조.
                if self._pending_hit is None or self._pending_hit[0] != active_id:
                    self._pending_hit = (active_id, hit, now)
                continue
            # CONFIRMED/REFUTED 는 방금 뜬 스냅샷(또는 이미 있는 기록)에서 나온 판정이라
            # 보관 중인 옛 트리거보다 새롭다 — 보류를 정리한다.
            self._pending_hit = None
            if verdict == Verdict.CONFIRMED and info is not None:
                self._record_and_apply(active_id, info, hit, now)
        self._apply(self.engine.on_tick(self.store.file, now), now)
        self._last_tick_at = now

    # ---------- 결정 실행 ----------

    def _apply(self, decision: Decision, now: float) -> None:
        if decision.kind == DecisionKind.NONE:
            return
        if decision.kind == DecisionKind.ALL_EXHAUSTED:
            notify("모든 계정 한도 소진", "전환 가능한 계정이 없습니다. 리셋을 기다려주세요.")
            return
        if decision.kind == DecisionKind.NOTIFY_EXHAUSTED_ONLY:
            name = self._nick(decision.target_id)
            notify("한도 소진 — 자동 전환이 꺼져 있습니다", f"{name} 계정이 한도에 도달했습니다. 수동으로 전환하세요.")
            return
        if decision.kind == DecisionKind.NOTIFY_ADVISORY_ONLY:
            # ★ 소진이 **아니다** — 문구에 소진 표현을 쓰면 거짓말이 된다.
            name = self._nick(decision.target_id)
            notify(f"⚠️ {name} 계정 한도가 가까워요",
                   "설정한 임계값에 도달했어요. 자동 전환이 꺼져 있으니 필요하면 직접 전환하세요.")
            return
        # SWITCH_TO
        target_id = decision.target_id
        reason = decision.reason
        # 자동 폴백으로 넘어가기 직전 실제 refresh로 대상 생사 확인 → 죽었으면 취소
        if reason in (SwitchReason.ACTIVE_EXHAUSTED, SwitchReason.THRESHOLD_ADVISORY):
            if not self._preflight_fallback(target_id, now):
                return
        from_id = self.store.file.active_account_id
        try:
            self.switcher.switch_to(target_id)
        except Exception as e:
            print(f"[mobius] 자동 전환 실패: {e}", flush=True)
            return
        self.engine.note_switched(now)
        # 자기 전환은 **시각을 정확히 안다** — 다음 틱의 관찰(불확실 구간)을 기다리지 않는다.
        self._note_active(now, exact=True)
        try:
            self.store.set_auto_switched_from_primary(
                reason in (SwitchReason.ACTIVE_EXHAUSTED, SwitchReason.THRESHOLD_ADVISORY))
        except Exception:
            pass
        name = self._nick(target_id)
        if reason == SwitchReason.PRIMARY_RECOVERED:
            notify(f"✅ {name} 계정으로 복귀", "한도가 초기화돼 주 계정으로 돌아왔어요.")
        elif reason == SwitchReason.THRESHOLD_ADVISORY:
            # ★ 소진이 아니라 **선제** 전환 — 문구를 구분한다.
            notify(f"🔄 {name} 계정으로 미리 전환",
                   f"{self._nick(from_id)} 한도가 가까워져 미리 옮겼어요. 새 claude 세션부터 적용돼요.")
        else:
            notify(f"🔄 {name} 계정으로 전환",
                   f"{self._nick(from_id)} 한도 소진 → {name}. 새 claude 세션부터 적용돼요.")

    # ---------- 임계값 선제 전환 ----------

    def _poll_threshold(self, now: float) -> None:
        """활성 계정 사용률을 보고 advisory 를 set/clear 한 뒤, 유효하면 후보 탐색→판정→적용.

        **5분 싱크 성사 뒤에만** 호출된다 — 활성 secret 이 방금 갱신됐으므로 저장 스냅샷으로
        조회한다(라이브 2차 읽기 없음).
        """
        active = self.store.file.active
        if active is None or active.needs_reauth:
            return
        snap = self.store.secret(active.id)
        if snap is None:
            return
        try:
            usage_snap = usage.fetch(snap.credentials_blob, now, self.usage_transport)
        except Exception:
            return
        if usage_snap is None:
            return

        threshold = self.store.file.advisory_threshold
        util = usage_snap.five_hour_percent or 0.0

        # 히스테리시스 set/clear. set: 임계값 이상 + 리셋 시각 존재 → detected_at 보존해 세운다.
        # clear: 밴드 아래(임계값-5 이하) + 기존 advisory 존재 → 해제하되 백오프·last-advised
        #        맵은 **건드리지 않는다**(새 창은 resets_at 이 달라 자연히 재알림된다).
        # 그 사이(밴드 내부)면 그대로 둔다.
        if (AdvisoryRecord.should_set(util, threshold)
                and usage_snap.five_hour_resets_at is not None):
            detected_at = active.advisory.detected_at if active.advisory else now
            self.store.set_advisory(active.id, AdvisoryRecord(
                utilization=util, resets_at=usage_snap.five_hour_resets_at,
                detected_at=detected_at))
        elif AdvisoryRecord.should_clear(util, threshold) and active.advisory is not None:
            self.store.set_advisory(active.id, None)

        advised = self.store.file.active
        if advised is None or advised.id != active.id or advised.advisory is None:
            return
        advisory = advised.advisory

        # 후보 탐색은 "전환 가능"할 때만 — 자동 전환이 켜져 있고 백오프 창을 지났을 때.
        # 꺼져 있으면 후보는 알림 경로에서 쓰이지 않으므로 탐색(네트워크)도 생략한다.
        verified_candidate = None
        if (self.store.file.auto_switch_enabled
                and self.engine.should_probe_candidates(self._last_no_candidate_at, now)):
            verified_candidate = self._probe_candidate(threshold, now)
            # 후보 있으면 백오프 리셋, 없으면 지금부터 백오프 시작.
            # ★ 여기서만 리셋한다 — advisory clear 에서는 절대 리셋하지 않는다(오실레이션 방어).
            self._last_no_candidate_at = None if verified_candidate else now

        # ★★★ 로드-베어링 순서 — 절대 재배열 금지.
        # "맵을 먼저 쓰고 플래그를 계산"하면 비교가 항상 같아져 already_advised 가 영원히
        # True 가 되고, 토글-off 알림(NOTIFY_ADVISORY_ONLY)이 영구히 삼켜진다(엔진 테스트는
        # 플래그를 파라미터로 주입받아 초록으로 남는다 — 포착-비교-호출-쓰기 순서로만 잡힌다).
        #   1) 직전 last-advised resets_at 을 **맵 쓰기 전에** 지역 변수로 포착
        prior_advised = self._last_advised_resets_at.get(active.id)
        #   2) 포착한 지역 값과 이번 advisory 를 비교
        already_advised = prior_advised == advisory.resets_at
        #   3) 엔진 판정 → 적용
        self._apply(self.engine.check_advisory(
            self.store.file, active.id, verified_candidate, already_advised, now), now)
        #   4) **그런 다음에야** 이번 폴의 resets_at 을 맵에 쓴다(토글 상태 무관)
        self._last_advised_resets_at[active.id] = advisory.resets_at

    def _probe_candidate(self, threshold: float, now: float) -> Optional[str]:
        """advisory 가 걸린 활성 계정의 폴백 후보를 우선순위대로 검증. 임계값 미만인 첫 후보.

        ★ 네트워크 refresh 는 `ESCALATE`(만료 + 쿨다운 경과)에서만 — 멀쩡한 폴백의 토큰을
          회전시켜 벽돌로 만들지 않기 위한 가드다. 이 메서드는 checker 가 스스로 하는 것
          이상으로 needs_reauth 를 마킹하지 않는다(알림도 없음).
        """
        active = self.store.file.active_account_id
        for p in self.store.file.accounts:
            if p.id == active or p.is_limited(now) or p.needs_reauth:
                continue
            snap = self.store.secret(p.id)
            if snap is None:
                continue
            blob = snap.credentials_blob
            action = AutoSwitchEngine.candidate_probe_action(
                credentials.expires_at(blob), now,
                self._last_usage_refresh_attempt.get(p.id), PROBE_REFRESH_COOLDOWN)
            if action == CandidateProbeAction.SKIP_COOLDOWN:
                continue   # 만료 + 쿨다운 중 — 죽었다고 단정하지 않고 넘어간다
            if action == CandidateProbeAction.ESCALATE:
                self._last_usage_refresh_attempt[p.id] = now
                r = self.fallback_checker.check(p.id, active, now, allow_network=True)
                if r != FallbackCheckResult.REFRESHED_ALIVE:
                    continue   # checker 가 이미 마킹 — 추가 알림 없이 스킵
                self._last_usage_refresh_attempt.pop(p.id, None)
                fresh = self.store.secret(p.id)
                if fresh is None:
                    continue
                blob = fresh.credentials_blob   # 회전된 신선한 토큰으로 조회
            try:
                s = usage.fetch(blob, now, self.usage_transport)
            except Exception:
                continue
            if s is None:
                continue
            if (s.five_hour_percent or 0.0) < threshold:
                return p.id     # 임계값 미만 = 검증된 후보
        return None

    # ---------- 귀속 (이슈 #19 근본 수정) ----------

    def _note_active(self, now: float, exact: bool = False) -> None:
        """활성 계정이 바뀌었으면 타임라인에 남긴다 — `(가능한 최이른, 관찰, 계정)`.

        ★ 두 시각을 따로 드는 이유: **자기 전환만 시각을 안다.** 외부 전환(사용자의
          `claude /login`, 다른 도구)은 reconcile 이 늦게 관찰하므로 실제 시각은
          (직전 틱, 지금] 어딘가다. 하나로 접으면 그 폭이 "확실히 안 바뀌었다"로
          둔갑해, 이 수정이 없애려는 추측이 다시 들어온다.
        """
        active = self.store.file.active_account_id
        if self._active_timeline and self._active_timeline[-1][2] == active:
            return
        earliest = now if exact else (self._last_tick_at
                                      if self._last_tick_at is not None else float("-inf"))
        self._active_timeline.append((earliest, now, active))
        del self._active_timeline[:-ACTIVE_TIMELINE_MAX]

    def _owner_for(self, hit: RateLimitHit, now: float) -> Optional[str]:
        """이 hit 의 주인. **구간 내내 활성이 그대로였을 때만** 답한다.

        ★ 왜 점이 아니라 구간인가 (이슈 #19 의 근본): hit 의 타임스탬프는 요청 시각이 아니라
          429 **재시도가 끝난** 시각이다(실측 2026-08-15: 2분 6초 — 엔진 쿨다운 120초보다
          길어서 시간 창으로는 못 막는다). 요청이 살아 있던 구간
          `[자격증명이 묶였을 수 있는 최이른 시각, 로그에 찍힌 시각]` 안에서 전환이
          일어날 수 있었다면 이 hit 의 주인은 **원리적으로 불확정**이다.

        ★ 이 판정은 "자격증명이 턴 시작에 묶인다" 같은 **가정을 쓰지 않는다.** 구간 안에
          전환이 없었다면 어떤 결합 규칙이든 답은 하나다. 있었다면 답하지 않는다 —
          그때만 usage 검증(계정별 토큰 = 오귀인 구조적 불가)으로 넘긴다.
          즉 이 수정은 검증을 **대체하지 않고 그 대상을 정확히 좁힌다**: 기존 코드는
          모든 hit 을 조회했고(평시 네트워크 0 계약 훼손), 그 전에는 모든 hit 을 믿었다.
        """
        bind = hit.bound_at
        if bind is None:
            return None
        end = hit.observed_at if hit.observed_at is not None else now
        if end < bind:
            end = bind
        timeline = self._active_timeline
        # bind 시점에 **이미 확정돼 있던** 상태(관찰까지 끝난 항목)를 찾는다.
        idx = -1
        for i, (_earliest, observed, _account) in enumerate(timeline):
            if observed <= bind:
                idx = i
            else:
                break
        if idx < 0:
            return None                       # 데몬 시작 전 — 모른다
        if idx + 1 < len(timeline) and timeline[idx + 1][0] <= end:
            return None                       # 구간 안에서 바뀌었을 수 있다
        return timeline[idx][2]

    def _record_attributed(self, account_id: str, hit: RateLimitHit, now: float) -> None:
        """귀속이 확정된 창 소진을 기록한다 — usage 조회 없이.

        엔진 결정은 **그 계정이 아직 활성일 때만** 돌린다. on_rate_limit_hit 은 hit 을 인자
        계정이 아니라 현재 활성 계정에 얹어 판단하므로, 이미 떠난 계정의 hit 로 부르면 엉뚱한
        계정을 소진으로 보고 결정한다. 기록만 남기면 on_tick 의 자가복구가 이어 받는다.
        """
        profile = next((a for a in self.store.file.accounts if a.id == account_id), None)
        if profile is None or profile.is_limited(now):
            return                      # 같은 소진의 후속 hit — 알림 폭풍 차단
        info = RateLimitInfo(resets_at=hit.effective_resets_at(now), recorded_at=now,
                             model_scoped=hit.model_scoped)
        if account_id == self.store.file.active_account_id:
            self._record_and_apply(account_id, info, hit, now)
            return
        try:
            self.store.update(account_id, lambda p, v=info: setattr(p, "rate_limit", v))
        except Exception:
            pass

    def _record_and_apply(self, account_id: str, info: RateLimitInfo,
                          hit: RateLimitHit, now: float) -> None:
        """확정된 소진을 기록하고 엔진 결정을 실행한다(인라인 경로와 보류 재시도 경로 공용)."""
        try:
            self.store.update(account_id, lambda p, v=info: setattr(p, "rate_limit", v))
        except Exception:
            pass
        self._apply(self.engine.on_rate_limit_hit(
            self.store.file, RateLimitHit(resets_at=info.resets_at, kind=hit.kind,
                                          model_scoped=info.model_scoped), now), now)

    def _retry_pending_hit(self, active_id: Optional[str], now: float) -> None:
        """판정 못 하고 보관해 둔 트리거를 다시 판정한다.

        ★ 왜 보관하는가: 로그 hit 는 워처 오프셋이 전진해 **한 번만 배달**되고, 사용자는
          한도 에러를 보면 타이핑을 멈춰 새 hit 이 오지 않는다. 예전 주석("hit 는 반복되므로
          다음 틱에 다시 시도된다")은 **사실이 아니었다** — 조회 한 번 실패가 곧 영구 유실이다.

        ★ 활성 계정이 바뀌면 버린다: 판정은 그 계정의 토큰으로 조회해야 하는데 비활성
          계정은 저장 토큰이 만료돼 refresh(토큰 회전)를 부르게 된다. 멀쩡한 폴백의 토큰을
          한도 판정 때문에 회전시키지 않는다는 원칙이 우선이다.
        """
        if self._pending_hit is None:
            return
        account_id, hit, first_seen = self._pending_hit
        if active_id != account_id:
            self._pending_hit = None
            return
        if now - first_seen >= PENDING_HIT_TTL:
            self._pending_hit = None
            print(f"[mobius] 한도 hit 판정 보류 만료 — {PENDING_HIT_TTL / 60:.0f}분간 usage 확인 "
                  f"실패로 폐기(귀속을 날조하지 않는다)", flush=True)
            return
        verdict, info = self._verify_hit(account_id, hit, now)
        if verdict == Verdict.UNKNOWN:
            return          # 계속 보관
        self._pending_hit = None
        if verdict == Verdict.CONFIRMED and info is not None:
            self._record_and_apply(account_id, info, hit, now)

    def _verify_hit(self, account_id: str, hit: RateLimitHit,
                    now: float) -> tuple[Verdict, Optional[RateLimitInfo]]:
        """로그 hit 가 **정말 이 계정 것인지** usage API 로 확인한다.

        ★ 왜 필요한가 (실측 2026-08-15): 세션 로그의 rate-limit 라인에는 계정이 적혀 있지
          않다. 실행 중 claude 세션은 자격증명을 메모리에 들고 있어 전환 뒤에도 **옛 계정의**
          한도 에러를 계속 뱉는다(동시 세션 4개, 2분 반에 걸쳐 6줄). 그 사이 전환이 일어나면
          한 계정의 소진이 다른 계정에 기록되고, 멀쩡한 폴백이 `is_limited` 로 후보에서
          빠져 **자동 전환이 통째로 죽는다**(실측: 7일 16% 쓴 계정에 100% 계정의 리셋 시각이
          덮였다). hit 의 자체 타임스탬프로는 못 거른다 — 전환 뒤에 찍히기 때문이다.
          usage 는 **계정별 토큰으로 조회**하므로 오귀인이 구조적으로 불가능하다.
          (upstream 이 needsReauth 에 대해 같은 결론을 내린 것과 같은 원칙 — AppState.swift:863.)

        ★ 부수 효과: 리셋 시각도 정확해진다. 로그의 "resets 9pm" 파싱 대신 API 가 주는
          실제 값을 쓴다(실측 차이: 21:00 vs 20:59).

        ★ **신선도는 나이가 아니라 순서로 판단한다.** "최근 확인했으면 스킵"은 40초 전
          96% 였던 스냅샷으로 방금 100% 가 된 창을 "여유"라고 오판한다. 그래서 낡은
          스냅샷을 재사용하지 않는다 — 매 판정은 **트리거보다 나중에** 뜬 조회로만
          내리고, 조회를 못 하면 판정하지 않는다(UNKNOWN). 순서 조건이 구조적으로 보장된다.

        네트워크 비용: 이미 기록이 있으면 0(버스트 전체가 공짜), 조회 간격 하한이 있고,
        평시(hit 없음)에는 호출 자체가 없다.
        """
        profile = next((a for a in self.store.file.accounts if a.id == account_id), None)
        if profile is None:
            return (Verdict.REFUTED, None)
        if profile.is_limited(now):
            return (Verdict.REFUTED, None)  # 이미 기록됨 — 같은 소진의 후속 hit 는 확인 불필요
        if (self._last_usage_verify.get(account_id, float("-inf"))
                >= now - USAGE_VERIFY_MIN_INTERVAL):
            return (Verdict.UNKNOWN, None)  # 조회 간격 하한 — 버리지 않고 보류한다
        self._last_usage_verify[account_id] = now

        # 활성 계정은 라이브 토큰으로 — claude 가 갱신하므로 저장 스냅샷은 낡아 401 오탐이 난다.
        snap = (self.io.read_live_snapshot() if account_id == self.store.file.active_account_id
                else self.store.secret(account_id))
        if snap is None:
            return (Verdict.UNKNOWN, None)
        try:
            usage_snap = usage.fetch(snap.credentials_blob, now, self.usage_transport)
        except Exception:
            return (Verdict.UNKNOWN, None)  # 네트워크/401 — 한도에 대해 아무 말도 안 한다
        if usage_snap is None:
            return (Verdict.UNKNOWN, None)  # 200 아님/파싱 실패 — 판단 근거 없음

        verdict, reset = verify_verdict(usage_snap, hit, now)
        if verdict != Verdict.CONFIRMED:
            return (verdict, None)
        if reset is None:  # API 가 시각을 안 줬을 때만(지출 한도) 로그/24h 폴백에 기댄다
            reset = hit.effective_resets_at(now)
        return (verdict, RateLimitInfo(resets_at=reset, recorded_at=now,
                                       model_scoped=hit.model_scoped))

    def _recheck_flagged_accounts(self, now: float) -> None:
        """needsReauth 딱지가 붙은 계정을 usage 조회로 재검사해, 살아있으면 딱지를 푼다.

        upstream 정본은 AppState.refreshUsageIfStale (Sources/MobiusApp/AppState.swift:313-315,
        377-381): 딱지 붙은 계정도 조회 대상에서 빼지 않고, 200이 오면 "잘못 남은 재로그인
        마킹 자가 해제". 사용자가 `claude /login` 으로 직접 복구한 경우가 이 경로로만 감지된다.

        포팅에서 이 경로가 통째로 누락돼 usage.py 가 호출되지 않는 죽은 모듈이었고, 그 결과
        딱지가 단조 상태(한 번 붙으면 수동 `mobius capture` 전까지 안 풀림)가 됐다.

        ★ 해제만 한다 — 401 을 딱지로 승격하지 않는다. 자연 만료 토큰의 401 은 오탐이고
          (upstream 이슈 #4: 오마킹 → 엔진이 멀쩡한 주계정을 밀어냄), 마킹 판정은
          refresh 결과를 보는 FallbackAuthChecker 의 몫이다.
        """
        active = self.store.file.active_account_id
        for p in list(self.store.file.accounts):
            if not p.needs_reauth:
                continue
            if self._last_reauth_recheck.get(p.id, float("-inf")) >= now - REAUTH_RECHECK_INTERVAL:
                continue
            self._last_reauth_recheck[p.id] = now

            # 활성이면 라이브 토큰으로, 비활성이면 저장 스냅샷으로 조회한다 — claude 가
            # 라이브 토큰을 갱신하므로 활성 계정에 저장본을 쓰면 401 오탐이 난다
            # (upstream AppState.swift:326-331).
            if p.id == active:
                snap = self.io.read_live_snapshot()
            else:
                snap = self.store.secret(p.id)
            if snap is None:
                continue
            try:
                if usage.fetch(snap.credentials_blob, now, self.usage_transport) is None:
                    continue          # 200 아님/파싱 실패 — 판단 근거 없음
            except usage.Unauthorized:
                continue              # 401/403 — 여전히 죽음. 마킹은 이미 돼 있다.
            except Exception:
                continue              # 네트워크 오류 — 토큰 문제가 아니다
            try:
                self.store.set_needs_reauth(p.id, False)
            except Exception:
                continue
            self._last_reauth_recheck.pop(p.id, None)
            notify("✅ 로그인 회복 감지", f"{p.nickname} 계정이 다시 사용 가능합니다.")

    def _clear_expired_rate_limits(self, now: float) -> None:
        """리셋 시각이 지난 rateLimit 레코드를 지운다.

        is_limited() 는 resets_at 을 올바르게 비교하므로 판정 자체는 stale 레코드에도
        맞다. 그러나 레코드가 남으면 primary 복귀 게이트(on_tick)와 `mobius list` 표시가
        지난 한도를 계속 참조하고, 무엇이 실제로 소진 상태인지 사람이 볼 수 없게 된다.
        """
        for p in list(self.store.file.accounts):
            rl = p.rate_limit
            if rl is None or now < rl.resets_at:
                continue
            try:
                self.store.update(p.id, lambda x: setattr(x, "rate_limit", None))
            except Exception:
                pass

    def _preflight_fallback(self, account_id: str, now: float) -> bool:
        r = self.fallback_checker.check(account_id, self.store.file.active_account_id,
                                        now, allow_network=True)
        if r in _DEAD_RESULTS:
            notify("재로그인 필요", f"{self._nick(account_id)} 계정의 로그인이 만료돼 전환을 건너뛰었어요. '다시 로그인' 하세요.")
            return False
        return True  # refreshed_alive / transient / not_fallback → 전환 진행

    def _proactive_refresh_expiring_fallbacks(self, now: float) -> None:
        active = self.store.file.active_account_id
        for p in list(self.store.file.accounts):
            if p.id == active or p.needs_reauth:
                continue
            snap = self.store.secret(p.id)
            if snap is None:
                continue
            exp = credentials.refresh_token_expires_at(snap.credentials_blob)
            if exp is None or exp - now >= PROACTIVE_RENEW_WINDOW:
                continue
            if self._last_proactive_refresh.get(p.id, float("-inf")) >= now - PROACTIVE_PER_ACCOUNT_GATE:
                continue
            self._last_proactive_refresh[p.id] = now
            r = self.fallback_checker.check(p.id, active, now, allow_network=True)
            if r in _DEAD_RESULTS:
                notify("재로그인 필요", f"{self._nick(p.id)} 계정의 로그인이 만료됐어요. '다시 로그인' 하세요.")

    def _nick(self, account_id: Optional[str]) -> str:
        p = next((a for a in self.store.file.accounts if a.id == account_id), None)
        return p.nickname if p else "?"
