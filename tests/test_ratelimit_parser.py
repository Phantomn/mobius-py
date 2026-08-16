import json

from mobius import ratelimit_parser as rp


def line(text, error="rate_limit", ts=None, **extra):
    obj = {"message": {"content": [{"type": "text", "text": text}]}}
    if error is not None:
        obj["error"] = error
    if ts is not None:
        obj["timestamp"] = ts
    obj.update(extra)
    return json.dumps(obj)


NOW = 1_700_000_000.0  # 2023-11-14 대략


def test_exclusion_not_your_usage_limit():
    assert rp.parse(line("this is not your usage limit, resets 8am (Asia/Seoul)"), NOW) is None


def test_non_candidate_returns_none():
    # error 필드 없고 429 아니면 후보 아님 (P4 제외)
    assert rp.parse(line("hit your usage limit resets 8am (Asia/Seoul)", error=None), NOW) is None


def test_p3_monthly_spend_is_its_own_kind_not_model_scoped():
    """★ 월간 지출 한도는 **모델 전용 한도가 아니다.**

    구 포팅은 model_scoped=True 로 표시했는데, 그러면 엔진의 "모델 전용 + 사용자 핀이면
    머문다" 예외가 걸려 **계정 전체가 막혔는데도 그 계정에 머문다.** 검증 대상도 다르다 —
    창이 아니라 usage 의 spend 블록으로 판정해야 한다.
    """
    hit = rp.parse(line("You've hit your monthly spend limit."), NOW)
    assert hit is not None and hit.resets_at is None
    assert hit.kind is rp.HitKind.MONTHLY_SPEND
    assert hit.model_scoped is False
    assert hit.effective_resets_at(NOW) == NOW + 24 * 3600


def test_window_hit_default_kind():
    hit = rp.parse(line("You've hit your usage limit. resets 8am (Asia/Seoul)"), NOW)
    assert hit is not None and hit.kind is rp.HitKind.WINDOW


def test_p1_time_only_rolls_to_future():
    hit = rp.parse(line("resets 8am (Asia/Seoul)", ts="2023-11-14T12:00:00.000Z"), NOW)
    assert hit is not None and hit.resets_at is not None
    # 8am KST 는 12:00Z(=21:00 KST) 이후이므로 익일로 굴러 미래여야 한다
    ref = 1_700_000_000.0
    import datetime
    assert hit.resets_at > datetime.datetime.fromisoformat("2023-11-14T12:00:00+00:00").timestamp()


def test_p2_date_and_time():
    hit = rp.parse(line("resets Jul 13 at 8am (Asia/Seoul)", ts="2023-06-01T00:00:00Z"), NOW)
    assert hit is not None and hit.resets_at is not None


def test_p4_legacy_epoch():
    future = int(NOW + 3600)
    obj = {"text": f"usage limit reached|{future}"}  # 후보 아님이지만 P4는 예외 인정
    hit = rp.parse(json.dumps(obj), NOW)
    assert hit is not None and abs(hit.resets_at - future) < 1


def test_p4_epoch_ms():
    future_ms = int((NOW + 3600) * 1000)
    hit = rp.parse(json.dumps({"text": f"usage limit reached|{future_ms}"}), NOW)
    assert hit is not None and abs(hit.resets_at - (NOW + 3600)) < 1


def test_candidate_via_api_error_status():
    obj = {"isApiErrorMessage": True, "apiErrorStatus": 429,
           "message": {"content": [{"text": "resets 9pm (America/New_York)"}]}}
    assert rp.parse(json.dumps(obj), NOW) is not None


def test_garbage_line():
    assert rp.parse("not json", NOW) is None
    assert rp.parse(line("", ), NOW) is None
