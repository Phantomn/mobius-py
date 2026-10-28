import json

from mobius.codex_config import CodexConfigIO
from mobius.daemon import Daemon
from mobius.models import CodexCredentialsSnapshot
from mobius.store import AccountStore


def _usage(percent, reset):
    return json.dumps({"plan_type": "pro", "rate_limit": {
        "primary_window": {"used_percent": percent, "reset_at": reset,
                            "window_minutes": 10080},
    }}).encode()


def test_daemon_switches_codex_account_when_active_window_exhausted(env):
    store = AccountStore(env)
    a = store.upsert_codex_profile("a", CodexCredentialsSnapshot(
        b'{"tokens":{"access_token":"at-a","account_id":"acct-a"}}', "acct-a", "pro"))
    b = store.upsert_codex_profile("b", CodexCredentialsSnapshot(
        b'{"tokens":{"access_token":"at-b","account_id":"acct-b"}}', "acct-b", "pro"))
    store.set_active(a.id)
    CodexConfigIO(env.home).write_auth(store.codex_secret(a.id).auth_blob)
    daemon = Daemon(env)
    daemon.codex_usage_transport = lambda token, account: (
        200, _usage(100 if account == "acct-a" else 10, "2099-01-01T00:00:00Z"))

    daemon.tick(1_700_000_000.0)

    assert daemon.store.file.active_account_id == b.id
    assert CodexConfigIO(env.home).read_auth().account_id == "acct-b"
    assert daemon.store.file.accounts[0].codex_rate_limit is not None
