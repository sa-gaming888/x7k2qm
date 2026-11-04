"""設定表載入（data/configs/，tools/dump_tabtoy.py 匯出、gitignore）與共用小工具。

各資料模組（pets、items、skills…）只透過 Tables 讀表，測試可以直接塞假表（Tables.from_dict）。
"""

import json
import os
from typing import Any, Dict, Iterable, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CONFIGS = os.path.join(ROOT, "data", "configs")


def _text(v) -> str:
    return "".join(v) if isinstance(v, list) else (v or "")


class MissingTable(LookupError):
    pass


class Tables:
    def __init__(self, configs: str = CONFIGS, data: Optional[Dict[str, List[dict]]] = None) -> None:
        self.configs = configs
        self._data: Dict[str, List[dict]] = dict(data or {})
        self._by_id: Dict[str, Dict[Any, dict]] = {}
        self._lang: Optional[Dict[int, str]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, List[dict]]) -> "Tables":
        """測試用：只用給定的表，不讀檔。"""
        t = cls(configs="", data=data)
        t._from_dict = True
        return t

    def has(self, table: str) -> bool:
        if table in self._data:
            return True
        if getattr(self, "_from_dict", False):
            return False
        return os.path.exists(os.path.join(self.configs, f"{table}.json"))

    def rows(self, table: str) -> List[dict]:
        """整張表（list of dict）。表不存在丟 MissingTable。"""
        if table not in self._data:
            if not self.has(table):
                raise MissingTable(table)
            with open(os.path.join(self.configs, f"{table}.json"), encoding="utf-8") as fp:
                self._data[table] = json.load(fp)
        return self._data[table]

    def by_id(self, table: str) -> Dict[Any, dict]:
        if table not in self._by_id:
            self._by_id[table] = {r["ID"]: r for r in self.rows(table) if "ID" in r}
        return self._by_id[table]

    def get(self, table: str, id_: Any) -> Optional[dict]:
        return self.by_id(table).get(id_)

    def lang(self, id_: Any) -> str:
        """語言表編號 → 繁體字串（NewLanguage.tcn、NewLanguagePublic.StrTn）；查不到回傳空字串。"""
        if self._lang is None:
            lang: Dict[int, str] = {}
            if self.has("NewLanguage"):
                lang.update({r["ID"]: _text(r.get("tcn")) for r in self.rows("NewLanguage")})
            if self.has("NewLanguagePublic"):
                lang.update({r["ID"]: _text(r.get("StrTn")) for r in self.rows("NewLanguagePublic")})
            self._lang = lang
        try:
            return self._lang.get(int(id_), "")
        except (TypeError, ValueError):
            return ""


def name_tn(row: Optional[dict]) -> str:
    """設定表列的繁體名稱（nameTn → nameCn）。"""
    if not row:
        return ""
    return row.get("nameTn") or row.get("nameCn") or ""


def ids(rows: Iterable[dict]) -> List[Any]:
    return [r["ID"] for r in rows]
