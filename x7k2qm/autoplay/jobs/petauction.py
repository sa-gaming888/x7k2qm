"""petauction：個人寵物倉庫裡的寵物上架交易所（使用者 2026-11-14「抓5隻水爆 1隻黑爆，沒達標統一上架 賣 200 聲望」）。

    python -X utf8 -m x7k2qm.autoplay.jobs petauction --profile 8 --discover --pet-uid 1000000193991 --price 200 --currency 聲望   # 只列
    python -X utf8 -m x7k2qm.autoplay.jobs petauction --profile 8 --discover --pet-uid A,B,C --price 200 --currency 聲望 --apply --confirm-list

協定照 參考專案 參考專案 `tools/headless/ops/petstall.py`（官方畫面錄檔 psa_20261023_130926，從個人倉上架 2 筆）：
- 讀攤位 `Other_MyPetShopGoods {action 1, petIndex 0}` → `petList[]`（uniqueIndex 是攤位那筆的，不是寵物的；type＝petId、itemNum 1）。
- 從個人寵物倉上架 `Other_NewUploadPetDepotToAh {depotIndex 寵物 uniqueIndex, isPublic false, currency, price, canCallPrice}` → `{isOK}`
  （失敗 ErrorTip 1 不存在／2 人物狀態非正常）。
- action 3 是下架／領取（同一個請求）——這個工作**不送**。
只從個倉上架（抓到的本來就存個倉；身上的要先 `pets --mode deposit`）。不開議價。
成功＝isOK、個倉少了那隻、攤位剛好多一筆（type／price／幣種／itemNum 1／未售都對）。
送之前對不上 → JobError；送了之後對不上 → JobHold（不重送，停在原地等人看）。多隻時中途出錯就停，前面完成的列出來。
"""

import time
from typing import List, Optional, Tuple

from . import JobError, JobHold
from .auction import CURRENCIES, CURRENCY_NAMES, currency_of

NAME = "petauction"
HELP = "個倉寵物上架交易所（--pet-uid 唯一編號可逗號多隻、--price、--currency；--apply 要再加 --confirm-list）"
PAUSE = lambda a: bool(getattr(a, "apply", False))      # noqa: E731

STALL = "Other_MyPetShopGoods"
UPLOAD = "Other_NewUploadPetDepotToAh"
ACTION_LIST = 1                         # 2＝從身上上架（這個工作不用）、3＝下架／領取（不送）
STALL_MAX = 20                          # Config.newShopBoothUpPetMaxNum
PRICE_MIN, PRICE_MAX = 10, 999_999      # 官方輸入窗的上下限（參考專案 IL 核對）
ERROR_TIPS = {1: "不存在", 2: "人物狀態非正常"}


def add_args(ap) -> None:
    ap.add_argument("--pet-uid", dest="pet_uid", default=None,
                    help="要上架的寵物唯一編號（逗號分隔可多隻；要在這組某隻角色的個人寵物倉庫）")
    ap.add_argument("--price", type=int, default=None, help=f"每隻賣多少（{PRICE_MIN}～{PRICE_MAX:,}）")
    ap.add_argument("--currency", default=None, help="收哪種錢（必填）：石幣／活力／聲望／鑽石，或代號 3／2／8／1")
    ap.add_argument("--confirm-list", dest="confirm_list", action="store_true",
                    help="--apply 時一定要加（上架會把寵物交給交易所）")


def uids_of(args) -> List[int]:
    raw = [s.strip() for s in str(getattr(args, "pet_uid", None) or "").split(",") if s.strip()]
    if not raw or not all(s.isdigit() for s in raw):
        raise JobError("要給 --pet-uid（寵物唯一編號，逗號分隔）")
    uids = [int(s) for s in raw]
    if len(set(uids)) != len(uids):
        raise JobError("--pet-uid 有重複")
    return uids


def check(args) -> None:
    uids_of(args)
    if getattr(args, "price", None) is None or getattr(args, "currency", None) is None:
        raise JobError("要給 --price、--currency")
    currency_of(args.currency)
    if not PRICE_MIN <= int(args.price) <= PRICE_MAX:
        raise JobError(f"--price 要在 {PRICE_MIN}～{PRICE_MAX:,}")
    if getattr(args, "apply", False) and not getattr(args, "confirm_list", False):
        raise JobError("上架要加 --confirm-list")


def when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def stall(c) -> List[dict]:
    r = c.request("Other_MyPetShopGoods", {"action": ACTION_LIST, "petIndex": 0}) or {}
    rows = r.get("petList") or []
    rows = list(rows.values()) if isinstance(rows, dict) else list(rows)
    return [dict(g) for g in rows if isinstance(g, dict) and g.get("uniqueIndex")]


def find(p, uid: int) -> Tuple[Optional[str], Optional[dict]]:
    """那隻寵物在這組哪一隻角色的個人寵物倉庫（個倉每隻角色各一份）。"""
    for _, name in p.party:
        for row in p.pet_depot(name):
            if row.get("uniqueIndex") == uid:
                return name, row
    return None, None


def refuse(row: dict) -> Optional[str]:
    if row.get("bind") == 1:
        return "綁定"
    if row.get("petLock") == 1:
        return "上鎖"
    return None


def label(p, row: dict) -> str:
    return f"{p.pet_name(row)}（{row.get('petId')}，{row.get('level')} 級，唯一編號 {row.get('uniqueIndex')}）"


def upload_one(p, who: str, row: dict, price: int, cur: int) -> dict:
    """上架一隻。成功回攤位上新的那一筆；被拒 JobError、對不上 JobHold（不重送）。"""
    c, uid, unit = p.clients[who], row["uniqueIndex"], CURRENCY_NAMES[cur]
    if not p.wait(lambda: not p.fighting.get(who), 60):
        raise JobError(f"{who} 還在戰鬥中，不上架")
    rows0 = stall(c)
    if len(rows0) >= STALL_MAX:
        raise JobError(f"{who} 寵物攤位已有 {len(rows0)} 筆（上限 {STALL_MAX}），不上架")
    old = {g["uniqueIndex"] for g in rows0}
    r = c.request("Other_NewUploadPetDepotToAh", {"depotIndex": uid, "isPublic": False, "currency": cur, "price": price,
                           "canCallPrice": False}) or {}
    gone = all(x.get("uniqueIndex") != uid for x in p.pet_depot(who))
    rows1 = stall(c)
    new = [g for g in rows1 if g["uniqueIndex"] not in old]
    hit = [g for g in new if g.get("type") == row.get("petId") and int(g.get("price") or 0) == price
           and g.get("currency") == cur and int(g.get("itemNum") or 0) == 1 and not g.get("isBuyed")]
    lost = old - {g["uniqueIndex"] for g in rows1}
    if not r.get("isOK") and not gone and not new:
        tip = ERROR_TIPS.get(r.get("ErrorTip"), "")
        raise JobError(f"{who} 上架 {label(p, row)} 被拒（{tip or r}），寵物還在個倉")
    if not (r.get("isOK") and gone and len(new) == 1 and len(hit) == 1 and not lost):
        raise JobHold(f"{who} 上架 {label(p, row)} 對不上：isOK={r.get('isOK')}、個倉少了={gone}、攤位新增 {len(new)} 筆"
                      f"（符合 {len(hit)} 筆）、攤位其他筆不見 {sorted(lost)}，停在原地")
    g = hit[0]
    p.log(f"寵物上架：{who} {label(p, row)} → {price:,} {unit}，到期 {when(g.get('overTime'))}（攤位編號 {g.get('uniqueIndex')}）")
    return g


def run(p, args) -> Tuple[str, dict]:
    check(args)
    uids, price, cur = uids_of(args), int(args.price), currency_of(args.currency)
    unit = CURRENCY_NAMES[cur]
    plan = []
    for uid in uids:
        who, row = find(p, uid)
        if not row:
            raise JobError(f"這組五隻的個人寵物倉庫都沒有唯一編號 {uid}，一隻都不上架")
        why = refuse(row)
        if why:
            raise JobError(f"{who} 的 {label(p, row)} {why}，不能上架，一隻都不上架")
        plan.append((who, row))
        p.log(f"寵物上架：{who} 個倉 {label(p, row)} → {price:,} {unit}")
    if not getattr(args, "apply", False):
        return (f"只列：{len(plan)} 隻各賣 {price:,} {unit}（沒加 --apply，沒有送任何動作）",
                {"count": len(plan), "currency": unit, "price": price})
    done = []
    for k, (who, row) in enumerate(plan):
        try:
            g = upload_one(p, who, row, price, cur)
        except (JobError, JobHold) as e:
            if not done:
                raise
            # 已經上架的不能當沒發生：前面成功的列出來，後面不做（規範 2）
            msg = f"第 {k + 1}/{len(plan)} 隻出錯，前面完成 {len(done)} 隻（{[d['petUid'] for d in done]}）：{e}"
            raise (JobHold if isinstance(e, JobHold) else JobError)(msg) from e
        done.append({"who": who, "petUid": row["uniqueIndex"], "petId": row.get("petId"),
                     "stallIndex": g.get("uniqueIndex"), "overTime": g.get("overTime")})
        if k + 1 < len(plan):
            p.nap(0.6)
    return (f"上架 {len(done)} 隻，各 {price:,} {unit}，到期 {when(done[-1]['overTime'])}",
            {"count": len(done), "currency": unit, "price": price, "lots": done})


__all__ = ["NAME", "HELP", "add_args", "check", "run", "stall", "find", "upload_one", "CURRENCIES"]
