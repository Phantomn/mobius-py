from mobius import token_refresher
from mobius.credentials import RefreshedTokens
from mobius.fallback_check import FallbackAuthChecker, FallbackCheckResult as R
from mobius.models import CredentialsSnapshot
from mobius.store import AccountStore
from tests.conftest import creds_blob

NOW = 1_700_000_000.0


def _snap(email, tag="A", **kw):
    return CredentialsSnapshot(credentials_blob=creds_blob(tag, **kw),
                               oauth_account={"emailAddress": email})


class FakeRefresher:
    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc
        self.calls = 0

    def refresh(self, rt, scopes, now):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def _store_with(env, **snaps):
    store = AccountStore(env)
    ids = {}
    for email, snap in snaps.items():
        p = store.upsert_profile(email.split("@")[0], snap)
        ids[email] = p.id
    return store, ids


def test_not_fallback_for_active(env):
    store, ids = _store_with(env, **{"a@x": _snap("a@x")})
    chk = FallbackAuthChecker(store, FakeRefresher())
    assert chk.check(ids["a@x"], ids["a@x"], NOW) == R.NOT_FALLBACK


def test_no_refresh_token_marks_reauth(env):
    store, ids = _store_with(env, **{"a@x": _snap("a@x"),
                                     "b@x": _snap("b@x", tag="B", refresh_token="")})
    chk = FallbackAuthChecker(store, FakeRefresher())
    assert chk.check(ids["b@x"], ids["a@x"], NOW) == R.NO_REFRESH_TOKEN
    store.reload()
    assert next(a for a in store.file.accounts if a.id == ids["b@x"]).needs_reauth is True


def test_locally_dead(env):
    dead = _snap("b@x", tag="B", rte_ms=int((NOW - 100) * 1000))
    store, ids = _store_with(env, **{"a@x": _snap("a@x"), "b@x": dead})
    chk = FallbackAuthChecker(store, FakeRefresher())
    assert chk.check(ids["b@x"], ids["a@x"], NOW) == R.LOCALLY_DEAD


def test_refreshed_alive_stores_rotation(env):
    store, ids = _store_with(env, **{"a@x": _snap("a@x"), "b@x": _snap("b@x", tag="B")})
    tokens = RefreshedTokens(access_token="NEW", refresh_token="ROT", expires_at_ms=int(NOW * 1000),
                             refresh_token_expires_at_ms=int((NOW + 99999) * 1000), scopes=["s"])
    chk = FallbackAuthChecker(store, FakeRefresher(result=tokens))
    assert chk.check(ids["b@x"], ids["a@x"], NOW) == R.REFRESHED_ALIVE
    import json
    saved = json.loads(store.secret(ids["b@x"]).credentials_blob)
    assert saved["claudeAiOauth"]["accessToken"] == "NEW"
    assert saved["claudeAiOauth"]["refreshToken"] == "ROT"


def test_dead_on_invalid_grant(env):
    store, ids = _store_with(env, **{"a@x": _snap("a@x"), "b@x": _snap("b@x", tag="B")})
    chk = FallbackAuthChecker(store, FakeRefresher(exc=token_refresher.InvalidGrant()))
    assert chk.check(ids["b@x"], ids["a@x"], NOW) == R.DEAD


def test_transient_does_not_mark(env):
    store, ids = _store_with(env, **{"a@x": _snap("a@x"), "b@x": _snap("b@x", tag="B")})
    chk = FallbackAuthChecker(store, FakeRefresher(exc=token_refresher.Transient()))
    assert chk.check(ids["b@x"], ids["a@x"], NOW) == R.TRANSIENT
    store.reload()
    assert next(a for a in store.file.accounts if a.id == ids["b@x"]).needs_reauth is False


def test_allow_network_false_local_only(env):
    store, ids = _store_with(env, **{"a@x": _snap("a@x"), "b@x": _snap("b@x", tag="B")})
    fake = FakeRefresher()
    chk = FallbackAuthChecker(store, fake)
    assert chk.check(ids["b@x"], ids["a@x"], NOW, allow_network=False) == R.TRANSIENT
    assert fake.calls == 0  # 네트워크 호출 없음
