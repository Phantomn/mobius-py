import json
import stat

import pytest

from mobius.models import CredentialsSnapshot
from mobius.store import AccountStore
from tests.conftest import creds_blob


def _snap(email="a@example.com", tag="A"):
    return CredentialsSnapshot(credentials_blob=creds_blob(tag),
                               oauth_account={"emailAddress": email, "organizationName": "Org",
                                              "organizationRateLimitTier": "default_claude_max_20x"})


def test_upsert_and_perms(env):
    store = AccountStore(env)
    p = store.upsert_profile("alice", _snap())
    assert p.tier_description == "Max 20x"
    # 0600 권한
    assert stat.S_IMODE(env.accounts_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.secret_file(p.id).stat().st_mode) == 0o600
    # 같은 이메일 재캡처 → 중복 아님
    store.upsert_profile("alice2", _snap())
    assert len(store.file.accounts) == 1 and store.file.accounts[0].nickname == "alice2"


def test_secret_roundtrip(env):
    store = AccountStore(env)
    p = store.upsert_profile("a", _snap())
    got = store.secret(p.id)
    assert got is not None and got.credentials_blob == creds_blob("A")
    assert got.oauth_account["emailAddress"] == "a@example.com"


def test_corrupt_backup(env):
    # 실패기록 13: 손상 파일은 백업 후 raise (빈 스토어가 덮어써도 복구 가능)
    env.accounts_file.parent.mkdir(parents=True, exist_ok=True)
    env.accounts_file.write_text("{ this is : not json")
    with pytest.raises(Exception):
        AccountStore(env)
    assert (env.accounts_file.parent / "accounts.corrupt.json").exists()


def test_missing_file_empty_store(env):
    store = AccountStore(env)
    assert store.file.accounts == []


def test_set_primary_resets_flag(env):
    store = AccountStore(env)
    a = store.upsert_profile("a", _snap("a@x", "A"))
    b = store.upsert_profile("b", _snap("b@x", "B"))
    store.set_auto_switched_from_primary(True)
    store.set_primary(b.id)
    assert store.file.accounts[0].id == b.id
    assert store.file.auto_switched_from_primary is False


def test_rotating_secret_clears_needs_reauth(env):
    """딱지는 특정 refresh 토큰에 대한 판정 — 토큰이 바뀌면 해제된다.

    해제가 없으면 데몬 선제 스윕(needs_reauth → skip)과 체커(활성 → NOT_FALLBACK)
    양쪽에서 제외돼 딱지가 영구 고착한다(= 매번 재로그인).
    """
    store = AccountStore(env)
    p = store.upsert_profile("a", _snap())
    store.set_needs_reauth(p.id, True)
    assert store.file.accounts[0].needs_reauth is True

    # 같은 blob 재저장 = 토큰 안 바뀜 → 딱지 유지
    store.set_secret(_snap(), p.id)
    assert store.file.accounts[0].needs_reauth is True

    # 새 blob = 토큰 회전/재로그인 → 딱지 해제
    store.set_secret(_snap(tag="B"), p.id)
    assert store.file.accounts[0].needs_reauth is False
    # 디스크에도 반영
    assert AccountStore(env).file.accounts[0].needs_reauth is False


def test_secret_rewrite_keeps_reauth_when_refresh_token_unchanged(env):
    """refresh 토큰이 그대로면 다른 바이트가 바뀌어도 딱지를 유지한다.

    `refresh_active_snapshot_if_stable` 이 활성 계정 스냅샷을 5분마다 무조건 되저장한다.
    죽은 계정도 mcpOAuth 등 Claude 인증과 무관한 필드가 바뀌면 blob 은 달라지므로,
    바이트 비교로 해제하면 죽은 계정의 딱지가 조용히 풀리고 엔진이 정상 후보로 취급한다.
    (upstream 이슈 #14 리뷰에서 지적된 회귀 — 실제로 이 포팅본에 있었다.)
    """
    store = AccountStore(env)
    same_rt = "rt-FIXED"
    p = store.upsert_profile("a", CredentialsSnapshot(
        credentials_blob=creds_blob("A", refresh_token=same_rt),
        oauth_account={"emailAddress": "a@example.com"}))
    store.set_needs_reauth(p.id, True)

    # 토큰은 동일, 나머지 바이트만 다른 스냅샷 재저장 (라이브싱크가 하는 일)
    store.set_secret(CredentialsSnapshot(
        credentials_blob=creds_blob("B", refresh_token=same_rt),
        oauth_account={"emailAddress": "a@example.com"}), p.id)

    assert store.file.accounts[0].needs_reauth is True
    assert AccountStore(env).file.accounts[0].needs_reauth is True


def test_reload_reflects_external_change(env):
    store = AccountStore(env)
    store.upsert_profile("a", _snap())
    # 외부 프로세스가 파일을 바꿈
    other = AccountStore(env)
    other.upsert_profile("b", _snap("b@x", "B"))
    store.reload()
    assert len(store.file.accounts) == 2
