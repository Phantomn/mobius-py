"""~/.claude/projects/**/*.jsonl 을 주기 스캔해 "새로 추가된" rate-limit 이벤트만 돌려준다.

SessionLogWatcher.swift 포팅. 네트워크 요청 없음. 첫 스캔에서는 기존 내용을 파싱하지 않고
오프셋만 기록한다(과거 이벤트로 오탐 방지). 쓰기 도중인 부분 라인은 다음 스캔까지 대기.
"""

from __future__ import annotations

import os
from typing import Optional

from .env import MobiusEnvironment
from . import ratelimit_parser
from .ratelimit_parser import RateLimitHit

_CHUNK = 64 * 1024


class SessionLogWatcher:
    def __init__(self, env: MobiusEnvironment, recent_window: float = 600):
        self.env = env
        self.recent_window = recent_window
        self._offsets: dict[str, int] = {}
        # 파일별 "마지막으로 본 턴 시작 시각" — hit 의 귀속 좌표(ratelimit_parser.turn_start_at).
        # 스캔은 append 만 읽으므로 이 값을 스캔 사이에 이어 들어야 한다. 프라이밍으로 건너뛴
        # 구간의 턴은 복원되지 않는다(=None) — 그건 데몬이 usage 검증으로 넘긴다.
        self._turn_start: dict[str, float] = {}
        self._primed = False

    def scan(self, now: float) -> list[RateLimitHit]:
        hits: list[RateLimitHit] = []
        projects = self.env.projects_dir
        if not projects.is_dir():
            return hits
        for root, _dirs, files in os.walk(projects):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(root, name)
                self._scan_file(path, now, hits)
        self._primed = True
        return hits

    def _scan_file(self, path: str, now: float, hits: list[RateLimitHit]) -> None:
        try:
            st = os.stat(path)
        except OSError:
            return
        seen = path in self._offsets
        # 최근 수정됐거나 아직 본 적 없는 파일만 본다.
        if not (st.st_mtime > now - self.recent_window or not seen):
            return
        try:
            f = open(path, "rb")
        except OSError:
            return
        with f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            start = self._offsets.get(path, 0)
            if start > end:  # 파일이 잘렸으면(로테이션) 기준점만 리셋
                self._offsets[path] = end
                return
            if start >= end:  # 새 내용 없음
                return
            if not self._primed:
                # 첫 스캔: 파싱 없이 오프셋만 — 마지막 개행까지만 전진(부분 라인 남김)
                off = self._offset_after_last_newline(f, start, end)
                self._offsets[path] = off if off is not None else start
                return
            f.seek(start)
            data = f.read(end - start)
            last_nl = data.rfind(b"\n")
            if last_nl < 0:  # 완성 라인 없음(부분 라인 대기)
                return
            complete_len = last_nl + 1
            self._offsets[path] = start + complete_len
            try:
                text = data[:complete_len].decode("utf-8")
            except UnicodeDecodeError:
                return
            for line in text.split("\n"):
                if not line:
                    continue
                hit, turn_start = ratelimit_parser.scan_line(line, now)
                if hit is not None:
                    hit.bound_at = self._binding_lower_bound(path)
                    hits.append(hit)
                if turn_start is not None:
                    self._turn_start[path] = turn_start

    def _binding_lower_bound(self, path: str) -> Optional[float]:
        """이 파일의 hit 이 자격증명에 묶였을 수 있는 **가장 이른** 시각.

        ★ 서브에이전트(`.../<sessionId>/subagents/**.jsonl`)는 부모와 **같은 CLI 프로세스**라
          자격증명이 부모 턴에 묶인다. 자기 파일의 프롬프트 라인을 쓰면 좌표가 늦게 잡히는데
          (실측 2026-08-22: 부모 턴 시작과 p50 **21분**, 61건 중 57건이 재시도 창 초과),
          **늦은 좌표 = 새 계정 = 이슈 #19 와 같은 방향의 오귀인**이다. 그래서 부모 세션
          파일의 턴 시작까지 넓혀 둘 중 이른 쪽을 쓴다.

        부모 쪽을 모르면(아직 스캔 전 등) None — 추측하지 않고 usage 검증으로 넘긴다.
        """
        own = self._turn_start.get(path)
        parent = self._parent_session_path(path)
        if parent is None:
            return own
        theirs = self._turn_start.get(parent)
        if theirs is None or own is None:
            return None          # 구간의 한쪽을 모르면 구간이 성립하지 않는다
        return min(own, theirs)

    @staticmethod
    def _parent_session_path(path: str) -> Optional[str]:
        """서브에이전트 로그면 부모 세션 파일 경로, 아니면 None.

        `<proj>/<sessionId>/subagents/[...]/agent-*.jsonl` → `<proj>/<sessionId>.jsonl`.
        중첩(workflows/)도 **첫** subagents 를 기준으로 잘라 같은 부모를 가리킨다.
        실측: 서브에이전트 hit 61건 전부 이 규칙으로 부모 파일이 실존했다.
        """
        parts = path.split(os.sep)
        if "subagents" not in parts:
            return None
        head = parts[:parts.index("subagents")]
        if not head:
            return None
        return os.sep.join(head) + ".jsonl"

    @staticmethod
    def _offset_after_last_newline(f, start: int, end: int) -> Optional[int]:
        """[start, end) 구간에서 마지막 개행 다음 오프셋. 큰 파일 대비 뒤에서부터 청크 역방향."""
        high = end
        while high > start:
            low = high - _CHUNK if high > start + _CHUNK else start
            f.seek(low)
            chunk = f.read(high - low)
            idx = chunk.rfind(b"\n")
            if idx >= 0:
                return low + idx + 1
            high = low
        return None
