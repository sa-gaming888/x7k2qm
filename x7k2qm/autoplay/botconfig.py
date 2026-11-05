"""練功設定檔（使用者 2026-11-05「每次加配置都要改代碼不合理，能不能用掛載的」「把外掛設定全部拉成配置檔」）。

    python -X utf8 -m x7k2qm.autoplay.headless_party --config config/bot.toml ...
    容器：有掛載 /config/bot.toml 就用它（或環境變數 CONFIG_FILE），沒有就用映像內建的 config/bot.toml。

格式（TOML，範例與預設值見 config/bot.toml）：
- `[bot]`：練功程式參數＝`headless_party --help` 的長選項（`-` 換成 `_`）。命令列有給的照命令列。
  練功程式以後加新參數，直接寫進這裡就好，不用改容器。容器自己管的（log、profile、api_port…）不能寫。
- `[files]`：`scenario`（劇本檔路徑；"builtin"＝內建轉生劇本、"none"＝只練功）、
  `item_rules`（道具處置規則表 .md；"builtin"＝內建）。
- `[waigua]`：外掛設定，每次登入（含轉生後）檢查，跟伺服器現在的不一樣才送：
  hide（全部隱藏）、fast_battle（快速戰鬥）、first_cmd（"spirit"＝照身上精靈金幣、"none"＝不動）、
  auto_points（"recommend"＝沒設過的設推薦、"none"）、point_plan（"隊長體,敏,攻/隊員體,敏,攻" 或 "none"）、
  battle_heal（戰鬥中技能補血門檻 %，0／false＝不設）。
- `[waigua.raw.<協定名，去掉 WaiGua_>]`：直接寫外掛協定的欄位（欄位名照 data/sproto_schema.json；
  查詢 API `/waigua` 看得到現在的值）。例：`[waigua.raw.FastBattleSettingMpChange]` `MpRoleItemFlg = true`。
  鎖定戰寵／騎寵（fightPetIndex、ridePetIndex）由寵物流程管，不能寫。

讀檔、驗證都在登入之前；任何一項不合法就不登入（規範第 2 點）。
"""

import json
import os
import tomllib
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILTIN = os.path.join(ROOT, "config", "bot.toml")
SCHEMA = os.path.join(ROOT, "data", "sproto_schema.json")
RULES_ENV = "BOT_ITEM_RULES"          # item_rules.load() 沒給路徑時看這個

SECTIONS = {"bot", "files", "waigua"}
FILE_KEYS = {"scenario", "item_rules"}
# 容器／啟動程式自己決定的，設定檔不能改（改了會跟容器的停止檔、API、資料目錄對不上）
RESERVED = {"log", "stop_file", "profile", "discover", "api_port", "api_host", "jobs_dir", "owner_dir",
            "scenario_dir", "config", "scenario"}
WAIGUA_KEYS = {"hide", "fast_battle", "first_cmd", "auto_points", "point_plan", "battle_heal", "raw"}
RAW_PROTOS = ("FastBattleSettingChange", "FastBattleSettingHpChange", "FastBattleSettingMpChange",
              "FastBattleSettingMpSkillChange", "FastBattleSettingReviveSkillChange", "CatchPetSetting",
              "NomalSettingEatExpItem", "NomalSettingHandleHeal", "AutoEatOffLineItem", "NomalSettingHandlePet")
RAW_DENY = {"NomalSettingHandlePet": {"fightPetIndex", "ridePetIndex"}}


class ConfigError(ValueError):
    """設定檔不合法（理由給人看）。"""


@dataclass
class Waigua:
    hide: bool = True
    fast_battle: bool = True
    first_cmd: str = "spirit"
    auto_points: str = "recommend"
    raw: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # 協定全名 → 欄位


@dataclass
class Config:
    path: str
    bot: Dict[str, Any] = field(default_factory=dict)       # argparse dest → 值
    scenario: Optional[str] = None                          # None＝不跑劇本
    item_rules: Optional[str] = None                        # None＝內建
    waigua: Waigua = field(default_factory=Waigua)


def _request_fields() -> Dict[str, set]:
    with open(SCHEMA, encoding="utf-8") as fp:
        s = json.load(fp)
    types = s.get("types") or {}
    out = {}
    for name, v in (s.get("protocols") or {}).items():
        req = v.get("request") if isinstance(v, dict) else None
        t = types.get(req) if req else None
        fields = t.get("fields") if isinstance(t, dict) else t
        out[name] = {f.get("name") if isinstance(f, dict) else f for f in (fields or [])}
    return out


def _path(base: str, v: Any, what: str, allow_none: bool) -> Optional[str]:
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"[files] {what} 要是檔案路徑字串")
    v = v.strip()
    if v == "builtin":
        return "builtin"
    if allow_none and v.lower() == "none":
        return None
    p = v if os.path.isabs(v) else os.path.join(os.path.dirname(base), v)
    if not os.path.isfile(p):
        raise ConfigError(f"[files] {what} 找不到檔案：{p}")
    return p


def parse_set(item: str):
    """`--set 區塊.鍵=值`（沒寫區塊＝bot）。值照 TOML 解析（true、50、"字串"），解析不了就當字串。"""
    key, sep, raw = item.partition("=")
    key = key.strip()
    if not sep or not key:
        raise ConfigError(f"--set 格式要是 區塊.鍵=值：{item!r}")
    parts = key.split(".")
    if parts[0] not in SECTIONS:
        parts = ["bot"] + parts
    try:
        value = tomllib.loads(f"v = {raw.strip()}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw.strip()
    return parts, value


def apply_sets(data: dict, sets) -> dict:
    for item in sets or ():
        parts, value = parse_set(item)
        d = data
        for k in parts[:-1]:
            d = d.setdefault(k, {})
            if not isinstance(d, dict):
                raise ConfigError(f"--set {item}：{k} 不是區塊")
        d[parts[-1]] = value
    return data


def load(path: str, parser=None, sets=()) -> Config:
    """讀設定檔並驗證。parser＝headless_party 的 ArgumentParser（驗證 [bot] 的鍵與型別；不給就不驗）；
    sets＝`--set` 覆寫（容器把有設的環境變數轉成這個，蓋過設定檔）。"""
    try:
        with open(path, "rb") as fp:
            data = tomllib.load(fp)
    except OSError as e:
        raise ConfigError(f"設定檔讀不到：{path}（{type(e).__name__}）") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"設定檔格式錯誤：{path}：{e}") from None
    apply_sets(data, sets)
    bad = set(data) - SECTIONS
    if bad:
        raise ConfigError(f"不認得的區塊 {sorted(bad)}（只有 {sorted(SECTIONS)}）")
    cfg = Config(path=path)
    cfg.bot = _bot(data.get("bot") or {}, parser)

    files = data.get("files") or {}
    bad = set(files) - FILE_KEYS
    if bad:
        raise ConfigError(f"[files] 不認得的欄位 {sorted(bad)}")
    sc = _path(path, files.get("scenario", "builtin"), "scenario", allow_none=True)
    cfg.scenario = os.path.join(ROOT, "config", "scenarios", "rebirth.toml") if sc == "builtin" else sc
    rules = _path(path, files.get("item_rules", "builtin"), "item_rules", allow_none=False)
    cfg.item_rules = None if rules == "builtin" else rules

    w = data.get("waigua") or {}
    bad = set(w) - WAIGUA_KEYS
    if bad:
        raise ConfigError(f"[waigua] 不認得的欄位 {sorted(bad)}")
    wg = Waigua()
    for k in ("hide", "fast_battle"):
        if k in w:
            if not isinstance(w[k], bool):
                raise ConfigError(f"[waigua] {k} 要是 true／false")
            setattr(wg, k, w[k])
    for k, ok in (("first_cmd", ("spirit", "none")), ("auto_points", ("recommend", "none"))):
        if k in w:
            if w[k] not in ok:
                raise ConfigError(f"[waigua] {k} 只能是 {ok}：{w[k]!r}")
            setattr(wg, k, w[k])
    if "point_plan" in w:                                 # 跟 [bot] 的 --point-plan 同一個設定
        v = w["point_plan"]
        if not isinstance(v, str):
            raise ConfigError("[waigua] point_plan 要是字串（例 \"100,100,999/80,80,999\" 或 \"none\"）")
        _once(cfg.bot, "point_plan", None if v.strip().lower() == "none" else v.strip())
    if "battle_heal" in w:                                # 跟 [bot] 的 --battle-heal 同一個設定
        v = w["battle_heal"]
        if v is False or v == 0:
            _once(cfg.bot, "battle_heal", None)
        elif isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 99:
            _once(cfg.bot, "battle_heal", v)
        else:
            raise ConfigError(f"[waigua] battle_heal 要是 1～99 或 false：{v!r}")
    raw = w.get("raw") or {}
    if not isinstance(raw, dict):
        raise ConfigError("[waigua.raw] 要是表格")
    if raw:
        known = _request_fields()
        for proto, fields in raw.items():
            if proto not in RAW_PROTOS:
                raise ConfigError(f"[waigua.raw.{proto}] 不能設定（可以的：{', '.join(RAW_PROTOS)}）")
            if not isinstance(fields, dict) or not fields:
                raise ConfigError(f"[waigua.raw.{proto}] 要寫至少一個欄位")
            full = "WaiGua_" + proto
            unknown = set(fields) - known.get(full, set())
            if unknown:
                raise ConfigError(f"[waigua.raw.{proto}] 協定沒有這些欄位 {sorted(unknown)}"
                                  f"（有：{sorted(known.get(full, set()))}）")
            deny = set(fields) & RAW_DENY.get(proto, set())
            if deny:
                raise ConfigError(f"[waigua.raw.{proto}] {sorted(deny)} 由寵物流程管，不能寫在設定檔")
            wg.raw[full] = dict(fields)
    cfg.waigua = wg
    return cfg


def _once(bot: dict, dest: str, value) -> None:
    if dest in bot:
        raise ConfigError(f"{dest} 在 [bot] 和 [waigua] 都寫了，只能寫一個地方")
    bot[dest] = value


def _bot(section: dict, parser) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if parser is None:
        return dict(section)
    actions = {a.dest: a for a in parser._actions if a.option_strings}
    for key, v in section.items():
        dest = key.replace("-", "_")
        if dest in RESERVED:
            raise ConfigError(f"[bot] {key} 由容器／啟動程式決定，不能寫在設定檔"
                              + ("（劇本寫在 [files] scenario）" if dest == "scenario" else ""))
        a = actions.get(dest)
        if a is None:
            raise ConfigError(f"[bot] 練功程式沒有 --{dest.replace('_', '-')} 這個參數")
        if a.nargs == 0:                                  # store_true／store_false
            if not isinstance(v, bool):
                raise ConfigError(f"[bot] {key} 要是 true／false")
        elif a.type is int:
            if isinstance(v, bool) or not isinstance(v, int):
                raise ConfigError(f"[bot] {key} 要是整數")
        elif not isinstance(v, str):
            raise ConfigError(f"[bot] {key} 要是字串")
        out[dest] = v
    return out


def find(environ) -> Optional[str]:
    """容器用：CONFIG_FILE ＞ /config/bot.toml（有掛載）＞ 映像內建 config/bot.toml。"""
    p = (environ.get("CONFIG_FILE") or "").strip()
    if p:
        return p
    if os.path.isfile("/config/bot.toml"):
        return "/config/bot.toml"
    return BUILTIN if os.path.isfile(BUILTIN) else None
