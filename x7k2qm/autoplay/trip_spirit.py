"""隊長（無畫面客戶端）去競技場買阿薩Lv5金幣並穿上，再回練功點。流程見 doc/autoplay/flows/buy-attribute-spirit.md。

    python -X utf8 -m x7k2qm.autoplay.trip_spirit --back-point 19

- 帳密讀第二組環境變數 BOT_ACCOUNT_2 / BOT_PASSWORD_2，角色 BOT_RID_2（隊長 rid）。
- 登入會把遊戲視窗裡的同一個角色擠下線（同角色只能一處登入）；其他角色不受影響。
- 每一步都等對應的封包；逾時或收到錯誤就**停**（不重試），印出停在哪一步。
- 花費：只買 1 個（500 石幣），先比價過（競技場最便宜）。
"""

import argparse
import sys
import time
from typing import Callable, Optional

from ..client.config import load
from ..client.session import open_client
from . import pathing

ARENA_GUIDE = (2030, 100074, (56, 59))      # 薩姆吉爾村 1000 的鬥技場嚮導：npcId、scriptId、位置
ARENA_SHOP = (2043, (16, 5))                # 競技場 130 道具屋店員、站的格子
ARENA_EXIT = (2035, 100075, (55, 10))       # 競技場回村的 NPC
ITEM_ID, SHOP_INDEX, PRICE = 1278, 5, 500   # 阿薩Lv5金幣（地）：預設值，可用參數換
VILLAGE_POINT = 1                           # Transfer：薩姆吉爾村，落點 (92,99)
# 鬥技場道具屋賣的五隻屬性精靈 Lv5：買到哪一個由 itemShopIndex 決定，而**格位從 0 還是 1 起算沒有實機驗證過**
# （`doc/autoplay/flows/buy-attribute-spirit.md` 待辦）。0 起算：4＝阿薩(地)、5＝阿昆尼斯(水)。
# 所以買完不要只等自己想要的那個 itemId，要看伺服器實際推回哪一個，才知道基準。
ARENA_SPIRITS = {1278: "阿薩Lv5金幣（地的精靈Lv5）", 1279: "阿昆尼斯Lv5金幣（水的精靈Lv5）",
                 1280: "芙雷美雅Lv5金幣（火的精靈Lv5）", 1281: "溫蒂妮Lv5的金幣（風的精靈Lv5）",
                 1271: "阿薩Lv4豎琴", 1272: "阿昆尼斯Lv4豎琴", 1273: "芙雷美雅Lv4豎琴", 1274: "溫蒂妮Lv4豎琴"}
# 屬性 → (道具屋 NPC, 站的格子, 第幾格（1 起算）, itemId)。競技場兩間道具屋（Npc.json npcArgs，全部 500 石幣、最便宜）：
# 2043 @ (16,3) 賣地／水、2044 @ (43,3) 賣火／風；櫃檯 y=4 是牆，站在 y=5 隔兩格點（2043 實機走過 (16,5)）。
ARENA_BY_ELEMENT = {"地": (2043, (16, 5), 5, 1278), "水": (2043, (16, 5), 6, 1279),
                    "火": (2044, (43, 5), 5, 1280), "風": (2044, (43, 5), 6, 1281)}
ELEMENT_ATTRS = (("earth", "地"), ("water", "水"), ("fire", "火"), ("wind", "風"))


def element_of(attr: dict) -> Optional[str]:
    """人物屬性（RoleAttr earth／water／fire／wind，每點 ×10）裡最高的那一種；全是 0 回 None。"""
    best = max(ELEMENT_ATTRS, key=lambda kv: attr.get(kv[0]) or 0)
    return best[1] if (attr.get(best[0]) or 0) > 0 else None
STONE_VOUCHER, STONE_VOUCHER_SCRIPT, STONE_VOUCHER_VALUE = 90336, 1033, 20000   # 石幣2萬
# 醫院道具商店（NPC 10065 @ 1005 (24,57)）：競技場沒賣的東西要來這裡，價格一律 20000 石幣。
# 格位同樣 1 起算（2026-10-22 實機確認，見 LESSONS）：繆雷金幣(極光) 2179 是第 7 格。
HOSPITAL_SHOP = (10065, (24, 60))           # 店員、最近可站的格子（櫃檯隔一排，距離 3 格）
HOSPITAL_ITEMS = {2179: "繆雷金幣（極光Lv1）", 2181: "阿薩Lv5金幣（地的精靈Lv5）",
                  2182: "阿昆尼斯Lv5金幣（水的精靈Lv5）"}
HOSPITAL_WARP = (1000, 80, 66)              # 村裡踩這格進醫院 1005


class Stop(RuntimeError):
    pass


class Trip:
    def __init__(self, client, log: Callable[[str], None] = print, walk_speed: float = 0.35) -> None:
        self.c, self.log, self.walk_speed = client, log, walk_speed
        self.pos: Optional[tuple] = None
        self.map: Optional[int] = None
        self.stone: Optional[int] = None

    # ---- 事件 ----
    def _mine(self, e) -> Optional[dict]:
        if e.get("name") != "Role_RoleInfo":
            return None
        d = e.get("data") or {}
        a = d.get("roleAttr") if isinstance(d.get("roleAttr"), dict) else d
        if a.get("rid") not in (None, self.c.rid):
            return None
        if "map" in a:
            self.map = a["map"]
        if "stone" in a:
            self.stone = a["stone"]
        p = a.get("nowPos") or {}
        if "x" in p:
            self.pos = (p["x"], p["y"])
        return a

    def wait(self, what: str, pred, timeout: float = 15.0):
        def on(e):
            self._mine(e)
            if e.get("kind") == "error":
                raise Stop(f"{what}：伺服器回錯誤 {e.get('data')}")
        e = self.c.wait_for(pred, timeout, on_event=on)
        if e is None:
            raise Stop(f"{what}：{timeout:.0f} 秒內沒等到封包")
        return e

    def wait_map(self, what: str, mapid: int):
        self.wait(what, lambda e: (self._mine(e) or {}).get("map") == mapid and self.map == mapid)
        time.sleep(2.0)

    # ---- 動作 ----
    def transfer(self, point: int, mapid: int) -> None:
        r = self.c.request("Transfer_TransferTo", {"point": point})
        if not r.get("isOK"):
            raise Stop(f"傳送 {point} 被拒：{r}")
        self.wait_map(f"傳送到 {mapid}", mapid)

    def walk_near(self, grid, target, radius: int = 2) -> None:
        if self.pos is None:
            raise Stop("不知道目前位置")
        path = pathing.near(grid, self.pos, target, radius) if radius else pathing.astar(grid, self.pos, target)
        if not path:
            raise Stop(f"{self.pos} 到 {target} 找不到路")
        self.c.send("Role_RoleWalk", pathing.walk_payload(self.map, path))
        self.log(f"走 {len(path) - 1} 步：{self.pos} → {path[-1]}")
        time.sleep(max(1.0, (len(path) - 1) * self.walk_speed))
        self.pos = path[-1]

    def npc(self, npc_id: int, **arg) -> None:
        """伺服器**不回** `Npc_NpcInteract` 的 response（2026-10-22 實機：request 逾時，
        和任務執行器踩到的是同一件事）→ 只送，結果看後面等的推送（換圖／道具進背包）。"""
        self.c.send("Npc_NpcInteract", {"npcId": npc_id, "npcArg": arg})

    # ---- 戰鬥 ----
    def stop_battle(self, timeout: float = 20.0) -> None:
        """任何狀態變更前先停戰鬥（PLAYBOOK 規則 3）：戰鬥中傳送會被拒（30749 非正常狀態不能傳送）。
        關原地遇敵後等 `Role_RoleInfo.status` 回到 0；等不到就停。"""
        self.c.send("Role_UnLoopBattle", {})
        end = time.time() + timeout
        while time.time() < end:
            e = self.c.wait_for(lambda x: x.get("name") == "Role_RoleInfo", 3.0, on_event=self._mine)
            if e is not None:
                a = (e.get("data") or {}).get("roleAttr") or {}
                if a.get("rid") in (None, self.c.rid) and a.get("status") == 0:
                    self.log("已停戰鬥")
                    time.sleep(1.0)
                    return
        self.log("沒等到「不在戰鬥」的回報，仍繼續（傳送被拒就會停）")

    # ---- 隊伍 ----
    def dismiss_team(self) -> None:
        """進競技場要求不在隊伍（`noTeam`，PLAYBOOK §5「要先拆隊」）。
        `Team_TeamDismiss` 沒有 response，只送；隊員可能不在線上、不一定會推 `Team_TeamInfo`，
        所以等不到也繼續（真的沒拆成，下一步進場會等不到換圖而停）。"""
        self.c.send("Team_TeamDismiss", {})
        self.log("已送出解散隊伍（進競技場要求不在隊伍）")
        time.sleep(2.0)

    def leave_team(self) -> None:
        """隊員自己離隊。組隊時**只有隊長能傳送**（隊員跟著走），隊員自己送 `Transfer_TransferTo`
        會被拒（2026-10-22 實機：已停戰鬥仍回 isOK False）。買完東西後再由練功程式重新組隊。"""
        self.c.send("Team_TeamLeave", {})
        self.log("已送出離隊（隊員自己移動要先離隊）")
        time.sleep(2.0)

    # ---- 錢 ----
    def ensure_stone(self, need: int) -> None:
        """石幣不夠就從個人倉庫取出「石幣2萬」並用掉。第三組開局時把它存進倉庫了（綁定道具存倉規則）。"""
        # 登入時伺服器就推過 Role_RoleInfo（含 stone），但那些事件還在佇列裡：先處理完再判斷，
        # 否則 self.stone 還是 None，會誤以為沒錢而去翻倉庫（2026-10-22 實機）。
        self.c.drain(on_event=self._mine)
        if self.stone is None:
            self.wait("讀取石幣", lambda e: self._mine(e) is not None and self.stone is not None, timeout=8.0)
        if self.stone is not None and self.stone >= need:
            self.log(f"石幣 {self.stone:,} 足夠（需要 {need:,}）")
            return
        r = self.c.request("ItemHandle_GetNewItemDepotInfo", {"page": 1, "isPublic": False}) or {}
        v = next((i for i in (r.get("itemInfoList") or []) if i.get("itemId") == STONE_VOUCHER), None)
        if v is None:
            raise Stop(f"石幣只有 {self.stone}，需要 {need:,}，個人倉庫也沒有石幣2萬")
        g = self.c.request("ItemHandle_GetItemToNewDepot",
                           {"page": 1, "isPublic": False, "uniqueIndex": v["uniqueIndex"], "count": 1}) or {}
        if not g.get("isOK"):
            raise Stop(f"從倉庫取出石幣2萬失敗：{g}")
        got = self.wait("取出石幣2萬", lambda e: e.get("name") == "Item_ItemInfo" and any(
            i.get("itemId") == STONE_VOUCHER and i.get("itemStorge", 0) == 0
            for i in (e.get("data") or {}).get("itemInfo") or []))
        idx = next(i["itemIndex"] for i in got["data"]["itemInfo"] if i.get("itemId") == STONE_VOUCHER)
        before = self.stone or 0
        # Item_ItemUse 伺服器不回 response（LESSONS #45），只送，看 stone 變多
        self.c.send("Item_ItemUse", {"itemIndex": idx, "itemNum": 1,
                                     "itemScriptId": STONE_VOUCHER_SCRIPT, "data": [], "useMuch": False})
        self.wait("用掉石幣2萬", lambda e: self._mine(e) is not None and (self.stone or 0) > before)
        self.log(f"用掉石幣2萬：石幣 {before:,} → {self.stone:,}")
        if (self.stone or 0) < need:
            raise Stop(f"用完還是只有 {self.stone:,} 石幣，需要 {need:,}")

    # ---- 固定路徑（室內 A* 算不出路，用實機走過的格子）----
    def walk_path(self, mapid: int, path) -> None:
        self.c.send("Role_RoleWalk", pathing.walk_payload(mapid, list(path)))
        time.sleep(max(1.0, (len(path) - 1) * self.walk_speed))
        self.pos = tuple(path[-1])
        self.log(f"走 {len(path) - 1} 步 → {self.pos}")

    # ---- 醫院道具商店 ----
    def hospital_buy(self, back_point: int, back_map: int, want_item: int,
                     shop_index: int, price: int = 20000) -> None:
        from .headless_party import HEAL_PATH_HOSPITAL, HEAL_PATH_VILLAGE
        self.stop_battle()
        self.leave_team()
        self.ensure_stone(price)
        self.transfer(VILLAGE_POINT, 1000)
        self.walk_path(1000, HEAL_PATH_VILLAGE)
        self.c.request("Role_RoleWarp", {"map": HOSPITAL_WARP[0], "x": HOSPITAL_WARP[1], "y": HOSPITAL_WARP[2]})
        self.wait_map("進醫院", 1005)
        # 固定路徑走到櫃檯前；(24,60) 就在補血路徑上，走到那裡就停
        upto = HEAL_PATH_HOSPITAL[:HEAL_PATH_HOSPITAL.index(HOSPITAL_SHOP[1]) + 1]
        self.walk_path(1005, upto)
        self.npc(HOSPITAL_SHOP[0], scriptId=300000, itemShopIndex=shop_index, itemBuyCount=1)
        got = self.wait(f"買第 {shop_index} 格", lambda e: e.get("name") == "Item_ItemInfo" and any(
            i.get("itemId") in HOSPITAL_ITEMS and i.get("itemStorge", 0) == 0
            for i in (e.get("data") or {}).get("itemInfo") or []))
        bought = next(i for i in got["data"]["itemInfo"] if i.get("itemId") in HOSPITAL_ITEMS)
        got_id, index = bought["itemId"], bought["itemIndex"]
        self.log(f"買到 {HOSPITAL_ITEMS[got_id]}（itemId {got_id}），背包第 {index} 格；石幣剩 {self.stone}")
        if got_id != want_item:
            raise Stop(f"買到的是 {HOSPITAL_ITEMS[got_id]}，不是要的 "
                       f"{HOSPITAL_ITEMS.get(want_item, want_item)}；格位對不上，先停下來")
        r = self.c.request("ItemHandle_itemEquipOn", {"itemIndex": index})
        if not r.get("isOK"):
            raise Stop(f"穿戴失敗：{r}")
        self.log(f"已穿上 {HOSPITAL_ITEMS[got_id]}")
        self.transfer(back_point, back_map)
        self.log("回到練功點")

    # ---- 競技場買屬性精靈（先買，等級夠才穿；使用者 2026-11-04）----
    def arena_buy(self, element: str, equip: bool) -> tuple:
        """去競技場買這個屬性的 Lv5 金幣（500 石幣），equip＝當場穿上；走出競技場回到村子就結束
        （回練功由呼叫端做）。回傳 (itemId, 背包 itemIndex, 有沒有穿)。"""
        shop, stand, shop_index, want = ARENA_BY_ELEMENT[element]
        self.ensure_stone(PRICE)                     # 會先 drain：常駐連線的事件佇列從登入就累積，舊事件不能拿來判斷
        self.pos = None                              # 換圖後的位置只信換圖之後的推送（不沿用練功地圖的座標）
        self.transfer(VILLAGE_POINT, 1000)
        if self.pos is None:
            self.pos = (92, 99)
        self.walk_near(pathing.load_grid(1000), ARENA_GUIDE[2])
        self.dismiss_team()
        self.pos = None
        self.npc(ARENA_GUIDE[0], scriptId=ARENA_GUIDE[1])
        self.wait_map("進競技場", 130)
        if self.pos is None:
            self.pos = (52, 19)
        arena = pathing.load_grid(130)
        self.walk_near(arena, stand, radius=0)
        before = self.stone
        self.npc(shop, scriptId=300000, itemShopIndex=shop_index, itemBuyCount=1)
        got = self.wait(f"買第 {shop_index} 格", lambda e: e.get("name") == "Item_ItemInfo" and any(
            i.get("itemId") in ARENA_SPIRITS and i.get("itemStorge", 0) == 0
            for i in (e.get("data") or {}).get("itemInfo") or []))
        bought = next(i for i in got["data"]["itemInfo"] if i.get("itemId") in ARENA_SPIRITS)
        got_id, index = bought["itemId"], bought["itemIndex"]
        self.log(f"買到 {ARENA_SPIRITS[got_id]}（itemId {got_id}），背包第 {index} 格；石幣 {before} → {self.stone}")
        if got_id != want:
            raise Stop(f"買到的是 {ARENA_SPIRITS[got_id]}，不是要的 {ARENA_SPIRITS[want]}；格位對不上，先停下來不要再買")
        worn = False
        if equip:
            r = self.c.request("ItemHandle_itemEquipOn", {"itemIndex": index})
            if not r.get("isOK"):
                raise Stop(f"穿戴失敗：{r}")
            worn = True
            self.log(f"已穿上 {ARENA_SPIRITS[got_id]}")
        self.walk_near(arena, ARENA_EXIT[2])
        self.npc(ARENA_EXIT[0], scriptId=ARENA_EXIT[1])
        self.wait_map("回到村子", 1000)
        return got_id, index, worn

    # ---- 全程 ----
    def run(self, back_point: int, back_map: int, want_item: int = ITEM_ID,
            shop_index: int = SHOP_INDEX, price: int = PRICE) -> None:
        self.stop_battle()
        self.ensure_stone(price)
        self.transfer(VILLAGE_POINT, 1000)
        if self.pos is None:
            self.pos = (92, 99)
        self.walk_near(pathing.load_grid(1000), ARENA_GUIDE[2])
        self.dismiss_team()
        self.npc(ARENA_GUIDE[0], scriptId=ARENA_GUIDE[1])
        self.wait_map("進競技場", 130)
        if self.pos is None:
            self.pos = (52, 19)
        arena = pathing.load_grid(130)
        self.walk_near(arena, ARENA_SHOP[1], radius=0)
        self.npc(ARENA_SHOP[0], scriptId=300000, itemShopIndex=shop_index, itemBuyCount=1)
        # **不要只等想買的那個 itemId**：格位基準沒驗證過，等任何一個精靈進背包，再看實際買到什麼
        got = self.wait(f"買第 {shop_index} 格", lambda e: e.get("name") == "Item_ItemInfo" and any(
            i.get("itemId") in ARENA_SPIRITS and i.get("itemStorge", 0) == 0
            for i in (e.get("data") or {}).get("itemInfo") or []))
        bought = next(i for i in got["data"]["itemInfo"] if i.get("itemId") in ARENA_SPIRITS)
        got_id, index = bought["itemId"], bought["itemIndex"]
        self.log(f"買到 {ARENA_SPIRITS[got_id]}（itemId {got_id}），背包第 {index} 格；石幣剩 {self.stone}")
        if got_id != want_item:
            raise Stop(f"買到的是 {ARENA_SPIRITS[got_id]}，不是要的 {ARENA_SPIRITS.get(want_item, want_item)}"
                       f"；itemShopIndex={shop_index} 的格位基準和設定表對不上，先停下來不要再買")
        r = self.c.request("ItemHandle_itemEquipOn", {"itemIndex": index})
        if not r.get("isOK"):
            raise Stop(f"穿戴失敗：{r}")
        self.log(f"已穿上 {ARENA_SPIRITS[got_id]}")
        self.walk_near(arena, ARENA_EXIT[2])
        self.npc(ARENA_EXIT[0], scriptId=ARENA_EXIT[1])
        self.wait_map("回到村子", 1000)
        self.transfer(back_point, back_map)
        self.log("回到練功點")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.trip_spirit")
    ap.add_argument("--back-point", type=int, default=19, help="回去的練級 Transfer point（19 五兄弟洞窟）")
    ap.add_argument("--back-map", type=int, default=31706)
    ap.add_argument("--profile", default="2", help="第幾組帳號（環境變數 BOT_ACCOUNT_<組>）")
    ap.add_argument("--rid", type=int, default=None, help="要操作的角色 rid（不給就用該組第一個角色）")
    ap.add_argument("--item-id", type=int, default=ITEM_ID,
                    help=f"要買哪一個：{ {k: v for k, v in ARENA_SPIRITS.items()} }")
    ap.add_argument("--shop-index", type=int, default=SHOP_INDEX, help="商店格位（**1 起算**，實機確認）")
    ap.add_argument("--route", choices=("arena", "hospital"), default="arena",
                    help="arena 競技場（500 石幣）／hospital 薩姆吉爾醫院（20000，競技場沒賣的才來）")
    args = ap.parse_args(argv)
    cfg = load(profile=args.profile)
    if args.rid is not None:
        cfg.rid = args.rid
    c, _ = open_client(cfg)
    try:
        t = Trip(c, log=lambda m: print(m, flush=True))
        if args.route == "hospital":
            t.hospital_buy(args.back_point, args.back_map, args.item_id, args.shop_index)
        else:
            t.run(args.back_point, args.back_map, want_item=args.item_id, shop_index=args.shop_index)
        return 0
    except Stop as e:
        print(f"停止：{e}", flush=True)
        return 1
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
