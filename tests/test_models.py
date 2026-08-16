from mobius.models import (AccountProfile, AccountsFile, RateLimitInfo,
                           ScopedUsageLimit, UsageSnapshot)


def _usage(five=None, five_r=None, week=None, week_r=None, scoped=()):
    return UsageSnapshot(five_hour_percent=five, five_hour_resets_at=five_r,
                         seven_day_percent=week, seven_day_resets_at=week_r,
                         fetched_at=0.0, scoped_limits=list(scoped))


def test_usage_exhaustion_none_when_under_limit():
    assert _usage(five=9.0, week=16.0).exhausted_account_window() is False
    assert _usage().exhausted_account_window() is False


def test_usage_scoped_limit_is_not_account_exhaustion():
    """★ 모델 전용 한도(Fable 주간)는 **계정 소진이 아니다.**

    이걸 소진으로 기록하면 `is_limited` 가 "계정을 못 쓴다"와 "그 모델만 못 쓴다"를
    구분 못 해, 계정은 멀쩡한 폴백이 후보에서 **며칠간** 빠진다 — 오귀인 사고(#19)와
    같은 실패를 자초한다. 모델 전용 자동 전환은 미지원(upstream PR #21 후속).
    """
    s = _usage(five=3.0, scoped=[ScopedUsageLimit(label="Fable", percent=100.0,
                                                  resets_at=77.0)])
    assert s.exhausted_account_window() is False
    assert s.account_reset_after(0.0) is None


def test_usage_latest_reset_wins():
    """★ 여러 창이 동시 소진이면 **늦은** 리셋을 쓴다(이른 쪽 아님).

    이른 쪽을 쓰면 레코드가 먼저 만료돼, 아직 7일 한도에 막힌 계정으로 엔진이 돌아갔다가
    즉시 다시 튕긴다.
    """
    s = _usage(five=100.0, five_r=500.0, week=100.0, week_r=200.0)
    assert s.account_reset_after(0.0) == 500.0


def test_usage_ignores_past_reset():
    """이미 지난 리셋 시각은 낡은 응답 — 기록하면 is_limited 가 즉시 False 라 무의미하다.

    소진 사실(True)과 시각 부재(None)를 **따로** 돌려줘, 호출측이 "여유"와 "판정 보류"를
    구분할 수 있게 한다.
    """
    s = _usage(week=100.0, week_r=200.0)
    assert s.exhausted_account_window() is True
    assert s.account_reset_after(300.0) is None
    assert _usage(week=100.0).account_reset_after(0.0) is None


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
