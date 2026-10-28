import json

import pytest

from mobius.codex_config import CodexAuthError, CodexConfigIO
from mobius.codex_usage import Unauthorized, fetch, parse


NOW = 1_700_000_000.0


def body():
    return json.dumps({
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {"used_percent": 42, "reset_at": "2026-01-01T00:00:00Z",
                                "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 8, "reset_at": 1700100000,
                                  "limit_window_seconds": 604800},
        },
    }).encode()


def test_parse_codex_rate_limit_windows():
    snap = parse(body(), NOW)
    assert snap is not None
    assert snap.plan_type == "pro"
    assert [(w.label, w.used_percent, w.window_seconds) for w in snap.windows] == [
        ("주요 창", 42.0, 18000), ("보조 창", 8.0, 604800)]


def test_fetch_passes_token_and_account_id():
    seen = []
    auth = type("Auth", (), {"access_token": "secret", "account_id": "acct"})()
    snap = fetch(auth, NOW, transport=lambda token, account: seen.append((token, account)) or (200, body()))
    assert snap is not None and seen == [("secret", "acct")]


def test_fetch_rejects_unauthorized():
    auth = type("Auth", (), {"access_token": "secret", "account_id": "acct"})()
    with pytest.raises(Unauthorized):
        fetch(auth, NOW, transport=lambda *_: (401, b""))


def test_codex_config_reads_auth_without_storing_token(tmp_path):
    auth_file = tmp_path / ".codex" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text(json.dumps({"tokens": {"access_token": "secret", "account_id": "acct"}}))
    auth = CodexConfigIO(tmp_path).read_auth()
    assert auth.access_token == "secret"
    assert auth.account_id == "acct"


def test_codex_config_rejects_missing_account_id(tmp_path):
    auth_file = tmp_path / ".codex" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text(json.dumps({"tokens": {"access_token": "secret"}}))
    with pytest.raises(CodexAuthError):
        CodexConfigIO(tmp_path).read_auth()
