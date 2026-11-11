"""train：依序練寵（一隻練到 5 隻角色都到級，再換下一隻）。

    python -X utf8 -m x7k2qm.autoplay.jobs train --profile 2 --discover --pets 49:18,307:30
    ... --pets 49:18,307:30 --start 海主人隧道Lv30          # 起點（預設 海主人隧道Lv30）

每一隻（`--pets` 的 petId:等級，照順序）：
1. `Party.train_pet = (petId, 等級)`、`pet_start = --start` → 走 `headless_party` 原本的練寵模式：
   `start_pet_training()` 把 5 隻的戰寵換成要練的那隻（`orig_fight` 記住原本的戰寵／騎寵；已經到級的不換），
   換圖照 `pet_route()`（看寵物等級、只往前不退），每隻到級 `check_pet_training()` 就把戰寵改回原本的。
2. 主迴圈 `Party.train_loop(..., until=...)`：5 隻都到級（`orig_fight` 清空，日誌「5 隻的寵物 X 都到 N 級以上了」）
   → 關遇敵、回來，換下一隻。
3. 練功自己停了（停止檔、補血失敗、連線中斷、例外…）→ **整串停**，後面的不練（規範第 2 點）。
   不管成敗，最後還沒改回的戰寵都改回原本的（`orig_fight` 剩下的）。

來源：2026-10-24 第二組實機用的 `train_seq.sh`（每隻開一個 headless_party 子行程、看日誌放停止檔）；
本檔改成在同一個 Party 上直接驅動，不再開子行程、也不用重登。
"""

import time
from typing import Callable, Dict, List, Tuple

from . import JobError

NAME = "train"
HELP = "依序練寵（--pets petId:等級,...；5 隻都到級才換下一隻，練功停了就整串停）"
PAUSE = True
START = "海主人隧道Lv30"
INTERVAL = 15.0                 # 主迴圈每輪間隔（同 headless_party.run）
STALL = 21600.0                              # 超過 6 小時沒有任何一隻升級才算卡住（11-02 第三組 125→126 就要 1 小時 55 分，2 小時誤判；120 級以上沒經驗加成一級可能超過 40 分鐘，11-02 兩次誤判）


def add_args(ap) -> None:
    ap.add_argument("--pets", required=True, help="要練的寵物，petId:等級 逗號分隔、照順序（例：49:18,307:30）")
    ap.add_argument("--start", default=START, help=f"每隻都從這個練級點開始（預設 {START}；pet_route 只往前）")
    ap.add_argument("--stop-file", default=None, help="停止檔（預設 <--log>.stop）；放了就正常結束、後面不練")


def plan_of(args) -> List[Tuple[int, int]]:
    out = []
    for part in str(getattr(args, "pets", "") or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        pid, sep, lv = part.partition(":")
        try:
            if not sep:
                raise ValueError(part)
            ids = tuple(int(x) for x in pid.split("+"))      # 55+57：五隻各練不同種時，每隻找自己身上那隻
            out.append((ids if len(ids) > 1 else ids[0], int(lv)))
        except ValueError:
            raise JobError(f"--pets 格式錯誤：{part}（要 petId:等級，例 49:18；不同種用 + 連，例 55+57:140）")
    if not out:
        raise JobError("--pets 沒有指定任何寵物")
    bad = [f"{pid}:{lv}" for pid, lv in out
           if min(pid if isinstance(pid, tuple) else (pid,)) <= 0 or not 1 < lv <= 140]
    if bad:
        raise JobError(f"--pets petId 要大於 0、等級要在 2～140：{bad}")
    return out


def check(args) -> None:
    plan_of(args)
    from ..supervisor import ROUTE
    start = getattr(args, "start", None) or START
    if start not in {n for n, _, _ in ROUTE}:          # 路線外的 SPOTS 不行：練寵要照路線往前換圖
        raise JobError(f"--start 不是練功路線上的地點：{start}")


def restore(p) -> List[str]:
    """還沒改回的戰寵改回原本的（先關遇敵等這場打完）。回傳改不回去的角色。"""
    left = list(p.orig_fight.items())
    if not left:
        return []
    bad = []
    if not p.pause():
        p.log("還原戰寵：關不掉遇敵，照樣送設定")
    for name, (fight, ride) in left:
        if p.set_fight_pet(name, fight, ride):
            del p.orig_fight[name]
        else:
            bad.append(name)
    return bad


def levels_of(p, pid: int) -> Dict[str, tuple]:
    """每隻角色身上那種 petId 所有隻的等級（由低到高；沒有就空）。
    同種多隻要全看：11-02 第六組練新抓的虎 307，身上另有 128 級騎寵貝魯卡，只看最高的永遠不變。"""
    from ..headless_party import species_of
    out = {}
    with p.lock:
        for _, name in p.party:
            ids = species_of(pid, name)
            out[name] = tuple(sorted(v.get("level", 0) for v in p.petinfo.get(name, {}).values()
                                     if v.get("petId") in ids))
    return out


def stall_guard(p, pid: int, clock: Callable[[], float] = time.time):
    """`train_loop` 的 until：練完就回 True；**超過 STALL 秒沒有任何一隻升級就丟 JobError**。

    2026-10-29 第四組連兩次卡死：要練的寵物沒真的上場（別隻佔著出戰），鎖定卻回報成功，
    舊的 `until=lambda: not p.orig_fight` 沒有任何逾時 → 無限等一隻永遠不長經驗的寵物，
    常駐佇列被佔住、後面什麼都排不進去。規範第 2 點：卡住就要停，不要空等。
    """
    state = {"t": clock(), "lv": levels_of(p, pid)}

    def until() -> bool:
        if not p.orig_fight:
            return True
        now = levels_of(p, pid)
        if now != state["lv"]:
            state.update(t=clock(), lv=now)
            return False
        if clock() - state["t"] > STALL:
            raise JobError(f"練 {pid} 卡住：{int(STALL)} 秒沒有任何一隻升級（多半是牠沒真的上場，"
                           f"別隻佔著出戰）；目前等級 {now}")
        return False
    return until


def run(p, args) -> Tuple[str, dict]:
    plan = plan_of(args)
    start = getattr(args, "start", None) or START
    old = (p.train_pet, p.pet_start, p.no_retreat, p.stop_file)
    if getattr(args, "stop_file", None):
        p.stop_file = args.stop_file
    elif p.stop_file is None and getattr(args, "log", None):
        p.stop_file = args.log + ".stop"
    p.pet_start = start
    p.no_retreat = True                     # 練寵只往前（train_seq.sh 帶 --no-retreat）
    done: List[int] = []
    try:
        for pid, lv in plan:
            p.train_pet = (pid, lv)
            p.orig_fight.clear()
            p.log(f"練寵：開始練 {pid} 到 {lv} 級（{start}）")
            code = p.train_loop(start, INTERVAL, until=stall_guard(p, pid))
            if code != 0 or p.orig_fight:
                why = "收到停止檔" if code == 0 else f"練功停止（結束碼 {code}）"
                raise JobError(f"練 {pid} 到 {lv} 級沒完成（{why}），後面 {plan[len(done) + 1:]} 不練；"
                               f"已練完 {done}")
            done.append(pid)
            p.log(f"練寵：{pid} 5 隻都到 {lv} 級")
    finally:
        bad = restore(p)
        if bad:
            p.log(f"還原戰寵：{bad} 改不回去（要手動設）")
        p.train_pet, p.pet_start, p.no_retreat, p.stop_file = old
    if bad:
        raise JobError(f"練寵完成，但 {bad} 的戰寵改不回去（要手動設）")
    return f"練寵完成：{plan}", {"done": done}
