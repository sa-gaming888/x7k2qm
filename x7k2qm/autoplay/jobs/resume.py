"""resume：常駐練功停在原地（hold）時，人處理完後清掉 hold、回練功。

    python -X utf8 -m x7k2qm.autoplay.jobs add resume --profile 2

只能排進常駐練功的佇列（`QUEUE_ONLY`）：沒有常駐在跑就沒有 hold 可清，單獨登入一次沒有意義。
回練功（組隊 → 傳回 hold 前的練功地圖 → 5 隻快戰 → 開原地遇敵）由常駐主迴圈做完工作後處理
（`headless_party.Party.process_job`）；回不去就停（規範第 2 點）。
"""

from typing import Tuple

NAME = "resume"
HELP = "常駐練功停在原地（hold）時：清掉 hold、回練功（只能排進佇列）"
PAUSE = False
QUEUE_ONLY = True


def run(party, args) -> Tuple[str, dict]:
    was = getattr(party, "hold", None)
    party.hold = None
    if was:
        return f"清掉停在原地（{str(was.get('reason', ''))[:80]}）→ 回練功", {"was": was}
    return "沒有停在原地，不用處理", {}
