"""自動玩的基本動作：點遊戲畫面 → **用封包確認結果**（不用截圖）。

前提：`python -m x7k2qm.autoplay.watch` 已在登入前啟動（寫 jsonl 與狀態檔），而且沒停過。
每個動作都回傳是否成功；失敗就停（規範第 2 點），由呼叫者決定下一步。
座標是 1366×768 客戶區（`doc/autoplay/flows/team-training.md`）。
"""

import json
import os
import time
from typing import Callable, Dict, List, Optional, Sequence

LIVE = os.path.join("captures", "autoplay_live.jsonl")
STATUS = os.path.join("captures", "autoplay_status.log")

# 畫面座標（客戶區）
SWITCH_BTN = (722, 732)
SWITCH_SLOTS = [(536, 274), (828, 274), (536, 354), (828, 354), (536, 434)]   # 欄位 1～5
QUICK_TEAM_BTN = (492, 539)
QT_LEADER_CARD = (520, 299)
QT_MEMBER_CARDS = [(466, 476), (614, 476), (769, 476), (927, 476)]
QT_CONFIRM = (664, 613)
TELEPORT_BTN = (1282, 62)
TELEPORT_TRAIN_TAB = (247, 290)
ENCOUNTER_BTN = (1294, 605)
WAIGUA_BTN = (1232, 714)
WAIGUA_CLOSE = (1249, 73)
WAIGUA_ROLE_RADIOS = [(130, 133), (284, 133), (438, 133), (130, 183), (284, 183)]
WAIGUA_FAST_BATTLE = (459, 316)
MAP_LOAD_SECONDS = 4.0
LEADER_CONN = "61466"                      # 隊長（角色101）在遊戲客戶端的連線埠尾碼；重新登入後要更新

# 練級傳送面板上的按鈕（客戶區），與 Transfer point、地圖 ID
TRAIN_POINTS = {
    "柯奧龜洞Lv10": ((700, 220), 11001),
    "阿布龜洞Lv20": ((519, 299), 10001),
    "海主人隧道Lv30": ((820, 612), 31401),
    "五兄弟洞窟Lv60": ((668, 411), 31706),
    "柯奧4F龜洞Lv80": ((814, 327), 11004),
    "波拉島Lv120": ((1044, 209), 500),
    "波拉島2Lv120": ((1104, 299), 502),
    "地雷區Lv130": ((1104, 369), 60049),
}


class Packets:
    """讀 watch 寫出的 jsonl／狀態檔，等待特定結果。"""

    def __init__(self, live: str = LIVE, status: str = STATUS, poll: float = 0.3) -> None:
        self.live, self.status, self.poll = live, status, poll

    def _lines(self, path: str) -> List[str]:
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fp:
            return fp.readlines()

    def mark(self) -> Dict[str, int]:
        return {"live": len(self._lines(self.live)), "status": len(self._lines(self.status))}

    def wait_status(self, m: Dict[str, int], must: Sequence[str], timeout: float) -> Optional[str]:
        end = time.time() + timeout
        while True:
            for line in self._lines(self.status)[m["status"]:]:
                if all(s in line for s in must):
                    return line.strip()
            if time.time() >= end:
                return None
            time.sleep(self.poll)

    def wait_packet(self, m: Dict[str, int], name: str, direction: str = "c2s",
                    timeout: float = 3.0) -> Optional[dict]:
        end = time.time() + timeout
        while True:
            for line in self._lines(self.live)[m["live"]:]:
                e = json.loads(line)
                if e.get("name") == name and e.get("dir") == direction:
                    return e.get("data") or {}
            if time.time() >= end:
                return None
            time.sleep(self.poll)

    def battle_status(self, rid: int) -> Optional[int]:
        """該角色最近一次 Role_RoleInfo 的 status（0 = 不在戰鬥，1 = 戰鬥中）。"""
        st = None
        for line in self._lines(self.live):
            e = json.loads(line)
            if e.get("name") == "Role_RoleInfo" and e.get("dir") == "s2c":
                a = (e.get("data") or {}).get("roleAttr") or {}
                if a.get("rid") == rid and "status" in a:
                    st = a["status"]
        return st

    def latest_team(self, conn_suffix: Optional[str] = None) -> List[str]:
        """最新的隊伍名單。Team_TeamInfo 有時只是某個隊員的局部更新（只含 1 人），
        所以只看指定連線（隊長）的、而且取最近一次「人數最多」的那筆（2026-10-21 誤判只剩 1 人）。"""
        best: List[str] = []
        for line in self._lines(self.live)[-5000:]:
            e = json.loads(line)
            if e.get("name") != "Team_TeamInfo":
                continue
            if conn_suffix and not e.get("conn", "").endswith(conn_suffix):
                continue
            team = [x.get("roleAttr", {}).get("name") for x in (e.get("data") or {}).get("teamMembers") or []]
            if len(team) >= len(best) or len(team) > 1:
                best = team
        return best


class Ops:
    def __init__(self, click: Callable[[int, int], None], packets: Optional[Packets] = None,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> None:
        self._click, self.p, self.sleep, self.log = click, packets or Packets(), sleep, log

    def click(self, xy, wait: float = 0.0) -> None:
        self._click(*xy)
        if wait:
            self.sleep(wait)

    def open_switch_panel(self) -> bool:
        """「切換」是開關：點了要看到 Role_SwitchRoleGetInfo 才算打開，否則再點一次。"""
        for _ in range(2):
            m = self.p.mark()
            self.click(SWITCH_BTN)
            if self.p.wait_packet(m, "Role_SwitchRoleGetInfo") is not None:
                self.sleep(1.0)
                return True
        return False

    def switch_to(self, slot: int) -> bool:
        if not self.open_switch_panel():
            self.log("切換面板打不開"); return False
        self.click(SWITCH_SLOTS[slot], 6.0)
        return True

    def set_loop(self, leader: str, on: bool) -> bool:
        want = "true" if on else "false"
        for _ in range(2):
            m = self.p.mark()
            self.click(ENCOUNTER_BTN)
            if self.p.wait_status(m, [leader, "LoopBattleFlg", want], 10):
                return True
        self.log(f"原地遇敵 {'開' if on else '關'} 沒確認到"); return False

    def pause_battle(self, leader: str, leader_rid: int, timeout: float = 40.0) -> bool:
        """任何狀態變更前先呼叫：關原地遇敵，等隊長 status 0（戰鬥結束）。使用者規則（2026-10-21）。"""
        if not self.set_loop(leader, False):
            return False
        end = time.time() + timeout
        while self.p.battle_status(leader_rid) not in (0, None):
            if time.time() >= end:
                self.log("等不到戰鬥結束"); return False
            self.sleep(1.0)
        return True

    def teleport_team(self, point: str, leader: str, members: Sequence[str]) -> bool:
        """隊長傳送到練級點；全隊（members）都要看到 map →目標。"""
        xy, map_id = TRAIN_POINTS[point]
        m = self.p.mark()
        self.click(TELEPORT_BTN, 2.5); self.click(TELEPORT_TRAIN_TAB, 1.5); self.click(xy)
        ok = all(self.p.wait_status(m, [n, f"→{map_id}"], 15) for n in [leader, *members])
        if ok:
            self.sleep(MAP_LOAD_SECONDS)
        else:
            self.log(f"傳送到 {point} 沒有全隊確認")
        return ok

    def quick_team(self, size: int = 5) -> bool:
        """快速組隊：隊長卡＋全部隊員卡；以送出的 Role_NomalHandle 與 Team_TeamInfo 人數確認。"""
        if not self.open_switch_panel():
            return False
        self.click(QUICK_TEAM_BTN, 2.5); self.click(QT_LEADER_CARD, 1.2)
        for xy in QT_MEMBER_CARDS[:size - 1]:
            self.click(xy, 1.2)
        m = self.p.mark()
        self.click(QT_CONFIRM)
        req = self.p.wait_packet(m, "Role_NomalHandle", "c2s", 5) or {}
        n = len((req.get("str") or "").split("-")) if req.get("str") else 0
        # 隊員是一個一個加入的：第一個 Team_TeamInfo 可能只有 1 人，要等到滿員或逾時
        members, end = 0, time.time() + 8
        while True:
            members = len(self.p.latest_team(LEADER_CONN))
            if members >= size or time.time() >= end:
                break
            self.sleep(0.5)
        self.log(f"組隊請求 {n} 人、隊伍 {members} 人")
        return n == size and members == size

    def set_fast_battle_all(self, names: Sequence[str]) -> List[str]:
        """外掛面板逐一勾快速戰鬥；回傳沒確認到的角色。已經開著的角色不要傳進來（勾選是開關）。"""
        failed = []
        self.click(WAIGUA_BTN, 2.5)
        for i, name in enumerate(names):
            self.click(WAIGUA_ROLE_RADIOS[i], 1.2)
            m = self.p.mark()
            self.click(WAIGUA_FAST_BATTLE)
            if not self.p.wait_status(m, [name, "fastBattleFlg False→True"], 6):
                failed.append(name)
        self.click(WAIGUA_CLOSE, 1.0)
        return failed


# 點畫面走路：角色約在畫面 (683,400)；地圖格 x+1 → 畫面右 62、上 46；y+1 → 右 62、下 46（2026-10-21 實機擬合）
SCREEN_CENTER = (683, 400)
TILE_PX = (62, 46)
WALK_AREA = (420, 1220, 110, 600)        # 可以點的畫面範圍（左邊 420 以內是任務欄與「詳情」按鈕，點到會開任務視窗）


def tile_to_screen(dx: int, dy: int) -> tuple:
    """相對於角色的格子位移 → 要點的畫面座標。"""
    return (int(SCREEN_CENTER[0] + TILE_PX[0] * (dx + dy)), int(SCREEN_CENTER[1] + TILE_PX[1] * (dy - dx)))


def occupied_tiles(ops: "Ops", mapid: int, tail: int = 3000) -> set:
    """最近封包裡同一張地圖上其他角色站的格子（點到人不會走路，瞄準時要避開）。"""
    where: Dict[int, tuple] = {}
    for line in ops.p._lines(ops.p.live)[-tail:]:
        e = json.loads(line)
        if e.get("name") != "Role_RoleInfoList":
            continue
        for a in (e.get("data") or {}).get("roleAttr") or []:
            p = a.get("nowPos") or {}
            if a.get("rid") and "x" in p and a.get("map", mapid) == mapid:
                where[a["rid"]] = (p["x"], p["y"])
    return set(where.values())


def wait_walk(ops: "Ops", m: Dict[str, int], conn_suffix: str, target, start_timeout: float = 1.2,
              idle: float = 0.5, limit: float = 5.0):
    """點了之後等走路結束：沒有 Role_RoleWalk → None；走到 target 或停止送封包 idle 秒 → 最後一格。"""
    t0, last, last_t = time.time(), None, None
    while time.time() - t0 < limit:
        n = 0
        for line in ops.p._lines(ops.p.live)[m["live"]:]:
            e = json.loads(line)
            if e.get("name") == "Role_RoleWalk" and e.get("dir") == "c2s" and e.get("conn", "").endswith(conn_suffix):
                p = e["data"].get("walkPath") or []
                if p:
                    n += 1
                    cur = (p[-1]["x"], p[-1]["y"])
                    if cur != last:
                        last, last_t = cur, time.time()
        if last is None and time.time() - t0 > start_timeout:
            return None
        if last is not None and (last == tuple(target) or time.time() - last_t > idle):
            return last
        ops.sleep(0.1)
    return last


def walk_to(ops: "Ops", conn_suffix: str, mapid: int, start, goal, radius: int = 2,
            max_legs: int = 40, log: Callable[[str], None] = print):
    """沿 A* 路線點畫面走路（封包驅動）：每次瞄準前方最遠、在畫面內、沒人站的格子（最多 9 格），
    走到或停下就馬上點下一段。位置以該角色送出的 Role_RoleWalk 最後一格為準；連續 3 次沒走就停。"""
    from . import pathing
    g = pathing.load_grid(mapid)
    pos, fails = tuple(start), 0
    for _ in range(max_legs):
        if max(abs(pos[0] - goal[0]), abs(pos[1] - goal[1])) <= radius:
            return pos
        path = pathing.near(g, pos, goal, radius) if radius else pathing.astar(g, pos, goal)
        if not path:
            log(f"找不到路 {pos}→{goal}"); return pos
        busy = occupied_tiles(ops, mapid)
        moved = None
        for ahead in (9, 8, 7, 6, 5, 4, 3, 2):
            wp = path[min(ahead, len(path) - 1)]
            if wp in busy and wp != path[-1]:
                continue
            sx, sy = tile_to_screen(wp[0] - pos[0], wp[1] - pos[1])
            if not (WALK_AREA[0] <= sx <= WALK_AREA[1] and WALK_AREA[2] <= sy <= WALK_AREA[3]):
                continue
            m = ops.p.mark(); ops.click((sx, sy))
            moved = wait_walk(ops, m, conn_suffix, wp)
            if moved:
                break
        if moved:
            pos, fails = moved, 0
        else:
            fails += 1
            if fails >= 3:
                log(f"連續 3 次沒走，停在 {pos}"); return pos
    return pos


def game_ops() -> Ops:
    from ..game.window import GameWindow
    w = GameWindow()
    return Ops(w.click)
