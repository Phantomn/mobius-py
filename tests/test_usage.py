import json

import pytest

from mobius import usage
from mobius.usage import Unauthorized

NOW = 1_700_000_000.0


def test_parse_five_and_seven():
    data = json.dumps({
        "five_hour": {"utilization": 42, "resets_at": "2023-11-14T20:00:00Z"},
        "seven_day": {"utilization": 10.5, "resets_at": "2023-11-20T00:00:00Z"},
    }).encode()
    s = usage.parse(data, NOW)
    assert s.five_hour_percent == 42.0 and s.seven_day_percent == 10.5
    assert s.five_hour_resets_at is not None


def test_parse_scoped_limits():
    data = json.dumps({
        "five_hour": {"utilization": 1},
        "limits": [{"kind": "weekly_scoped", "percent": 88,
                    "scope": {"model": {"display_name": "Fable"}},
                    "resets_at": "2023-11-20T00:00:00Z"}],
    }).encode()
    s = usage.parse(data, NOW)
    assert len(s.scoped_limits) == 1 and s.scoped_limits[0].label == "Fable"
    assert s.scoped_limits[0].percent == 88.0


def test_parse_empty_returns_none():
    assert usage.parse(b"{}", NOW) is None


def test_fetch_unauthorized():
    with pytest.raises(Unauthorized):
        usage.fetch(json.dumps({"claudeAiOauth": {"accessToken": "t"}}).encode(),
                    NOW, transport=lambda tok: (401, b""))


def test_fetch_no_token_returns_none():
    assert usage.fetch(b"{}", NOW, transport=lambda tok: (200, b"{}")) is None


def test_fetch_goes_through_injected_transport(monkeypatch):
    """fetch 는 주입된 transport 로만 나간다 — 기본 HTTP 경로를 타면 실패.

    upstream b40c026 의 교훈: "전송 속성이 올바른가"만 보는 테스트는 회귀를 못 막는다.
    호출부가 기본 경로로 되돌아가도 안 쓰이는 속성은 그대로 남아 초록불이기 때문이다.
    그러므로 **fetch 가 주입 경로를 실제로 탔는지**를 단언한다.
    """
    def boom(*a, **k):
        raise AssertionError("기본 전송 경로가 사용됐다 — 주입이 무시됨")
    monkeypatch.setattr(usage, "_default_transport", boom)

    seen = []
    blob = json.dumps({"claudeAiOauth": {"accessToken": "tok-abc"}}).encode()
    body = json.dumps({"five_hour": {"utilization": 7}}).encode()

    snap = usage.fetch(blob, NOW, transport=lambda tok: seen.append(tok) or (200, body))

    assert seen == ["tok-abc"]        # 주입 경로가 실제로 호출됐고, 토큰이 전달됐다
    assert snap is not None and snap.five_hour_percent == 7.0
