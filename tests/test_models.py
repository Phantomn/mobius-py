from mobius.models import AccountProfile, AccountsFile, RateLimitInfo


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
