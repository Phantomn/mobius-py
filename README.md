# Mobius (Linux) — Claude Code 계정 매니저 CLI

macOS 메뉴바 앱 **Mobius**의 코어를 Linux CLI로 포팅한 것. Claude Code 계정을
전환하고, primary 계정이 사용 한도에 도달하면 fallback 계정으로 **자동 전환**한 뒤
primary가 회복되면 되돌아온다. 런타임 의존성 없음(Python 3.10+ 표준 라이브러리만).

## 원본 (upstream)

이 저장소는 **[chussum/mobius](https://github.com/chussum/mobius)** (Swift/macOS)의
비공식 Python 포크다. 원본과 이 포크 모두 MIT 라이선스이며, 원 저작권 고지는
[LICENSE](LICENSE)에 함께 담겨 있다.

| | |
|---|---|
| 원본 | https://github.com/chussum/mobius |
| 포팅 기준 | `v0.5.1` (`b6dcd0e`, 2026-08-10) |

포팅 범위는 **MobiusCore의 Claude 경로**로 한정된다. 메뉴바 UI·Claude Desktop 연동·
멀티 Mac 동기화·DMG 자동 업데이트는 macOS 전용이라 옮기지 않았고, Codex 지원은
아직 포팅하지 않았다. 자동 전환은 데몬(`--watch` 또는 systemd)이 담당한다.

upstream 변경을 반영할 때는 [PORTING.md](PORTING.md)의 대응표를 갱신한다 —
어떤 커밋을 왜 적용했는지, 왜 적용하지 않았는지가 거기 기록된다.

## macOS 원본과의 차이

Linux의 Claude Code는 자격증명을 **Keychain이 아니라 `~/.claude/.credentials.json`
파일**에 저장한다. 따라서 계정 전환은 두 파일의 스왑으로 이뤄진다:

- `~/.claude/.credentials.json` (토큰 blob, 통째 교체)
- `~/.claude.json` 의 `oauthAccount` (이메일/조직 메타)

이 차이 때문에 원본의 `KeychainClient` 계열 수정(`security` CLI의 4KB 한계·hex 인코딩
등)은 이 포크에 대응물이 없다. 반대로 macOS 앱이 UI 이벤트(팝오버 열기)로 돌리던 주기
작업은 데몬의 시간 게이트로 옮겨야 한다.

## 설치

```bash
git clone https://github.com/Phantomn/mobius-py.git
pip install --user -e mobius-py
```

`mobius` 명령이 PATH에 생긴다. (설치 없이 `PYTHONPATH=. python3 -m mobius ...` 로도 실행 가능)

## 사용법

```bash
mobius capture <이름>     # 현재 claude 로그인 계정을 프로필로 저장 (여러 계정을 각각 로그인→capture)
mobius list              # 등록된 계정 목록 (● = 현재 활성)
mobius switch <이름>      # 해당 계정으로 전환
mobius status            # 현재 활성 계정 + 자동 전환 상태
mobius auto on|off       # 자동 fallback 켜기/끄기
mobius auto --watch      # 자동 전환 데몬을 foreground 실행 (세션 로그 감시)
mobius auto --install-service   # systemd user unit 생성
```

### 계정 등록 절차

1. `claude` 에서 계정 A로 로그인 → `mobius capture 회사계정`
2. `claude` 에서 계정 B로 로그인 → `mobius capture 개인계정`
3. `mobius list` 로 확인. `accounts[0]` 이 primary(주 계정)다.

### 자동 전환 상시 구동 (systemd)

```bash
mobius auto --install-service
systemctl --user daemon-reload
systemctl --user enable --now mobius
journalctl --user -u mobius -f      # 로그
```

데몬은 3초마다 `~/.claude/projects/**/*.jsonl` 세션 로그를 감시한다(네트워크 0).
활성 계정이 한도에 걸린 이벤트를 감지하면, 전환 직전 대상 fallback의 OAuth refresh로
생사를 확인한 뒤 살아있는 계정으로 전환한다. primary 리셋 시각이 지나면 자동 복귀한다.

## 동작 원칙 (macOS 원본에서 계승)

- **활성 계정은 절대 OAuth refresh하지 않는다** — claude가 관리하는 토큰을 동시에
  로테이션하면 실행 중 세션이 깨진다. refresh는 폴백 계정 검증 전용.
- **전환 전 안정 읽기** — 토큰과 이메일을 간격 두고 두 번 읽어 일치할 때만 저장한다
  (로그인/전환 찰나의 "새 토큰 + 옛 이메일" 오염 방지).
- **관대한 디코드** — `accounts.json` 에 새 필드가 생겨도 구버전 파일이 깨지지 않고,
  손상 시 `accounts.corrupt.json` 으로 백업한 뒤 중단한다(데이터 유실 방지).
- **실행 중 claude 세션**에는 전환이 즉시 반영되지 않을 수 있다 — 새 세션부터 적용된다.

## 개발/테스트

```bash
MOBIUS_HOME=/tmp/mobtest mobius ...      # 실제 ~/.claude 를 건드리지 않고 격리 실행
PYTHONPATH=. python3 -m pytest tests -q  # 유닛/통합 테스트
```

## 구조

```
mobius/
  env.py              경로 컨테이너 (MOBIUS_HOME override)
  models.py           데이터 모델 (관대 디코드)
  credentials.py      토큰 blob 파싱/재구성
  claude_config.py    라이브 자격증명 읽기/쓰기 (안정 읽기)
  store.py            프로필/비밀 스냅샷 영속 (0600)
  switcher.py         전환/롤백/reconcile/adopt
  ratelimit_parser.py 세션 로그 rate-limit 이벤트 파서
  log_watcher.py      세션 로그 tail (오프셋 추적)
  autoswitch.py       자동 전환 상태머신
  token_refresher.py  OAuth refresh (폴백 생사 판정)
  usage.py            usage 엔드포인트 조회
  fallback_check.py   폴백 계정 생사 검증
  daemon.py           자동 전환 틱 루프
  cli.py              CLI 진입점
```
