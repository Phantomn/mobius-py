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


def refresh_token_rotated(previous: Optional[bytes], nxt: bytes) -> bool:
    """needsReauth 딱지를 내려도 되는가 — refresh 토큰이 **다른 값으로 교체**됐는가.

    딱지의 의미는 정확히 하나다: **저장된 *그* refresh 토큰이 폐기됐다**(invalid_grant,
    시간 만료, 빈 토큰, 회전본 저장 실패). 그러므로 그 토큰이 다른 값으로 바뀌면 판정의
    전제가 사라진다. 교체가 일어나는 경로는 둘뿐이고 둘 다 살아있다는 증거다 —
    성공한 refresh(서버가 old 토큰을 소비하고 새 토큰을 발급) 또는 새 로그인.

    ★ **"저장 바이트가 바뀌면 해제"로 넓히지 말 것** — `refresh_active_snapshot_if_stable`이
      활성 계정 스냅샷을 5분마다 **무조건** 되저장한다. 계정이 진짜 죽어도 mcpOAuth 등
      Claude 인증과 무관한 필드가 바뀌면 blob은 달라지므로, 바이트를 신호로 삼으면 죽은
      계정의 딱지가 조용히 풀리고 엔진이 정상 후보로 취급한다(upstream 이슈 #14 리뷰 지적).

    파싱 불가·토큰 없음(빈 문자열 포함)이면 **False**로 보수적으로 물러난다 — 모르면
    딱지를 유지한다(살아있다고 단정하지 않는다).
    """
    if previous is None:
        return False
    old = refresh_token(previous)
    new = refresh_token(nxt)
    if old is None or new is None:
        return False
    return old != new


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
