"""Claude Code 자격증명 2곳(.credentials.json / ~/.claude.json oauthAccount)의 읽기·쓰기.

ClaudeConfigIO.swift 의 Linux판. macOS와 달리 Keychain이 없으므로 .credentials.json 파일이
곧 토큰의 진실의 원천이다(Keychain 폴백 없음).
"""

from __future__ import annotations

import json
import time
from typing import Optional

from .env import MobiusEnvironment
from .fsutil import write_atomic
from .models import CredentialsSnapshot


class MalformedClaudeJSON(Exception):
    pass


class ClaudeConfigIO:
    def __init__(self, env: MobiusEnvironment):
        self.env = env

    # ---------- 읽기 ----------

    def read_live_snapshot(self) -> Optional[CredentialsSnapshot]:
        """현재 로그인 상태의 스냅샷. 로그아웃(자격증명 파일 없음/빈 파일)이면 None."""
        try:
            blob = self.env.credentials_file.read_bytes()
        except OSError:
            return None
        if not blob:
            return None
        return CredentialsSnapshot(credentials_blob=blob,
                                   oauth_account=self.read_oauth_account_dict())

    def read_oauth_account_dict(self) -> Optional[dict]:
        try:
            data = self.env.claude_json.read_bytes()
        except OSError:
            return None
        try:
            obj = json.loads(data)
        except ValueError:
            raise MalformedClaudeJSON()
        if not isinstance(obj, dict):
            raise MalformedClaudeJSON()
        acct = obj.get("oauthAccount")
        return acct if isinstance(acct, dict) else None

    def live_email(self) -> Optional[str]:
        acct = self.read_oauth_account_dict()
        if not acct:
            return None
        email = acct.get("emailAddress")
        return email if isinstance(email, str) else None

    def read_stable_live_snapshot(self, gap: float = 0.7):
        """토큰+이메일을 간격 두고 두 번 읽어 **값이 일치할 때만** 반환한다.

        로그인/전환 도중 토큰과 이메일이 순차 갱신되는 찰나엔 두 읽기가 달라지므로 None을
        반환해 "새 토큰 + 옛 이메일" 오저장을 막는다(실패기록 2·9). 반환: (snapshot, email).
        """
        s1 = self.read_live_snapshot()
        try:
            e1 = self.live_email()
        except MalformedClaudeJSON:
            return None
        if s1 is None or e1 is None:
            return None
        time.sleep(gap)
        s2 = self.read_live_snapshot()
        try:
            e2 = self.live_email()
        except MalformedClaudeJSON:
            return None
        if s2 is None or e2 is None:
            return None
        if s1.credentials_blob != s2.credentials_blob or e1 != e2:
            return None
        return (s2, e2)

    # ---------- 쓰기 ----------

    def write_live_snapshot(self, snap: CredentialsSnapshot) -> None:
        write_atomic(snap.credentials_blob, self.env.credentials_file, 0o600)
        self._patch_oauth_account(snap.oauth_account)

    def _patch_oauth_account(self, oauth_account: Optional[dict]) -> None:
        obj: dict = {}
        try:
            data = self.env.claude_json.read_bytes()
        except OSError:
            data = None
        if data:
            try:
                existing = json.loads(data)
            except ValueError:
                raise MalformedClaudeJSON()
            if not isinstance(existing, dict):
                raise MalformedClaudeJSON()
            obj = existing
        if oauth_account is not None:
            obj["oauthAccount"] = oauth_account
        else:
            obj.pop("oauthAccount", None)
        out = json.dumps(obj, sort_keys=True).encode("utf-8")
        write_atomic(out, self.env.claude_json, 0o600)
