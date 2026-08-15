import json

from mobius import credentials
from mobius.credentials import RefreshedTokens


def blob(**oauth):
    base = {"accessToken": "at", "refreshToken": "rt", "expiresAt": 1900000000000,
            "refreshTokenExpiresAt": 1900001000000, "scopes": ["s1", "s2"]}
    base.update(oauth)
    return json.dumps({"mcpOAuth": {"k": 1}, "claudeAiOauth": base}).encode()


def test_parse_wrapped_and_flat():
    assert credentials.access_token(blob()) == "at"
    flat = json.dumps({"accessToken": "flat", "refreshToken": "r"}).encode()
    assert credentials.access_token(flat) == "flat"


def test_empty_refresh_token_is_none():
    # 실패기록 14: 빈 refreshToken 은 손상 → None
    assert credentials.refresh_token(blob(refreshToken="")) is None
    assert credentials.refresh_token(blob()) == "rt"


def test_expires_at_ms_and_seconds():
    assert credentials.expires_at(blob(expiresAt=1900000000000)) == 1900000000.0
    assert credentials.expires_at(blob(expiresAt=1900000000)) == 1900000000.0


def test_is_refresh_token_expired():
    # 실측: refreshTokenExpiresAt 은 13자리 epoch ms (>1e12) → 초로 환산
    exp_sec = 1_900_000_000
    b = blob(refreshTokenExpiresAt=exp_sec * 1000)
    assert credentials.is_refresh_token_expired(b, now=exp_sec + 100) is True
    assert credentials.is_refresh_token_expired(b, now=exp_sec - 100) is False
    # 값 없으면 죽었다고 단정 안 함
    nb = json.dumps({"claudeAiOauth": {"accessToken": "a"}}).encode()
    assert credentials.is_refresh_token_expired(nb, now=1e12) is False


def test_refresh_token_rotated():
    """딱지 해제 근거는 **refresh 토큰 값의 교체**뿐 — 그 외에는 보수적으로 유지."""
    a = blob(refreshToken="R0")
    b = blob(refreshToken="R1")

    assert credentials.refresh_token_rotated(a, b) is True     # 회전 = 살아있다는 증거

    # 같은 토큰인데 다른 필드만 바뀜 → 유지. 라이브싱크가 5분마다 되저장하므로
    # 바이트 비교로 판정하면 죽은 계정의 딱지가 조용히 풀린다.
    assert credentials.refresh_token_rotated(a, blob(refreshToken="R0",
                                                     accessToken="다름")) is False
    assert credentials.refresh_token_rotated(a, a) is False

    # 모르면 유지 — 이전 스냅샷 없음/파싱 불가/토큰 없음(빈 문자열 포함)
    assert credentials.refresh_token_rotated(None, b) is False
    assert credentials.refresh_token_rotated(b"not json", b) is False
    assert credentials.refresh_token_rotated(a, b"not json") is False
    assert credentials.refresh_token_rotated(a, blob(refreshToken="")) is False
    assert credentials.refresh_token_rotated(blob(refreshToken=""), b) is False


def test_rebuild_preserves_other_fields():
    b = blob()
    t = RefreshedTokens(access_token="NEW", refresh_token="NEWRT", expires_at_ms=123,
                        refresh_token_expires_at_ms=456, scopes=["z"])
    out = json.loads(credentials.rebuild(b, t))
    assert out["mcpOAuth"] == {"k": 1}                # 다른 필드 보존
    assert out["claudeAiOauth"]["accessToken"] == "NEW"
    assert out["claudeAiOauth"]["refreshToken"] == "NEWRT"
    assert out["claudeAiOauth"]["expiresAt"] == 123
    assert out["claudeAiOauth"]["refreshTokenExpiresAt"] == 456
    assert out["claudeAiOauth"]["scopes"] == ["z"]
