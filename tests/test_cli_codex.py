import json

from mobius import cli
from mobius.codex_usage import CodexUsageSnapshot, UsageWindow
from mobius.store import AccountStore


def _write_auth(env):
    auth_file = env.home / ".codex" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text(json.dumps({"tokens": {"access_token": "at", "account_id": "account-12345678"}}))


def test_list_includes_codex_usage(env, monkeypatch, capsys):
    _write_auth(env)
    assert cli.main(["capture", "codex-account"]) == 0
    monkeypatch.setattr(cli.codex_usage, "fetch", lambda auth, now: CodexUsageSnapshot(
        "pro", (UsageWindow("primary", 42, now + 3600, 300),), now))

    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "primary   codex-account  <account-12345678>" in out
    assert "5시간      42%  리셋" in out


def test_status_includes_codex_usage(env, monkeypatch, capsys):
    _write_auth(env)
    assert cli.main(["capture", "codex-account"]) == 0
    monkeypatch.setattr(cli.codex_usage, "fetch", lambda auth, now: CodexUsageSnapshot(
        "plus", (UsageWindow("secondary", 8, now + 3600, 10080),), now))

    assert cli.main(["status"]) == 0
    assert "7일        8%  리셋" in capsys.readouterr().out


def test_capture_registers_codex_in_shared_account_pool(env, capsys):
    _write_auth(env)

    assert cli.main(["capture", "codex-account"]) == 0
    store = AccountStore(env)
    profile = store.file.accounts[0]
    assert profile.nickname == "codex-account"
    assert store.codex_secret(profile.id).account_id == "account-12345678"
    assert "캡처 완료" in capsys.readouterr().out
