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
                hit = ratelimit_parser.parse(line, now)
                if hit is not None:
                    hits.append(hit)

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
