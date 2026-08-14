"""데이터 모델 (Models.swift 포팅).

★ 관대 디코드(실패기록 13): 저장 구조에 필드를 추가해도 구버전 파일이 깨지지 않도록,
from_dict 은 없는 키를 기본값으로 채운다. 날짜는 epoch 초(float)로 통일 저장한다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class RateLimitInfo:
    resets_at: float           # epoch 초
    recorded_at: float         # epoch 초
    # 모델 전용 한도(예: Fable 주간)인가 — 계정 자체(5시간/주간)는 여유가 있을 수 있다.
    model_scoped: bool = False

    def to_dict(self) -> dict:
        return {
            "resetsAt": self.resets_at,
            "recordedAt": self.recorded_at,
            "modelScoped": self.model_scoped,
        }

    @staticmethod
    def from_dict(d: dict) -> "RateLimitInfo":
        return RateLimitInfo(
            resets_at=float(d["resetsAt"]),
            recorded_at=float(d["recordedAt"]),
            model_scoped=bool(d.get("modelScoped", False)),
        )


@dataclass
class AccountProfile:
    id: str
    nickname: str
    email_address: str
    organization_name: str = ""
    tier_description: str = ""
    needs_reauth: bool = False
    rate_limit: Optional[RateLimitInfo] = None
    has_desktop_snapshot: bool = False
    # 사용자가 이 계정을 직접(수동) 골랐는가. true면 모델 전용 한도(Fable 등)로는 밀어내지 않는다.
    user_pinned: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nickname": self.nickname,
            "emailAddress": self.email_address,
            "organizationName": self.organization_name,
            "tierDescription": self.tier_description,
            "needsReauth": self.needs_reauth,
            "rateLimit": self.rate_limit.to_dict() if self.rate_limit else None,
            "hasDesktopSnapshot": self.has_desktop_snapshot,
            "userPinned": self.user_pinned,
        }

    @staticmethod
    def from_dict(d: dict) -> "AccountProfile":
        rl = d.get("rateLimit")
        return AccountProfile(
            id=str(d["id"]),
            nickname=str(d["nickname"]),
            email_address=str(d["emailAddress"]),
            organization_name=str(d.get("organizationName", "")),
            tier_description=str(d.get("tierDescription", "")),
            needs_reauth=bool(d.get("needsReauth", False)),
            rate_limit=RateLimitInfo.from_dict(rl) if rl else None,
            has_desktop_snapshot=bool(d.get("hasDesktopSnapshot", False)),
            user_pinned=bool(d.get("userPinned", False)),
        )

    def is_limited(self, now: float) -> bool:
        """지금 한도에 걸려 있는가 (리셋 시각 전인가)."""
        if self.rate_limit is None:
            return False
        return now < self.rate_limit.resets_at

    def auto_switch_may_leave(self, now: float) -> bool:
        """자동 전환이 이 계정을 밀어내도 되는가. 모델 전용 한도 + 사용자 핀이면 밀어내지 않는다."""
        if not (self.is_limited(now) or self.needs_reauth):
            return False
        rl = self.rate_limit
        if rl is not None and rl.model_scoped and self.user_pinned:
            return False
        return True


@dataclass
class AccountsFile:
    """accounts.json 전체. accounts[0] = primary(고정), 1... = fallback 우선순위."""

    accounts: list[AccountProfile] = field(default_factory=list)
    active_account_id: Optional[str] = None
    auto_switch_enabled: bool = True
    desktop_sync_enabled: bool = True
    desktop_auto_switch_enabled: bool = False
    # 현재 fallback 활성이 "자동 전환"의 결과인가. onTick의 primary 자동 복귀는 이게 true일 때만.
    auto_switched_from_primary: bool = False

    def to_dict(self) -> dict:
        return {
            "accounts": [a.to_dict() for a in self.accounts],
            "activeAccountID": self.active_account_id,
            "autoSwitchEnabled": self.auto_switch_enabled,
            "desktopSyncEnabled": self.desktop_sync_enabled,
            "desktopAutoSwitchEnabled": self.desktop_auto_switch_enabled,
            "autoSwitchedFromPrimary": self.auto_switched_from_primary,
        }

    @staticmethod
    def from_dict(d: dict) -> "AccountsFile":
        return AccountsFile(
            accounts=[AccountProfile.from_dict(a) for a in d.get("accounts", [])],
            active_account_id=d.get("activeAccountID"),
            auto_switch_enabled=bool(d.get("autoSwitchEnabled", True)),
            desktop_sync_enabled=bool(d.get("desktopSyncEnabled", True)),
            desktop_auto_switch_enabled=bool(d.get("desktopAutoSwitchEnabled", False)),
            auto_switched_from_primary=bool(d.get("autoSwitchedFromPrimary", False)),
        )

    @property
    def primary(self) -> Optional[AccountProfile]:
        return self.accounts[0] if self.accounts else None

    @property
    def active(self) -> Optional[AccountProfile]:
        return next((a for a in self.accounts if a.id == self.active_account_id), None)


@dataclass
class CredentialsSnapshot:
    """Claude Code 자격증명의 원자적 스냅샷.

    Linux에서 credentials_blob 은 ~/.claude/.credentials.json 의 **통째 바이트**다
    ({mcpOAuth, claudeAiOauth, designOauth}). oauth_account 는 ~/.claude.json 의
    oauthAccount 서브트리(이메일/조직 메타).
    """

    credentials_blob: bytes
    oauth_account: Optional[dict]

    def to_dict(self) -> dict:
        # 통째 스왑: .credentials.json 텍스트를 그대로 보존해 byte-동일 복원 보장.
        return {
            "credentialsText": self.credentials_blob.decode("utf-8"),
            "oauthAccount": self.oauth_account,
        }

    @staticmethod
    def from_dict(d: dict) -> "CredentialsSnapshot":
        return CredentialsSnapshot(
            credentials_blob=d["credentialsText"].encode("utf-8"),
            oauth_account=d.get("oauthAccount"),
        )


@dataclass
class ScopedUsageLimit:
    label: str
    percent: float
    resets_at: Optional[float]  # epoch 초


@dataclass
class UsageSnapshot:
    five_hour_percent: Optional[float]
    five_hour_resets_at: Optional[float]
    seven_day_percent: Optional[float]
    seven_day_resets_at: Optional[float]
    fetched_at: float
    scoped_limits: list[ScopedUsageLimit] = field(default_factory=list)


def new_account_id() -> str:
    return str(uuid.uuid4())
