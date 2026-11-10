"""練功狀態上傳：向各組練功程式的查詢 API 要 `/state`，整理成網頁要的文件，只把有變的 POST 到指定的上傳網址。

    python -X utf8 -m x7k2qm.autoplay.status_upload             # 常駐：每 5 分鐘一輪
    python -X utf8 -m x7k2qm.autoplay.status_upload --once      # 跑一次
    python -X utf8 -m x7k2qm.autoplay.status_upload --once --dry-run   # 只整理、列出路徑和大小，不上傳

使用者 2026-11-11「要給K8s集群用的」「你一樣可以build docker 公開」「我部署專案是分開的」：
跟練功程式**同一個映像**，換一個指令跑；設定全部走環境變數，網址、金鑰、路徑前綴都不寫在程式裡。

| 環境變數 | 用途 |
| :-- | :-- |
| `BOT_UPLOAD_TARGETS` | 要問的練功程式：`組=網址`，逗號分隔；可加標籤 `組:標籤=網址`（例 `2:靈石=http://bot-2:8790,3=http://bot-3:8790`） |
| `BOT_UPLOAD_API_TOKEN` | 查詢 API 的 token（練功容器的 API_TOKEN；送 `Authorization: Bearer`） |
| `BOT_UPLOAD_URL` | 上傳網址（POST JSON） |
| `BOT_UPLOAD_KEY` | 上傳金鑰（放 Secret） |
| `BOT_UPLOAD_HEADER` | 金鑰標頭名稱（預設 `X-Ingest-Key`） |
| `BOT_UPLOAD_PREFIX` | 每份文件路徑的前綴（上傳端規定的，例 `/b/<後端>`） |
| `BOT_UPLOAD_STATE` | 雜湊狀態檔（放 PVC；沒設就放 /tmp，重啟後第一輪全送） |
| `BOT_UPLOAD_EVERY` | 幾秒一輪（預設 300） |

上傳格式：`{"docs": [{"path", "body": "<JSON 字串>", "hash"}], "delete": [...], "synced": <秒>}`，回 `{"ok": true}` 才算成功；
一次 POST 0.6 MB 以內。只讀查詢 API（GET），**不發任何遊戲封包、不排工作**。
上傳或查詢失敗只記日誌，同一輪不重送，下一輪照常（CLAUDE.md 規範 2）；被拒的那批不記雜湊，下一輪自然會再送。
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.request
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple

EVERY = 300.0                      # 5 分鐘一輪
BATCH_BYTES = 600_000              # 一次 POST 最多約 0.6 MB
TIMEOUT = 60.0
FETCH_TIMEOUT = 15.0
STALE = 900                        # 資料超過 15 分鐘沒更新＝不是在練
VOLATILE = ("age", "age_seconds", "last_seen_age", "last_seen_seconds", "uptime", "uptime_seconds")
ENV = "BOT_UPLOAD_"


def read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fp:
            d = json.load(fp)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


# ---------------- 整理成網頁要的文件 ----------------

def age_text(sec: Optional[float]) -> Optional[str]:
    if sec is None:
        return None
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} 秒前"
    if sec < 3600:
        return f"{sec // 60} 分鐘前"
    if sec < 86400:
        return f"{sec // 3600} 小時 {sec % 3600 // 60} 分前"
    return f"{sec // 86400} 天前"


def fmt_time(t: Optional[float]) -> Optional[str]:
    return None if t is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _pet(x: Optional[dict], status: str) -> Optional[dict]:
    if not x:
        return None
    return {"name": x.get("name"), "level": x.get("lv"), "status": status, "hp": x.get("hp"), "hpMax": x.get("hpMax")}


def _buffs(r: dict, now: float) -> List[dict]:
    out = []
    for b in r.get("exp") or []:
        u = b.get("until")
        out.append({"name": b.get("name"), "until": fmt_time(u) if isinstance(u, (int, float)) else None,
                    "left_minutes": int((u - now) // 60) if isinstance(u, (int, float)) else None})
    return out


_SKILLS: Optional[Dict[int, str]] = None
# 寵物面板要的戰鬥數值：網頁欄位 → PetInfo 欄位（同參考專案的面板格式）
PET_FIELDS = (("hp", "hp"), ("hp_max", "hpMax"), ("mp", "mp"), ("mp_max", "mpMax"), ("exp", "exp"),
              ("attack", "physicalAttack"), ("defense", "physicalDefense"), ("agile", "agile"),
              ("crit", "physicalCrit"), ("hit", "physicalHit"), ("dodge", "physicalDodge"),
              ("crit_damage", "physicalCritDamage"), ("penetrate", "physicalPenetrate"),
              ("resist_crit", "physicalResistCrit"), ("rank", "rankNum"), ("star", "petStar"))
GROWTH = ("體", "腕", "耐", "速")


def skill_name(sid, lv) -> str:
    """技能名稱（設定表 SkillBattle：ID＝技能×1000＋等級）；讀不到回空字串。"""
    global _SKILLS
    if _SKILLS is None:
        _SKILLS = {}
        try:
            from ..features.gamedb.tables import CONFIGS
            with open(os.path.join(CONFIGS, "SkillBattle.json"), encoding="utf-8") as fp:
                _SKILLS = {r["ID"]: r.get("nameTn") or "" for r in json.load(fp) if r.get("ID")}
        except Exception:  # noqa: BLE001  名稱只是加註
            pass
    try:
        sid, lv = int(sid), int(lv or 1)
    except (TypeError, ValueError):
        return ""
    return _SKILLS.get(sid * 1000 + lv) or _SKILLS.get(sid * 1000 + 1, "")


def pet_panel(row: dict) -> dict:
    """查詢 API /pet 的一列 → 寵物面板（欄位名照參考專案：species、status_name、hp_max…）。不帶 rid。"""
    x = row.get("info") or {}
    growth = row.get("growth") or []
    skills = []
    for s in sorted([s for s in (x.get("skills") or []) if isinstance(s, dict) and s.get("skillId")],
                    key=lambda s: s.get("skillPos") or 0):
        skills.append({"pos": s.get("skillPos"), "skillId": s.get("skillId"), "level": s.get("skillLevel"),
                       "name": skill_name(s.get("skillId"), s.get("skillLevel"))})
    out = {"slot": row.get("index"), "uniqueIndex": x.get("uniqueIndex"), "petId": x.get("petId"),
           "species": row.get("species"), "nickname": row.get("nickname") or "", "level": x.get("level"),
           "trans": x.get("trans") or 0, "status": x.get("status"), "status_name": row.get("status"),
           "locked_fight": bool(row.get("lockedFight")), "locked_ride": bool(row.get("lockedRide")),
           "score": row.get("score"), "loyalty": row.get("loyalty"),
           "grade": {"標籤": row.get("grade"), "檔差合計": row.get("gradeDiff"),
                     "個體檔": {k: (growth[i] if i < len(growth) else None) for i, k in enumerate(GROWTH)}},
           "skills": skills}
    out.update({k: x.get(src) for k, src in PET_FIELDS})
    return out


def pet_docs(group: str, meta: dict, pet_rows: Dict[str, List[dict]]) -> Dict[str, object]:
    """身上全部寵物 → /groups/<組>/pets（每隻角色一個清單）、/pet-index（uniqueIndex → 面板）、/search-index/<組>。"""
    by_role, index, search = OrderedDict(), {}, []
    for role, rows in pet_rows.items():
        panels = [pet_panel(r) for r in rows]
        by_role[role] = panels
        for p in panels:
            where = f"{role} 身上第{p['slot']}格"
            if p["uniqueIndex"] is not None:
                index[str(p["uniqueIndex"])] = {"where": where, "pet": p}
            search.append({"group": group, "kind": "pet", "role": role, "where": "身上", "where_detail": where,
                           "name": p["species"], "nickname": p["nickname"], "itemId": None, "petId": p["petId"],
                           "uniqueIndex": p["uniqueIndex"], "count": 1, "bind": None})
    base = f"/groups/{group}"
    return {f"{base}/pets": dict(meta, section="pets", roles=by_role, note="身上全部寵物（含休息中）；倉庫的沒有上傳"),
            f"{base}/pet-index": {"meta": meta, "pets": index},
            f"/search-index/{group}": search}


def group_docs(group: str, label: str, snap: Optional[dict], snap_t: Optional[float], source: str,
               tail: List[str], program: Optional[str], pid, started: Optional[float], running: bool, now: float,
               place: Callable[[object], str], reason: str = "",
               pet_rows: Optional[Dict[str, List[dict]]] = None) -> Dict[str, object]:
    """一組的 /groups/<組>、/pets、/money、/live。snap＝快照格式（Party.snapshot／查詢 API /state）；沒有就只有 live。
    pet_rows（查詢 API /pet 的 {角色: [列]}）有給 → 寵物分頁換成身上全部寵物的面板，另外加 pet-index、search-index。"""
    base = f"/groups/{group}"
    age = None if snap_t is None else now - snap_t
    fresh = running and age is not None and age <= STALE
    # 資料是新的但最後一場戰鬥超過 15 分鐘＝程式活著但沒在打（卡住、遇敵關著）；查詢 API 永遠是「現在」，只看資料時間抓不到
    lb = ((snap or {}).get("lastBattle") or {}).get("t")
    idle = (snap_t - lb) if fresh and isinstance(lb, (int, float)) and snap_t is not None else None
    stuck = idle is not None and idle > STALE
    state = "running" if fresh and not stuck else ("stale" if running else "stopped")
    stale_text = f"程式在跑但 {age_text(idle)}最後一場戰鬥" if stuck else f"程式在跑但資料 {age_text(age) or '沒有'}"
    state_text = {"running": "練功中", "stale": stale_text, "stopped": "沒在跑"}[state]
    meta = {"group": group, "label": label, "data_time": fmt_time(snap_t), "age_seconds": None if age is None else int(age),
            "age": age_text(age), "source": source, "state": state, "state_text": state_text}
    docs: Dict[str, object] = {}
    roles = (snap or {}).get("roles") or []
    map_name = place(snap.get("map")) if snap and snap.get("map") is not None else None
    live_roles = OrderedDict()
    for r in roles:
        live_roles[r.get("name")] = {
            "map": (snap or {}).get("map"), "map_name": map_name, "loop": (snap or {}).get("loop"),
            "level": r.get("lv"), "hp": r.get("hp"), "hpMax": r.get("hpMax"), "stone": r.get("stone"),
            "pets": [p for p in (_pet(r.get("fight"), "出戰"), _pet(r.get("ride"), "騎乘")) if p]}
    li = {"state": state, "state_text": state_text, "program": program if running else None,
          "pid": pid if running else None, "started": fmt_time(started) if running else None,
          "last_seen": fmt_time(snap_t), "last_seen_age": age_text(age),
          "last_seen_seconds": None if age is None else int(age), "data_time": fmt_time(snap_t),
          "reason": "" if state == "running" else (reason or (stale_text if stuck else "")
                                                     or (tail[-1][:200] if tail else ""))}
    docs[f"{base}/live"] = dict(group=group, **li, roles=live_roles,
                                team=(snap or {}).get("team"), fast=(snap or {}).get("fast"),
                                last_battle=(snap or {}).get("lastBattle"),
                                wins=(snap or {}).get("wins"), losses=(snap or {}).get("losses"),
                                judge=(snap or {}).get("judge"), kicked=(snap or {}).get("kicked"))
    if not roles:
        return docs
    money = (snap or {}).get("money") or {}
    summary, pets, money_by = OrderedDict(), OrderedDict(), OrderedDict()
    for r in roles:
        n = r.get("name")
        ps = [p for p in (_pet(r.get("fight"), "出戰"), _pet(r.get("ride"), "騎乘")) if p]
        summary[n] = {"name": n, "level": r.get("lv"), "trans": r.get("trans"), "hp": r.get("hp"), "hp_max": r.get("hpMax"),
                      "map": map_name, "money": {"石幣": r.get("stone")},
                      "pets": [f"{p['name']} Lv{p['level']} {p['status']}" for p in ps],
                      "exp_buffs": _buffs(r, now)}
        pets[n] = ps
        money_by[n] = {"石幣": r.get("stone")}
    # 石幣帳號共用（11-11 實測五隻同值）：取隊長那隻的，不用 money.stone——11-11 以前的練功程式那欄是五隻相加（LESSONS #195）
    stone = next((r.get("stone") for r in roles if r.get("stone") is not None), None)
    totals = {"money": {"石幣": stone, "活力": money.get("gold"), "聲望": money.get("lockDenar")}}
    docs[base] = dict(meta, roles=summary, totals=totals, sections=["pets", "money", "live"],
                      note="石幣、活力、聲望都是帳號共用（取隊長），不是五隻相加")
    docs[f"{base}/pets"] = dict(meta, section="pets", roles=pets,
                                note="只有出戰、騎乘中的寵物；休息中與倉庫的寵物沒有上傳")
    docs[f"{base}/money"] = dict(meta, section="money", roles=money_by, totals=totals)
    if pet_rows:
        docs.update(pet_docs(group, meta, pet_rows))
    return docs


def summarize(per_group: List[Tuple[str, str, Dict[str, object], dict]], now: float) -> Dict[str, object]:
    """各組 group_docs 的結果 → 全部文件＋/groups、/live、/procs。per_group：(組, 標籤, 文件, 程式資訊 or None)。"""
    docs: Dict[str, object] = {}
    glist, live_list, procs = [], [], []
    for g, label, gd, proc in per_group:
        docs.update(gd)
        live = gd[f"/groups/{g}/live"]
        live_list.append({k: live[k] for k in ("group", "state", "state_text", "program", "pid", "started",
                                               "last_seen", "last_seen_age", "last_seen_seconds", "data_time", "reason")})
        roles = (gd.get(f"/groups/{g}") or {}).get("roles") or {}
        glist.append({"group": g, "label": label, "purpose": label, "data_time": live["data_time"],
                      "age_seconds": live["last_seen_seconds"], "age": live["last_seen_age"],
                      "state": live["state"], "state_text": live["state_text"],
                      "roles": [{"index": i + 1, "name": n, "level": r.get("level"), "trans": r.get("trans")}
                                for i, (n, r) in enumerate(roles.items())]})
        if proc is not None:
            procs.append(dict(proc, group=g, label=label, state=live["state"]))
    docs["/groups"] = {"groups": glist, "count": len(glist), "updated": fmt_time(now)}
    docs["/live"] = {"live": live_list, "count": len(live_list),
                     "running": sum(1 for x in live_list if x["state"] == "running")}
    docs["/procs"] = {"procs": procs, "count": len(procs), "problems": [p for p in procs if p["state"] != "running"]}
    return docs


# ---------------- 向查詢 API 要資料 ----------------

def parse_targets(text: str) -> List[Tuple[str, str, str]]:
    """`2:靈石=http://a:8790,3=http://b:8790` → [(組, 標籤, 網址)]；格式錯丟 ValueError。"""
    out = []
    for part in [x.strip() for x in (text or "").split(",") if x.strip()]:
        if "=" not in part:
            raise ValueError(f"目標格式錯（要 組=網址）：{part}")
        head, url = part.split("=", 1)
        g, _, label = head.partition(":")
        g, url = g.strip(), url.strip().rstrip("/")
        if not g or not url.startswith(("http://", "https://")):
            raise ValueError(f"目標格式錯（要 組=http://…）：{part}")
        out.append((g, label.strip() or f"第 {g} 組", url))
    return out


def fetch_json(url: str, token: Optional[str], timeout: float = FETCH_TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "status-upload/1",
                                               **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8") or "{}")
    if not isinstance(d, dict):
        raise ValueError("回應不是 JSON 物件")
    return d


def build_docs_from_api(targets: List[Tuple[str, str, str]], token: Optional[str], now: float,
                        fetch: Callable[[str, Optional[str]], dict], place: Callable[[object], str]) -> Dict[str, object]:
    """每組 GET <網址>/state、/pet；連不上就那組標 stopped、附原因，其他組照常。"""
    per = []
    for g, label, url in targets:
        gd, proc = api_group(g, label, url, token, now, fetch, place)
        per.append((g, label, gd, proc))
    return summarize(per, now)


def api_group(g: str, label: str, url: str, token: Optional[str], now: float,
              fetch: Callable[[str, Optional[str]], dict], place: Callable[[object], str],
              program: str = "練功", pid=None, started: Optional[float] = None, tail: Optional[List[str]] = None,
              log_name: Optional[str] = None) -> Tuple[Dict[str, object], dict]:
    """一組：GET /state（失敗＝stopped）＋GET /pet（失敗只是沒有寵物面板）。回 (文件, 程式資訊)。"""
    try:
        st = fetch(url + "/state", token)
        err = ""
    except Exception as e:  # noqa: BLE001  一組連不上不影響其他組
        st, err = None, f"查詢 API 連不上：{type(e).__name__} {str(e)[:150]}"
    pets = None
    if st is not None:
        try:
            pets = (fetch(url + "/pet", token) or {}).get("pet") or None
        except Exception:  # noqa: BLE001  寵物面板拿不到：總覽照送
            pets = None
    ok = st is not None and not st.get("kicked")
    reason = err or ("被擠下線" if st and st.get("kicked") else "")
    tail = list(tail or [])
    gd = group_docs(g, label, st, now if st is not None else None, "練功程式查詢 API /state、/pet", tail,
                    program if ok else None, pid, started, ok, now, place, reason=reason, pet_rows=pets)
    proc = {"pid": pid, "kind": program, "started": fmt_time(started), "log": log_name,
            "log_tail": tail or ([reason] if reason else [])}
    return gd, proc


def place_default(mapid) -> str:
    try:
        from .places import place_name           # 不從 tg_watch 拿：會把 Telegram 監看帶進公開映像
        return place_name(mapid)
    except Exception:  # noqa: BLE001
        return f"地圖 {mapid}"


# ---------------- 上傳 ----------------

def _hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def strip_volatile(obj):
    """比較有沒有變時不看「多久前」這類每分鐘都會變的字。"""
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items() if k not in VOLATILE}
    if isinstance(obj, list):
        return [strip_volatile(x) for x in obj]
    return obj


def post_json(url: str, body: dict, key: str, header: str = "X-Ingest-Key") -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8", header: key, "User-Agent": "status-upload/1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


class Syncer:
    """有變的才送；送成功才記雜湊。state_path、prefix 一定要給（測試不碰真實路徑；前綴由上傳端規定）。"""

    def __init__(self, url: str, key: str, state_path: str, post: Callable[[str, dict, str], dict],
                 prefix: str, clock: Callable[[], float] = time.time) -> None:
        self.url, self.key, self.state_path, self.post, self.clock = url, key, state_path, post, clock
        self.prefix = prefix.rstrip("/")
        self.state: Dict[str, str] = read_json(state_path) or {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self.state, fp)
        os.replace(tmp, self.state_path)

    def plan(self, docs: Dict[str, object], force: bool = False) -> Tuple[List[dict], List[str], Dict[str, str]]:
        changed, hashes = [], {}
        for path, body in docs.items():
            full = self.prefix + path
            h = _hash(strip_volatile(body))
            hashes[full] = h
            if force or self.state.get(full) != h:
                changed.append({"path": full, "body": json.dumps(body, ensure_ascii=False, default=str), "hash": h})
        gone = [p for p in self.state if p.startswith(self.prefix + "/") and p not in hashes]
        return changed, gone, hashes

    def sync(self, docs: Dict[str, object], force: bool = False) -> Tuple[int, int]:
        """回 (送了幾份, 沒變幾份)。任何一批被拒就丟例外（已送成功的批次照記）。"""
        now = self.clock()
        changed, gone, hashes = self.plan(docs, force)
        batches, batch, size = [], [], 0
        for d in changed:
            n = len(d["body"].encode("utf-8"))
            if batch and size + n > BATCH_BYTES:
                batches.append(batch)
                batch, size = [], 0
            batch.append(d)
            size += n
        batch.append({"path": self.prefix + "/_heartbeat",
                      "body": json.dumps({"at": now, "changed": len(changed)}), "hash": ""})
        batches.append(batch)
        sent = 0
        for i, b in enumerate(batches):
            last = i == len(batches) - 1
            r = self.post(self.url, {"docs": b, "delete": gone if last else [], "synced": now}, self.key)
            if not (r or {}).get("ok"):
                raise RuntimeError(f"上傳被拒：{str(r)[:200]}")
            for d in b:
                if d["path"] in hashes:
                    self.state[d["path"]] = hashes[d["path"]]
            sent += sum(1 for d in b if d["path"] in hashes)
            if last:
                for p in gone:
                    self.state.pop(p, None)
            self._save()
        return sent, len(docs) - sent


# ---------------- 主程式 ----------------

LOG_PATH: Optional[str] = None


def log(msg: str) -> None:
    line = f"{time.strftime('%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        except OSError:
            pass


def run_loop(build: Callable[[float], Dict[str, object]], sy: Syncer, every: float, once: bool, dry_run: bool,
             force: bool, stop_file: Optional[str] = None) -> int:
    first = True
    while True:
        t0 = time.time()
        try:
            docs = build(t0)
            if dry_run:
                changed, _, _ = sy.plan(docs, force=True)
                for d in changed:
                    log(f"  {d['path']}  {len(d['body'].encode('utf-8'))} bytes")
                log(f"dry-run：{len(changed)} 份，沒上傳")
                return 0
            sent, same = sy.sync(docs, force=force and first)
            log(f"同步：送 {sent} 份、沒變 {same} 份（{time.time() - t0:.1f} 秒）")
        except Exception as e:  # noqa: BLE001  網路斷、對方忙：只記日誌，下一輪照常（不在同一輪重送）
            log(f"同步失敗（下一輪再送）：{type(e).__name__} {str(e)[:200]}")
        first = False
        if once:
            return 0
        while time.time() - t0 < every:
            if stop_file and os.path.exists(stop_file):
                log("收到停止檔，結束")
                return 0
            time.sleep(5.0)


def main(argv=None, env=None) -> int:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(prog="status_upload", description="向練功程式查詢 API 要狀態，整理後上傳")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只整理、印出要送的路徑與大小，不上傳")
    ap.add_argument("--force", action="store_true", help="第一輪全部重送")
    ap.add_argument("--every", type=float, default=float(env.get(ENV + "EVERY") or EVERY))
    ap.add_argument("--targets", default=env.get(ENV + "TARGETS") or "")
    ap.add_argument("--state", default=env.get(ENV + "STATE") or os.path.join(tempfile.gettempdir(), "status_upload_state.json"))
    ap.add_argument("--stop-file", default=None)
    ap.add_argument("--log", default=None)
    a = ap.parse_args(argv)
    global LOG_PATH
    LOG_PATH = a.log
    try:
        targets = parse_targets(a.targets)
    except ValueError as e:
        log(f"停止：{e}")
        return 1
    url, key, prefix = env.get(ENV + "URL") or "", env.get(ENV + "KEY") or "", env.get(ENV + "PREFIX") or ""
    if not targets:
        log(f"停止：沒有要問的練功程式（設 {ENV}TARGETS，例 2=http://bot-2:8790）")
        return 1
    if not a.dry_run and not (url and key and prefix.startswith("/")):
        log(f"停止：上傳設定不完整（要 {ENV}URL、{ENV}KEY、{ENV}PREFIX，前綴以 / 開頭）")
        return 1
    header = env.get(ENV + "HEADER") or "X-Ingest-Key"
    token = env.get(ENV + "API_TOKEN") or None
    sy = Syncer(url, key, a.state, lambda u, b, k: post_json(u, b, k, header), prefix or "/dry-run")
    log(f"上傳開始：{len(targets)} 組（{'、'.join(g for g, _, _ in targets)}），每 {a.every:g} 秒，只送有變的")
    return run_loop(lambda now: build_docs_from_api(targets, token, now, fetch_json, place_default),
                    sy, a.every, a.once, a.dry_run, a.force, a.stop_file)


if __name__ == "__main__":
    sys.exit(main())
