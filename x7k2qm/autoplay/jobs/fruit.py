"""fruit：把倉庫裡的經驗果取出來吃掉（每日簽到拿到的 2 倍智慧果等）。

    python -X utf8 -m x7k2qm.autoplay.jobs fruit --profile 3 --discover            # 只列每隻背包、倉庫有幾顆
    python -X utf8 -m x7k2qm.autoplay.jobs add fruit --profile 3 --apply           # 取出來吃掉

為什麼要有這個工作：經驗果的加成算**整個帳號**（使用者 2026-10-29），所以拿到就該吃，放著只是佔格子。
開局基礎規範（`init_baseline`）只在**登入那次**取倉庫的果子、每隻吃一顆；之後每天 00:00 簽到拿到的
（活動表 type 7 的 30 天輪，大半天數給 2 倍智慧果 90398）就沒人處理了，要靠這個工作補。

做法：`Party.take_exp_fruits()` 把個倉／共倉第 1 頁的經驗道具整疊取出 → 每隻一顆一顆吃
（`Item_ItemUse` 沒有 response，看 `Item_ItemInfo` 數量變少才算數），吃不動就停，不重送。
吃東西不能在戰鬥中，所以 `PAUSE`＝要先關遇敵等這場打完。
"""

from typing import Dict, Tuple

from . import JobError
from ..headless_party import EXP_FRUITS

NAME = "fruit"
HELP = "把倉庫的經驗果取出來吃掉（簽到拿到的 2 倍智慧果；加成算整個帳號）"
PAUSE = True
MAX_EAT = 20                     # 一次最多吃幾顆（保護：避免無限迴圈）


def add_args(ap) -> None:
    ap.add_argument("--who", default=None, help="只做這些角色（逗號分隔）；預設全隊")


def names_for(p, args):
    names = [n for _, n in p.party]
    who = getattr(args, "who", None)
    if not who:
        return names
    pick = {x.strip() for x in str(who).replace("，", ",").split(",") if x.strip()}
    bad = pick - set(names)
    if bad:
        raise JobError(f"--who 有不在這組的角色：{sorted(bad)}")
    return [n for n in names if n in pick]


def in_bag(p, name: str) -> int:
    """背包裡的經驗果總數。"""
    return sum(it.get("overlay", 1) for it in p.bag(name) if it.get("itemId") in EXP_FRUITS)


def exp_until(p, name: str) -> Dict[str, int]:
    """目前每種經驗加成 buff 的到期時間（名稱 → unix 秒）。

    要分種類比：只看「最晚的那個」的話，已經有 2倍智慧果到 11-06 時吃 3倍聰明的豆子（到當天 16:22），
    最晚時間不變，會被當成沒生效（2026-11-03 第六組實測：豆子其實有效，程式判沒變長）。"""
    out: Dict[str, int] = {}
    for b in p.exp_buffs(name):
        k = b.get("name") or ""
        out[k] = max(out.get(k, 0), int(b.get("until") or 0))
    return out


def longer(before: Dict[str, int], after: Dict[str, int]) -> bool:
    """有任何一種加成是新出現的、或到期時間變長。"""
    return any(t > before.get(k, -1) for k, t in after.items())


def eat_all(p, name: str) -> int:
    """背包裡的經驗果一顆一顆吃；**每吃一顆要看到加成時間真的變長才繼續**。

    同一種果子在效果還在時再吃會不會延長時間，官方沒說、我們也沒驗過（wiki 只寫「多種經驗果可疊加」）；
    萬一不會延長，一次把簽到累積的果子全吃掉就是白白浪費（primaljason-home 2026-10-29 審出）。
    所以：加成時間沒變長就停在那裡、回報，剩下的留著下次吃。吃不動（數量沒變少）一樣停、不重送。
    """
    eaten = 0
    while in_bag(p, name) > 0:
        if eaten >= MAX_EAT:
            raise JobError(f"{name} 已經吃了 {MAX_EAT} 顆還有剩，先停下來看看（上限保護）")
        before = exp_until(p, name)
        if not p.eat_exp_fruit(name):
            raise JobError(f"{name} 吃經驗果沒看到數量減少，停（背包還有 {in_bag(p, name)} 顆）")
        eaten += 1
        p.wait(lambda: longer(before, exp_until(p, name)), 5)
        after = exp_until(p, name)
        if not longer(before, after):
            p.log(f"{name} 吃了第 {eaten} 顆，但經驗加成到期時間沒變長（{before}→{after}）："
                  f"背包還有 {in_bag(p, name)} 顆，先不吃了")
            break
        changed = {k: (before.get(k, 0), t) for k, t in after.items() if t > before.get(k, -1)}
        p.log(f"{name} 經驗加成到期變長：" + "、".join(f"{k} {a}→{b}" for k, (a, b) in changed.items()))
    return eaten


def run(p, args) -> Tuple[str, dict]:
    names = names_for(p, args)
    if not getattr(args, "apply", False):
        out = {n: in_bag(p, n) for n in names}
        for n in names:
            p.log(f"{n}：背包 {out[n]} 顆經驗果（倉庫要 --apply 才會查、取出）")
        return "只列背包的經驗果（沒加 --apply，沒有送任何動作）", {"bag": out}
    took = p.take_exp_fruits(names)
    eaten: Dict[str, int] = {}
    for n in names:
        eaten[n] = eat_all(p, n)
    total = sum(eaten.values())
    p.log(f"經驗果：從倉庫取出 {took} 疊，吃掉 {total} 顆 {eaten}")
    return f"吃掉 {total} 顆經驗果（倉庫取出 {took} 疊）：{eaten}", {"took": took, "eaten": eaten}
