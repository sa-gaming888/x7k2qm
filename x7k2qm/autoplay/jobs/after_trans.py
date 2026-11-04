"""after-trans：轉生後規範＋從低等圖開練（劇本最後一步；取代「轉生完重開常駐讓 init_baseline 套」）。

    python -X utf8 -m x7k2qm.autoplay.jobs after-trans --profile 2                                # 只列（不送任何封包）
    python -X utf8 -m x7k2qm.autoplay.jobs add after-trans --profile 2 --apply --start 柯奧龜洞Lv10

在已登入的 Party 上（常駐佇列裡做，不另外登入）：
1. 檢查全隊都是「1 級、轉數 ≥ 1」（轉生後規範只套轉生後的 1 級；不是就停，什麼都不送——
   寵物會被整欄重配，套錯角色會把練好的戰寵存進倉庫）。
2. 組隊 → `Party.init_after_trans(全隊)`（CLAUDE.md「轉生後規範」：禮盒、吃果、寵物存倉＋金虎騎寵＋戰寵、
   隱藏、首次動作、自訂加點）；任一項沒確認就停。
3. 傳到 `--start` 練功地圖 → 5 隻快戰並確認；打開「不允許退圖」（`no_retreat`，轉生後規範第 5 點，
   一直到常駐重啟都開著）。
4. 回傳 `resume_place`：常駐做完這件工作從這張圖開遇敵（不是轉生前那張高等圖）。
"""

from typing import List, Tuple

from . import JobError

NAME = "after-trans"
HELP = "轉生後規範（禮盒、吃果、寵物、隱藏、首次動作、加點）＋傳到 --start 開快戰；不加 --apply 只列"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731
START = "柯奧龜洞Lv10"


def add_args(ap) -> None:
    ap.add_argument("--start", default=START, help=f"練功起點（預設 {START}；之後照練功路線往前、不退圖）")


def check(args) -> None:
    from ..headless_party import NAME_TO_MAP
    start = getattr(args, "start", None) or START
    if start not in NAME_TO_MAP:
        raise JobError(f"--start 不是練功路線上的地點：{start}")


def not_reborn(p) -> List[str]:
    """不是「1 級、轉數 ≥ 1」的角色（等級或轉數不知道也算）。"""
    with p.lock:
        rows = [(n, p.s.levels.get(n), p.trans.get(n)) for _, n in p.party]
    return [f"{n}（{lv} 級 {tr} 轉）" for n, lv, tr in rows if lv != 1 or not tr or tr < 1]


def run(p, args) -> Tuple[str, dict]:
    start = getattr(args, "start", None) or START
    names = [n for _, n in p.party]
    bad = not_reborn(p)
    if not names or bad:
        raise JobError(f"轉生後規範只套「1 級、轉數 ≥ 1」的角色，這些不是：{bad or '沒有角色'}；沒有送任何東西")
    if not getattr(args, "apply", False):
        p.log(f"轉生後規範會套在：{'、'.join(names)}，之後傳到 {start}")
        return "只列（沒加 --apply，沒有送任何動作）", {"names": names, "start": start}
    if not p.form_team():
        raise JobError("組隊失敗")
    if not p.init_after_trans(names):
        raise JobError("轉生後規範有項目沒確認（看日誌），不傳送")
    if not p.teleport_team(start):
        raise JobError(f"傳送到 {start} 失敗")
    fast = p.ensure_fast()
    if fast:
        raise JobError(f"快速戰鬥沒開成 {fast}")
    p.no_retreat = True                      # 轉生後規範第 5 點：不允許退圖（使用者 2026-10-24）
    return (f"轉生後規範完成（{'、'.join(names)}），已在 {start}、快戰開好；不允許退圖",
            {"names": names, "resume_place": start})
