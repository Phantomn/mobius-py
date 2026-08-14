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

**실측 (2026-08-13~14).** `tr3-ai` 계정의 refresh 토큰은 9/12까지 유효했고
rate limit은 7/31에 이미 리셋됐는데, 8/13 16:47 데몬이 `ALL_EXHAUSTED`를 4번 찍었다.
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
| `store.py` | `set_secret`에서 blob 변경 시 딱지 해제 | 딱지는 특정 토큰에 대한 판정 — 토큰 교체 시 전제 소멸 |
| `switcher.py` | `reconcile` 조기 반환에 `not needs_reauth` | 라이브가 그 계정 = 살아있음 증명 |

**★ 해제만 한다 — 401을 딱지로 승격하지 않는다.** 자연 만료 토큰의 401은 오탐이고,
마킹 판정은 refresh 결과를 보는 `FallbackAuthChecker`의 몫이다. upstream
`AuthSuspicion.swift:15-17`이 명시적으로 경고하는 지점 — 추측성 신호를 `needsReauth`로
올리면 엔진이 멀쩡한 주계정을 밀어내는 이슈 #4가 재발한다.

---

## 알려진 갭 (미포팅)

upstream에 있으나 아직 옮기지 않은 것. 버그가 아니라 범위 밖이다.

- **Codex 지원 전반** (`Codex*.swift`) — 이 포크는 Claude 경로만 다룬다.
- **임계값 선제 전환** (`usageAdvisory` / `pinnedAt`, `b8627bb`) — 한도 100% 전에
  미리 전환. `AccountProfile`에 필드 2개와 엔진 분기가 필요하다.
- **`AuthSuspicion`** (`b8627bb`) — 세션 활동 × 토큰 만료 상관으로 "세션은 도는데
  로그인이 죽었다"를 감지하는 **표시 전용** 배지. 데몬엔 표시할 UI가 없어 후순위.
- **`UsagePollBreaker`** (`b8627bb`) — usage 조회 3연속 실패 시 배경 폴 중단.
  이 포크의 usage 호출은 딱지 붙은 계정 한정 + 30분 게이트라 현재는 폭주 여지가 작다.

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
