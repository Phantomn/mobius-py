import json

from mobius import cli
from mobius.codex_usage import CodexUsageSnapshot, UsageWindow

def test_codex_usage_command_prints_windows(env, monkeypatch, capsys):
    auth_file = env.home / ".codex" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text(json.dumps({"tokens": {"access_token": "at", "account_id": "acct"}}))
    monkeypatch.setattr(cli.codex_usage, "fetch", lambda auth, now: CodexUsageSnapshot(
        "pro", (UsageWindow("주요 창", 42, now + 3600, 18000),), now))

    assert cli.main(["codex", "usage"]) == 0
    out = capsys.readouterr().out
    assert "Codex 사용량 (pro)" in out
    assert "주요 창" in out and "42%" in out


def test_codex_usage_command_requires_login(env, capsys):
    assert cli.main(["codex", "usage"]) == 1
    assert "Codex 로그인 상태" in capsys.readouterr().err
