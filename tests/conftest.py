import json
import os
from pathlib import Path

import pytest

from mobius.env import MobiusEnvironment


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
