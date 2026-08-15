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
# 실제 소진 — 로그 hit 를 확인해 주는 응답.
_USAGE_EXHAUSTED = json.dumps({"five_hour": {"utilization": 100.0,
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


def test_daemon_rejects_hit_when_usage_says_not_exhausted(env):
    """전환 뒤 도착한 **옛 계정의** 한도 에러가 새 활성 계정에 기록되면 안 된다.

    실측 2026-08-15: 동시 실행 세션 4개가 옛 자격증명을 메모리에 들고 있어, 전환 뒤에도
    2분 반 동안 옛 계정의 rate-limit 에러를 뱉었다. 로그 라인엔 계정이 안 적혀 있어
    활성 계정에 오귀인됐고, 7일 16% 쓴 멀쩡한 폴백에 100% 계정의 리셋 시각이 덮여
    자동 전환이 통째로 죽었다. hit 의 자체 타임스탬프는 전환 **뒤**라 못 거른다.
    """
    write_live(env, tag="A", email="a@example.com")
    daemon = Daemon(env)
    daemon.usage_transport = lambda token: (200, _USAGE_OK)  # 실제로는 12% — 소진 아님
    a = daemon.store.upsert_profile("a", daemon.io.read_live_snapshot())
    daemon.store.set_active(a.id)

    proj = env.projects_dir / "p"
    proj.mkdir(parents=True)
    log = proj / "s.jsonl"
    log.write_text("")
    daemon.tick(NOW)                      # prime

    log.write_text(_rate_limit_line())    # 옛 계정 것인 지연 hit 도착
    daemon.tick(NOW)

    daemon.store.reload()
    assert daemon.store.file.accounts[0].rate_limit is None


def test_daemon_uses_usage_reset_time_not_log(env):
    """리셋 시각은 로그 파싱값이 아니라 usage API 가 준 실제 값을 쓴다."""
    write_live(env, tag="A", email="a@example.com")
    daemon = Daemon(env)
    daemon.usage_transport = lambda token: (200, _USAGE_EXHAUSTED)
    a = daemon.store.upsert_profile("a", daemon.io.read_live_snapshot())
    daemon.store.set_active(a.id)

    proj = env.projects_dir / "p"
    proj.mkdir(parents=True)
    log = proj / "s.jsonl"
    log.write_text("")
    daemon.tick(NOW)
    log.write_text(_rate_limit_line())
    daemon.tick(NOW)

    daemon.store.reload()
    rl = daemon.store.file.accounts[0].rate_limit
    assert rl is not None
    # _USAGE_EXHAUSTED 의 resets_at = 2023-11-14T18:00:00Z. 로그의 "8am (Asia/Seoul)" 이 아니다.
    assert rl.resets_at == 1699984800.0


def test_daemon_skips_usage_when_already_limited(env):
    """이미 기록이 있으면 후속 hit 에 usage 를 다시 조회하지 않는다(버스트 비용 0)."""
    write_live(env, tag="A", email="a@example.com")
    daemon = Daemon(env)
    calls = []
    daemon.usage_transport = lambda t: calls.append(t) or (200, _USAGE_EXHAUSTED)
    a = daemon.store.upsert_profile("a", daemon.io.read_live_snapshot())
    daemon.store.set_active(a.id)
    daemon.store.update(a.id, lambda p: setattr(
        p, "rate_limit", RateLimitInfo(resets_at=NOW + 3600, recorded_at=NOW)))

    proj = env.projects_dir / "p"
    proj.mkdir(parents=True)
    log = proj / "s.jsonl"
    log.write_text("")
    daemon.tick(NOW)
    log.write_text(_rate_limit_line())
    daemon.tick(NOW)

    assert calls == []


# ---------- 임계값 선제 전환 ----------

def _usage_pct(pct, resets="2023-11-15T06:00:00Z"):   # NOW(=2023-11-14T22:13:20Z) 이후
    return json.dumps({"five_hour": {"utilization": pct, "resets_at": resets}}).encode()


def _advisory_daemon(env, threshold=90.0, auto=True):
    """A(활성 라이브) + B(폴백), 미리 전환 켜짐. 5분 싱크가 즉시 돌게 세팅."""
    write_live(env, tag="A", email="a@example.com")
    d = Daemon(env)
    d.fallback_checker.refresher = FakeRefresher()
    a = d.store.upsert_profile("a", d.io.read_live_snapshot())
    b = d.store.upsert_profile("b", CredentialsSnapshot(
        credentials_blob=creds_blob("B"), oauth_account={"emailAddress": "b@example.com"}))
    d.store.set_active(a.id)
    d.store.set_advisory_config(enabled=True, threshold=threshold)
    d.store.set_auto_switch(auto)
    (env.projects_dir / "p").mkdir(parents=True)
    return d, a, b


def test_advisory_set_and_preswitch(env):
    """임계값 도달 → advisory 세팅 → 여유 있는 폴백으로 **미리** 전환."""
    d, a, b = _advisory_daemon(env)
    # 활성 A는 92%, 후보 B는 10% (같은 transport 로 순차 응답)
    responses = [_usage_pct(92.0), _usage_pct(10.0)]
    d.usage_transport = lambda t: (200, responses.pop(0) if responses else _usage_pct(10.0))

    d.tick(NOW)

    d.store.reload()
    assert d.store.file.active_account_id == b.id
    assert d.store.file.auto_switched_from_primary is True


def test_advisory_never_leaks_into_is_limited(env):
    """advisory 는 소진이 아니다 — is_limited/rate_limit 에 절대 새면 안 된다."""
    d, a, b = _advisory_daemon(env, auto=False)   # 전환은 막고 advisory 만 세우게
    d.usage_transport = lambda t: (200, _usage_pct(92.0))

    d.tick(NOW)

    d.store.reload()
    p = next(x for x in d.store.file.accounts if x.id == a.id)
    assert p.advisory is not None and p.advisory.utilization == 92.0
    assert p.rate_limit is None          # 소진 기록은 만들지 않는다
    assert p.is_limited(NOW) is False    # 표시·엔진의 "쓸 수 없다" 신호는 그대로
    assert p.has_active_advisory(NOW) is True


def test_advisory_hysteresis_band_keeps_state(env):
    """밴드 내부(임계값-5 < util < 임계값)에서는 기존 상태를 유지한다."""
    d, a, b = _advisory_daemon(env, auto=False)
    d.usage_transport = lambda t: (200, _usage_pct(92.0))
    d.tick(NOW)
    d.store.reload()
    assert d.store.file.accounts[0].advisory is not None

    # 87% — 밴드 내부(85 < 87 < 90). 해제되면 안 된다.
    d._last_active_sync = float("-inf")
    d.usage_transport = lambda t: (200, _usage_pct(87.0))
    d.tick(NOW + 1)
    d.store.reload()
    assert d.store.file.accounts[0].advisory is not None

    # 84% — 밴드 아래(90-5=85 이하) → 해제
    d._last_active_sync = float("-inf")
    d.usage_transport = lambda t: (200, _usage_pct(84.0))
    d.tick(NOW + 2)
    d.store.reload()
    assert d.store.file.accounts[0].advisory is None


def test_advisory_off_by_default_no_network(env):
    """기본 꺼짐 — usage 를 조회하지 않는다."""
    write_live(env, tag="A", email="a@example.com")
    d = Daemon(env)
    calls = []
    d.usage_transport = lambda t: calls.append(t) or (200, _usage_pct(99.0))
    a = d.store.upsert_profile("a", d.io.read_live_snapshot())
    d.store.set_active(a.id)
    (env.projects_dir / "p").mkdir(parents=True)

    d.tick(NOW)

    assert d.store.file.advisory_enabled is False
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
    # 로그 hit 는 트리거일 뿐 — usage 가 실제 소진을 확인해야 기록·전환된다.
    daemon.usage_transport = lambda token: (200, _USAGE_EXHAUSTED)
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
