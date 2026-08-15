from mobius.models import (AccountProfile, AccountsFile, RateLimitInfo,
                           ScopedUsageLimit, UsageSnapshot)


def _usage(five=None, five_r=None, week=None, week_r=None, scoped=()):
    return UsageSnapshot(five_hour_percent=five, five_hour_resets_at=five_r,
                         seven_day_percent=week, seven_day_resets_at=week_r,
                         fetched_at=0.0, scoped_limits=list(scoped))


def test_usage_exhaustion_none_when_under_limit():
    assert _usage(five=9.0, week=16.0).exhaustion() is None
    assert _usage().exhaustion() is None


def test_usage_exhaustion_account_window_wins_over_scoped():
    """계정 창 소진은 model_scoped=False — 모델 전용과 자동 전환 판단이 다르다."""
    s = _usage(week=100.0, week_r=300.0,
               scoped=[ScopedUsageLimit(label="Fable", percent=100.0, resets_at=100.0)])
    assert s.exhaustion() == (300.0, False)


def test_usage_exhaustion_earliest_reset():
    """여러 창이 동시 소진이면 가장 이른 리셋을 쓴다."""
    s = _usage(five=100.0, five_r=500.0, week=100.0, week_r=200.0)
    assert s.exhaustion() == (200.0, False)


def test_usage_exhaustion_scoped_only():
    s = _usage(five=3.0, scoped=[ScopedUsageLimit(label="Fable", percent=100.0,
                                                  resets_at=77.0)])
    assert s.exhaustion() == (77.0, True)


def test_usage_exhaustion_without_reset_time():
    """소진은 맞는데 API 가 시각을 안 주면 None — 호출측이 로그/24h 폴백을 쓴다."""
    assert _usage(week=100.0).exhaustion() == (None, False)


def test_lenient_decode_missing_keys():
    # 실패기록 13: 없는 키가 있어도 기본값으로 디코드돼야 한다.
    p = AccountProfile.from_dict({"id": "1", "nickname": "n", "emailAddress": "e@x"})
    assert p.organization_name == "" and p.tier_description == ""
    assert p.needs_reauth is False and p.rate_limit is None
    assert p.has_desktop_snapshot is False and p.user_pinned is False

    f = AccountsFile.from_dict({})
    assert f.accounts == [] and f.auto_switch_enabled is True
    assert f.auto_switched_from_primary is False


def test_ratelimit_lenient_model_scoped_default():
    rl = RateLimitInfo.from_dict({"resetsAt": 100.0, "recordedAt": 50.0})
    assert rl.model_scoped is False


def test_roundtrip():
    p = AccountProfile(id="1", nickname="n", email_address="e@x",
                       rate_limit=RateLimitInfo(200.0, 100.0, model_scoped=True), user_pinned=True)
    p2 = AccountProfile.from_dict(p.to_dict())
    assert p2 == p


def test_is_limited_and_may_leave():
    now = 1000.0
    p = AccountProfile(id="1", nickname="n", email_address="e@x",
                       rate_limit=RateLimitInfo(now + 500, now, model_scoped=True), user_pinned=True)
    assert p.is_limited(now) is True
    # 모델 전용 한도 + 핀 → 밀어내지 않는다
    assert p.auto_switch_may_leave(now) is False
    p.user_pinned = False
    assert p.auto_switch_may_leave(now) is True
    # 리셋 지난 뒤엔 제한 아님
    assert p.is_limited(now + 1000) is False


def test_primary_and_active():
    a = AccountProfile(id="1", nickname="a", email_address="a@x")
    b = AccountProfile(id="2", nickname="b", email_address="b@x")
    f = AccountsFile(accounts=[a, b], active_account_id="2")
    assert f.primary is a and f.active is b
