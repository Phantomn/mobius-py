from mobius.autoswitch import AutoSwitchEngine, DecisionKind, SwitchReason
from mobius.models import AccountProfile, AccountsFile, RateLimitInfo
from mobius.ratelimit_parser import RateLimitHit

NOW = 1000.0


def make_file(active="a", auto=True, from_primary=False, accounts=None):
    if accounts is None:
        accounts = [
            AccountProfile(id="a", nickname="a", email_address="a@x"),
            AccountProfile(id="b", nickname="b", email_address="b@x"),
            AccountProfile(id="c", nickname="c", email_address="c@x"),
        ]
    return AccountsFile(accounts=accounts, active_account_id=active,
                        auto_switch_enabled=auto, auto_switched_from_primary=from_primary)


def test_hit_switches_to_next_available():
    eng = AutoSwitchEngine()
    d = eng.on_rate_limit_hit(make_file(), RateLimitHit(resets_at=NOW + 3600), NOW)
    assert d.kind == DecisionKind.SWITCH_TO and d.target_id == "b"
    assert d.reason == SwitchReason.ACTIVE_EXHAUSTED


def test_cooldown_blocks_reswitch():
    eng = AutoSwitchEngine(cooldown=120)
    eng.note_switched(NOW)
    d = eng.on_rate_limit_hit(make_file(), RateLimitHit(resets_at=NOW + 3600), NOW + 60)
    assert d.kind == DecisionKind.NONE
    d2 = eng.on_rate_limit_hit(make_file(), RateLimitHit(resets_at=NOW + 3600), NOW + 200)
    assert d2.kind == DecisionKind.SWITCH_TO


def test_disabled_notifies_only():
    eng = AutoSwitchEngine()
    d = eng.on_rate_limit_hit(make_file(auto=False), RateLimitHit(resets_at=NOW + 10), NOW)
    assert d.kind == DecisionKind.NOTIFY_EXHAUSTED_ONLY and d.target_id == "a"


def test_all_exhausted_when_others_limited():
    accts = [
        AccountProfile(id="a", nickname="a", email_address="a@x"),
        AccountProfile(id="b", nickname="b", email_address="b@x",
                       rate_limit=RateLimitInfo(NOW + 5000, NOW)),
        AccountProfile(id="c", nickname="c", email_address="c@x", needs_reauth=True),
    ]
    eng = AutoSwitchEngine()
    d = eng.on_rate_limit_hit(make_file(accounts=accts), RateLimitHit(resets_at=NOW + 10), NOW)
    assert d.kind == DecisionKind.ALL_EXHAUSTED


def test_model_scoped_pinned_stays():
    accts = [AccountProfile(id="a", nickname="a", email_address="a@x", user_pinned=True),
             AccountProfile(id="b", nickname="b", email_address="b@x")]
    eng = AutoSwitchEngine()
    d = eng.on_rate_limit_hit(make_file(accounts=accts),
                              RateLimitHit(resets_at=None, model_scoped=True), NOW)
    assert d.kind == DecisionKind.NONE


def test_ontick_primary_recovery():
    accts = [
        AccountProfile(id="a", nickname="a", email_address="a@x",
                       rate_limit=RateLimitInfo(NOW - 100, NOW - 200)),  # primary 리셋 지남
        AccountProfile(id="b", nickname="b", email_address="b@x"),
    ]
    f = make_file(active="b", from_primary=True, accounts=accts)
    eng = AutoSwitchEngine(margin=60)
    d = eng.on_tick(f, NOW)  # margin 지났으므로 복귀
    assert d.kind == DecisionKind.SWITCH_TO and d.target_id == "a"
    assert d.reason == SwitchReason.PRIMARY_RECOVERED


def test_ontick_no_recovery_when_manual():
    accts = [
        AccountProfile(id="a", nickname="a", email_address="a@x",
                       rate_limit=RateLimitInfo(NOW - 100, NOW - 200)),
        AccountProfile(id="b", nickname="b", email_address="b@x"),
    ]
    f = make_file(active="b", from_primary=False, accounts=accts)  # 수동 전환 상태
    assert AutoSwitchEngine().on_tick(f, NOW).kind == DecisionKind.NONE
