"""無畫面五開的本機唯讀查詢 API：練功程式記憶體裡已有的狀態，開 HTTP 隨時查，不用另外登入
（另外登入會把正在練功的角色擠下線，同一角色只能一處登入）。

    python -X utf8 -m x7k2qm.autoplay.headless_party --log captures/autoplay_headless.log --api-port 8791
    python -X utf8 -m x7k2qm.autoplay.status_api bag          # 查詢工具（印成中文表格）

安全：
  - **只聽 127.0.0.1**、**只有 GET**（其他方法回 405）、不提供任何會改變遊戲狀態的動作。
    唯一例外 `POST /infolog`：只打開「人物寵物每分鐘印一次、5 分鐘後自動關」的日誌，不送封包、不碰遊戲狀態。
  - 記憶體狀態（/state /roles /role /bag /equip /pets /tasks /buffs /battle /waigua）**不送任何封包**，
    讀的時候拿 `Party.lock` 一次讀完；持鎖期間**不呼叫** Party 裡也要拿這把鎖的函式
    （bag、snapshot、state、locks、pet_index、wounded、hp_lows…；threading.Lock 不可重入 → 死結）。
  - 倉庫（/depot /petdepot）要在練功連線上送「查詢」請求（唯讀，不存不取）：
    走 GameClient 的 0.3～0.8 秒間隔；單次查詢最多 `MAX_REQUESTS` 個請求，超過就停；
    結果（含失敗）快取 `CACHE_SECONDS` 秒；同時只跑一個倉庫查詢；任何請求失敗就回錯誤＋已拿到的部分，不重試。
    送請求時**不持有 Party.lock**（回應由收封包執行緒處理，它的監聽器也要拿這把鎖）。

端點與欄位見 doc/autoplay/flows/headless-party.md §5.3。
"""

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple

MAX_REQUESTS = 30           # 倉庫查詢：單次最多送幾個請求
CACHE_SECONDS = 60.0        # 倉庫查詢：結果快取秒數
MAX_PERSONAL_PAGES = 5      # 個人倉庫頁數上限（Config maxItemDepotPlace／maxPetDepotPlace 5）
MAX_PUBLIC_PAGES = 10       # 公共倉庫最多查幾頁（實際上限 99／20 頁，請求數另有 MAX_REQUESTS 擋）
DEPOT_TIMEOUT = 8.0         # 單一請求等回應的秒數
BASE_PORT = 8789            # 每組各自一個 port：第 2 組 8791、第 3 組 8792…（交易所常駐用 8787）

POINT_TYPES = {1: "自訂", 2: "推薦"}             # RoleAttr.autoSetPointType
POINT_ATTRS = {1: "體力", 2: "腕力", 3: "耐力", 4: "速度"}   # autoPointDiyList 的 type
STATUS_NAMES = {2: "出戰", 3: "騎乘"}          # 其他值＝休息
TASK_STATUS = {-1: "可接", 0: "無", 1: "進行中", 2: "完成", 3: "放棄", 4: "進度更新", 5: "可接條件", 100: "未完成"}
# Item.type → 部位（headless_party 的 CHAR_EQUIP_TYPES 註解）
EQUIP_PART = {0: "武器", 1: "武器", 2: "武器", 3: "武器", 4: "武器", 17: "武器", 18: "武器", 19: "武器",
              6: "兜", 7: "鎧", 8: "手環", 9: "貝鈴", 10: "項鍊", 11: "戒指", 13: "耳飾", 15: "護身符",
              24: "腰帶", 25: "盾", 26: "鞋", 27: "手套"}
# /role 先列的重點欄位（RoleAttr；其餘欄位放 attr 全部列）
ROLE_KEYS = ("level", "trans", "exp", "nextExp", "hp", "hpMax", "mp", "mpMax", "vitality", "strength", "stamina",
             "speed", "potentialPoint", "autoSetPointType", "earth", "water", "fire", "wind",
             "stone", "gold", "lockDenar", "map", "nowPos", "job", "bagNum", "itemDepotPlace", "petDepotPlace",
             "pItemDepotPlace", "pPetDepotPlace", "rideLevel")
ROLE_LABELS = {"vitality": "體力", "strength": "腕力", "stamina": "耐力", "speed": "速度", "potentialPoint": "剩餘點數",
               "autoSetPointType": "自動配點（1 自訂、2 推薦）", "earth": "地", "water": "水", "fire": "火", "wind": "風",
               "exp": "經驗", "nextExp": "下一級經驗"}
DEPOTS = {
    "depot": {"proto": "ItemHandle_GetNewItemDepotInfo", "list": "itemInfoList",
              "pages": ("itemDepotPlace", "pItemDepotPlace"), "extra": {}},
    "petdepot": {"proto": "NewPet_GetNewPetDepotInfo", "list": "petInfoList",
                 "pages": ("petDepotPlace", "pPetDepotPlace"), "extra": {"isAll": False}},
}


def port_for(profile: str) -> int:
    """每組帳號的預設 port（第 2 組 8791）。"""
    return BASE_PORT + int(profile)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _values(v) -> list:
    """sproto 的 map<int64,X> 解出來可能是 dict 或 list，統一成 list。"""
    if isinstance(v, dict):
        return list(v.values())
    return list(v or [])


class Views:
    """記憶體狀態的唯讀檢視。每個方法拿 Party.lock 一次，持鎖期間只讀欄位、不呼叫拿鎖的函式。"""

    def __init__(self, party) -> None:
        self.p = party
        self._task_names: Optional[Tuple[Dict[int, str], Dict[int, str]]] = None

    # ---- 共用 ----
    def _names(self) -> List[str]:
        return [n for _, n in self.p.party]

    def _item_name(self, item_id) -> str:
        return self.p.item_table().get(item_id, {}).get("nameTn") or str(item_id)

    def _item_row(self, it: dict) -> dict:
        return {"itemId": it.get("itemId"), "name": self._item_name(it.get("itemId")),
                "count": it.get("overlay", 1), "slot": it.get("itemIndex"), "pos": it.get("itemPos"),
                "bind": it.get("bind")}

    def task_names(self) -> Tuple[Dict[int, str], Dict[int, str]]:
        """(任務組 → 組名, 任務 ID → 任務名)；組名取該組第 1 步的 nameTn（Task.json）。"""
        if self._task_names is None:
            import os
            from ..features.gamedb.tables import CONFIGS
            with open(os.path.join(CONFIGS, "Task.json"), encoding="utf-8") as fp:
                rows = json.load(fp)
            groups: Dict[int, str] = {}
            for r in sorted(rows, key=lambda r: (r.get("group", 0), r.get("step", 0))):
                groups.setdefault(r.get("group"), r.get("nameTn") or r.get("nameCn") or "")
            self._task_names = (groups, {r["ID"]: r.get("nameTn") or r.get("nameCn") or "" for r in rows})
        return self._task_names

    # ---- 端點 ----
    def state(self) -> dict:
        snap = self.p.snapshot()                     # snapshot 自己拿鎖：先呼叫完，再拿鎖讀其他欄位
        with self.p.lock:
            p = self.p
            extra = {"profile": p.profile, "party": [{"rid": r, "name": n} for r, n in p.party],
                     "teamMembers": list(p.s.team), "kicked": p.s.kicked, "leaderPos": p.leader_pos,
                     "heals": p.heals, "stay": p.stay, "noRetreat": p.no_retreat,
                     "trainPet": list(p.train_pet) if p.train_pet else None, "signinDay": p.signin_day}
        sc = getattr(self.p, "scenario", None)       # 劇本進度（讀進度檔，不拿 Party 鎖）
        extra["scenario"] = sc.summary() if sc is not None else None
        return {**snap, **extra, "generatedAt": _now_iso()}

    def roles(self) -> dict:
        p = self.p
        out = []
        with p.lock:
            for _, n in p.party:
                a = p.attrs.get(n, {})
                pos = a.get("nowPos") if isinstance(a.get("nowPos"), dict) else None
                if n == p.party[0][1] and p.leader_pos:
                    pos = {"x": p.leader_pos[1], "y": p.leader_pos[2]}
                m = p.money.get(n, {})
                out.append({"name": n, "lv": p.s.levels.get(n), "trans": p.trans.get(n),
                            "hp": p.hp.get(n), "hpMax": p.hp_max.get(n), "map": p.s.member_maps.get(n),
                            "pos": pos, "stone": m.get("stone"), "gold": m.get("gold"),
                            "lockDenar": m.get("lockDenar")})
        return {"roles": out}

    def role(self) -> dict:
        p = self.p
        out = []
        with p.lock:
            for _, n in p.party:
                a = dict(p.attrs.get(n, {}))
                out.append({"name": n, **{k: a.get(k) for k in ROLE_KEYS}, "attr": a})
        return {"labels": ROLE_LABELS, "roles": out}

    def bag(self) -> dict:
        p = self.p
        out = {}
        with p.lock:
            for _, n in p.party:
                its = [dict(it) for it in p.items.get(n, {}).values()
                       if it.get("overlay", 1) > 0 and it.get("itemStorge", 0) == 0 and it.get("itemId")]
                out[n] = sorted((self._item_row(it) for it in its), key=lambda r: (r["slot"] is None, r["slot"] or 0))
        return {"bag": out}

    def equip(self) -> dict:
        p = self.p
        table = p.item_table()                       # 讀檔不拿鎖，先載入
        out = {}
        with p.lock:
            for _, n in p.party:
                rows = []
                for it in p.items.get(n, {}).values():
                    if not it.get("itemId") or it.get("overlay", 1) <= 0 or it.get("itemStorge", 0) == 0:
                        continue
                    t = table.get(it["itemId"], {})
                    rows.append({**self._item_row(it), "storge": it.get("itemStorge"),
                                 "part": EQUIP_PART.get(t.get("type"), f"類型{t.get('type')}"),
                                 "level": t.get("level")})
                out[n] = sorted(rows, key=lambda r: r["storge"] or 0)
        return {"equip": out}

    def pets(self) -> dict:
        p = self.p
        out = {}
        with p.lock:
            for _, n in p.party:
                wg = p.wg.get(n, {})
                fight, ride = wg.get("fightPetIndex"), wg.get("ridePetIndex")
                rows = []
                for idx, x in sorted(p.petinfo.get(n, {}).items()):
                    if (x.get("petId") or 0) <= 0:
                        continue
                    u = x.get("uniqueIndex")
                    rows.append({"index": idx, "petId": x.get("petId"), "name": p.pet_name(x), "lv": x.get("level"),
                                 "hp": x.get("hp"), "hpMax": x.get("hpMax"),
                                 "status": STATUS_NAMES.get(x.get("status"), "休息"), "statusCode": x.get("status"),
                                 "uniqueIndex": u, "lockedFight": u is not None and u == fight,
                                 "lockedRide": u is not None and u == ride})
                out[n] = {"pets": rows, "fightPetIndex": fight, "ridePetIndex": ride}
        return {"pets": out}

    def pet(self) -> dict:
        """寵物完整資料：伺服器 PetInfo 全部欄位（攻防敏、屬性、暴擊等、技能、評分 score）＋原名、狀態。"""
        p = self.p
        out = {}
        with p.lock:
            for _, n in p.party:
                wg = p.wg.get(n, {})
                fight, ride = wg.get("fightPetIndex"), wg.get("ridePetIndex")
                rows = []
                for idx, x in sorted(p.petinfo.get(n, {}).items()):
                    if (x.get("petId") or 0) <= 0:
                        continue
                    u = x.get("uniqueIndex")
                    rows.append({"index": idx, "species": p.pet_name(x), "nickname": x.get("name") or None,
                                 "status": STATUS_NAMES.get(x.get("status"), "休息"),
                                 "lockedFight": u is not None and u == fight, "lockedRide": u is not None and u == ride,
                                 "info": json.loads(json.dumps(x, default=str))})
                out[n] = rows
        return {"pet": out}

    def scenario(self) -> dict:
        sc = getattr(self.p, "scenario", None)
        return {"scenario": sc.summary() if sc is not None else None}

    def jobs(self) -> dict:
        q = getattr(self.p, "jobq", None)
        if q is None:
            return {"pending": [], "running": [], "recent": []}
        return {"pending": q.pending(), "running": q.running(), "recent": q.recent(20)}

    def health(self) -> dict:
        p = self.p
        with p.lock:
            names = [n for _, n in p.party]
            online = [n for n in names if (c := p.clients.get(n)) is not None and not getattr(c, "closed", False)]
            kicked = p.s.kicked
        return {"ok": bool(names) and len(online) == len(names) and not kicked,
                "roles": len(names), "online": len(online), "kicked": kicked, "generatedAt": _now_iso()}

    def tasks(self, groups: Optional[List[int]] = None) -> dict:
        gname, tname = self.task_names()
        p = self.p
        with p.lock:
            finished = {n: list(p.finished.get(n, [])) for _, n in p.party}
            latest: Dict[str, Dict[int, dict]] = {}
            for who, e in p.history:
                if e.get("name") != "Task_TaskInfo":
                    continue
                for t in (e.get("data") or {}).get("taskInfo") or []:
                    if t.get("taskId") is not None:
                        latest.setdefault(who, {})[t["taskId"]] = dict(t)
        want = set(groups) if groups else None
        out = {}
        for _, n in self.p.party:
            done = [{"group": g, "name": gname.get(g, "")} for g in finished[n] if want is None or g in want]
            doing = []
            for tid, t in sorted(latest.get(n, {}).items()):
                g = tid // 100000
                if t.get("taskStatus") in (2, 3) or (want is not None and g not in want):
                    continue                        # 完成／放棄的客戶端會移除，不算進行中
                doing.append({"taskId": tid, "name": tname.get(tid, ""), "group": g, "groupName": gname.get(g, ""),
                              "status": TASK_STATUS.get(t.get("taskStatus"), str(t.get("taskStatus"))),
                              "statusCode": t.get("taskStatus"), "schedule": t.get("taskSchedule")})
            out[n] = {"finishedGroups": done, "active": doing}
        return {"groups": sorted(want) if want else None, "tasks": out}

    def buffs(self, now: Optional[float] = None) -> dict:
        p = self.p
        out = {}
        with p.lock:
            for _, n in p.party:
                out[n] = {"exp": p.exp_buffs(n, now=now),          # exp_buffs 不拿鎖
                          "all": {str(b): t for b, t in sorted(p.buffs.get(n, {}).items())}}
        return {"buffs": out}

    def waigua(self) -> dict:
        """外掛設定全部：遇敵、快戰、隱藏、自動加點（方式＋自訂清單）、一般設定（鎖定戰寵騎寵、吃經驗果…）、
        戰鬥設定（首次動作、技能補血…）、抓寵設定，再加練功程式自己的開關（bot）。
        一般／戰鬥設定用合併過的 p.wg／p.battle（伺服器推的是部分欄位，attrs 那份會被整個蓋掉）。"""
        p = self.p
        out = {}
        with p.lock:
            for _, n in p.party:
                a = p.attrs.get(n, {})
                pets = {x.get("uniqueIndex"): p.pet_name(x) for x in p.petinfo.get(n, {}).values()
                        if (x.get("petId") or 0) > 0 and x.get("uniqueIndex") is not None}
                wg = dict(p.wg.get(n, {}))
                diy = sorted(_values(a.get("autoPointDiyList")), key=lambda d: d.get("sort") or 0)
                out[n] = {
                    "autoEncount": a.get("autoEncountFlag"), "fastBattle": p.fast.get(n), "hideMode": p.hide.get(n),
                    "autoEatOffLineItem": a.get("autoEatOffLineItem"), "allRoleSetFlg": a.get("allRoleSetFlg"),
                    "point": {"type": p.point_type.get(n, a.get("autoSetPointType")),
                              "typeLabel": POINT_TYPES.get(p.point_type.get(n, a.get("autoSetPointType")), "沒設過"),
                              "diy": [{**d, "label": POINT_ATTRS.get(d.get("type"), str(d.get("type")))} for d in diy]},
                    "normal": {**wg, "fightPetName": pets.get(wg.get("fightPetIndex")),
                               "ridePetName": pets.get(wg.get("ridePetIndex"))},
                    "battle": dict(p.battle.get(n, {})),
                    "catchPet": a.get("catchPetSetting"),
                }
            bot = {"pointPlan": p.point_plan, "battleHeal": p.battle_heal, "autoSpirit": p.auto_spirit,
                   "noRetreat": p.no_retreat, "stay": p.stay,
                   "infoLogUntil": int(p.info_until) if p.info_until else None}
        return {"waigua": out, "bot": bot}

    def battle(self, now: Optional[float] = None) -> dict:
        p = self.p
        now = time.time() if now is None else now
        with p.lock:
            recent = [w for t, w in p.battles if now - t <= 300]
            last = None if p.last_battle is None else {"t": int(p.last_battle[0]), "win": p.last_battle[1]}
            fast = {n: p.fast.get(n) for _, n in p.party}
            return {"wins5m": recent.count(1), "losses5m": recent.count(2), "battles5m": len(recent),
                    "lastBattle": last, "loop": bool(p.s.loop_on), "fast": fast,
                    "fastAll": all(fast.values()) if fast else False}


class DepotQuery:
    """倉庫查詢（會送請求）：上限、快取、同時一個、失敗不重試。"""

    def __init__(self, party, clock: Callable[[], float] = time.monotonic, max_requests: int = MAX_REQUESTS,
                 cache_seconds: float = CACHE_SECONDS) -> None:
        self.p, self.clock, self.max_requests, self.cache_seconds = party, clock, max_requests, cache_seconds
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[float, int, dict]] = {}

    def query(self, kind: str) -> Tuple[int, dict]:
        spec = DEPOTS[kind]
        with self._lock:
            hit = self._cache.get(kind)
            if hit and self.clock() - hit[0] < self.cache_seconds:
                return hit[1], dict(hit[2], cached=True)
            code, body = self._run(kind, spec)
            self._cache[kind] = (self.clock(), code, body)
            return code, dict(body, cached=False)

    def _run(self, kind: str, spec: dict) -> Tuple[int, dict]:
        p = self.p
        with p.lock:                                 # 只在這裡短暫讀頁數，送請求時不持鎖
            names = [n for _, n in p.party]
            pages = {n: dict(p.attrs.get(n, {})) for n in names}
            clients = {n: p.clients.get(n) for n in names}
        body = {"kind": kind, "personal": {}, "public": None, "requests": 0, "queriedAt": _now_iso()}
        used = [0]

        def fetch(name: str, public: bool, max_pages: int) -> List[dict]:
            c = clients.get(name)
            if c is None or getattr(c, "closed", False):
                raise _Stop(503, f"{name} 目前沒有連線")
            rows: List[dict] = []
            for page in range(1, max_pages + 1):
                if used[0] >= self.max_requests:
                    raise _Stop(502, f"超過單次請求上限 {self.max_requests}，停止")
                used[0] += 1
                body["requests"] = used[0]
                try:
                    r = c.request(spec["proto"], {"page": page, "isPublic": public, **spec["extra"]},
                                  timeout=DEPOT_TIMEOUT) or {}
                except Exception as e:  # noqa: BLE001  出錯就停，不重試
                    raise _Stop(502, f"{name} 查{'公共' if public else '個人'}第 {page} 頁失敗：{type(e).__name__}")
                if r.get("isOK") is False:
                    raise _Stop(502, f"{name} 查{'公共' if public else '個人'}第 {page} 頁被拒")
                got = _values(r.get(spec["list"]))
                if not got:
                    break                            # 空頁就停（後面的頁通常沒開，要鑽石）
                rows.extend(self._row(kind, x, page) for x in got)
            return rows

        def pages_of(name: str, key: str, cap: int) -> int:
            v = pages[name].get(key)
            return max(1, min(v if isinstance(v, int) and v > 0 else 1, cap))

        try:
            for n in names:
                body["personal"][n] = fetch(n, False, pages_of(n, spec["pages"][0], MAX_PERSONAL_PAGES))
            if names:                                # 公共倉庫帳號共用：用隊長查一次
                body["public"] = fetch(names[0], True, pages_of(names[0], spec["pages"][1], MAX_PUBLIC_PAGES))
        except _Stop as s:
            return s.code, dict(body, error=s.msg)
        return 200, body

    def _row(self, kind: str, x: dict, page: int) -> dict:
        if kind == "depot":
            return {"itemId": x.get("itemId"), "name": self.p.item_table().get(x.get("itemId"), {}).get("nameTn")
                    or str(x.get("itemId")), "count": x.get("overlay", 1), "page": page,
                    "uniqueIndex": x.get("uniqueIndex"), "bind": x.get("bind")}
        return {"petId": x.get("petId"), "name": self.p.pet_name(x), "lv": x.get("level"), "page": page,
                "hp": x.get("hp"), "hpMax": x.get("hpMax"), "uniqueIndex": x.get("uniqueIndex")}


class _Stop(Exception):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code, self.msg = code, msg


ENDPOINTS = ("state", "roles", "role", "bag", "equip", "pets", "pet", "tasks", "buffs", "battle", "waigua", "depot", "petdepot",
             "scenario", "jobs", "health", "openapi.json")
DOCS = {   # 端點 → 說明（/openapi.json 用）
    "state": "總覽：地圖、隊伍、每隻人物與戰寵騎寵血量、經驗加成、金錢、劇本進度",
    "roles": "人物一覽：等級、轉數、血量、地圖、座標、石幣",
    "role": "人物完整屬性（經驗、四圍、剩餘點數、屬性、倉庫頁數…；attr 是伺服器全部欄位）",
    "bag": "道具欄（不含身上裝備）",
    "equip": "身上裝備與部位",
    "pets": "寵物一覽：等級、血量、出戰／騎乘／休息",
    "pet": "寵物完整資料：攻防敏、屬性、暴擊等戰鬥數值、技能、伺服器評分 score",
    "tasks": "任務：已完成的任務組、進行中的步驟（?groups=2000,2031 只看這些組）",
    "buffs": "經驗加成與到期時間",
    "battle": "近 5 分鐘勝敗、遇敵、快速戰鬥",
    "waigua": "外掛設定全部：遇敵、快戰、隱藏、自動加點、鎖定戰寵騎寵、首次動作、技能補血、抓寵；bot＝練功程式的開關",
    "depot": "道具倉庫（個人每隻＋公共；會送查詢請求，結果快取 60 秒）",
    "petdepot": "寵物倉庫（同上）",
    "scenario": "劇本進度：第幾步、狀態、最近結果",
    "jobs": "工作佇列：排隊中、執行中、最近完成",
    "health": "連線狀態（不需要 token；容器健康檢查用）",
}
OPEN = ("/health",)          # 不用 token 的端點（不含遊戲資料）


def openapi() -> dict:
    paths = {}
    for e, desc in DOCS.items():
        params = [{"name": "role", "in": "query", "required": False, "schema": {"type": "string"},
                   "description": "只看這隻人物（名字）"}]
        if e == "tasks":
            params.append({"name": "groups", "in": "query", "required": False, "schema": {"type": "string"}})
        paths[f"/{e}"] = {"get": {"summary": desc, "parameters": params,
                                  "responses": {"200": {"description": "JSON"}}}}
    paths["/infolog"] = {"post": {"summary": "人物寵物（等級、血量）每分鐘印一次日誌，5 分鐘後自動關；再打一次從現在重算",
                                  "responses": {"200": {"description": "JSON"}}}}
    return {"openapi": "3.0.3", "info": {"title": "唯讀查詢 API", "version": "1"},
            "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
            "security": [{"bearer": []}], "paths": paths}


def only_role(obj, role: str):
    """?role= 篩選：以人物名字為鍵的 dict、或帶 name 欄位的人物清單，只留這隻；其他欄位照舊。"""
    if not role or not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        if isinstance(v, dict) and role in v:
            out[k] = {role: v[role]}
        elif isinstance(v, list) and v and all(isinstance(r, dict) and "name" in r for r in v) and k in ("roles",):
            out[k] = [r for r in v if r.get("name") == role]
        else:
            out[k] = v
    return out


def make_handler(views: Views, depot: DepotQuery, token: Optional[str] = None):
    import hmac

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 不印每個請求
            pass

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, path: str) -> bool:
            if token is None or path in OPEN:
                return True
            got = self.headers.get("Authorization", "")
            got = got[7:] if got.startswith("Bearer ") else self.headers.get("X-Api-Token", "")
            return hmac.compare_digest(got.encode("utf-8"), token.encode("utf-8"))

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            path = u.path.rstrip("/") or "/"
            if not self._authorized(path):
                return self._send(401, {"error": "要帶 token（Authorization: Bearer <token>）"})
            role = q.get("role", "")
            try:
                if path == "/":
                    return self._send(200, {"endpoints": [f"/{e}" for e in ENDPOINTS]})
                if path == "/openapi.json":
                    return self._send(200, openapi())
                if path == "/tasks":
                    raw = q.get("groups", "")
                    try:
                        groups = [int(x) for x in raw.split(",") if x.strip()] if raw else None
                    except ValueError:
                        return self._send(400, {"error": "groups 要是逗號分隔的數字，例如 2000,2031"})
                    return self._send(200, only_role(views.tasks(groups), role))
                if path in ("/depot", "/petdepot"):
                    code, body = depot.query(path[1:])
                    return self._send(code, only_role(body, role))
                fn = {"/state": views.state, "/roles": views.roles, "/role": views.role, "/bag": views.bag,
                      "/equip": views.equip, "/pets": views.pets, "/pet": views.pet, "/buffs": views.buffs,
                      "/battle": views.battle, "/waigua": views.waigua, "/scenario": views.scenario, "/jobs": views.jobs,
                      "/health": views.health}.get(path)
                if fn is None:
                    return self._send(404, {"error": "找不到", "endpoints": [f"/{e}" for e in ENDPOINTS]})
                return self._send(200, only_role(fn(), role))
            except Exception as e:  # noqa: BLE001  讀取出錯只回錯誤，不影響練功
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def _deny(self):
            # 先把請求內容讀掉再回：沒讀完就回應，Windows 會把連線直接切斷，客戶端收到 10053 而不是 405（測試偶發失敗）
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 65536)
                if n:
                    self.rfile.read(n)
            except (ValueError, OSError):
                pass
            self._send(405, {"error": "只支援 GET（唯讀）"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            if path != "/infolog":
                return self._deny()
            try:                                 # 同 _deny：先把請求內容讀掉再回
                n = min(int(self.headers.get("Content-Length") or 0), 65536)
                if n:
                    self.rfile.read(n)
            except (ValueError, OSError):
                pass
            if not self._authorized(path):
                return self._send(401, {"error": "要帶 token（Authorization: Bearer <token>）"})
            until = views.p.start_info_log()
            return self._send(200, {"ok": True, "every": views.p.INFO_EVERY, "until": int(until),
                                    "untilIso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(until))})

        do_PUT = do_DELETE = do_PATCH = _deny

    return Handler


LOOPBACK = ("127.0.0.1", "localhost", "::1")
TOKEN_ENV = "BOT_API_TOKEN"
MIN_TOKEN = 16


def serve(party, port: int, depot: Optional[DepotQuery] = None, host: str = "127.0.0.1",
          token: Optional[str] = None) -> ThreadingHTTPServer:
    """在背景執行緒啟動唯讀 API，回傳 server（shutdown() 停止）。port 0＝系統挑。
    預設只聽 127.0.0.1；聽其他位址（容器對外）**一定要 token**（至少 16 字元），沒有就拒絕啟動。"""
    if host not in LOOPBACK and (not token or len(token) < MIN_TOKEN):
        raise ValueError(f"API 要對外（{host}）一定要設 token（至少 {MIN_TOKEN} 字元）")
    srv = ThreadingHTTPServer((host, port), make_handler(Views(party), depot or DepotQuery(party), token or None))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="x7k2qm-party-api", daemon=True).start()
    return srv
