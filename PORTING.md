# 포팅 대응표

upstream **[chussum/mobius](https://github.com/chussum/mobius)** (Swift/macOS) 대비
이 포크의 상태를 기록한다. 목적은 하나다 — **어떤 upstream 변경을 왜 적용했고, 왜
적용하지 않았는지**를 남겨 다음 pull 때 같은 판정을 반복하지 않는 것.

포팅 기준: `v0.5.1` (`b6dcd0e`, 2026-08-10)

---

## 모듈 대응

| upstream (Swift) | 이 포크 (Python) |
|---|---|
| `MobiusCore/Models.swift` | `mobius/models.py` |
| `MobiusCore/AccountStore.swift` | `mobius/store.py` |
| `MobiusCore/ClaudeConfigIO.swift` | `mobius/claude_config.py` |
| `MobiusCore/Switcher.swift` | `mobius/switcher.py` |
| `MobiusCore/AutoSwitchEngine.swift` | `mobius/autoswitch.py` |
| `MobiusCore/RateLimitParser.swift` | `mobius/ratelimit_parser.py` |
| `MobiusCore/SessionLogWatcher.swift` | `mobius/log_watcher.py` |
| `MobiusCore/TokenRefresher.swift` | `mobius/token_refresher.py` |
| `MobiusCore/FallbackAuthChecker.swift` | `mobius/fallback_check.py` |
| `MobiusCore/UsageFetcher.swift` | `mobius/usage.py` |
| `MobiusCore/MobiusEnvironment.swift` | `mobius/env.py` |
| `MobiusApp/AppState.swift` (틱 루프) | `mobius/daemon.py` |
| `mobius/Mobius.swift` (CLI) | `mobius/cli.py` |
| `MobiusCore/KeychainClient.swift` | **없음** — Linux는 파일이 진실의 원천 |
| `MobiusApp/Views/*`, `SyncEngine`, `DesktopSwitcher`, `UpdateChecker` | **없음** — macOS 전용 |
| `MobiusCore/Codex*` | **미포팅** — Claude 경로만 옮겼다 |
| `MobiusCore/AuthSuspicion.swift`, `UsagePollBreaker.swift` | **미포팅** (아래 갭 참조) |

### 구조적 차이 (판정에 반복해서 쓰인다)

1. **Keychain 없음.** upstream의 `security` CLI 우회로(파티션 도장, 4KB 한계,
   `-w` hex 인코딩)는 이 포크에 대응물이 없다. `KeychainClient.swift`만 건드리는
   커밋은 자동으로 "해당 없음"이다.
2. **UI 이벤트 없음.** macOS 앱이 "팝오버 열 때" 하던 일은 데몬의 시간 게이트로
   옮겨야 한다. 그냥 빼면 그 경로가 통째로 사라진다 — 실제로 그렇게 사고가 났다
   (아래 `usage.py` 항목).
3. **HTTP 스택이 다르다.** Python은 `urllib`을 쓴다. `URLSession` 고유 동작
   (공유 세션의 디스크 캐시 등)에 기인한 문제는 대응물이 없다 — 단, **근거를
   실측으로 확인하고** 기록할 것.

---

## upstream 변경 적용 이력

### `512abfa..b6dcd0e` (v0.5.0 → v0.5.1) — 검토 2026-08-14

| 커밋 | 내용 | 판정 |
|---|---|---|
| `68b68d4` | `security -i` 4KB 초과 시 잘린 쓰기 → argv(`-X` hex) 우회 | **해당 없음** — Keychain 미사용 |
| `5ad6afe` | `security -w`가 비출력 바이트를 hex로 반환 → blob 손상 | **해당 없음** — Keychain 미사용 |
| `7fc66d2` | `URLSession.shared`가 Bearer 토큰을 디스크 캐시에 평문 저장 (#12) | **해당 없음** — 근거 아래 |
| `b40c026` | 위 픽스의 테스트가 빈 단언이었음 → 전송 경로 주입 단언 | **적용** |
| `b6dcd0e` | 버전 0.5.1 | 해당 없음 |

**`7fc66d2` 해당 없음의 근거 (실측 2026-08-14)** — 추측이 아니라 확인:

```python
>>> urllib.request.build_opener().handlers
[UnknownHandler, HTTPHandler, HTTPDefaultErrorHandler, HTTPRedirectHandler,
 FTPHandler, FileHandler, DataHandler, HTTPSHandler, HTTPErrorProcessor]
>>> [n for n in dir(urllib.request) if "ache" in n]
['CacheFTPHandler', '__cached__', 'ftpcache']     # 캐시는 FTP 전용뿐
>>> "cache" in inspect.getsource(urllib.request.OpenerDirector.open).lower()
False
```

`urllib.request`는 HTTP 응답 디스크 캐시 계층이 없다. 문제는 `URLSession.shared`가
물고 있는 `URLCache` 고유의 것이다.

**`b40c026` 적용 내용** — 교훈이 그대로 들어맞았다. `usage.fetch`는 `transport`
파라미터를 이미 갖고 있었지만, 기존 테스트는 주입 값의 *결과*만 봤을 뿐
**fetch가 주입 경로로만 나가는지**를 단언하지 않았다. 호출부가 `_default_transport`로
되돌아가도 통과했을 것이다. `tests/test_usage.py::test_fetch_goes_through_injected_transport`
추가. upstream이 한 대로 **회귀를 일부러 주입해 빨간불이 되는 것까지 확인**하고 넣었다.

> upstream CLAUDE.md 실패 기록 18의 교훈 (2):
> "세션 속성이 올바른가"를 보는 테스트는 회귀를 못 막는다 — 호출부가 다른 세션으로
> 되돌아가도 안 쓰이는 속성은 그대로 남아 초록불이다. 전송 경로를 주입하고
> **fetch가 그 경로를 실제로 탔는지**를 단언할 것.

---

## 포팅 누락으로 발생한 결함 (수정 완료)

### `needsReauth` 딱지가 영구 고착 — 계정이 "매번 재로그인" 상태로 갇힘

**증상.** `mobius list`에서 멀쩡한 계정이 `[재로그인 필요]`로 표시되고 풀리지 않음.
자동 전환 후보에서도 빠져, 다른 계정이 한도에 걸리면 "모든 계정 한도 소진"이 뜬다.

**실측 (2026-08-13~14).** 폴백 계정의 refresh 토큰은 9/12까지 유효했고 rate limit은
7/31에 이미 리셋됐는데, 8/13 16:47 데몬이 `ALL_EXHAUSTED`를 4번 찍었다.
`needsReauth: true` 하나 때문에 멀쩡한 계정이 후보에서 빠진 것.

**근본 원인 — 포팅 누락.** upstream에는 자가 해제 경로가 있다:

```swift
// MobiusApp/AppState.swift:377-381 (refreshUsageIfStale)
// 조회 성공 = 토큰 살아있음 → 잘못 남은 재로그인 마킹 자가 해제
if profile.needsReauth {
    try? store.setNeedsReauth(profile.id, false)
```

같은 파일 `:313-315` 주석이 의도를 명시한다 — "needsReauth 계정도 계속 조회한다.
CLI에서 직접 `claude auth login`으로 복구하는 경우, 조회가 200이면 그 복구를 감지해
needsReauth를 자동으로 푼다."

이 포크는 `UsageFetcher`를 `usage.py`로 옮겼지만 **그것을 구동하는
`AppState.refreshUsageIfStale()`을 옮기지 않았다.** 결과: `usage.py`는 어디서도
호출되지 않는 죽은 모듈이 됐고, 딱지는 단조 상태가 됐다(붙으면 수동 `mobius capture`
전까지 안 풀림). 위 "구조적 차이 2"의 전형적 사고 — UI 이벤트로 돌던 경로를 데몬으로
옮기지 않고 그냥 뺐다.

**수정.**

| 파일 | 변경 | 근거 |
|---|---|---|
| `daemon.py` | `_recheck_flagged_accounts()` — 딱지 붙은 계정만 usage 조회, 200이면 해제 | upstream `AppState.swift:377-381` 이식. UI 이벤트 대신 30분 게이트 |
| `daemon.py` | `_clear_expired_rate_limits()` — 리셋 지난 레코드 소거 | stale 레코드가 복귀 게이트·표시를 오염 |
| `store.py` | `set_secret`에서 **refresh 토큰 회전 시** 딱지 해제 | 딱지는 특정 토큰에 대한 판정 — 토큰 교체 시 전제 소멸 |
| `switcher.py` | `reconcile` 조기 반환에 `not needs_reauth` | 라이브가 그 계정 = 살아있음 증명 |

**★ 해제만 한다 — 401을 딱지로 승격하지 않는다.** 자연 만료 토큰의 401은 오탐이고,
마킹 판정은 refresh 결과를 보는 `FallbackAuthChecker`의 몫이다. upstream
`AuthSuspicion.swift:15-17`이 명시적으로 경고하는 지점 — 추측성 신호를 `needsReauth`로
올리면 엔진이 멀쩡한 주계정을 밀어내는 이슈 #4가 재발한다.

**★ 해제 근거는 바이트가 아니라 refresh 토큰 값이다** (upstream 이슈 #14 리뷰 반영,
2026-08-15). 초기 구현은 저장 blob 바이트 변경을 신호로 썼는데, `refresh_active_snapshot_if_stable`
이 활성 계정 스냅샷을 5분마다 무조건 되저장하고 Claude 인증과 무관한 필드(mcpOAuth 등)만
바뀌어도 blob 이 달라진다 → **죽은 계정의 딱지가 조용히 풀린다**(재현 확인). 회전은 성공한
토큰 교환이나 새 로그인에서만 일어나므로 그것만이 살아있다는 증거다.

---

## 상류에도 있는 결함 (이 포크에서 먼저 수정)

### 세션 로그의 rate-limit hit 가 엉뚱한 계정에 귀속됨

**증상 (실측 2026-08-15).** 7일 사용량 16%인 멀쩡한 폴백에 100% 소진된 주계정의 리셋
시각이 덮였다. `is_limited` 가 True 가 되어 전환 후보에서 빠지고 "모든 계정 한도 소진"
알림이 반복됐다 — 자동 전환이 통째로 죽는다.

**근본 원인.** 세션 로그의 rate-limit 라인에는 **어느 계정 것인지 적혀 있지 않다.**
데몬은 "스캔 시점의 활성 계정"으로 추정했는데, 이벤트의 진짜 주체는 "요청을 보낼 때
활성이던 계정"이고 그 사이 지연이 무제한이다 — 실행 중 claude 세션이 자격증명을
메모리에 들고 있어 전환 뒤에도 옛 계정의 에러를 계속 뱉는다(동시 세션 4개, 2분 반,
6줄). **hit 의 자체 타임스탬프로는 못 거른다** — 전환 뒤에 찍히기 때문이다.

**upstream 은 같은 원칙을 세워 두고 적용하지 않았다** (`AppState.swift:858-864`):

> 인증 만료 로그는 "어느 계정" 것인지 적혀 있지 않아 활성 계정에 오귀인된다 →
> needsReauth 는 로그가 아니라 usage API 401(계정별 토큰으로 조회 → 오귀인 불가)로만
> 판정한다. **여기서는 rate-limit(창 소진)만 처리한다.**

`needsReauth` 에는 적용됐고 `rateLimit` 에는 안 됐다. upstream 의 `verifyWindowsAfterSpendLimit`
는 P3(월간 지출)에만 걸려 있어 weekly/session hit 은 무검증으로 활성 계정에 기록된다.

**수정.** 로그 hit 를 **증거가 아니라 트리거**로 강등한다.

| 파일 | 변경 |
|---|---|
| `daemon.py` | `_verify_hit()` — usage 로 실제 소진을 확인해야 기록·엔진 호출 |
| `models.py` | `UsageSnapshot.exhaustion()` — 소진 창의 (리셋 시각, model_scoped) |

- 리셋 시각도 API 실제 값을 쓴다 — 로그 "resets 9pm" 파싱보다 정확(21:00 vs 20:59)
- 네트워크: 이미 기록이 있으면 0(버스트 전체 공짜), 조회 간격 하한, 평시 0

---

### 그 검증이 만든 2차 결함 — 판정을 이진으로 접었다 (수정 2026-08-16)

위 수정은 방향이 맞았지만 **판정 결과를 `Optional`(있음/없음)로 표현**했다. 실제 도메인은
넷이다 — ①확인됨 ②확인 결과 아님 ③아직 모름 ④이 증거로는 판정 불가. ③④가 전부 ②로
흡수됐고, 증거 스트림이 **일회성**이라 ②는 파괴적이다:

- `SessionLogWatcher` 는 오프셋을 전진시킨 뒤 파싱하므로 hit 는 **한 번만 배달**된다
- 사용자는 한도 에러를 보면 타이핑을 멈춘다 → 새 hit 이 오지 않는다

즉 **조회 한 번 실패 = 진짜 소진의 영구 유실**. 검증 도입 전(무조건 기록)보다 나쁘다.
당시 주석에 "hit 는 반복되므로 다음 틱에 다시 시도된다"고 적었는데 **사실이 아니었다.**

여기서 파생된 결함 5개를 함께 고쳤다.

| # | 결함 | 근본 원인 | 수정 |
|---|---|---|---|
| 1 | 조회 실패 시 hit 영구 유실 | ③을 ②로 접음 | `Verdict.UNKNOWN` + 트리거 보관(`_pending_hit`, TTL 15분) |
| 2 | 쿨다운 안에 온 hit 폐기 | 신선도를 **나이**로 판단 | 낡은 스냅샷을 재사용하지 않는다 → 순서 조건이 구조적으로 보장. 게이트 안이면 보류 |
| 3 | 월간 지출에 "모델 전용+핀" 예외 적용 | `model_scoped` 에 지출 한도를 밀어넣음 | `HitKind.MONTHLY_SPEND` 로 분리, `model_scoped=False` |
| 4 | 모델 전용 한도를 계정 소진으로 기록 | `is_limited` 가 두 개념을 한 필드에 섞음 | `exhaustion()` 에서 scoped 분기 **삭제** |
| 5 | 동시 소진 시 **이른** 리셋 사용 | — | `account_reset_after()` 가 최댓값. 지난 리셋은 보류로 |

결함 4가 특히 중요하다: 모델 전용 주간 한도(Fable 등)는 **며칠간** 100% 로 남는데,
그걸 소진으로 기록하면 계정은 멀쩡한 폴백이 `_first_available` 후보에서 며칠 빠진다 —
**우리가 #19 로 제보한 실패를 스스로 재현하는 것**이다. upstream 은 PR #21 에서 같은
결론에 도달했고(`isLimited` / `isModelLimited` 분리), 이 포크는 그 개념 분리 전까지
"모델 전용 한도로는 자동 전환하지 않는다"로 둔다.

**한 번 잘못 짚었다가 되돌린 것 — 월간 지출 한도(P3).** 처음엔 "P3 hit 가 창 검증에
걸려 100% 폐기된다"를 결함으로 보고 응답의 `spend` 블록으로 판정하도록 고쳤다.
PR #21 본문을 읽고 **그 판단이 틀렸음을 확인해 되돌렸다** — upstream 실측(2026-07-13):

> 월간 지출 한도(P3)는 창 소진이 아니다 — 기록하면 24h 폴백 오탐이 된다
> (플랜 창 여유 상태에서도 뜨고 세션은 정상 동작). 단 창 소진과 겹치면 이 메시지가
> 우선 표시돼 창 소진을 가리므로 usage 로 교차 확인.

즉 P3 는 **"계정을 못 쓴다"의 증거가 아니다.** 창으로 교차 확인하던 기존 동작이 맞았고,
`spend` 블록 판정은 검증 도입 전의 24h 오탐을 되살리는 것이었다. `spend` 파싱은 다시
뺐고, `HitKind` 는 판정 분기가 아니라 **"지출 한도는 모델 전용 한도가 아니다"의 표식**
으로 남는다(결함 3).

**usage 응답 실측 (2026-08-16, Max 20x).** `limits[]` 는 있고 `weekly_scoped` 항목도
들어 있다 — 단 `percent: 0`, `resets_at: null`, `is_active: false` (`scope.model.display_name`
= "Fable"). 즉 파싱은 되지만 소진을 밟은 적이 없다. 모델별 한도는 최상위 형제 키
(`seven_day_opus`/`seven_day_sonnet`/`seven_day_cowork`/`seven_day_omelette`/
`seven_day_oauth_apps` + 코드네임들)에도 중복 표현돼 있고 그쪽은 전부 `null` 이다 —
소진 시 어느 쪽이 정본인지는 실측 전까지 모른다.

> 이 문단의 첫 판정("`limits[]` 키가 아예 없다", 커밋 f082562 메시지에도 그대로 들어갔다)은
> **오독이었다.** 응답 아래쪽에 있는 것을 상단 블록만 보고 단정했다. 판정 근거는 응답 원문을
> 끝까지 읽고 적는다.

---

### 검증이 답하지 않은 질문 — 귀속 그 자체 (수정 2026-08-22)

위 두 절은 **증거원을 바꿔**(로그 → usage API) 귀속 문제를 *회피*했다. usage 는 계정별
토큰으로 조회하니 오귀인이 불가능하다 — 맞다. 그러나 그것이 답하는 질문은
*"이 계정이 소진인가"* 이지 *"이 hit 이 누구 것인가"* 가 아니다. 그래서 둘이 남았다:

- **평시 네트워크 0 계약이 깨진다** — 모든 hit 이 조회를 부른다. upstream PR #21 도 같고,
  그쪽의 `giveUpVerification` 은 조회가 계속 실패하면 **결국 로그를 그대로 믿는다**
  (= 결함 그대로) — 가드는 "최근 5분 내 전환 없음"이라는 **거친 프록시**다.
- **진짜 주인의 소진이 버려진다** — 남의 hit 으로 판정돼 `REFUTED` 로 폐기된다.

**근본 원인은 증거원이 아니라 시간 좌표였다.** hit 라인의 타임스탬프는 요청 시각이 아니라
**429 재시도가 끝난** 시각이다. 실측(2026-08-15 사고 로그 6줄):

| 지표 | 값 |
|---|---|
| 재시도 창(요청 시작 → hit 기록) | **2분 6초** (엔진 쿨다운 120초보다 **길다**) |
| 사고 hit 6건 중 선행 턴 시작 라인 복원 | 6/6 |
| 전체 로그 hit 88건 중 복원 | **88/88** (서브에이전트 61건 포함) |

즉 시간 창으로 억누르는 처방(쿨다운 연장 등)은 구조적으로 못 막고, 필요한 좌표는
**로그 안에 이미 있다.**

**수정 — 점이 아니라 구간으로 판정한다.**

```
owner = [묶였을 수 있는 최이른 시각, 로그에 찍힌 시각] 내내 활성이 안 바뀌었으면 그 계정
        바뀔 수 있었으면 → 답하지 않는다 → 기존 usage 검증
```

구간 안에 전환이 없으면 자격증명 결합 규칙이 무엇이든 답은 하나다 — **가정을 쓰지 않는다.**
검증을 대체하지 않고 **그 대상을 불확정 hit 으로 좁힌다.**

| 파일 | 변경 |
|---|---|
| `ratelimit_parser.py` | `turn_start_at()` · `scan_line()`(라인 1회 디코드) · `bound_at`/`observed_at` |
| `log_watcher.py` | 파일별 턴 경계 유지 + 서브에이전트는 부모 세션까지 넓힌 하한 |
| `daemon.py` | `_active_timeline`(가능한 최이른, 관찰, 계정) · `_owner_for()` · `_record_attributed()` |

**1차 설계에서 스스로 만든 임시 처방 3건** — 가정을 뽑아 실측에 걸어 전부 기각했다.

| 가정 | 실측 | 처리 |
|---|---|---|
| 타임라인 항목 = 전환 시각 | 외부 전환은 reconcile(15초)이 **관찰**만 한다 | 항목을 `(최이른, 관찰)` 쌍으로. 자기 전환만 `exact` |
| 턴 시작은 그 파일 안에 있다 | 부모 턴 ↔ 서브에이전트 프롬프트 격차 **p50 21분**, 61건 중 **57건**이 재시도 창 초과 | 부모 세션 파일까지 넓혀 이른 쪽 사용. 부모를 모르면 답하지 않음 |
| 귀속되면 로그 리셋 시각을 믿는다 | 시각 못 읽는 변형(P5)은 24h 폴백 | 귀속돼도 기록 안 함 — 실제 시각은 API 만 안다 |

두 번째가 결정적이다. 서브에이전트는 부모와 **같은 CLI 프로세스**라 자격증명이 부모 턴에
묶인다. 파일별 좌표는 21분 **늦게** 잡히고 **늦은 좌표 = 새 계정** — #19 와 같은 방향의
오귀인이다. 관측 hit 의 69%가 여기 해당했다.

**회귀 검증.** 방어 4종을 하나씩 되돌려 무는 것을 확인했다. 그 과정에서 검사 하나가
**다른 이유로 초록**인 것을 발견했다 — 부모 조회를 없애도 결과가 "기록 안 함"이라
포기와 구분되지 않았다. 양성 결과를 요구하는 검사를 추가하고 그것도 되돌려 확인했다.

**기각한 대안 — `~/.claude.json` 의 `cachedUsageUtilization`** (claude **2.1.239**, 측정
**2026-08-22**). `accountUuid` 가 박힌 로컬 usage 스냅샷이라 네트워크 0 판정이 가능해 보였다.
번들 구현을 직접 읽어 기각했다:

- 쓰기는 **라이브 계정 것만**(`liveAccountUuid !== fetchedFor → return`), **5분 쓰로틀**
- 읽기는 계정 불일치 시 **캐시를 삭제**, TTL **1시간**
- 호출부는 `/usage` 화면과 `collectUsageData`(텔레메트리) 뿐 — **한도 이벤트와 무관**

mobius 가 hit 시점에 물어야 할 것은 대개 "**방금 떠난** 계정이 소진인가"인데, 이 캐시는
비활성 계정 정보를 **구조적으로 담을 수 없다**. 실측 뒷받침: 이 머신의 값은 사고 32초 뒤
1회 기록된 뒤 **170시간 정지**였다. *재검토 트리거: 429 응답 시 갱신하거나 계정별 항목을
보관하는 버전이 나오면 다시 본다.*

부수 확인 — CLI 는 매 응답 헤더(`anthropic-ratelimit-unified-*`)로 사용률·리셋을 이미
알지만(`seedUtilization`), 그 값은 인메모리에만 살고 세션 로그·설정 어디에도 기록되지
않는다(전수 grep: 매치는 전부 대화 내용). **외부 프로세스가 붙을 지점이 없다.**

---

## 알려진 갭 (미포팅)

upstream에 있으나 아직 옮기지 않은 것. 버그가 아니라 범위 밖이다.

- **Codex 지원 전반** (`Codex*.swift`) — 이 포크는 Claude 경로만 다룬다.
- **모델 전용 주간 한도로의 자동 전환** — `is_limited` 가 "계정을 못 쓴다"와 "그 모델만
  못 쓴다"를 구분하지 못한다. 구분 없이 기록만 넣으면 멀쩡한 폴백이 며칠간 후보에서
  빠진다(위 결함 5). upstream PR #21 의 `isModelLimited` 분리를 따라가야 풀린다.
- ~~**임계값 선제 전환**~~ — **포팅 완료** (2026-08-15). `AdvisoryRecord` + `pinned_at`,
  엔진 `check_advisory`, 데몬 `_poll_threshold`/`_probe_candidate`, CLI `mobius advisory`.
  UI 이벤트가 없으므로 5분 라이브싱크 성사 뒤에 폴을 얹었다(`refresh_active_snapshot_if_stable`
  이 bool 을 돌려주는 신선도 계약). 기본 꺼짐 — 켜야 네트워크를 쓴다.
  upstream 이 리뷰에서 잡은 결함 두 개를 그대로 가져왔다: **핀 시각 비교**(옛 핀이 새 경고를
  영구 거부하지 않도록)와 **복귀 게이트 max**(advisory 만으로 떠난 경우 2분 주기 핑퐁 방지).
- **`AuthSuspicion`** (`b8627bb`) — 세션 활동 × 토큰 만료 상관으로 "세션은 도는데
  로그인이 죽었다"를 감지하는 **표시 전용** 배지. 데몬엔 표시할 UI가 없어 후순위.
- **`UsagePollBreaker`** (`b8627bb`) — usage 조회 3연속 실패 시 배경 폴 중단. upstream 은
  팝오버를 열 때마다 폴이 추가로 돌아 필요했지만, 이 포크는 UI 이벤트가 없어 폴 주기가
  5분으로 고정이다(실패 1회 = 타임아웃 1회). 폭주 여지가 없어 넣지 않았다.

---

## upstream pull 후 절차

1. `cd ~/mobius && git log --oneline <이전HEAD>..HEAD` 로 범위 확인
2. `git diff --stat <이전HEAD>..HEAD` 로 건드린 파일 확인 — 위 **모듈 대응**표에서
   "없음"인 파일만 바뀌었다면 거기서 끝
3. 남은 커밋은 `git show <sha>` 로 **커밋 메시지 본문까지** 읽는다. 이 저장소의
   커밋 메시지에는 실측 근거와 판정 이유가 들어 있어, diff만 봐서는 놓친다
4. "해당 없음" 판정은 **근거를 실측으로 확인**하고 이 문서에 남긴다 — 특히 언어/런타임
   차이를 근거로 들 때(위 `7fc66d2` 항목이 예시)
5. 적용한 것은 테스트를 함께 옮기고, **회귀를 주입해 빨간불이 되는지 확인**한다
6. 이 문서의 "포팅 기준"과 적용 이력을 갱신
