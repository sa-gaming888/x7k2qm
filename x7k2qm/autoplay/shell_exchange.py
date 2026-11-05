"""千年龜的殼：回村時存公共倉庫、共倉滿 10 由隊長去薩村寵物店兌換石幣券並用掉；背包快滿時原地存。

使用者 2026-10-24：「1. 如果在波拉島打到龜殼存共倉　2. 滿 10 隊長回村順便賣卷」，
同日改：「改一下，回村再檢查龜殼存倉」、「快滿 停止禦敵 非戰鬥狀態 存共倉」「不然太浪費時間」：
- 平常撿到殼**不停、不存**，留在背包。
- **背包快滿**（任一隻空格 ≤2 而且背包有殼）：練功地圖**原地**停遇敵、等不在戰鬥、存共倉、開回遇敵（不回村）。
- **本來就要回村時**（補血）：順便存倉；共倉 ≥10 就隊長去寵物店兌換 4013、用券用到完，再照原流程回練功地點。
  11-05 起（使用者「每次回村補血都要檢查道具欄或個倉、共倉是否有龜殼可以賣」）：每次都查三處——
  個倉的殼先領回背包、背包的存共倉、共倉每次都數（以前背包沒殼就不查共倉）。
  同日「倉庫或道具欄如果有石幣道具，一律打開使用」：個倉、共倉的石幣券也領回背包、跟背包的一起用掉。
- 共倉滿 10 **不特地回村**，等下次本來就要回村。
流程與協定見 `doc/autoplay/flows/headless-party.md` §7、`doc/autoplay/game/item-rules.md`。

這是**附加功能**：任何一步失敗都只記日誌、這次放棄、照原本流程回練功（不停掉練功）；
**連續失敗 3 次**就停用到下次啟動。只有原地存殼後「遇敵開不回來」回報 fatal，交給主迴圈停止（規範第 2 點）。
"""

import time
from typing import Dict, List, Optional, Tuple

SHELL = 11879                              # 千年龜的殼（可疊，佔 1 格）
# 石幣券：Item.json 裡 scripts 是 1033 的全部 13 種（使用者 11-05「石幣道具一律打開使用」；以前只有兌換拿到的前 3 種）。
# 90336 石幣2萬是迎新禮盒送的（trip_spirit 缺錢時也會從個倉拿來用），一樣直接用掉。
STONE_TICKETS = (90338, 90811, 90810,
                 90335, 90336, 90337, 90339, 90340, 90373, 90389, 90834, 90837, 91557)
TICKET_SCRIPT = 1033                       # 道具表查不到 scripts 時的預設值
EXCHANGE_ID = 4013                         # Item_NewExchangeItem：10 殼 → 石幣5萬券 90338
SHELLS_PER_EXCHANGE = 10
BAG_SIZE = 15                              # 背包格數（同 quest/headless.py）
FULL_FREE = 2                              # 背包空格 ≤ 這個數＝快滿 → 原地停遇敵存殼
STORE_EVERY = 600.0                        # 原地存殼最短間隔（秒）：存不進去／背包被別的東西塞滿時不會每圈都停
MAX_DEPOT_PAGES = 3                        # 數公共倉庫最多查幾頁（遇到空頁就停）
MAX_EXCHANGES = 3                          # 一次回村最多換幾輪（每輪 10 殼）
MAX_TICKET_USES = 30                       # 一隻角色一次回村最多用幾張券（上限保護）
MAX_FAILS = 3                              # 連續失敗幾次就停用

VILLAGE = 1000                             # 薩姆吉爾村
VILLAGE_LANDING = (92, 99)                 # Transfer_TransferTo{point:1} 的落點（補血路徑的起點）
SHOP_DOOR = (68, 72)                       # 寵物店門口（踩 Role_RoleWarp 進 1003）
SHOP_MAP = 1003
# 1003 沒有 mapgrid：照 2026-10-24 錄檔走的格子。終點 (11,15) 在回收員 10315 (11,13) 2 格內
# （Mac 站 8 格外被拒 result:4）
SHOP_PATH = [(15, 21), (15, 20), (15, 19), (14, 18), (14, 17), (13, 16), (12, 16), (11, 15)]

OK, FAIL, FATAL, SKIP = "ok", "fail", "fatal", "skip"


class ShellExchange:
    def __init__(self, party, enabled: bool = True) -> None:
        self.p = party
        self.enabled = enabled
        self.fails = 0                     # 連續失敗次數（成功歸零；到 MAX_FAILS 停用）
        self.last_store = 0.0              # 最後一次「試過」原地存殼的時間（不論成敗）
        self.public_tickets: Dict[int, List[Tuple[int, int, int]]] = {}   # depot_shells 同一次查到的共倉石幣券
        self.ticket_stuck: set = set()     # 這次回村用券沒看到數量減少的角色（不再重送）
        self._village_path: Optional[List[Tuple[int, int]]] = None

    # ---- 背包 ----
    def _count(self, name: str, ids) -> int:
        """持有 party.lock 時也能呼叫（直接讀 items，不呼叫 bag()；wait() 的條件會持鎖呼叫）。"""
        return sum(it.get("overlay", 1) for it in list(self.p.items.get(name, {}).values())
                   if it.get("itemId") in ids and it.get("itemStorge", 0) == 0 and it.get("overlay", 1) > 0)

    def bag_shells(self) -> Dict[str, int]:
        with self.p.lock:
            out = {n: self._count(n, (SHELL,)) for _, n in self.p.party}
        return {n: c for n, c in out.items() if c > 0}

    def _done(self, result: str, why: str = "") -> str:
        if result == OK:
            self.fails = 0
        elif result in (FAIL, FATAL):
            self.fails += 1
            self.p.log(f"龜殼：{why}（連續失敗 {self.fails} 次）")
            if self.fails >= MAX_FAILS and self.enabled:
                self.enabled = False
                self.p.log(f"龜殼：連續失敗 {MAX_FAILS} 次，存共倉／兌換停用到下次啟動")
        return result

    # ---- 回村時（補血後、主動回村）----
    def on_village(self) -> str:
        """已停戰鬥、人在薩村（或村裡的醫院）時呼叫：存殼 → 共倉 ≥10 就去寵物店兌換 → 全隊用掉石幣券。
        **不負責傳回練功地點**（呼叫的人照原本流程傳回去）。回傳 ok／fail／skip，不丟例外。"""
        if not self.enabled:
            return SKIP
        p = self.p
        try:
            # 使用者 11-05「每次回村補血都要檢查道具欄或個倉、共倉是否有龜殼可以賣」
            # 「倉庫或道具欄如果有石幣道具，一律打開使用」。順序（review 後）：
            # 1. 背包原本的券先用掉（券不能疊、一張一格，先空出格子）
            # 2. 每隻個倉：一疊一疊領，券領了馬上用；背包沒空格就留在倉庫下次（不算失敗）；一隻失敗不影響別隻
            # 3. 背包的殼存共倉 → 4. 共倉查一次：券隊長一張一張領、用；殼 ≥10 就換 → 5. 剩下的券用完
            ok, acts = True, 0
            self.ticket_stuck = set()
            for _, name in p.party:
                acts += self.use_tickets(name)
            for _, name in p.party:
                good, n = self.pull_depot(name, False)
                ok, acts = ok and good, acts + n
            had = self.bag_shells()
            if had:
                ok = self.store_all() and ok
                acts += 1
            dep = self.depot_shells()
            if dep is None:
                ok = False
            else:
                total, stacks = dep
                good, n = self.pull_public_tickets()
                ok, acts = ok and good, acts + n
                if total >= SHELLS_PER_EXCHANGE:
                    ok = self.exchange_at_shop(total, stacks) and ok
                    acts += 1
            for _, name in p.party:                            # 兌換拿到的券、還沒用完的也用掉
                acts += self.use_tickets(name)
                if self.tickets(name):                         # 沒用完（數量沒減少就停了）也算這次失敗
                    ok = False
            if ok and not acts:
                return SKIP                                   # 三處都沒殼、也沒券：只查過倉庫，沒送任何動作
        except Exception as e:  # noqa: BLE001
            p.log(f"龜殼：回村處理例外 {type(e).__name__}: {e}")
            ok = False
        return self._done(OK if ok else FAIL, "回村存倉／兌換沒做完，照常回練功地點")

    # ---- 背包快滿：原地存（使用者 2026-10-24「快滿 停止禦敵 非戰鬥狀態 存共倉」「不然太浪費時間」）----
    def bag_free(self, name: str) -> int:
        """背包空格（BAG_SIZE 15 減掉背包裡有東西的格子；身上裝備不算）。持鎖時也能呼叫。"""
        used = sum(1 for it in list(self.p.items.get(name, {}).values())
                   if it.get("itemId") and it.get("overlay", 1) > 0 and it.get("itemStorge", 0) == 0)
        return BAG_SIZE - used

    def full_with_shells(self) -> List[str]:
        """背包快滿（空格 ≤ FULL_FREE）而且背包裡有殼的角色。"""
        with self.p.lock:
            return [n for _, n in self.p.party
                    if self.bag_free(n) <= FULL_FREE and self._count(n, (SHELL,)) > 0]

    def store_due(self, now: Optional[float] = None) -> bool:
        """要不要在練功地圖原地存殼：功能開著、有人背包快滿且有殼、距上次試過 ≥ STORE_EVERY（存不進去時不會每圈都停）。"""
        now = time.time() if now is None else now
        return self.enabled and now - self.last_store >= STORE_EVERY and bool(self.full_with_shells())

    def store_in_place(self, now: Optional[float] = None) -> str:
        """原地：關遇敵、等到真的不在戰鬥 → 全隊把殼存進公共倉庫 → 開回遇敵。**不回村、不兌換**
        （共倉滿 10 也等下次本來就要回村時再換）。遇敵開不回來 → FATAL；其他失敗 → FAIL（繼續練）。"""
        self.last_store = time.time() if now is None else now
        p = self.p
        p.log(f"龜殼：背包快滿（{'、'.join(self.full_with_shells())}），原地停遇敵存公共倉庫")
        if not p.pause():
            if not p.set_loop(True):                         # 同簽到：關不掉就把遇敵開回來，這次不存
                p.log("停止：龜殼：遇敵開不回來")
                return FATAL
            return self._done(FAIL, "停不了戰鬥，這次不存")
        try:
            ok = self.store_all()
        except Exception as e:  # noqa: BLE001
            p.log(f"龜殼：原地存倉例外 {type(e).__name__}: {e}")
            ok = False
        if not p.set_loop(True):
            p.log("停止：龜殼：存完遇敵開不回來")
            return FATAL
        return self._done(OK if ok else FAIL, "有殼存不進公共倉庫（可能滿了）")

    # ---- 存公共倉庫 ----
    def store_all(self) -> bool:
        """每隻把背包裡的殼存進公共倉庫第 1 頁。存不進去（isOK false、逾時）記日誌就停（不換別隻重送）。"""
        p = self.p
        for _, name in p.party:
            for it in p.bag(name):
                if it["itemId"] != SHELL:
                    continue
                try:
                    r = p.clients[name].request("ItemHandle_SaveItemToNewDepot",
                                                {"page": 1, "isPublic": True, "itemIndex": it["itemIndex"]}) or {}
                except Exception as e:  # noqa: BLE001
                    r = {"error": f"{type(e).__name__}: {e}"}
                if not r.get("isOK"):
                    p.log(f"龜殼：{name} 的 {it.get('overlay', 1)} 個存不進公共倉庫（可能滿了）：{r}")
                    return False
                p.log(f"龜殼：{name} 存 {it.get('overlay', 1)} 個進公共倉庫")
        return True

    # ---- 倉庫：查、領（使用者 11-05「每次回村都要檢查個倉、共倉」「石幣道具一律打開使用」）----
    def _pages(self, name: str, public: bool) -> int:
        """這隻開了幾頁倉庫（RoleAttr itemDepotPlace／pItemDepotPlace；不知道當 1，最多 MAX_DEPOT_PAGES）。
        沒開的頁伺服器回 isOK false，不是空頁：11-05 五組都只開 1 頁，每次回村查第 2 頁被拒 →
        連續失敗 3 次，龜殼存倉／兌換整個停用（查詢 API /depot 一樣照這兩個欄位定頁數）。"""
        with self.p.lock:
            v = (self.p.attrs.get(name) or {}).get("pItemDepotPlace" if public else "itemDepotPlace")
        return max(1, min(v if isinstance(v, int) and v > 0 else 1, MAX_DEPOT_PAGES))

    def _scan(self, name: str, public: bool, ids) -> Optional[Dict[int, List[Tuple[int, int, int]]]]:
        """這隻查倉庫一次（第 1 頁起，遇空頁停，只查開了的頁）：{itemId: [(page, uniqueIndex, 數量)]}，只收 ids。
        查詢逾時、例外、isOK false → 回 None（不能當成空的：共倉明明有殼卻記成 0 個，規範第 2 點）。"""
        out: Dict[int, List[Tuple[int, int, int]]] = {}
        where = "公共倉庫" if public else "個人倉庫"
        client = self.p.clients[name]
        for page in range(1, self._pages(name, public) + 1):
            try:
                r = client.request("ItemHandle_GetNewItemDepotInfo", {"page": page, "isPublic": public})
            except Exception as e:  # noqa: BLE001
                r = {"error": f"{type(e).__name__}: {e}"}
            if not r or r.get("isOK") is False or "error" in r:
                self.p.log(f"龜殼：查{where}第 {page} 頁失敗：{r}")
                return None
            items = r.get("itemInfoList") or []
            if not items:
                break
            for it in items:
                if it.get("itemId") in ids and it.get("uniqueIndex"):
                    out.setdefault(it["itemId"], []).append((page, it["uniqueIndex"], it.get("overlay", 1)))
        return out

    def _has_room(self, name: str, item_id: int) -> bool:
        """領這一疊之前：背包要有空格；殼另外可以疊進背包已有的那疊。持鎖時也能呼叫。"""
        return self.bag_free(name) > 0 or (item_id == SHELL and self._count(name, (SHELL,)) > 0)

    def _take(self, name: str, public: bool, item_id: int, stack: Tuple[int, int, int]) -> bool:
        """這隻從倉庫領一疊回背包，等背包數量增加才算。"""
        p, (page, uid, n) = self.p, stack
        with p.lock:
            before = self._count(name, (item_id,))
        try:
            g = p.clients[name].request("ItemHandle_GetItemToNewDepot",
                                        {"page": page, "isPublic": public, "uniqueIndex": uid, "count": n}) or {}
        except Exception as e:  # noqa: BLE001
            g = {"error": f"{type(e).__name__}: {e}"}
        ok = bool(g.get("isOK")) and p.wait(lambda: self._count(name, (item_id,)) >= before + n, 5)
        where = "公共倉庫" if public else "個人倉庫"
        p.log(f"龜殼：{name} 從{where}取出 {item_id}×{n}" + ("" if ok else f" 沒確認：{g}"))
        return ok

    def _take_all(self, name: str, public: bool, found: Dict[int, List[Tuple[int, int, int]]]) -> Tuple[bool, int]:
        """一疊一疊領；券領到就馬上用掉（一張一格，不先全部堆進背包）。背包沒空格就把剩下的留在倉庫等下次，
        不算失敗。回傳 (有沒有出錯, 做了幾個動作)。"""
        acts = 0
        for item_id, stacks in found.items():
            for stack in stacks:
                with self.p.lock:
                    room = self._has_room(name, item_id)
                if not room:
                    self.p.log(f"龜殼：{name} 背包沒空格，{'公共' if public else '個人'}倉庫剩下的殼／券留到下次")
                    return True, acts
                if not self._take(name, public, item_id, stack):
                    return False, acts
                acts += 1
                if item_id in STONE_TICKETS:
                    acts += self.use_tickets(name)
        return True, acts

    def pull_depot(self, name: str, public: bool) -> Tuple[bool, int]:
        """這隻把個倉（public=False）的殼、石幣券領回背包（券順便用掉）。回傳 (有沒有出錯, 做了幾個動作)。"""
        found = self._scan(name, public, (SHELL, *STONE_TICKETS))
        if found is None:
            return False, 0
        return self._take_all(name, public, found)

    def depot_shells(self) -> Optional[Tuple[int, List[Tuple[int, int, int]]]]:
        """隊長查公共倉庫一次。回傳 (殼總數, [(page, uniqueIndex, 數量)])，查詢失敗回 None；
        同一次查到的石幣券記在 `public_tickets`，給 pull_public_tickets 用（不用再查一次）。"""
        found = self._scan(self.p.party[0][1], True, (SHELL, *STONE_TICKETS))
        if found is None:
            self.public_tickets = {}
            return None
        stacks = found.pop(SHELL, [])
        self.public_tickets = found
        total = sum(n for _, _, n in stacks)
        n_tickets = sum(n for v in found.values() for _, _, n in v)
        self.p.log(f"龜殼：公共倉庫共 {total} 個" + (f"、石幣券 {n_tickets} 張" if n_tickets else ""))
        return total, stacks

    def pull_public_tickets(self) -> Tuple[bool, int]:
        """隊長把上次 depot_shells 查到的共倉石幣券一張一張領回背包、馬上用掉。"""
        found, self.public_tickets = self.public_tickets, {}
        return self._take_all(self.p.party[0][1], True, found)

    # ---- 兌換 ----
    def withdraw(self, want: int, stacks: List[Tuple[int, int, int]]) -> int:
        """隊長從公共倉庫取出 want 個殼；回傳隊長背包實際增加的數量（看 Item_ItemInfo）。"""
        p, lead = self.p, self.p.party[0][1]
        with p.lock:
            before = self._count(lead, (SHELL,))
        need = want
        for page, uid, n in stacks:
            if need <= 0:
                break
            k = min(n, need)
            g = p.leader().request("ItemHandle_GetItemToNewDepot",
                                   {"page": page, "isPublic": True, "uniqueIndex": uid, "count": k}) or {}
            if not g.get("isOK"):
                p.log(f"龜殼：從公共倉庫取出 {k} 個失敗：{g}")
                break
            need -= k
        p.wait(lambda: self._count(lead, (SHELL,)) >= before + want - need, 5)
        with p.lock:
            got = self._count(lead, (SHELL,)) - before
        p.log(f"龜殼：隊長從公共倉庫取出 {got} 個")
        return got

    def village_path(self, start: Tuple[int, int]) -> List[Tuple[int, int]]:
        """薩村 start → 寵物店門口（A*，load_grid(1000) 有資料）。從落點出發的那條算一次就快取。"""
        from .pathing import astar, load_grid
        if start != VILLAGE_LANDING:
            return astar(load_grid(VILLAGE), start, SHOP_DOOR) or []
        if self._village_path is None:
            self._village_path = astar(load_grid(VILLAGE), VILLAGE_LANDING, SHOP_DOOR) or []
        return self._village_path

    def to_village(self) -> bool:
        """隊長 Transfer_TransferTo{point:1}，全隊一起到薩村落點。**只有隊長送**（隊員送會 tip 30746）。"""
        p = self.p
        r = p.leader().request("Transfer_TransferTo", {"point": 1}) or {}
        if not r.get("isOK") or not p.wait(lambda: all(p.s.member_maps.get(n) == VILLAGE for _, n in p.party), 15):
            p.log(f"龜殼：傳到薩姆吉爾村失敗 {r}")
            return False
        with p.lock:
            p.leader_pos = (VILLAGE,) + VILLAGE_LANDING
        return True

    def go_to_collector(self) -> bool:
        """（人在村裡）走到寵物店門 → Warp 進 1003 → 照錄檔路徑走到 (11,15)。
        已經在薩村而且知道隊長座標就直接走；否則（例如在醫院 1005）先傳到薩村落點。
        不能在薩村裡再傳薩村：傳到同一張圖會被拒（2026-10-24 實測）。"""
        p, lead = self.p, self.p.party[0][1]
        with p.lock:
            here, pos = p.s.leader_map, p.leader_pos
        if here == VILLAGE and pos and pos[0] == VILLAGE:
            start = (pos[1], pos[2])
        elif here == VILLAGE:
            p.log("龜殼：人在薩村但不知道隊長座標，走不到寵物店")
            return False
        elif self.to_village():
            start = VILLAGE_LANDING
        else:
            return False
        path = self.village_path(start)
        if not path:
            p.log(f"龜殼：算不出薩村 {start} 到寵物店的路")
            return False
        p.walk(VILLAGE, path)
        r = p.leader().request("Role_RoleWarp", {"map": VILLAGE, "x": SHOP_DOOR[0], "y": SHOP_DOOR[1]})
        if not p.wait(lambda: p.s.member_maps.get(lead) == SHOP_MAP, 8):
            p.log(f"龜殼：進不了寵物店 {r}")
            return False
        p.walk(SHOP_MAP, SHOP_PATH)
        return True

    def exchange(self, rounds: int) -> int:
        """開對話（NomalHandle type 6，只送不等）→ 送 rounds 次 4013。回傳成功幾次；result 不是 1 就停。"""
        p = self.p
        p.leader().send("Role_NomalHandle", {"type": 6, "extra": 0})
        done = 0
        for _ in range(rounds):
            r = p.leader().request("Item_NewExchangeItem", {"ID": EXCHANGE_ID}) or {}
            if r.get("result") != 1:
                p.log(f"龜殼：兌換 {EXCHANGE_ID} 被拒（result 4＝離回收員太遠）：{r}")
                break
            done += 1
        p.log(f"龜殼：兌換 {done} 次（10 殼 → 石幣5萬券）")
        return done

    def exchange_at_shop(self, total: int, stacks) -> bool:
        """隊長取出 10×輪數 → 走到回收員旁 → 兌換。回傳是否全部換成。"""
        p = self.p
        rounds = min(total // SHELLS_PER_EXCHANGE, MAX_EXCHANGES)
        got = self.withdraw(rounds * SHELLS_PER_EXCHANGE, stacks)
        rounds = min(rounds, got // SHELLS_PER_EXCHANGE)
        if rounds <= 0:
            p.log("龜殼：取不出 10 個，這次不兌換")
            return False
        if not self.go_to_collector():
            return False
        return self.exchange(rounds) == rounds

    # ---- 石幣券 ----
    def tickets(self, name: str) -> List[dict]:
        return [it for it in self.p.bag(name) if it["itemId"] in STONE_TICKETS]

    def use_tickets(self, name: str) -> int:
        """把這隻背包裡的石幣券**一張一張**用完。Item_ItemUse 沒有 response、實測一次只用掉 1 張 →
        每張送一次、等那格數量減少才送下一張；沒減少就停（不硬重送）。回傳用了幾張。"""
        p, used = self.p, 0
        if name in self.ticket_stuck:
            return 0
        table = p.item_table()
        stone0 = p.money.get(name, {}).get("stone")
        for _ in range(MAX_TICKET_USES):
            t = next(iter(self.tickets(name)), None)
            if not t:
                break
            idx, before = t["itemIndex"], t.get("overlay", 1)
            script = (table.get(t["itemId"], {}).get("scripts") or [TICKET_SCRIPT])[0]
            p.clients[name].send("Item_ItemUse", {"itemIndex": idx, "itemNum": 1, "itemScriptId": script,
                                                  "data": [], "useMuch": False})
            if not p.wait(lambda: p.items[name].get(idx, {}).get("overlay", 0) < before, 5):
                p.log(f"龜殼：{name} 用石幣券 {t['itemId']} 沒看到數量減少，停止用券")
                self.ticket_stuck.add(name)                   # 這次回村不再對他送（不硬重送）
                break
            used += 1
        if used:
            p.log(f"龜殼：{name} 用了 {used} 張石幣券，石幣 {stone0} → {p.money.get(name, {}).get('stone')}")
        return used
