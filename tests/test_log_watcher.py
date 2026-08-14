import json

from mobius.log_watcher import SessionLogWatcher

NOW = 1_700_000_000.0


def _rl_line(text="resets 8am (Asia/Seoul)"):
    return json.dumps({"error": "rate_limit",
                       "message": {"content": [{"text": text}]}}) + "\n"


def test_prime_skips_existing_then_reads_new(env):
    proj = env.projects_dir / "p1"
    proj.mkdir(parents=True)
    log = proj / "session.jsonl"
    log.write_text(_rl_line("resets 7am (Asia/Seoul)"))  # 기존 내용 (과거)

    w = SessionLogWatcher(env)
    # 첫 스캔: prime — 기존 이벤트를 파싱하지 않는다
    assert w.scan(NOW) == []

    # 새 이벤트 추가
    with open(log, "a") as f:
        f.write(_rl_line("resets 9am (Asia/Seoul)"))
    hits = w.scan(NOW)
    assert len(hits) == 1


def test_partial_line_waits(env):
    proj = env.projects_dir / "p2"
    proj.mkdir(parents=True)
    log = proj / "s.jsonl"
    log.write_text("")
    w = SessionLogWatcher(env)
    w.scan(NOW)  # prime

    # 개행 없는 부분 라인 → 다음 스캔까지 대기
    with open(log, "a") as f:
        f.write('{"error":"rate_limit","message":{"content":[{"text":"resets 8am (Asia/Seoul)"')
    assert w.scan(NOW) == []
    # 라인 완성
    with open(log, "a") as f:
        f.write('}]}}\n')
    assert len(w.scan(NOW)) == 1


def test_no_projects_dir(env):
    w = SessionLogWatcher(env)
    assert w.scan(NOW) == []
