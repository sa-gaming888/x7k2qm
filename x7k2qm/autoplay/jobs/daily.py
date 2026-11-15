"""daily：每日委託（買材料 → 合成料理 → 交件），每隻角色各 5 環。

    python -X utf8 -m x7k2qm.autoplay.jobs daily --profile 3 --discover            # 只列要做什麼、估成本
    python -X utf8 -m x7k2qm.autoplay.jobs daily --profile 3 --discover --apply    # 真的跑

條件：**3 轉 1 級以上**（0～2 轉的角色 `NewRoleWTask_GetTaskInfo` 回 isOK:false）。
清單每天 0 點重置、當天 23:59 過期；**沒有「接受」這一步**，點 NPC 只是開面板，清單本來就在身上。

流程（參考專案-本遊戲-wiki 2026-10-28 提供 ＋ 本方第三組同日實機驗證，五隻各拿 1 顆裝備升級石 93016）：
- 查清單 `NewRoleWTask_GetTaskInfo{}`（空 request）→ `wtList`；每筆 `needList` 是兩樣 `type:1` 道具。
- 料理 `Item_ItemBook{ID, extra:0}`，`ID` 是 **ItemBook 表的列號**（`itemId==成品` 且 `isMain` 空的第一列），
  每次扣 1000 石幣；材料從該列 `needItem` 反查。
- 材料全在兩間便利商店、每樣 500 石幣：1009 薩姆吉爾、2009 瑪麗娜絲。
  `Npc_NpcInteract{npcId, npcArg{scriptId 300000, itemShopIndex=在 npcArgs 的位置+1, itemBuyCount}}`，
  **站在店員 (x−2, y)**；門格（MapWarp）1000 (106,91)→1009、2000 (73,103)→2009。
- 交件 `NewRoleWTask_UploadWTask{uniqueIndex, petIndex:0}`，之後再查一次確認那筆 `isDone`。

規範：只花**石幣**（鑽石不能動，送出前核對幣種與餘額）；每一步等推送確認，失敗就停、不重試。
背包只有 15 格裝不下 10 道料理 → **湊齊一筆就先交**；背包滿時連買材料都會被拒（提示 4002），
所以買之前先檢查空格、必要時先交件騰位。寵物環（`needList.type != 1`）目前跳過。
"""

import json
import os
from functools import lru_cache
from typing import Dict, List, Tuple

from . import JobError

REWARD = 93016                                     # 裝備升級石（每環獎勵）
NAME = "daily"
HELP = "每日委託：買材料、合成料理、交件（3 轉以上才有；只花石幣）"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731

SHOP_SCRIPT = 300000
SHOPS = {1009: (1, 1000, (106, 91)), 2009: (2, 2000, (73, 103))}   # 商店地圖 → (傳送點, 村地圖, 門格)
STONE = 3                      # 幣種 3＝石幣
BAG_SLOTS = 15
ITEM_NEED = 1                  # needList.type：1＝道具


def add_args(ap) -> None:
    ap.add_argument("--who", default=None, help="只做這些角色（逗號分隔）；預設全部")


@lru_cache(maxsize=None)
def _table(fname: str) -> list:
    from ...features.gamedb.tables import CONFIGS
    with open(os.path.join(CONFIGS, fname), encoding="utf-8") as fp:
        return json.load(fp)


def item_name(p, item_id: int) -> str:
    return (p.item_table().get(item_id, {}) or {}).get("nameTn") or str(item_id)


def bag_count(p, name: str, item_id: int) -> int:
    """背包裡這個 itemId 有幾個（不含倉庫、不含穿著）。"""
    return sum(it.get("overlay", 1) for it in p.bag(name) if it.get("itemId") == item_id)


def free_slots(p, name: str) -> int:
    return BAG_SLOTS - len(p.bag(name))


def rings(p, name: str) -> List[dict]:
    """這隻角色今天還沒交的委託。"""
    r = p.clients[name].request("NewRoleWTask_GetTaskInfo", {}) or {}
    if not r.get("isOK"):
        raise JobError(f"{name} 查不到每日委託（要 3 轉 1 級以上）：{r}")
    wt = r.get("wtList")
    rows = list(wt.values()) if isinstance(wt, dict) else (wt or [])
    return [x for x in rows if isinstance(x, dict) and not x.get("isDone")]


def recipe(p, product: int) -> Tuple[int, List[Tuple[int, int]], int]:
    """成品 itemId → (ItemBook 列號, [(材料 itemId, 數量)], 合成要的石幣)。"""
    book = _table("ItemBook.json")
    row = next((x for x in book if x.get("itemId") == product and not x.get("isMain")), None)
    if row is None:
        raise JobError(f"ItemBook 找不到 {item_name(p, product)}（{product}）的配方")
    need = [(n["itemId"], n["itemNum"]) for n in row.get("needItem") or [] if n.get("itemId")]
    cost = next((n.get("currencyValue") for n in row.get("needItem") or [] if n.get("currencyType")), 0)
    return row["ID"], need, int(cost or 0)


def seller(p, item_id: int):
    """在兩間便利商店找賣家 → (npc, 第幾格, 單價, 幣種)。"""
    for npc in _table("Npc.json"):
        if npc.get("map") not in SHOPS:
            continue
        for i, a in enumerate(npc.get("npcArgs") or []):
            if a.get("itemId") == item_id:
                return npc, i + 1, a.get("price"), a.get("currency")
    raise JobError(f"兩間便利商店都沒有賣 {item_name(p, item_id)}（{item_id}）")


def map_of(p, name: str):
    with p.lock:
        return (p.attrs.get(name) or {}).get("map")


def pos_of(p, name: str):
    with p.lock:
        pos = (p.attrs.get(name) or {}).get("nowPos") or {}
    return (pos.get("x"), pos.get("y")) if pos else None


def go_shop(p, name: str, shop_map: int) -> None:
    """確保這隻角色人在那間便利商店（不在就傳到村子、走到門格踩進去）。"""
    from ..pathing import load_grid, near, walk_payload
    if map_of(p, name) == shop_map:
        return
    point, village, door = SHOPS[shop_map]
    c = p.clients[name]
    if map_of(p, name) != village:
        c.request("Transfer_TransferTo", {"point": point})
        if not p.wait(lambda: p.attrs.get(name, {}).get("map") == village, 12):
            raise JobError(f"{name} 傳不到村子 {village}（現在 {map_of(p, name)}）")
    grid = load_grid(village)
    pos = pos_of(p, name)
    path = near(grid, pos, door, 0) if (grid and pos) else None
    if path:
        c.send("Role_RoleWalk", walk_payload(village, path))
        p.nap(len(path) / 4.0 + 1.0)
    c.request("Role_RoleWarp", {"map": village, "x": door[0], "y": door[1]})
    if not p.wait(lambda: p.attrs.get(name, {}).get("map") == shop_map, 12):
        raise JobError(f"{name} 進不了便利商店 {shop_map}（現在 {map_of(p, name)}）")


def buy(p, name: str, item_id: int, count: int) -> None:
    """跟便利商店店員買材料。只花石幣；背包沒空格先交件騰位（滿格連買都會被拒，提示 4002）。"""
    from ..pathing import walk_payload
    npc, slot, price, currency = seller(p, item_id)
    if currency != STONE:
        raise JobError(f"{item_name(p, item_id)} 不是用石幣賣的（幣種 {currency}），不買")
    if free_slots(p, name) < 1:
        raise JobError(f"{name} 背包沒空格，買不了 {item_name(p, item_id)}")
    go_shop(p, name, npc["map"])
    stand = (npc["x"] - 2, npc["y"])                      # 官方站在店員左邊兩格
    p.clients[name].send("Role_RoleWalk", walk_payload(npc["map"], [pos_of(p, name) or stand, stand]))
    p.nap(2.0)
    before, money = bag_count(p, name, item_id), p.money.get(name, {}).get("stone")
    p.clients[name].send("Npc_NpcInteract", {"npcId": npc["ID"],
                                             "npcArg": {"scriptId": SHOP_SCRIPT, "isPetNpc": False,
                                                        "npcExtraType": 0, "npcExtraId": 0,
                                                        "itemShopIndex": slot, "itemBuyCount": count}})
    if not p.wait(lambda: sum(it.get("overlay", 1) for it in p.bag(name)
                              if it.get("itemId") == item_id) >= before + count, 8):
        raise JobError(f"{name} 買 {item_name(p, item_id)}×{count} 沒到貨"
                       f"（{before}→{bag_count(p, name, item_id)}）")
    now = p.money.get(name, {}).get("stone")
    p.log(f"{name} 買 {item_name(p, item_id)}×{count}（{npc.get('nameTn')} 第 {slot} 格）"
          f"石幣 {money:,}→{now:,}" if money and now else
          f"{name} 買 {item_name(p, item_id)}×{count}")


def cook(p, name: str, product: int, book_id: int) -> None:
    """合成一道料理：`Item_ItemBook{ID, extra:0}`。背包滿格伺服器不回應，所以先確認有空格。"""
    if free_slots(p, name) < 2:
        raise JobError(f"{name} 背包只剩 {free_slots(p, name)} 格，合成會沒回應（要留 2 格）")
    before = bag_count(p, name, product)
    p.clients[name].request("Item_ItemBook", {"ID": book_id, "extra": 0})
    if not p.wait(lambda: sum(it.get("overlay", 1) for it in p.bag(name)
                              if it.get("itemId") == product) > before, 8):
        raise JobError(f"{name} 合成 {item_name(p, product)}（ItemBook {book_id}）沒拿到成品")
    p.log(f"{name} 合成 {item_name(p, product)}")


def submit_ready(p, name: str, rows: List[dict]) -> int:
    """料理湊齊的委託先交掉（背包 15 格放不下 10 道）。回傳這次交了幾環。"""
    n = 0
    for w in rows:
        if w.get("_done"):
            continue
        ids = [x["id"] for x in (w.get("needList") or []) if x.get("type") == ITEM_NEED]
        if not ids or any(bag_count(p, name, i) < 1 for i in ids):
            continue
        r = p.clients[name].request("NewRoleWTask_UploadWTask",
                                    {"uniqueIndex": w["uniqueIndex"], "petIndex": 0}) or {}
        p.nap(1.5)
        cur = next((x for x in rings(p, name) if x.get("uniqueIndex") == w["uniqueIndex"]), None)
        if cur is not None:                                # 還在未完成清單裡＝沒交成功
            raise JobError(f"{name} 交委託 {w['uniqueIndex']} 沒變成完成：{r}")
        w["_done"] = True
        n += 1
        p.log(f"{name} ✔ 交委託 {w['uniqueIndex']}（{[item_name(p, i) for i in ids]}）"
              f"背包空 {free_slots(p, name)} 格")
    return n


def plan_for(p, name: str) -> Tuple[List[dict], Dict[int, int], Dict[int, int], int]:
    """回 (未完成的委託, {成品: 幾份}, {材料: 幾個（扣掉身上已有）}, 預估石幣)。"""
    rows = rings(p, name)
    want: Dict[int, int] = {}
    for w in rows:
        for n in w.get("needList") or []:
            if n.get("type") != ITEM_NEED:
                continue
            want[n["id"]] = want.get(n["id"], 0) + int(n.get("num") or 1)
    mats: Dict[int, int] = {}
    cost = 0
    for product, cnt in want.items():
        _, need, one = recipe(p, product)
        cost += one * cnt
        for iid, num in need:
            mats[iid] = mats.get(iid, 0) + num * cnt
    short = {i: n - bag_count(p, name, i) for i, n in mats.items() if bag_count(p, name, i) < n}
    cost += sum(short.values()) * 500
    return rows, want, short, cost


def run(p, args) -> Tuple[str, dict]:
    names = [n for _, n in p.party]
    who = getattr(args, "who", None)
    if who:
        pick = {x.strip() for x in str(who).replace("，", ",").split(",") if x.strip()}
        bad = pick - set(names)
        if bad:
            raise JobError(f"--who 有不在這組的角色：{sorted(bad)}")
        names = [n for n in names if n in pick]
    if not getattr(args, "apply", False):
        out = {}
        for name in names:
            rows, want, short, cost = plan_for(p, name)
            out[name] = {"rings": len(rows), "cook": {item_name(p, k): v for k, v in want.items()},
                         "buy": sum(short.values()), "stone": cost}
            p.log(f"{name}：{len(rows)} 環、要做 {len(want)} 道料理、買 {sum(short.values())} 件材料，"
                  f"約 {cost:,} 石幣")
        return "只列每日委託要做什麼（沒加 --apply，沒有送任何動作）", {"plan": out}
    # 組隊中隊員跟著隊長走、自己不能傳送（提示 30746）：2026-10-28 第二組隊長做完在 2009，
    # 角色35跟著在 2009、要去 1009 傳不回村子就停 → 開跑前先解散，回練功時 form_team 會重組
    p.dismiss_team()
    done = {}
    for name in names:
        done[name] = do_one(p, name)
    from .clean import stash_public
    for name in names:                                    # 使用者 2026-11-02：每日獎勵存共倉
        stash_public(p, name, (REWARD,))
    return f"每日委託完成：{done}", {"done": done}


def do_one(p, name: str) -> int:
    """一隻角色：買材料 → 合成 → 交件。回傳交了幾環。"""
    rows, want, _, _ = plan_for(p, name)
    if not rows:
        p.log(f"{name} 今天的每日委託已經交完")
        return 0
    submitted = submit_ready(p, name, rows)               # 上次做好沒交的先交掉
    for product, cnt in want.items():
        book_id, need, _ = recipe(p, product)
        new = sum(1 for iid, num in need if bag_count(p, name, iid) < 1)   # 背包還沒有的材料各佔 1 格
        if new and free_slots(p, name) < new + 2:         # 買完還要留 2 格合成（cook）
            # 2026-10-31 第三組角色53：背包 4 格阿布的水、空 6 格，買齊 5 樣材料剩 0 格合成不了 → 材料卡在背包
            raise JobError(f"{name} 背包空 {free_slots(p, name)} 格，{item_name(p, product)} 要買 {new} 樣材料"
                           f"＋合成留 2 格，放不下（先清背包）")
        for iid, num in need:
            lack = num * cnt - bag_count(p, name, iid)
            if lack > 0:
                buy(p, name, iid, lack)
        for _ in range(max(0, cnt - bag_count(p, name, product))):   # 同一道料理好幾環都要 → 做好幾次
            cook(p, name, product, book_id)
            submitted += submit_ready(p, name, rows)      # 湊齊就先交，空出背包
    items_only = [w for w in rows if all(n.get("type") == ITEM_NEED for n in w.get("needList") or [])]
    p.log(f"{name} 每日委託交了 {submitted}／{len(rows)} 環")
    if submitted < len(items_only):                       # 道具環都該交完；少了就停，不要當成功
        raise JobError(f"{name} 每日委託只交了 {submitted} 環，道具環有 {len(items_only)} 環")
    junk = set(want) | {iid for product in want for iid, _ in recipe(p, product)[1]}
    discard_leftovers(p, name, junk)
    return submitted


def discard_leftovers(p, name: str, junk) -> None:
    """解完只留獎勵（使用者 11-15「每日解完只留獎勵，任務材料都丟棄」）：今天委託的料理與材料，背包剩下的全部刪掉。
    只刪這幾種 itemId（逐格核對，`clean.do_delete`）；獎勵 93016 不刪。"""
    from .clean import _in_bag, do_delete
    ids = set(junk) - {REWARD}
    with p.lock:
        cells = [(idx, it.get("itemId")) for idx, it in p.items.get(name, {}).items()
                 if it.get("itemId") in ids and _in_bag(it, it.get("itemId"))]
    if not cells:
        return
    p.log(f"{name} 每日委託剩下的材料／料理刪掉：{[item_name(p, i) for _, i in cells]}")
    do_delete(p, name, cells)
