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
