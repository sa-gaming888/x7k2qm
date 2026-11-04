"""任務規格：從設定表（Task.json）產生每一步要送的封包，給無畫面執行器用；並能和實機封包比對。

    python -X utf8 -m x7k2qm.autoplay.quest.spec 2012                       # 印出規格
    python -X utf8 -m x7k2qm.autoplay.quest.spec 2012 --check captures/quests/2012_五兄弟之謎.jsonl --leader 53537

依據（2026-10-21～22 實機 14 個任務）：
- 接任務 `Npc_NpcInteract{startNpc, startScript, 第 1 步 taskId}`（之前用 `Task_Transfer{taskId}` 傳到 NPC 旁）。
- 每一步交任務／開戰 `Npc_NpcInteract{endNpc, endScript, taskId}`。
- 跨地圖：`Task_RequestNavigate{taskId, map, x, y}`，座標＝`taskNavigate` 裡地圖變了的那一點。
  **只在人還不在那張圖時才送**：伺服器常在交任務或打完仗後自己把人傳過去（馬祖快遞進迷宮、五兄弟打完守護者），
  執行器每次跳之前看 `Role_RoleInfo.map`。

比對結果（15 個任務）：NPC 互動 100% 一致；實機的跳點都在規格內；規格多出的 9 個點全是伺服器自己傳送的情況。
"""

import argparse
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ...features.gamedb.tables import CONFIGS

Point = Tuple[int, int, int]                      # (map, x, y)


@dataclass
class Step:
    task_id: int
    type: int
    name: str
    nav: List[Point]
    end_npc: int
    end_script: int
    start_npc: int = 0
    start_script: int = 0
    items: List[Tuple[int, int]] = field(default_factory=list)   # 交任務需要的 (itemId, 數量)
    pets: List[Tuple[int, int]] = field(default_factory=list)    # 交任務需要的寵物 (petId／petDna, 最低等級)
    reward_pets: int = 0                                          # 這一步會給幾隻寵物（寵物欄要有空位）
    wait_items: List[Tuple[int, int]] = field(default_factory=list)
    """收集型（type 10）要打怪收集的 (itemId, 數量)。這種步驟**沒有 endNpc**，
    道具到手由伺服器判定，客戶端送 `Task_TaskwaitThingCheck` 讓它重新檢查。"""

    def hops(self, prev_map: Optional[int] = None) -> List[Point]:
        """要用 Task_RequestNavigate 跳過去的點：路徑上地圖和前一點不同的那一點。"""
        out, cur = [], prev_map
        for m, x, y in self.nav:
            if m != cur:
                out.append((m, x, y))
            cur = m
        return out


def _cfg(name: str, configs: str) -> list:
    with open(os.path.join(configs, f"{name}.json"), encoding="utf-8") as fp:
        return json.load(fp)


def load_steps(group: int, configs: str = CONFIGS, tasks: Optional[list] = None) -> List[Step]:
    rows = tasks if tasks is not None else _cfg("Task", configs)
    out = []
    for t in sorted((t for t in rows if t["group"] == group), key=lambda t: t["step"]):
        conds = t.get("condition") or []
        items = [(c["itemId"], c.get("itemNum", 1)) for c in conds if c.get("itemId")]
        pets = [(c.get("petId") or c.get("petDna"), c.get("petLevel", 1)) for c in conds
                if c.get("petId") or c.get("petDna")]
        reward_pets = sum(1 for r in t.get("reward") or [] if r.get("petId"))
        wait_items = [(w["itemId"], w.get("itemNum", 1)) for w in t.get("waitItem") or [] if w.get("itemId")]
        out.append(Step(task_id=t["ID"], type=t.get("type", 1), name=t.get("explandTn") or t.get("nameTn") or "",
                        nav=[(p["map"], p["x"], p["y"]) for p in t.get("taskNavigate") or []],
                        end_npc=t.get("endNpc") or 0, end_script=t.get("endScript") or 0,
                        start_npc=t.get("startNpc") or 0, start_script=t.get("startScript") or 0, items=items,
                        pets=pets, reward_pets=reward_pets, wait_items=wait_items))
    if not out:
        raise KeyError(f"設定表沒有任務群組 {group}")
    return out


def npc_maps(configs: str = CONFIGS) -> Dict[int, int]:
    return {n["ID"]: n["map"] for n in _cfg("Npc", configs)}


def expected_actions(steps: List[Step], npc_map: Optional[Dict[int, int]] = None) -> List[tuple]:
    """產生要送的關鍵請求序列（不含同地圖走路與戰鬥確認）。
    npc_map：NPC → 地圖；Task_Transfer 會把人傳到接任務 NPC 那張圖，之後的同圖跳點就不用送。"""
    acts: List[tuple] = []
    first = steps[0]
    prev_map = None
    if first.start_npc:
        acts.append(("Task_Transfer", first.task_id))
        acts.append(("Npc_NpcInteract", first.start_npc, first.start_script, first.task_id))
        prev_map = (npc_map or {}).get(first.start_npc)
    for s in steps:
        for m, x, y in s.hops(prev_map):
            acts.append(("Task_RequestNavigate", s.task_id, m, x, y))
        if s.nav:
            prev_map = s.nav[-1][0]
        if s.end_npc:
            acts.append(("Npc_NpcInteract", s.end_npc, s.end_script, s.task_id))
    return acts


def actual_actions(events: List[dict], group: int, leader: str) -> List[tuple]:
    """實機封包裡隊長送出的同類請求（只取這個任務群組的）。"""
    acts: List[tuple] = []
    for e in events:
        if not e.get("conn", "").endswith(leader) or e.get("dir") != "c2s":
            continue
        n, d = e.get("name"), e.get("data") or {}
        if n == "Task_Transfer" and d.get("taskId", 0) // 100000 == group:
            acts.append((n, d["taskId"]))
        elif n == "Task_RequestNavigate" and d.get("taskId", 0) // 100000 == group:
            acts.append((n, d["taskId"], d.get("map"), d.get("x"), d.get("y")))
        elif n == "Npc_NpcInteract":
            a = d.get("npcArg") or {}
            if a.get("taskId", 0) // 100000 == group:
                acts.append((n, d.get("npcId"), a.get("scriptId"), a["taskId"]))
    return acts


def compare(expected: List[tuple], actual: List[tuple]) -> Dict[str, list]:
    """比對：NPC 互動要完全一致（順序、npcId、scriptId、taskId）；換圖點檢查實機有沒有用到規格裡的點。
    實機會有重試（同一個請求連送）與 GUI 多送的換圖（例如回程），只列出來不算錯。"""
    ex_npc = [a for a in expected if a[0] == "Npc_NpcInteract"]
    ac_npc = []
    for a in actual:                                   # 去掉連續重複（實機重試）
        if a[0] == "Npc_NpcInteract" and (not ac_npc or ac_npc[-1] != a):
            ac_npc.append(a)
    ex_nav = [a for a in expected if a[0] == "Task_RequestNavigate"]
    ac_nav = [a for a in actual if a[0] == "Task_RequestNavigate"]
    return {
        "npc_missing": [a for a in ex_npc if a not in ac_npc],
        "npc_extra": [a for a in ac_npc if a not in ex_npc],
        "npc_order_ok": [a for a in ac_npc if a in ex_npc] == [a for a in ex_npc if a in ac_npc],
        "nav_unused": [a for a in ex_nav if a not in ac_nav],
        "nav_extra": [a for a in ac_nav if a not in ex_nav],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.quest.spec")
    ap.add_argument("group", type=int)
    ap.add_argument("--check", help="實機封包 jsonl")
    ap.add_argument("--leader", default="53537")
    args = ap.parse_args(argv)
    steps = load_steps(args.group)
    exp = expected_actions(steps, npc_maps())
    if not args.check:
        for s in steps:
            print(s.task_id, s.name, "type", s.type, "交", s.end_npc, s.end_script, "道具", s.items, "跳點", s.hops())
        for a in exp:
            print(" ", a)
        return 0
    with open(args.check, encoding="utf-8") as fp:
        evs = [json.loads(l) for l in fp if l.strip()]
    r = compare(exp, actual_actions(evs, args.group, args.leader))
    ok = not r["npc_missing"] and not r["npc_extra"] and r["npc_order_ok"]
    print(json.dumps({"一致": ok, **r}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
