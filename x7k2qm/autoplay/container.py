"""容器進入點（使用者 2026-11-04「封裝成 docker，啟動時帶入帳密，自動登入練功」）。

    docker run -d -e GAME_ACCOUNT=… -e GAME_PASSWORD=… -v data:/data <映像>
    python -m x7k2qm.autoplay.container --check      # 只檢查設定與資料檔（CI 用，不登入）

環境變數（對外名稱，換成內部 BOT_*_1 再交給練功程式）：
  GAME_ACCOUNT          帳號（必填）
  GAME_PASSWORD         密碼（必填；或用 GAME_PASSWORD_FILE 指向 docker secret 檔）
  GAME_LOGIN_HOST       HTTP 登入伺服器（選填，預設 eologin.idealsa.org；使用者 11-04 指定）
  GAME_NODE             伺服器節點（選填，預設 game1）
  GAME_CLIENT_VER       客戶端版本（選填；遊戲改版時帶新版本號）
  CONFIG_FILE           設定檔路徑（預設：有掛載 /config/bot.toml 就用，沒有用映像內建 config/bot.toml）。
                        練功參數、劇本、規則表、外掛設定全部在設定檔（使用者 2026-11-05「能不能用掛載的」
                        「把外掛設定全部拉成配置檔」；格式見 config/bot.toml、botconfig.py）。
  AUTO_REBIRTH          1＝轉生前的同意關卡自動通過；其他值（預設 0）停在關卡只練功

以下環境變數**有設才**蓋過設定檔（舊的啟動方式照樣能用；沒設照設定檔）：
  START                 起始練功點（空字串＝留在原地）                       → bot.start
  SCENARIO              劇本檔路徑；none＝只練功、不解任務不轉生              → files.scenario
  AUTO_SPIRIT           1＝隊長買精靈金幣、60 級自動穿；其他值＝不買          → bot.auto_spirit
  NO_RETREAT            1＝不退回上一張圖；其他值＝照常退                     → bot.no_retreat
  POINT_PLAN            外掛自訂加點「隊長體,敏,攻/隊員體,敏,攻」；none＝不設 → waigua.point_plan
  BATTLE_HEAL           外掛戰鬥中技能補血門檻 %；none＝不設                  → waigua.battle_heal
  API_TOKEN             查詢 API 的 token（至少 16 字元；或 API_TOKEN_FILE）。有設才對外開 8790 埠，
                        沒設只能在容器裡查（docker exec … status_api）
  DATA_DIR              劇本進度、工作佇列、鎖、停止檔的目錄（預設 /data）

- 密碼只放在行程環境變數，不寫檔、不進日誌（`describe()` 只列有沒有設）。
- 練功程式崩潰或被強殺：照 keepalive 有限次重開（5、10、20 分鐘）；自己回報停止、被擠下線不重開（規範 2）。
- `docker stop`（SIGTERM）→ 寫停止檔，練功程式在兩場戰鬥之間登出；不在戰鬥中斷線。
- 日誌只印到標準輸出（`docker logs` 看），不寫日誌檔（使用者 11-04「log 統一吐在 console，本地不寫檔案」）。
  練功程式用 `--log -` 印到 stdout，這裡逐行轉印，同時留最後 TAIL_LINES 行給 keepalive 判斷結束原因。
"""

import argparse
import collections
import json
import os
import signal
import subprocess
import sys
import time
from typing import Callable, Dict, List, Mapping, TextIO, Tuple

from . import botconfig, keepalive
from ..client.http_login import LOGIN_HOST as DEFAULT_LOGIN_HOST

PROFILE = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_PORT = 8790
MIN_TOKEN = 16
TAIL_LINES = 2000                              # 每次執行只留最後這些行（判斷「停止：…」用），不會越跑越吃記憶體


class SetupError(ValueError):
    """設定不合法（理由給人看，不含密碼）。"""


def _secret(env: Mapping[str, str], name: str) -> str:
    v = env.get(name, "")
    path = env.get(name + "_FILE", "")
    if not v and path:
        try:
            with open(path, encoding="utf-8") as fp:
                v = fp.read().strip()
        except OSError as e:
            raise SetupError(f"{name}_FILE 讀不到：{type(e).__name__}") from None
    return v


def child_env(env: Mapping[str, str]) -> Dict[str, str]:
    """對外環境變數 → 練功程式用的環境變數。缺必填就丟 SetupError（什麼都還沒送）。"""
    account, password = env.get("GAME_ACCOUNT", "").strip(), _secret(env, "GAME_PASSWORD")
    host = env.get("GAME_LOGIN_HOST", "").strip() or DEFAULT_LOGIN_HOST
    missing = [n for n, v in (("GAME_ACCOUNT", account), ("GAME_PASSWORD", password)) if not v]
    if missing:
        raise SetupError(f"缺少環境變數 {', '.join(missing)}")
    data = env.get("DATA_DIR") or "/data"
    out = {k: v for k, v in env.items() if not k.startswith("BOT_") and k not in ("GAME_PASSWORD",)}
    p = f"_{PROFILE}"
    out.update({
        "BOT_ACCOUNT" + p: account, "BOT_PASSWORD" + p: password, "BOT_LOGIN_HOST" + p: host,
        "BOT_OWNER_DIR": os.path.join(data, "owner"), "BOT_JOBS_DIR": os.path.join(data, "jobs"),
        "BOT_SCENARIO_DIR": os.path.join(data, "scenario"),
        "BOT_ACCOUNTS_FILE": os.path.join(data, "accounts.json"),   # 不存在＝只用環境變數
    })
    for src, dst in (("GAME_NODE", "BOT_NODE" + p), ("GAME_CLIENT_VER", "BOT_CLIENT_VER" + p)):
        if env.get(src, "").strip():
            out[dst] = env[src].strip()
    if env.get("AUTO_REBIRTH", "0").strip() == "1":
        out["BOT_SCENARIO_AUTO_APPROVE"] = "1"
    token = _secret(env, "API_TOKEN")
    out.pop("API_TOKEN", None)
    if token:
        if len(token) < MIN_TOKEN:
            raise SetupError(f"API_TOKEN 至少 {MIN_TOKEN} 字元")
        out["BOT_API_TOKEN"] = token
    return out


def env_sets(env: Mapping[str, str]) -> List[str]:
    """**有設的**環境變數 → 設定檔覆寫（`--set`）。沒設的不給，照設定檔（預設值在 config/bot.toml）。"""
    q = json.dumps                                       # JSON 字串也是合法的 TOML 字串
    out = []
    if "START" in env:
        out.append(f"bot.start={q(env['START'].strip())}")
    if "SCENARIO" in env:
        sc = env["SCENARIO"].strip()
        if sc and sc.lower() not in ("builtin", "none") and not os.path.isabs(sc):
            sc = os.path.abspath(sc)           # 舊用法的相對路徑照工作目錄算（設定檔裡的才相對設定檔）
        out.append(f"files.scenario={q(sc or 'builtin')}")
    if "AUTO_SPIRIT" in env:
        out.append(f"bot.auto_spirit={'true' if env['AUTO_SPIRIT'].strip() == '1' else 'false'}")
    if "NO_RETREAT" in env:
        out.append(f"bot.no_retreat={'true' if env['NO_RETREAT'].strip() == '1' else 'false'}")
    if "POINT_PLAN" in env:
        plan = env["POINT_PLAN"].strip()
        out.append(f"waigua.point_plan={q(plan or 'none')}")
    if "BATTLE_HEAL" in env:
        heal = env["BATTLE_HEAL"].strip()
        if heal.lower() in ("", "0", "none", "false", "off"):
            heal = "false"
        elif not heal.isdigit():
            heal = q(heal)                     # 60%、50.5 這種：當字串交給設定檔驗證 → 登入前就報錯停止
        out.append(f"waigua.battle_heal={heal}")
    return out


def party_argv(env: Mapping[str, str], stop_file: str) -> List[str]:
    argv = ["--log", "-", "--stop-file", stop_file, "--profile", PROFILE, "--discover", "--api-port", str(API_PORT)]
    if _secret(env, "API_TOKEN"):             # 有 token 才對外聽（-p 8790:8790 連得到）；沒有只在容器內
        argv += ["--api-host", "0.0.0.0"]
    argv += ["--config", botconfig.find(env) or botconfig.BUILTIN]
    for s in env_sets(env):
        argv += ["--set", s]
    return argv


def describe(env: Mapping[str, str]) -> str:
    """啟動摘要（不印密碼、不印帳號全名）。"""
    acc = env.get("GAME_ACCOUNT", "")
    shown = (acc[:2] + "…") if acc else "（未設）"
    pw = "已設" if _secret(env, "GAME_PASSWORD") else "未設"
    auto = "自動同意" if env.get("AUTO_REBIRTH", "0").strip() == "1" else "停在關卡等人"
    return f"帳號 {shown}｜密碼 {pw}｜轉生 {auto}"


def run_tee(cmd: List[str], env: Mapping[str, str], cwd: str, out: TextIO = None) -> Tuple[int, str]:
    """跑練功程式：stdout／stderr 逐行轉印到 out（docker logs），回傳 (結束碼, 最後 TAIL_LINES 行)。"""
    out = out or sys.stdout
    tail: collections.deque = collections.deque(maxlen=TAIL_LINES)
    with subprocess.Popen(cmd, env=dict(env), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL) as proc:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").replace("\r\n", "\n")
            out.write(line)
            out.flush()
            tail.append(line)
        rc = proc.wait()
    return rc, "".join(tail)


def check(env: Mapping[str, str]) -> List[str]:
    """不登入的自我檢查：劇本能載入、工作都認得、地圖格子與設定表在。回傳問題清單（空＝通過）。"""
    problems = []
    from .headless_party import build_parser
    from .scenario import ScenarioError, load
    path = botconfig.find(env) or botconfig.BUILTIN
    try:
        ap = build_parser()
        cfg = botconfig.load(path, ap, env_sets(env))       # 也會解析掛載的規則表
        botconfig.check_overlap(cfg, cfg.bot.get("battle_heal", ap.get_default("battle_heal")), cfg.waigua.first_cmd)
    except botconfig.ConfigError as e:
        problems.append(f"設定檔 {path}：{e}")
        cfg = None
    if cfg is not None and cfg.scenario:
        try:
            load(cfg.scenario)
        except ScenarioError as e:
            problems.append(f"劇本：{e}")
    from .jobs import registry
    registry()
    for rel in (("data", "sproto_schema.json"), ("data", "configs", "Monster.json"), ("data", "mapgrids", "1000.json")):
        if not os.path.exists(os.path.join(ROOT, *rel)):
            problems.append(f"缺資料檔 {'/'.join(rel)}")
    return problems


def main(argv=None, environ: Mapping[str, str] = os.environ,
         spawn: Callable[..., Tuple[int, str]] = run_tee) -> int:
    ap = argparse.ArgumentParser(prog="container")
    ap.add_argument("--check", action="store_true", help="只檢查設定與資料檔，不登入")
    args = ap.parse_args(argv)
    if args.check:
        problems = check(environ)
        for p in problems:
            print(f"檢查失敗：{p}")
        print("檢查通過" if not problems else f"檢查失敗 {len(problems)} 項")
        return 1 if problems else 0
    try:
        env = child_env(environ)
    except SetupError as e:
        print(f"停止：{e}")
        return 2
    data = environ.get("DATA_DIR") or "/data"
    os.makedirs(data, exist_ok=True)
    stop_path = os.path.join(data, "party.stop")       # 練功程式看到就在兩場戰鬥之間登出
    keep_stop = os.path.join(data, "container.stop")   # 這裡看到就不再重開
    for p in (stop_path, keep_stop):          # 上次 docker stop 留下的停止檔：這次是使用者重新啟動
        if os.path.exists(p):
            os.remove(p)

    def log(msg: str) -> None:
        sys.stdout.write(f"{time.strftime('%m-%d %H:%M:%S')} [container] {msg}\n")
        sys.stdout.flush()

    def on_term(signum, frame) -> None:      # docker stop：讓練功在兩場戰鬥之間登出，不再重開
        for p in (stop_path, keep_stop):
            with open(p, "w", encoding="utf-8") as fp:
                fp.write("stop\n")
        log("收到停止訊號，等這場戰鬥打完登出")
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    log(f"啟動：{describe(environ)}")
    cmd = [sys.executable, "-X", "utf8", "-m", "x7k2qm.autoplay.headless_party", *party_argv(environ, stop_path)]

    def run_once() -> tuple:
        return spawn(cmd, env=env, cwd=data)
    why = keepalive.supervise(run_once, lambda: os.path.exists(keep_stop), log, log)
    log(f"結束：{why}")
    return 0 if why in (keepalive.DONE, keepalive.PLANNED, "stopped") else 1


if __name__ == "__main__":
    sys.exit(main())
