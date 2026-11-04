"""signin：每日簽到（一個帳號一天一次，隊長送）。常駐練功會自動簽；沒有常駐時用這個補簽。

    python -X utf8 -m x7k2qm.autoplay.jobs signin --profile 3 --discover --apply

協定與門檻見 `doc/autoplay/game/daily.md` §2；實作是 `Party.sign_in_daily()`（失敗只記日誌，不重試）。
"""

from typing import Tuple

NAME = "signin"
HELP = "每日簽到（帳號一天一次；不加 --apply 不送）"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731


def add_args(ap) -> None:
    pass


def run(p, args) -> Tuple[str, dict]:
    if not getattr(args, "apply", False):
        return "只列（沒加 --apply，沒有簽到）", {"signed": False}
    ok = p.sign_in_daily()
    return ("簽到成功" if ok else "沒有簽到（今天已簽過、等級未到或失敗，看日誌）"), {"signed": ok}
