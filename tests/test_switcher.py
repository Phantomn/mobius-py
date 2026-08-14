import json

import pytest

from mobius.claude_config import ClaudeConfigIO
from mobius.models import CredentialsSnapshot
from mobius.store import AccountStore
from mobius.switcher import Switcher
from tests.conftest import creds_blob, write_live


def _setup_two(env):
    """A(활성 라이브) + B(폴백) 등록. 반환: (store, io, sw, id_a, id_b)."""
    write_live(env, tag="A", email="a@example.com")
    store = AccountStore(env)
    io = ClaudeConfigIO(env)
    sw = Switcher(store, io)
    a = store.upsert_profile("a", io.read_live_snapshot())
    # B 스냅샷 직접 등록
    b_snap = CredentialsSnapshot(credentials_blob=creds_blob("B"),
                                 oauth_account={"emailAddress": "b@example.com"})
    b = store.upsert_profile("b", b_snap)
    store.set_active(a.id)
    return store, io, sw, a.id, b.id


def test_switch_swaps_live(env):
    store, io, sw, id_a, id_b = _setup_two(env)
    sw.switch_to(id_b)
    live = json.loads(env.credentials_file.read_bytes())
    assert live["claudeAiOauth"]["accessToken"] == "at-B"
    assert live["mcpOAuth"] == {"x": "B"}  # 통째 스왑
    assert store.file.active_account_id == id_b


def test_switch_rollback_on_write_failure(env, monkeypatch):
    store, io, sw, id_a, id_b = _setup_two(env)
    original = io.write_live_snapshot
    calls = {"n": 0}

    def failing(snap):
        calls["n"] += 1
        if calls["n"] == 1:  # 대상 기록 시도에서 실패
            raise OSError("disk full")
        return original(snap)  # 롤백(before) 쓰기는 성공

    monkeypatch.setattr(io, "write_live_snapshot", failing)
    with pytest.raises(OSError):
        sw.switch_to(id_b)
    # 롤백으로 라이브는 A 유지
    live = json.loads(env.credentials_file.read_bytes())
    assert live["claudeAiOauth"]["accessToken"] == "at-A"


def test_reconcile_noop_when_unchanged(env, monkeypatch):
    store, io, sw, id_a, id_b = _setup_two(env)
    # 활성=A, 라이브=A, 시크릿 존재 → read_stable 호출조차 안 해야 함
    called = {"stable": False}
    monkeypatch.setattr(io, "read_stable_live_snapshot",
                        lambda *a, **k: called.__setitem__("stable", True) or None)
    sw.reconcile()
    assert called["stable"] is False


def test_reconcile_adopts_external_login(env):
    store, io, sw, id_a, id_b = _setup_two(env)
    # 외부에서 B로 로그인 (라이브를 B로)
    write_live(env, tag="B", email="b@example.com")
    sw.reconcile()
    assert store.file.active_account_id == id_b
    assert store.file.auto_switched_from_primary is False


def test_reconcile_clears_needs_reauth_on_live_account(env):
    """라이브가 그 계정이면 claude 가 그 자격증명으로 동작 중 = 딱지 전제가 거짓."""
    store, io, sw, id_a, id_b = _setup_two(env)
    store.set_needs_reauth(id_a, True)
    # 라이브를 A 의 새 토큰으로 갱신(외부 재로그인)
    write_live(env, tag="A2", email="a@example.com")
    sw.reconcile()
    assert next(a for a in store.file.accounts if a.id == id_a).needs_reauth is False


def test_adopt_unregistered(env):
    write_live(env, tag="A", email="solo@example.com")
    store = AccountStore(env)
    io = ClaudeConfigIO(env)
    sw = Switcher(store, io)
    p = sw.adopt_live_account_if_unregistered()
    assert p is not None and p.nickname == "solo"
    assert store.file.active_account_id == p.id
