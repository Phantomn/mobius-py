import time

from mobius.cli import _fmt_in

NOW = 1_700_000_000.0


def _hm(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


def test_fmt_in():
    assert _fmt_in(None, NOW) == "-"
    assert _fmt_in(NOW - 1, NOW) == "-"
    e = NOW + 90 * 60
    assert _fmt_in(e, NOW) == f"1시간 30분 후 ({_hm(e)})"
    e = NOW + (2 * 1440 + 3 * 60 + 59) * 60
    assert _fmt_in(e, NOW).startswith("2일 3시간 59분 후 (")
