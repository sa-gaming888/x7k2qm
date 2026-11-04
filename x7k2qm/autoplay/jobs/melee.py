"""melee：週末 5V5 超級大亂鬥（一個帳號 5 隻自己組隊）；`--score` 只查今天的 PVP 積分。

    python -X utf8 -m x7k2qm.autoplay.jobs melee --profile 2 --discover --apply        # 打一段（被拒兩次就結束、回報要重登）
    python -X utf8 -m x7k2qm.autoplay.jobs.melee --profile 2 --discover --apply --relogin-max 15   # 含重登（到 21:00）
    python -X utf8 -m x7k2qm.autoplay.jobs melee --profile 2 --discover --score         # 只查積分（唯讀、不停練功）

流程（規則與協定見 doc/autoplay/game/melee.md）：
1. 5 隻組隊 → 回薩村醫院 1005 補血（已經在醫院的：解散、各自回醫院補血、在醫院重組）。
2. 等活動開始（`NewRandomPkRec_GetRandomPkInfo.isBegin`，每 15 秒查一次）。
3. **隊長**報名 `NewRandomPkRec_JoinorLeaveRandomPk{action:1}`；報名前查 5 隻的 `GetRandomPkRoleStatus`，
   有人不是 0（隊員還沒完全結束）就先等。
4. 每隻角色一條執行緒各自下指令：打對方**人物**裡 hp 最低的，人寵打同一個（第 1 回合看 `BattleInit mode 3`）。
5. 打完：隊長還在醫院就再報名；隊長不在醫院（被送回村）→ 解散、各自回醫院（`solo_hospital`）、真的重組再報名。
6. 報名被拒：第 1 次先解散重組再報名；**連續第 2 次被拒 → 結束，回報「需要重登」**（data `relogin: True`），
   由外層決定（`python -m x7k2qm.autoplay.jobs.melee ... --relogin-max N` 會登出再登入，最多 N 次）。
   根因（LESSONS #77）：解散後隊伍快取沒清，`form_team` 以為還是 5 人就不送組隊 → 一律用 `Party.dismiss_team()`。

到活動結束（`overDate`，21:00）或停止檔就結束；出錯就停：補血／組隊失敗、回醫院超過上限、被踢、連線中斷。

來源：2026-10-23～24 第二組實機跑的 `melee_team.py`、`melee_score.py`（scratchpad），註解裡的時間都是實測紀錄。
"""

import collections
import os
import sys
import threading
import time
from typing import Dict, Optional, Tuple

from . import JobError

NAME = "melee"
HELP = "週末 5V5 超級大亂鬥（--apply 打；--score 只查今天 PVP 積分）"
PVP = 90378                 # PVP積分（每場一封系統信：贏 10、輸 2）
HOSPITAL, VILLAGE = 1005, 1000
NURSE = {"npcId": 2720, "npcArg": {"scriptId": 300002, "taskId": 0, "isPetNpc": False,
                                   "npcExtraType": 0, "npcExtraId": 0}}
BEGIN_POLL = 15.0           # 等活動開始：每幾秒查一次
WAIT_BEGIN_MAX = 4 * 3600   # 等活動開始最多幾秒（沒開就停）
STATUS_EVERY = 15.0         # 在醫院時每幾秒查一次報名狀態
HEALS_MAX = 40              # 回醫院次數上限
REJECT_MAX = 2              # 連續被拒幾次就結束（第 1 次重組、第 2 次回報要重登）
BATTLE_STALE = 90.0         # 戰鬥中超過幾秒沒消息就當這場結束
MAIL_PAGES_MAX = 50
FIGHTER_TICK = 0.3

clock = time.time           # 測試換成假時鐘
sleep = time.sleep


def PAUSE(args) -> bool:
    return not getattr(args, "score", False)


def add_args(ap) -> None:
    ap.add_argument("--score", action="store_true", help="只查系統信裡今天的 PVP 積分（唯讀、不領取、不停練功）")
    ap.add_argument("--date", default=None, help="--score 要算哪一天（YYYY-MM-DD，預設今天）")
    ap.add_argument("--stop-file", default=None, help="停止檔（預設 captures/melee_<組>.stop）")
    ap.add_argument("--relogin-max", type=int, default=0,
                    help="（只有 python -m x7k2qm.autoplay.jobs.melee 入口）報名連續被拒時登出再登入，最多幾次")


def check(args) -> None:
    if getattr(args, "score", False):
        return
    if not getattr(args, "apply", False):
        raise JobError("大亂鬥要加 --apply 才會打（只查積分用 --score）")
    if getattr(args, "relogin_max", 0) < 0 or getattr(args, "relogin_max", 0) > 30:
        raise JobError("--relogin-max 要在 0～30")


# ---- 積分（唯讀）----
def mail_list(c) -> Dict[object, dict]:
    """系統信全部讀出來。分頁陷阱：翻過 maxPage 會一直重複最後一批 → 從 1 讀到 maxPage、用 msgUniqueIndex 去重、
    新增 0 筆就停，最多 MAIL_PAGES_MAX 頁。"""
    seen: Dict[object, dict] = {}
    page, maxp = 1, 1
    while page <= maxp and page <= MAIL_PAGES_MAX:
        r = c.request("NewEmail_GetNewSystemThingsList", {"page": page}) or {}
        maxp = r.get("maxPage") or 1
        new = [m for m in r.get("msgInfoList") or [] if m.get("msgUniqueIndex") not in seen]
        for m in new:
            seen[m["msgUniqueIndex"]] = m
        if not new:
            break
        page += 1
    return seen


def tally(mails, day: str) -> Dict[str, list]:
    """那一天的 PVP 積分：收件人 → [封數, 分數]。"""
    per: Dict[str, list] = collections.defaultdict(lambda: [0, 0])
    for m in mails:
        if m.get("thingId") == PVP and time.strftime("%Y-%m-%d", time.localtime(m.get("date") or 0)) == day:
            who = m.get("thingGetName") or m.get("thingGetRid")
            per[who][0] += 1
            per[who][1] += m.get("thingCount") or 0
    return dict(per)


def score(p, args) -> Tuple[str, dict]:
    day = getattr(args, "date", None) or time.strftime("%Y-%m-%d", time.localtime(clock()))
    seen = mail_list(p.leader())
    per = tally(seen.values(), day)
    wins = {w: (s - 2 * n) // 8 for w, (n, s) in per.items()}       # 贏 10、輸 2 → 勝場 = (分數 − 2×封數) / 8
    return (f"系統信共 {len(seen)} 封；{day} PVP 積分（收件人：[封數, 分數]）{per}；推算勝場 {wins}",
            {"mails": len(seen), "per": per, "wins": wins})


# ---- 大亂鬥 ----
class Melee:
    def __init__(self, p, stop_file: Optional[str] = None):
        self.p = p
        self.names = [n for _, n in p.party]
        self.lead_name = self.names[0]
        self.lead = p.clients[self.lead_name]
        self.stop_file = stop_file
        self.stats: Dict[str, dict] = {}
        self.in_battle: Dict[str, bool] = {}
        self.stopped = False
        self.threads = []

    def log(self, m: str) -> None:
        self.p.log(m)

    def stop_requested(self) -> bool:
        if self.stop_file and os.path.exists(self.stop_file):
            os.remove(self.stop_file)
            return True
        return False

    def broken(self) -> Optional[str]:
        """被踢或連線斷了 → 回原因（出錯就停）。"""
        if self.p.s.kicked:
            return "有角色被踢下線"
        dead = [n for n, c in self.p.clients.items() if getattr(c, "closed", False)]
        return f"連線中斷（{'、'.join(dead)}）" if dead else None

    def maps(self) -> dict:
        with self.p.lock:
            return dict(self.p.s.member_maps)

    def solo_hospital(self, name: str) -> bool:
        """單隻：傳薩村 → 走到醫院門口 → 進醫院 → 走到小護士旁 → 補血（隊員不能自己傳送，先解散隊伍再做）。
        一律從村子走一趟：留在醫院的也要補（10-23 20:32 實測：全員在 1005 但輸了沒補血 → 報名一直被拒、沒提示）。"""
        from ..headless_party import HEAL_PATH_HOSPITAL, HEAL_PATH_VILLAGE
        from ..pathing import walk_payload
        p, c = self.p, self.p.clients[name]
        r = c.request("Transfer_TransferTo", {"point": 1}) or {}
        if not r.get("isOK") or not p.wait(lambda: p.s.member_maps.get(name) == VILLAGE, 15):
            self.log(f"{name} 傳薩村失敗 {r}")
            return False
        c.send("Role_RoleWalk", walk_payload(VILLAGE, HEAL_PATH_VILLAGE))
        p.nap((len(HEAL_PATH_VILLAGE) - 1) / 4.0 + 0.5)          # 4 格/秒，不瞬移
        c.request("Role_RoleWarp", {"map": VILLAGE, "x": 80, "y": 66})
        if not p.wait(lambda: p.s.member_maps.get(name) == HOSPITAL, 8):
            self.log(f"{name} 進不了醫院")
            return False
        c.send("Role_RoleWalk", walk_payload(HOSPITAL, HEAL_PATH_HOSPITAL))
        p.nap((len(HEAL_PATH_HOSPITAL) - 1) / 4.0 + 0.5)
        c.send("Npc_NpcInteract", NURSE)
        p.nap(1)
        return True

    def ready_team(self, first: bool = False) -> None:
        """組好 5 人隊、全員在醫院補過血。失敗丟 JobError。"""
        p = self.p
        if first:                              # 開場：全隊一起傳（大家都在練功地圖同一處）
            if not p.form_team():
                raise JobError("組隊失敗")
            p.set_loop(False)
            if not p.heal_at_samugil():
                raise JobError("回醫院補血失敗")
            return
        # 打完：有人被送回村、有人留在醫院，伺服器還當 5 人隊但隊員不會跟（10-23 20:21 實測）→ 解散、各自回醫院、重組。
        # 一定要用 dismiss_team()：它會清隊伍快取，不然 form_team 以為還是 5 人就不送組隊（LESSONS #77）
        p.dismiss_team()
        for n in self.names:
            if not self.solo_hospital(n):
                raise JobError(f"{n} 回醫院失敗")
        if not p.form_team():
            raise JobError("在醫院重組失敗")

    # ---- 每隻角色的戰鬥 ----
    def fighter(self, name: str) -> None:
        """每隻各自處理戰鬥（第 1 回合看 BattleInit mode 3、之後看 CmdBegin），打對方人物 hp 最低的。"""
        p, c = self.p, self.p.clients[name]
        st = self.stats.setdefault(name, {"戰鬥": 0, "勝": 0})
        with p.lock:
            seen = len(p.history)              # 只處理開始之後的新事件（不要把登入後的舊戰鬥算進來）
        ctx = {"units": {}, "my_pos": None, "want": None, "seen_at": 0.0}
        while not self.stopped:
            with p.lock:
                new = [e for who, e in p.history[seen:] if who == name]
                seen = len(p.history)
            for e in new:
                self.on_event(name, c, st, ctx, e)
            self.maybe_command(name, c, ctx)
            if self.in_battle.get(name) and clock() - ctx["seen_at"] > BATTLE_STALE:
                self.in_battle[name] = False
            sleep(FIGHTER_TICK)

    def on_event(self, name, c, st, ctx, e) -> None:
        n, d = e.get("name", ""), e.get("data") or {}
        if n in ("Battle_BattleInit", "Battle_BattleCmdBegin") or (n == "Battle_BattleAllInfo" and d.get("isBattle")):
            if not self.in_battle.get(name):
                st["戰鬥"] += 1
            self.in_battle[name], ctx["seen_at"] = True, clock()
        if n == "Battle_BattleRoleInfo":
            ctx["seen_at"] = clock()
            for u in d.get("battleRoleInfo") or []:
                a, pos = u.get("roleAttr") or {}, u.get("rolePos")
                unit = ctx["units"].setdefault(pos, {})
                if "hp" in a:
                    unit["hp"] = a["hp"]
                if a.get("roleType"):
                    unit["type"] = a["roleType"]
                if a.get("rid") == c.rid and a.get("roleType") == 1:     # 寵物的 rid 也是主人的 → 要 roleType 1
                    ctx["my_pos"] = pos
        if n == "Battle_BattleCmdBegin":
            ctx["want"] = (d, clock())
        if n == "Battle_BattleInit" and (d.get("battleScene") or {}).get("mode") == 3:
            ctx["want"] = ({"canRoleSend": True, "canPetSend": True}, clock())   # 第 1 回合沒有 CmdBegin
        if n == "Battle_BattleCmdSeq":
            c.send("Battle_BattlePlayOver", {})
            c.send("Battle_BattleEscapeOver", {})
        if n == "Battle_BattleEnd":
            st["勝"] += d.get("win") == 1
            self.in_battle[name] = False
            ctx["units"], ctx["my_pos"] = {}, None
            self.log(f"{name} 戰鬥結束 win={d.get('win')}（{st}）")
        if n == "Role_RoleInfo":                 # BattleEnd 有時收不到 → 也看 status 歸 0 或換了地圖
            a = d.get("roleAttr") if isinstance(d.get("roleAttr"), dict) else d
            if self.in_battle.get(name) and (a.get("status") == 0 or ("map" in a and a["map"] != HOSPITAL)):
                self.in_battle[name] = False
                ctx["units"], ctx["my_pos"] = {}, None
                self.log(f"{name} 這場結束（{'回到地圖 ' + str(a['map']) if 'map' in a else 'status 0'}）")

    def maybe_command(self, name, c, ctx) -> None:
        want = ctx["want"]
        if not (want and self.in_battle.get(name) and (ctx["my_pos"] is not None or clock() - want[1] > 2)):
            return
        cmd, ctx["want"] = want[0], None
        units, my_pos = ctx["units"], ctx["my_pos"]
        side = my_pos // 10 if my_pos is not None else None
        foes = [q for q, u in units.items() if q is not None and side is not None and q // 10 != side and u.get("hp", 1) > 0]
        foes = [q for q in foes if units[q].get("type") == 1] or foes      # 先打人物
        foes.sort(key=lambda q: units[q].get("hp", 10 ** 9))
        base = {"cmdType": 1, "cmdArg": "", "cmdArgEx": "", "cmdTarget": foes[0] if foes else 0,
                "changeCmd": "", "isAuto": 0, "realRid": 0}
        if cmd.get("canRoleSend", True):
            c.send("Battle_BattleRoleCmd", dict(base, roleType=1))
        if cmd.get("canPetSend", True):
            c.send("Battle_BattleRoleCmd", dict(base, roleType=2))

    def start_fighters(self) -> None:
        self.threads = [threading.Thread(target=self.fighter, args=(n,), daemon=True) for n in self.names]
        for t in self.threads:
            t.start()

    # ---- 主流程 ----
    def wait_begin(self) -> Optional[dict]:
        """等活動開始；停止檔／被踢／等太久回 None。"""
        start = clock()
        while True:
            if self.stop_requested():
                return None
            why = self.broken()
            if why:
                raise JobError(why)
            info = self.lead.request("NewRandomPkRec_GetRandomPkInfo", {}) or {}
            if info.get("isBegin"):
                return info
            over = info.get("overDate")
            if (over and clock() >= over) or clock() - start > WAIT_BEGIN_MAX:
                raise JobError(f"等不到活動開始（{info}）")
            self.p.nap(BEGIN_POLL)

    def statuses(self) -> Dict[str, object]:
        """5 隻的報名狀態（0 未報名、1 已報名等配對）；隊長不是 0 就不用查隊員。"""
        out = {self.lead_name: (self.lead.request("NewRandomPkRec_GetRandomPkRoleStatus", {}) or {}).get("status")}
        if out[self.lead_name] == 0:
            for n in self.names[1:]:
                out[n] = (self.p.clients[n].request("NewRandomPkRec_GetRandomPkRoleStatus", {}) or {}).get("status")
        return out

    def join(self) -> dict:
        p = self.p
        r = self.lead.request("NewRandomPkRec_JoinorLeaveRandomPk", {"action": 1}) or {}
        with p.lock:
            tips = [e.get("data") for who, e in p.history[-20:] if who == self.lead_name and e.get("name") == "Other_SystemTip"]
        self.log(f"隊長報名：{r}；最近提示 {tips[-2:]}")
        return r

    def fight(self) -> dict:
        """從組隊到活動結束。回傳 {joins, rejects, heals, relogin, reason}；出錯丟 JobError。"""
        p = self.p
        info = self.lead.request("NewRandomPkRec_GetRandomPkInfo", {}) or {}
        self.log(f"5V5：{self.names}；活動 {info}")
        self.ready_team(first=self.maps().get(self.lead_name) != HOSPITAL)
        info = self.wait_begin()
        res = {"joins": 0, "rejects": 0, "heals": 0, "relogin": False, "reason": ""}
        if info is None:
            res["reason"] = "收到停止檔"
            return res
        over = info.get("overDate") or (clock() + 3600)
        res["over"] = over
        self.log(f"活動開始：{info}")
        self.start_fighters()
        fails, next_check = 0, 0.0
        while True:
            if clock() >= over:
                res["reason"] = "活動結束"
                return res
            if self.stop_requested():
                res["reason"] = "收到停止檔"
                return res
            why = self.broken()
            if why:
                raise JobError(why)
            if any(self.in_battle.values()):
                p.nap(1)
                continue
            # 只看隊長在不在醫院（隊員地圖推送不一定會更新，10-23 20:05 實測：補完血還是判成不在 → 空轉補血 40 次）
            if self.maps().get(self.lead_name) != HOSPITAL:
                res["heals"] += 1
                self.log(f"不在醫院，回去補血（各隻地圖 {self.maps()}）")
                if res["heals"] > HEALS_MAX:
                    raise JobError(f"回醫院超過 {HEALS_MAX} 次")
                self.ready_team()
                next_check = 0.0
                continue
            if clock() < next_check:
                p.nap(1)
                continue
            next_check = clock() + STATUS_EVERY
            ms = self.statuses()
            if ms[self.lead_name] != 0:          # 已報名等配對（或戰鬥中）
                p.nap(1)
                continue
            if any(v != 0 for v in ms.values()):
                # 使用者 2026-10-24：「是不是隊員還沒有完全結束」——有人不是 0 就先等，不報名
                self.log(f"隊員還沒結束，先不報名：{ms}")
                p.nap(1)
                continue
            r = self.join()
            res["joins"] += 1
            if r.get("isOK"):
                fails = 0
                p.nap(1)
                continue
            fails += 1
            res["rejects"] += 1
            if fails >= REJECT_MAX:
                # 真的重組過還是被拒：只有重新登入才會過（10-23 20:22、20:34 實測）→ 結束這次，外層決定要不要重登
                self.log(f"報名連續 {fails} 次被拒，需要重新登入（各隻地圖 {self.maps()}）")
                res["relogin"], res["reason"] = True, f"報名連續 {fails} 次被拒，需要重新登入"
                return res
            # 第 1 次被拒：多半是隊伍其實散了（LESSONS #77）→ 解散、各自回醫院、真的重組再報名
            self.log(f"報名被拒，先解散各自回醫院重組（各隻地圖 {self.maps()}）")
            self.ready_team()
            next_check = 0.0

    def close(self) -> None:
        self.stopped = True
        for t in self.threads:
            t.join(timeout=2)


def default_stop_file(args) -> str:
    return getattr(args, "stop_file", None) or os.path.join("captures", f"melee_{getattr(args, 'profile', 'x')}.stop")


def run(p, args) -> Tuple[str, dict]:
    if getattr(args, "score", False):
        return score(p, args)
    m = Melee(p, default_stop_file(args))
    try:
        res = m.fight()
    finally:
        m.close()
        p.log(f"大亂鬥戰績：{m.stats}")
    res["stats"] = m.stats
    detail = (f"大亂鬥結束（{res['reason']}）：報名 {res['joins']} 次、被拒 {res['rejects']} 次、"
              f"回醫院 {res['heals']} 次；{m.stats}")
    return detail, res


# ---- 含重登的入口（取代原本外層的 bash 迴圈）----
def main(argv=None, party_factory=None, pause_between: float = 10.0) -> int:
    """`python -m x7k2qm.autoplay.jobs.melee --profile N [--discover] --apply [--relogin-max 15]`

    一次 = 登入 → run_job → 登出。結果是「需要重登」且還沒到活動結束、重登次數 < --relogin-max 才再登入一次；
    其他情況（活動結束、停止檔、出錯）都直接結束。結束碼 0＝正常結束、1＝出錯或重登次數用完。"""
    import argparse
    from . import run_job
    from ..logrotate import RotatingFile
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.jobs.melee", description=HELP)
    ap.add_argument("--profile", required=True, help="第幾組帳號（BOT_ACCOUNT_<組>）；必填")
    ap.add_argument("--discover", action="store_true", help="角色清單從登入結果取")
    ap.add_argument("--apply", action="store_true", help="真的打（--score 不用）")
    ap.add_argument("--log", default="captures/jobs.log")
    add_args(ap)
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    fp = RotatingFile(args.log, max_bytes=5 * 1024 * 1024, keep=10)

    def log(msg: str) -> None:
        line = f"{time.strftime('%m-%d %H:%M:%S')} [{NAME}] {msg}"
        print(line, flush=True)
        fp.write(line + "\n")
    if party_factory is None:
        from ..headless_party import Party

        def party_factory(log):
            return Party(log, profile=args.profile, discover=args.discover)
    try:
        check(args)
    except JobError as e:
        log(f"停止：{e}")
        return 1
    relogins, over = 0, None
    while True:
        p = party_factory(log)
        try:
            detail, data = run_job(sys.modules[__name__], args, p)
            log(detail)
        except JobError as e:
            log(f"停止：{e}")
            return 1
        except Exception as e:  # noqa: BLE001
            log(f"停止：例外 {type(e).__name__}: {e}")
            return 1
        finally:
            p.close()
        if not data.get("relogin"):
            return 0
        if relogins >= args.relogin_max:
            log(f"停止：需要重登，但已經重登 {relogins} 次（上限 --relogin-max {args.relogin_max}）")
            return 1
        over = over or data.get("over")
        if over and clock() >= over:
            log("活動已結束，不再重登")
            return 0
        relogins += 1
        log(f"重新登入（第 {relogins}／{args.relogin_max} 次）")
        sleep(pause_between)


if __name__ == "__main__":
    sys.exit(main())
