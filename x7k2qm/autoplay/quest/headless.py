"""無畫面解任務：在無畫面五開的 5 條連線上，照設定表規格（spec.py）送封包，只操作隊長。

    python -X utf8 -m x7k2qm.autoplay.quest.headless --group 2017 --log captures/quests/headless.log

每一步：
1. 跳點：`Task_RequestNavigate`，**只在人不在那張圖時才送**（伺服器常自己傳送）。
2. 走到 NPC 旁 2 格內：A*（`pathing`）→ `Role_RoleWalk`，依路徑長度等足時間（不瞬移）。
3. `Npc_NpcInteract{npcId, scriptId, taskId}` → 等全隊 `Task_TaskInfo` 推進；戰鬥型（type 4）等打完。
出錯就停（使用者規則）：隊員狀態不一致、有人被打死（hp 剩 1）、背包空格不足、等不到回應。
戰鬥確認（PlayOver）由 headless_party 處理，這裡用它的 nap 等待。
"""

import argparse
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .spec import Step, load_steps, npc_maps

BAG_SIZE = 15
PET_MAX = 5                     # 身上最多帶 5 隻寵物
WALK_SPEED = 4.0                # 格/秒，保守估計（寧可多等，不要比真人快）
INTERACT_TRIES = 3              # 戰鬥型互動最多送幾次（打完遇敵的怪、任務沒推進才補送；LESSONS #116）
RESEND_GRACE = 5.0              # 打完一場後再等幾秒看任務推進，才當成「那場不是任務戰鬥」
BATTLE_HARD_CAP = 1800.0        # 戰鬥還在進行就一直看著，最多 30 分鐘（LESSONS #121）
HOP_BATTLE_WAIT = 150.0        # 跳點前／被拒後等原地遇敵那場打完，最多這麼久（LESSONS #169）


class QuestStop(RuntimeError):
    pass


class HeadlessQuest:
    def __init__(self, clients: Dict[str, object], leader: str, steps: List[Step],
                 npc_pos: Dict[int, Tuple[int, int, int]], log: Callable[[str], None] = print,
                 grid_fn: Optional[Callable[[int], object]] = None, idle: Optional[Callable[[float], None]] = None,
                 clock: Callable[[], float] = time.time, need_free: int = 1, battle_timeout: float = 180.0,
                 history: Optional[List[tuple]] = None, collect_limit: float = 1800.0,
                 stop_after: Optional[int] = None, after_reward: Optional[Callable[[Step], None]] = None,
                 record_dir: Optional[str] = None, before_step: Optional[Callable[[Step], None]] = None,
                 only: Optional[List[str]] = None) -> None:
        self.clients, self.leader, self.steps, self.npc_pos = clients, leader, steps, npc_pos
        self.actors = list(clients)          # 這個群組真的要接／交任務的人（run() 會排除已完成的）
        self.only = only                     # 只讓這些人接／交（落後的人補進度，其他人跟隊；None＝全隊）
        self.after_reward = after_reward         # 給寵物的步驟做完後呼叫（jobs/quests 用來馬上照規則表整理寵物欄）
        self.before_step = before_step           # 每一步開始、跳點之前呼叫（jobs/quests 用來在血量低時先回村補血）
        self.log, self.grid_fn, self.clock = log, grid_fn, clock
        self.idle = idle or time.sleep
        self.need_free, self.battle_timeout = need_free, battle_timeout
        self.collect_limit = collect_limit       # 收集型刷掉落的時間上限（秒），超過就停
        # 做完這一步（taskId）就結束，後面的不做。轉生 2005：使用者規則「打完機暴（200500004）後，
        # 全部寵物存個人倉庫」，要在機暴和守護人之間停下來整理寵物（2026-10-24）
        self.stop_after = stop_after
        self.stopped_early = False
        self.lock = threading.Lock()
        self.tasks: Dict[str, Dict[int, int]] = {n: {} for n in clients}
        self.maps: Dict[str, int] = {}
        self.pos: Optional[Tuple[int, int]] = None
        self.dir = 0                 # 隊長面向（走路時記下來，開遇敵前的 NewRole_SynRolePos 要用）
        self.items: Dict[str, Dict[int, dict]] = {n: {} for n in clients}
        self.hp: Dict[str, int] = {}            # 每隻**目前**的 hp（看最新的，不是「出現過 hp=1」）
        self.fighting: Dict[str, bool] = {n: False for n in clients}     # Role_RoleInfo.status 1＝戰鬥中
        self.finished: Dict[str, List[int]] = {n: [] for n in clients}
        self.pets: Dict[str, Dict[int, dict]] = {n: {} for n in clients}
        self.recent: List[tuple] = []           # 隊長最近的提示／開戰／結束（時間, 封包, lanID），失敗時印出來
        self.battle_ends = 0                    # 隊長收到幾次 Battle_BattleEnd（戰鬥型互動判斷「打完一場」）
        self.last_win: Optional[int] = None     # 最後一場的勝負（Battle_BattleEnd.win）：只有贏了才當成遇敵的怪補送
        # 戰鬥型互動錄檔（使用者 2026-10-31「你錄製」：天空之島黑暗精靈王連輸看不出怎麼死的）：
        # 一次互動一個 jsonl，格式同 herobf（start → 隊長收到的每個封包 → end），None＝不錄
        self.record_dir = record_dir
        self.rec = None
        self.rec_t0 = 0.0
        self.records: List[str] = []
        on = {name: self._listener(name) for name in clients}
        # 先重播登入時已收到的事件（2026-10-22 實機：監聽器晚掛上，任務狀態全是空的）
        for name, e in history or []:
            if name in on:
                on[name](e)
        for name, c in clients.items():
            c.listeners.append(on[name])

    # ---- 封包 ----
    def _listener(self, name: str):
        def on(e):
            n, d = e.get("name"), e.get("data") or {}
            with self.lock:
                if name == self.leader and self.rec is not None:
                    self._rec_row({"t": round(self.clock() - self.rec_t0, 2), "dir": "recv", "name": n, "data": d})
                if name == self.leader and n in ("Other_SystemTip", "Battle_BattleInit", "Battle_BattleEnd",
                                                 "Npc_NpcTalk", "Npc_OpenNpcDialog"):
                    # 診斷用（2026-10-25 五兄弟守護者 201200007 第二次「送了互動沒開戰」）：失敗時印出最近幾則
                    self.recent.append((round(self.clock(), 1), n, d.get("lanID") if n == "Other_SystemTip" else ""))
                    del self.recent[:-8]
                    if n == "Battle_BattleEnd":
                        self.battle_ends += 1
                        self.last_win = d.get("win")        # 1 贏、2 輸（headless_party 的戰績也看這欄）
                if n == "Task_TaskInfo":
                    for t in d.get("taskInfo") or []:
                        self.tasks[name][t["taskId"]] = t.get("taskStatus")
                elif n == "Role_RoleInfo":
                    a = d.get("roleAttr") or {}
                    if a.get("rid") not in (None, getattr(self.clients[name], "rid", None)):
                        return
                    if "map" in a:
                        self.maps[name] = a["map"]
                        if name == self.leader and a.get("nowPos"):
                            self.pos = (a["nowPos"]["x"], a["nowPos"]["y"])
                    if "hp" in a:
                        self.hp[name] = a["hp"]
                    if "status" in a:
                        self.fighting[name] = a["status"] == 1
                    if "finishTasks" in a:
                        self.finished[name] = list(a["finishTasks"] or [])
                elif n == "Item_ItemInfo":
                    for it in d.get("itemInfo") or []:
                        self.items[name].setdefault(it["itemIndex"], {}).update(it)
                elif n == "Pet_PetInfo":
                    for p in d.get("petInfo") or []:
                        x = self.pets[name].setdefault(p["petIndex"], {})
                        x.update({k: p[k] for k in ("petId", "level", "status") if k in p})
        return on

    def wait(self, cond: Callable[[], bool], timeout: float) -> bool:
        end = self.clock() + timeout
        while True:
            with self.lock:
                # 有人倒下也要等這場打完才停：戰鬥中登出等於放棄，活著的隊友本來打得贏
                # （2026-10-23 四寶玉第 1 關：倒 2 隻就停、全隊登出 → 5 隻都沒推進）。LESSONS #68
                if self.dead and not any(self.fighting.values()):
                    raise QuestStop(f"有人被打死：{self.dead}（先重新組隊、回村補血）")
                if cond():
                    return True
            if self.clock() >= end:
                return False
            self.idle(0.3)

    @property
    def dead(self) -> List[str]:
        """**目前** hp 剩 1 的人。看最新血量，不是「歷史上出現過 hp=1」——建立執行器時會重播
        `Party.history`（登入後的所有事件），舊的 hp=1 會讓人永遠被當成死亡：2026-10-30 第五組
        五隻明明 779/779 滿血，每次排 2016 都立刻「有人被打死：['角色22']」，要重啟常駐清掉
        history 才會好。"""
        return [n for n, hp in self.hp.items() if hp == 1]

    def statuses(self, tid: int) -> Dict[str, Optional[int]]:
        with self.lock:
            return {n: self.tasks[n].get(tid) for n in self.actors}

    def bag_free(self, name: str) -> int:
        with self.lock:
            used = sum(1 for it in self.items[name].values() if it.get("overlay", 1) > 0 and it.get("itemStorge", 0) == 0)
        return BAG_SIZE - used

    def check_pets(self, step: Step) -> None:
        """交寵物型：每隻都要有該寵物且等級夠；會給寵物：寵物欄（上限 5）至少空一格（使用者）。不符合就停，不送請求。"""
        # 只看**這一步還在進行中**的人：已經做完那一步的人狀態是 None（伺服器只推目前那一步），
        # 他們的寵物早就交出去了，算進來會停在「要寵物 N 練到 L 級」（2026-10-30 第五組 2016，
        # 隊長換成落後的角色11重跑第 1 步）。全員都還沒接（都 None）時照舊看全隊。
        doers = [n for n in self.actors if self.tasks[n].get(step.task_id) in (1, 4)] or list(self.actors)
        with self.lock:
            # petId ≤ 0 是空格（寵物交出去後那格變 petId -1；2026-10-22 誤算成 5 隻）
            pets = {n: [p for p in self.pets[n].values() if (p.get("petId") or 0) > 0] for n in doers}
        if step.reward_pets:
            # 這一步要交出去的寵物會**先被收走**，那些格子馬上空出來（2026-10-23 第三組：
            # 4 隻鯊魚＋騎寵＝5/5 被誤判成「不夠空」，實際上交完就空 4 格）。
            freed = len(step.pets)
            full = {n: len(ps) for n, ps in pets.items()
                    if PET_MAX - len(ps) + freed < step.reward_pets}
            if full:
                raise QuestStop(f"寵物欄不夠空（上限 {PET_MAX}、要空 {step.reward_pets} 格、"
                                f"這步會交出 {freed} 隻）：{full}")
        for pid, lv in step.pets:
            short = {}
            for n, ps in pets.items():
                best = max((p.get("level", 0) for p in ps if p.get("petId") == pid), default=None)
                if best is None or best < lv:
                    short[n] = best
            if short:
                raise QuestStop(f"步驟 {step.task_id} 要寵物 {pid} 練到 {lv} 級，目前（無＝沒有這隻）：{short}")
        # 條件寵物**不能在出戰／騎乘中**：NPC 會拒絕（提示 80183），交寵物型更是交不出去。
        # 2026-10-30 第五組 2019 第 1 步（type 1 互動型）就卡在這裡——四色鯊魚有一隻在出戰。
        for pid, _lv in step.pets:
            for n in doers:
                idx = self.pet_slot(n, pid, _lv)
                if idx is not None:
                    self.rest_pet(n, idx)

    def L(self):
        return self.clients[self.leader]

    # ---- 動作 ----
    def hop(self, tid: int, m: int, x: int, y: int) -> None:
        """照官方客戶端送 `Task_RequestNavigate`（`doc/autoplay/參考專案-learnings.md` §1.9）：

        - **不帶 `extra`**：官方沒有這個欄位，我們送 `extra:0` 會被編碼進去，位元組就和官方不同。
        - **換圖一律送，連已經站在那張圖（門格）也送**：官方 1.82 錄檔 137 次換圖全部走 RN、
          0 次 `Role_RoleWarp`。已經在該圖時被拒不算錯（舊版根本不送），只記日誌。
        """
        with self.lock:
            here = self.maps.get(self.leader)
        # 戰鬥中跳點會被拒（11-04、11-05 第七組四大 2003 各一次 hold）：先等那場打完；被拒時還在打就等完重送一次
        self.wait(lambda: not any(self.fighting.values()), HOP_BATTLE_WAIT)
        r = self.L().request("Task_RequestNavigate", {"taskId": tid, "map": m, "x": x, "y": y})
        if not (r or {}).get("isOK") and any(self.fighting.values()) \
                and self.wait(lambda: not any(self.fighting.values()), HOP_BATTLE_WAIT):
            self.log(f"跳點 {m}({x},{y}) 被拒時還在戰鬥中，打完重送一次：{r}")
            r = self.L().request("Task_RequestNavigate", {"taskId": tid, "map": m, "x": x, "y": y})
        if not (r or {}).get("isOK"):
            if here == m:
                self.log(f"跳點 {m}({x},{y}) 被拒但人已經在這張圖，繼續：{r}")
                return
            raise QuestStop(f"跳點被拒 {tid} → {m}({x},{y})：{r}")
        if here != m and not self.wait(lambda: self.maps.get(self.leader) == m, 10):
            raise QuestStop(f"跳點後沒換到地圖 {m}")
        with self.lock:
            self.pos = (x, y)
        self.log(f"跳到 {m}({x},{y})")

    def walk_near(self, npc: int, radius: int = 2) -> None:
        if npc not in self.npc_pos:
            raise QuestStop(f"設定表找不到 NPC {npc} 的位置")
        m, x, y = self.npc_pos[npc]
        with self.lock:
            here, pos = self.maps.get(self.leader), self.pos
        if here != m:
            raise QuestStop(f"NPC {npc} 在地圖 {m}，隊長在 {here}")
        if pos and max(abs(pos[0] - x), abs(pos[1] - y)) <= radius:
            return
        if self.grid_fn is None or pos is None:
            raise QuestStop(f"不知道隊長位置或沒有地圖資料，不能走到 NPC {npc}")
        from ..pathing import near, walk_payload
        path = near(self.grid_fn(m), pos, (x, y), radius)
        if not path:
            raise QuestStop(f"地圖 {m} 找不到路 {pos} → NPC {npc}({x},{y})")
        payload = walk_payload(m, path)
        self.L().send("Role_RoleWalk", payload)
        self.idle(max(len(path) - 1, 0) / WALK_SPEED + 0.5)            # 依路徑長度等足，不瞬移
        with self.lock:
            self.pos = path[-1]
            self.dir = payload.get("dir", self.dir)   # 伺服器不推走路後的座標／面向，用自己走的最後一段
        self.log(f"走到 NPC {npc} 旁 {path[-1]}（{len(path) - 1} 步）")

    def pet_slot(self, name: str, pid: int, lv: int) -> Optional[int]:
        """找這隻角色身上符合條件（petId 相同、等級夠）的寵物欄位置；沒有回 None。
        同一種寵物有多隻時挑**到級裡等級最低**的：還沒練到的本來就不算，最低的就是剛練好要交的那隻。
        舊版挑最高的：11-02 第六組 2016 要交虎 307（30 級），會把 128 級騎寵貝魯卡交出去（不可逆）。"""
        with self.lock:
            # self.pets[name] 是 {petIndex: {petId, level}}，petIndex 是鍵不是欄位
            fit = [(idx, p.get("level") or 0) for idx, p in self.pets[name].items()
                   if p.get("petId") == pid and (p.get("level") or 0) >= lv]
        if not fit:
            return None
        return min(fit, key=lambda t: t[1])[0]

    def rest_pet(self, name: str, idx: int) -> None:
        """要交的寵物在出戰／騎乘中就先改休息——出戰中的交不出去（提示 80183
        「所需寵物狀態為戰鬥或騎乘，無法繼續操作」）。以前都靠人先跑一次 `pets --mode rest`，
        漏掉就整組卡住（2026-10-30 第五組 2016：角色11的狗在出戰沒交成，四隻先走到第 2 步，
        之後怎麼跑都對不起來）。`NewPet_NewPetChangeStatus` 沒有 response，send 後看 `Pet_PetInfo`。"""
        with self.lock:
            if (self.pets[name].get(idx) or {}).get("status") not in (2, 3):
                return
        self.clients[name].send("NewPet_NewPetChangeStatus", {"petIndex": idx, "status": 1})
        if not self.wait(lambda: (self.pets[name].get(idx) or {}).get("status") == 1, 6):
            # 外掛**鎖定**的戰寵／騎寵改不了狀態（也存不進倉庫）→ 先把鎖定清成 0 再改一次。
            # 清鎖定要一併關自動換戰寵，不然伺服器不受理（LESSONS #104）。
            self.log(f"{name} 第 {idx} 格改不成休息，先清掉外掛寵物鎖定再試")
            self.clients[name].request("WaiGua_NomalSettingHandlePet",
                                       {"fightPetIndex": 0, "ridePetIndex": 0,
                                        "autoChangeFightPetFlg": False})
            self.clients[name].send("NewPet_NewPetChangeStatus", {"petIndex": idx, "status": 1})
            if not self.wait(lambda: (self.pets[name].get(idx) or {}).get("status") == 1, 6):
                raise QuestStop(f"{name} 第 {idx} 格的寵物改不成休息，交不出去")
        self.log(f"{name} 第 {idx} 格的寵物先改休息（出戰中交不出去）")

    def hand_over(self, s: Step) -> None:
        """交寵物型（`type 7`）：**每隻角色各自送**，而且 `npcArg` 一定要帶 `petIndex`。

        2026-10-22 第二組實機（`doc/autoplay/quests/2016.md`）：
        - 只送 `scriptId`／`taskId`（沒有 `petIndex`）伺服器**不推進**，任務卡住。
        - 第一次送出時**伺服器會自動解散隊伍**（提示 33109），所以不能只靠隊長送，
          必須每隻各自送自己的 `petIndex`。
        """
        if not s.pets:
            raise QuestStop(f"步驟 {s.task_id} 是交寵物型（type 7）但設定表沒寫要交什麼寵物")
        if len(s.pets) > 1:
            # 卡坦的願望（2019）每一步要**一次交 4 隻**（4 色鯊魚／4 色雞）。`npcArg` 另有
            # `petIndexList`（*integer）欄位，多隻很可能走它，但**還沒錄到實機封包**。
            # 規範第 2 點：不猜著送。等 primaljason-home 用遊戲畫面錄到再實作。
            raise QuestStop(f"步驟 {s.task_id} 要一次交 {len(s.pets)} 隻寵物（{s.pets}），"
                            f"多隻的封包格式還沒驗證（可能是 npcArg.petIndexList），先不送")
        # **第一次送出時伺服器會先解散隊伍**（提示 33109），那一封常常被解散動作吃掉、不算數
        # （2026-10-23 第三組實機：5 隻只有隊長沒推進）。所以**最多補送一輪**，只補還沒推進的。
        # 這不是「出錯重試」：是伺服器已知的行為，上限 2 輪、補完還沒好就停。
        # 只對**進行中**（1／4）的人交：狀態 None 的是已經做完那一步的人（伺服器只推目前那一步），
        # 把他們也算進去會在 pet_slot 找不到寵物而停（2026-10-30 第五組 2016：四隻早就交完狗，
        # 隊長換成落後的角色11重跑第 1 步）。
        mine = [n for n in self.actors if self.tasks[n].get(s.task_id) in (1, 4)]
        for attempt in (1, 2):
            todo = [n for n in mine if self.tasks[n].get(s.task_id) != 2]
            if not todo:
                break
            if attempt == 2:
                self.log(f"交寵物：{todo} 還沒推進（第一封被解散隊伍吃掉），補送一次")
            for pid, lv in s.pets:
                for name in todo:
                    idx = self.pet_slot(name, pid, lv)
                    if idx is None:
                        raise QuestStop(f"{name} 身上沒有 {lv} 級以上的寵物 {pid}，不能交")
                    self.rest_pet(name, idx)       # 出戰／騎乘中的交不出去（提示 80183）
                    self.clients[name].send(
                        "Npc_NpcInteract",
                        {"npcId": s.end_npc,
                         "npcArg": {"scriptId": s.end_script, "taskId": s.task_id,
                                    "petIndex": idx, "isPetNpc": False,
                                    "npcExtraType": 0, "npcExtraId": 0}})
                    self.idle(0.5)
            self.wait(lambda: all(self.tasks[n].get(s.task_id) == 2 for n in mine), 12)
        if not all(self.statuses(s.task_id).get(n) == 2 for n in mine):
            raise QuestStop(f"交寵物後任務 {s.task_id} 沒有變成 2：{self.statuses(s.task_id)}")
        self.log(f"任務 {s.task_id} → 2（全隊交寵物）")

    def have(self, name: str, item_id: int) -> int:
        """這隻角色背包裡某道具的總數（倉庫裡的不算）。"""
        with self.lock:
            return sum(it.get("overlay", 1) for it in self.items[name].values()
                       if it.get("itemId") == item_id and it.get("itemStorge", 0) == 0)

    def missing(self, s: Step) -> Dict[str, Dict[int, int]]:
        """每隻角色還差哪些道具幾個（收集型）。"""
        out: Dict[str, Dict[int, int]] = {}
        for n in self.actors:
            short = {iid: num - self.have(n, iid) for iid, num in s.wait_items
                     if self.have(n, iid) < num}
            if short:
                out[n] = short
        return out

    def collect(self, s: Step) -> None:
        """收集型（`type 10`）：**沒有 endNpc**，要在任務指定的地圖打怪等掉落。

        `doc/autoplay/game/quests.md` §4：道具到手由伺服器判定，客戶端送
        `Task_TaskwaitThingCheck{taskwaitThing, taskwaitType}`（type 10 用 `taskwaitType=1`）
        讓伺服器重新檢查。

        出錯就停（規範第 2 點）：**一定有上限**——超過 `collect_limit` 秒還沒收齊就停，
        並把實際收集進度記下來，不無止盡刷。
        """
        if not s.wait_items:
            raise QuestStop(f"步驟 {s.task_id} 是收集型（type 10）但設定表沒寫 waitItem")
        short = self.missing(s)
        if short:
            self.log(f"收集 {s.task_id}：開始刷掉落，還差 {short}")
            # 照官方外掛「原地遇敵」：先 NewRole_SynRolePos 再 Role_LoopBattle（參考專案-learnings §1.8，
            # 和 headless_party.set_loop() 一致）。座標不知道就只送 LoopBattle 並記日誌。
            with self.lock:
                here, pos, d = self.maps.get(self.leader), self.pos, self.dir
            if here is not None and pos is not None:
                self.L().send("NewRole_SynRolePos", {"mapId": here, "x": pos[0], "y": pos[1], "dir": d})
            else:
                self.log(f"收集：不知道隊長座標（地圖 {here}、位置 {pos}），只送 Role_LoopBattle")
            self.L().send("Role_LoopBattle", {})          # 開遇敵（打怪才會掉）
            try:
                end = self.clock() + self.collect_limit
                while self.clock() < end:
                    self.idle(5.0)                        # idle 會順便回戰鬥確認（PlayOver）
                    short = self.missing(s)
                    if not short:
                        break
                else:
                    raise QuestStop(f"收集 {s.task_id} 超過 {self.collect_limit:.0f} 秒還沒收齊，"
                                    f"還差 {short}（先停下來，不無止盡刷）")
            finally:
                self.L().send("Role_UnLoopBattle", {})    # 不論成敗都關遇敵，不要放著自己打
            self.log(f"收集 {s.task_id}：道具到齊")
        for iid, _ in s.wait_items:
            for n in self.actors:
                self.clients[n].send("Task_TaskwaitThingCheck", {"taskwaitThing": iid, "taskwaitType": 1})
            self.idle(0.4)
        if not self.wait(lambda: all(self.tasks[n].get(s.task_id) == 2 for n in self.actors), 15):
            raise QuestStop(f"收集齊了但任務 {s.task_id} 沒有變成 2：{self.statuses(s.task_id)}")
        self.log(f"任務 {s.task_id} → 2（全隊收集完成）")

    def interact(self, npc: int, script: int, tid: int, want: int, battle: bool) -> None:
        # 伺服器不回 Npc_NpcInteract 的 response（2026-10-22 實機 request 逾時）→ 只送，結果看推送的 Task_TaskInfo
        def done() -> bool:
            return all(self.tasks[n].get(tid) == want for n in self.actors)

        def send() -> None:
            self.L().send("Npc_NpcInteract", {"npcId": npc, "npcArg": {"scriptId": script, "taskId": tid,
                                                                        "isPetNpc": False, "npcExtraType": 0,
                                                                        "npcExtraId": 0}})
        if not battle:
            send()
            ok = self.wait(done, 8)
        else:
            ok = self.interact_battle(send, done, tid, npc)
        if not ok:
            with self.lock:
                diag = f"；隊長位置估計 {self.pos}、地圖 {self.maps.get(self.leader)}、最近封包 {list(self.recent)}"
            raise QuestStop(f"任務 {tid} 沒有變成 {want}：{self.statuses(tid)}{diag}")
        self.log(f"任務 {tid} → {want}（全隊）")

    # ---- 錄檔（只錄戰鬥型互動；寫檔失敗只記日誌，不影響解任務）----
    def _rec_row(self, row: dict) -> None:
        """呼叫端已拿 self.lock。"""
        if self.rec is None:
            return
        try:
            import json
            self.rec.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            self.rec.flush()
        except OSError as e:
            self.log(f"任務戰鬥錄檔寫不進去：{e}")
            self.rec = None

    def _rec_open(self, tid: int, npc: Optional[int]) -> None:
        if not self.record_dir:
            return
        import os
        try:
            day = time.strftime("%Y%m%d")
            os.makedirs(os.path.join(self.record_dir, day), exist_ok=True)
            stem = os.path.join(self.record_dir, day, f"{time.strftime('%H%M%S')}_{tid}")
            path, k = stem + ".jsonl", 1
            while os.path.exists(path):
                k += 1
                path = f"{stem}_{k}.jsonl"
            f = open(path, "w", encoding="utf-8")
        except OSError as e:
            self.log(f"任務戰鬥錄檔開不了：{e}")
            return
        with self.lock:
            self.rec, self.rec_t0 = f, self.clock()
            self.records.append(path)
            self._rec_row({"type": "start", "task": tid, "npc": npc, "lead": self.leader,
                           "rid": getattr(self.clients[self.leader], "rid", None)})

    def _rec_close(self) -> None:
        with self.lock:
            if self.rec is None:
                return
            self._rec_row({"type": "end", "win": self.last_win})
            try:
                self.rec.close()
            except OSError:
                pass
            self.rec = None
        self.log(f"任務戰鬥錄檔：{self.records[-1]}")

    def watch_battle(self, cond: Callable[[], bool], end: float) -> bool:
        """等 cond 成立；到 end 時**還在戰鬥中就繼續看**（每次延 30 秒），戰鬥結束才算數，上限 BATTLE_HARD_CAP。
        使用者 2026-10-31：「為什麼是等待，而不是你一直看著」——柯黑穆肯打到第 54 回合（約 3 分鐘）、BOSS 剩一半，
        被固定 180 秒判失敗中斷（LESSONS #121）。"""
        hard = self.clock() + BATTLE_HARD_CAP
        told = False
        while True:
            if self.wait(cond, max(0.0, end - self.clock())):
                return True
            with self.lock:
                busy = any(self.fighting.values())
            if not busy or self.clock() >= hard:
                return False
            if not told:
                self.log("戰鬥還在進行，繼續看著到打完（不判逾時）")
                told = True
            end = min(self.clock() + 30.0, hard)

    def interact_battle(self, send: Callable[[], None], done: Callable[[], bool], tid: int,
                        npc: Optional[int] = None) -> bool:
        self._rec_open(tid, npc)
        try:
            return self._interact_battle(send, done, tid)
        finally:
            self._rec_close()

    def _interact_battle(self, send: Callable[[], None], done: Callable[[], bool], tid: int) -> bool:
        """戰鬥型互動：**戰鬥中送互動伺服器不理**。2026-10-31 第二組碧青 200200005、天空之島 203400008：
        站在 NPC 旁被原地遇敵的怪纏上（約 35 秒一場、場場贏），互動剛好送在戰鬥中 → 任務戰鬥沒觸發，
        空等 180 秒；重跑一次就過（LESSONS #116）。
        所以：先等沒人在打才送；打完一場、過 RESEND_GRACE 秒任務還沒推進 → 那場是遇敵的怪，
        再送一次，最多 INTERACT_TRIES 次，總時間仍以 battle_timeout 為上限。"""
        end = self.clock() + self.battle_timeout
        for k in range(1, INTERACT_TRIES + 1):
            if not self.wait(lambda: not any(self.fighting.values()), max(0.0, end - self.clock())):
                return False
            with self.lock:
                ends0 = self.battle_ends
            send()
            settled = self.watch_battle(lambda: done() or (self.battle_ends > ends0 and not any(self.fighting.values())),
                                        end)
            if not settled:
                return False
            if done() or self.wait(done, RESEND_GRACE):
                return True
            with self.lock:
                win = self.last_win
            if win != 1:
                # 打輸（或不知道勝負）就是任務戰鬥本身沒過：不要當成遇敵的怪馬上再送（會沒補血連打）。
                # 2026-10-31 天空之島黑暗精靈王：第一版把輸的那場也重送，沒補血連打 3 場（LESSONS #116 補充）
                self.log(f"任務 {tid} 的戰鬥打輸了（win={win}），不重送，交給補血後重排")
                return False
            if k < INTERACT_TRIES:
                self.log(f"打完一場但任務 {tid} 沒推進（多半是原地遇敵的怪，互動沒觸發任務戰鬥），"
                         f"再送一次互動（第 {k + 1}/{INTERACT_TRIES} 次）")
        return False

    # ---- 流程 ----
    def run(self) -> None:
        first = self.steps[0]
        group = first.task_id // 100000
        with self.lock:
            done = [n for n in self.clients if group in self.finished[n]]
        if len(done) == len(self.clients):
            self.log(f"任務群組 {group} 全隊都已完成")
            return                                  # 已完成就直接跳過，不檢查空格
        if done:
            # 使用者 2026-10-28 同意：**有人已經完成就自動續跑**（第四組 2010 只有隊長角色54完成，
            # 已完成的任務放棄指令清不掉，整組就永遠卡著、轉生做不了）。已完成的人留在隊伍裡跟著打，
            # 但不接任務、不交付、不列入狀態檢查。步驟不一致（大家都在做、停在不同步）仍然停，見下面。
            self.actors = [n for n in self.clients if n not in done]
            if self.leader not in self.actors:
                # 接任務、跳點、互動都是隊長送的，已完成的人送會被伺服器拒；換隊長要在組隊那層做
                # （`jobs/quests.py` 的 `lead_actor`），這裡只擋住誤用。
                raise QuestStop(f"隊長 {self.leader} 已經完成任務 {group}，要先換沒完成的人帶隊")
            self.log(f"任務群組 {group} 已完成的隊員 {done} 跟隊但不接任務，其他 {self.actors} 照跑")
        if self.only is not None:
            # 陣亡後進度分岔：落後的人補做到領先的人那一步之前，領先的人跟隊幫忙打、不接不交
            # （11-05 2010 風的寶玉陣亡，放棄第 5 步伺服器不清，整組卡住；jobs/quests.py 的 laggards）
            self.actors = [n for n in self.actors if n in self.only]
            if self.leader not in self.actors:
                raise QuestStop(f"隊長 {self.leader} 不在補進度的人 {self.only} 裡，要先換人帶隊")
            self.log(f"任務群組 {group} 只有 {self.actors} 補進度，其他人跟隊")
        # 空格檢查要在「已完成就跳過」之後：2026-10-24 第三組只剩 2010 沒做完，收集到的四寶玉
        # 還在身上（任務道具不能丟），整批重跑卻先卡在早就完成的 2999「要 15 空格」。
        short = {n: self.bag_free(n) for n in self.actors if self.bag_free(n) < self.need_free}
        if short:
            raise QuestStop(f"背包空格不足 {self.need_free}：{short}")
        st = self.statuses(first.task_id)
        # 進行中的步驟：登入時伺服器只推目前那一步，前面的步驟沒有狀態（2026-10-22 猜謎三兄弟：
        # 第 2 步進行中，程式卻把空的第 1 步當成「可接」重接一次 → 多拿一隻金豬）
        current = next((i for i, s in enumerate(self.steps)
                        if self.statuses(s.task_id).get(self.leader) in (1, 4)), None)
        # 伺服器不一定推「可接（-1）」：1.82 任務在面板上的「待接取」是客戶端用設定表算的（2026-10-22 實機）
        # → 還沒完成、沒有任何一步在進行中、第 1 步狀態又是空的，才當作可接
        if current is None and all(v in (-1, None) for v in st.values()) and first.start_npc:
            self.L().send("Task_Transfer", {"taskId": first.task_id})
            if not self.wait(lambda: self.maps.get(self.leader) == self.npc_pos.get(first.start_npc, (None,))[0], 10):
                raise QuestStop("Task_Transfer 之後沒到接任務 NPC 的地圖")
            self.walk_near(first.start_npc)
            self.interact(first.start_npc, first.start_script, first.task_id, 1, battle=False)
        for i, s in enumerate(self.steps):
            if current is not None and i < current:
                if s.task_id == self.stop_after:
                    self.stopped_early = True
                    return                                # 指定的那一步早就做完了
                continue                                  # 進行中那一步之前的都已經做完
            st = self.statuses(s.task_id)
            if all(v == 2 for v in st.values()):
                if s.task_id == self.stop_after:
                    self.stopped_early = True
                    return
                continue
            # 交寵物型是**每隻各自交**，中途被打斷會留下「有人 2、有人 1」的混合狀態；
            # 這種情況不該停，hand_over() 只會補送還沒推進的那幾隻（2026-10-23 第三組實機）。
            # 交寵物型只要隊長在進行中就做：其他人**已經做完**（伺服器只推目前那一步，做完的是 None）
            # 或還在同一步都行，`hand_over()` 只會對還沒推進的補送（2026-10-23 第三組、2026-10-30 第五組）。
            mixed_ok = s.type == 7 and set(st.values()) <= {1, 2, 4, None} and st[self.leader] in (1, 4)
            if not mixed_ok and (len(set(st.values())) != 1 or st[self.leader] not in (1, 4)):
                raise QuestStop(f"隊員任務狀態不一致或不在進行中：{s.task_id} {st}")
            self.log(f"步驟 {s.task_id} {s.name}")
            self.check_pets(s)
            if self.before_step is not None:
                self.before_step(s)                   # 可能回村補血：之後照目前地圖重算跳點
            with self.lock:
                cur = self.maps.get(self.leader)
            for m, x, y in s.hops(cur):
                self.hop(s.task_id, m, x, y)
            if s.type == 10:
                self.collect(s)                       # 收集型沒有 endNpc，在這張圖刷掉落
                continue
            if s.end_npc:
                # 戰鬥型要站到 1 格內：2026-10-23 五兄弟守護者（2184）站 2 格送了不開戰、等滿 180 秒；
                # 走到 1 格內（同 GUI 錄檔 (25,9)）馬上開戰。LESSONS #67
                self.walk_near(s.end_npc, radius=1 if s.type == 4 else 2)
                if s.type == 7:
                    # ⚠ 只有 type 7 走這裡（2016 恐龍博士實機驗證過）。**2019 卡坦的願望、
                    # 3003 猜謎三兄弟的交寵物步驟其實是 type 1**（2026-10-23 查設定表），
                    # 走原本的 interact()：沒驗證過那些步驟是「交出寵物」還是「身上要有」，
                    # 亂帶 petIndex 可能把寵物白白送掉（不可逆）。推不動就會停，再查。
                    self.hand_over(s)                 # 交寵物：每隻各自送、要帶 petIndex
                else:
                    self.interact(s.end_npc, s.end_script, s.task_id, 2, battle=s.type == 4)
            if s.reward_pets and self.after_reward is not None:
                # 使用者 2026-10-25「不希望跑到一半寵物不夠」：這一步給了寵物（藍暴、修寶、金飛…）就馬上照規則表處理，
                # 不等整個任務做完；處理失敗照樣停（after_reward 自己丟例外）
                self.after_reward(s)
            if s.task_id == self.stop_after:
                self.log(f"做完 {s.task_id} 就停（--stop-after），後面的步驟不做")
                self.stopped_early = True
                return


def npc_positions() -> Dict[int, Tuple[int, int, int]]:
    import json
    import os
    from ...features.gamedb.tables import CONFIGS
    with open(os.path.join(CONFIGS, "Npc.json"), encoding="utf-8") as fp:
        return {n["ID"]: (n["map"], n["x"], n["y"]) for n in json.load(fp)}


def build_quest(p, group: int, log: Callable[[str], None], grid_fn, need_free: int = 1,
                stop_after: Optional[int] = None, battle_timeout: float = 180.0,
                after_reward: Optional[Callable[[Step], None]] = None,
                record_dir: Optional[str] = None,
                before_step: Optional[Callable[[Step], None]] = None,
                only: Optional[List[str]] = None) -> "HeadlessQuest":
    """隊長＝這組實際登入的第一個角色（p.party[0]），不是寫死的第 2 組 PARTY（2026-10-22 Mac 回報：
    寫死會讓第三組的任務去登第 2 組、把 Windows 練功的 5 隻擠下線）。"""
    return HeadlessQuest(p.clients, p.party[0][1], load_steps(group), npc_positions(), log=log,
                         grid_fn=grid_fn, idle=p.nap, need_free=need_free,
                         history=list(p.history) + (p.snapshot_events() if hasattr(p, "snapshot_events") else []),
                         stop_after=stop_after, battle_timeout=battle_timeout, after_reward=after_reward,
                         record_dir=record_dir, before_step=before_step, only=only)


def main(argv=None) -> int:
    from ..headless_party import Party
    from ..logrotate import RotatingFile
    from ..pathing import load_grid
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.quest.headless")
    ap.add_argument("--group", type=int, required=True)
    ap.add_argument("--profile", required=True,
                    help="第幾組帳號（BOT_ACCOUNT_<組>）；必填，避免沒寫就登到別台正在跑的那組")
    ap.add_argument("--discover", action="store_true", help="第 2 組也從登入結果取角色清單")
    ap.add_argument("--log", required=True)
    ap.add_argument("--free", type=int, default=1, help="開始前每隻背包至少要空幾格")
    ap.add_argument("--stop-after", type=int, default=None,
                    help="做完這一步（taskId）就正常結束，例：轉生 2005 打完機暴 200500004 先停、整理寵物")
    # 轉生的「挑戰」要打很久：2026-10-24 第三組二轉，漆黑 10 樓與機暴都在 180 秒內沒翻成 2，
    # 下一輪重跑一看其實早就完成了——是等太短，不是打輸。
    ap.add_argument("--battle-timeout", type=float, default=180.0,
                    help="戰鬥型步驟等任務狀態翻面的秒數（轉生的挑戰建議 420）")
    ap.add_argument("--owner-dir", default=None, help="帳號擁有者鎖目錄（預設 BOT_OWNER_DIR 或 captures/owner）")
    args = ap.parse_args(argv)
    fp = RotatingFile(args.log, max_bytes=5 * 1024 * 1024, keep=10)

    def log(msg: str) -> None:
        fp.write(f"{time.strftime('%m-%d %H:%M:%S')} [{args.group}] {msg}\n")
    from .. import owner
    odir = args.owner_dir or owner.default_dir()
    try:                                     # 同組已有常駐練功／固定指令在用就不登入（不互相擠下線）
        owner.acquire(odir, args.profile, owner.KIND_QUEST, log=log)
    except owner.OwnerBusy as e:
        log(f"停止：{e}（常駐練功在跑時改用 `jobs add quests --profile {args.profile} --groups {args.group} --apply`）")
        return 1
    try:
        return _main_owned(args, log, Party, load_grid)
    finally:
        owner.release(odir, args.profile)


def _main_owned(args, log, Party, load_grid) -> int:
    p = Party(log, profile=args.profile, discover=args.discover)
    try:
        p.login_all()
        p.nap(3)
        if not p.form_team():
            log("停止：組隊失敗"); return 1
        if not p.set_loop(False):
            log("停止：關不了原地遇敵"); return 1
        grids: Dict[int, object] = {}

        def grid(m):
            if m not in grids:
                grids[m] = load_grid(m)
            return grids[m]
        q = build_quest(p, args.group, log, grid, need_free=args.free, stop_after=args.stop_after,
                        battle_timeout=args.battle_timeout)
        q.run()
        log(f"停在 {args.stop_after}（--stop-after）" if q.stopped_early else "完成")
        return 0
    except QuestStop as e:
        log(f"停止：{e}")
        return 1
    except Exception as e:  # noqa: BLE001
        log(f"停止：例外 {type(e).__name__}: {e}")
        return 1
    finally:
        p.close()


if __name__ == "__main__":
    sys.exit(main())
