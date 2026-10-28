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
    assert [(w.name, w.used_percent, w.window_minutes) for w in snap.windows] == [
        ("primary", 42.0, 300), ("secondary", 8.0, 10080)]


def test_parse_accepts_codex_window_minutes():
    data = json.dumps({"rate_limit": {"primary_window": {
        "used_percent": 3, "window_minutes": 300,
    }}}).encode()
    snap = parse(data, NOW)
    assert snap is not None and snap.windows[0].window_minutes == 300


def test_normalizes_windows_by_duration_not_primary_secondary_name():
    snap = parse(json.dumps({"rate_limit": {"primary_window": {
        "used_percent": 45, "window_minutes": 10080, "reset_at": 1700100000,
    }}}).encode(), NOW)
    usage = snap.as_usage_snapshot()
    assert usage.five_hour_percent is None
    assert usage.seven_day_percent == 45
    assert usage.seven_day_resets_at == 1700100000


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
