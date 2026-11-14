"""trade-take：兩邊各自的程式交易，收方這一半（給方是 trade-give，在另一個程式／另一台機器）。

    # 只列：收的人是誰、rid、寵物欄與背包空格、交易暫存
    python -X utf8 -m x7k2qm.autoplay.jobs add trade-take --profile 2 --discover --taker 乙 --giver 甲 --pet-kind 481:1:0 --item 1501=3
    # 真的收：停戰鬥 → 核對 → 等甲的邀請（最多 10 分鐘），日誌「收方就緒」後再叫給方排 trade-give
    python -X utf8 -m x7k2qm.autoplay.jobs add trade-take --profile 2 --discover --apply --no-wait --taker 乙 --giver 甲 --pet-kind 481:1:0 --store

使用者 11-14 定的流程：AI 通知兩邊 session → 各自停戰鬥、確認 → 照現有交易規定交易。收方先排（被動等），給方後排（主動邀請），
不用另外約「同時開始」。只接交易單上那個人的邀請；對方放上來的東西（道具 ID 與數量、寵物種類／等級／轉數）跟交易單完全一樣、
對方先確認了，才按確認。多放、放錯、等不到 → 取消（還沒確認，不會成交）。確認之後出錯 → JobHold，不要重做。
"""

from typing import Tuple

from . import JobError, JobHold
from . import trade as T

NAME = "trade-take"
HELP = "兩邊各自的程式交易：收方（先排這個，等給方 trade-give 的邀請；不加 --apply 只列）"
SOLO = True
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731
HOSTED_OK = True


def add_args(ap) -> None:
    ap.add_argument("--taker", required=True, help="收的角色名字（這組的）")
    ap.add_argument("--role", type=int, default=None, help="沒有常駐時登這組第幾隻人物（1 起算），要跟 --taker 同一隻")
    ap.add_argument("--giver", required=True, help="給的角色名字（另一個帳號；邀請的名字要一樣）")
    ap.add_argument("--giver-rid", dest="giver_rid", type=int, default=None, help="給的角色 rid（有給就連 rid 一起核對）")
    ap.add_argument("--item", action="append", default=[], help="要收的道具 ID 或完整名稱[=數量]，可多次")
    ap.add_argument("--pet-kind", dest="pet_kind", action="append", default=[],
                    help="要收的寵物 petId[:等級[:轉數]]，一隻一個，可多次")
    ap.add_argument("--store", action="store_true", help="收完把收到的寵物存個人寵物倉")
    ap.add_argument("--invite-wait", dest="invite_wait", type=float, default=T.INVITE_WAIT_MIN,
                    help=f"等邀請最多幾分鐘（預設 {T.INVITE_WAIT_MIN}、上限 {T.INVITE_WAIT_MAX}）")
    ap.add_argument("--order", default=None, help="交易單號（只寫進日誌，方便兩邊對）")
    ap.add_argument("--half-wait", dest="half_wait", type=float, default=T.HALF_WAIT, help="等給方每一步最多幾秒")
    ap.add_argument("--group4-ok", dest="group4_ok", default=None, help="使用者當下明講要動第四組時填他的原話")


def check(args) -> None:
    T.check_profile(args)
    items, kinds = T.parse_items(args.item or []), T.parse_pet_kinds(args.pet_kind or [])
    if not items and not kinds:
        raise JobError("要給 --item 或 --pet-kind")
    if len(kinds) > T.MAX_PETS or len(kinds) + len(items) > T.MAX_WINDOW:
        raise JobError(f"一次最多 {T.MAX_PETS} 隻、共 {T.MAX_WINDOW} 格，這張單要分開排")
    if not 0 < args.invite_wait <= T.INVITE_WAIT_MAX:
        raise JobError(f"--invite-wait 要在 0～{T.INVITE_WAIT_MAX} 分鐘")
    if args.store and not kinds:
        raise JobError("--store 是存收到的寵物，沒有 --pet-kind 不用加")


def run(p, args) -> Tuple[str, dict]:
    if args.taker not in p.clients:
        raise JobError(f"{args.taker} 不在這次登入的人物裡（{sorted(p.clients)}）")
    order = f"交易單 {args.order}：" if args.order else ""
    items, kinds = T.parse_items(args.item or []), T.parse_pet_kinds(args.pet_kind or [])
    t = T.Side(p, args.taker)
    if not getattr(args, "apply", False):
        return (f"{order}只列（沒加 --apply，沒有送任何動作）：{t.name} rid {t.rid}，身上寵物 {len(t.pets())}/{T.PET_SLOTS}，"
                f"背包空 {t.free()} 格", {"rid": t.rid, "pets": len(t.pets()), "free": t.free()})
    before = {x.get("uniqueIndex") for x in t.pets().values()}
    got, n = T.take_half(t, args.giver, args.giver_rid, items, kinds, args.invite_wait * 60, wait=args.half_wait)
    new = [x for x in t.pets().values() if x.get("uniqueIndex") not in before]
    uids = [x.get("uniqueIndex") for x in new]
    if args.store and new:
        from .pets import do_deposit
        for x in new:
            try:
                do_deposit(p, t.name, int(x.get("petId")), uid=x.get("uniqueIndex"))
            except Exception as e:  # noqa: BLE001  已成交：存倉出錯一律 JobHold
                raise JobHold(f"{order}已成交，{t.name} 把 {x.get('uniqueIndex')} 存個倉失敗（{e}）：不要重做") from e
        p.log(f"交易：{t.name} 收到的 {len(new)} 隻存進個倉")
    return (f"{order}交易完成（收方）：{args.giver} → {t.name}：{T.describe(got, n)}"
            + (f"，寵物 {uids}" if uids else "") + ("（已存個倉）" if args.store and new else ""),
            {"items": got, "pets": uids, "order": args.order})
