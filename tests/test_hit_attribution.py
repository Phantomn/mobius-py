"""이슈 #19 회귀 — hit 의 **귀속 구간**으로 주인을 가른다 (실측 재현).

2026-08-15 사고 로그 6줄이 디스크에 남아 있어 그대로 재현한다. 결정적인 한 줄:

    03:32:51.538Z  세션 D  USER-PROMPT      ← 요청이 자격증명에 묶인 하한
    03:33:37.091Z  세션 C  weekly limit     → primary 에 기록 (여기서 전환 발생)
    03:33:54.486Z  세션 D  weekly limit     → **폴백에 오기록**

hit 의 타임스탬프(03:33:54)는 요청 시각이 아니라 **429 재시도가 끝난** 시각이다. 실측
재시도 창은 2분 6초로 **엔진 쿨다운(120초)보다 길다** — 시간 창으로 억누르는 처방이
구조적으로 못 막는 이유다.

판정 규칙: 요청이 살아 있던 구간 `[묶인 하한, 로그 기록 시각]` 안에서 전환이 일어날 수
있었다면 주인은 원리적으로 불확정 → 기록하지 않고 usage 검증으로 넘긴다. 구간 안에
전환이 없었다면 답은 하나뿐이므로 네트워크 0으로 기록한다.
"""

import datetime
import json

from mobius.daemon import Daemon
from mobius.models import CredentialsSnapshot
from tests.conftest import creds_blob, write_live
from tests.test_daemon_integration import FakeRefresher


def _epoch(iso: str) -> float:
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# 사고 당일 실측 타임스탬프
PRIME = _epoch("2026-08-15T03:30:00.000Z")
PROMPT_D = "2026-08-15T03:32:51.538Z"
SWITCH = _epoch("2026-08-15T03:33:40.000Z")   # C 의 hit 로 전환이 일어난 지점
HIT_D = "2026-08-15T03:33:54.486Z"


def _prompt_line(ts: str) -> str:
    return json.dumps({
        "type": "user", "promptSource": "typed", "promptId": "p1",
        "message": {"role": "user", "content": [{"type": "text", "text": "계속"}]},
        "timestamp": ts,
    }) + "\n"


def _hit_line(ts: str, text: str = "You've hit your weekly limit · resets 9pm (Asia/Seoul)") -> str:
    return json.dumps({
        "type": "assistant", "error": "rate_limit", "isApiErrorMessage": True,
        "apiErrorStatus": 429,
        "message": {"content": [{"type": "text", "text": text}]},
        "timestamp": ts,
    }) + "\n"


def _boom(_token):
    raise OSError("network down")


def _two_accounts(env, log_name="session-d.jsonl"):
    """primary(A, 라이브 활성) + fallback(B), 프라이밍 끝난 로그 파일."""
    write_live(env, tag="A", email="primary@example.com")
    d = Daemon(env)
    d.usage_transport = _boom      # usage 를 타면 즉시 드러난다 (귀속 경로는 네트워크 0)
    d.fallback_checker.refresher = FakeRefresher()   # 전환 직전 검증은 이 테스트의 관심사가 아니다
    primary = d.store.upsert_profile("primary", d.io.read_live_snapshot())
    fallback = d.store.upsert_profile("fallback", CredentialsSnapshot(
        credentials_blob=creds_blob("B"), oauth_account={"emailAddress": "fb@example.com"}))
    d.store.set_active(primary.id)
    proj = env.projects_dir / "p"
    proj.mkdir(parents=True, exist_ok=True)
    log = proj / log_name
    log.write_text("")
    d.tick(PRIME)                  # prime + 타임라인 첫 항목
    return d, primary, fallback, log


def test_hit_spanning_a_switch_is_never_charged_to_the_new_account(env):
    """#19 본체 — 전환 뒤 도착했지만 전환 전에 시작된 요청은 새 계정에 기록되지 않는다."""
    d, primary, fallback, log = _two_accounts(env)

    log.write_text(_prompt_line(PROMPT_D))
    d.tick(_epoch(PROMPT_D) + 1)                  # 워처가 턴 시작을 본다

    d.switcher.switch_to(fallback.id)             # 라이브까지 스왑
    d.tick(SWITCH)

    with log.open("a") as f:
        f.write(_hit_line(HIT_D))
    d.tick(_epoch(HIT_D) + 1)

    d.store.reload()
    by_id = {p.id: p for p in d.store.file.accounts}
    assert by_id[fallback.id].rate_limit is None, "멀쩡한 폴백이 소진으로 박히면 안 된다(#19)"
    assert by_id[primary.id].rate_limit is None, "구간이 전환을 품었으면 기록하지 않는다"
    assert d._pending_hit is not None, "불확정 hit 은 버리지 않고 usage 검증으로 넘긴다"


def test_hit_without_a_switch_in_its_window_is_recorded_with_no_network(env):
    """구간 안에 전환이 없으면 답은 하나 — 조회 없이 기록한다(평시 네트워크 0 유지)."""
    d, primary, _fallback, log = _two_accounts(env)

    log.write_text(_prompt_line("2026-08-15T03:31:00.000Z"))
    d.tick(_epoch("2026-08-15T03:31:01.000Z"))
    with log.open("a") as f:
        f.write(_hit_line("2026-08-15T03:33:06.000Z"))     # 실측 재시도 창 2분 6초
    d.tick(_epoch("2026-08-15T03:33:07.000Z"))

    d.store.reload()
    by_id = {p.id: p for p in d.store.file.accounts}
    assert by_id[primary.id].rate_limit is not None
    assert d._pending_hit is None, "조회 없이 끝나야 한다"


def test_subagent_hit_uses_the_parent_turn_as_its_binding_bound(env):
    """서브에이전트는 부모와 같은 프로세스 — 자기 파일의 프롬프트를 좌표로 쓰면 안 된다.

    실측(2026-08-22): 부모 턴 시작과 서브에이전트 프롬프트의 격차 p50 **21분**, 61건 중
    57건이 재시도 창을 넘는다. 자기 파일 기준이면 좌표가 전환 **뒤로** 밀려 #19 와 같은
    방향으로 오귀인된다.
    """
    d, primary, fallback, parent_log = _two_accounts(env, log_name="parent.jsonl")
    sub_dir = parent_log.parent / "parent" / "subagents"
    sub_dir.mkdir(parents=True)
    sub_log = sub_dir / "agent-x.jsonl"
    sub_log.write_text("")

    # 부모 턴은 전환 **전에** 시작됐다. 서브에이전트는 전환 **뒤에** 스폰됐다.
    parent_log.write_text(_prompt_line("2026-08-15T03:31:00.000Z"))
    d.tick(_epoch("2026-08-15T03:31:01.000Z"))

    d.switcher.switch_to(fallback.id)
    d.tick(SWITCH)

    sub_log.write_text(_prompt_line("2026-08-15T03:33:45.000Z"))
    d.tick(_epoch("2026-08-15T03:33:46.000Z"))
    with sub_log.open("a") as f:
        f.write(_hit_line("2026-08-15T03:33:50.000Z"))
    d.tick(_epoch("2026-08-15T03:33:51.000Z"))

    d.store.reload()
    by_id = {p.id: p for p in d.store.file.accounts}
    assert by_id[fallback.id].rate_limit is None, \
        "부모 턴이 전환 전에 시작됐으므로 새 계정에 기록하면 안 된다"
    assert d._pending_hit is not None


def test_subagent_hit_is_recorded_when_the_parent_turn_also_clears_the_switch(env):
    """부모 좌표를 **쓴다**는 것까지 검사한다 — "모르겠다"로 넘겨도 통과하면 안 된다.

    ★ 이 검사가 없으면 부모 조회를 없애도 초록이다(다른 이유로 초록: 위 테스트는 결과가
      '기록 안 함'이라 포기와 구분되지 않는다). 실제로 되돌림 검증에서 그 사각을 밟았다.
    """
    d, primary, _fallback, parent_log = _two_accounts(env, log_name="parent.jsonl")
    sub_dir = parent_log.parent / "parent" / "subagents"
    sub_dir.mkdir(parents=True)
    sub_log = sub_dir / "agent-x.jsonl"

    parent_log.write_text(_prompt_line("2026-08-15T03:31:00.000Z"))   # 전환 없음
    d.tick(_epoch("2026-08-15T03:31:01.000Z"))
    sub_log.write_text(_prompt_line("2026-08-15T03:31:20.000Z"))
    d.tick(_epoch("2026-08-15T03:31:21.000Z"))
    with sub_log.open("a") as f:
        f.write(_hit_line("2026-08-15T03:33:26.000Z"))
    d.tick(_epoch("2026-08-15T03:33:27.000Z"))

    d.store.reload()
    by_id = {p.id: p for p in d.store.file.accounts}
    assert by_id[primary.id].rate_limit is not None, "부모 좌표가 유효하면 조회 없이 기록된다"
    assert d._pending_hit is None


def test_hit_without_a_reset_time_is_not_given_a_fabricated_24h_block(env):
    """P5(시각 못 읽는 변형) — 귀속이 확실해도 24시간을 날조하지 않는다."""
    d, primary, _fallback, log = _two_accounts(env)

    log.write_text(_prompt_line("2026-08-15T03:31:00.000Z"))
    d.tick(_epoch("2026-08-15T03:31:01.000Z"))
    with log.open("a") as f:
        f.write(_hit_line("2026-08-15T03:31:30.000Z",
                          text="You've hit your weekly limit, it resets soon"))
    d.tick(_epoch("2026-08-15T03:31:31.000Z"))

    d.store.reload()
    assert all(p.rate_limit is None for p in d.store.file.accounts)
    assert d._pending_hit is not None, "실제 리셋 시각은 API 만 안다 — 검증으로 넘긴다"


def test_unrecoverable_binding_time_falls_back_to_usage_verification(env):
    """귀속 좌표를 못 얻으면(프라이밍으로 건너뛴 턴) 추측하지 않고 기존 검증 경로로 넘긴다."""
    d, _primary, _fallback, log = _two_accounts(env)

    log.write_text(_hit_line(HIT_D))              # 선행 턴 시작 라인 없음
    d.tick(_epoch(HIT_D) + 1)

    d.store.reload()
    assert all(p.rate_limit is None for p in d.store.file.accounts)
    assert d._pending_hit is not None
