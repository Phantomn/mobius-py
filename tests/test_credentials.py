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
