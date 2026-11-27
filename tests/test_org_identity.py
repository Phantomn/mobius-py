"""같은 이메일의 팀/개인 조직을 별도 계정으로 구분한다."""

import json

from mobius.claude_config import ClaudeConfigIO
from mobius.store import AccountStore
from mobius.switcher import Switcher
from tests.conftest import write_live


def _capture(env, store, io, name, **kw):
    write_live(env, **kw)
    return store.upsert_profile(name, io.read_live_snapshot())


def test_same_email_different_org_are_two_profiles(env):
    store, io = AccountStore(env), ClaudeConfigIO(env)
    team = _capture(env, store, io, "team", tag="T", email="me@x.com", org="Team", org_uuid="org-team")
    personal = _capture(env, store, io, "personal", tag="P", email="me@x.com", org="Mine",
                        org_uuid="org-personal")
    assert team.id != personal.id
    assert [a.nickname for a in store.file.accounts] == ["team", "personal"]
    assert store.secret(team.id).credentials_blob != store.secret(personal.id).credentials_blob
    # 같은 조직으로 다시 캡처하면 갱신(새 프로필 아님)
    again = _capture(env, store, io, "team2", tag="T2", email="me@x.com", org="Team", org_uuid="org-team")
    assert again.id == team.id and len(store.file.accounts) == 2


def test_reconcile_picks_matching_org(env):
    store, io = AccountStore(env), ClaudeConfigIO(env)
    sw = Switcher(store, io)
    team = _capture(env, store, io, "team", tag="T", email="me@x.com", org_uuid="org-team")
    personal = _capture(env, store, io, "personal", tag="P", email="me@x.com", org_uuid="org-personal")
    store.set_active(personal.id)
    write_live(env, tag="T3", email="me@x.com", org_uuid="org-team")  # 외부에서 팀으로 재로그인
    sw.reconcile()
    assert store.file.active_account_id == team.id
    assert b"at-T3" in store.secret(team.id).credentials_blob
    assert b"at-P" in store.secret(personal.id).credentials_blob


def test_adopt_second_org_gets_distinct_nickname(env):
    store, io = AccountStore(env), ClaudeConfigIO(env)
    sw = Switcher(store, io)
    write_live(env, tag="T", email="me@x.com", org_uuid="orgteam1234")
    first = sw.adopt_live_account_if_unregistered()
    write_live(env, tag="P", email="me@x.com", org_uuid="orgpers5678")
    second = sw.adopt_live_account_if_unregistered()
    assert first is not None and second is not None and first.id != second.id
    assert first.nickname == "me" and second.nickname == "me-orgpers5"


def test_legacy_profile_backfilled_from_secret(env):
    store, io = AccountStore(env), ClaudeConfigIO(env)
    old = _capture(env, store, io, "personal", tag="P", email="me@x.com", org_uuid="org-personal")
    # 구버전 accounts.json: organizationUUID 키 없음
    data = json.loads(env.accounts_file.read_bytes())
    for a in data["accounts"]:
        a.pop("organizationUUID")
    env.accounts_file.write_text(json.dumps(data))

    store = AccountStore(env)
    assert store.file.accounts[0].organization_uuid == "org-personal"
    team = _capture(env, store, io, "team", tag="T", email="me@x.com", org_uuid="org-team")
    assert team.id != old.id and len(store.file.accounts) == 2
