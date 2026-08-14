import json

import pytest

from mobius import token_refresher as tr
from mobius.token_refresher import InvalidGrant, Malformed, OAuthTokenRefresher, Transient

NOW = 1_700_000_000.0


def test_build_body():
    body = json.loads(tr.build_body("myrt", ["s1", "s2"]))
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "myrt"
    assert body["client_id"] == tr.CLIENT_ID
    assert body["scope"] == "s1 s2"


def test_parse_200_rotation():
    data = json.dumps({"access_token": "AT", "refresh_token": "RT2", "expires_in": 3600,
                       "refresh_token_expires_in": 100, "scope": "a b"}).encode()
    t = tr.parse_response(200, data, NOW)
    assert t.access_token == "AT" and t.refresh_token == "RT2"
    assert t.expires_at_ms == int(NOW * 1000) + 3600 * 1000
    assert t.refresh_token_expires_at_ms == int(NOW * 1000) + 100 * 1000
    assert t.scopes == ["a", "b"]


def test_parse_invalid_grant():
    with pytest.raises(InvalidGrant):
        tr.parse_response(400, json.dumps({"error": "invalid_grant"}).encode(), NOW)


def test_parse_other_4xx_is_transient():
    # invalid_request(형식 거부)는 폐기가 아니라 transient — 실패기록 14
    with pytest.raises(Transient):
        tr.parse_response(400, json.dumps({"error": "invalid_request"}).encode(), NOW)


def test_parse_5xx_transient():
    with pytest.raises(Transient):
        tr.parse_response(503, b"", NOW)


def test_parse_malformed_200():
    with pytest.raises(Malformed):
        tr.parse_response(200, b"{}", NOW)


def test_refresh_with_injected_transport():
    data = json.dumps({"access_token": "X", "refresh_token": "Y", "expires_in": 60}).encode()
    r = OAuthTokenRefresher(transport=lambda body: (200, data))
    t = r.refresh("rt", ["s"], NOW)
    assert t.access_token == "X"


def test_refresh_network_error_is_transient():
    def boom(_body):
        raise OSError("network down")
    with pytest.raises(Transient):
        OAuthTokenRefresher(transport=boom).refresh("rt", ["s"], NOW)
