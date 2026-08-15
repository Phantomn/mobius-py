"""계정 전환 엔진 — Switcher.swift 포팅.

순서: 라이브 되저장 → 대상 기록 → 실패 시 롤백. reconcile/adopt는 이메일(승인창 없음)로
먼저 판별하고 실제 변화가 있을 때만 자격증명을 만진다.
"""

from __future__ import annotations

from typing import Optional

from .claude_config import ClaudeConfigIO
from .models import AccountProfile
from .store import AccountStore, UnknownAccount


class NoStoredSecret(Exception):
    pass


class Switcher:
    def __init__(self, store: AccountStore, io: ClaudeConfigIO):
        self.store = store
        self.io = io

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
        target = self.store.secret(account_id)
        if target is None:
            raise NoStoredSecret()

        # 1. 라이브 최신 토큰 되저장 (CLI가 refresh했을 수 있으므로)
        before = self.io.read_live_snapshot()
        self.resave_live_into_matching_profile()

        # 2. 대상 기록, 실패 시 롤백
        try:
            self.io.write_live_snapshot(target)
        except Exception:
            if before is not None:
                try:
                    self.io.write_live_snapshot(before)
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

    def reconcile(self) -> None:
        """외부 재로그인 감지 시 상태 대사: 라이브 email이 아는 프로필이면 활성 표시+토큰 흡수."""
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
