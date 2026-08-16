from mobius.autoswitch import (AutoSwitchEngine, CandidateProbeAction, DecisionKind,
                               SwitchReason)
from mobius.models import AccountProfile, AccountsFile, AdvisoryRecord, RateLimitInfo
from mobius.ratelimit_parser import HitKind, RateLimitHit

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


def test_monthly_spend_pinned_still_leaves():
    """★ 월간 지출 한도는 **모델 전용 한도가 아니다** — 핀이어도 떠난다.

    구 포팅은 P3 를 model_scoped=True 로 표시해, 계정 전체가 막혔는데도 핀 예외가 걸려
    그 계정에 머물렀다.
    """
    accts = [AccountProfile(id="a", nickname="a", email_address="a@x", user_pinned=True),
             AccountProfile(id="b", nickname="b", email_address="b@x")]
    eng = AutoSwitchEngine()
    d = eng.on_rate_limit_hit(make_file(accounts=accts),
                              RateLimitHit(resets_at=NOW + 500, kind=HitKind.MONTHLY_SPEND), NOW)
    assert d.kind == DecisionKind.SWITCH_TO and d.target_id == "b"


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


# ---------- 임계값 선제 전환 (advisory) ----------


def _advisory(resets_at=NOW + 5000, detected_at=NOW - 10, utilization=92.0):
    return AdvisoryRecord(utilization=utilization, resets_at=resets_at,
                          detected_at=detected_at)


def _advised_file(**kw):
    accts = [AccountProfile(id="a", nickname="a", email_address="a@x",
                            advisory=_advisory(), **kw),
             AccountProfile(id="b", nickname="b", email_address="b@x")]
    return make_file(accounts=accts)


def test_advisory_switches_to_verified_candidate():
    d = AutoSwitchEngine().check_advisory(_advised_file(), "a", "b", False, NOW)
    assert d.kind == DecisionKind.SWITCH_TO and d.target_id == "b"
    assert d.reason == SwitchReason.THRESHOLD_ADVISORY


def test_advisory_stays_silent_without_candidate():
    """검증된 후보가 없으면 조용히 머문다 — 알림도 전환도 없음."""
    d = AutoSwitchEngine().check_advisory(_advised_file(), "a", None, False, NOW)
    assert d.kind == DecisionKind.NONE


def test_advisory_notify_only_when_autoswitch_off():
    accts = [AccountProfile(id="a", nickname="a", email_address="a@x", advisory=_advisory()),
             AccountProfile(id="b", nickname="b", email_address="b@x")]
    f = make_file(auto=False, accounts=accts)
    d = AutoSwitchEngine().check_advisory(f, "a", None, False, NOW)
    assert d.kind == DecisionKind.NOTIFY_ADVISORY_ONLY and d.target_id == "a"
    # 같은 창을 이미 알렸으면 재알림 없음
    assert AutoSwitchEngine().check_advisory(f, "a", None, True, NOW).kind == DecisionKind.NONE


def test_advisory_notify_survives_cooldown():
    """자동 전환 꺼짐 알림은 **쿨다운보다 먼저** 평가돼야 한다.

    쿨다운을 먼저 두면 무관한 전환의 쿨다운 창이 이 알림을 영구히 삼킨다.
    """
    accts = [AccountProfile(id="a", nickname="a", email_address="a@x", advisory=_advisory()),
             AccountProfile(id="b", nickname="b", email_address="b@x")]
    eng = AutoSwitchEngine()
    eng.note_switched(NOW)  # 쿨다운 진입
    d = eng.check_advisory(make_file(auto=False, accounts=accts), "a", None, False, NOW)
    assert d.kind == DecisionKind.NOTIFY_ADVISORY_ONLY


def test_advisory_pin_after_warning_vetoes():
    """경고를 **보고 나서** 돌아온 핀만 거부권을 갖는다."""
    f = _advised_file(user_pinned=True, pinned_at=NOW)  # detected_at=NOW-10 보다 나중
    assert AutoSwitchEngine().check_advisory(f, "a", "b", False, NOW).kind == DecisionKind.NONE


def test_advisory_pin_before_warning_has_no_veto():
    """경고 이전의 핀(또는 시각 없는 구버전 핀)은 거부권 없음."""
    f = _advised_file(user_pinned=True, pinned_at=NOW - 100)
    assert AutoSwitchEngine().check_advisory(f, "a", "b", False, NOW).kind == DecisionKind.SWITCH_TO
    f2 = _advised_file(user_pinned=True, pinned_at=None)  # 구버전 파일
    assert AutoSwitchEngine().check_advisory(f2, "a", "b", False, NOW).kind == DecisionKind.SWITCH_TO


def test_ontick_recovery_blocked_by_advisory_alone():
    """advisory 만 보고 떠난 경우(rate_limit 없음)에도 복귀 게이트가 걸려야 한다.

    rate_limit 만 검사하면 가드가 통째로 스킵돼, 쿨다운이 풀리는 순간 primary 로 돌아갔다가
    아직 임계값 위인 primary 를 다시 떠나는 2분 주기 핑퐁이 창 리셋까지 계속된다.
    """
    accts = [AccountProfile(id="a", nickname="a", email_address="a@x",
                            advisory=_advisory(resets_at=NOW + 5000)),  # rate_limit 은 None
             AccountProfile(id="b", nickname="b", email_address="b@x")]
    f = make_file(active="b", from_primary=True, accounts=accts)
    assert AutoSwitchEngine().on_tick(f, NOW).kind == DecisionKind.NONE


def test_ontick_recovery_uses_latest_gate():
    """두 게이트 중 **늦은 쪽**을 지나야 복귀한다."""
    accts = [AccountProfile(id="a", nickname="a", email_address="a@x",
                            rate_limit=RateLimitInfo(NOW - 500, NOW - 600),   # 지남
                            advisory=_advisory(resets_at=NOW + 500)),          # 아직
             AccountProfile(id="b", nickname="b", email_address="b@x")]
    f = make_file(active="b", from_primary=True, accounts=accts)
    assert AutoSwitchEngine().on_tick(f, NOW).kind == DecisionKind.NONE
    # 둘 다 지나면 복귀
    accts[0].advisory = _advisory(resets_at=NOW - 200)
    assert AutoSwitchEngine().on_tick(f, NOW).kind == DecisionKind.SWITCH_TO


def test_probe_backoff():
    eng = AutoSwitchEngine(candidate_probe_backoff=900)
    assert eng.should_probe_candidates(None, NOW) is True
    assert eng.should_probe_candidates(NOW - 100, NOW) is False
    assert eng.should_probe_candidates(NOW - 901, NOW) is True


def test_candidate_probe_action():
    act = AutoSwitchEngine.candidate_probe_action
    # 만료 정보 없음/아직 유효 → 저장 토큰으로 조회(refresh 0회)
    assert act(None, NOW, None, 60) == CandidateProbeAction.USE_STORED_TOKEN
    assert act(NOW + 10, NOW, None, 60) == CandidateProbeAction.USE_STORED_TOKEN
    # 만료 + 쿨다운 안 → 판정하지 않고 넘어감
    assert act(NOW - 10, NOW, NOW - 30, 60) == CandidateProbeAction.SKIP_COOLDOWN
    # 만료 + 쿨다운 경과 → refresh 승격
    assert act(NOW - 10, NOW, NOW - 100, 60) == CandidateProbeAction.ESCALATE
    assert act(NOW - 10, NOW, None, 60) == CandidateProbeAction.ESCALATE
