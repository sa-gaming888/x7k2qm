"""buy：向 NPC 商店買道具（取代暫存資料夾的 buy_1501.py）。**只花石幣**。

    python -X utf8 -m x7k2qm.autoplay.jobs buy --profile 2 --discover --item 1501               # 只列：誰要買幾個、在哪買、多少錢
    python -X utf8 -m x7k2qm.autoplay.jobs buy --profile 2 --discover --item 1501 --count 1 --apply

每隻角色買到背包有 `--count` 個（已經夠的跳過）。

花費規則（CLAUDE.md 規範 8）：
- **鑽石（真錢）一律不花、聲望不花**：設定表裡這個道具只用鑽石／聲望／活力賣 → 登入前就拒絕。
- **買之前比價、挑最便宜的**（使用者：「不要當盤子」）：從 `data/configs/Npc.json` 找出所有道具商店
  （script 300000）賣這個道具的格子；最便宜的那家沒有驗證過的走法（`ROUTES`）就停，不去比較貴的那家買。
- 送前核對：商品格號、價格、幣種都從設定表取；身上石幣不夠全部要買的量就不開始。
- 送後核對（每買 1 個）：背包那個道具 **+1**、石幣 **正好 −價格**、背包**沒有別的變化**
  （LESSONS #59：itemShopIndex 是 1 起算，算錯會買到別的東西白花錢）；任何一項不符就停，不重送。

流程（2026-10-24 第二組實機買 1501）：組隊 → 傳到薩姆吉爾村 → 走到醫院門口踩傳送 → 醫院裡走到藥劑師旁
（同補血路線）→ 隊長先買 → **隊員離隊**（隊伍中隊員不能跟 NPC 對話，伺服器回 NPC_TALK_IN_TEAM，npc.md §6）各自買。
`Npc_NpcInteract{npcId, npcArg{scriptId 300000, itemShopIndex（1 起算）, itemBuyCount 1}}` 沒有 response → 只 send，
結果看推回的 `Item_ItemInfo` 與 `Role_RoleInfo.stone`。
買完隊員是離隊狀態：重開練功會自己重組隊。
"""

import json
import os
from functools import lru_cache
from typing import Dict, List, Tuple

from . import JobError

NAME = "buy"
HELP = "向 NPC 買道具（--item、--count；只花石幣、先比價；不加 --apply 只列）"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731

SHOP_SCRIPT = 300000                 # NpcScript 300000「道具商店」（itemShopSell）
CURRENCY = {1: "鑽石", 2: "活力", 3: "石幣", 8: "聲望"}
STONE = 3                            # 只准花的幣種
MAX_COUNT = 10                       # 上限保護：每隻最多買到 10 個
BUY_WAIT = 8.0


def _route_hospital():
    from ..headless_party import HEAL_PATH_HOSPITAL, HEAL_PATH_VILLAGE
    return {"point": 1, "village": 1000, "village_path": HEAL_PATH_VILLAGE, "warp": (1000, 80, 66),
            "inside": 1005, "inside_path": HEAL_PATH_HOSPITAL}


# 實機走過的 NPC：npcId → 走法。薩姆吉爾醫院 1005 藥劑師 2721 (22,57)，終點 (22,60)（2026-10-24 實機買 1501）。
# 醫院的武器商店 10272 斧棒／10273 槍爪／10274 弓標／10275 石斧：Npc.json `visable: False`、座標 (63,41～47) 在
# 醫院地圖（50×73）外面 → 是介面開的隱藏商店，走不到也不用走；照藥劑師路線進醫院後直接送（2026-10-31 使用者
# 「你去買醫院棒 8」，第一次實機：沒到貨／石幣沒扣就停，不重送）。
def _route_astar(point: int, village: int, start, door, inside: int, land, npc, radius: int = 2):
    """傳送點 → 村內 A* 走到門格 → 踩進店 → 店內 A* 走到店員 radius 格內（藥劑師實測站 3 格外也買得到）。
    路徑用 `data/mapgrids` 現算，算不出來就停。"""
    def build():
        from ..pathing import load_grid, near
        vp = near(load_grid(village), start, door, 0)
        ip = near(load_grid(inside), land, npc, radius)
        if not vp or not ip:
            raise JobError(f"算不出到商店的路（村 {village} {start}→{door}：{bool(vp)}；店 {inside} {land}→{npc}：{bool(ip)}）")
        return {"point": point, "village": village, "village_path": vp, "warp": (village, *door),
                "inside": inside, "inside_path": ip}
    return build


# 使用者 2026-10-31「買個投石＋五個昏睡棒」（doc/autoplay/game/sleep-spirit-weapons.md）：
# 卡坦的武器店 2778（特洛金Lv3棍棒，昏睡 Lv3）、塔姆塔姆的武器店 2862（特洛金Lv2石，昏睡 Lv2）。門格與落點照 MapWarp，第一次實機。
ROUTES = {2721: _route_hospital, 10272: _route_hospital, 10273: _route_hospital, 10274: _route_hospital,
          10275: _route_hospital,
          2778: _route_astar(5, 1400, (74, 104), (81, 83), 1401, (15, 21), (19, 17)),
          2862: _route_astar(9, 3100, (81, 70), (41, 78), 3109, (10, 25), (27, 26))}


def add_args(ap) -> None:
    ap.add_argument("--item", type=int, required=True, help="道具 itemId（例 1501 耐久力回複藥）")
    ap.add_argument("--count", type=int, default=1, help=f"每隻買到背包有幾個（1～{MAX_COUNT}，預設 1）")
    ap.add_argument("--who", default=None, help="只幫這些角色買（逗號分隔）；預設全隊")


@lru_cache(maxsize=1)
def _tables():
    from ...features.gamedb.tables import CONFIGS
    with open(os.path.join(CONFIGS, "Npc.json"), encoding="utf-8") as fp:
        npcs = json.load(fp)
    with open(os.path.join(CONFIGS, "Item.json"), encoding="utf-8") as fp:
        items = {r["ID"]: r for r in json.load(fp)}
    return npcs, items


def item_name(iid: int) -> str:
    return _tables()[1].get(iid, {}).get("nameTn") or str(iid)


def offers(iid: int) -> List[dict]:
    """設定表裡所有道具商店賣這個道具的格子：npcId、地圖、itemShopIndex（**1 起算**）、價格、幣種。"""
    out = []
    for n in _tables()[0]:
        if SHOP_SCRIPT not in (n.get("script") or []):
            continue
        for i, a in enumerate(n.get("npcArgs") or []):
            if isinstance(a, dict) and a.get("itemId") == iid:
                out.append({"npcId": n["ID"], "npc": n.get("nameTn"), "map": n.get("map"), "index": i + 1,
                            "price": a.get("price"), "currency": a.get("currency")})
    return out


def pick(iid: int) -> dict:
    """挑最便宜、只收石幣、而且走法驗證過的那家。不符合就丟 JobError（登入前就停）。"""
    all_ = offers(iid)
    if not all_:
        raise JobError(f"設定表沒有道具商店賣 {iid} {item_name(iid)}")
    stone = [o for o in all_ if o["currency"] == STONE and isinstance(o["price"], int) and o["price"] > 0]
    if not stone:
        cur = sorted({CURRENCY.get(o["currency"], str(o["currency"])) for o in all_})
        raise JobError(f"{iid} {item_name(iid)} 只用 {cur} 賣：只准花石幣（鑽石一律不花、聲望不花），拒絕")
    cheapest = min(o["price"] for o in stone)
    routed = [o for o in stone if o["npcId"] in ROUTES]
    if not routed:
        where = sorted({(o["npcId"], o["npc"], o["map"]) for o in stone if o["price"] == cheapest})
        raise JobError(f"{iid} {item_name(iid)} 最便宜 {cheapest} 石幣在 {where[:5]}，都沒有驗證過的走法（ROUTES）")
    best = min(routed, key=lambda o: o["price"])
    if best["price"] > cheapest:
        where = sorted({(o["npcId"], o["npc"], o["map"]) for o in stone if o["price"] == cheapest})
        raise JobError(f"{iid} {item_name(iid)}：驗證過的 {best['npcId']} 賣 {best['price']}，"
                       f"但 {where[:5]} 只要 {cheapest}（不當盤子；先補那家的走法）")
    return best


def check(args) -> None:
    n = getattr(args, "count", 1)
    if not isinstance(n, int) or not 1 <= n <= MAX_COUNT:
        raise JobError(f"--count 要在 1～{MAX_COUNT}：{n}")
    pick(args.item)


def have(p, name: str, iid: int) -> int:
    return sum(i.get("overlay", 1) for i in p.bag(name) if i["itemId"] == iid)


def _bag_cells(p, name: str) -> Dict[int, Tuple[int, int]]:
    with p.lock:
        return {i: (it.get("itemId"), it.get("overlay", 1)) for i, it in p.items.get(name, {}).items()
                if it.get("itemId") and it.get("overlay", 1) > 0 and it.get("itemStorge", 0) == 0}


def _stone(p, name: str):
    with p.lock:
        return p.money.get(name, {}).get("stone")


def _diff(before: Dict[int, Tuple[int, int]], after: Dict[int, Tuple[int, int]], iid: int) -> List[str]:
    """背包除了「這個道具 +1」以外的變化（買到別的東西、別的格子變了都會列出來）。"""
    out, gained = [], 0
    for i in sorted(set(before) | set(after)):
        b, a = before.get(i), after.get(i)
        if b == a:
            continue
        if a and a[0] == iid and (b is None or b[0] == iid):
            gained += a[1] - (b[1] if b else 0)
            continue
        out.append(f"第 {i} 格 {b}→{a}")
    if gained != 1:
        out.append(f"{iid} 多了 {gained} 個（應該 1）")
    return out


def go_shop(p, route: dict) -> None:
    """全隊走到商店那張室內圖：傳村 → 走到門口 → 踩傳送 → 室內走到 NPC 旁。每段看地圖確認。"""
    lead = p.party[0][1]
    inside = route["inside"]
    if all(p.s.member_maps.get(n) == inside for _, n in p.party):
        p.log(f"全隊已經在地圖 {inside}")
        return
    if not p.form_team():
        raise JobError("組隊失敗（要全隊一起走到商店）")
    r = p.leader().request("Transfer_TransferTo", {"point": route["point"]}) or {}
    if not r.get("isOK") or not p.wait(lambda: all(p.s.member_maps.get(n) == route["village"] for _, n in p.party), 15):
        raise JobError(f"傳到地圖 {route['village']} 失敗：{r}")
    p.walk(route["village"], route["village_path"])
    m, x, y = route["warp"]
    r = p.leader().request("Role_RoleWarp", {"map": m, "x": x, "y": y}) or {}
    if not p.wait(lambda: all(p.s.member_maps.get(n) == inside for _, n in p.party), 8):
        raise JobError(f"進不了地圖 {inside}：{r}｜目前 {dict(p.s.member_maps)}")
    p.walk(inside, route["inside_path"])
    p.log(f"到地圖 {inside}（隊長 {lead}）")


def buy_one(p, name: str, offer: dict, iid: int) -> None:
    """買 1 個，核對 +1、石幣 −價格、背包沒有別的變化。"""
    before_cells, before_n, before_s = _bag_cells(p, name), have(p, name, iid), _stone(p, name)
    price = offer["price"]
    if before_s is None:
        raise JobError(f"{name} 不知道身上石幣（Role_RoleInfo.stone 沒推過），不買")
    if before_s < price:
        raise JobError(f"{name} 石幣 {before_s} 不夠買 {price}")
    p.clients[name].send("Npc_NpcInteract", {"npcId": offer["npcId"], "npcArg": {
        "scriptId": SHOP_SCRIPT, "itemShopIndex": offer["index"], "itemBuyCount": 1,
        "taskId": 0, "isPetNpc": False, "npcExtraType": 0, "npcExtraId": 0}})
    ok = p.wait(lambda: sum(i.get("overlay", 1) for i in p.items[name].values()
                            if i.get("itemId") == iid and i.get("itemStorge", 0) == 0) == before_n + 1
                and p.money.get(name, {}).get("stone") == before_s - price, BUY_WAIT)
    after_s = _stone(p, name)
    extra = _diff(before_cells, _bag_cells(p, name), iid)
    if not ok or extra or after_s != before_s - price:
        raise JobError(f"{name} 買 {iid} 沒確認：數量 {before_n}→{have(p, name, iid)}、石幣 {before_s}→{after_s}"
                       f"（應該 −{price}）" + (f"、背包其他變化 {extra}" if extra else ""))
    p.log(f"{name} 買到 {iid} {item_name(iid)}：{before_n}→{before_n + 1} 個，石幣 {before_s}→{after_s}")


def run(p, args) -> Tuple[str, dict]:
    iid, count = args.item, getattr(args, "count", 1)
    offer = pick(iid)
    lead = p.party[0][1]
    who = getattr(args, "who", None)
    pick_names = None
    if who:
        pick_names = {x.strip() for x in str(who).replace("，", ",").split(",") if x.strip()}
        bad = pick_names - {n for _, n in p.party}
        if bad:
            raise JobError(f"--who 有不在這組的角色：{sorted(bad)}")
    need = {n: max(0, count - have(p, n, iid)) for _, n in p.party if pick_names is None or n in pick_names}
    todo = {n: k for n, k in need.items() if k}
    shop = f"{offer['npc']}（npc {offer['npcId']}，地圖 {offer['map']}，第 {offer['index']} 格）{offer['price']} 石幣"
    if not todo:
        return f"每隻背包都已經有 {count} 個 {iid} {item_name(iid)}，不用買", {"bought": {}}
    short = {n: (_stone(p, n), k * offer["price"]) for n, k in todo.items()
             if _stone(p, n) is None or _stone(p, n) < k * offer["price"]}
    plan = f"要買 {todo}（{shop}），共 {sum(todo.values()) * offer['price']} 石幣"
    if not getattr(args, "apply", False):
        p.log(plan + (f"｜石幣不夠或不知道（身上, 要花）{short}" if short else ""))
        return f"只列（沒加 --apply，沒有送任何動作）：{plan}", {"bought": {}, "todo": todo}
    if short:
        raise JobError(f"石幣不夠或不知道（身上, 要花）{short}，一個都不買")
    p.log(plan)
    go_shop(p, ROUTES[offer["npcId"]]())
    bought: Dict[str, int] = {}
    for _, n in p.party:
        k = todo.get(n, 0)
        if not k:
            p.log(f"{n} 已經有 {count} 個，跳過")
            continue
        if n != lead:                          # 隊伍中隊員不能跟 NPC 對話
            p.clients[n].send("Team_TeamLeave", {})
            p.nap(1.5)
        for _ in range(k):
            buy_one(p, n, offer, iid)
            bought[n] = bought.get(n, 0) + 1
    return (f"買完：{bought}（{shop}）；隊員已離隊，重開練功會重組隊",
            {"bought": bought, "offer": offer})
