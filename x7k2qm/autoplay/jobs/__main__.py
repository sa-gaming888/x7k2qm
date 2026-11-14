"""固定指令入口（工作佇列＋帳號擁有者鎖；doc/autoplay/flows/jobs.md）。

    python -X utf8 -m x7k2qm.autoplay.jobs add <工作> --profile N [--apply] [工作參數] [--no-wait | --wait 秒]
    python -X utf8 -m x7k2qm.autoplay.jobs <工作> --profile N ...          # 舊寫法，等同 add
    python -X utf8 -m x7k2qm.autoplay.jobs status --profile N
    python -X utf8 -m x7k2qm.autoplay.jobs cancel <id> --profile N
    python -X utf8 -m x7k2qm.autoplay.jobs scenario status|approve|resume|reset --profile N   # 劇本進度

- `--profile` 必填（避免沒寫就登到別台正在練功的那組，LESSONS #46）。
- **那組有活著的常駐練功**（看 `captures/owner/<組>.json`）：只把工作排進它的佇列，不登入；
  預設等結果印出（`--wait` 秒數上限，常駐不在了也不再等），`--no-wait` 排完就走。
- **沒有常駐**：先拿帳號擁有者鎖（別的程式在用就拒絕），自己登入執行，做完登出、放掉鎖。
- 結束碼：0＝ok；1＝failed／參數錯／被拒；3＝hold；4＝取消／過期／等不到結果。
- 日誌同時印在畫面與 `--log`（預設 captures/jobs.log）。
"""

import argparse
import sys
import time

from .. import jobs as _pkg
from .. import jobqueue, owner
from . import JobError, run_job

EXIT = {"ok": 0, "failed": 1, "hold": 3, "cancelled": 4, "expired": 4}
EXIT_WAIT = 4                 # 等不到結果（逾時或常駐不在了）
SUBCOMMANDS = ("add", "status", "cancel", "scenario")
WAIT_DEFAULT = 7200.0         # 等結果最多兩小時（任務可能跑很久）


def _common(sp) -> None:
    sp.add_argument("--profile", required=True, help="第幾組帳號（BOT_ACCOUNT_<組>）；必填")
    sp.add_argument("--jobs-dir", default=None, help="工作佇列目錄（預設 BOT_JOBS_DIR 或 captures/jobs）")
    sp.add_argument("--owner-dir", default=None, help="帳號擁有者鎖目錄（預設 BOT_OWNER_DIR 或 captures/owner）")


def build_parser(jobs) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.jobs")
    sub = ap.add_subparsers(dest="job", required=True)
    for name, mod in jobs.items():
        if name in SUBCOMMANDS:
            raise RuntimeError(f"工作名稱 {name} 跟子指令撞名")
        # argparse 把 help 當 % 格式字串：說明裡有「%」（mpitem「--per% 喝」）會讓 `jobs --help` 整個丟 ValueError
        sp = sub.add_parser(name, help=mod.HELP.replace("%", "%%"))
        _common(sp)
        sp.add_argument("--discover", action="store_true", help="角色清單從登入結果取（第 2 組預設寫死 5 隻）")
        sp.add_argument("--apply", action="store_true", help="真的執行；不加只列清單／狀態")
        sp.add_argument("--log", default="captures/jobs.log")
        sp.add_argument("--wait", type=float, default=WAIT_DEFAULT, help="排進常駐佇列後最多等結果幾秒")
        sp.add_argument("--no-wait", action="store_true", help="排進常駐佇列後不等結果")
        sp.add_argument("--api-port", type=int, default=None,
                        help="自己登入時開唯讀查詢 API（只聽 127.0.0.1；第 N 組慣例 8789+N），給狀態上傳拿寵物面板（11-11）")
        if hasattr(mod, "add_args"):
            mod.add_args(sp)
    return ap


def parse(argv, jobs=None):
    """常駐端用：佇列裡的 argv → (工作模組, args)。參數錯丟 JobError（不讓 argparse 結束整個常駐）。"""
    jobs = jobs if jobs is not None else _pkg.registry()
    try:
        args = build_parser(jobs).parse_args(list(argv))
    except SystemExit:
        raise JobError(f"參數不合法：{' '.join(argv)}")
    return jobs[args.job], args


def _dirs(args):
    return args.jobs_dir or jobqueue.default_dir(), args.owner_dir or owner.default_dir()


def cmd_status(argv, out=print, is_alive=None) -> int:
    is_alive = is_alive or owner.alive
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.jobs status")
    _common(ap)
    a = ap.parse_args(argv)
    jdir, odir = _dirs(a)
    own = owner.read(odir, a.profile)
    state = "活著" if own and is_alive(own.get("pid")) else "已不在（過期）" if own else ""
    out(f"第 {a.profile} 組帳號擁有者：{owner.describe(own)}{('，' + state) if state else ''}")
    q = jobqueue.Queue((own or {}).get("jobs_dir") or jdir, a.profile)
    for label, rows in (("做到一半", q.running()), ("排隊中", q.pending())):
        out(f"{label}：{len(rows)} 件")
        for j in rows:
            out(f"  {j['id']}  {' '.join(j.get('argv') or [j.get('name', '?')])}")
    done = q.recent(10)
    out(f"最近結果：{len(done)} 件")
    for r in done:
        out(f"  {r['id']}  {r.get('name')}  {r.get('status')}：{r.get('detail')}")
    return 0


def cmd_cancel(argv, out=print) -> int:
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.jobs cancel")
    ap.add_argument("id")
    _common(ap)
    a = ap.parse_args(argv)
    jdir, odir = _dirs(a)
    q = jobqueue.Queue((owner.read(odir, a.profile) or {}).get("jobs_dir") or jdir, a.profile)
    if q.cancel(a.id):
        out(f"已取消 {a.id}")
        return 0
    r = q.result(a.id)
    out(f"取消不了 {a.id}：" + (f"已經結束（{r.get('status')}）" if r else
                              "已經開始做了（只能取消還沒開始的）" if any(j["id"] == a.id for j in q.running())
                              else "找不到這個工作"))
    return 1


def main(argv=None, party_factory=None, is_alive=None, sleep=time.sleep) -> int:
    is_alive = is_alive or owner.alive
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["status"]:
        return cmd_status(argv[1:], is_alive=is_alive)
    if argv[:1] == ["cancel"]:
        return cmd_cancel(argv[1:])
    if argv[:1] == ["scenario"]:                 # 劇本進度（doc/autoplay/flows/scenario.md）；只讀寫進度檔，不登入
        from ..scenario import cli
        return cli(argv[1:])
    if argv[:1] == ["add"]:
        argv = argv[1:]
    jobs = _pkg.registry()
    args = build_parser(jobs).parse_args(argv)
    mod = jobs[args.job]
    from ..logrotate import RotatingFile
    fp = RotatingFile(args.log, max_bytes=5 * 1024 * 1024, keep=10)

    def log(msg: str) -> None:
        line = f"{time.strftime('%m-%d %H:%M:%S')} [{args.job}] {msg}"
        print(line, flush=True)
        fp.write(line + "\n")
    try:
        chk = getattr(mod, "check", None)
        if chk:
            chk(args)                       # 參數錯就不排隊、不登入
    except JobError as e:
        log(f"停止：{e}")
        return 1
    jdir, odir = _dirs(args)
    own = owner.live(odir, args.profile, is_alive)
    if own and own.get("kind") == owner.KIND_PARTY:
        return _enqueue(args, argv, own, jdir, odir, log, is_alive, sleep)
    if own and own.get("kind") == owner.KIND_JOBS and own.get("jobs_dir"):
        # 長跑的固定指令（petgoal --apply）也開了佇列：只收宣告 HOSTED_OK 的工作（trade-give／trade-take），
        # 其他工作（換寵、存倉…）會搶練寵的寵物（練寵中別換戰寵），一律不排
        if getattr(mod, "HOSTED_OK", False):
            return _enqueue(args, argv, own, jdir, odir, log, is_alive, sleep)
        log(f"停止：{owner.describe(own)} 正在用第 {args.profile} 組帳號，它的佇列只收 "
            f"{'／'.join(sorted(n for n, m in jobs.items() if getattr(m, 'HOSTED_OK', False)))}，{args.job} 不排、不登入")
        return 1
    if own:
        log(f"停止：{owner.describe(own)} 正在用第 {args.profile} 組帳號（不是常駐練功，沒有佇列），不登入")
        return 1
    if getattr(mod, "QUEUE_ONLY", False):
        log(f"停止：{args.job} 只能排進常駐練功的佇列，第 {args.profile} 組現在沒有常駐在跑")
        return 1
    host = hosts_queue(mod, args)
    try:
        owner.acquire(odir, args.profile, owner.KIND_JOBS, api_port=getattr(args, "api_port", None), log=log,
                      is_alive=is_alive, jobs_dir=jdir if host else None)
    except owner.OwnerBusy as e:
        log(f"停止：{e}")
        return 1
    try:
        return _run_here(mod, args, log, party_factory, jdir if host else None)
    finally:
        owner.release(odir, args.profile)


def default_party(mod, args, log):
    """沒有常駐、自己登入時的 Party。工作宣告 `SOLO = True`（單人活動，例 herobf）→ 只登一隻人物：
    預設帳號第一隻，`--role N` 指定第 N 隻（使用者 10-31「帳號 3 角色 2」）；
    `--roles 3,4,5` 同一個程式（同一個 pcId）登那幾隻，各自單人做（使用者 10-31「第 3、4、5 隻同時打」）。"""
    from ..headless_party import Party
    solo = getattr(mod, "SOLO", False)
    if callable(solo):                         # 跟 PAUSE 一樣可以看參數決定（lingshi：有 --role 才單人）
        solo = bool(solo(args))
    roles = getattr(args, "roles", None) if solo else None
    if roles:
        return Party(log, profile=args.profile, discover=args.discover, size=len(roles), picks=list(roles))
    return Party(log, profile=args.profile, discover=args.discover, size=1 if solo else 5,
                 pick=getattr(args, "role", None) if solo else None)


def hosts_queue(mod, args) -> bool:
    """長跑的工作（宣告 HOSTS_QUEUE，例 petgoal --apply）自己登入時也開工作佇列，練功迴圈在兩場戰鬥之間收
    HOSTED_OK 的工作（trade-give／trade-take；使用者 11-14「讓兩邊不同程式自己交易」）。"""
    h = getattr(mod, "HOSTS_QUEUE", False)
    return bool(h(args)) if callable(h) else bool(h)


def _run_here(mod, args, log, party_factory, jdir=None) -> int:
    p = party_factory(log) if party_factory is not None else default_party(mod, args, log)
    if jdir:
        p.jobq = jobqueue.Queue(jdir, args.profile)
        p.hosted_only = True
        log(f"工作佇列：{jdir}（只收 trade-give／trade-take，用 `jobs add` 排）")
    srv = None
    try:
        if getattr(args, "api_port", None):
            from .. import party_api
            try:
                srv = party_api.serve(p, args.api_port)          # 唯讀、只聽 127.0.0.1；開不起來只記日誌，工作照跑
                log(f"本機查詢 API：http://127.0.0.1:{args.api_port}/state")
            except OSError as e:
                log(f"本機查詢 API 開不起來（埠 {args.api_port}）：{e}；工作照跑")
        detail, _ = run_job(mod, args, p)
        log(detail)
        return 0
    except JobError as e:
        log(f"停止：{e}")
        return 1
    except Exception as e:  # noqa: BLE001
        log(f"停止：例外 {type(e).__name__}: {e}")
        return 1
    finally:
        if srv is not None:
            srv.shutdown()
        p.close()


def _enqueue(args, argv, own, jdir, odir, log, is_alive, sleep) -> int:
    q = jobqueue.Queue(own.get("jobs_dir") or jdir, args.profile)
    job = q.add(args.job, argv)
    log(f"第 {args.profile} 組有常駐練功（{owner.describe(own)}）：已排進佇列 {job['id']}，不另外登入")
    if args.no_wait:
        log(f"不等結果；之後用 `jobs status --profile {args.profile}` 看")
        return 0

    def still_there() -> bool:
        cur = owner.read(odir, args.profile)
        return bool(cur and cur.get("pid") == own.get("pid") and is_alive(cur.get("pid")))
    r = q.wait(job["id"], args.wait, still_there=still_there, sleep=sleep)
    if r is None:
        why = "常駐練功已經不在了" if not still_there() else f"等了 {args.wait:.0f} 秒"
        log(f"{why}，還沒有結果（工作 {job['id']}；用 `jobs status --profile {args.profile}` 看）")
        return EXIT_WAIT
    log(f"結果：{r.get('status')}：{r.get('detail')}")
    return EXIT.get(r.get("status"), 1)


if __name__ == "__main__":
    sys.exit(main())
