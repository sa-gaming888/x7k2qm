"""loyalty：把身上指定寵物的忠誠用石幣加到目標（預設 100）。使用者 2026-11-12「做」（先加一隻看騎不騎得上）。

    python -X utf8 -m x7k2qm.autoplay.jobs loyalty --profile 2 --discover --who 角色68                      # 只列（預設看鎖定的騎寵）
    python -X utf8 -m x7k2qm.autoplay.jobs loyalty --profile 2 --discover --apply --who 角色68 --uid 1000000176912

起因（LESSONS #198）：11-12 查到鎖定的騎寵 29 隻裡 13 隻一直「休息」沒騎上，忠誠都不到 100；在騎的 16 隻忠誠都是 100。
補送 `NewPet_NewPetChangeStatus` 改成騎乘伺服器也不理。

協定 `Pet_NewPetAllegiance{petIndex, action 1 加}`，每次石幣 20000（`Config.changeZc`），11-04 實機一次 +5。
每次都要 isOK、忠誠真的變高、石幣少了、鑽石沒變才繼續，不對就停（規範 2）；一次工作最多花 --budget 石幣。
"""

from typing import Dict, List, Tuple

from . import JobError, JobHold
from .revive import names_of

# 跟 lingshi 補戰寵忠誠同一組數字；不 import lingshi，petgoal 收進公開映像時才不會把 lingshi 一起帶進去
LOYAL_COST = 20000                   # Config.changeZc.currencyValue（石幣）
LOYAL_WAIT = 8.0

NAME = "loyalty"
HELP = "寵物忠誠用石幣加到目標（--who --uid，預設鎖定的騎寵；--to 100、--budget）"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731


def add_args(ap) -> None:
    ap.add_argument("--who", default=None, help="角色（逗號分隔）；預設全隊")
    ap.add_argument("--uid", type=int, default=None, help="寵物唯一編號；不給就用那隻角色鎖定的騎寵")
    ap.add_argument("--to", type=int, default=100, help="加到多少（預設 100）")
    ap.add_argument("--budget", type=int, default=400000, help="這次工作最多花多少石幣（預設 40 萬＝0 加到 100 一隻）")


def check(args) -> None:
    if not 1 <= int(args.to) <= 100:
        raise JobError("--to 要在 1～100")
    if int(args.budget) < LOYAL_COST:
        raise JobError(f"--budget 至少 {LOYAL_COST}")


def target(p, n: str, uid) -> Tuple[int, dict]:
    """（格子, 寵物）：給了 --uid 用它，不然用鎖定的騎寵；身上沒有就停。"""
    want = uid or p.locks(n)[1]
    with p.lock:
        hit = next(((i, dict(v)) for i, v in (p.petinfo.get(n) or {}).items() if v.get("uniqueIndex") == want), None)
    if not want or hit is None:
        raise JobError(f"{n} 身上找不到寵物 {want or '（沒鎖騎寵）'}")
    return hit


def raise_to(p, n: str, idx: int, goal: int = 100, budget: int = 400000) -> Tuple[int, int]:
    """把 n 第 idx 格寵物的忠誠一次一次加到 goal，回（加了幾次, 花了多少石幣）。
    花到 budget 還不到、石幣不夠 → JobError（還沒送出的不送）；送了卻對不上、鑽石變了 → JobHold（規範 2）。
    petgoal 換騎寵騎不上時也用這個（使用者 11-12「全部補，以後自動補」）。"""
    times = spent = 0
    uid = p.petinfo[n][idx].get("uniqueIndex")
    while int(p.petinfo[n][idx].get("allegiance") or 0) < goal:
        if spent + LOYAL_COST > budget:
            raise JobError(f"{n} 寵物 {uid} 已經花 {spent} 石幣到上限 {budget}，忠誠還沒到 {goal}")
        with p.lock:
            m = dict(p.money.get(n, {}))
        stone, denar = m.get("stone"), m.get("denar")
        if stone is None or stone < LOYAL_COST:
            raise JobError(f"{n} 石幣不夠（{stone}），寵物 {uid} 忠誠沒加（已加 {times} 次）")
        before = int(p.petinfo[n][idx].get("allegiance") or 0)
        r = p.clients[n].request("Pet_NewPetAllegiance", {"petIndex": idx, "action": 1}) or {}
        ok = bool(r.get("isOK")) and p.wait(
            lambda: int(p.petinfo[n][idx].get("allegiance") or 0) > before
            and (p.money.get(n, {}).get("stone") or 0) < stone, LOYAL_WAIT)
        if not ok:
            raise JobHold(f"{n} 寵物 {uid} 加忠誠沒確認（回應 {r}、忠誠 "
                          f"{p.petinfo[n][idx].get('allegiance')}、石幣 {p.money.get(n, {}).get('stone')}），停")
        if p.money.get(n, {}).get("denar") != denar:
            raise JobHold(f"{n} 加忠誠鑽石變了（{denar} → {p.money.get(n, {}).get('denar')}），停")
        spent += stone - int(p.money.get(n, {}).get("stone") or 0)
        times += 1
    return times, spent


def run(p, args) -> Tuple[str, dict]:
    names = names_of(p, args)
    goal, budget = int(args.to), int(args.budget)
    now: Dict[str, dict] = {}
    for n in names:
        idx, v = target(p, n, getattr(args, "uid", None))
        now[n] = {"uid": v.get("uniqueIndex"), "petId": v.get("petId"), "status": v.get("status"),
                  "loyalty": int(v.get("allegiance") or 0)}
        p.log(f"忠誠：{n} 寵物 {now[n]['uid']}（{now[n]['petId']}）狀態 {now[n]['status']} 忠誠 {now[n]['loyalty']}")
    if not getattr(args, "apply", False):
        return "只列忠誠（沒加 --apply，沒有送任何動作）", {"now": now}
    spent = 0
    done: Dict[str, int] = {}
    for n in names:
        idx, v = target(p, n, getattr(args, "uid", None))
        times, cost = raise_to(p, n, idx, goal, budget - spent)
        spent += cost
        done[n] = times
        p.log(f"忠誠：{n} 寵物 {v.get('uniqueIndex')} → {p.petinfo[n][idx].get('allegiance')}（加 {times} 次）")
    after: List[str] = [f"{n} {p.petinfo[n][target(p, n, getattr(args, 'uid', None))[0]].get('allegiance')}" for n in names]
    return f"忠誠加好：{after}，共花 {spent} 石幣", {"done": done, "spent": spent}
