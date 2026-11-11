"""petsell：把練好的寵物賣給薩姆吉爾村寵物店的店長換石幣（使用者 2026-11-06「+3 以下練的120 賣給寵物店」）。

    python -X utf8 -m x7k2qm.autoplay.jobs petsell --profile 3 --discover --pet-id 54                 # 只列
    python -X utf8 -m x7k2qm.autoplay.jobs petsell --profile 3 --discover --pet-id 54 --apply --confirm-sell
    ... --min-level 120 --max-grade 2 --who 甲,乙 --max-count 20                                        # 預設值以外

找**身上＋各角色個倉**裡：petId 對、等級 ≥ `--min-level`（且 ≥ 80，店長只收 80 級以上）、檔差 ≤ `--max-grade`、
沒改過名、不是出戰／騎乘／外掛鎖定／上鎖的寵物。不加 `--apply` 只列（只查倉庫，不送任何改動）；
`--apply` 一定要再加 `--confirm-sell`（賣寵物不可逆）。

協定（參考專案 `tools/headless/ops/petsell.py`，依 2026-10-23 官方錄檔，A 級；**本專案還沒實機**）：
- `Npc_NpcInteract{npcId 2718, npcArg{scriptId 3124, petIndex N}}`：send、**沒有 response**；N＝身上寵物欄格位
  （不是 uniqueIndex），一隻一次。店長在寵物店（1003）(16,13)，要站 3 格內。
- 成功＝那格推 `Pet_PetInfo{petIndex N, petId -1}` **且** 石幣增加。等不到／不符 → `JobHold`（不重送，停在原地等人）。
- 價格（店長對話）：80-99 5W、100-119 10W、120-129 22.5W、130-139 35W、140 50W；參考專案 錄檔實際對不上，
  所以不寫死，每隻記實際石幣增量。鑽石（denar）賣前賣後要一樣，變了也 `JobHold`。

只有隊長能跟 NPC 互動（隊員送會被拒 6012）→ 全部由隊長賣：
- 隊員身上的：隊員存共倉 → 隊長從共倉領；隊員個倉的：先領到隊員身上 → 存共倉 → 隊長領。隊長個倉的直接領。
- 隊長寵物欄 5 格（鎖定的戰寵、騎寵佔格）→ 分批：領滿 → 賣 → 再領。一格都空不出來 → 先把隊長身上沒鎖定、不賣的（等級最低）暫存寵物倉，賣完領回（LESSONS #194）；連這個都沒有才停。
走法共用 `shell_exchange.walk_into_pet_shop`（傳回薩村 → 寵物店門 (68,72) → Role_RoleWarp 進 1003 → 店內走到 (16,15)）。
`jobs petgoal --sell-low` 也呼叫這裡（`sell_targets`）。
"""

import re
from typing import Dict, List, Optional, Tuple

from . import JobError, JobHold
from .pets import SLOT_MAX, rest_pet, slots, store_pet, take_pet, to_public
from ..petgrade import grade

NAME = "petsell"
HELP = "賣寵物給薩村寵物店店長換石幣（--pet-id N [--min-level 120] [--max-grade 2]；--apply 要再加 --confirm-sell，不可逆）"
PAUSE = lambda a: bool(getattr(a, "apply", False))      # noqa: E731  只列時不停練功

NPC_ID, SCRIPT_ID = 2718, 3124      # 店長、腳本 3124「賣寵物」（func petRecycle）
SHOP_MAP = 1003
NPC_POS = (16, 13)
REACH = 3                           # 站在店長 3 格內才點得到（參考專案 AGENTS 4.5）
MIN_LEVEL = 80                      # 店長只回收 80 級以上
# 寵物店內：Role_RoleWarp 落點 (15,21) 往北走到 (16,15)（離店長 2 格）。1003 沒有 data/mapgrids，
# 這條是 Windows 用遊戲地圖檔 load_grid(1003)＋A* 算的（2026-11-06，未實機）；龜殼走的 SHOP_PATH 是錄檔
KEEPER_PATH = [(15, 21), (15, 20), (15, 19), (15, 18), (15, 17), (15, 16), (16, 15)]
MAX_SELL = 20                       # 一次最多賣幾隻（上限保護）
PRICES = ((140, 500000), (130, 350000), (120, 225000), (100, 100000), (80, 50000))   # 店長對話的價格（只記日誌對照）
LOCKED = 1                          # Pet_PetInfo.petLock＝1：上鎖


def add_args(ap) -> None:
    ap.add_argument("--pet-id", dest="pet_id", type=int, required=True, help="要賣的寵物種類（petId，例：利則諾頓 54）")
    ap.add_argument("--min-level", dest="min_level", type=int, default=120, help="等級 ≥ 這個才賣（預設 120；最低 80）")
    ap.add_argument("--max-grade", dest="max_grade", type=int, default=2, help="檔差 ≤ 這個才賣（預設 2，即 +3 以下）")
    ap.add_argument("--who", default=None, help="只賣這些角色的（身上＋個倉；名字逗號分隔，預設全隊）")
    ap.add_argument("--max-count", dest="max_count", type=int, default=MAX_SELL, help=f"最多賣幾隻（預設 {MAX_SELL}）")
    ap.add_argument("--confirm-sell", dest="confirm_sell", action="store_true", help="--apply 時一定要加（賣寵物不可逆）")


def check(args) -> None:
    if int(args.min_level) < MIN_LEVEL:
        raise JobError(f"--min-level 不能低於 {MIN_LEVEL}（店長只回收 {MIN_LEVEL} 級以上）")
    if not 1 <= int(getattr(args, "max_count", MAX_SELL)) <= MAX_SELL:
        raise JobError(f"--max-count 要在 1～{MAX_SELL}")
    if getattr(args, "apply", False) and not getattr(args, "confirm_sell", False):
        raise JobError("賣寵物不可逆：--apply 要再加 --confirm-sell；沒有送任何東西")


def price_of(level: int) -> int:
    return next((v for lv, v in PRICES if level >= lv), 0)


AUTO_NAME = re.compile(r"\d+/\d+/\d+/\d+")   # 外掛抓寵自動取的名字（初始 體/腕/耐/速），不算使用者改名


def renamed(p, pet: dict) -> bool:
    """改過名＝名字有值、不是物種原名、也不是外掛抓寵自動取的「44/9/6/3」這種名字（沒改過名時伺服器不帶 name）。
    11-06 第十組外掛抓的利則諾頓都叫「44/9/6/3」，舊版當成改過名、整個 petgoal --sell-low 停下（LESSONS #172）。"""
    nm = pet.get("name")
    if not nm:
        return False
    if AUTO_NAME.fullmatch(str(nm).strip()):
        return False
    return nm != p.pet_name({"petId": pet.get("petId")})


def refuse_reason(p, name: str, pet: dict, on_body: bool) -> Optional[str]:
    """這隻不能賣的理由；可以賣回 None（不送任何東西）。"""
    if not pet.get("uniqueIndex"):
        return "沒有唯一編號"
    if (pet.get("level") or 0) < MIN_LEVEL:
        return f"{pet.get('level')} 級 < {MIN_LEVEL}（店長只收 {MIN_LEVEL} 級以上）"
    if pet.get("petLock") == LOCKED:
        return "上鎖"
    if renamed(p, pet):
        return f"改過名「{pet.get('name')}」"
    if on_body:
        if pet.get("status") in (2, 3):
            return "出戰中" if pet.get("status") == 2 else "騎乘中"
        if pet.get("uniqueIndex") in p.locks(name):
            return "外掛鎖定的戰寵／騎寵"
    return None


def candidates(p, names: List[str], args) -> Tuple[List[dict], List[str]]:
    """只讀：身上＋個倉裡符合條件的 → ([{who, where, uid, level, diff, pet}], [排除的說明])。"""
    ok: List[dict] = []
    skip: List[str] = []

    def add(who: str, where: str, pet: dict) -> None:
        if pet.get("petId") != args.pet_id or (pet.get("level") or 0) < args.min_level:
            return
        _, diff = grade(pet)
        tag = f"{who} {where} {p.pet_name(pet)} {pet.get('level')} 級（{pet.get('uniqueIndex')}）"
        if diff is None:
            skip.append(f"{tag} 檔次算不出來")
            return
        if diff > args.max_grade:
            skip.append(f"{tag} 檔差 {diff:+d} > {args.max_grade:+d}")
            return
        why = refuse_reason(p, who, pet, where == "身上")
        if why:
            skip.append(f"{tag} {why}")
            return
        ok.append({"who": who, "where": where, "uid": pet["uniqueIndex"], "level": pet.get("level"),
                   "diff": diff, "pet": pet})
    for n in names:
        for v in slots(p, n).values():
            add(n, "身上", v)
        for r in p.pet_depot(n):
            add(n, "個倉", r)
    return ok, skip


# ---- 移給隊長 ----
def _body_idx(p, name: str, uid: int) -> Optional[int]:
    return next((k for k, v in slots(p, name).items() if v.get("uniqueIndex") == uid), None)


def bring_to_leader(p, c: dict) -> None:
    """把一隻要賣的寵物移到隊長身上（隊長身上的不動）。每步都等封包確認，沒確認丟 JobError。"""
    lead = p.party[0][1]
    who, uid = c["who"], c["uid"]
    if who == lead and c["where"] == "身上":
        return
    if who == lead:                                         # 隊長個倉：直接領
        take_pet(p, lead, c["pet"])
        return
    if c["where"] == "個倉":                                # 隊員個倉：先領到隊員身上
        if len(slots(p, who)) >= SLOT_MAX:
            raise JobError(f"{who} 寵物欄滿了，個倉的 {uid} 領不出來（要先空一格）")
        take_pet(p, who, c["pet"])
    if _body_idx(p, who, uid) is None:
        raise JobError(f"{who} 身上找不到要賣的寵物 {uid}，停")
    if len(p.pet_depot(lead, public=True)) >= _depot_num():
        raise JobError(f"共倉第 1 頁滿了，{who} 的寵物 {uid} 移不到隊長身上（隊員不能自己賣）")
    to_public(p, who, uid)
    row = next((r for r in p.pet_depot(lead, public=True) if r.get("uniqueIndex") == uid), None)
    if row is None:
        raise JobError(f"{who} 的寵物 {uid} 存共倉後隊長在共倉找不到它，停")
    take_pet(p, lead, row)
    p.log(f"賣寵物：{who} 的寵物 {uid} 經共倉移給隊長 {lead}")


def _depot_num() -> int:
    from ..headless_party import PET_DEPOT_NUM
    return PET_DEPOT_NUM


# ---- 走到店長旁 ----
def near(a: Tuple[int, int], b: Tuple[int, int]) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def in_reach(p) -> bool:
    with p.lock:
        here, pos = p.s.leader_map, p.leader_pos
    return here == SHOP_MAP and bool(pos) and pos[0] == SHOP_MAP and near((pos[1], pos[2]), NPC_POS) <= REACH


def go_to_keeper(p) -> None:
    """隊長（隊員跟著）走到店長 3 格內。已經在就不動；在寵物店裡但不在旁邊 → 停（不亂走）。"""
    from ..shell_exchange import walk_into_pet_shop
    if in_reach(p):
        return
    with p.lock:
        here, pos = p.s.leader_map, p.leader_pos
    if here == SHOP_MAP:
        # 上次中斷時已經走進店裡、停在進店路線上（11-06 第二組停在落點 (15,20)，LESSONS #176）：從那一格接著走完
        at = (pos[1], pos[2]) if pos and pos[0] == SHOP_MAP else None
        if at not in KEEPER_PATH:
            raise JobError(f"隊長在寵物店裡但不在店長 {REACH} 格內（{pos}），也不在進店路線上，不知道怎麼走，停")
        p.walk(SHOP_MAP, KEEPER_PATH[KEEPER_PATH.index(at):])
        if not in_reach(p):
            raise JobError(f"走完還不在店長 {REACH} 格內（{p.leader_pos}），不賣")
        return
    if not walk_into_pet_shop(p, KEEPER_PATH, "賣寵物"):
        raise JobError("走不到寵物店店長旁（傳送／進店失敗，見日誌）")
    if not in_reach(p):
        raise JobError(f"走完還不在店長 {REACH} 格內（{p.leader_pos}），不賣")


# ---- 賣 ----
def sell_one(p, pet: dict) -> dict:
    """隊長賣身上第 pet['petIndex'] 格那隻。送前再核：那格仍是同一隻（uniqueIndex）、資格、在店長旁、知道石幣。
    送後：該格 petId -1 **且** 石幣增加、鑽石不變；不符 → JobHold（不重送）。回傳 {uid, level, petIndex, gain}。"""
    lead = p.party[0][1]
    idx, uid = pet["petIndex"], pet["uniqueIndex"]
    cur = slots(p, lead).get(idx)
    if not cur or cur.get("uniqueIndex") != uid:
        raise JobError(f"{lead} 第 {idx} 格現在是 {cur and cur.get('uniqueIndex')}，不是要賣的 {uid}（寵物欄換格了？），不賣")
    why = refuse_reason(p, lead, cur, True)
    if why:
        raise JobError(f"{lead} 第 {idx} 格寵物 {uid} 送前複查：{why}，不賣")
    if not in_reach(p):
        raise JobError(f"{lead} 不在店長 {REACH} 格內（{p.leader_pos}），不賣")
    with p.lock:
        money = dict(p.money.get(lead, {}))
    before, denar = money.get("stone"), money.get("denar")
    if not isinstance(before, int):
        raise JobError(f"{lead} 沒收到石幣推送（不知道賣前有多少），不賣")
    lv = cur.get("level") or 0
    p.clients[lead].send("Npc_NpcInteract", {"npcId": NPC_ID, "npcArg": {"scriptId": SCRIPT_ID, "petIndex": idx}})
    gone = p.wait(lambda: (p.petinfo[lead].get(idx, {}).get("petId") or -1) <= 0, 5)
    rose = p.wait(lambda: (p.money.get(lead, {}).get("stone") or 0) > before, 5)
    with p.lock:
        after = dict(p.money.get(lead, {}))
    if not (gone and rose):
        raise JobHold(f"{lead} 賣第 {idx} 格寵物 {uid}（{lv} 級）送出後推送不符（那格消失 {gone}、石幣增加 {rose}、"
                      f"石幣 {before} → {after.get('stone')}）；不重送，停在原地等人確認")
    if after.get("denar") != denar:
        raise JobHold(f"{lead} 賣寵物後鑽石變了（{denar} → {after.get('denar')}），停在原地等人確認")
    gain = after["stone"] - before
    p.log(f"賣寵物：{lead} 賣第 {idx} 格 {p.pet_name(cur)}（{lv} 級，{uid}）石幣 +{gain}"
          f"（店長說 {price_of(lv)}）")
    return {"uid": uid, "level": lv, "petIndex": idx, "gain": gain}


def park_leader_pet(p, lead: str, keep: set) -> Optional[dict]:
    """隊長寵物欄滿、沒有要賣的在身上（11-11 第三組：戰寵、騎寵＋輪流練的兩隻老虎＋一隻人龍佔滿 5 格）：
    挑一隻沒出戰／騎乘、沒被外掛鎖定、不在這次要賣清單的，暫存寵物倉（個倉優先，store_pet 自己挑），賣完再領回。沒得挑回 None。"""
    with p.lock:
        wg = dict(p.wg.get(lead, {}))
    locked = {wg.get("fightPetIndex"), wg.get("ridePetIndex")} - {None, 0}
    rows = [(k, v) for k, v in slots(p, lead).items()
            if v.get("status") not in (2, 3) and v.get("uniqueIndex") not in locked and v.get("uniqueIndex") not in keep]
    if not rows:
        return None
    idx, pet = min(rows, key=lambda kv: kv[1].get("level") or 0)
    public = p.pet_depot_target(lead)
    store_pet(p, lead, idx)
    where = "共倉" if public else "個倉"
    p.log(f"賣寵物：隊長 {lead} 寵物欄滿，{p.pet_name(pet)}（{pet.get('level')} 級，{pet.get('uniqueIndex')}）先暫存{where}，賣完領回")
    return {"uid": pet.get("uniqueIndex"), "public": bool(public), "where": where}


def _parked_note(parked: List[dict]) -> str:
    return "" if not parked else "；隊長暫存在倉庫還沒領回：" + "、".join(f"{r['uid']}（{r['where']}）" for r in parked)


def sell_all(p, cands: List[dict]) -> List[dict]:
    """走到店長旁 → 分批（隊長寵物欄空格有限）：移給隊長 → 賣 → 再移下一批。任何一隻失敗就停，例外訊息帶已賣的。"""
    lead = p.party[0][1]
    sold: List[dict] = []
    if not cands:
        return sold
    if len(cands) > MAX_SELL:
        raise JobError(f"一次要賣 {len(cands)} 隻超過上限 {MAX_SELL}，不賣")
    parked: List[dict] = []
    try:
        go_to_keeper(p)
        pending = list(cands)
        while pending:
            batch = [c for c in pending if c["who"] == lead and c["where"] == "身上"]
            free = SLOT_MAX - len(slots(p, lead))
            for c in [c for c in pending if c not in batch]:
                if free <= 0:
                    break
                bring_to_leader(p, c)
                batch.append(c)
                free -= 1
            if not batch:
                got = park_leader_pet(p, lead, {c["uid"] for c in pending})     # LESSONS #194
                if got is None:
                    raise JobError(f"隊長 {lead} 寵物欄沒有空格（鎖定的戰寵、騎寵佔格，也沒有能暫存的），移不過來，停")
                parked.append(got)
                continue
            for c in batch:
                idx = _body_idx(p, lead, c["uid"])
                if idx is None:
                    raise JobError(f"隊長 {lead} 身上找不到要賣的寵物 {c['uid']}，停")
                sold.append(sell_one(p, dict(slots(p, lead)[idx], petIndex=idx)))
                pending.remove(c)
        for row in parked:                                  # 賣完領回暫存的
            back = next((r for r in p.pet_depot(lead, public=row["public"]) if r.get("uniqueIndex") == row["uid"]), None)
            if back is None:
                raise JobError(f"隊長 {lead} 暫存的寵物 {row['uid']} 在{row['where']}找不到，沒領回")
            take_pet(p, lead, back)
    except JobHold as e:
        raise JobHold(f"{e}（已賣：{_summary(sold)}{_parked_note(parked)}）") from None
    except JobError as e:
        raise JobError(f"{e}（已賣：{_summary(sold)}{_parked_note(parked)}）") from None
    p.log(f"賣寵物：共 {len(sold)} 隻、石幣 +{sum(x['gain'] for x in sold)}")
    return sold


def sell_targets(p, targets: List[Tuple[str, int]]) -> List[dict]:
    """給 petgoal --sell-low：指定 (角色, 唯一編號) 的身上寵物賣掉。不能賣的（鎖定、改名…）丟 JobError、一隻都不送。"""
    cands = []
    for who, uid in targets:
        idx = _body_idx(p, who, uid)
        if idx is None:
            raise JobError(f"{who} 身上沒有要賣的寵物 {uid}，不賣")
        if uid not in p.locks(who):                         # 剛練完、戰寵已改回：還是出戰狀態就先改休息
            rest_pet(p, who, idx)
        pet = slots(p, who)[idx]
        why = refuse_reason(p, who, pet, True)
        if why:
            raise JobError(f"{who} 的寵物 {uid}（{pet.get('level')} 級）{why}，不賣")
        cands.append({"who": who, "where": "身上", "uid": uid, "level": pet.get("level"), "pet": pet})
    return sell_all(p, cands)


def _summary(sold: List[dict]) -> str:
    return "、".join(f"{x['uid']} {x['level']} 級 +{x['gain']}" for x in sold) or "無"


def run(p, args) -> Tuple[str, Dict]:
    from .pets import names_of
    names = names_of(p, args)
    cands, skip = candidates(p, names, args)
    for c in cands:
        p.log(f"賣寵物：要賣 {c['who']} {c['where']} {p.pet_name(c['pet'])} {c['level']} 級 檔差 {c['diff']:+d}"
              f"（{c['uid']}，店長說 {price_of(c['level'] or 0)}）")
    for s in skip:
        p.log(f"賣寵物：不賣 {s}")
    cands = cands[:int(getattr(args, "max_count", MAX_SELL))]
    data = {"plan": [{k: c[k] for k in ("who", "where", "uid", "level", "diff")} for c in cands], "skipped": skip}
    if not getattr(args, "apply", False):
        return f"只列（沒加 --apply）：要賣 {len(cands)} 隻、不賣 {len(skip)} 隻", data
    sold = sell_all(p, cands)
    data["sold"] = sold
    return f"賣了 {len(sold)} 隻寵物，石幣 +{sum(x['gain'] for x in sold)}：{_summary(sold)}", data
