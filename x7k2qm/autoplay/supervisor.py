"""練功監管程式：讀封包狀態 → 決定要做什麼 → 用 ops 執行（每個動作都等封包確認）。

    python -X utf8 -m x7k2qm.autoplay.supervisor --log captures/autoplay_supervisor.log

決策（decide）是純函式、可測；執行（run）才碰遊戲。規則來源：.claude/skills/x7k2qm-training/SKILL.md。
- 遇敵被關：如果不是監管程式自己關的（使用者手動），**不蓋掉**，等使用者重新開。
- 隊伍少於 5 人 → 重新組隊；有人不在隊長的地圖（被打飛）→ 傳回去再組隊。
- 隊伍最低血量 < 50%（使用者規則）→ **優先回村補血**，補完回練功地圖；連續補 3 次還是過低就停。
- 隊長整個離開練功路線（全隊被打死送回記錄點、被任務傳走）→ 停止並回報，不要在村裡空轉。
- 近 5 分鐘敗場 ≥ 20% → 退回上一張；全隊最低等級 ≥ 下一張門檻 → 換下一張（等級接近才跳）。
- 被擠下線、連續 3 次動作失敗 → 停止並寫日誌。
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 練功路線：(練級傳送名稱, 地圖 ID, 換過來的最低等級)。
# 使用者 2026-10-23：「1 轉後因為有強寵，等級達標儘量往下一關練等，經驗才會多」→ 門檻改成伺服器的傳送等級門檻
# （71266 實測：五兄弟 45、柯奧4F 60、波拉島 80、地雷區 120；見 doc/autoplay/game/map-limits.md）。打不贏照舊退圖。
# 舊值（0 轉時「接近建議等級」）：46／70／81／125。
ROUTE: List[Tuple[str, int, int]] = [
    ("柯奧龜洞Lv10", 11001, 1),
    ("阿布龜洞Lv20", 10001, 12),
    ("海主人隧道Lv30", 31401, 20),
    ("五兄弟洞窟Lv60", 31706, 45),
    ("柯奧4F龜洞Lv80", 11004, 60),
    ("波拉島Lv120", 500, 80),
    ("地雷區Lv130", 60049, 120),
]
LOSE_RATE_RETREAT = 0.20
MIN_BATTLES_FOR_RATE = 8
TEAM_SIZE = 5
HEAL_BELOW = 50        # 使用者 2026-10-22：隊伍血量低於 50% 要**優先**回村補血
MAX_HEALS = 3          # 補完還是過低就停，不要一直在村子和練功地圖之間來回
# 5 分鐘內補血這麼多次 = 這張圖打不過（滿血回去幾十秒就又掉到門檻以下），退回上一張。
# 2026-10-22 實機：81 級在波拉島 36 秒補一次，而且場場都贏，所以靠「連續補血次數」永遠抓不到
# （打贏就歸零）；要用「時間窗內的次數」。
# 11-05 從 3 調成 6（LESSONS #164）：3 次＝平均 100 秒補一次就退，容器五組在波拉島全勝、約 60 秒補一次也被退回，
# 在柯奧4F 100 級一小時才升 1 級。補一次約 20 秒，60 秒一次也還有六成時間在高等圖打；6 次（≤50 秒一次）才算打不過，
# 10-22 那次 36 秒一次照樣會退。
HEALS_PER_WINDOW = 6
REPORT_EVERY = 300.0   # 每 5 分鐘印一次狀態摘要


@dataclass
class State:
    leader_map: Optional[int] = None
    member_maps: Dict[str, int] = field(default_factory=dict)   # 名字 → 地圖
    levels: Dict[str, int] = field(default_factory=dict)
    team: List[str] = field(default_factory=list)
    loop_on: bool = False
    loop_off_by_us: bool = False
    wins: int = 0
    losses: int = 0
    kicked: bool = False
    # 刻意移動中（傳送、跑任務、回村補血）：這段期間隊長本來就會離開練功地圖，不要當成異常
    moving: bool = False
    # 使用者指定「就待在這張圖」：不自動退圖（補血、組隊、接人、換下一張都照舊）。
    # 2026-10-22：106 級時柯奧4F已經榨不出經驗（10 分鐘 124 場全勝、0 升級），
    # 退回去反而比在波拉島一直補血更慢，所以要能關掉退圖。
    stay: bool = False
    # 使用者 2026-10-23「不允許退回」：只擋退圖，**前進照舊**。
    # 和 stay 的差別：stay 是兩邊都鎖（練寵用），這個只保證不往回走。
    no_retreat: bool = False
    hp_percent: Optional[int] = None   # 隊伍（人物＋戰寵／騎寵）最低血量百分比；不知道就 None
    heals: int = 0                     # 這一輪連續補血幾次（補完血量還是過低就不要一直跑）
    heals_recent: int = 0              # 近 5 分鐘補血幾次（用來判斷這張圖是不是打不過）


# 不在自動路線上、只能用 `--start <名稱> --stay` 鎖著練的練功點（使用者 2026-11-02「第七第八組去英雄島」→ 選波拉島2）。
# 不放進 ROUTE：放進去會改變所有組的自動前進順序（500 → 502 → 60049）。在這些圖上不退圖、不前進。
SPOTS: List[Tuple[str, int, int]] = [
    ("波拉島2Lv120", 502, 80),       # 傳送點 23；門檻沿用波拉島（伺服器 0 轉 80 級，未實測 502）
    ("大象區Lv80", 300, 0),          # 傳送點 40，吉魯島，無門檻
]
SPOT_MAPS = frozenset(m for _, m, _ in SPOTS)


def route_index(mapid: Optional[int]) -> Optional[int]:
    for i, (_, m, _) in enumerate(ROUTE):
        if m == mapid:
            return i
    return None


def decide(s: State, names: List[str]) -> Tuple[str, Optional[str]]:
    """回傳 (動作, 參數)。動作：stop / wait / heal / fetch(名字) / team / retreat(地點) / advance(地點) / loop / ok。"""
    if s.kicked:
        return "stop", "被擠下線"
    if not s.loop_on and not s.loop_off_by_us:
        return "wait", "遇敵被手動關閉，等使用者重新開"
    # 使用者 2026-10-22：**隊伍血量低於 50% 優先回村補血**。擺在「不在練功地圖就停」之前，
    # 因為全隊被打死送回記錄點時血量正好也很低——那種情況補血才是正解，停下來只是卡住。
    if s.hp_percent is not None and s.hp_percent < HEAL_BELOW:
        if s.heals >= MAX_HEALS:
            return "stop", f"連續補血 {s.heals} 次，血量還是只有 {s.hp_percent}%"
        return "heal", f"隊伍最低血量 {s.hp_percent}%"
    # 補太頻繁＝這張圖打不過（場場贏也一樣，傷害吃不消）→ 退回上一張。
    # 一定要擺在補血**之後**：血量還低就退圖等於帶著殘血傳走，而且下一張圖血量仍低、
    # heals_recent 也還在時間窗內，會一路往回退到第一張（2026-10-22 第一版就是這樣）。
    # 補完血（血量已滿）才退，退完由呼叫端清掉補血時間戳，在新地圖重新評估。
    i = route_index(s.leader_map)
    if s.heals_recent >= HEALS_PER_WINDOW and i is not None and i > 0 \
            and not s.stay and not s.no_retreat:
        return "retreat", ROUTE[i - 1][0]
    # 隊長不在練功路線上的任何一張地圖：多半是全隊被打死送回記錄點（薩姆吉爾村 1000），
    # 也可能被任務傳走。route_index 會回 None，retreat／advance 都不會觸發，舊版一路回報「ok」
    # 讓隊伍在村裡空轉（2026-10-22 第三組 5 分鐘 0 勝 2 敗卻顯示 ok；LESSONS #31「安靜不等於正常」）。
    # 要擺在 away 之前：隊長在村、隊員還在練功地圖時，先停下來，不要反過來把隊員抓去村裡。
    if s.leader_map is not None and route_index(s.leader_map) is None and s.leader_map not in SPOT_MAPS \
            and not s.moving:
        return "stop", f"隊長不在練功地圖（地圖 {s.leader_map}），可能被打回記錄點或被傳走"
    away = [n for n in names if s.member_maps.get(n) not in (None, s.leader_map)]
    if away:
        return "fetch", away[0]
    if len([n for n in s.team if n]) < TEAM_SIZE:
        return "team", None
    i = route_index(s.leader_map)
    total = s.wins + s.losses
    if i is not None and i > 0 and total >= MIN_BATTLES_FOR_RATE and s.losses / total >= LOSE_RATE_RETREAT \
            and not s.stay and not s.no_retreat:
        return "retreat", ROUTE[i - 1][0]
    # stay＝鎖在這張圖：**退圖和前進都不做**。舊版只擋退圖，練寵時角色等級夠就被帶去更高的圖
    # （2026-10-23 第二組：練火雞時從五兄弟被帶去柯奧 4F，低等寵物上場就被秒）。
    if i is not None and i + 1 < len(ROUTE) and s.levels and min(s.levels.values()) >= ROUTE[i + 1][2] \
            and not s.stay:
        return "advance", ROUTE[i + 1][0]
    if not s.loop_on:
        return "loop", None
    return "ok", None


# ---------------------------------------------------------------------------
# 讀封包狀態
# ---------------------------------------------------------------------------

def read_state(live_path: str, conns: Dict[str, str], leader_conn: str, window: float = 300.0,
               tail: int = 20000) -> State:
    """conns：連線尾碼 → 角色名。只讀檔案最後 tail 行。"""
    s = State()
    now = time.time()
    with open(live_path, encoding="utf-8") as fp:
        lines = fp.readlines()[-tail:]
    best_team: List[str] = []
    for line in lines:
        e = json.loads(line)
        c = e.get("conn", "")[-5:]
        name = e.get("name")
        if c not in conns:
            continue
        who = conns[c]
        if name == "Role_RoleInfo" and e.get("dir") == "s2c":
            a = (e.get("data") or {}).get("roleAttr") or (e.get("data") or {})
            if a.get("map") is not None:
                s.member_maps[who] = a["map"]
                if c == leader_conn:
                    s.leader_map = a["map"]
            if a.get("level") is not None:
                s.levels[who] = a["level"]
            if c == leader_conn and "autoEncountFlag" in a:
                s.loop_on = bool(a["autoEncountFlag"])
        elif name == "Team_TeamInfo" and c == leader_conn:
            team = [m.get("roleAttr", {}).get("name") for m in (e.get("data") or {}).get("teamMembers") or []]
            if len(team) > 1 or not best_team:
                best_team = team
        elif name == "Battle_BattleEnd" and c == leader_conn and now - e.get("ts", 0) <= window:
            w = (e.get("data") or {}).get("win")
            s.wins += w == 1
            s.losses += w == 2
        elif name == "System_KickConnect":
            s.kicked = True
    s.team = best_team
    return s


def carry_over(s: State, prev: State) -> State:
    """地圖、等級只在變化時才有封包，讀取範圍內沒有就沿用上一次的值（2026-10-21 回報「地圖 None」）。"""
    if s.leader_map is None:
        s.leader_map = prev.leader_map
    s.member_maps = {**prev.member_maps, **s.member_maps}
    s.levels = {**prev.levels, **s.levels}
    if not s.team:
        s.team = prev.team
    return s


# ---------------------------------------------------------------------------
# 執行
# ---------------------------------------------------------------------------

def run(interval: float, log) -> int:
    from .ops import LEADER_CONN, SWITCH_SLOTS, TELEPORT_BTN, TELEPORT_TRAIN_TAB, TRAIN_POINTS, game_ops
    conns = {"61466": "角色95", "61467": "角色34", "61468": "角色54", "61469": "角色44", "61470": "角色62"}
    slots = {n: i for i, n in enumerate(conns.values())}
    leader, leader_rid = "角色95", 10003754
    members = [n for n in conns.values() if n != leader]
    o = game_ops()
    o.log = log
    fails, off_by_us, last_move, last_report = 0, False, 0.0, 0.0
    known = read_state(o.p.live, conns, LEADER_CONN, tail=10 ** 7)   # 啟動時讀整份，取得目前地圖與等級
    while True:
        s = carry_over(read_state(o.p.live, conns, LEADER_CONN), known)
        known = s
        s.loop_off_by_us = off_by_us
        act, arg = decide(s, list(conns.values()))
        if time.time() - last_report >= REPORT_EVERY:
            last_report = time.time()
            lv = "、".join(f"{n}{s.levels.get(n, '?')}" for n in conns.values())
            log(f"狀態：地圖 {s.leader_map}｜等級 {lv}｜隊伍 {len(s.team)} 人｜近 5 分鐘 勝 {s.wins}／敗 {s.losses}"
                f"｜遇敵 {'開' if s.loop_on else '關'}｜判斷 {act}")
        if act == "ok":
            fails = 0
        elif act == "stop":
            log(f"停止：{arg}"); return 1
        elif act == "wait":
            pass
        else:
            log(f"動作：{act} {arg or ''}（地圖 {s.leader_map}、等級 {sorted(set(s.levels.values()))}、近 5 分鐘 勝{s.wins}/敗{s.losses}、隊伍 {len(s.team)} 人）")
            ok = True
            if act in ("fetch", "team", "retreat", "advance"):
                off_by_us = True
                ok = o.pause_battle(leader, leader_rid)
            if ok and act == "fetch":
                ok = o.switch_to(slots[arg])
                point = next((p for p, (xy, m) in TRAIN_POINTS.items() if m == s.leader_map), None)
                if ok and point:
                    m = o.p.mark()
                    o.click(TELEPORT_BTN, 2.5); o.click(TELEPORT_TRAIN_TAB, 1.5); o.click(TRAIN_POINTS[point][0])
                    ok = bool(o.p.wait_status(m, [arg, f"→{s.leader_map}"], 15))
                    o.sleep(4)
                ok = ok and o.switch_to(slots[leader]) and o.quick_team()
            elif ok and act == "team":
                ok = o.quick_team()
            elif ok and act in ("retreat", "advance"):
                if time.time() - last_move < 240:
                    log("剛換過地圖，先觀察"); ok = True
                else:
                    ok = o.teleport_team(arg, leader, members); last_move = time.time()
            if ok:
                ok = o.set_loop(leader, True)
                off_by_us = not ok
            fails = 0 if ok else fails + 1
            if fails >= 3:
                log("連續 3 次動作失敗，停止"); return 1
        time.sleep(interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.supervisor")
    ap.add_argument("--log", required=True)
    ap.add_argument("--interval", type=float, default=20.0)
    args = ap.parse_args(argv)
    from .logrotate import RotatingFile
    fp = RotatingFile(args.log, max_bytes=5 * 1024 * 1024, keep=10)   # 5MB 輪替、壓縮、留 10 份

    def log(msg: str) -> None:
        fp.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    log("監管開始")
    try:
        return run(args.interval, log)
    except Exception as e:  # noqa: BLE001
        log(f"停止：例外 {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
