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

import json
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
