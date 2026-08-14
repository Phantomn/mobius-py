"""자동 전환 데몬 — AppState.tick/apply/preflight/proactiveRefresh 포팅.

macOS 앱의 3초 타이머 루프를 foreground 프로세스로 옮긴 것. 게이트별 상수는 실측값.
매 틱: (외부 CLI 변경 반영 위해) store 리로드 → reconcile/adopt(15s) → 활성 스냅샷 동기화(5m)
→ 만료임박 폴백 선제 refresh(1h) → 세션 로그 스캔 → 엔진 결정 실행.
"""

from __future__ import annotations

import signal
import time
from typing import Optional

from . import credentials, usage
from .autoswitch import AutoSwitchEngine, Decision, DecisionKind, SwitchReason
from .claude_config import ClaudeConfigIO
from .env import MobiusEnvironment
from .fallback_check import FallbackAuthChecker, FallbackCheckResult
from .log_watcher import SessionLogWatcher
from .models import RateLimitInfo
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
                self.switcher.refresh_active_snapshot_if_stable()
            except Exception:
                pass

        self._clear_expired_rate_limits(now)
        self._recheck_flagged_accounts(now)

        if now - self._last_proactive_sweep >= PROACTIVE_SWEEP_INTERVAL:
            self._last_proactive_sweep = now
            self._proactive_refresh_expiring_fallbacks(now)

        # 배치 내 모든 hit는 스캔 시점의 활성 계정에 귀속.
        active_id = self.store.file.active_account_id
        for hit in self.watcher.scan(now):
            if active_id is not None:
                try:
                    self.store.update(active_id, lambda p, h=hit: setattr(
                        p, "rate_limit", RateLimitInfo(resets_at=h.effective_resets_at(now),
                                                       recorded_at=now, model_scoped=h.model_scoped)))
                except Exception:
                    pass
            self._apply(self.engine.on_rate_limit_hit(self.store.file, hit, now), now)
        self._apply(self.engine.on_tick(self.store.file, now), now)

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
        # SWITCH_TO
        target_id = decision.target_id
        reason = decision.reason
        # 자동 폴백으로 넘어가기 직전 실제 refresh로 대상 생사 확인 → 죽었으면 취소
        if reason == SwitchReason.ACTIVE_EXHAUSTED:
            if not self._preflight_fallback(target_id, now):
                return
        from_id = self.store.file.active_account_id
        try:
            self.switcher.switch_to(target_id)
        except Exception as e:
            print(f"[mobius] 자동 전환 실패: {e}", flush=True)
            return
        self.engine.note_switched(now)
        try:
            self.store.set_auto_switched_from_primary(reason == SwitchReason.ACTIVE_EXHAUSTED)
        except Exception:
            pass
        name = self._nick(target_id)
        if reason == SwitchReason.PRIMARY_RECOVERED:
            notify(f"✅ {name} 계정으로 복귀", "한도가 초기화돼 주 계정으로 돌아왔어요.")
        else:
            notify(f"🔄 {name} 계정으로 전환",
                   f"{self._nick(from_id)} 한도 소진 → {name}. 새 claude 세션부터 적용돼요.")

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
