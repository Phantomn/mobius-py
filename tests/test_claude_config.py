from mobius.claude_config import ClaudeConfigIO
from tests.conftest import creds_blob, write_live


def test_read_live_and_email(env):
    write_live(env, tag="A", email="a@example.com")
    io = ClaudeConfigIO(env)
    snap = io.read_live_snapshot()
    assert snap is not None and snap.credentials_blob == creds_blob("A")
    assert snap.oauth_account["emailAddress"] == "a@example.com"
    assert io.live_email() == "a@example.com"


def test_read_live_none_when_logged_out(env):
    io = ClaudeConfigIO(env)
    assert io.read_live_snapshot() is None


def test_write_preserves_other_keys(env):
    write_live(env, tag="A", email="a@example.com")
    io = ClaudeConfigIO(env)
    snap = io.read_live_snapshot()
    # 다른 계정 blob 으로 스왑
    from mobius.models import CredentialsSnapshot
    new = CredentialsSnapshot(credentials_blob=creds_blob("B"),
                              oauth_account={"emailAddress": "b@example.com"})
    io.write_live_snapshot(new)
    import json
    assert json.loads(env.credentials_file.read_bytes())["claudeAiOauth"]["accessToken"] == "at-B"
    cj = json.loads(env.claude_json.read_bytes())
    assert cj["oauthAccount"]["emailAddress"] == "b@example.com"
    assert cj["keep"] == "me"  # 다른 키 보존


def test_stable_snapshot_ok_when_unchanged(env):
    write_live(env, tag="A", email="a@example.com")
    io = ClaudeConfigIO(env)
    result = io.read_stable_live_snapshot(gap=0)
    assert result is not None
    _snap, email = result
    assert email == "a@example.com"


def test_stable_snapshot_none_on_race(env, monkeypatch):
    # 실패기록 2·9: 두 읽기가 다르면 None (토큰/이메일 순차 갱신 찰나)
    write_live(env, tag="A", email="a@example.com")
    io = ClaudeConfigIO(env)
    calls = {"n": 0}
    real = io.read_live_snapshot

    def flapping():
        calls["n"] += 1
        if calls["n"] == 2:
            from mobius.models import CredentialsSnapshot
            return CredentialsSnapshot(credentials_blob=creds_blob("DIFFERENT"), oauth_account={})
        return real()

    monkeypatch.setattr(io, "read_live_snapshot", flapping)
    assert io.read_stable_live_snapshot(gap=0) is None
