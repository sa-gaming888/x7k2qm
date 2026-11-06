"""劇本執行器（使用者 2026-10-25「劇本同步」）：兩組帳號照同一份劇本各自往下跑、各自記進度。

    python -X utf8 -m x7k2qm.autoplay.headless_party --log captures/autoplay_headless.log --profile 2 \\
        --scenario config/scenarios/rebirth.toml
    python -X utf8 -m x7k2qm.autoplay.jobs scenario status|approve|resume|reset --profile 2

劇本（TOML，格式見 config/scenarios/rebirth.toml 開頭、doc/autoplay/flows/scenario.md）：
`[scenario]` name、loop；每個 `[[step]]` 一定有 name，且只能是下面四種之一：
- `until = "level>=need"`（或 `level>=131`）：條件不成立就照常練功；成立才往下。
- `job = "..."`／`jobs = ["...", ...]`：依序排進自己的工作佇列，一件 ok 才排下一件；任一失敗＝劇本停在這一步。
  可加 `skip_if = "done=群組,..."`（全隊都完成就跳過）、`pet_train = true`
  （quests 失敗訊息是「要寵物 P 練到 N 級」→ 自動排 train P:N 再重跑同一個 quests，同一步最多 3 次）；
  `pet_start = "練級點"` 改這一步自動練寵的地圖（預設 PET_START）。
- `gate = "approve"`：停下來等人 `jobs scenario approve --profile N`（只對目前這個 gate 有效）。

進度檔 `<dir>/<組>.json`（dir＝環境變數 `BOT_SCENARIO_DIR`，沒有才用 `captures/scenario`；測試一律暫存目錄）：
劇本名、目前第幾步、這一步做到第幾件工作、自動練寵次數、gate 同意、停止原因、每步結果與時間。
重開常駐從進度檔那一步接著做；做到一半的工作（常駐死掉時留在 running/）狀態不明 → 劇本停止等人看。

出錯就停（規範第 2 點）：劇本停止後**只做練功、不再往下**，直到人 `jobs scenario resume`。
不自動重試（pet_train 是使用者同意的例外，上限 3 次）。轉生（`--confirm-rebirth`）前一步一定要是 gate（載入時檢查）。
"""

import json
import os
import re
import shlex
import time
import tomllib
from typing import Callable, List, Optional, Tuple

PET_START = "海主人隧道Lv30"       # 自動練寵的起點（使用者：從海主人隧道跳圖）
PET_TRAIN_MAX = 3                  # 同一步、同一隻寵物自動練寵最多幾次（使用者同意的例外）
PET_TRAIN_STEP_MAX = 10
AUTO_RETRY_WAIT = 1800.0           # 工作失敗停住後幾秒自動重做那件（不用人 resume）
AUTO_RETRY_LOST = 60.0             # 工作做到一半常駐掛掉／工作不見了：幾秒後重排
AUTO_RETRY_MAX = 24                # 同一步最多自動重試幾次（約 12 小時），超過才真的停            # 同一步全部寵物加起來最多幾次（英雄島要練 1483、1481、1482 三隻）
HISTORY_MAX = 300                  # 進度檔保留幾筆結果
SCENARIO_KEYS = {"name", "loop"}
STEP_KEYS = {"name", "until", "job", "jobs", "gate", "pet_train", "pet_start", "skip_if"}
GATES = ("approve",)
FORBIDDEN_ARGS = ("--profile", "--jobs-dir", "--owner-dir", "--wait", "--no-wait")
FORBIDDEN_JOBS = ("resume",)       # 清 hold 是人的決定，不放進劇本
PET_NEED = re.compile(r"要寵物 (\d+) 練到 (\d+) 級")
STATES = {"run": "進行中", "gate": "等使用者同意", "stopped": "停止（等人處理）", "done": "全部做完"}


class ScenarioError(ValueError):
    """劇本格式錯、進度檔對不上、CLI 操作不合法（理由給人看）。"""


AUTO_APPROVE_ENV = "BOT_SCENARIO_AUTO_APPROVE"   # "1"＝gate 自動同意（容器用；使用者 2026-11-04「可以通過環境變數設定」）


def auto_approve(environ=None) -> bool:
    """只有明確設成 1 才自動同意；沒設、空字串、0、其他值一律停在 gate 等人。"""
    return (os.environ if environ is None else environ).get(AUTO_APPROVE_ENV, "").strip() == "1"


def default_dir() -> str:
    return os.environ.get("BOT_SCENARIO_DIR") or os.path.join("captures", "scenario")


# ---------------------------------------------------------------------------
# 劇本：讀檔與驗證
# ---------------------------------------------------------------------------

class Step:
    def __init__(self, i: int, name: str, kind: str, until=None, jobs=(), gate=None, pet_train=False,
                 skip_if=None, pet_start=None):
        self.i, self.name, self.kind = i, name, kind
        self.until = until            # ("level", "need" | int)
        self.jobs: List[List[str]] = [list(a) for a in jobs]   # 每件工作的 argv（不含 --profile）
        self.gate = gate
        self.pet_train = pet_train
        self.pet_start = pet_start or PET_START   # 自動練寵的地圖（使用者 11-05：金豬鎖定波拉島）
        self.skip_if = skip_if        # ("done", [群組…])


class Scenario:
    def __init__(self, name: str, loop: bool, steps: List[Step], path: str = ""):
        self.name, self.loop, self.steps, self.path = name, loop, steps, path


def parse_until(s) -> Tuple[str, object]:
    m = re.fullmatch(r"\s*level\s*>=\s*(need|\d+)\s*", str(s)) if isinstance(s, str) else None
    if not m:
        raise ScenarioError(f"until 只認得 level>=need 或 level>=數字：{s!r}")
    v = m.group(1)
    return ("level", v if v == "need" else int(v))


def parse_skip(s) -> Tuple[str, List[int]]:
    m = re.fullmatch(r"\s*done\s*=\s*(\d+(?:\s*,\s*\d+)*)\s*", str(s)) if isinstance(s, str) else None
    if not m:
        raise ScenarioError(f"skip_if 只認得 done=群組,群組…：{s!r}")
    return ("done", [int(x) for x in m.group(1).split(",")])


def check_job(text, where: str, registry=None) -> List[str]:
    """一件工作的字串 → argv；工作名稱、參數、check() 不合法就丟 ScenarioError（載入時就擋，不等跑到那步）。"""
    from .jobs import JobError
    from .jobs import __main__ as jobs_main
    if not isinstance(text, str) or not text.strip():
        raise ScenarioError(f"{where}：工作要是非空字串：{text!r}")
    argv = shlex.split(text)
    jobs = registry if registry is not None else _registry()
    if argv[0] not in jobs:
        raise ScenarioError(f"{where}：不認得的工作 {argv[0]}（有 {'、'.join(sorted(jobs))}）")
    if argv[0] in FORBIDDEN_JOBS:
        raise ScenarioError(f"{where}：{argv[0]} 不能放進劇本（要人決定）")
    bad = [a for a in argv if a.split("=", 1)[0] in FORBIDDEN_ARGS]
    if bad:
        raise ScenarioError(f"{where}：劇本裡不能寫 {bad}（--profile 由常駐自己帶）")
    try:
        mod, args = jobs_main.parse(argv + ["--profile", "0"], jobs)
        chk = getattr(mod, "check", None)
        if chk:
            chk(args)
    except JobError as e:
        raise ScenarioError(f"{where}：{e}")
    return argv


def _registry():
    from . import jobs
    return jobs.registry()


def parse(data: dict, path: str = "", registry=None) -> Scenario:
    if not isinstance(data, dict):
        raise ScenarioError("劇本格式錯誤")
    extra = set(data) - {"scenario", "step"}
    if extra:
        raise ScenarioError(f"劇本有不認得的區塊：{sorted(extra)}")
    head = data.get("scenario")
    if not isinstance(head, dict):
        raise ScenarioError("缺 [scenario] 區塊")
    extra = set(head) - SCENARIO_KEYS
    if extra:
        raise ScenarioError(f"[scenario] 有不認得的欄位：{sorted(extra)}")
    name = head.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScenarioError("[scenario] 要有 name")
    loop = head.get("loop", False)
    if not isinstance(loop, bool):
        raise ScenarioError("[scenario] loop 要是 true／false")
    raw = data.get("step")
    if not isinstance(raw, list) or not raw:
        raise ScenarioError("劇本沒有任何 [[step]]")
    jobs = registry if registry is not None else _registry()
    steps: List[Step] = []
    seen = set()
    for i, st in enumerate(raw):
        where = f"第 {i + 1} 步"
        if not isinstance(st, dict):
            raise ScenarioError(f"{where} 格式錯誤")
        extra = set(st) - STEP_KEYS
        if extra:
            raise ScenarioError(f"{where} 有不認得的欄位：{sorted(extra)}")
        sname = st.get("name")
        if not isinstance(sname, str) or not sname.strip():
            raise ScenarioError(f"{where} 要有 name")
        if sname in seen:
            raise ScenarioError(f"{where} 名稱「{sname}」重複（進度檔用名稱核對，不能重複）")
        seen.add(sname)
        where = f"{where}「{sname}」"
        kinds = [k for k in ("until", "job", "jobs", "gate") if k in st]
        if len(kinds) != 1:
            raise ScenarioError(f"{where}：until／job／jobs／gate 要剛好一個（現在 {kinds or '沒有'}）")
        kind = kinds[0]
        if kind in ("until", "gate") and ({"pet_train", "pet_start", "skip_if"} & set(st)):
            raise ScenarioError(f"{where}：{kind} 步驟不能加 pet_train／pet_start／skip_if")
        until = gate = None
        argvs: List[List[str]] = []
        if kind == "until":
            until = parse_until(st["until"])
        elif kind == "gate":
            if st["gate"] not in GATES:
                raise ScenarioError(f"{where}：gate 只認得 {GATES}：{st['gate']!r}")
            gate = st["gate"]
        else:
            texts = [st["job"]] if kind == "job" else st["jobs"]
            if not isinstance(texts, list) or not texts:
                raise ScenarioError(f"{where}：jobs 要是非空的字串陣列")
            argvs = [check_job(t, f"{where} 第 {k + 1} 件", jobs) for k, t in enumerate(texts)]
            kind = "jobs"
        pet = st.get("pet_train", False)
        if not isinstance(pet, bool):
            raise ScenarioError(f"{where}：pet_train 要是 true／false")
        if pet and not any(a[0] == "quests" for a in argvs):
            raise ScenarioError(f"{where}：pet_train 只用在有 quests 的步驟")
        pstart = st.get("pet_start")
        if pstart is not None:
            if not pet:
                raise ScenarioError(f"{where}：pet_start 要搭配 pet_train = true")
            from .supervisor import ROUTE
            if pstart not in {n for n, _, _ in ROUTE}:      # 同 jobs/train.check：練寵要在練功路線上
                raise ScenarioError(f"{where}：pet_start 不是練功路線上的地點：{pstart!r}")
        skip = parse_skip(st["skip_if"]) if "skip_if" in st else None
        steps.append(Step(i, sname, kind, until=until, jobs=argvs, gate=gate, pet_train=pet, skip_if=skip,
                          pet_start=pstart))
    # 轉生不可逆：--confirm-rebirth 一定要是那一步的第一件工作，而且前一步是 gate（不能預先同意、不能藏在中間）
    for s in steps:
        for k, a in enumerate(s.jobs):
            if "--confirm-rebirth" in a and (k != 0 or s.i == 0 or steps[s.i - 1].kind != "gate"):
                raise ScenarioError(f"第 {s.i + 1} 步「{s.name}」：轉生（--confirm-rebirth）前一步一定要是 gate，"
                                    f"而且要是那一步的第一件工作")
    return Scenario(name, loop, steps, path)


def load(path: str, registry=None) -> Scenario:
    try:
        with open(path, "rb") as fp:
            data = tomllib.load(fp)
    except OSError as e:
        raise ScenarioError(f"讀不到劇本 {path}：{e}")
    except tomllib.TOMLDecodeError as e:
        raise ScenarioError(f"劇本 {path} 不是合法的 TOML：{e}")
    return parse(data, path, registry)


# ---------------------------------------------------------------------------
# 進度檔
# ---------------------------------------------------------------------------

class Progress:
    def __init__(self, base: str, profile: str):
        self.path = os.path.join(base, f"{profile}.json")
        self.profile = str(profile)

    def read(self) -> Optional[dict]:
        try:
            with open(self.path, encoding="utf-8") as fp:
                d = json.load(fp)
            return d if isinstance(d, dict) else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise ScenarioError(f"進度檔 {self.path} 讀不了：{e}")

    def write(self, d: dict) -> None:
        d["updated"] = time.time()
        d["history"] = list(d.get("history") or [])[-HISTORY_MAX:]
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(d, fp, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, self.path)


def fresh(sc: Scenario) -> dict:
    return {"scenario": sc.name, "path": sc.path, "steps": len(sc.steps), "index": 0,
            "step_name": sc.steps[0].name, "sub": 0, "pet_tries": 0, "state": "run", "pending": None,
            "approved": None, "stop_reason": None, "round": 1, "history": []}


def describe(d: Optional[dict]) -> str:
    """一行：第 N/M 步 名稱、狀態。"""
    if not d:
        return "沒有劇本進度"
    s = f"劇本 {d.get('scenario')} 第 {d.get('index', 0) + 1}/{d.get('steps', '?')} 步「{d.get('step_name')}」、" \
        f"{STATES.get(d.get('state'), d.get('state'))}"
    if d.get("state") == "stopped" and d.get("stop_reason"):
        s += f"：{d['stop_reason']}"
    pend = d.get("pending")
    if pend:
        s += f"（等工作 {pend.get('id')}：{' '.join(pend.get('argv') or [])}）"
    return s


def rebirth_need(trans) -> Optional[int]:
    """本轉要轉生的等級門檻（跟 jobs/rebirth.py 同一份：2004～2007 要 131、2008 五轉要 140）；沒有下一轉回 None。"""
    from .jobs import JobError
    from .jobs import rebirth
    if trans is None:
        return None
    try:
        return rebirth.MIN_LEVEL.get(rebirth.group_for(int(trans)), rebirth.DEFAULT_LEVEL)
    except JobError:
        return None


# ---------------------------------------------------------------------------
# 常駐端：每輪（佇列空、沒有 hold）tick；工作做完 on_result
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, sc: Scenario, progress: Progress, queue, profile: str, log: Callable[[str], None],
                 environ=None):
        self.sc, self.pg, self.q, self.profile, self.log = sc, progress, queue, str(profile), log
        self.auto = auto_approve(environ)

    # ---- 進度 ----
    def start(self) -> dict:
        """常駐啟動時呼叫：沒有進度檔就從第 1 步開始；有就接著（劇本名對不上＝停止，不自己重設）。"""
        d = self.pg.read()
        if d is None:
            d = fresh(self.sc)
            self.pg.write(d)
            self.log(f"劇本：{self.sc.name} 開始（第 1/{len(self.sc.steps)} 步「{self.sc.steps[0].name}」）")
            return d
        self.log(f"劇本：從進度檔接續，{describe(d)}")
        return d

    def summary(self) -> Optional[dict]:
        try:
            d = self.pg.read()
        except ScenarioError:
            return None
        if not d:
            return None
        return {k: d.get(k) for k in ("scenario", "index", "steps", "step_name", "state", "sub", "pet_tries",
                                      "stop_reason", "round", "pending")} | {"text": describe(d)}

    def _read(self) -> Optional[dict]:
        d = self.pg.read()
        if d is None:
            d = fresh(self.sc)
        if d.get("state") == "stopped" and d.get("retry_at") and time.time() >= d["retry_at"]:
            # 使用者 11-06「要讓他們可以自動解任務順利」：可重做的失敗停一陣子後自己重做失敗的那件
            # （誘餌／石幣不夠、補血傳送被拒、逾時……），不用等人 resume
            d.update(state="run", stop_reason=None, pending=None, retry_at=None, pet_tries=0, pet_tries_by={})
            self._hist(d, "auto_retry", f"自動重試第 {d.get('auto_tries', 0)}/{AUTO_RETRY_MAX} 次")
            self.log(f"劇本：自動重試 {self._where(d)}（第 {d.get('auto_tries', 0)}/{AUTO_RETRY_MAX} 次）")
            self.pg.write(d)
        if d.get("state") in ("stopped", "done"):
            return None
        why = None
        if d.get("scenario") != self.sc.name:
            why = f"進度檔是劇本「{d.get('scenario')}」，現在載入的是「{self.sc.name}」"
        elif not 0 <= int(d.get("index", -1)) < len(self.sc.steps):
            why = f"進度檔第 {d.get('index')} 步不在劇本裡（共 {len(self.sc.steps)} 步）"
        elif self.sc.steps[d["index"]].name != d.get("step_name"):
            why = (f"劇本改過：進度檔第 {d['index'] + 1} 步是「{d.get('step_name')}」，劇本現在是"
                   f"「{self.sc.steps[d['index']].name}」（用 jobs scenario reset --step 指定從哪一步）")
        if why:
            self._stop(d, why)
            return None
        return d

    def _hist(self, d: dict, result: str, detail: str = "") -> None:
        st = self.sc.steps[d["index"]]
        d.setdefault("history", []).append({"round": d.get("round", 1), "step": d["index"] + 1, "name": st.name,
                                            "sub": d.get("sub", 0), "result": result, "detail": detail[:300],
                                            "ts": time.time()})

    def _stop(self, d: dict, why: str, retry: Optional[float] = None) -> None:
        """停劇本（之後只練功）。retry＝幾秒後自動重做失敗的那件；同一步自動重試超過 AUTO_RETRY_MAX 次就真的停。"""
        tries = d.get("auto_tries", 0)
        if retry is not None and tries < AUTO_RETRY_MAX:
            d.update(state="stopped", stop_reason=why[:500], pending=None, retry_at=time.time() + retry,
                     auto_tries=tries + 1)
            idx = d.get("index", 0)
            name = self.sc.steps[idx].name if 0 <= idx < len(self.sc.steps) else d.get("step_name")
            d.setdefault("history", []).append({"round": d.get("round", 1), "step": idx + 1, "name": name,
                                                "result": "retry_later", "detail": why[:300], "ts": time.time()})
            self.pg.write(d)
            self.log(f"劇本暫停：第 {idx + 1}/{len(self.sc.steps)} 步「{name}」：{why}"
                     f"（先練功，{int(retry // 60) or 1} 分鐘後自動重做；第 {tries + 1}/{AUTO_RETRY_MAX} 次）")
            return
        d.update(state="stopped", stop_reason=why[:500], pending=None, retry_at=None)
        idx = d.get("index", 0)
        name = self.sc.steps[idx].name if 0 <= idx < len(self.sc.steps) else d.get("step_name")
        d.setdefault("history", []).append({"round": d.get("round", 1), "step": idx + 1, "name": name,
                                            "result": "stopped", "detail": why[:300], "ts": time.time()})
        self.pg.write(d)
        self.log(f"劇本停止：第 {idx + 1}/{len(self.sc.steps)} 步「{name}」：{why}"
                 f"（之後只練功；處理完 `jobs scenario resume --profile {self.profile}`）")

    def _where(self, d: dict) -> str:
        return f"第 {d['index'] + 1}/{len(self.sc.steps)} 步「{self.sc.steps[d['index']].name}」"

    # ---- 工作 ----
    def _enqueue(self, d: dict, argv: List[str], kind: str) -> None:
        full = list(argv) + ["--profile", self.profile]
        job = self.q.add(argv[0], full)
        d["pending"] = {"id": job["id"], "kind": kind, "argv": full, "sub": d.get("sub", 0)}
        self.log(f"劇本排工作：{self._where(d)} {' '.join(full)}（{job['id']}）")

    def _advance(self, d: dict) -> bool:
        """往下一步；回 True＝這次 tick 到此為止（回到第 1 步或全部做完）。
        回到第 1 步不在同一次 tick 接著看：至少隔一輪練功，條件一直成立也不會原地空轉。"""
        d.update(index=d["index"] + 1, sub=0, pet_tries=0, pet_tries_by={}, approved=None, pending=None,
                 auto_tries=0, retry_at=None)
        if d["index"] >= len(self.sc.steps):
            if self.sc.loop:
                d.update(index=0, round=d.get("round", 1) + 1, step_name=self.sc.steps[0].name, state="run")
                self.log(f"劇本：{self.sc.name} 最後一步做完，回到第 1 步（第 {d['round']} 輪）")
            else:
                d.update(index=len(self.sc.steps) - 1, state="done")
                self.log(f"劇本：{self.sc.name} 全部做完")
            return True
        d["step_name"] = self.sc.steps[d["index"]].name
        d["state"] = "run"
        return False

    def _step(self, party, d: dict) -> None:
        """看目前這一步：條件成立／跳過／同意就往下（一次 tick 最多走完整份劇本一遍），要做工作就排第一件。"""
        for _ in range(len(self.sc.steps)):
            if d.get("state") != "run" and d.get("state") != "gate":
                return
            st = self.sc.steps[d["index"]]
            if st.kind == "until":
                if not until_ok(party, st.until):
                    return                                   # 照常練功
                self._hist(d, "ok", "條件成立")
                self.log(f"劇本：{self._where(d)} 條件成立")
                if self._advance(d):
                    return
                continue
            if st.kind == "gate":
                ap = d.get("approved")
                if isinstance(ap, dict) and ap.get("index") == d["index"] and ap.get("round") == d.get("round", 1):
                    self._hist(d, "approved", "使用者同意")
                    self.log(f"劇本：{self._where(d)} 使用者已同意，往下")
                    if self._advance(d):
                        return
                    continue
                if self.auto:
                    self._hist(d, "approved", f"自動同意（{AUTO_APPROVE_ENV}=1）")
                    self.log(f"劇本：{self._where(d)} 自動同意（{AUTO_APPROVE_ENV}=1），往下")
                    if self._advance(d):
                        return
                    continue
                if d.get("state") != "gate":
                    d["state"] = "gate"
                    self.log(f"劇本：{self._where(d)} 停下來等使用者同意"
                             f"（`jobs scenario approve --profile {self.profile}`；沒同意前只練功）")
                return
            if d.get("sub", 0) == 0 and st.skip_if and skip_ok(party, st.skip_if):
                self._hist(d, "skipped", f"全隊都完成 {st.skip_if[1]}")
                self.log(f"劇本：{self._where(d)} 全隊都完成 {st.skip_if[1]}，跳過")
                if self._advance(d):
                    return
                continue
            if d.get("sub", 0) >= len(st.jobs):
                # resume --skip 略過的是這一步最後一件：這一步算做完、往下（2026-10-26 第二組四轉後略過
                # 最後一步「轉生後規範」，舊版 st.jobs[sub] IndexError，劇本停在例外，LESSONS #91）
                self.log(f"劇本：{self._where(d)} 完成（最後一件已略過）")
                if self._advance(d):
                    return
                continue
            self._enqueue(d, st.jobs[d.get("sub", 0)], "step")
            return

    def tick(self, party) -> None:
        """主迴圈每輪呼叫（佇列空、沒有 hold、不在工作中）。不送任何封包，只排工作與寫進度。"""
        try:
            d = self._read()
            if d is None:
                return
            pend = d.get("pending")
            if pend:
                r = self.q.result(pend["id"])
                if r is not None:
                    self._handle(party, d, r.get("status"), r.get("detail") or "")
                elif any(j.get("id") == pend["id"] for j in self.q.running()) and getattr(party, "job", None) is None:
                    # 常駐重開（同一個 Pod，/data 還在）：劇本的工作都可重做（quests 接著進度、catch 先領倉庫、
                    # rebirth 有轉生前檢查），一分鐘後重排同一件，不要整個劇本停住等人
                    self._stop(d, f"工作 {pend['id']} 做到一半常駐就不在了，重做這件", retry=AUTO_RETRY_LOST)
                    return
                elif not any(j.get("id") == pend["id"] for j in self.q.pending() + self.q.running()):
                    self._stop(d, f"工作 {pend['id']} 不見了（不在佇列也沒有結果），重做這件", retry=AUTO_RETRY_LOST)
                    return
                else:
                    return                                   # 還在排隊
            else:
                self._step(party, d)
            self.pg.write(d)
        except ScenarioError as e:
            self.log(f"劇本停止：{e}")
        except Exception as e:  # noqa: BLE001  劇本程式出錯：停劇本、不影響練功
            self._crash(e)

    def on_result(self, party, job: dict, status: str, detail: str) -> None:
        """process_job 做完一件工作、回練功之前呼叫：是劇本排的就記結果、馬上排下一件（下一件接著做，不先回練功）。"""
        try:
            d = self._read()
            if d is None or not d.get("pending") or d["pending"].get("id") != job.get("id"):
                return
            self._handle(party, d, status, detail)
            self.pg.write(d)
        except ScenarioError as e:
            self.log(f"劇本停止：{e}")
        except Exception as e:  # noqa: BLE001
            self._crash(e)

    def _crash(self, e: Exception) -> None:
        try:
            d = self.pg.read() or fresh(self.sc)
            self._stop(d, f"劇本程式例外 {type(e).__name__}: {e}")
        except Exception as e2:  # noqa: BLE001
            self.log(f"劇本停止：劇本程式例外 {type(e).__name__}: {e}（進度檔也寫不了：{e2}）")

    def _handle(self, party, d: dict, status: str, detail: str) -> None:
        pend = d.get("pending") or {}
        d["pending"] = None
        st = self.sc.steps[d["index"]]
        argv = pend.get("argv") or []
        if pend.get("kind") == "pet_train":
            if status != "ok":
                self._stop(d, f"自動練寵 {' '.join(argv)} 沒成功（{status}）：{detail}",
                           retry=AUTO_RETRY_WAIT if status == "failed" else None)
                return
            self._hist(d, "pet_train_ok", " ".join(argv))
            self.log(f"劇本：{self._where(d)} 自動練寵完成，重跑 {' '.join(st.jobs[d.get('sub', 0)])}")
            self._enqueue(d, st.jobs[d.get("sub", 0)], "step")
            return
        if status == "ok":
            self._hist(d, "ok", detail)
            d["sub"] = d.get("sub", 0) + 1
            if d["sub"] < len(st.jobs):
                self._enqueue(d, st.jobs[d["sub"]], "step")
                return
            self.log(f"劇本：{self._where(d)} 完成")
            if not self._advance(d):
                self._step(party, d)                     # 下一步的第一件馬上排（接著做，不先回練功）
            return
        m = PET_NEED.search(detail or "")
        if status == "failed" and st.pet_train and argv[:1] == ["quests"] and m:
            # 每隻寵物各算次數：英雄島 2031 要練三隻不同的寵物，整步共用 3 次的話剛好用完、
            # 任何一隻要重練就停（11-06 第三組 1482 用到第 3/3 次）
            pet = f"{m.group(1)}:{m.group(2)}"
            by = d.setdefault("pet_tries_by", {})
            if by.get(pet, 0) >= PET_TRAIN_MAX or d.get("pet_tries", 0) >= PET_TRAIN_STEP_MAX:
                self._stop(d, f"自動練寵 {pet} 已經 {by.get(pet, 0)} 次（這一步共 {d.get('pet_tries', 0)} 次），"
                              f"quests 還是要練寵：{detail}", retry=AUTO_RETRY_WAIT)
                return
            by[pet] = by.get(pet, 0) + 1
            d["pet_tries"] = d.get("pet_tries", 0) + 1
            self._hist(d, "need_pet", detail)
            self.log(f"劇本：{self._where(d)} 要寵物 {m.group(1)} 練到 {m.group(2)} 級 → 自動練寵"
                     f"（這隻第 {by[pet]}/{PET_TRAIN_MAX} 次，這一步共 {d['pet_tries']} 次）")
            self._enqueue(d, ["train", "--pets", pet, "--start", st.pet_start], "pet_train")
            return
        # failed（JobError）是「沒做成、狀態清楚」：等一陣子自動重做（錢存夠、重置過了、隊伍好了就會過）。
        # hold（花了錢對不上、還在戰鬥中、程式例外）狀態不明，照舊停下來等人看
        self._stop(d, f"工作 {' '.join(argv)} {status}：{detail}",
                   retry=AUTO_RETRY_WAIT if status == "failed" else None)


def until_ok(party, cond) -> bool:
    """level>=need：全隊每隻都到（各自轉數的門檻）；等級或轉數不知道＝不成立。"""
    _, need = cond
    with party.lock:
        rows = [(party.s.levels.get(n), party.trans.get(n, (party.attrs.get(n) or {}).get("trans")))
                for _, n in party.party]
    if not rows:
        return False
    for level, trans in rows:
        want = rebirth_need(trans) if need == "need" else need
        if level is None or want is None or level < want:
            return False
    return True


def skip_ok(party, cond) -> bool:
    """done=群組…：全隊每隻都完成這些群組（finishTasks）。"""
    _, groups = cond
    with party.lock:
        fins = [set(party.finished.get(n) or (party.attrs.get(n) or {}).get("finishTasks") or [])
                for _, n in party.party]
    return bool(fins) and all(set(groups) <= f for f in fins)


# ---------------------------------------------------------------------------
# CLI：python -m x7k2qm.autoplay.jobs scenario status|approve|resume|reset --profile N
# ---------------------------------------------------------------------------

def cli(argv, out=print) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.jobs scenario")
    ap.add_argument("action", choices=("status", "approve", "resume", "reset"))
    ap.add_argument("--profile", required=True, help="第幾組帳號；必填")
    ap.add_argument("--scenario-dir", default=None, help="進度檔目錄（預設 BOT_SCENARIO_DIR 或 captures/scenario）")
    ap.add_argument("--skip", action="store_true", help="resume 用：失敗的那件工作當作人已經處理好，接著做下一件")
    ap.add_argument("--step", type=int, default=None, help="reset 用：從第幾步開始（1 起算；預設第 1 步）")
    ap.add_argument("--scenario", default=None, help="reset --step 用：劇本檔（預設進度檔記的路徑），拿來核對步驟名稱")
    a = ap.parse_args(list(argv))
    pg = Progress(a.scenario_dir or default_dir(), a.profile)
    try:
        d = pg.read()
        if a.action == "status":
            return _status(d, out)
        if d is None:
            out(f"第 {a.profile} 組沒有劇本進度（常駐加 --scenario 才會建立）")
            return 1
        msg = {"approve": _approve, "resume": _resume, "reset": _reset}[a.action](d, a)
    except ScenarioError as e:
        out(f"拒絕：{e}")
        return 1
    pg.write(d)
    out(msg)
    out(describe(d))
    return 0


def _status(d: Optional[dict], out) -> int:
    out(describe(d))
    if d:
        out(f"第 {d.get('round', 1)} 輪｜這一步第 {d.get('sub', 0) + 1} 件工作｜自動練寵 {d.get('pet_tries', 0)}/"
            f"{PET_TRAIN_MAX} 次｜劇本檔 {d.get('path') or '?'}")
        hist = (d.get("history") or [])[-8:]
        out(f"最近結果：{len(hist)} 筆")
        for h in hist:
            t = time.strftime("%m-%d %H:%M", time.localtime(h.get("ts") or 0))
            out(f"  {t} 第 {h.get('step')} 步「{h.get('name')}」 {h.get('result')}：{h.get('detail', '')}")
    return 0


def _approve(d: dict, a) -> str:
    if d.get("state") != "gate":
        raise ScenarioError(f"現在不是停在 gate（{describe(d)}）；不能預先同意後面的 gate")
    d["approved"] = {"index": d.get("index"), "round": d.get("round", 1), "ts": time.time()}
    return f"已同意第 {d.get('index', 0) + 1} 步「{d.get('step_name')}」（只對這個 gate 有效），常駐下一輪往下"


def _resume(d: dict, a) -> str:
    if d.get("state") != "stopped":
        raise ScenarioError(f"劇本沒有停止，不用 resume（{describe(d)}）")
    d.update(state="run", stop_reason=None, pending=None, pet_tries=0, pet_tries_by={}, retry_at=None, auto_tries=0)
    if a.skip:
        d["sub"] = d.get("sub", 0) + 1
        d.setdefault("history", []).append({"round": d.get("round", 1), "step": d.get("index", 0) + 1,
                                            "name": d.get("step_name"), "result": "skipped_by_user",
                                            "detail": "jobs scenario resume --skip", "ts": time.time()})
        return "已恢復：失敗的那件當作已處理，常駐下一輪接著做這一步的下一件（沒有下一件就往下一步）"
    return "已恢復：常駐下一輪重做這一步失敗的那件工作"


def _reset(d: dict, a) -> str:
    step = a.step or 1
    path = a.scenario or d.get("path")
    if not path:
        raise ScenarioError("不知道劇本檔在哪（加 --scenario）")
    sc = load(path)
    names = [s.name for s in sc.steps]
    if not 1 <= step <= len(names):
        raise ScenarioError(f"--step 要在 1～{len(names)}")
    d.update(scenario=sc.name, path=path, index=step - 1, sub=0, pet_tries=0, state="run", pending=None,
             approved=None, stop_reason=None, step_name=names[step - 1], steps=len(names))
    d.setdefault("history", []).append({"round": d.get("round", 1), "step": step, "name": d["step_name"],
                                        "result": "reset", "detail": "jobs scenario reset", "ts": time.time()})
    return f"已重設到第 {step} 步「{d['step_name']}」"
