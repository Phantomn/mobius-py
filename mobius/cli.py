"""CLI — list / switch / status / capture / auto (Mobius.swift 포팅 + 데몬/서비스).

auto on|off 는 자동 전환 플래그 토글, auto --watch 는 foreground 데몬, auto --install-service 는
systemd user unit 생성.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

from .claude_config import ClaudeConfigIO
from .env import MobiusEnvironment
from .models import AccountProfile
from .store import AccountStore
from .switcher import Switcher


class _Ctx:
    def __init__(self):
        self.env = MobiusEnvironment.live()
        self.store = AccountStore(self.env)
        self.io = ClaudeConfigIO(self.env)
        self.switcher = Switcher(self.store, self.io)


def _fmt_reset(p: AccountProfile, now: float) -> str:
    rl = p.rate_limit
    if rl is None or rl.resets_at <= now:
        return ""
    mins = int((rl.resets_at - now) / 60)
    return f"  [한도 소진 {mins // 60}시간 {mins % 60}분 후 리셋]"


def _cmd_list(_args) -> int:
    ctx = _Ctx()
    ctx.switcher.reconcile()
    now = time.time()
    if not ctx.store.file.accounts:
        print("등록된 계정이 없습니다. `mobius capture <이름>`으로 등록하세요.")
        return 0
    active_id = ctx.store.file.active_account_id
    for i, p in enumerate(ctx.store.file.accounts):
        active = "●" if p.id == active_id else " "
        role = "primary " if i == 0 else f"fallback{i}"
        reauth = "  [재로그인 필요]" if p.needs_reauth else ""
        print(f"{active} {role}  {p.nickname}  <{p.email_address}>  "
              f"{p.tier_description}{_fmt_reset(p, now)}{reauth}")
    return 0


def _cmd_switch(args) -> int:
    ctx = _Ctx()
    target = next((a for a in ctx.store.file.accounts if a.nickname == args.name), None)
    if target is None:
        names = ", ".join(a.nickname for a in ctx.store.file.accounts)
        print(f"'{args.name}' 계정 없음. 등록된 계정: {names}", file=sys.stderr)
        return 1
    try:
        ctx.switcher.switch_to(target.id)
    except Exception as e:
        print(f"전환 실패: {e}", file=sys.stderr)
        return 1
    # 사용자가 직접 고른 계정 — 모델 전용 한도로 자동으로 밀어내지 않고, 자동 복귀 대상도 아니다.
    ctx.store.set_user_pinned(target.id)
    ctx.store.set_auto_switched_from_primary(False)
    print(f"전환 완료 → {target.nickname} <{target.email_address}>")
    print("실행 중인 claude 세션엔 즉시 적용되지 않을 수 있습니다 — 그 경우 세션을 새로 시작하세요.")
    return 0


def _cmd_primary(args) -> int:
    ctx = _Ctx()
    target = next((a for a in ctx.store.file.accounts if a.nickname == args.name), None)
    if target is None:
        names = ", ".join(a.nickname for a in ctx.store.file.accounts)
        print(f"'{args.name}' 계정 없음. 등록된 계정: {names}", file=sys.stderr)
        return 1
    if ctx.store.file.accounts and ctx.store.file.accounts[0].id == target.id:
        print(f"{target.nickname} 은(는) 이미 primary(주 계정)입니다.")
        return 0
    old_primary = ctx.store.file.accounts[0].nickname if ctx.store.file.accounts else "?"
    ctx.store.set_primary(target.id)
    print(f"주 계정 변경: {old_primary} → {target.nickname} (기존 주 계정은 fallback으로 내려갑니다)")
    print("※ 이 명령은 우선순위만 바꿉니다. 지금 활성 계정을 바꾸려면 `mobius switch` 를 쓰세요.")
    return 0


def _cmd_status(_args) -> int:
    ctx = _Ctx()
    ctx.switcher.reconcile()
    now = time.time()
    active = ctx.store.file.active
    if active is None:
        print("활성 계정 없음 (claude 로그아웃 상태이거나 미등록 계정)")
        return 0
    primary = ctx.store.file.primary
    role = "primary" if primary and active.id == primary.id else "fallback"
    print(f"활성: {active.nickname} <{active.email_address}> ({role}){_fmt_reset(active, now)}")
    print(f"자동 전환: {'켜짐' if ctx.store.file.auto_switch_enabled else '꺼짐'}")
    return 0


def _cmd_capture(args) -> int:
    ctx = _Ctx()
    snap = ctx.io.read_live_snapshot()
    if snap is None:
        print("claude 로그인 상태가 아닙니다. 먼저 `claude`에서 /login 하세요.", file=sys.stderr)
        return 1
    try:
        p = ctx.store.upsert_profile(args.name, snap)
    except Exception as e:
        print(f"캡처 실패: {e}", file=sys.stderr)
        return 1
    ctx.store.set_active(p.id)
    print(f"캡처 완료: {p.nickname} <{p.email_address}> {p.tier_description}")
    return 0


_SERVICE_UNIT = """\
[Unit]
Description=Mobius — Claude Code 계정 자동 전환 데몬
After=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def _install_service() -> int:
    exec_path = shutil.which("mobius")
    exec_start = f"{exec_path} auto --watch" if exec_path else f"{sys.executable} -m mobius auto --watch"
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "mobius.service"
    unit_path.write_text(_SERVICE_UNIT.format(exec_start=exec_start), encoding="utf-8")
    print(f"systemd user unit 작성: {unit_path}")
    print("활성화:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable --now mobius")
    print("로그: journalctl --user -u mobius -f")
    return 0


def _cmd_auto(args) -> int:
    if args.watch:
        from .daemon import Daemon
        Daemon().run()
        return 0
    if args.install_service:
        return _install_service()
    if args.mode in ("on", "off"):
        ctx = _Ctx()
        ctx.store.set_auto_switch(args.mode == "on")
        print(f"자동 전환: {'켜짐' if args.mode == 'on' else '꺼짐'}")
        return 0
    print("사용법: mobius auto on|off | --watch | --install-service", file=sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mobius", description="Claude Code 계정 매니저 (뫼비우스)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="계정 목록").set_defaults(func=_cmd_list)

    sw = sub.add_parser("switch", help="계정 전환")
    sw.add_argument("name", help="전환할 계정 닉네임")
    sw.set_defaults(func=_cmd_switch)

    pr = sub.add_parser("primary", help="지정 계정을 주 계정(primary)으로 승격 — main/fallback 교체")
    pr.add_argument("name", help="주 계정으로 만들 닉네임")
    pr.set_defaults(func=_cmd_primary)

    sub.add_parser("status", help="현재 상태").set_defaults(func=_cmd_status)

    cap = sub.add_parser("capture", help="현재 claude 로그인 계정을 프로필로 캡처")
    cap.add_argument("name", help="저장할 닉네임")
    cap.set_defaults(func=_cmd_capture)

    au = sub.add_parser("auto", help="자동 fallback 켜기/끄기/데몬")
    au.add_argument("mode", nargs="?", choices=["on", "off"], help="on 또는 off")
    au.add_argument("--watch", action="store_true", help="foreground 자동 전환 데몬 실행")
    au.add_argument("--install-service", action="store_true", help="systemd user unit 생성")
    au.set_defaults(func=_cmd_auto)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)
