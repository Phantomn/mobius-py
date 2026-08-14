"""폴백(비활성) 계정의 로그인 생사를 미리 판정 — FallbackAuthChecker.swift 포팅.

판정 신호는 refresh 결과다(모호한 usage 401 아님):
  - 로컬 선검사(네트워크 0): refreshTokenExpiresAt 지났으면 죽음 확정
  - refresh 성공 → 살아있음(+새 토큰 원자 저장)
  - invalid_grant → 죽음 확정 → 재로그인 필요
  - 네트워크/5xx → 일시적(죽음으로 단정 안 함)

★ 활성(라이브) 계정은 절대 refresh하지 않는다 — 첫 가드로 차단(세션 파괴 방지).
★ 원자성: refresh 성공 시 old refresh 토큰은 서버에서 이미 소비됨. 새 토큰 저장 실패 시
  needsReauth로 마킹해 재로그인으로 복구.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from . import credentials, token_refresher
from .models import CredentialsSnapshot
from .store import AccountStore
from .token_refresher import OAuthTokenRefresher


class FallbackCheckResult(Enum):
    NOT_FALLBACK = "not_fallback"        # 활성 계정 — 건드리지 않음
    NO_SECRET = "no_secret"
    NO_REFRESH_TOKEN = "no_refresh_token"
    LOCALLY_DEAD = "locally_dead"        # refreshTokenExpiresAt 지남(네트워크 0)
    REFRESHED_ALIVE = "refreshed_alive"  # refresh 성공 + 새 스냅샷 원자 저장
    DEAD = "dead"                        # invalid_grant → 재로그인 필요
    TRANSIENT = "transient"              # 네트워크/5xx → 마킹 안 함(재시도)
    STORE_FAILED = "store_failed"        # refresh 성공했으나 저장 실패 → 재로그인 필요로 마킹


class FallbackAuthChecker:
    def __init__(self, store: AccountStore, refresher: Optional[OAuthTokenRefresher] = None):
        self.store = store
        self.refresher = refresher or OAuthTokenRefresher()

    def check(self, account_id: str, active_account_id: Optional[str], now: float,
              allow_network: bool = True) -> FallbackCheckResult:
        if account_id == active_account_id:          # 활성 절대 제외
            return FallbackCheckResult.NOT_FALLBACK
        snap = self.store.secret(account_id)
        if snap is None:
            return FallbackCheckResult.NO_SECRET
        rt = credentials.refresh_token(snap.credentials_blob)
        if rt is None:
            self._mark(account_id, True)
            return FallbackCheckResult.NO_REFRESH_TOKEN
        if credentials.is_refresh_token_expired(snap.credentials_blob, now):
            self._mark(account_id, True)
            return FallbackCheckResult.LOCALLY_DEAD
        if not allow_network:                         # 로컬 검사 통과 — 네트워크 생략
            return FallbackCheckResult.TRANSIENT

        scopes = credentials.scopes(snap.credentials_blob)
        try:
            tokens = self.refresher.refresh(rt, scopes, now)
        except token_refresher.InvalidGrant:
            self._mark(account_id, True)
            return FallbackCheckResult.DEAD
        except Exception:
            return FallbackCheckResult.TRANSIENT     # 네트워크/5xx — 죽음으로 단정 안 함

        # 여기 도달 = old refresh 토큰은 서버에서 소비됨. 새 토큰을 반드시 저장해야 한다.
        new_blob = credentials.rebuild(snap.credentials_blob, tokens)
        if new_blob is None:
            self._mark(account_id, True)
            return FallbackCheckResult.STORE_FAILED
        new_snap = CredentialsSnapshot(credentials_blob=new_blob, oauth_account=snap.oauth_account)
        try:
            self.store.set_secret(new_snap, account_id)   # 원자 저장
            self._mark(account_id, False)                 # 살아있음 → 딱지 해제
            return FallbackCheckResult.REFRESHED_ALIVE
        except Exception:
            self._mark(account_id, True)
            return FallbackCheckResult.STORE_FAILED

    def _mark(self, account_id: str, flag: bool) -> None:
        try:
            self.store.set_needs_reauth(account_id, flag)
        except Exception:
            pass
