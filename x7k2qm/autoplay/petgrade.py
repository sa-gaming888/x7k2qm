"""寵物檔次（成長檔）判斷：檔差 −8～+8。只讀設定表，不碰遊戲。

方法照 參考專案 參考專案 的 psa-grow skill 與 `tools/query/growth.py`（使用者 2026-10-26「去參考 Dk wiki 怎麼判斷寵物檔次」）：
- 物種檔＝`Monster.json` 的 `growArg`（vitArg 體／atkArg 腕／defArg 耐／dexArg 速）。
- 個體檔＝伺服器寵物資料 `PetInfo.vitArg／atkArg／defArg／dexArg`＝物種檔＋每項偏差 −2～+2。
- 檔差＝四項偏差合計，−8～+8；**+8（四項全 +2，機率 1/625）＝滿檔**，其他照 參考專案 寫帶正負號的檔差（+5、+0、-3）。
  講總檔要附檔差：「總 116（-2）」。（2026-11-01 使用者糾正：舊寫法「-n＝8−檔差」把 +0 標成 -8，錯。）
- 例外：**轉生寵（trans ≥ 1）不跟 0 轉物種檔比**（轉生後檔另外算，參考專案 也只有 3 隻樣本）；
  **餵石寵物**（`Config.eatSpriteStonePet`，瑪蕾菲雅）四欄是餵石進度、不是成長檔；
  偏差超出 ±2 或資料缺欄 → 標「異常」「無資料」，不下結論。
"""

import itertools
import json
import math
import os
from functools import lru_cache
from typing import Dict, Optional, Tuple

ARGS = ("vitArg", "atkArg", "defArg", "dexArg")     # 體、腕、耐、速
FULL = 8                                            # 四項全 +2


@lru_cache(None)
def _tables() -> Tuple[Dict[int, Tuple[int, int, int, int]], frozenset]:
    from ..features.gamedb.tables import CONFIGS
    with open(os.path.join(CONFIGS, "Monster.json"), encoding="utf-8") as fp:
        rows = json.load(fp)
    species = {}
    for r in rows:
        g = r.get("growArg") or {}
        if r.get("ID") is not None and g:
            species[int(r["ID"])] = tuple(int(g.get(k) or 0) for k in ARGS)
    with open(os.path.join(CONFIGS, "Config.json"), encoding="utf-8") as fp:
        cfg = json.load(fp)
    cfg = cfg[0] if isinstance(cfg, list) else cfg
    stone = frozenset(int(x) for x in (cfg.get("eatSpriteStonePet") or []))
    return species, stone


def grade(pet: dict, species: Optional[Dict[int, Tuple[int, ...]]] = None,
          stone_pets: Optional[frozenset] = None) -> Tuple[str, Optional[int]]:
    """一隻寵物（PetInfo dict）→ (標籤, 檔差)。標籤：滿檔／-n／轉生寵／餵石寵／異常／無資料；
    檔差只有 0 轉、資料齊全時才有值（其他回 None）。"""
    if species is None or stone_pets is None:
        species, stone_pets = _tables()
    pid = pet.get("petId")
    if pid in stone_pets:
        return "餵石寵（四欄是餵石進度，不算檔）", None
    if (pet.get("trans") or 0) >= 1:
        return f"{pet.get('trans')} 轉寵（不跟 0 轉物種檔比）", None
    base = species.get(pid)
    got = [pet.get(k) for k in ARGS]
    if base is None or any(v is None for v in got) or not any(got):
        return "無資料", None
    offs = [int(g) - b for g, b in zip(got, base)]
    if any(abs(o) > 2 for o in offs):
        return f"異常（偏差 {offs}）", None
    diff = sum(offs)
    return ("滿檔" if diff >= FULL else f"{diff:+d}"), diff


# ---- Lv1 血量 → 檔差機率（抓 Lv1 野寵時只看得到等級和血量上限）----
# 公式照 參考專案 `tools/query/growth.py` 的 lv1_hp_table（使用者 2026-11-08「參考 dk wiki 怎麼從森林抓寵…是否需要做什麼血量判斷」）：
#   Lv1 血 = floor(initArg × (4體 + 腕 + 耐 + 速) / 100)，四項＝物種檔＋個體偏差（各 −2～+2、均勻）＋隨機點數
#   （LV1_POINTS 點，每點 1/4 落到其中一項）。參考專案 2026-10-25 夢幻森林 1,928 隻 Lv1 野怪實測：8 點全吻合、舊說的 10 點全不吻合。
# 所以血量只能算「這個血量是 +N 以上的機率」，不是確定的檔差；抓到後伺服器給個體檔才是準的（grade()）。
LV1_POINTS = 8
HP_WEIGHT = (4, 1, 1, 1)


@lru_cache(None)
def _grow(pid: int) -> Optional[Tuple[float, Tuple[int, int, int, int]]]:
    from ..features.gamedb.tables import CONFIGS
    with open(os.path.join(CONFIGS, "Monster.json"), encoding="utf-8") as fp:
        row = next((r for r in json.load(fp) if r.get("ID") == pid), None)
    g = (row or {}).get("growArg") or {}
    if not g.get("initArg"):
        return None
    return float(g["initArg"]), tuple(int(g.get(k) or 0) for k in ARGS)


@lru_cache(None)
def _points(n: int = LV1_POINTS) -> Tuple[Tuple[int, float], ...]:
    """隨機 n 點各 1/4 落到四項 → ((對血量的加權點數, 機率), …)。"""
    out: Dict[int, float] = {}
    for r in itertools.product(range(n + 1), repeat=3):
        if sum(r) > n:
            continue
        rv = r + (n - sum(r),)
        p = math.factorial(n) / math.prod(math.factorial(k) for k in rv) / 4 ** n
        k = sum(w * x for w, x in zip(HP_WEIGHT, rv))
        out[k] = out.get(k, 0.0) + p
    return tuple(sorted(out.items()))


@lru_cache(None)
def lv1_hp_table(pid: int, plus: int) -> Dict[int, float]:
    """物種 pid 的 Lv1 血量 → P(檔差 ≥ plus | 這個血量)。設定表沒有成長檔回空 dict。"""
    sp = _grow(pid)
    if sp is None:
        return {}
    init, args = sp
    base = sum(w * a for w, a in zip(HP_WEIGHT, args))
    tab: Dict[int, list] = {}
    for f in itertools.product(range(-2, 3), repeat=4):
        wf = sum(w * x for w, x in zip(HP_WEIGHT, f))
        good = sum(f) >= plus
        for k, q in _points():
            h = int(math.floor(init * (base + wf + k) / 100.0 + 1e-9))
            t = tab.setdefault(h, [0.0, 0.0])
            t[0] += q
            if good:
                t[1] += q
    return {h: (g / a if a else 0.0) for h, (a, g) in sorted(tab.items())}


# ---- 從目前四圍推估練到某級的評分（使用者 2026-11-08「黑暴練到 40 等，告訴我每一隻預估的分數」）----
# 照 參考專案 `tools/query/growth.py` forward：每升一級每項 raw 加 (個體檔 + 隨機 0～5) × 係數，係數依物種總檔分級
# （IL PetLogic.m_PetLevelUpTable，萬分比下限／上限）；畫面四圍 = 血 4體+腕+耐+速、攻 腕+0.1(體+耐)+0.05速、
# 防 耐+0.1(體+腕)+0.05速、敏 速；評分 = floor(血/4 + 攻 + 防 + 敏)（伺服器 PetInfo.score）。
LEVELUP_TABLE = ((100, 450, 500), (95, 470, 520), (90, 490, 540), (85, 510, 560), (80, 530, 580), (0, 550, 600))
SCORE_COEF = (1.2, 1.35, 1.35, 1.35)      # 評分對 raw 體／腕／耐／速 的係數（由上面四圍式子合併）
STAT_KEYS = ("hpMax", "physicalAttack", "physicalDefense", "agile")


def _rate(total: int) -> Tuple[float, float]:
    for th, lo, hi in LEVELUP_TABLE:
        if total >= th:
            return lo / 10000.0, hi / 10000.0
    return LEVELUP_TABLE[-1][1] / 10000.0, LEVELUP_TABLE[-1][2] / 10000.0


def _four(raw) -> Tuple[float, float, float, float]:
    v, a, d, x = raw
    return 4 * v + a + d + x, a + 0.1 * (v + d) + 0.05 * x, d + 0.1 * (v + a) + 0.05 * x, x


def _solve4(show) -> Tuple[float, ...]:
    """畫面四圍 → raw（_four 的反函數，高斯消去）。"""
    m = [[4, 1, 1, 1], [0.1, 1, 0.1, 0.05], [0.1, 0.1, 1, 0.05], [0, 0, 0, 1]]
    a = [row[:] + [float(b)] for row, b in zip(m, show)]
    for c in range(4):
        piv = max(range(c, 4), key=lambda r: abs(a[r][c]))
        a[c], a[piv] = a[piv], a[c]
        for r in range(4):
            if r != c:
                k = a[r][c] / a[c][c]
                a[r] = [x - k * y for x, y in zip(a[r], a[c])]
    return tuple(a[i][4] / a[i][i] for i in range(4))


def _score(raw) -> int:
    hp, atk, de, agi = (int(math.floor(v + 1e-9)) for v in _four(raw))
    return int(math.floor(hp / 4 + atk + de + agi + 1e-9))


def forecast_score(pet: dict, level: int = 140) -> Optional[dict]:
    """一隻 0 轉寵物（PetInfo：等級、血量上限、攻、防、敏、個體檔）練到 level 的評分：期望值與約 95% 範圍、最差／最好。
    資料不齊、轉生寵、已經超過 level → None。"""
    lv = pet.get("level")
    show = [pet.get(k) for k in STAT_KEYS]
    args = [pet.get(k) for k in ARGS]
    sp = _grow(int(pet.get("petId") or 0))
    if (sp is None or not isinstance(lv, int) or lv > level or (pet.get("trans") or 0) >= 1
            or any(not isinstance(v, int) for v in show + args)):
        return None
    lo, hi = _rate(sum(sp[1]))
    mid, n = (lo + hi) / 2, level - lv
    raw = _solve4(show)
    mean = [r + n * (a + 2.5) * mid for r, a in zip(raw, args)]
    worst = [r + n * a * lo for r, a in zip(raw, args)]
    best = [r + n * (a + 5) * hi for r, a in zip(raw, args)]
    # 每級每項變異：隨機 0～5 均勻（35/12）× 係數² ＋ 係數在上下限間均勻的部分（同 參考專案 grow_var）
    var = sum(c * c * n * ((35 / 12) * mid * mid + (a + 2.5) ** 2 * (hi - lo) ** 2 / 12)
              for c, a in zip(SCORE_COEF, args))
    exp = _score(mean)
    sd = math.sqrt(var)
    return {"level": level, "from": lv, "score": exp, "low": int(round(exp - 1.96 * sd)),
            "high": int(round(exp + 1.96 * sd)), "worst": _score(worst), "best": _score(best), "now": pet.get("score")}


def lv1_hp_min(pid: int, plus: int) -> Optional[int]:
    """要有機會是 +plus 以上，Lv1 血量最少多少（比這低的一定不到 +plus）。算不出來回 None。"""
    hits = [h for h, p in lv1_hp_table(pid, plus).items() if p > 0]
    return min(hits) if hits else None
