"""모든 파일 경로의 단일 출처 (MobiusEnvironment.swift 포팅).

테스트·격리 실행은 MOBIUS_HOME 환경변수로 홈을 재지정한다. MOBIUS_HOME이 설정되면
앱 데이터(accounts.json, secrets/)도 그 홈 아래로 따라가 완전 격리된다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MobiusEnvironment:
    home: Path

    # --- Claude Code 자격증명/설정 (진실의 원천) ---
    @property
    def claude_dir(self) -> Path:
        return self.home / ".claude"

    @property
    def claude_json(self) -> Path:
        """~/.claude.json — oauthAccount(이메일/조직 메타)가 여기 있다."""
        return self.home / ".claude.json"

    @property
    def credentials_file(self) -> Path:
        """~/.claude/.credentials.json (0600) — Linux는 이 파일이 토큰의 진실의 원천."""
        return self.claude_dir / ".credentials.json"

    @property
    def projects_dir(self) -> Path:
        return self.claude_dir / "projects"

    # --- Mobius 앱 데이터 ---
    @property
    def app_data_dir(self) -> Path:
        return self._app_data_root / "mobius"

    @property
    def accounts_file(self) -> Path:
        return self.app_data_dir / "accounts.json"

    @property
    def secrets_dir(self) -> Path:
        """계정별 자격증명 스냅샷 보관소(0700). Claude Code의 .credentials.json(0600)과 동일 보안 수준."""
        return self.app_data_dir / "secrets"

    def secret_file(self, account_id: str) -> Path:
        return self.secrets_dir / f"{account_id}.json"

    # --- 내부 ---
    @property
    def _app_data_root(self) -> Path:
        # MOBIUS_HOME override 시 앱 데이터도 그 홈 아래로 (테스트 완전 격리).
        if os.environ.get("MOBIUS_HOME"):
            return self.home / ".local" / "share"
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            return Path(xdg)
        return self.home / ".local" / "share"

    @staticmethod
    def live() -> "MobiusEnvironment":
        override = os.environ.get("MOBIUS_HOME")
        home = Path(override) if override else Path.home()
        return MobiusEnvironment(home=home)
