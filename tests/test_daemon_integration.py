import json

from mobius.credentials import RefreshedTokens
from mobius.daemon import Daemon
from mobius.models import CredentialsSnapshot, RateLimitInfo
from tests.conftest import creds_blob, write_live

NOW = 1_700_000_000.0


class FakeRefresher:
    def refresh(self, rt, scopes, now):
        return RefreshedTokens(access_token="rotated", refresh_token="rot2",
                               expires_at_ms=int(now * 1000) + 3600000,
                               refresh_token_expires_at_ms=int(now * 1000) + 9_000_000_000,
                               scopes=["s"])


def _rate_limit_line():
    return json.dumps({"error": "rate_limit",
                       "message": {"content": [{"text": "You've hit your usage limit. resets 8am (Asia/Seoul)"}]},
                       "timestamp": "2023-11-14T12:00:00.000Z"}) + "\n"


_USAGE_OK = json.dumps({"five_hour": {"utilization": 12.0,
                                      "resets_at": "2023-11-14T18:00:00Z"}}).encode()


def test_daemon_clears_needs_reauth_when_usage_succeeds(env):
    """upstream AppState.swift:377-381 — usage 200 = 토큰 살아있음 → 딱지 자가 해제.

    포팅 누락으로 usage.py 가 호출되지 않아 딱지가 단조 상태였다(수동 capture 전까지 고착).
    """
    write_live(env, tag="A", email="a@example.com")
    daemon = Daemon(env)
    daemon.usage_transport = lambda token: (200, _USAGE_OK)
    a = daemon.store.upsert_profile("a", daemon.io.read_live_snapshot())
    daemon.store.set_active(a.id)
    daemon.store.set_needs_reauth(a.id, True)

    daemon.tick(NOW)

    daemon.store.reload()
    assert daemon.store.file.accounts[0].needs_reauth is False


def test_daemon_keeps_needs_reauth_when_usage_401(env):
    """401 이면 여전히 죽음 — 해제하지 않는다."""
    write_live(env, tag="A", email="a@example.com")
    daemon = Daemon(env)
    daemon.usage_transport = lambda token: (401, b"")
    a = daemon.store.upsert_profile("a", daemon.io.read_live_snapshot())
    daemon.store.set_active(a.id)
    daemon.store.set_needs_reauth(a.id, True)

    daemon.tick(NOW)

    daemon.store.reload()
    assert daemon.store.file.accounts[0].needs_reauth is True


def test_daemon_skips_usage_for_healthy_accounts(env):
    """딱지 없는 계정엔 usage 조회를 하지 않는다 — 평시 네트워크 0."""
    write_live(env, tag="A", email="a@example.com")
    daemon = Daemon(env)
    calls = []
    daemon.usage_transport = lambda token: calls.append(token) or (200, _USAGE_OK)
    a = daemon.store.upsert_profile("a", daemon.io.read_live_snapshot())
    daemon.store.set_active(a.id)

    daemon.tick(NOW)

    assert calls == []


def test_daemon_clears_expired_rate_limit(env):
    """리셋 지난 rateLimit 레코드는 지워진다 — 남으면 지난 한도를 계속 참조한다."""
    write_live(env, tag="A", email="a@example.com")
    daemon = Daemon(env)
    a = daemon.store.upsert_profile("a", daemon.io.read_live_snapshot())
    daemon.store.set_active(a.id)
    daemon.store.update(a.id, lambda p: setattr(
        p, "rate_limit", RateLimitInfo(resets_at=NOW - 1, recorded_at=NOW - 100)))

    daemon.tick(NOW)

    daemon.store.reload()
    assert daemon.store.file.accounts[0].rate_limit is None


def test_daemon_keeps_active_rate_limit(env):
    """아직 리셋 전인 레코드는 유지된다."""
    write_live(env, tag="A", email="a@example.com")
    daemon = Daemon(env)
    a = daemon.store.upsert_profile("a", daemon.io.read_live_snapshot())
    daemon.store.set_active(a.id)
    daemon.store.update(a.id, lambda p: setattr(
        p, "rate_limit", RateLimitInfo(resets_at=NOW + 3600, recorded_at=NOW)))

    daemon.tick(NOW)

    daemon.store.reload()
    assert daemon.store.file.accounts[0].rate_limit is not None


def test_daemon_auto_switches_on_rate_limit(env):
    # A(활성 라이브) + B(폴백) 등록
    write_live(env, tag="A", email="a@example.com")
    daemon = Daemon(env)
    daemon.fallback_checker.refresher = FakeRefresher()  # 네트워크 차단
    a = daemon.store.upsert_profile("a", daemon.io.read_live_snapshot())
    b_snap = CredentialsSnapshot(credentials_blob=creds_blob("B"),
                                 oauth_account={"emailAddress": "b@example.com"})
    b = daemon.store.upsert_profile("b", b_snap)
    daemon.store.set_active(a.id)

    proj = env.projects_dir / "p"
    proj.mkdir(parents=True)
    log = proj / "s.jsonl"
    log.write_text("")

    daemon.tick(NOW)  # 1틱: watcher prime + reconcile
    assert daemon.store.file.active_account_id == a.id

    log.write_text(_rate_limit_line())  # 활성(A) 한도 소진 이벤트 발생
    daemon.tick(NOW)  # 2틱: hit 감지 → B로 자동 전환

    daemon.store.reload()
    assert daemon.store.file.active_account_id == b.id
    assert daemon.store.file.auto_switched_from_primary is True
    # 라이브 자격증명이 B(회전됨)로 스왑됐는지
    live = json.loads(env.credentials_file.read_bytes())
    assert live["claudeAiOauth"]["accessToken"] == "rotated"
