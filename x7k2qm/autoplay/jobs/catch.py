"""catch：用外掛自動抓寵，五隻角色同時抓（每隻角色一條執行緒、各自的連線）。

    python -X utf8 -m x7k2qm.autoplay.jobs catch --profile 3 --discover --pets 49:1,307:1
    ... --pets 319:1,320:1,321:1,322:1 --allow-full     # 卡坦要一次帶 4 隻，允許寵物欄暫時 5/5

每隻角色的流程：誘餌夠不夠（不夠從個人倉庫取）→ 離隊 → 外掛戰寵清空＋戰寵休息 →
`WaiGua_CatchPetSetting{catchPetSettingFlg:true, petId}` → 等抓到指定隻數 → 關掉 → 戰寵設回。

為什麼戰寵一定要先休息：不休息的話 1 級的目標會被戰寵秒殺，誘餌也不會扣
（primaljason-home 2026-10-23 第二組實機）。

規範第 2 點（出錯就停）在這裡的範圍是**每隻角色各自**：某一隻誘餌不夠、開不起來、寵物欄會滿，
就只停那一隻並記錄，其他四隻照抓完——五隻是獨立的連線，一隻的問題不該讓其他四隻半途而廢。
抓不到目標數量不重試、不換圖，時間到就收工回報實際抓到幾隻。

來源：primaljason-home 2026-10-24 交來的 `wg_catch_all.py`（第二組實機跑過），
本檔把寫死的 profile 改成參數、加上 job 介面與測試。
"""

import threading
from typing import Dict, List, Optional, Tuple

from . import JobError

NAME = "catch"
HELP = "用外掛自動抓寵（--pets petId:隻數,...；五隻角色同時抓）"
PAUSE = True

BAIT = 90402                     # 寵物誘餌：一隻用 1 個
CATCH_TIMEOUT = 240.0            # 單一 petId 最多等幾秒
POLL = 0.5                       # 每隔幾秒看一次抓到沒
SLOT_MAX, SLOT_MAX_FULL = 4, 5   # 預設抓完要留 1 格；--allow-full 允許暫時滿格
DRAGON = 1589                    # 人龍：上次中斷沒設回戰寵時的備援


def add_args(ap) -> None:
    ap.add_argument("--pets", required=True, help="要抓什麼，petId:隻數 逗號分隔（例：49:1,307:1）")
    ap.add_argument("--allow-full", action="store_true",
                    help="允許抓完寵物欄 5/5（卡坦 2019 要同時帶 4 隻；使用者 2026-10-23 同意）")
    ap.add_argument("--buy-bait", action="store_true",
                    help="誘餌不夠時用石幣補買缺的量（抓寵「+」，每個 50,000 石幣、只花石幣；使用者 2026-10-25 同意）")
    ap.add_argument("--who", default=None,
                    help="只幫這些角色抓（逗號分隔名字；預設全隊）。補抓用：2026-10-25 角色99邦恩吉 240 秒沒遇到")


def plan_of(args) -> List[Tuple[int, int]]:
    out = []
    for part in str(getattr(args, "pets", "") or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        pid, _, num = part.partition(":")
        try:
            out.append((int(pid), int(num or 1)))
        except ValueError:
            raise JobError(f"--pets 格式錯誤：{part}（要 petId:隻數，例 49:1）")
    if not out:
        raise JobError("--pets 沒有指定任何寵物")
    bad = [f"{pid}:{n}" for pid, n in out if n <= 0]
    if bad:
        raise JobError(f"--pets 隻數要大於 0：{bad}")
    return out


def check(args) -> None:
    plan_of(args)


def _pets(p, name: str) -> Dict[int, dict]:
    with p.lock:
        return {k: dict(v) for k, v in p.petinfo.get(name, {}).items() if (v.get("petId") or 0) > 0}


def _count(p, name: str, pid: int) -> int:
    return sum(1 for v in _pets(p, name).values() if v.get("petId") == pid)


def _bait(p, name: str) -> int:
    return sum(i.get("overlay", 1) for i in p.bag(name) if i["itemId"] == BAIT)


def _setting(p, name: str, on: bool, pid: int) -> dict:
    return p.clients[name].request("WaiGua_CatchPetSetting",
                                   {"catchPetSettingFlg": on, "petId": pid, "hp": 0, "gj": 0, "fy": 0, "mj": 0,
                                    "autoSaveDepot": False, "autoDelete": False}) or {}


def take_bait(p, name: str, need: int) -> bool:
    """誘餌不夠就從個人倉庫整疊取出來；還不夠再從公共倉庫只取缺的量（全帳號共用，不整疊拿走，
    使用者 2026-11-03：第六組共倉有 90 個誘餌、個倉沒有，劇本卡在恐龍博士抓寵）。取完還是不夠就回 False（這隻停手）。"""
    if _bait(p, name) >= need:
        return True
    c = p.clients[name]
    for public in (False, True):
        dep = c.request("ItemHandle_GetNewItemDepotInfo", {"page": 1, "isPublic": public}) or {}
        rows = dep.get("itemInfoList") or []
        rows = list(rows.values()) if isinstance(rows, dict) else rows
        it = next((i for i in rows if i.get("itemId") == BAIT), None)
        if it is not None:
            have = it.get("overlay", 1)
            n = min(have, need - _bait(p, name)) if public else have
            c.request("ItemHandle_GetItemToNewDepot",
                      {"page": 1, "isPublic": public, "uniqueIndex": it["uniqueIndex"], "count": n})
            p.nap(1)
        if _bait(p, name) >= need:
            return True
    return False


BUY_PRICE = 50000                  # OneKeyBuy 表 90402 = {stone 50000}（送前再核對一次）
BUY_MAX = 5                        # 一隻角色一次最多買幾個（上限保護）


def onekey_price() -> Optional[int]:
    """OneKeyBuy 表裡寵物誘餌的價格：只接受「只有石幣一種」；其他幣種或讀不到回 None（不買）。"""
    import json
    import os
    from ...features.gamedb.tables import CONFIGS
    try:
        with open(os.path.join(CONFIGS, "OneKeyBuy.json"), encoding="utf-8") as fp:
            row = next((r for r in json.load(fp) if r.get("ID") == BAIT), None)
    except Exception:  # noqa: BLE001
        return None
    info = (row or {}).get("Info") or []
    if len(info) != 1 or set(info[0]) != {"stone"}:
        return None
    return int(info[0]["stone"])


def buy_bait(p, name: str, n: int) -> None:
    """抓寵畫面的「+」：`Item_oneKeyBuyItem{itemId 90402, itemNum 1}`，一次 1 個（參考專案 A 級錄檔）。
    **只花石幣**（使用者 2026-10-25「買」：角色99誘餌少 1 個）。送前核對 OneKeyBuy 表＝石幣 50,000、石幣夠；
    每買 1 個要看到 誘餌 +1、石幣正好 −50,000、鑽石（denar）不變，任何一項對不上就丟 JobHold（花了錢、停在原地等人看）。"""
    from . import JobHold
    price = onekey_price()
    if price != BUY_PRICE:
        raise JobError(f"OneKeyBuy 表寵物誘餌價格是 {price}，不是石幣 {BUY_PRICE:,}（設定表變了，不買）")
    if n > BUY_MAX:
        raise JobError(f"{name} 要買 {n} 個誘餌，超過一次上限 {BUY_MAX}")
    for _ in range(n):
        with p.lock:
            money = dict(p.money.get(name, {}))
        s0, d0, c0 = money.get("stone"), money.get("denar"), _bait(p, name)
        if s0 is None or s0 < price:
            raise JobError(f"{name} 石幣 {s0} 不夠 {price:,}，不買")
        r = p.clients[name].request("Item_oneKeyBuyItem", {"itemId": BAIT, "itemNum": 1}) or {}
        # **石幣是帳號共用**：五隻同時買，每隻看到的扣款是大家加起來的（2026-10-27 第五組：
        # 以為只扣 50,000，實際看到 −200,000），所以只要求「有扣、至少一份、鑽石沒變」。
        p.wait(lambda: _bait_raw(p, name) >= c0 + 1 and (p.money.get(name, {}).get("stone") or 0) <= s0 - price, 6)
        with p.lock:
            s1, d1 = p.money.get(name, {}).get("stone"), p.money.get(name, {}).get("denar")
        c1 = _bait(p, name)
        if c1 == c0 and s1 == s0:
            raise JobError(f"{name} 買誘餌被拒（回應 {r}），沒花錢")
        if c1 != c0 + 1 or s1 is None or s1 > s0 - price or d1 != d0:
            raise JobHold(f"{name} 買誘餌後對不上：誘餌 {c0}→{c1}（要 +1）、石幣 {s0}→{s1}（至少要 −{price:,}；帳號共用，別人同時買會扣更多）、"
                          f"鑽石 {d0}→{d1}（不能變），停在原地")
        p.log(f"{name} 用石幣買 1 個寵物誘餌：誘餌 {c0}→{c1}、石幣 {s0:,}→{s1:,}")


def _bait_raw(p, name: str) -> int:
    """持鎖時也能呼叫的誘餌數（p.wait 的條件不能呼叫會上鎖的方法，LESSONS #29）。"""
    return sum(i.get("overlay", 1) for i in p.items.get(name, {}).values()
               if i.get("itemId") == BAIT and i.get("overlay", 1) > 0 and i.get("itemStorge", 0) == 0)


def rest_fight_pet(p, name: str) -> bool:
    """外掛戰寵清空 ＋ 出戰中的寵物改休息。沒休息成功就回 False（不抓，免得目標被秒殺）。"""
    c = p.clients[name]
    fight, ride = p.locks(name)
    c.request("WaiGua_NomalSettingHandlePet", {"fightPetIndex": 0, "ridePetIndex": ride})
    p.nap(1.5)
    slot = next((k for k, v in _pets(p, name).items() if v.get("status") == 2), None)
    if slot is not None:
        # NewPet_NewPetChangeStatus 內層不帶 session（沒有 response）→ 只送，看 Pet_PetInfo
        c.send("NewPet_NewPetChangeStatus", {"petIndex": slot, "status": 1})
        p.wait(lambda: p.petinfo.get(name, {}).get(slot, {}).get("status") != 2, 5)
    return not any(v.get("status") == 2 for v in _pets(p, name).values())


def restore_fight_pet(p, name: str, fight: int, ride: int) -> None:
    if not fight:
        # 抓寵前戰寵鎖定就是 0（卡坦交寵物前全部休息、解鎖）→ 用身上等級最高、不是金虎的那隻（剛抓的都是 1 級）。
        # 舊版寫死人龍，三轉後人龍在倉庫 → 戰寵一直沒設回、接著練寵「戰 0」停（2026-10-25 #88）
        cands = [v for v in _pets(p, name).values() if v.get("petId") != 310 and v.get("uniqueIndex")]
        fight = max(cands, key=lambda v: v.get("level") or 0).get("uniqueIndex") if cands else 0
    if fight:
        p.nap(2)
        p.log(f"{name} 戰寵設回 → {p.set_fight_pet(name, fight, ride)}")


def make_room(p, name: str, over: int, targets: set) -> None:
    """寵物欄放不下要抓的：休息中的寵物（不是鎖定的戰寵／騎寵、不在出戰／騎乘、不是這次要抓的物種）先存個倉，
    最多存 over 隻、等級低的先存。11-03 第六、八組、11-04 第八組恐龍博士 2016 各停一次「超過上限 4」——
    使用者指定戰寵後原本的戰寵留在欄位休息，多佔一格（LESSONS #162）。個倉滿了 store_pet 會丟 JobError，照樣停。"""
    from .pets import main_fight, store_pet
    fight_u, ride_u = p.locks(name)
    # 主戰寵也不動：卡坦交寵物前 `pets rest` 會把鎖定清成 0、全部改休息，這時只看鎖定會把主戰寵存掉。
    # 有鎖定戰寵就以鎖定的為準：main_fight 遇到鎖定的是金虎會改挑「最高的非金虎」，把休息的當主戰寵留著（11-05 #167）
    main = None if fight_u else (main_fight(p, name) or {}).get("uniqueIndex")
    rest = sorted(((k, v) for k, v in _pets(p, name).items()
                   if v.get("status") not in (2, 3) and v.get("uniqueIndex") not in (fight_u, ride_u, main, None, 0)
                   and v.get("petId") not in targets),
                  key=lambda kv: kv[1].get("level") or 0)
    for k, v in rest[:over]:
        p.log(f"{name} 寵物欄放不下要抓的，休息中的 {p.pet_name(v)}（{v.get('level')} 級）先存個倉")
        store_pet(p, name, k)


def catch_one(p, name: str, plan: List[Tuple[int, int]], slot_max: int,
              timeout: float = CATCH_TIMEOUT, buy: bool = False) -> Dict[int, int]:
    """一隻角色抓完整個清單，回傳 {petId: 實際抓到幾隻}。這隻出錯就停這隻（丟 JobError）。"""
    c = p.clients[name]
    todo = [(pid, n - _count(p, name, pid)) for pid, n in plan]
    todo = [(pid, n) for pid, n in todo if n > 0]
    need = sum(n for _, n in todo)
    if not need:
        p.log(f"{name} 已經都有，不用抓")
        return {}
    if len(_pets(p, name)) + need > slot_max:
        make_room(p, name, len(_pets(p, name)) + need - slot_max, {pid for pid, _ in plan})
    if len(_pets(p, name)) + need > slot_max:
        raise JobError(f"{name} 停止：現在 {len(_pets(p, name))} 隻、要抓 {need} 隻，超過上限 {slot_max}")
    if not take_bait(p, name, need):
        if not buy:
            raise JobError(f"{name} 停止：誘餌不夠（要 {need} 個，身上 {_bait(p, name)} 個）；"
                           f"要用石幣補買加 --buy-bait（每個 {BUY_PRICE:,} 石幣）")
        buy_bait(p, name, need - _bait(p, name))     # 只買缺的量
    c.send("Team_TeamLeave", {})
    p.nap(1.5)
    fight, ride = p.locks(name)
    if not rest_fight_pet(p, name):
        raise JobError(f"{name} 停止：戰寵休息不了（不休息目標會被秒殺）")
    got: Dict[int, int] = {}
    last = todo[-1][0]
    try:
        for pid, n in todo:
            base = _count(p, name, pid)
            if not _setting(p, name, True, pid).get("isOK"):
                raise JobError(f"{name} 開自動抓寵 {pid} 失敗")
            # 用 p.nap 計次而不是看時鐘：測試把 nap 換成不睡，就不用等真的 240 秒
            for _ in range(int(timeout / POLL)):
                if _count(p, name, pid) - base >= n:
                    break
                p.nap(POLL)
            _setting(p, name, False, pid)
            got[pid] = _count(p, name, pid) - base
            p.log(f"{name} petId {pid}：抓到 {got[pid]}／{n}，誘餌剩 {_bait(p, name)}")
            p.nap(2)
    finally:
        _setting(p, name, False, last)
        restore_fight_pet(p, name, fight, ride)
    return got


def run(p, args) -> Tuple[str, dict]:
    plan = plan_of(args)
    slot_max = SLOT_MAX_FULL if getattr(args, "allow_full", False) else SLOT_MAX
    result: Dict[str, Dict[int, int]] = {}
    errors: Dict[str, str] = {}

    def runner(name: str) -> None:
        try:
            result[name] = catch_one(p, name, plan, slot_max, buy=bool(getattr(args, "buy_bait", False)))
        except Exception as e:                       # noqa: BLE001  一隻出錯只停那一隻
            errors[name] = str(e)
            p.log(f"{name} 抓寵停止：{e}")

    names = [n for _, n in p.party]
    who = [w.strip() for w in str(getattr(args, "who", "") or "").replace("，", ",").split(",") if w.strip()]
    if who:
        bad = [w for w in who if w not in names]
        if bad:
            raise JobError(f"--who 有不在這組的角色：{bad}")
        names = [n for n in names if n in who]
    if not getattr(args, "apply", False):
        # 不加 --apply 只列、不送任何封包。以前這裡沒判斷，11-05 第二組「先列清單」卻真的抓了五隻（LESSONS #171）
        plan_rows = {}
        for n in names:
            have = {pid: _count(p, n, pid) for pid, _ in plan}
            need = sum(max(0, want - have[pid]) for pid, want in plan)
            plan_rows[n] = {"have": have, "need": need, "bait": _bait(p, n), "slots": len(_pets(p, n))}
            p.log(f"{n} 只列：身上 {plan_rows[n]['slots']} 隻、已有 {have}、要抓 {need} 隻、"
                  f"背包誘餌 {plan_rows[n]['bait']} 個（上限 {slot_max} 隻）")
        return "只列，沒有抓（加 --apply 才抓）", {"plan": plan_rows}
    threads = [threading.Thread(target=runner, args=(n,), daemon=True) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 看「身上實際有幾隻」而不是「這次抓到幾隻」：本來就有的角色 catch_one 會回 {}，
    # 以前被當成沒抓滿整個工作就失敗（2026-10-27 第五組金豬，五隻都已經有卻 failed）。
    short = {n: {pid: want - _count(p, n, pid) for pid, want in plan
                 if _count(p, n, pid) < want and n not in errors}
             for n in names}
    short = {n: v for n, v in short.items() if v}
    detail = f"抓寵完成：{result}" + (f"；出錯 {errors}" if errors else "") + (f"；沒抓滿 {short}" if short else "")
    if errors or short:
        # 2026-10-25 第二組劇本：五隻都「寵物欄超過上限」沒抓，工作卻回 ok → 劇本接著去練寵、跟著失敗。
        # 每隻照樣各自做完（一隻的錯不影響別隻），但只要有人出錯或沒抓滿，整個工作就算失敗
        err = JobError(detail)
        err.data = {"caught": result, "errors": errors, "short": short}
        raise err
    return detail, {"caught": result, "errors": errors, "short": short}
