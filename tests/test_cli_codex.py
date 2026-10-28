import json

from mobius import cli
from mobius.codex_usage import CodexUsageSnapshot, UsageWindow


def _write_auth(env):
    auth_file = env.home / ".codex" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text(json.dumps({"tokens": {"access_token": "at", "account_id": "account-12345678"}}))


def test_list_includes_codex_usage(env, monkeypatch, capsys):
    _write_auth(env)
    monkeypatch.setattr(cli.codex_usage, "fetch", lambda auth, now: CodexUsageSnapshot(
        "pro", (UsageWindow("주요 창", 42, now + 3600, 18000),), now))

    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "Codex 계정 (pro) <...12345678>" in out
    assert "주요 창" in out and "42%" in out


def test_status_includes_codex_usage(env, monkeypatch, capsys):
    _write_auth(env)
    monkeypatch.setattr(cli.codex_usage, "fetch", lambda auth, now: CodexUsageSnapshot(
        "plus", (UsageWindow("보조 창", 8, now + 3600, 604800),), now))

    assert cli.main(["status"]) == 0
    assert "Codex 계정 (plus)" in capsys.readouterr().out
