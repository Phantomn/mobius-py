"""Codex CLI의 ChatGPT OAuth 상태를 읽는다.

토큰은 ~/.codex/auth.json에서 읽기만 하며, Mobius 저장소에는 복사하지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class CodexAuth:
    access_token: str
    account_id: str
    raw_blob: bytes = b""


class CodexAuthError(Exception):
    """Codex 인증 파일이 없거나 필요한 필드가 잘못됐다."""


class CodexConfigIO:
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()

    @property
    def auth_file(self) -> Path:
        return self.home / ".codex" / "auth.json"

    def read_auth(self) -> CodexAuth:
        try:
            data = self.auth_file.read_bytes()
            obj = json.loads(data)
        except (OSError, ValueError) as exc:
            raise CodexAuthError("Codex 로그인 상태를 읽을 수 없습니다") from exc
        tokens = obj.get("tokens") if isinstance(obj, dict) else None
        access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
        account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise CodexAuthError("Codex access token이 없습니다")
        if not isinstance(account_id, str) or not account_id:
            raise CodexAuthError("Codex account id가 없습니다")
        return CodexAuth(access_token=access_token, account_id=account_id, raw_blob=data)

    def write_auth(self, raw_blob: bytes) -> None:
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        from .fsutil import write_atomic
        write_atomic(raw_blob, self.auth_file, 0o600)
