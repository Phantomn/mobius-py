"""자격증명 blob(JSON) 파싱/재구성 (TokenRefresher.swift 의 CredentialBlob 포팅).

blob 은 ~/.claude/.credentials.json 의 바이트다. 토큰은 claudeAiOauth 서브딕트에 있으며
(실측), 평면 형태도 허용한다(관용성). expiresAt/refreshTokenExpiresAt 는 13자리 epoch ms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class RefreshedTokens:
    access_token: str
    refresh_token: str            # 회전된 새 refresh 토큰 — 반드시 저장해야 함
    expires_at_ms: int            # epoch ms
    refresh_token_expires_at_ms: Optional[int]  # epoch ms (응답이 주면)
    scopes: Optional[list[str]]


def _loads(blob: bytes) -> Optional[dict]:
    try:
        obj = json.loads(blob)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _token_dict(obj: dict) -> dict:
    oauth = obj.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else obj


def _ms_to_seconds(raw: Any) -> Optional[float]:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    n = float(raw)
    return n / 1000.0 if n > 1e12 else n


def access_token(blob: bytes) -> Optional[str]:
    obj = _loads(blob)
    if obj is None:
        return None
    tok = _token_dict(obj).get("accessToken")
    return tok if isinstance(tok, str) else None


def refresh_token(blob: bytes) -> Optional[str]:
    """빈 문자열은 손상/미완성 스냅샷 → None으로 취급(재로그인 유도, 실패기록 14)."""
    obj = _loads(blob)
    if obj is None:
        return None
    rt = _token_dict(obj).get("refreshToken")
    if isinstance(rt, str) and rt:
        return rt
    return None


def scopes(blob: bytes) -> list[str]:
    obj = _loads(blob)
    if obj is None:
        return []
    sc = _token_dict(obj).get("scopes")
    return [str(s) for s in sc] if isinstance(sc, list) else []


def expires_at(blob: bytes) -> Optional[float]:
    """access token 만료 시각(epoch 초). ms/s 양쪽 허용."""
    obj = _loads(blob)
    if obj is None:
        return None
    return _ms_to_seconds(_token_dict(obj).get("expiresAt"))


def refresh_token_expires_at(blob: bytes) -> Optional[float]:
    obj = _loads(blob)
    if obj is None:
        return None
    return _ms_to_seconds(_token_dict(obj).get("refreshTokenExpiresAt"))


def is_refresh_token_expired(blob: bytes, now: float) -> bool:
    """네트워크 0 로컬 선검사: refresh 토큰이 **확실히** 만료됐는가.

    값이 없거나 미래면 False(죽었다고 단정하지 않음 — 오탐 방지).
    """
    exp = refresh_token_expires_at(blob)
    if exp is None:
        return False
    return exp < now


def rebuild(blob: bytes, tokens: RefreshedTokens) -> Optional[bytes]:
    """갱신된 토큰을 blob에 반영해 새 blob 바이트를 만든다(다른 필드 보존)."""
    obj = _loads(blob)
    if obj is None:
        return None
    target = obj["claudeAiOauth"] if isinstance(obj.get("claudeAiOauth"), dict) else obj
    target["accessToken"] = tokens.access_token
    target["refreshToken"] = tokens.refresh_token
    target["expiresAt"] = tokens.expires_at_ms
    if tokens.refresh_token_expires_at_ms is not None:
        target["refreshTokenExpiresAt"] = tokens.refresh_token_expires_at_ms
    if tokens.scopes:
        target["scopes"] = tokens.scopes
    return json.dumps(obj).encode("utf-8")
