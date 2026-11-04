"""打無畫面五開的本機唯讀查詢 API（party_api），印成好讀的中文表格。不連遊戲伺服器、不登入。

    python -X utf8 -m x7k2qm.autoplay.status_api                 # 總覽（/state）
    python -X utf8 -m x7k2qm.autoplay.status_api bag             # 背包
    python -X utf8 -m x7k2qm.autoplay.status_api tasks --groups 2000,2031
    python -X utf8 -m x7k2qm.autoplay.status_api depot --profile 3
    python -X utf8 -m x7k2qm.autoplay.status_api pets --json     # 原始 JSON

port：預設依 --profile（第 2 組 8791、第 N 組 8789+N），也可用 --port 指定。
API 沒開時提示要在 headless_party 加 `--api-port`。
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from .party_api import ENDPOINTS, port_for

DASH = "—"


def fetch(port: int, what: str, query: str = "", timeout: float = 60.0) -> Tuple[int, dict]:
    """GET http://127.0.0.1:<port>/<what>；HTTP 錯誤也回 (狀態碼, 內容)。連不上丟 URLError。"""
    url = f"http://127.0.0.1:{port}/{what}" + (f"?{query}" if query else "")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return e.code, {"error": str(e)}


def _v(x) -> str:
    return DASH if x is None else (f"{x:,}" if isinstance(x, int) and not isinstance(x, bool) else str(x))


def _hp(hp, mx) -> str:
    return f"{_v(hp)}/{_v(mx)}"


def _time(t) -> str:
    if not t:
        return "無期限"
    return time.strftime("%m-%d %H:%M", time.localtime(t))


def fmt_state(d: dict) -> List[str]:
    m = d.get("money") or {}
    lb = d.get("lastBattle")
    out = [f"地圖 {_v(d.get('map'))}｜隊伍 {_v(d.get('team'))}｜遇敵 {'開' if d.get('loop') else '關'}"
           f"｜快戰 {'開' if d.get('fast') else '關'}｜被踢 {'是' if d.get('kicked') else '否'}"
           f"｜最後一場 {_time(lb['t']) + (' 勝' if lb.get('win') == 1 else ' 敗') if lb else DASH}",
           f"活力 {_v(m.get('gold'))}／聲望 {_v(m.get('lockDenar'))}／石幣合計 {_v(m.get('stone'))}"]
    for r in d.get("roles") or []:
        pets = "／".join(f"{k} {p['name']} {_v(p.get('lv'))} 級 {_hp(p.get('hp'), p.get('hpMax'))}" if p else f"無{k}"
                        for k, p in (("戰寵", r.get("fight")), ("騎寵", r.get("ride"))))
        out.append(f"  {r['name']}：{_v(r.get('trans'))} 轉 {_v(r.get('lv'))} 級 hp {_hp(r.get('hp'), r.get('hpMax'))}｜{pets}")
    return out


def fmt_roles(d: dict) -> List[str]:
    out = ["  角色｜轉｜等級｜hp｜地圖（座標）｜石幣｜活力｜聲望"]
    for r in d.get("roles") or []:
        pos = r.get("pos") or {}
        out.append(f"  {r['name']}｜{_v(r.get('trans'))}｜{_v(r.get('lv'))}｜{_hp(r.get('hp'), r.get('hpMax'))}"
                   f"｜{_v(r.get('map'))}（{_v(pos.get('x'))},{_v(pos.get('y'))}）"
                   f"｜{_v(r.get('stone'))}｜{_v(r.get('gold'))}｜{_v(r.get('lockDenar'))}")
    return out


def fmt_role(d: dict) -> List[str]:
    labels = d.get("labels") or {}
    keys = ("level", "trans", "exp", "nextExp", "hp", "hpMax", "vitality", "strength", "stamina", "speed",
            "potentialPoint", "autoSetPointType", "earth", "water", "fire", "wind")
    out = []
    for r in d.get("roles") or []:
        out.append(f"[{r['name']}]")
        out.append("  " + "｜".join(f"{labels.get(k, k)} {_v(r.get(k))}" for k in keys))
    return out


def _per_role(d: dict, key: str, line: Callable[[dict], str], empty: str = "（空）") -> List[str]:
    out = []
    for name, rows in (d.get(key) or {}).items():
        out.append(f"[{name}] {len(rows)} 項")
        out.extend(f"  {line(x)}" for x in rows) if rows else out.append(f"  {empty}")
    return out


def fmt_bag(d: dict) -> List[str]:
    return _per_role(d, "bag", lambda x: f"格 {_v(x.get('slot'))}｜{x['name']}（{x.get('itemId')}）×{_v(x.get('count'))}")


def fmt_equip(d: dict) -> List[str]:
    return _per_role(d, "equip", lambda x: f"{x.get('part')}｜{x['name']}（{x.get('itemId')}，{_v(x.get('level'))} 級）",
                     "（沒穿裝備）")


def fmt_pets(d: dict) -> List[str]:
    out = []
    for name, v in (d.get("pets") or {}).items():
        out.append(f"[{name}] 外掛鎖定 戰寵 {_v(v.get('fightPetIndex'))}／騎寵 {_v(v.get('ridePetIndex'))}")
        for p in v.get("pets") or []:
            lock = "（鎖定戰寵）" if p.get("lockedFight") else "（鎖定騎寵）" if p.get("lockedRide") else ""
            out.append(f"  第 {p.get('index')} 格｜{p['name']}｜{_v(p.get('lv'))} 級｜hp {_hp(p.get('hp'), p.get('hpMax'))}"
                       f"｜{p.get('status')}{lock}")
    return out


def fmt_tasks(d: dict) -> List[str]:
    out = []
    for name, v in (d.get("tasks") or {}).items():
        done = v.get("finishedGroups") or []
        out.append(f"[{name}] 已完成任務組 {len(done)} 個：" + ("、".join(f"{g['group']} {g['name']}" for g in done) or DASH))
        for t in v.get("active") or []:
            sch = f"（進度 {t['schedule']}）" if t.get("schedule") else ""
            out.append(f"  進行中 {t['taskId']} {t.get('name')}｜組 {t['group']} {t.get('groupName')}｜{t.get('status')}{sch}")
    return out


def fmt_buffs(d: dict) -> List[str]:
    out = []
    for name, v in (d.get("buffs") or {}).items():
        exp = v.get("exp") or []
        out.append(f"  {name}：" + ("、".join(f"{b['name']} 到 {_time(b.get('until'))}" for b in exp) or "沒有經驗加成"))
    return out


def fmt_battle(d: dict) -> List[str]:
    lb = d.get("lastBattle")
    fast = d.get("fast") or {}
    return [f"近 5 分鐘 勝 {d.get('wins5m')}／敗 {d.get('losses5m')}（共 {d.get('battles5m')} 場）"
            f"｜最後一場 {_time(lb['t']) + (' 勝' if lb.get('win') == 1 else ' 敗') if lb else DASH}"
            f"｜遇敵 {'開' if d.get('loop') else '關'}",
            "快戰：" + "、".join(f"{n} {'開' if on else '關'}" for n, on in fast.items())]


def fmt_depot(d: dict) -> List[str]:
    pet = d.get("kind") == "petdepot"

    def line(x):
        if pet:
            return f"第 {x.get('page')} 頁｜{x['name']}｜{_v(x.get('lv'))} 級"
        return f"第 {x.get('page')} 頁｜{x['name']}（{x.get('itemId')}）×{_v(x.get('count'))}"
    title = "寵物倉庫" if pet else "道具倉庫"
    out = [f"{title}（送了 {d.get('requests')} 個請求{'，快取' if d.get('cached') else ''}，查詢時間 {d.get('queriedAt')}）"]
    if d.get("error"):
        out.append(f"⚠ 查詢中斷：{d['error']}（以下是已拿到的部分）")
    out.extend(_per_role({"x": d.get("personal") or {}}, "x", line))
    if d.get("public") is not None:
        out.extend(_per_role({"x": {"公共倉庫": d["public"]}}, "x", line))
    return out


FORMATS: Dict[str, Callable[[dict], List[str]]] = {
    "state": fmt_state, "roles": fmt_roles, "role": fmt_role, "bag": fmt_bag, "equip": fmt_equip, "pets": fmt_pets,
    "tasks": fmt_tasks, "buffs": fmt_buffs, "battle": fmt_battle, "depot": fmt_depot, "petdepot": fmt_depot}


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.status_api")
    ap.add_argument("what", nargs="?", default="state", choices=ENDPOINTS)
    ap.add_argument("--profile", default="2", help="第幾組帳號（決定預設 port：第 2 組 8791）")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--groups", default="", help="tasks 用：只列這些任務組，例：2000,2031")
    ap.add_argument("--json", action="store_true", help="印原始 JSON")
    args = ap.parse_args(argv)
    port = args.port or port_for(args.profile)
    query = f"groups={args.groups}" if args.what == "tasks" and args.groups else ""
    try:
        code, d = fetch(port, args.what, query)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        print(f"連不上本機查詢 API（127.0.0.1:{port}）：{getattr(e, 'reason', e)}", file=out)
        print(f"第 {args.profile} 組的 headless_party 啟動時要加 `--api-port {port}` 才會開。", file=out)
        return 1
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=1), file=out)
    else:
        if "error" in d and args.what not in ("depot", "petdepot"):
            print(f"錯誤（HTTP {code}）：{d['error']}", file=out)
            return 1
        fmt = FORMATS.get(args.what)                 # 沒有中文表格的端點（pet、scenario、jobs…）印 JSON
        for line in (fmt(d) if fmt else json.dumps(d, ensure_ascii=False, indent=1).splitlines()):
            print(line, file=out)
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
