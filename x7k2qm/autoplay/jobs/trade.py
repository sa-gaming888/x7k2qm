"""trade：自己兩個帳號的角色之間交易道具／寵物（RoleTrade）。不是集市，不花錢、不下單。

    # 只列（不登收的那組、不動任何東西）：背包能交易的，或指定的東西夠不夠
    python -X utf8 -m x7k2qm.autoplay.jobs trade --profile 2 --discover --giver 甲
    python -X utf8 -m x7k2qm.autoplay.jobs trade --profile 2 --discover --giver 甲 --to 3 --taker 乙 --item 1501=3 --pet 123456
    # 真的交易
    python -X utf8 -m x7k2qm.autoplay.jobs add trade --profile 2 --discover --apply --giver 甲 --to 3 --taker 乙 --item 1501=3
    # 多筆一次做（雙方各登入一次、同一組連線依序交易；使用者 11-03「效率差到不行」）
    python -X utf8 -m x7k2qm.autoplay.jobs trade --profile 2 --discover --apply --to 9 \\
        --move 甲>乙:1000000143366 --move 丙>乙:1000000143367,1501=3

做法引進自另一個版本 `參考專案/autoplay/trade.py`＋`doc/trade.md`（官方錄檔 official_trade_20261027.pcapng、實機多次成交），
2026-11-02 本專案實機成交（道具 1 件、寵物 1 隻）；協定欄位以本專案 data/sproto_schema.json 為準。

流程：
  1. 給的人把要交易的道具備到背包（背包 → 個倉 → 共倉，只拿沒綁定的那幾格）；寵物身上沒有就從寵物倉第 1 頁領
  2. TradeRequest → 收的人 TradeRequestGet（rid、名字都要對）→ TradeResponse → 雙方 TradeBegin
  3. 一格一格 TradeUploadThing（1 道具、2 寵物，每格約隔 2 秒）；伺服器回的清單要跟放的一樣（只比內容，第 2 格起是倒序）
  4. 收的人看到 TradeChangeThing → 給的人 TradeSure → 收的人 TradeSure → 雙方 TradeHandleing stauts 2（伺服器拼法）→ 雙方 TradeStop
  5. 成交的東西進「交易暫存」：GetTradeSaveList → GetTradeSaveItemPet{type, index 1} 一格一格領到清單空
  6. 比對雙方數量

規則：
- 綁定／上鎖的道具、綁定／上鎖／騎乘／出戰中的寵物不交；穿著的裝備不交。
- 同一個帳號不能互相交易（時光實機：收的人等不到邀請），用共倉搬。
- 第 1 組（交易所常駐沒有擁有者鎖）、第 4 組（沒有使用者當下指示不能開）一律拒絕。
- 收的那組有常駐在跑 → 擁有者鎖擋下、不登入（同帳號第二個程式會把人擠下線）。給的那組有常駐時 `add` 會排進它的佇列。
- 另一台機器有沒有登那組，鎖看不到：要先照規範 15 確認。
- 背包空格、寵物欄先算好再動；一個交易視窗最多 MAX_WINDOW 格，分批最多 MAX_BATCHES 批。
- **出錯就停、不重試**：雙方都按確認之前出錯 → TradeStop 取消、丟 JobError（不會成交）。
  收的人按下確認之後任何不對（等不到成交、領暫存被拒、數量對不上）→ 丟 JobHold：東西可能已經成交，
  **絕對不能當失敗重做**；留在交易暫存的東西不會不見，等人看。
"""

import random
from contextlib import contextmanager
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from .. import owner
from . import JobError, JobHold
from .buy import _tables, item_name
from .carry import BAG_SLOTS

NAME = "trade"
HELP = "自己兩個帳號的角色之間交易道具／寵物（--giver --to --taker --item --pet；不加 --apply 只列）"
# 沒有常駐時只登給的那一隻（--role）；--move 多筆時登整組，一次登入做完全部
SOLO = lambda a: not getattr(a, "move", None)          # noqa: E731
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731

ITEM, PET = 1, 2                     # TradeUploadThing.type
REST, BATTLE, RIDE = 1, 2, 3         # PetInfo.status
BLOCKED_GROUPS = {"": "交易所常駐用的帳號", "1": "第一組（交易所常駐，沒有擁有者鎖）",
                  "4": "第四組（沒有使用者當下指示不能開）"}
STEP_WAIT = 15.0
UPLOAD_PAUSE = (1.5, 2.5)            # 官方錄檔一格約 2 秒
STEP_PAUSE = (0.8, 1.5)
MAX_WINDOW = 7                       # 一個交易視窗最多幾格（時光實機放過 7 格）
MAX_BATCHES = 5                      # 上限保護：最多分幾批
MAX_PETS = 5
MAX_MOVES = 25                       # 上限保護：--move 一次最多幾筆
PET_UNIQUE_DIGITS = 10               # --move 裡 10 位數以上的數字是寵物 uniqueIndex（道具 ID 最多 6 位）
PET_SLOTS = 5                        # 身上寵物欄
DEPOT_PAGES = 5                      # 道具倉最多翻幾頁（個倉上限 5 頁）

REQUEST_GET = "RoleTrade_TradeRequestGet"
BEGIN = "RoleTrade_TradeBegin"
CHANGE = "RoleTrade_TradeChangeThing"
SURE_SEND = "RoleTrade_TradeSureSend"
HANDLING = "RoleTrade_TradeHandleing"
STOP_SEND = "RoleTrade_TradeStopSend"


def add_args(ap) -> None:
    ap.add_argument("--giver", default=None, help="給的角色名字（要是 --profile 這組的）；用 --move 時不用給")
    ap.add_argument("--move", action="append", default=[],
                    help="多筆一次做：給>收:東西[,東西]（東西＝寵物 uniqueIndex 或 道具ID/名稱[=數量]），可多次；"
                         "雙方各登入一次、依序交易，第一筆出錯就停")
    ap.add_argument("--role", type=int, default=None, help="沒有常駐時登這組第幾隻人物（1 起算），要跟 --giver 同一隻")
    ap.add_argument("--to", default=None, help="收的那組（要是另一個帳號）")
    ap.add_argument("--taker", default=None, help="收的角色名字（登入後核對，不對就停）")
    ap.add_argument("--taker-role", type=int, default=None, help="收的那組第幾隻人物（1 起算），要跟 --taker 同一隻")
    ap.add_argument("--item", action="append", default=[], help="道具 ID 或完整名稱[=數量]，可多次")
    ap.add_argument("--pet", action="append", type=int, default=[], help="寵物 uniqueIndex，可多次（只收編號，避免同名送錯）")
    ap.add_argument("--taker-store", dest="taker_store", action="store_true",
                    help="--move 每筆收完，收的人把收到的寵物存個人寵物倉（使用者 11-11「把達分人龍 交易到07」：32 隻、第七組身上只空 11 格）")
    ap.add_argument("--group4-ok", dest="group4_ok", default=None,
                    help="使用者當下明講要動第四組時填他的原話（沒填一律拒絕第四組）")


def rows(v) -> list:
    return list(v.values()) if isinstance(v, dict) else list(v or [])


@lru_cache(maxsize=None)
def same_name(iid: int) -> frozenset:
    """同名的道具 ID（時光實機：騎證-帖拉基歐 在 Item 表有新舊兩個 id，倉庫裡的是另一個）。"""
    table = _tables()[1]
    n = (table.get(iid) or {}).get("nameTn")
    return frozenset({i for i, v in table.items() if n and v.get("nameTn") == n} | {iid})


def find_item(spec: str) -> int:
    spec = spec.strip()
    if spec.isdigit():
        return int(spec)
    hit = sorted(i for i, v in _tables()[1].items() if v.get("nameTn") == spec)
    if not hit:
        raise JobError(f"找不到道具：{spec}")
    return hit[0]


def parse_items(specs: List[str]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for s in specs:
        name, sep, n = s.rpartition("=")
        name, n = (name, n) if sep else (s, "1")
        if not n.strip().isdigit() or int(n) < 1:
            raise JobError(f"--item 數量不合法：{s}")
        iid = find_item(name)
        out[iid] = out.get(iid, 0) + int(n)
    return out


def parse_moves(specs: List[str]) -> List[Tuple[str, str, Dict[int, int], List[int]]]:
    """「給>收:東西,東西」→ [(給, 收, {道具: 數量}, [寵物 uniqueIndex])]。寵物重複、格式不對一律停（還沒登入）。"""
    out, seen = [], set()
    if len(specs) > MAX_MOVES:
        raise JobError(f"--move 一次最多 {MAX_MOVES} 筆，給了 {len(specs)} 筆")
    for s in specs:
        who, sep, what = s.partition(":")
        giver, arrow, taker = who.partition(">")
        giver, taker = giver.strip(), taker.strip()
        if not sep or not arrow or not giver or not taker or not what.strip():
            raise JobError(f"--move 格式要是「給>收:東西[,東西]」：{s}")
        items, pets = [], []
        for x in (t.strip() for t in what.split(",")):
            if not x:
                raise JobError(f"--move 有空的項目：{s}")
            (pets.append(int(x)) if x.isdigit() and len(x) >= PET_UNIQUE_DIGITS else items.append(x))
        if len(pets) > MAX_PETS or seen & set(pets) or len(set(pets)) != len(pets):
            raise JobError(f"--move 寵物不能重複、一筆最多 {MAX_PETS} 隻：{s}")
        seen |= set(pets)
        out.append((giver, taker, parse_items(items), pets))
    return out


def check(args) -> None:
    prof = str(getattr(args, "profile", "") or "")
    to = getattr(args, "to", None)
    allow4 = bool(str(getattr(args, "group4_ok", "") or "").strip())
    for g in (prof, *([str(to)] if to is not None else [])):
        if g == "4" and allow4:          # 使用者當下明講要動第四組（原話寫在 --group4-ok），2026-11-02
            continue
        if g in BLOCKED_GROUPS:
            raise JobError(f"不碰 {BLOCKED_GROUPS[g]}" + ("；使用者當下同意時加 --group4-ok \"原話\"" if g == "4" else ""))
    if to is not None and str(to) == prof:
        raise JobError("同一個帳號的角色不能交易（收的人收不到邀請），改用共倉搬")
    moves = getattr(args, "move", None) or []
    if moves:
        if getattr(args, "giver", None) or getattr(args, "item", None) or getattr(args, "pet", None) \
                or getattr(args, "taker", None) or getattr(args, "role", None) or getattr(args, "taker_role", None):
            raise JobError("--move 不能跟 --giver／--taker／--item／--pet／--role／--taker-role 混用")
        if to is None:
            raise JobError("--move 要給 --to")
        parse_moves(moves)
        return
    if not getattr(args, "giver", None):
        raise JobError("要給 --giver（單筆）或 --move（多筆）")
    items, pets = parse_items(getattr(args, "item", []) or []), getattr(args, "pet", []) or []
    if len(set(pets)) != len(pets) or len(pets) > MAX_PETS:
        raise JobError(f"--pet 不能重複、最多 {MAX_PETS} 隻：{pets}")
    if getattr(args, "apply", False):
        if to is None or not getattr(args, "taker", None):
            raise JobError("--apply 要給 --to 和 --taker")
        if not items and not pets:
            raise JobError("--apply 要給 --item 或 --pet")


def tradable_item(it: dict) -> bool:
    return not (it.get("bind") or it.get("bindTime") or it.get("itemLock"))


def pet_problem(pet: dict) -> Optional[str]:
    if pet.get("bind"):
        return "綁定"
    if pet.get("petLock"):
        return "上鎖"
    if pet.get("status") == RIDE:
        return "騎乘中"
    if pet.get("status") == BATTLE:
        return "出戰中"
    return None


class Side:
    """交易的一邊：哪個 Party 的哪一隻。"""

    def __init__(self, party, name: str) -> None:
        self.p, self.name = party, name
        self.c = party.clients[name]
        self.rid = self.c.rid

    def count(self, iid: int) -> int:
        ids = same_name(iid)
        return sum(i.get("overlay", 1) for i in self.p.bag(self.name) if i.get("itemId") in ids)

    def free(self) -> int:
        return BAG_SLOTS - len(self.p.bag(self.name))

    def cells(self, iid: int) -> List[dict]:
        """背包裡這個道具、可以交易的格子（bag() 已經排除穿著的；要格號）。"""
        ids = same_name(iid)
        return sorted((i for i in self.p.bag(self.name) if i.get("itemId") in ids and tradable_item(i)),
                      key=lambda i: i["itemIndex"])

    def stock(self, iid: int) -> int:
        return sum(i.get("overlay", 1) for i in self.cells(iid))

    def pets(self) -> Dict[int, dict]:
        with self.p.lock:
            return {i: dict(x, petIndex=i) for i, x in self.p.petinfo.get(self.name, {}).items()
                    if (x.get("petId") or 0) > 0}

    def pet(self, unique: int) -> Optional[dict]:
        return next((x for x in self.pets().values() if x.get("uniqueIndex") == unique), None)


class Watcher:
    """收集一隻角色的 RoleTrade_* 推送；要在送邀請之前掛上（推送可能比 wait 早到）。"""

    def __init__(self, side: Side) -> None:
        self.side, self.events = side, []
        side.c.listeners.append(self)

    def __call__(self, e: dict) -> None:
        n = e.get("name") or ""
        if n.startswith("RoleTrade_"):
            with self.side.p.lock:
                self.events.append((n, e.get("data") or {}))

    def close(self) -> None:
        if self in self.side.c.listeners:
            self.side.c.listeners.remove(self)

    def wait(self, name: str, pred=lambda d: True, timeout: float = STEP_WAIT, why: str = "", guard: bool = False) -> dict:
        """guard：長時間等對方（trade-take 等邀請最多 30 分鐘）時也看連線中斷與停止檔，不卡著不理。"""
        p, found = self.side.p, {}

        def cond() -> bool:
            if p.s.kicked:
                raise JobError(f"{self.side.name} 被踢下線（System_KickConnect）")
            if guard and getattr(self.side.c, "closed", False):
                raise JobError(f"{self.side.name} 連線中斷")
            if guard and callable(getattr(p, "stop_requested", None)) and p.stop_requested():
                raise JobError("收到停止檔")
            for i, (n, d) in enumerate(self.events):
                if n == name and pred(d):
                    del self.events[:i + 1]
                    found["d"] = d
                    return True
                if n == STOP_SEND:
                    raise JobError(f"{self.side.name} 收到對方關閉交易：{d}")
            return False
        if not p.wait(cond, timeout):
            raise JobError(f"{self.side.name} 等不到 {name}{why}")
        return found["d"]


# ---- 備貨 ----
def depot_rows(side: Side, public: bool) -> List[dict]:
    out: List[dict] = []
    for page in range(1, DEPOT_PAGES + 1):
        r = side.c.request("ItemHandle_GetNewItemDepotInfo", {"page": page, "isPublic": public}) or {}
        got = rows(r.get("itemInfoList"))
        if not r.get("isOK") or not got:
            break
        out += [dict(x, page=page, isPublic=public) for x in got]
    return out


def survey(side: Side, want: Dict[int, int]) -> Dict[int, dict]:
    """每種道具：背包能交易幾個、缺的從倉庫哪幾格來。背包夠的不查倉庫（少送請求）。只讀不動。"""
    out: Dict[int, dict] = {}
    cache: Dict[bool, List[dict]] = {}
    for iid, need in want.items():
        have, depot = side.stock(iid), []
        for public in (False, True):
            if have + sum(int(x.get("overlay") or 1) for x in depot) >= need:
                break
            if public not in cache:
                cache[public] = depot_rows(side, public)
            depot += [x for x in cache[public] if x.get("itemId") in same_name(iid) and x.get("uniqueIndex")
                      and tradable_item(x) and int(x.get("overlay") or 1) > 0]
        out[iid] = {"need": need, "bag": have, "depot": depot,
                    "total": have + sum(int(x.get("overlay") or 1) for x in depot)}
    return out


def fetch(side: Side, row: dict, count: int, taken: List[str]) -> None:
    """倉庫取到背包；回 isOK 且背包真的變多才算。"""
    iid, before, where = row["itemId"], side.count(row["itemId"]), "共倉" if row["isPublic"] else "個倉"
    r = side.c.request("ItemHandle_GetItemToNewDepot", {"page": row["page"], "isPublic": row["isPublic"],
                                                        "uniqueIndex": row["uniqueIndex"], "count": count}) or {}
    if not (r.get("isOK") and side.p.wait(lambda: side.count(iid) > before, 8)):
        raise JobError(f"{side.name} 從{where}取 {item_name(iid)}×{count} 沒確認：{r}")
    row["overlay"] = int(row.get("overlay") or 1) - count
    taken.append(f"{item_name(iid)}×{count}（{where}第 {row['page']} 頁）")
    side.p.log(f"交易：{side.name} 從{where}取出 {item_name(iid)}×{count}")
    side.p.nap(random.uniform(0.6, 1.2))


def prepare(side: Side, remaining: Dict[int, int], info: Dict[int, dict], room: int,
            taken: List[str]) -> Tuple[List[Tuple[int, int, int]], Dict[int, int]]:
    """把這批備到背包，回傳 (要放的格 [(itemIndex, itemId, 數量)], 這批每種交易幾個)。
    room＝這批最多幾格；給的人背包沒空格就先交易已備好的，剩下的下一批。"""
    slots: List[Tuple[int, int, int]] = []
    traded: Dict[int, int] = {}
    for iid, need in remaining.items():
        while side.stock(iid) < need and side.free() >= 1 and len(side.cells(iid)) + len(slots) < room:
            row = next((x for x in info[iid]["depot"] if int(x.get("overlay") or 1) > 0), None)
            if row is None:
                raise JobError(f"{side.name} 沒綁定的 {item_name(iid)} 不夠：要 {need}，只找到 {side.stock(iid)}")
            fetch(side, row, min(need - side.stock(iid), int(row.get("overlay") or 1)), taken)
        left = need
        for it in side.cells(iid):
            if left <= 0 or len(slots) >= room:
                break
            c = min(left, it.get("overlay", 1))
            slots.append((it["itemIndex"], it["itemId"], c))
            traded[iid] = traded.get(iid, 0) + c
            left -= c
    return slots, traded


def prepare_pets(side: Side, uniques: List[int], depot: List[dict], taken: List[str]) -> List[dict]:
    """身上的直接用；寵物倉的領到身上。回傳要放的寵物（含 petIndex）。"""
    out = []
    for u in uniques:
        pet = side.pet(u)
        if pet is None:
            row = next((x for x in depot if x.get("uniqueIndex") == u), None)
            if row is None:
                raise JobError(f"{side.name} 身上、寵物倉第 1 頁都沒有寵物 {u}")
            r = side.c.request("NewPet_GetPetFromNewDepot", {"page": side.p.pet_page(side.name, row),
                                                             "isPublic": bool(row.get("isPublic")),
                                                             "uniqueIndex": u}) or {}
            if not (r.get("isOK") and side.p.wait(lambda: side.pet(u) is not None, 8)):
                raise JobError(f"{side.name} 從寵物倉領 {u} 沒確認（身上滿了？）：{r}")
            taken.append(f"寵物 {u}（{'共' if row.get('isPublic') else '個'}倉）")
            side.p.nap(random.uniform(0.6, 1.2))
            pet = side.pet(u)
        why = pet_problem(pet)
        if why:
            raise JobError(f"{side.name} 的寵物 {u} {why}，不能交易")
        out.append(pet)
    return out


# ---- 交易視窗 ----
def stop_both(g: Side, t: Side) -> None:
    for s in (t, g):
        try:
            s.c.send("RoleTrade_TradeStop", {})
        except Exception as e:  # noqa: BLE001  連線已斷也要讓後面照原因處理，只記日誌
            s.p.log(f"交易：{s.name} 送 TradeStop 失敗：{e}")


def upload_items(g: Side, slots: List[Tuple[int, int, int]]) -> None:
    expect: List[Tuple[int, int, int]] = []
    for idx, iid, c in slots:
        g.p.nap(random.uniform(*UPLOAD_PAUSE))
        r = g.c.request("RoleTrade_TradeUploadThing", {"type": ITEM, "index": idx, "count": c}, timeout=STEP_WAIT) or {}
        expect.append((idx, iid, c))
        got = sorted((x.get("index"), x.get("id"), x.get("count")) for x in rows(r.get("itemList")))
        if got != sorted(expect):                # 伺服器回的順序不照放的順序（時光實機第 2 格起倒序），只比內容
            raise JobError(f"放 {item_name(iid)}×{c} 後伺服器回的道具清單不對：{got}（應該 {sorted(expect)}）")
        g.p.log(f"交易：{g.name} 放上 {item_name(iid)}×{c}")


def upload_pets(g: Side, pets: List[dict]) -> None:
    for n, pet in enumerate(pets, 1):
        g.p.nap(random.uniform(*UPLOAD_PAUSE))
        r = g.c.request("RoleTrade_TradeUploadThing", {"type": PET, "index": pet["petIndex"], "count": 1},
                        timeout=STEP_WAIT) or {}
        plist = rows(r.get("petList"))               # RoleTradeUploadInfo 沒有 uniqueIndex：比 index（身上第幾格），沒有才比 id
        hit = any(x.get("index") == pet["petIndex"] if x.get("index") is not None else x.get("id") == pet.get("petId")
                  for x in plist)
        if len(plist) != n or not hit:
            raise JobError(f"放寵物 {pet.get('uniqueIndex')} 後伺服器回的寵物清單對不上：{plist}")
        g.p.log(f"交易：{g.name} 放上寵物 {pet.get('uniqueIndex')}")


def window(g: Side, t: Side, slots: List[Tuple[int, int, int]], pets: List[dict]) -> None:
    """開視窗 → 放東西 → 雙方確認 → 成交 → 關視窗。收的人確認之前出錯＝取消；之後出錯＝JobHold。"""
    wg, wt = Watcher(g), Watcher(t)
    committed = False
    try:
        g.c.send("RoleTrade_TradeRequest", {"targetRid": t.rid})
        wt.wait(REQUEST_GET, lambda d: d.get("requestRid") == g.rid and d.get("requestName") == g.name,
                why="（邀請沒到：同帳號？收的人關了交易邀請開關？）")
        t.p.nap(random.uniform(*STEP_PAUSE))
        t.c.send("RoleTrade_TradeResponse", {"result": True, "targetRid": g.rid})
        wg.wait(BEGIN, lambda d: d.get("targetRid") == t.rid)
        wt.wait(BEGIN, lambda d: d.get("targetRid") == g.rid)
        upload_items(g, slots)
        upload_pets(g, pets)
        wt.wait(CHANGE, lambda d: (len(rows(d.get("itemList"))), len(rows(d.get("petList")))) == (len(slots), len(pets)))
        g.p.nap(random.uniform(*STEP_PAUSE))
        g.c.send("RoleTrade_TradeSure", {"sure": True})
        wt.wait(SURE_SEND, lambda d: d.get("sure") and d.get("targetRid") == g.rid)
        t.p.nap(random.uniform(*STEP_PAUSE))
        committed = True                             # 這一包送出去就可能成交了
        t.c.send("RoleTrade_TradeSure", {"sure": True})
        wg.wait(HANDLING, lambda d: d.get("stauts") == 2)
        wt.wait(HANDLING, lambda d: d.get("stauts") == 2)
    except Exception as e:  # noqa: BLE001  出錯就停：任何例外都先關視窗再往外丟
        stop_both(g, t)
        if committed:
            raise JobHold(f"雙方都確認了但沒看到成交（{e}）：可能已成交，不要重做，先查雙方背包與交易暫存") from e
        raise JobError(f"交易取消（還沒雙方確認，不會成交）：{e}") from e
    finally:
        wg.close()
        wt.close()
    stop_both(g, t)
    g.p.log(f"交易：{g.name} → {t.name} 成交")


def claim(t: Side, expected: int) -> int:
    """從交易暫存一格一格領（每次領第 1 格，領完再列一次）。到這裡已經成交，被拒一律 JobHold，不重做。"""
    got = 0
    try:
        for _ in range(expected + 5):                # 上限保護
            r = t.c.request("RoleTrade_GetTradeSaveList", {}) or {}
            its, pets = rows(r.get("itemSaveList")), rows(r.get("petSaveList"))
            if not its and not pets:
                return got
            rr = t.c.request("RoleTrade_GetTradeSaveItemPet", {"type": ITEM if its else PET, "index": 1}) or {}
            if not rr.get("isOK"):
                raise JobHold(f"{t.name} 領交易暫存被拒（背包或寵物欄滿？）：{rr}；已成交、東西還在暫存，不要重做")
            got += 1
            t.p.nap(random.uniform(0.4, 0.9))
    except JobHold:
        raise
    except Exception as e:  # noqa: BLE001
        raise JobHold(f"{t.name} 領交易暫存出錯（{e}）：已成交、東西還在暫存，不要重做") from e
    raise JobHold(f"{t.name} 交易暫存領了 {got} 格還沒領完（應該 {expected} 格）：停，等人看")


def verify(g: Side, t: Side, before: Dict[int, Tuple[int, int]], traded: Dict[int, int], pets: List[dict]) -> None:
    def ok() -> bool:
        return (all(t.count(i) - before[i][1] == n and before[i][0] - g.count(i) == n for i, n in traded.items())
                and all(t.pet(x["uniqueIndex"]) is not None and g.pet(x["uniqueIndex"]) is None for x in pets))
    if t.p.wait(ok, 8):
        return
    bad = [f"{item_name(i)}：{g.name} 少 {before[i][0] - g.count(i)}、{t.name} 多 {t.count(i) - before[i][1]}，應該 {n}"
           for i, n in traded.items()]
    bad += [f"寵物 {x['uniqueIndex']}：{t.name} 有 {t.pet(x['uniqueIndex']) is not None}、{g.name} 還有 "
            f"{g.pet(x['uniqueIndex']) is not None}" for x in pets]
    raise JobHold("交易後數量對不上（已成交，不要重做）：" + "；".join(bad))


# ---- 兩邊各自的程式交易（trade-give／trade-take；使用者 11-14「讓兩邊不同程式自己交易」）----
# 給的、收的各在自己的常駐程式（練寵、練功）裡排一件工作，用自己的連線各做一半；不再由同一個行程登兩組，
# 機房與這台不同 IP 也能交易。由 AI 通知兩邊 session：收方先排 trade-take（停戰鬥、核對、等邀請），回報就緒後給方才排 trade-give。
HALF_WAIT = 120.0                    # 一半流程等對方下一步（對方是另一個程式，放東西一格約 2 秒）
INVITE_WAIT_MIN = 10                 # 收方等邀請預設幾分鐘
INVITE_WAIT_MAX = 30                 # 上限（佇列 MAX_AGE 也是 30 分鐘）


def parse_pet_kinds(specs: List[str]) -> List[Tuple[int, Optional[int], Optional[int]]]:
    """「petId[:等級[:轉數]]」→ [(petId, 等級或 None, 轉數或 None)]。"""
    out = []
    for s in specs:
        parts = [x.strip() for x in str(s).split(":")]
        if not 1 <= len(parts) <= 3 or not all(x.isdigit() for x in parts):
            raise JobError(f"--pet-kind 格式要是 petId[:等級[:轉數]]：{s}")
        n = [int(x) for x in parts] + [None] * (3 - len(parts))
        out.append((n[0], n[1], n[2]))
    return out


def check_profile(args) -> None:
    prof = str(getattr(args, "profile", "") or "")
    if prof == "4" and str(getattr(args, "group4_ok", "") or "").strip():
        return
    if prof in BLOCKED_GROUPS:
        raise JobError(f"不碰 {BLOCKED_GROUPS[prof]}" + ("；使用者當下同意時加 --group4-ok \"原話\"" if prof == "4" else ""))


def stop_one(s: Side) -> None:
    try:
        s.c.send("RoleTrade_TradeStop", {})
    except Exception as e:  # noqa: BLE001  連線已斷也要讓後面照原因處理，只記日誌
        s.p.log(f"交易：{s.name} 送 TradeStop 失敗：{e}")


def give_half(g: Side, taker_rid: int, taker_name: str, slots: List[Tuple[int, int, int]], pets: List[dict],
              wait: float = HALF_WAIT) -> None:
    """給方一半：邀請 → 對方接受 → 放東西 → 自己確認 → 等成交。
    自己確認之後出錯：東西都還在身上＝對方沒確認、沒成交（JobError）；少了或看不出來＝JobHold，不要重做。"""
    wg = Watcher(g)
    before = {iid: g.count(iid) for _, iid, _ in slots}
    committed = False
    try:
        g.c.send("RoleTrade_TradeRequest", {"targetRid": taker_rid})
        wg.wait(BEGIN, lambda d: d.get("targetRid") == taker_rid, timeout=wait,
                why=f"（{taker_name} 沒接邀請：收方沒排 trade-take、名字／rid 不對、或關了交易邀請開關？）", guard=True)
        upload_items(g, slots)
        upload_pets(g, pets)
        g.p.nap(random.uniform(*STEP_PAUSE))
        committed = True                             # 自己確認之後，收方一按確認就成交
        g.c.send("RoleTrade_TradeSure", {"sure": True})
        wg.wait(HANDLING, lambda d: d.get("stauts") == 2, timeout=wait,
                why=f"（{taker_name} 沒按確認：收方核對內容不符？）")
    except Exception as e:  # noqa: BLE001  出錯就停：先關視窗再往外丟
        stop_one(g)
        if not committed:
            raise JobError(f"交易取消（還沒確認，不會成交）：{e}") from e
        g.p.nap(5)
        still = (all(g.count(i) >= n for i, n in before.items())
                 and all(g.pet(x["uniqueIndex"]) is not None for x in pets))
        if still:
            raise JobError(f"交易取消（收方沒確認，東西都還在 {g.name} 身上）：{e}") from e
        raise JobHold(f"{g.name} 確認後出錯、東西已經不在身上（{e}）：可能已成交，不要重做，先查收方") from e
    finally:
        wg.close()
    stop_one(g)
    traded: Dict[int, int] = {}
    for _, iid, c in slots:
        traded[iid] = traded.get(iid, 0) + c

    def gone() -> bool:
        return (all(before[i] - g.count(i) == n for i, n in traded.items())
                and all(g.pet(x["uniqueIndex"]) is None for x in pets))
    if not g.p.wait(gone, 8):
        raise JobHold(f"{g.name} 成交後身上數量對不上（已成交，不要重做）："
                      f"{[(item_name(i), before[i] - g.count(i), n) for i, n in traded.items()]}")
    g.p.log(f"交易：{g.name} → {taker_name} 成交（給方）")


def change_matches(d: dict, items: Dict[int, int], kinds: List[Tuple[int, Optional[int], Optional[int]]]) -> bool:
    """收方看對方放上來的東西：跟交易單一樣回 True；還沒放完回 False；多放、放錯一律丟 JobError（不按確認）。"""
    got: Dict[int, int] = {}
    for x in rows(d.get("itemList")):
        iid = int(x.get("id") or 0)
        key = next((i for i in items if iid in same_name(i)), None)
        if key is None:
            raise JobError(f"對方放了交易單沒有的道具 {item_name(iid)}（{iid}）×{x.get('count')}，不確認")
        got[key] = got.get(key, 0) + int(x.get("count") or 0)
    over = [f"{item_name(i)} {n}/{items[i]}" for i, n in got.items() if n > items[i]]
    if over:
        raise JobError(f"對方放的道具比交易單多：{over}，不確認")
    left = list(kinds)
    plist = rows(d.get("petList"))
    if len(plist) > len(kinds):
        raise JobError(f"對方放了 {len(plist)} 隻寵物，交易單只有 {len(kinds)} 隻，不確認")
    for x in plist:
        pid, lv, tr = int(x.get("id") or 0), x.get("level"), x.get("trans")
        hit = next((k for k in left if k[0] == pid and (k[1] is None or lv is None or k[1] == lv)
                    and (k[2] is None or tr is None or k[2] == tr)), None)
        if hit is None:
            raise JobError(f"對方放的寵物 {pid}（{lv} 級 {tr} 轉）不在交易單，不確認")
        left.remove(hit)
    return got == items and not left


def take_half(t: Side, giver_name: str, giver_rid: Optional[int], items: Dict[int, int],
              kinds: List[Tuple[int, Optional[int], Optional[int]]], invite_wait: float,
              wait: float = HALF_WAIT) -> Tuple[Dict[int, int], int]:
    """收方一半：等交易單上那個人的邀請 → 接受 → 等對方放的東西跟交易單完全一樣 → 對方確認後自己確認 → 領暫存 → 核對。
    回傳 (每種道具多了幾個, 收到幾隻寵物)。"""
    old = t.c.request("RoleTrade_GetTradeSaveList", {}) or {}
    if rows(old.get("itemSaveList")) or rows(old.get("petSaveList")):
        raise JobError(f"{t.name} 交易暫存還有東西（上次沒領完？），先處理，這次不交易")
    if len(t.pets()) + len(kinds) > PET_SLOTS:
        raise JobError(f"{t.name} 身上 {len(t.pets())} 隻，收 {len(kinds)} 隻會超過 {PET_SLOTS} 格，先騰寵物欄")
    if t.free() < len(items):
        raise JobError(f"{t.name} 背包只剩 {t.free()} 格，收 {len(items)} 種道具放不下")
    before_items = {i: t.count(i) for i in items}
    before_pets = {x.get("uniqueIndex") for x in t.pets().values()}
    wt = Watcher(t)
    last: Dict[str, dict] = {}
    committed = False
    rid = giver_rid
    try:
        t.p.log(f"交易：收方就緒：{t.name}（rid {t.rid}）等 {giver_name}"
                f"{'（rid %s）' % giver_rid if giver_rid else ''} 的邀請，最多 {invite_wait / 60:.0f} 分鐘")
        got = wt.wait(REQUEST_GET, lambda d: d.get("requestName") == giver_name
                      and (giver_rid is None or d.get("requestRid") == giver_rid),
                      timeout=invite_wait, why=f"（{giver_name} 沒發邀請）", guard=True)
        rid = got.get("requestRid")
        t.p.nap(random.uniform(*STEP_PAUSE))
        t.c.send("RoleTrade_TradeResponse", {"result": True, "targetRid": rid})
        wt.wait(BEGIN, lambda d: d.get("targetRid") == rid, timeout=wait)

        def full(d: dict) -> bool:
            last["d"] = d
            return change_matches(d, items, kinds)
        wt.wait(CHANGE, full, timeout=wait, why="（對方東西沒放齊）")
        wt.wait(SURE_SEND, lambda d: d.get("sure") and d.get("targetRid") == rid, timeout=wait,
                why="（對方沒按確認）")
        if not change_matches(last["d"], items, kinds):          # 確認前最後再對一次（理論上不會變）
            raise JobError("對方確認時的東西跟交易單不一樣")
        t.p.nap(random.uniform(*STEP_PAUSE))
        committed = True
        t.c.send("RoleTrade_TradeSure", {"sure": True})
        wt.wait(HANDLING, lambda d: d.get("stauts") == 2, timeout=wait)
    except Exception as e:  # noqa: BLE001
        stop_one(t)
        if committed:
            raise JobHold(f"雙方都確認了但沒看到成交（{e}）：可能已成交，不要重做，先查雙方背包與交易暫存") from e
        raise JobError(f"交易取消（收方還沒確認，不會成交）：{e}") from e
    finally:
        wt.close()
    stop_one(t)
    claim(t, len(rows(last["d"].get("itemList"))) + len(rows(last["d"].get("petList"))))

    def ok() -> bool:
        new = [x for x in t.pets().values() if x.get("uniqueIndex") not in before_pets]
        return all(t.count(i) - before_items[i] == n for i, n in items.items()) and len(new) == len(kinds)
    if not t.p.wait(ok, 8):
        raise JobHold(f"{t.name} 收完數量對不上（已成交，不要重做）："
                      f"{[(item_name(i), t.count(i) - before_items[i], n) for i, n in items.items()]}")
    t.p.log(f"交易：{giver_name} → {t.name} 成交（收方）")
    return dict(items), len(kinds)


def open_taker(args, log):
    """登收的那組：單筆只登一隻；--move 多筆登整組（一次登入，收的人可能好幾隻）。測試換掉這個函式。"""
    from ..headless_party import Party
    if getattr(args, "move", None):
        return Party(log, profile=str(args.to), discover=True, size=5)
    return Party(log, profile=str(args.to), discover=True, size=1, pick=getattr(args, "taker_role", None))


def trade_all(g: Side, t: Side, want: Dict[int, int], uniques: List[int], info: Dict[int, dict],
              pet_depot: List[dict], taken: List[str]) -> Tuple[Dict[int, int], int]:
    """分批交易到全部交完；回傳 (每種交易了幾個, 交易幾隻寵物)。"""
    old = t.c.request("RoleTrade_GetTradeSaveList", {}) or {}
    if rows(old.get("itemSaveList")) or rows(old.get("petSaveList")):
        raise JobError(f"{t.name} 交易暫存還有東西（上次沒領完？），先處理，這次不交易")
    if len(t.pets()) + len(uniques) > PET_SLOTS:
        raise JobError(f"{t.name} 身上 {len(t.pets())} 隻，收 {len(uniques)} 隻會超過 {PET_SLOTS} 格，先騰寵物欄")
    remaining, pending, done, sent_pets = dict(want), list(uniques), {}, 0
    for batch in range(1, MAX_BATCHES + 1):
        if not remaining and not pending:
            return done, sent_pets
        pets = prepare_pets(g, pending, pet_depot, taken)
        room = min(MAX_WINDOW - len(pets), t.free())
        if remaining and room < 1:
            raise JobError(f"{t.name} 背包沒有空格，收不了；已交易 {done}，還剩 {remaining}")
        slots, traded = prepare(g, remaining, info, room, taken) if remaining else ([], {})
        if not slots and not pets:
            raise JobError(f"{g.name} 備不了貨（背包滿？）；已交易 {done}，還剩 {remaining}")
        ids = list(traded)
        before = {i: (g.count(i), t.count(i)) for i in ids}
        g.p.log(f"交易：第 {batch} 批 {[(item_name(i), n) for i, n in traded.items()]}、寵物 {len(pets)} 隻")
        window(g, t, slots, pets)
        taken.clear()                                # 拿出來的已經交易出去了
        claim(t, len(slots) + len(pets))
        verify(g, t, before, traded, pets)
        for i, n in traded.items():
            done[i] = done.get(i, 0) + n
            remaining[i] -= n
            if remaining[i] <= 0:
                del remaining[i]
        sent_pets += len(pets)
        pending = []
        g.p.nap(random.uniform(*STEP_PAUSE))
    if remaining or pending:
        raise JobError(f"分了 {MAX_BATCHES} 批還沒交易完：已交易 {done}，還剩 {remaining}")
    return done, sent_pets


def plan(p, g: Side, want: Dict[int, int], uniques: List[int]) -> Tuple[Dict[int, dict], List[dict], List[str]]:
    """只讀：每種道具夠不夠、寵物在哪、能不能交易。回傳 (道具來源, 寵物倉清單, 問題)。"""
    info = survey(g, want)
    short = [f"{item_name(i)} 要 {v['need']} 只有 {v['total']}" for i, v in info.items() if v["total"] < v["need"]]
    missing = [u for u in uniques if g.pet(u) is None]
    pet_depot = p.pet_depots(g.name) if missing else []
    lost = [u for u in missing if not any(x.get("uniqueIndex") == u for x in pet_depot)]
    bad = [u for u in missing if any(x.get("uniqueIndex") == u and x.get("bind") for x in pet_depot)]
    bad += [u for u in uniques if g.pet(u) is not None and pet_problem(g.pet(u))]
    for iid, v in info.items():
        p.log(f"交易：{item_name(iid)} 要 {v['need']}，背包 {v['bag']}、倉庫 {v['total'] - v['bag']}")
    problems = short + ([f"找不到寵物 {lost}"] if lost else []) + ([f"寵物不能交易 {bad}"] if bad else [])
    return info, pet_depot, problems


def run(p, args) -> Tuple[str, dict]:
    if getattr(args, "move", None):
        return run_moves(p, args)
    want, uniques = parse_items(args.item or []), list(args.pet or [])
    if args.giver not in p.clients:
        raise JobError(f"{args.giver} 不在這次登入的人物裡（{sorted(p.clients)}）：沒有常駐時用 --role 指定第幾隻")
    g = Side(p, args.giver)
    if not want and not uniques:
        names = sorted((item_name(i["itemId"]), i.get("overlay", 1)) for i in p.bag(g.name) if tradable_item(i))
        free_pets = [(x.get("uniqueIndex"), x.get("level")) for x in g.pets().values() if not pet_problem(x)]
        return f"只列（沒有送任何動作）：{g.name} 背包能交易 {names}；身上寵物能交易 {free_pets}", {"items": names, "pets": free_pets}
    info, pet_depot, problems = plan(p, g, want, uniques)
    if not getattr(args, "apply", False):
        return (f"只列（沒加 --apply，沒有送任何動作）：{'不能交易：' + '；'.join(problems) if problems else '要交易的都有、沒綁定'}",
                {"survey": {i: v["total"] for i, v in info.items()}, "problems": problems})
    if problems:
        raise JobError("不能交易：" + "；".join(problems))
    taken: List[str] = []
    with taker_party(p, args, [args.taker], "用 --taker-role 指定") as tp:
        try:
            done, n = trade_all(g, Side(tp, args.taker), want, uniques, info, pet_depot, taken)
        except Exception:  # noqa: BLE001  連線錯誤也要報拿出來的東西
            if taken:
                p.log(f"交易：已從倉庫拿到 {g.name} 身上、沒交易出去：{taken}")
            raise
    return f"交易完成：{g.name} → 第 {args.to} 組 {args.taker}：{describe(done, n)}", {"items": done, "pets": n}


def describe(done: Dict[int, int], n: int) -> str:
    return "、".join([f"{item_name(i)}×{c}" for i, c in done.items()] + ([f"寵物 {n} 隻"] if n else []))


@contextmanager
def taker_party(p, args, takers: List[str], hint: str):
    """拿收的那組的擁有者鎖 → 登入 → 核對收的人都在 → 停原地遇敵；結束一定登出、放鎖。"""
    odir = getattr(args, "owner_dir", None) or owner.default_dir()
    try:
        owner.acquire(odir, str(args.to), owner.KIND_JOBS, log=p.log)
    except owner.OwnerBusy as e:
        raise JobError(f"收的那組不能登入：{e}") from e
    tp = None
    try:
        tp = open_taker(args, p.log)
        tp.login_all()
        tp.nap(3)
        miss = [t for t in takers if t not in tp.clients]
        if miss:
            raise JobError(f"第 {args.to} 組登入的是 {sorted(tp.clients)}，不是 {'、'.join(miss)}：{hint}")
        if not tp.pause():
            raise JobError(f"{'、'.join(takers)} 停不了原地遇敵")
        yield tp
    finally:
        try:
            if tp is not None:
                tp.close()
        finally:
            owner.release(odir, str(args.to))


def run_moves(p, args) -> Tuple[str, dict]:
    """--move 多筆：給的那組、收的那組各登入一次，同一組連線依序交易。
    全部先只讀檢查（缺貨、綁定、找不到、收的人寵物欄）再開第一個視窗；任何一筆出錯就停，後面的不做。"""
    moves = parse_moves(args.move)
    plans, problems = [], []
    for giver, taker, want, uniques in moves:
        if giver not in p.clients:
            raise JobError(f"{giver} 不在這次登入的人物裡（{sorted(p.clients)}）")
        info, depot, probs = plan(p, Side(p, giver), want, uniques)
        problems += [f"{giver}：{x}" for x in probs]
        plans.append((giver, taker, want, uniques, info, depot))
    if not getattr(args, "apply", False):
        head = "不能交易：" + "；".join(problems) if problems else f"{len(plans)} 筆要交易的都有、沒綁定"
        return f"只列（沒加 --apply，沒有送任何動作）：{head}", {"moves": len(plans), "problems": problems}
    if problems:
        raise JobError("不能交易：" + "；".join(problems))
    takers = list(dict.fromkeys(t for _, t, *_ in plans))
    results: List[dict] = []
    with taker_party(p, args, takers, "--move 的收的人要是這組的角色") as tp:
        store = bool(getattr(args, "taker_store", False))
        for t in takers:                             # 收的人寵物欄先算總數，免得交到一半才卡住
            # --taker-store：每筆收完就存個倉，只要每一筆放得下；個倉也要放得下全部
            need = (max(len(u) for _, tk, _, u, _, _ in plans if tk == t) if store
                    else sum(len(u) for _, tk, _, u, _, _ in plans if tk == t))
            have = len(Side(tp, t).pets())
            if have + need > PET_SLOTS:
                raise JobError(f"{t} 身上 {have} 隻，{'一筆最多' if store else '這次'}要收 {need} 隻會超過 {PET_SLOTS} 格，先騰寵物欄")
            if store:
                total = sum(len(u) for _, tk, _, u, _, _ in plans if tk == t)
                from ..headless_party import PET_DEPOT_NUM
                used = len(tp.pet_depot(t))
                if used + total > PET_DEPOT_NUM:
                    raise JobError(f"{t} 個人寵物倉第 1 頁已有 {used} 隻，再存 {total} 隻會超過 {PET_DEPOT_NUM}，不交易")
        for k, (giver, taker, want, uniques, info, depot) in enumerate(plans, 1):
            p.log(f"交易：第 {k}/{len(plans)} 筆 {giver} → {taker}")
            taken: List[str] = []
            try:
                done, n = trade_all(Side(p, giver), Side(tp, taker), want, uniques, info, depot, taken)
            except Exception:  # noqa: BLE001  出錯就停：後面的不做，先報已完成的
                if taken:
                    p.log(f"交易：已從倉庫拿到 {giver} 身上、沒交易出去：{taken}")
                p.log(f"交易：第 {k} 筆出錯，前面完成 {len(results)} 筆、後面 {len(plans) - k} 筆沒做")
                raise
            results.append({"giver": giver, "taker": taker, "items": done, "pets": n})
            p.log(f"交易：第 {k}/{len(plans)} 筆完成：{giver} → {taker}：{describe(done, n)}")
            if store and uniques:
                # 已成交：存倉出錯一律 JobHold（東西在收的人身上，不能當沒交易重做）
                from .pets import do_deposit
                for u in uniques:
                    pet = Side(tp, taker).pet(u)
                    try:
                        if pet is None:
                            raise JobError(f"身上找不到剛收的寵物 {u}")
                        do_deposit(tp, taker, int(pet.get("petId")), uid=u)
                    except Exception as e:  # noqa: BLE001
                        raise JobHold(f"交易：第 {k} 筆已成交，{taker} 把 {u} 存個倉失敗（{e}）：停，不要重做這筆") from e
                    tp.nap(random.uniform(0.6, 1.2))
                p.log(f"交易：{taker} 收到的 {len(uniques)} 隻存進個倉")
            p.nap(random.uniform(*STEP_PAUSE))
    return f"交易完成 {len(results)} 筆 → 第 {args.to} 組", {"moves": results}
