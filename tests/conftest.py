import json
import os
from pathlib import Path

import pytest

from mobius.env import MobiusEnvironment


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """테스트는 절대 실제 HTTP 를 타지 않는다 — transport 주입을 빠뜨리면 즉시 실패한다.

    ★ 왜 (실측 2026-08-15): daemon 에 usage 검증(_verify_hit)을 넣자 기존 전환 테스트가
      실제 api.anthropic.com 으로 나갔다. 증상이 "네트워크로 나갔다"가 아니라 "전환이 안 됨"
      으로 보여 원인을 우회로로 찾았다. 이 가드가 있으면 한 줄로 끝난다.
      네트워크를 타는 경로는 계속 늘어난다(usage.fetch / token_refresher / _poll_threshold /
      _probe_candidate) — 사람이 기억하는 대신 하네스가 강제한다.

    ★ `socket.socket` 을 막지 말 것 — `ssl` 이 `class SSLSocket(socket)` 로 상속하므로
      import 자체가 깨진다(실제로 밟았다). 막을 지점은 `urlopen` 하나로 충분하다.

    ★ `AssertionError` 를 쓰지 말 것 — 네트워크 호출부는 대부분 `except Exception` 으로
      감싸여 있어(daemon._verify_hit / _poll_threshold / usage 조회 전반) 가드가 조용히
      삼켜진다. 차단은 되지만 실패 메시지가 "전환이 안 됨" 같은 **엉뚱한 증상**으로 나와
      원인을 다시 우회로로 찾게 된다(실제로 밟았다). `pytest.fail` 이 던지는 `Failed` 는
      `BaseException` 기반이라 `except Exception` 을 통과한다.
    """
    def blocked(*a, **k):
        pytest.fail("테스트가 실제 네트워크를 시도했다 — transport 를 주입할 것", pytrace=False)

    monkeypatch.setattr("urllib.request.urlopen", blocked)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("MOBIUS_HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return MobiusEnvironment.live()


def creds_blob(tag="A", expires_ms=1900000000000, refresh_token=None, rte_ms=None,
               scopes=("user:inference", "user:profile")):
    rt = f"rt-{tag}" if refresh_token is None else refresh_token
    return json.dumps({
        "mcpOAuth": {"x": tag},
        "claudeAiOauth": {
            "accessToken": f"at-{tag}", "refreshToken": rt,
            "expiresAt": expires_ms,
            "refreshTokenExpiresAt": rte_ms if rte_ms is not None else expires_ms + 1_000_000,
            "scopes": list(scopes), "subscriptionType": "max",
        },
        "designOauth": {"y": tag},
    }).encode("utf-8")


def write_live(env, tag="A", email="a@example.com", org="Org",
               tier_key="organizationRateLimitTier", tier="default_claude_max_20x", **kw):
    env.credentials_file.write_bytes(creds_blob(tag, **kw))
    env.claude_json.write_text(json.dumps({
        "oauthAccount": {"emailAddress": email, "organizationName": org, tier_key: tier},
        "keep": "me",
    }))
