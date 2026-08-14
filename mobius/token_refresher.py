"""OAuth refresh 토큰으로 새 access 토큰 발급 — TokenRefresher.swift 포팅.

폴백(비활성) 계정의 로그인 생사 판정 + 토큰 회전 저장에 쓴다. **활성 계정은 절대 refresh
하지 않는다**(claude가 관리 → 동시 로테이션=세션 파괴) — 호출측(FallbackAuthChecker)이 차단.

상수·요청 형식은 claude 바이너리 실측값이다:
  POST https://platform.claude.com/v1/oauth/token
  Content-Type: application/json
  { grant_type, refresh_token, client_id, scope }
  200 → { access_token, refresh_token(회전), expires_in, refresh_token_expires_in?, scope? }

★ User-Agent 필수(실측): claude와 동일 UA로 맞춰 서버가 형식을 받아들이게 하고, 정상
  클라이언트와 구분되지 않게 한다. Python urllib은 헤더를 그대로 전송하므로 Swift의
  CFNetwork UA 무시 함정은 없다.
판정: 200=살아있음(+토큰 회전), invalid_grant=폐기(재로그인), 그 외 4xx/5xx/네트워크=transient.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Optional

from .credentials import RefreshedTokens

ENDPOINT = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
USER_AGENT = "claude-cli/2.1.208 (external, cli)"


class InvalidGrant(Exception):
    """refresh 토큰 폐기/만료 → 진짜 재로그인 필요 (확정 신호)."""


class Transient(Exception):
    """네트워크/5xx/기타 4xx → 일시적, 다음에 재시도 (죽음으로 단정 금지)."""


class Malformed(Exception):
    """200인데 응답 파싱 실패."""


def _to_int(v) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    return None


def build_body(refresh_token: str, scopes: list[str]) -> bytes:
    return json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "scope": " ".join(scopes),
    }).encode("utf-8")


def parse_response(status: int, data: bytes, now: float) -> RefreshedTokens:
    """응답 판정(순수 — 테스트로 회전/invalid_grant/5xx 검증)."""
    if status == 200:
        try:
            obj = json.loads(data)
            at = obj["access_token"]
            rt = obj["refresh_token"]
        except (ValueError, KeyError, TypeError):
            raise Malformed()
        if not isinstance(at, str) or not isinstance(rt, str):
            raise Malformed()
        now_ms = int(now * 1000)
        expires_at_ms = now_ms + (_to_int(obj.get("expires_in")) or 3600) * 1000
        rte_in = _to_int(obj.get("refresh_token_expires_in"))
        rte_ms = now_ms + rte_in * 1000 if rte_in is not None else None
        scope = obj.get("scope")
        scopes = scope.split(" ") if isinstance(scope, str) else None
        return RefreshedTokens(access_token=at, refresh_token=rt,
                               expires_at_ms=expires_at_ms,
                               refresh_token_expires_at_ms=rte_ms, scopes=scopes)
    # invalid_grant = refresh 토큰 폐기 → 확정 죽음. 그 외 오류는 오탐 방지 위해 transient.
    if 400 <= status <= 499:
        err = None
        try:
            err = json.loads(data).get("error")
        except (ValueError, AttributeError, TypeError):
            pass
        if err == "invalid_grant":
            raise InvalidGrant()
        raise Transient()
    raise Transient()


# HTTP 전송(주입식) — 테스트는 캐닝된 (status, data)를 넣는다.
Transport = Callable[[bytes], tuple]


def _default_transport(body: bytes) -> tuple:
    req = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return (resp.status, resp.read())
    except urllib.error.HTTPError as e:
        return (e.code, e.read())
    # URLError(네트워크) 등은 호출측(refresh)이 Transient 로 처리한다.


class OAuthTokenRefresher:
    def __init__(self, transport: Optional[Transport] = None):
        self._transport = transport or _default_transport

    def refresh(self, refresh_token: str, scopes: list[str], now: float) -> RefreshedTokens:
        body = build_body(refresh_token, scopes)
        try:
            status, data = self._transport(body)
        except Exception:
            raise Transient()  # 네트워크 실패 = 일시적
        return parse_response(status, data, now)
