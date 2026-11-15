"""stall：交易所攤位（道具＋寵物）列出、下架、領取（使用者 2026-11-15「我可以 api 把完整資訊通知帳號，他檢查過沒問題，
會自動去交易所上下架跟領取所有販賣道具寵物」；遠端指令走 `marketinbox.py`）。

    python -X utf8 -m x7k2qm.autoplay.jobs stall --profile N --discover                                  # 列出全隊攤位（只讀）
    python -X utf8 -m x7k2qm.autoplay.jobs add stall --profile N --mode claim --apply --confirm-stall     # 領取
    python -X utf8 -m x7k2qm.autoplay.jobs add stall --profile N --mode delist --uid 1000000940090 --apply --confirm-stall

協定照 參考專案 參考專案 `tools/headless/ops/market.py`、`ops/petstall.py`：
- 讀攤位：道具 `Other_MyItemShopGoods {action 1, itemIndex 0}` → itemList；寵物 `Other_MyPetShopGoods {action 1, petIndex 0}` → petList。
  每筆 uniqueIndex 是**攤位那筆**的編號（不是寵物的），type＝道具 ID／petId，isBuyed、overTime（到期 Unix 秒）。
- 下架／領取是**同一個請求** action 3＋攤位編號，伺服器看賣掉沒（賣了給錢、沒賣退東西）。所以送之前一定重讀，
  照「現在」的 isBuyed 判斷這次是哪一種，核對方式跟著換。下架的寵物一律回**身上**寵物欄（參考專案 錄檔），身上滿了不送。
- `--mode claim`（領取所有）：已賣出的領錢；**已到期**沒賣掉的退回背包／身上。還在賣、沒到期的不碰。
- `--mode delist --uid A,B`：只下架指定的攤位編號（一定要還沒賣出）。
成功＝那筆從攤位消失、其他筆一筆不少，而且：領錢的那種錢增加、別種錢不減；退回的道具背包多 itemNum 個、寵物身上多一隻同物種。
送之前對不上 → JobError；送了之後對不上 → JobHold（不重送）。多筆中途出錯就停，前面完成的列出來（規範 2）。
"""

import time
from typing import Dict, List, Optional, Tuple

from . import JobError, JobHold
from .auction import CURRENCY_NAMES

NAME = "stall"
HELP = "交易所攤位（道具＋寵物）：--mode list 列出／claim 領取已賣出與已到期／delist --uid 下架；--apply 要再加 --confirm-stall"
PAUSE = lambda a: bool(getattr(a, "apply", False))      # noqa: E731

ITEM, PET = "item", "pet"
REQ = {ITEM: ("Other_MyItemShopGoods", "itemIndex", "itemList"), PET: ("Other_MyPetShopGoods", "petIndex", "petList")}
KIND_NAMES = {ITEM: "道具", PET: "寵物"}
ACTION_LIST, ACTION_TAKE = 1, 3
MONEY_FIELD = {3: "stone", 1: "denar", 2: "gold", 8: "lockDenar"}   # 幣種 → RoleAttr 欄位（headless_party money）
BODY_MAX = 5
WAIT = 8.0
MODES = ("list", "claim", "delist")


def add_args(ap) -> None:
    ap.add_argument("--mode", default="list", choices=MODES, help="list 列出（預設）／claim 領取已賣出與已到期／delist 下架 --uid")
    ap.add_argument("--uid", default=None, help="delist：攤位編號（stall 列出的「編號」，逗號分隔多筆）")
    ap.add_argument("--who", default=None, help="只看這隻角色（預設全隊）")
    ap.add_argument("--confirm-stall", dest="confirm_stall", action="store_true", help="--apply 時一定要加（會動攤位）")


def uids_of(args) -> List[int]:
    raw = str(getattr(args, "uid", None) or "").strip()
    if not raw:
        return []
    out = []
    for s in raw.split(","):
        s = s.strip()
        if not s.isdigit() or int(s) <= 0:
            raise JobError(f"--uid 要是攤位編號（正整數），收到 {s!r}")
        if int(s) not in out:
            out.append(int(s))
    return out


def check(args) -> None:
    mode = getattr(args, "mode", "list") or "list"
    if mode not in MODES:
        raise JobError(f"--mode 只能是 {'／'.join(MODES)}")
    uids = uids_of(args)
    if mode == "delist" and not uids:
        raise JobError("下架要給 --uid（攤位編號）")
    if mode != "delist" and uids:
        raise JobError("--uid 只有 --mode delist 用")
    if getattr(args, "apply", False) and mode != "list" and not getattr(args, "confirm_stall", False):
        raise JobError("動攤位要加 --confirm-stall")


def when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def read(c, kind: str) -> List[dict]:
    req, field, out = REQ[kind]
    r = c.request(req, {"action": ACTION_LIST, field: 0}) or {}
    rows = r.get(out) or []
    rows = list(rows.values()) if isinstance(rows, dict) else list(rows)
    return [dict(g, kind=kind) for g in rows if isinstance(g, dict) and g.get("uniqueIndex")]


def stalls(p, who: str) -> List[dict]:
    c = p.clients[who]
    return read(c, ITEM) + read(c, PET)


def goods_name(p, g: dict) -> str:
    t = g.get("type")
    if g["kind"] == PET:
        return f"{p.pet_name({'petId': t})}（{t}）"
    return f"{(p.item_table().get(t, {}) or {}).get('nameTn') or t}×{g.get('itemNum') or 1}"


def state_of(g: dict, now: float) -> str:
    if g.get("isBuyed"):
        return "sold"
    if g.get("overTime") and int(g["overTime"]) <= now:
        return "expired"
    return "selling"


STATE_NAMES = {"sold": "已賣出", "expired": "已到期", "selling": "在賣"}


def describe(p, who: str, g: dict, now: float) -> dict:
    return {"who": who, "kind": g["kind"], "uid": g.get("uniqueIndex"), "type": g.get("type"), "name": goods_name(p, g),
            "num": int(g.get("itemNum") or 1), "price": int(g.get("price") or 0),
            "currency": CURRENCY_NAMES.get(g.get("currency"), g.get("currency")), "state": state_of(g, now),
            "overTime": g.get("overTime")}


def money(p, who: str) -> Dict[str, Optional[int]]:
    with p.lock:
        m = dict(p.money.get(who) or {})
    return {f: m.get(f) for f in MONEY_FIELD.values()}


def bag_count(p, who: str, item: int) -> int:
    return sum(int(it.get("overlay") or 1) for it in p.bag(who) if it.get("itemId") == item)


def body_pets(p, who: str) -> Dict[int, dict]:
    with p.lock:
        return {i: dict(x) for i, x in p.petinfo[who].items()}


def take_one(p, who: str, g: dict, now: float) -> dict:
    """對攤位一筆送 action 3。送前重讀確認這筆還在、狀態跟計畫的一樣；送後照狀態核對。回描述（含 result）。"""
    c, kind, uid = p.clients[who], g["kind"], g["uniqueIndex"]
    req, field, _ = REQ[kind]
    rows0 = {x["uniqueIndex"]: x for x in read(c, kind)}
    cur = rows0.get(uid)
    if cur is None:
        raise JobError(f"{who} {KIND_NAMES[kind]}攤位已經沒有 {uid}（可能已經被領走），不送")
    st = state_of(cur, now)
    if st != state_of(g, now):
        raise JobError(f"{who} {uid} 狀態從 {STATE_NAMES[state_of(g, now)]} 變成 {STATE_NAMES[st]}，不送（重新列一次再決定）")
    d = describe(p, who, cur, now)
    w0, t0, b0 = money(p, who), None, None
    if st != "sold":
        if kind == ITEM:
            t0 = bag_count(p, who, cur.get("type"))
        else:
            b0 = body_pets(p, who)
            if len(b0) >= BODY_MAX:
                raise JobError(f"{who} 身上寵物 {len(b0)} 隻已滿：下架的寵物回身上，先存倉騰一格再下架 {uid}")
    if not p.wait(lambda: not p.fighting.get(who), 60):
        raise JobError(f"{who} 還在戰鬥中，不動攤位")
    c.request(req, {"action": ACTION_TAKE, field: uid})
    rows1 = {x["uniqueIndex"]: x for x in read(c, kind)}
    lost = sorted(set(rows0) - set(rows1) - {uid})
    what = f"{who} {'領取' if st == 'sold' else '下架'} {d['name']}（攤位編號 {uid}）"
    if uid in rows1 and not lost:
        raise JobError(f"{what} 被拒：攤位上還有這筆、沒有任何變化") if _same_money(w0, money(p, who)) else \
            JobHold(f"{what} 後攤位還有這筆，但錢有變化 {w0} → {money(p, who)}，停在原地")
    if uid in rows1 or lost:
        raise JobHold(f"{what} 對不上：這筆還在={uid in rows1}、其他筆不見 {lost}，停在原地")
    if st == "sold":
        field_m = MONEY_FIELD.get(cur.get("currency"))
        up = p.wait(lambda: field_m is None or (money(p, who).get(field_m) or 0) > (w0.get(field_m) or 0), WAIT)
        if not up:
            raise JobHold(f"{what} 後 {d['currency']} 沒增加（{w0.get(field_m)}），停在原地")
        d["got"] = (money(p, who).get(field_m) or 0) - (w0.get(field_m) or 0) if field_m else None
    elif kind == ITEM:
        n = int(cur.get("itemNum") or 1)
        if not p.wait(lambda: bag_count(p, who, cur.get("type")) >= t0 + n, WAIT):
            raise JobHold(f"{what} 後背包沒多 {n} 個（{t0} → {bag_count(p, who, cur.get('type'))}），停在原地")
    else:
        def back():
            now_b = body_pets(p, who)
            return [x for i, x in now_b.items() if i not in b0 and x.get("petId") == cur.get("type")]
        if not p.wait(lambda: len(back()) == 1, WAIT):
            raise JobHold(f"{what} 後身上沒有剛好多一隻 {d['name']}（多了 {len(back())} 隻），停在原地")
        d["petUid"] = back()[0].get("uniqueIndex")
    w1 = money(p, who)
    less = {k: (w0[k], w1[k]) for k in w0 if w0[k] is not None and w1[k] is not None and w1[k] < w0[k]}
    if less:
        raise JobHold(f"{what} 後錢變少 {less}，停在原地")
    d["result"] = "claimed" if st == "sold" else "returned"
    p.log(f"攤位：{what} 完成" + (f"，{d['currency']} +{d['got']:,}" if d.get("got") else ""))
    return d


def _same_money(a: dict, b: dict) -> bool:
    return all(a[k] == b[k] for k in a)


def run(p, args) -> Tuple[str, dict]:
    check(args)
    names = [n for _, n in p.party]
    who = getattr(args, "who", None)
    if who and who not in names:
        raise JobError(f"--who {who} 不在這組")
    roles = [who] if who else names
    mode, apply, now = getattr(args, "mode", "list") or "list", bool(getattr(args, "apply", False)), time.time()
    rows: List[Tuple[str, dict]] = []
    for n in roles:
        for g in stalls(p, n):
            rows.append((n, g))
            d = describe(p, n, g, now)
            p.log(f"攤位：{n}｜{KIND_NAMES[g['kind']]}｜編號 {d['uid']}｜{d['name']}｜{d['price']:,} {d['currency']}｜"
                  f"{STATE_NAMES[d['state']]}｜到期 {when(d['overTime'])}")
    listing = [describe(p, n, g, now) for n, g in rows]
    if mode == "list":
        cnt = {s: sum(1 for d in listing if d["state"] == s) for s in STATE_NAMES}
        return (f"攤位 {len(listing)} 筆：在賣 {cnt['selling']}、已賣出 {cnt['sold']}、已到期 {cnt['expired']}",
                {"stall": listing})
    if mode == "claim":
        todo = [(n, g) for n, g in rows if state_of(g, now) in ("sold", "expired")]
    else:
        want = uids_of(args)
        by = {g["uniqueIndex"]: (n, g) for n, g in rows}
        miss = [u for u in want if u not in by]
        if miss:
            raise JobError(f"攤位上找不到編號 {miss}（先 --mode list 看編號）")
        sold = [u for u in want if by[u][1].get("isBuyed")]
        if sold:
            raise JobError(f"編號 {sold} 已經賣出，用 --mode claim 領錢，不是下架")
        todo = [by[u] for u in want]
    # 寵物退回身上：先算每隻角色身上放不放得下（送任何東西之前）
    for n in roles:
        need = sum(1 for m, g in todo if m == n and g["kind"] == PET and state_of(g, now) != "sold")
        have = len(body_pets(p, n))
        if need and have + need > BODY_MAX:
            raise JobError(f"{n} 身上寵物 {have} 隻，要退回 {need} 隻會超過 {BODY_MAX}，先存倉騰位置")
    plan = [describe(p, n, g, now) for n, g in todo]
    if not todo:
        return f"沒有要{'領取' if mode == 'claim' else '下架'}的（攤位 {len(listing)} 筆）", {"stall": listing, "done": []}
    if not apply:
        return (f"只列（沒加 --apply）：要{'領取' if mode == 'claim' else '下架'} {len(plan)} 筆：" +
                "、".join(f"{d['who']} {d['name']}（{STATE_NAMES[d['state']]}）" for d in plan),
                {"stall": listing, "plan": plan})
    done = []
    for k, (n, g) in enumerate(todo):
        try:
            done.append(take_one(p, n, g, now))
        except (JobError, JobHold) as e:
            if not done:
                raise
            msg = f"第 {k + 1}/{len(todo)} 筆出錯，前面完成 {len(done)} 筆（{[d['uid'] for d in done]}）：{e}"
            raise (JobHold if isinstance(e, JobHold) else JobError)(msg) from e
        if k + 1 < len(todo):
            p.nap(0.6)
    got = {}
    for d in done:
        if d.get("got"):
            got[d["currency"]] = got.get(d["currency"], 0) + d["got"]
    ret = sum(1 for d in done if d["result"] == "returned")
    head = f"{'領取' if mode == 'claim' else '下架'} {len(done)} 筆"
    tail = "、".join(f"{c} +{v:,}" for c, v in got.items())
    return (head + (f"：{tail}" if tail else "") + (f"，退回 {ret} 筆" if ret else ""), {"stall": listing, "done": done})
