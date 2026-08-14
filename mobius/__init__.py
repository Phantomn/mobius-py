"""Mobius (Linux) — Claude Code 계정 전환/자동 fallback CLI.

macOS SwiftUI 앱 Mobius의 코어 로직을 Python으로 포팅한 것. Linux Claude Code는
자격증명을 Keychain이 아니라 ~/.claude/.credentials.json 파일에 두므로, 전환은
그 파일과 ~/.claude.json 의 oauthAccount 를 스왑하는 것으로 이뤄진다.
"""

__version__ = "0.1.0"
