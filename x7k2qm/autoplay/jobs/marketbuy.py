"""marketbuy：交易所（集市）買別人上架的道具（使用者 2026-11-09「用 twidc／cgy 的帳號去交易所用聲望買這些道具，
要確認資料完全正確才可以購買，平均買，不要把一個帳號聲望清空」）。

    python -X utf8 -m x7k2qm.autoplay.jobs marketbuy --profile 1 --item 豪華船生魚片 --currency 聲望 --max-price 3000      # 只列
    python -X utf8 -m x7k2qm.autoplay.jobs add marketbuy --profile 1 --item 2174 --currency 聲望 --max-price 3000 \\
        --uid <列表裡那一筆的 uniqueIndex> --keep 1000 --apply --confirm-buy                                                 # 買那 1 筆

協定照 參考專案 `tools/headless/ops/marketbuy.py`（官方錄檔 10-24、10-25、10-28，A 級）：
- 分類頁 `Other_GetNewShopBoothGoodType {shopBoothNameId 1, page, groupIndex 0, petImage 0, priceType 0}` → `goodId[]`：
  官方先翻到商品出現才點進去；翻完都沒有＝沒人在賣，不讀列表。
- 列表 `Other_GetNewShopBoothGoodInfo {goodId, extra "0", page, type 1}` → `newshopGoodInfo[]`（6 筆／頁）；
  官方每讀一頁就送一筆行情 `Other_NewShopGetItemPriceRecord {itemId, currecy 1}`，照送。
- 購買 `Other_NewShopBuyGood {type 1, uniqueIndex, sellerRid}` → `{buyOk}`：不帶 extra；請求沒有數量欄，
  一筆 goodNum 個整筆買；扣款正好 −price（整筆價），**買方沒有手續費**（稅是賣方領錢時扣）。

只買指定的那一筆（`--uid`，從只列的結果挑），每次 1 筆。不買鑽石（真錢）賣的；不買自己這組賣的。
送前核對：那筆還在列表、道具在要買的 ID 裡、幣種相符、0 < 整筆價 ≤ `--max-price`、餘額 − 整筆價 ≥ `--keep`、背包有空格、不在戰鬥。
送後核對：buyOk、該幣種**正好 −整筆價**、其他幣種不變、背包那個道具 **+goodNum**；
扣了錢卻對不上／送出沒回應 → JobHold 停在原地（不重送）；buyOk false 而且什麼都沒變 → JobError。
"""

import time
from typing import Dict, List, Optional, Tuple

from . import JobError, JobHold

NAME = "marketbuy"
HELP = "交易所買別人上架的道具（--item、--currency、--max-price；--apply 要 --uid 指定那一筆、再加 --confirm-buy）"
PAUSE = lambda a: True                  # noqa: E731  讀列表也要不在戰鬥（伺服器戰鬥中不回交易所）

TYPE_PROTO = "Other_GetNewShopBoothGoodType"
LIST = "Other_GetNewShopBoothGoodInfo"
RECORD = "Other_NewShopGetItemPriceRecord"
BUY = "Other_NewShopBuyGood"
ITEM = 1                                # 列表／購買的 type：1 道具（2 寵物，這個工作不買寵物）
BOOTH_ITEM = 1                          # 分類頁 shopBoothNameId：1 道具
PAGE_CAP = 30                           # 防呆：每個 ID 最多翻幾頁（官方錄檔 maxPage 最多 10）
CURRENCIES = {"活力": 2, "石幣": 3, "聲望": 8}    # 鑽石（1）是真錢，不買
CURRENCY_NAMES = {v: k for k, v in CURRENCIES.items()}
FIELDS = {2: "gold", 3: "stone", 8: "lockDenar"}
WALLET = ("gold", "stone", "lockDenar", "denar")
KEEP_DEFAULT = 1000                     # 買完至少留多少（使用者：不要把一個帳號清空）
BAG_DEFAULT = 15
BUY_WAIT = 8.0


def add_args(ap) -> None:
    ap.add_argument("--item", default=None, help="要買的道具（名稱或 ID；同名的幾個 ID 一起查）")
    ap.add_argument("--currency", default="聲望", help="用哪種錢：聲望（預設）／活力／石幣（鑽石不買）")
    ap.add_argument("--max-price", dest="max_price", type=int, default=None, help="整筆價上限（實際扣的錢）")
    ap.add_argument("--uid", type=int, default=None, help="要買的那一筆 uniqueIndex（--apply 必給，從只列的結果挑）")
    ap.add_argument("--keep", type=int, default=KEEP_DEFAULT, help=f"買完至少留多少（預設 {KEEP_DEFAULT}）")
    ap.add_argument("--who", default=None, help="哪一隻買（預設隊長）")
    ap.add_argument("--confirm-buy", dest="confirm_buy", action="store_true", help="--apply 時一定要加（花錢、不可逆）")


def currency_of(v) -> int:
    s = str(v if v is not None else "聲望").strip()
    if s in CURRENCIES:
        return CURRENCIES[s]
    if s.isdigit() and int(s) in CURRENCY_NAMES:
        return int(s)
    raise JobError(f"幣別 {v!r} 不買（只准 {'／'.join(CURRENCIES)}；鑽石是真錢）")


def check(args) -> None:
    if not getattr(args, "item", None):
        raise JobError("要給 --item（道具名稱或 ID）")
    currency_of(getattr(args, "currency", "聲望"))
    if not getattr(args, "max_price", None) or int(args.max_price) <= 0:
        raise JobError("要給 --max-price（整筆價上限）")
    if getattr(args, "apply", False):
        if not getattr(args, "confirm_buy", False):
            raise JobError("交易所購買要加 --confirm-buy")
        if not getattr(args, "uid", None):
            raise JobError("買要用 --uid 指定那一筆（先不加 --apply 只列，核對過再挑）")


def item_ids(p, spec: str) -> List[int]:
    """道具名稱或 ID → 同名的全部 ID（Item 表同一個名字常有新舊幾個 ID，交易所可能掛在任一個）。"""
    table = p.item_table()
    spec = str(spec).strip()
    if spec.isdigit():
        name = (table.get(int(spec)) or {}).get("nameTn")
        return sorted({int(spec)} | {i for i, v in table.items() if name and v.get("nameTn") == name})
    hit = sorted(i for i, v in table.items() if v.get("nameTn") == spec)
    if not hit:
        raise JobError(f"找不到道具：{spec}")
    return hit


def when(ts) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def rows_of(r: Optional[dict]) -> List[dict]:
    v = (r or {}).get("newshopGoodInfo") or []
    return list(v.values()) if isinstance(v, dict) else list(v)


def browse(c, gid: int) -> bool:
    """官方搜尋：分類頁從第 1 頁翻到 goodId 出現為止；翻完都沒有＝沒人在賣。"""
    page, maxp = 1, 1
    while page <= min(maxp, PAGE_CAP):
        r = c.request("Other_GetNewShopBoothGoodType", {"shopBoothNameId": BOOTH_ITEM, "page": page, "groupIndex": 0,
                                   "petImage": 0, "priceType": 0}) or {}
        maxp = int(r.get("maxPage") or 1)
        if gid in (r.get("goodId") or []):
            return True
        page += 1
    return False


def listing(c, ids: List[int]) -> List[dict]:
    """要買的那幾個 ID 在交易所的全部上架（翻到 maxPage；每頁後送一筆行情，同官方）。"""
    out, seen = [], set()
    for gid in ids:
        if not browse(c, gid):
            continue
        page, maxp = 1, 1
        while page <= min(maxp, PAGE_CAP):
            r = c.request("Other_GetNewShopBoothGoodInfo", {"goodId": gid, "extra": "0", "page": page, "type": ITEM}) or {}
            maxp = int(r.get("maxPage") or 1)
            for g in rows_of(r):
                if g.get("uniqueIndex") not in seen:
                    seen.add(g.get("uniqueIndex"))
                    out.append(g)
            c.request("Other_NewShopGetItemPriceRecord", {"itemId": gid, "currecy": 1})
            page += 1
    return out


def describe(p, g: dict) -> str:
    cur = g.get("currency")
    name = (p.item_table().get(g.get("goodId"), {}) or {}).get("nameTn") or str(g.get("goodId"))
    return (f"編號 {g.get('uniqueIndex')}｜{name}({g.get('goodId')})×{g.get('goodNum')}｜"
            f"{int(g.get('price') or 0):,} {CURRENCY_NAMES.get(cur, f'幣種{cur}')}｜賣家 {g.get('sellerRid')}｜到期 {when(g.get('overTime'))}")


def wallet(p, who: str) -> Dict[str, Optional[int]]:
    with p.lock:
        m = dict(p.money.get(who) or {})
    return {k: m.get(k) for k in WALLET}


def count_of(p, who: str, ids) -> int:
    return sum(int(it.get("overlay") or 1) for it in p.bag(who) if it.get("itemId") in ids)


def bag_free(p, who: str) -> int:
    with p.lock:
        size = (p.attrs.get(who) or {}).get("bagNum") or BAG_DEFAULT
    return size - len(p.bag(who))


def run(p, args) -> Tuple[str, dict]:
    check(args)
    names = [n for _, n in p.party]
    who = getattr(args, "who", None) or names[0]
    if who not in names:
        raise JobError(f"--who {who} 不在這組")
    ids, cur, cap = item_ids(p, args.item), currency_of(getattr(args, "currency", "聲望")), int(args.max_price)
    field, keep = FIELDS[cur], int(getattr(args, "keep", KEEP_DEFAULT) or 0)
    if not p.wait(lambda: not p.fighting.get(who), 60):
        raise JobError(f"{who} 還在戰鬥中，交易所不會回應")
    c = p.clients[who]
    rows = listing(c, ids)
    for g in rows:
        p.log(f"交易所：{describe(p, g)}")
    have = wallet(p, who)[field]
    p.log(f"交易所：{ids} 共 {len(rows)} 筆；{who} {CURRENCY_NAMES[cur]} {have}")
    found = [{k: g.get(k) for k in ("uniqueIndex", "goodId", "goodNum", "price", "currency", "sellerRid", "overTime")}
             for g in rows]
    if not getattr(args, "apply", False):
        return f"只列：{len(rows)} 筆（沒加 --apply，沒有買）", {"listing": found, "have": have}
    uid = int(args.uid)
    hit = [g for g in rows if g.get("uniqueIndex") == uid]
    if not hit:
        raise JobError(f"列表裡沒有編號 {uid}（可能已被買走／下架），不買")
    g = hit[0]
    price, num = int(g.get("price") or 0), int(g.get("goodNum") or 0)
    own = {r for r, _ in p.party} | {getattr(x, "rid", None) for x in p.clients.values()}
    if g.get("goodId") not in ids:
        raise JobError(f"編號 {uid} 是 goodId {g.get('goodId')}，不在要買的 {ids}，不買")
    if g.get("currency") != cur:
        raise JobError(f"編號 {uid} 用幣種 {g.get('currency')} 賣，不是{CURRENCY_NAMES[cur]}，不買")
    if not 0 < price <= cap or num < 1:
        raise JobError(f"編號 {uid} 整筆價 {price:,}×{num} 不在 1～{cap:,}，不買")
    if g.get("sellerRid") in own:
        raise JobError(f"編號 {uid} 是這組自己賣的，不買")
    if have is None:
        raise JobError(f"{who} 還沒收到{CURRENCY_NAMES[cur]}推送，無法核對扣款，不買")
    if have - price < keep:
        raise JobError(f"{who} {CURRENCY_NAMES[cur]} {have:,} 買完剩 {have - price:,}，低於保底 {keep:,}，不買")
    if bag_free(p, who) < 1:
        raise JobError(f"{who} 背包沒空格，買了收不下，不買")
    w0, c0 = wallet(p, who), count_of(p, who, set(ids))
    p.log(f"交易所：{who} 買 {describe(p, g)}（{CURRENCY_NAMES[cur]} {have:,} → 應剩 {have - price:,}）")
    try:
        r = c.request("Other_NewShopBuyGood", {"type": ITEM, "uniqueIndex": uid, "sellerRid": g.get("sellerRid")}) or {}
    except Exception as e:  # noqa: BLE001  送出後沒回應：不知道買了沒，停在原地
        raise JobHold(f"{who} 買編號 {uid} 送出後沒回應（{e}），停在原地（不重送）")
    ok = bool(r.get("buyOk"))
    if ok:
        p.wait(lambda: wallet(p, who)[field] == w0[field] - price and count_of(p, who, set(ids)) >= c0 + num, BUY_WAIT)
    w1, c1 = wallet(p, who), count_of(p, who, set(ids))
    changed = [k for k in WALLET if w1[k] != w0[k]]
    if not ok:
        if changed or c1 != c0:
            raise JobHold(f"買編號 {uid} 回 buyOk false，但錢包 {changed}／件數 {c0} → {c1} 變了，停在原地")
        raise JobError(f"買編號 {uid} 回 buyOk false（可能已被別人買走），錢包、背包都沒變，不重送")
    if w1[field] != w0[field] - price:
        raise JobHold(f"買編號 {uid} 後{CURRENCY_NAMES[cur]} {w0[field]} → {w1[field]}（應正好 −{price:,}），停在原地")
    other = [f"{k} {w0[k]} → {w1[k]}" for k in changed if k != field and not (k == "gold" and (w1[k] or 0) > (w0[k] or 0))]
    if other:
        raise JobHold(f"買編號 {uid} 後其他幣種也變了 {other}，停在原地")
    if c1 != c0 + num:
        raise JobHold(f"買編號 {uid} 後背包 {c0} → {c1}（應 +{num}），停在原地")
    p.log(f"交易所：{who} 買到編號 {uid}，{CURRENCY_NAMES[cur]} {w0[field]:,} → {w1[field]:,}，背包 {c0} → {c1}")
    return (f"{who} 買到編號 {uid}（{num} 個，{price:,} {CURRENCY_NAMES[cur]}），剩 {w1[field]:,}",
            {"uid": uid, "price": price, "num": num, "before": w0[field], "after": w1[field]})
