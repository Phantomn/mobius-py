"""프로필 영속(accounts.json) + 비밀 스냅샷(0600 파일) — AccountStore.swift 포팅.

★ 관대 디코드 + 손상 백업(실패기록 13): accounts.json 디코드 실패 시 원본을
accounts.corrupt.json 으로 백업한 뒤 예외를 올린다(빈 스토어가 덮어써도 복구 가능).
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from . import credentials
from .env import MobiusEnvironment
from .fsutil import write_atomic
from .models import AccountProfile, AccountsFile, CredentialsSnapshot, new_account_id


class AccountStoreError(Exception):
    pass


class SnapshotMissingEmail(AccountStoreError):
    pass


class CannotMovePrimary(AccountStoreError):
    pass


class UnknownAccount(AccountStoreError):
    pass


def _tier_description(block: dict) -> str:
    tier = block.get("organizationRateLimitTier") or block.get("organizationType") or ""
    s = (str(tier)
         .replace("default_", "")
         .replace("claude_", "")
         .replace("_", " "))
    # Swift .capitalized 동등: 각 단어 첫 글자만 대문자, 나머지 소문자.
    return " ".join(w[:1].upper() + w[1:].lower() for w in s.split(" "))


class AccountStore:
    def __init__(self, env: MobiusEnvironment):
        self.env = env
        try:
            data = env.accounts_file.read_bytes()
        except OSError:
            self.file = AccountsFile()
            return
        try:
            self.file = AccountsFile.from_dict(json.loads(data))
        except Exception:
            backup = env.accounts_file.parent / "accounts.corrupt.json"
            try:
                write_atomic(data, backup, 0o600)
            except OSError:
                pass
            raise

    def save(self) -> None:
        data = json.dumps(self.file.to_dict()).encode("utf-8")
        write_atomic(data, self.env.accounts_file, 0o600)

    def reload(self) -> None:
        """디스크에서 accounts.json을 다시 읽는다(외부 CLI 프로세스 변경 반영).

        읽기/디코드 실패 시 현재 메모리 상태를 유지한다(손상 파일이 덮어쓰지 않도록).
        """
        try:
            data = self.env.accounts_file.read_bytes()
        except OSError:
            return
        try:
            self.file = AccountsFile.from_dict(json.loads(data))
        except Exception:
            return

    # ---------- 프로필 ----------

    def upsert_profile(self, nickname: str, snapshot: CredentialsSnapshot) -> AccountProfile:
        block = snapshot.oauth_account
        if not block or not isinstance(block.get("emailAddress"), str):
            raise SnapshotMissingEmail()
        email = block["emailAddress"]
        org = str(block.get("organizationName") or "")
        tier = _tier_description(block)

        existing = next((a for a in self.file.accounts if a.email_address == email), None)
        if existing is not None:
            existing.nickname = nickname
            existing.organization_name = org
            existing.tier_description = tier
            existing.needs_reauth = False
            profile = existing
        else:
            profile = AccountProfile(id=new_account_id(), nickname=nickname,
                                     email_address=email, organization_name=org,
                                     tier_description=tier)
            self.file.accounts.append(profile)
            if self.file.active_account_id is None:
                self.file.active_account_id = profile.id
        self.set_secret(snapshot, profile.id)
        self.save()
        return profile

    # ---------- 비밀 스냅샷 (0600 파일) ----------

    def secret(self, account_id: str) -> Optional[CredentialsSnapshot]:
        try:
            data = self.env.secret_file(account_id).read_bytes()
        except OSError:
            return None
        return CredentialsSnapshot.from_dict(json.loads(data))

    def set_secret(self, snapshot: CredentialsSnapshot, account_id: str) -> None:
        # needsReauth 는 "저장된 refresh 토큰이 폐기됨"이라는 **그 토큰에 대한** 판정이다.
        # 토큰이 다른 값으로 교체되면 판정의 전제가 사라지므로 딱지를 내린다. 이 해제가
        # 없으면 재로그인/토큰 회전 뒤에도 딱지가 남고, 딱지 붙은 계정은 데몬 선제 스윕
        # (needs_reauth → skip)과 체커(활성 → NOT_FALLBACK) 양쪽에서 제외되어
        # 다시는 검사되지 않는다 = 일방향 래치.
        # ★ 순서 주의: 값싼 플래그 검사를 먼저 하고, 이전 스냅샷 읽기는 딱지가 붙어 있을
        #   때만 한다. 딱지는 드물다 — 정상 계정이 5분 라이브싱크마다 파일을 더 읽을 이유가 없다.
        profile = next((a for a in self.file.accounts if a.id == account_id), None)
        flagged = profile is not None and profile.needs_reauth
        prev = self.secret(account_id) if flagged else None

        self.env.secrets_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.env.secrets_dir.chmod(0o700)
        except OSError:
            pass
        data = json.dumps(snapshot.to_dict()).encode("utf-8")
        write_atomic(data, self.env.secret_file(account_id), 0o600)

        if flagged and credentials.refresh_token_rotated(
                prev.credentials_blob if prev else None, snapshot.credentials_blob):
            self.set_needs_reauth(account_id, False)

    # ---------- 상태 변경 ----------

    def set_active(self, account_id: Optional[str]) -> None:
        self.file.active_account_id = account_id
        self.save()

    def set_auto_switch(self, enabled: bool) -> None:
        self.file.auto_switch_enabled = enabled
        self.save()

    def set_auto_switched_from_primary(self, flagged: bool) -> None:
        self.file.auto_switched_from_primary = flagged
        self.save()

    def set_needs_reauth(self, account_id: str, flag: bool) -> None:
        p = self._find(account_id)
        if p.needs_reauth == flag:
            return
        p.needs_reauth = flag
        self.save()

    def set_user_pinned(self, account_id: str) -> None:
        """지정 계정에만 핀을 세운다(나머지 해제). 변화 있을 때만 저장."""
        changed = False
        for a in self.file.accounts:
            want = a.id == account_id
            if a.user_pinned != want:
                a.user_pinned = want
                changed = True
        if changed:
            self.save()

    def set_primary(self, account_id: str) -> None:
        idx = self._index(account_id)
        if idx == 0:
            return
        item = self.file.accounts.pop(idx)
        self.file.accounts.insert(0, item)
        self.file.auto_switched_from_primary = False
        self.save()

    def move_fallback(self, from_index: int, to_index: int) -> None:
        n = len(self.file.accounts)
        if not (from_index >= 1 and to_index >= 1 and from_index < n and to_index < n):
            raise CannotMovePrimary()
        item = self.file.accounts.pop(from_index)
        self.file.accounts.insert(to_index, item)
        self.save()

    def remove(self, account_id: str) -> None:
        if not any(a.id == account_id for a in self.file.accounts):
            raise UnknownAccount()
        self.file.accounts = [a for a in self.file.accounts if a.id != account_id]
        if self.file.active_account_id == account_id:
            self.file.active_account_id = self.file.accounts[0].id if self.file.accounts else None
        try:
            self.env.secret_file(account_id).unlink()
        except OSError:
            pass
        self.save()

    def update(self, account_id: str, mutate: Callable[[AccountProfile], None]) -> None:
        p = self._find(account_id)
        mutate(p)
        self.save()

    # ---------- 내부 ----------

    def _find(self, account_id: str) -> AccountProfile:
        p = next((a for a in self.file.accounts if a.id == account_id), None)
        if p is None:
            raise UnknownAccount()
        return p

    def _index(self, account_id: str) -> int:
        for i, a in enumerate(self.file.accounts):
            if a.id == account_id:
                return i
        raise UnknownAccount()
