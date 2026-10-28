"""계정 전환 엔진 — Switcher.swift 포팅.

순서: 라이브 되저장 → 대상 기록 → 실패 시 롤백. reconcile/adopt는 이메일(승인창 없음)로
먼저 판별하고 실제 변화가 있을 때만 자격증명을 만진다.
"""

from __future__ import annotations

from typing import Optional

from .claude_config import ClaudeConfigIO
from .codex_config import CodexConfigIO
from .models import AccountProfile
from .store import AccountStore, UnknownAccount


class NoStoredSecret(Exception):
    pass


class Switcher:
    def __init__(self, store: AccountStore, io: ClaudeConfigIO,
                 codex_io: Optional[CodexConfigIO] = None):
        self.store = store
        self.io = io
        self.codex_io = codex_io or CodexConfigIO(store.env.home)

    def resave_live_into_matching_profile(self) -> Optional[str]:
        """현재 라이브 상태를, email이 일치하는 프로필에 되저장한다. 반환: 프로필 id."""
        live = self.io.read_live_snapshot()
        if live is None:
            return None
        email = self.io.live_email()
        if email is None:
            return None
        profile = next((a for a in self.store.file.accounts if a.email_address == email), None)
        if profile is None:
            return None
        self.store.set_secret(live, profile.id)
        return profile.id

    def refresh_active_snapshot_if_stable(self) -> bool:
        """활성 계정 스냅샷을 라이브(갱신된 토큰)와 동기화한다. 안정 읽기로 레이스 회피.

        반환: **이번 호출에서 실제로 새 스냅샷을 썼는지.** 호출자는 이 값으로 "활성 계정의
        저장 secret 이 이번 사이클 기준으로 신선하다"를 알고, 그 덕에 라이브를 한 번 더
        읽지 않는다(임계값 폴이 이 계약에 얹힌다). 그래서 이 불리언은 **신선도 계약** 자체다 —
        쓰기가 실패했는데 True 를 돌려주면 호출자가 낡은 스냅샷을 신선하다고 믿고 조용히 틀린다.
        """
        email = self.io.live_email()
        if email is None:
            return False
        profile = next((a for a in self.store.file.accounts if a.email_address == email), None)
        if profile is None or profile.id != self.store.file.active_account_id:
            return False
        stable = self.io.read_stable_live_snapshot()
        if stable is None:
            return False
        live, stable_email = stable
        if stable_email != email:
            return False
        try:
            self.store.set_secret(live, profile.id)
            return True
        except Exception:
            return False  # 디스크 실패 등 — 신선하다고 보고하면 안 된다(위 계약 참조)

    def switch_to(self, account_id: str) -> None:
        if not any(a.id == account_id for a in self.store.file.accounts):
            raise UnknownAccount()
        claude_target = self.store.secret(account_id)
        codex_target = self.store.codex_secret(account_id)
        if claude_target is None and codex_target is None:
            raise NoStoredSecret()

        # 1. 라이브 최신 토큰 되저장 (CLI가 refresh했을 수 있으므로)
        before_claude = self.io.read_live_snapshot()
        before_codex = None
        try:
            before_codex = self.codex_io.read_auth()
        except Exception:
            pass
        self.resave_live_into_matching_profile()

        # 2. 대상 기록, 실패 시 롤백
        try:
            if claude_target is not None:
                self.io.write_live_snapshot(claude_target)
            if codex_target is not None:
                self.codex_io.write_auth(codex_target.auth_blob)
        except Exception:
            if before_claude is not None:
                try:
                    self.io.write_live_snapshot(before_claude)
                except Exception:
                    pass
            if before_codex is not None:
                try:
                    self.codex_io.write_auth(before_codex.raw_blob)
                except Exception:
                    pass
            raise
        self.store.set_active(account_id)

    def adopt_live_account_if_unregistered(self) -> Optional[AccountProfile]:
        """현재 claude 로그인 계정이 미등록이면 자동 흡수(부트스트랩)."""
        email = self.io.live_email()
        if email is None or any(a.email_address == email for a in self.store.file.accounts):
            return None
        stable = self.io.read_stable_live_snapshot()
        if stable is None:
            return None
        live, stable_email = stable
        if stable_email != email:
            return None
        nickname = email.split("@", 1)[0] or "account"
        profile = self.store.upsert_profile(nickname, live)
        self.store.set_active(profile.id)
        return profile

    def adopt_codex_account_if_unregistered(self) -> Optional[AccountProfile]:
        """현재 Codex 인증을 계정 풀에 자동 채택한다."""
        try:
            live = self.codex_io.read_snapshot()
        except Exception:
            return None
        existing = next((a for a in self.store.file.accounts
                         if a.codex_account_id == live.account_id), None)
        if existing is not None:
            if self.store.codex_secret(existing.id) is None:
                self.store.set_codex_secret(live, existing.id)
            return None
        nickname = f"account-{live.account_id[-8:]}"
        profile = self.store.upsert_codex_profile(nickname, live)
        if self.store.file.active_account_id is None:
            self.store.set_active(profile.id)
        return profile

    def reconcile(self) -> None:
        """외부 재로그인 감지 시 상태 대사: 라이브 email이 아는 프로필이면 활성 표시+토큰 흡수."""
        active = self.store.file.active
        # Codex-only 계정이 활성인 동안 Claude의 이전 라이브 자격을 보고 active를 되돌리면
        # 통합 계정 풀의 switch 결과가 매 list/status 호출마다 사라진다.
        if active is not None and self.store.secret(active.id) is None:
            return
        email = self.io.live_email()
        if email is None:
            return
        profile = next((a for a in self.store.file.accounts if a.email_address == email), None)
        if profile is None:
            return
        active_unchanged = self.store.file.active_account_id == profile.id
        already_has_secret = self.store.secret(profile.id) is not None
        if active_unchanged and already_has_secret and not profile.needs_reauth:
            return  # 정상 상태 — 자격증명 접근 없음
        # needs_reauth 인데 라이브가 이 계정이면 = claude 가 이 자격증명으로 동작 중 =
        # 딱지의 전제(refresh 토큰 폐기)가 거짓. 아래 set_secret 이 해제한다.

        stable = self.io.read_stable_live_snapshot()
        if stable is None:
            return
        live, stable_email = stable
        if stable_email != email:
            return
        self.store.set_secret(live, profile.id)
        if not active_unchanged:
            self.store.set_active(profile.id)
            # 외부 로그인으로 활성이 바뀐 것 — 자동 복귀를 막는다.
            self.store.set_auto_switched_from_primary(False)
