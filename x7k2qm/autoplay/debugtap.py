"""執行中除錯（使用者 2026-11-05「需要一個 runtime debug 方式」「萬一要抓其他封包抓的到嗎」）。

不重開、不重新登入，用查詢 API 臨時打開，**有時限、時間到自動關**（預設 5 分鐘、最多 30 分鐘）：

- trace：收／送的封包照名稱（萬用字元，例 `Battle_*`、`*`）和角色篩選，原始內容印到日誌（截斷 maxlen 字）
- battle：統計出招——每隻（戰鬥位置對到名字）放了什麼技能、幾次、耗多少 MP，戰場屬性分布
- history：不用先打開，直接查 Party.history（登入後收到的事件，最多約 2 萬筆）

只讀、只記錄，不送封包；唯一會動到遊戲的是「暫時關快戰」（快戰時伺服器只回勝負、不送出招），
那個由 Party 主迴圈送（headless_party.debug_fast_off／tick_debug），這裡只記時間。
2026-11-05 容器實跑：快戰 48 場只收到 Battle_BattleEnd {"win": 1}；關快戰才有 Battle_BattleCmdSeq。
"""

import fnmatch
import json
import threading
import time
from typing import Callable, Iterable, List, Optional

DEFAULT_MINUTES = 5.0
MAX_MINUTES = 30.0
DEFAULT_MAXLEN = 2000
MAX_MAXLEN = 20000
HISTORY_LIMIT = 50                    # /debug/history 預設回幾筆
HISTORY_MAX = 500
FIELD_NAMES = {0: "無", 1: "地", 2: "水", 3: "火", 4: "風"}   # BattleSeqSkillInfo.battleField（jobs/herobf FIELD_CODE）
SKILL_TYPES = {1: "普攻", 2: "防禦", 3: "技能", 9: "逃跑"}


class DebugError(ValueError):
    """參數不對（API 回 400）。"""


def minutes_of(raw, default: float = DEFAULT_MINUTES) -> float:
    if raw in (None, ""):
        return default
    try:
        m = float(raw)
    except (TypeError, ValueError):
        raise DebugError(f"minutes 要是數字：{raw!r}") from None
    if not 0 < m <= MAX_MINUTES:
        raise DebugError(f"minutes 要在 0～{MAX_MINUTES:g}：{raw!r}")
    return m


def int_of(raw, default: int, low: int, high: int, what: str) -> int:
    if raw in (None, ""):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise DebugError(f"{what} 要是整數：{raw!r}") from None
    if not low <= v <= high:
        raise DebugError(f"{what} 要在 {low}～{high}：{raw!r}")
    return v


def split(raw: Optional[str]) -> List[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def brief(obj, maxlen: int) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= maxlen else s[:maxlen] + f"…（共 {len(s)} 字）"


def matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name or "", p) for p in patterns)


def _iso(t: Optional[float]) -> Optional[str]:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)) if t else None


class DebugTap:
    def __init__(self, log: Callable[[str], None], clock: Callable[[], float] = time.time) -> None:
        self.log, self.clock = log, clock
        self.lock = threading.Lock()          # 自己的鎖：收封包執行緒、API 執行緒、主迴圈都會碰；不碰 Party.lock
        self.trace: Optional[dict] = None
        self.battle: Optional[dict] = None
        self.last_battle: Optional[dict] = None   # 時間到關掉後保留最後一份統計，GET /debug/battle 還看得到

    # ---- 開關 ----
    def start_trace(self, names: str = "", who: str = "", dirs: str = "", minutes=None, maxlen=None) -> dict:
        pats = split(names)
        if not pats:
            raise DebugError("names 要給封包名稱，可用萬用字元（例 Battle_*；全部用 *）")
        ds = set(split(dirs) or ["recv"])
        if not ds <= {"recv", "send"}:
            raise DebugError(f"dir 只能是 recv、send（逗號分隔）：{dirs!r}")
        now = self.clock()
        t = {"names": pats, "who": split(who) or None, "dirs": sorted(ds),
             "until": now + minutes_of(minutes) * 60, "maxlen": int_of(maxlen, DEFAULT_MAXLEN, 100, MAX_MAXLEN, "maxlen"),
             "count": 0, "since": now}
        with self.lock:
            self.trace = t
        self.log(f"[debug] 封包追蹤開始：{','.join(pats)}｜角色 {','.join(t['who'] or ['全部'])}｜{'、'.join(t['dirs'])}"
                 f"｜到 {_iso(t['until'])}")
        return self.status()

    def start_battle(self, minutes=None) -> dict:
        now = self.clock()
        b = {"since": now, "until": now + minutes_of(minutes) * 60, "battles": 0, "wins": 0, "pos": {},
             "skills": {}, "mp": {}, "fields": {}}
        with self.lock:
            self.battle = b
        self.log(f"[debug] 出招統計開始，到 {_iso(b['until'])}（快戰開著時伺服器不送出招，要配 /debug/fast）")
        return self.status()

    def stop(self, what: str = "all") -> dict:
        if what not in ("trace", "battle", "all"):
            raise DebugError(f"what 只能是 trace、battle、all：{what!r}")
        with self.lock:
            if what in ("trace", "all") and self.trace:
                self.trace = None
                self.log("[debug] 封包追蹤關閉")
            if what in ("battle", "all") and self.battle:
                self.last_battle, self.battle = self.battle, None
                self.log("[debug] 出招統計關閉")
        return self.status()

    def tick(self) -> None:
        """時間到自動關（收封包時、主迴圈、API 查詢都會呼叫）。"""
        now = self.clock()
        with self.lock:
            if self.trace and now >= self.trace["until"]:
                self.log(f"[debug] 封包追蹤時間到，自動關閉（印了 {self.trace['count']} 筆）")
                self.trace = None
            if self.battle and now >= self.battle["until"]:
                self.log(f"[debug] 出招統計時間到，自動關閉（{self.battle['battles']} 場）")
                self.last_battle, self.battle = self.battle, None

    def status(self) -> dict:
        self.tick()
        with self.lock:
            t, b = self.trace, self.battle
            return {"trace": None if not t else {k: t[k] for k in ("names", "who", "dirs", "maxlen", "count")}
                    | {"until": _iso(t["until"])},
                    "battle": None if not b else {"battles": b["battles"], "until": _iso(b["until"])}}

    # ---- 收／送 ----
    def on_recv(self, role: str, e: dict, lead: bool) -> None:
        if not self.trace and not self.battle:
            return
        self.tick()
        name = e.get("name") or ""
        with self.lock:
            t = self.trace
            if t and "recv" in t["dirs"] and (t["who"] is None or role in t["who"]) and matches(name, t["names"]):
                t["count"] += 1
                line = f"[debug] 收 {role} {name} {brief(e.get('data'), t['maxlen'])}"
            else:
                line = None
            if self.battle and lead:
                self._count(self.battle, name, e.get("data") or {})
        if line:
            self.log(line)

    def on_send(self, role: str, name: str, data) -> None:
        t = self.trace
        if not t or "send" not in t["dirs"]:
            return
        self.tick()
        with self.lock:
            t = self.trace
            if not t or (t["who"] is not None and role not in t["who"]) or not matches(name, t["names"]):
                return
            t["count"] += 1
            line = f"[debug] 送 {role} {name} {brief(data, t['maxlen'])}"
        self.log(line)

    @staticmethod
    def _count(b: dict, name: str, d: dict) -> None:
        if name == "Battle_BattleInit":
            b["battles"] += 1
            b["pos"] = {}
        elif name == "Battle_BattleEnd":
            b["wins"] += 1 if d.get("win") else 0
        elif name == "Battle_BattleRoleInfo":
            for no, info in (d.get("battleRoleInfo") or {}).items():
                attr = (info or {}).get("roleAttr") or {}
                if attr.get("name"):
                    b["pos"][int(no)] = attr["name"]
        elif name == "Battle_BattleCmdSeq":
            for seq in d.get("battleSeq") or []:
                si = seq.get("skillInfo") or {}
                st = si.get("skillType")
                label = (f"{si['skillId']} Lv{si.get('skillLevel')}" if si.get("skillId")
                         else SKILL_TYPES.get(st, f"類型{st}"))
                if st in SKILL_TYPES and si.get("skillId"):
                    label += f"（{SKILL_TYPES[st]}）"
                f = si.get("battleField") or 0
                b["fields"][f] = b["fields"].get(f, 0) + 1
                cost = (si.get("skillCost") or {}).get("costMp") or 0
                for no in seq.get("attackNos") or []:
                    who = b["pos"].get(no, f"#{no}")
                    row = b["skills"].setdefault(who, {})
                    row[label] = row.get(label, 0) + 1
                    if cost:
                        b["mp"][who] = b["mp"].get(who, 0) + cost

    def battle_report(self) -> dict:
        self.tick()
        with self.lock:
            b = self.battle or self.last_battle
            if not b:
                return {"active": False, "note": "還沒開過；POST /debug/battle（快戰開著要先 POST /debug/fast）"}
            return {"active": b is self.battle, "since": _iso(b["since"]), "until": _iso(b["until"]),
                    "battles": b["battles"], "wins": b["wins"],
                    "skills": {k: dict(sorted(v.items(), key=lambda kv: -kv[1])) for k, v in b["skills"].items()},
                    "mpCost": dict(b["mp"]),
                    "fields": {FIELD_NAMES.get(k, str(k)): v for k, v in sorted(b["fields"].items())},
                    "note": "skills 以戰鬥位置對到名字（對不到顯示 #位置）；合擊每個出手的人各算一次；"
                            "battles 為 0＝快戰開著（伺服器只回勝負）"}


def history(rows: List[tuple], names: str = "", who: str = "", limit=None, maxlen=None) -> dict:
    """Party.history（(角色, 事件) 清單的複本）→ 最近 limit 筆符合的。新的在後。"""
    pats = split(names) or ["*"]
    whos = split(who)
    n = int_of(limit, HISTORY_LIMIT, 1, HISTORY_MAX, "limit")
    ml = int_of(maxlen, DEFAULT_MAXLEN, 100, MAX_MAXLEN, "maxlen")
    out = []
    for role, e in reversed(rows):
        if (whos and role not in whos) or not matches(e.get("name") or "", pats):
            continue
        out.append({"time": _iso(e.get("ts")), "role": role, "name": e.get("name"), "kind": e.get("kind"),
                    "data": brief(e.get("data"), ml)})
        if len(out) >= n:
            break
    out.reverse()
    return {"count": len(out), "events": out, "note": "只有收到的封包；送出的要用 POST /debug/trace dir=send"}
