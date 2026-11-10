"""地圖編號 → 名稱（tg_watch、status、status_upload 共用）。

獨立一個小模組：status_upload 要進公開映像，從 tg_watch 拿會把 Telegram 監看和多開程式一起帶進去（11-11）。
"""

import json
import os


def place_name(mapid) -> str:
    """地圖編號 → 名稱。先查練功路線（有 Lv 門檻比較好認），再查設定表 MapName，都沒有就回編號。"""
    try:
        from .supervisor import ROUTE
        for name, m, _ in ROUTE:
            if str(m) == str(mapid):
                return name
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..features.gamedb.tables import CONFIGS
        with open(os.path.join(CONFIGS, "MapName.json"), encoding="utf-8") as fp:
            for r in json.load(fp):
                if str(r.get("ID")) == str(mapid):
                    return r.get("nameTn") or r.get("nameCn") or str(mapid)
    except Exception:  # noqa: BLE001
        pass
    return f"地圖 {mapid}"
