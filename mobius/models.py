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
class AdvisoryRecord:
    """임계값 선제 경고 — **소진이 아니다.** rate_limit 과 같은 레코드에 플래그를 얹지 않고
    별도 필드로 두는 이유: 같은 레코드에 얹으면 모든 소비자가 그 플래그를 각자 알아야 하는
    버그 클래스가 된다(메뉴바·CLI·알림이 "소진"이라고 거짓말하게 된다).
    """

    utilization: float      # 경고를 유발한 사용률(%)
    resets_at: float        # 경고가 걸린 창의 리셋 시각 — 지나면 자동 무효(시간 게이트)
    detected_at: float      # 처음 경고가 올라온 시각. 수동 핀의 거부권 판정에 쓴다.

    # 경계에서 사용률이 오르내릴 때 경고가 깜빡이며 알림·후보 탐색 백오프를 매번
    # 리셋하는 것을 막는 히스테리시스 폭(포인트).
    HYSTERESIS = 5.0

    def to_dict(self) -> dict:
        return {"utilization": self.utilization, "resetsAt": self.resets_at,
                "detectedAt": self.detected_at}

    @staticmethod
    def from_dict(d: dict) -> "AdvisoryRecord":
        return AdvisoryRecord(utilization=float(d["utilization"]),
                              resets_at=float(d["resetsAt"]),
                              detected_at=float(d["detectedAt"]))

    @staticmethod
    def should_set(utilization: float, threshold: float) -> bool:
        return utilization >= threshold

    @staticmethod
    def should_clear(utilization: float, threshold: float) -> bool:
        """임계값 - 5 이하. 둘 사이(밴드 내부)면 set 도 clear 도 False → 기존 상태 유지."""
        return utilization <= threshold - AdvisoryRecord.HYSTERESIS


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
    # 핀을 세운 시각. 플래그만으로는 "경고를 보고 나서 일부러 돌아온 핀"과 "몇 시간 전에 그냥
    # 눌러둔 핀"을 구분할 수 없다 — advisory 전환의 거부권은 전자에게만 준다.
    pinned_at: Optional[float] = None
    # 임계값 선제 경고(소진 아님). **엔진의 advisory 경로만** 읽는다 — 아래 주석 참조.
    advisory: Optional[AdvisoryRecord] = None

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
            "pinnedAt": self.pinned_at,
            "advisory": self.advisory.to_dict() if self.advisory else None,
        }

    @staticmethod
    def from_dict(d: dict) -> "AccountProfile":
        rl = d.get("rateLimit")
        ad = d.get("advisory")
        pinned_at = d.get("pinnedAt")
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
            pinned_at=float(pinned_at) if isinstance(pinned_at, (int, float)) else None,
            advisory=AdvisoryRecord.from_dict(ad) if ad else None,
        )

    def is_limited(self, now: float) -> bool:
        """지금 한도에 걸려 있는가 (리셋 시각 전인가)."""
        if self.rate_limit is None:
            return False
        return now < self.rate_limit.resets_at

    def has_active_advisory(self, now: float) -> bool:
        """임계값 선제 경고가 지금 유효한가(경고가 걸린 창의 리셋 전인가).

        `is_limited` 와 시간 게이트 모양은 같지만 **의미가 다르다 — 소진이 아니라
        "곧 찰 것 같다"이다.**
        ★ `is_limited`·`auto_switch_may_leave`·CLI 표시에서 **절대 읽지 말 것.** 그 셋은
          "이 계정을 지금 쓸 수 없다"는 정직한 신호라, 경고가 섞이는 순간 표시와 알림 문구가
          거짓말을 한다. 소비자는 엔진의 advisory 경로뿐이다.
        """
        return self.advisory is not None and now < self.advisory.resets_at

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
    # 한도 차기 전 미리 전환 — 기본 꺼짐. 켜면 5분마다 활성 계정 사용량을 조회한다(네트워크).
    advisory_enabled: bool = False
    advisory_threshold: float = 90.0    # 50~95

    def to_dict(self) -> dict:
        return {
            "accounts": [a.to_dict() for a in self.accounts],
            "activeAccountID": self.active_account_id,
            "autoSwitchEnabled": self.auto_switch_enabled,
            "desktopSyncEnabled": self.desktop_sync_enabled,
            "desktopAutoSwitchEnabled": self.desktop_auto_switch_enabled,
            "autoSwitchedFromPrimary": self.auto_switched_from_primary,
            "advisoryEnabled": self.advisory_enabled,
            "advisoryThreshold": self.advisory_threshold,
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
            advisory_enabled=bool(d.get("advisoryEnabled", False)),
            advisory_threshold=float(d.get("advisoryThreshold", 90.0)),
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
    # ponytail: 파싱만 하고 판정에는 쓰지 않는다 — 아래 exhausted_account_window 주석 참조.
    # upstream 이 유지하는 와이어 포맷이라 대응표(PORTING.md)를 위해 남긴다.
    scoped_limits: list[ScopedUsageLimit] = field(default_factory=list)
    # 월간 지출 한도 블록(`spend`). enabled=False 면 이 계정엔 지출 한도가 없다.
    spend_enabled: Optional[bool] = None
    spend_percent: Optional[float] = None

    def exhausted_account_window(self) -> bool:
        """**계정 자체**(5시간·7일)가 막혔는가 — 리셋 시각의 유효성과 무관.

        세션 로그의 rate-limit 라인은 **어느 계정 것인지 적혀 있지 않아** 활성 계정에
        오귀인된다(실행 중 claude 세션이 옛 자격증명을 들고 있어 전환 뒤에도 옛 계정의
        에러를 뱉는다). 이 스냅샷은 **계정별 토큰으로 조회**한 것이라 오귀인이 불가능하므로,
        로그 hit의 진위를 여기서 판정한다.

        ★ **모델 전용 한도(scoped_limits)를 일부러 제외한다.** 그것까지 소진으로 기록하면
          `is_limited` 가 "계정을 못 쓴다"와 "그 모델만 못 쓴다"를 구분하지 못해, 계정은
          멀쩡한데 Fable 주간만 100% 인 폴백이 `_first_available` 후보에서 **며칠간**
          빠진다 — 오귀인 사고(#19)와 같은 실패를 자초하는 것이다. 모델 전용 한도로
          전환하려면 `is_limited`/후보 선택이 그 구분을 먼저 이해해야 한다(미지원).
        """
        return (self.five_hour_percent or 0.0) >= 100 or (self.seven_day_percent or 0.0) >= 100

    def account_reset_after(self, now: float) -> Optional[float]:
        """소진된 계정 창 중 **아직 안 지난** 리셋 시각의 최댓값. 없으면 None.

        ★ 최댓값이다(최솟값 아님). 5시간·7일이 동시에 100% 면 계정은 **늦은 쪽**이 풀릴
          때까지 막혀 있다. 이른 쪽을 쓰면 레코드가 먼저 만료돼 엔진이 아직 막힌 계정으로
          돌아갔다가 즉시 다시 튕긴다.
        ★ 이미 지난 리셋은 버린다 — 창이 리셋됐는데 100% 로 읽힌 낡은 응답이라, 그대로
          기록하면 `is_limited` 가 곧바로 False 가 돼 판정이 무의미해진다. 호출자는 이
          None 을 "여유 있음"이 아니라 **판정 보류**로 다뤄야 한다.
        """
        resets = [r for pct, r in ((self.five_hour_percent, self.five_hour_resets_at),
                                   (self.seven_day_percent, self.seven_day_resets_at))
                  if pct is not None and pct >= 100 and r is not None and r > now]
        return max(resets) if resets else None


def new_account_id() -> str:
    return str(uuid.uuid4())
