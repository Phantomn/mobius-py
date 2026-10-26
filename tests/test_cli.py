from mobius import cli
from mobius.store import AccountStore
from tests.conftest import write_live
from tests.test_store import _snap


def _two_accounts(env):
    write_live(env, tag="A", email="a@x")  # a 가 claude 에 로그인된 라이브 계정
    store = AccountStore(env)
    a = store.upsert_profile("a", _snap("a@x", "A"))
    b = store.upsert_profile("b", _snap("b@x", "B"))
    store.file.active_account_id = a.id
    store.save()
    return a, b


def test_remove_inactive_account(env):
    a, b = _two_accounts(env)
    assert cli.main(["remove", "b"]) == 0
    store = AccountStore(env)
    assert [p.id for p in store.file.accounts] == [a.id]
    assert not env.secret_file(b.id).exists()


def test_remove_refuses_active_account(env):
    a, _ = _two_accounts(env)
    assert cli.main(["remove", "a"]) == 1
    assert len(AccountStore(env).file.accounts) == 2
    assert env.secret_file(a.id).exists()


def test_remove_unknown_name(env):
    _two_accounts(env)
    assert cli.main(["remove", "nope"]) == 1


def test_list_queries_usage_with_rotated_live_token(env, monkeypatch):
    # claude 가 활성 계정 토큰을 교체한 직후(데몬 싱크 전) — 저장 사본(at-A)은 낡았다.
    a, _ = _two_accounts(env)
    write_live(env, tag="A2", email="a@x")
    monkeypatch.setattr("mobius.claude_config.time.sleep", lambda _s: None)
    seen = []
    monkeypatch.setattr(cli.usage, "fetch", lambda blob, *_: seen.append(blob) or None)

    assert cli.main(["list"]) == 0
    assert b"at-A2" in seen[0]
    assert b"at-A2" in AccountStore(env).secret(a.id).credentials_blob


def test_usage_failure_reasons():
    from tests.conftest import creds_blob
    now = 1_800_000_000.0
    valid, expired = creds_blob(), creds_blob(expires_ms=1_700_000_000_000)
    assert cli._usage_failure(valid, [], OSError("timed out"), now) == "네트워크 오류 — timed out"
    assert cli._usage_failure(b"{}", [], None, now) == "저장된 토큰 없음"
    assert cli._usage_failure(expired, [(429, b"")], None, now) == "저장된 토큰 만료"
    assert cli._usage_failure(valid, [(401, b"")], cli.usage.Unauthorized(), now).startswith("토큰 거부")
    assert cli._usage_failure(valid, [(429, b"")], None, now) == "요청 제한(429)"
    assert cli._usage_failure(valid, [(200, b"{}")], None, now) == "응답 해석 실패"
    assert cli._usage_failure(valid, [(500, b"")], None, now) == "HTTP 500"


def test_list_shows_usage_failure_reason(env, monkeypatch, capsys):
    _two_accounts(env)
    monkeypatch.setattr("mobius.claude_config.time.sleep", lambda _s: None)
    monkeypatch.setattr(cli.usage, "_default_transport", lambda tok: (429, b""))

    assert cli.main(["list"]) == 0
    assert "[사용량 조회 실패: 요청 제한(429)]" in capsys.readouterr().out
