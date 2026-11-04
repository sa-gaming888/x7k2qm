"""地圖可走格與 A* 尋路（照客戶端 MapLogic 的規則；路線計算是唯讀的）。

可走格 = 地面 tile 的 MapOffset.walkable，加上建築佔地覆蓋，傳送點一律可走；NPC 站的格子與 MapName.banPos 不可走。
地圖檔：AssetBundles/mapdata_<id>.assetbundle（去掉前 331 bytes 是 UnityFS），MapChange 可把地圖 ID 換成檔案 ID。
"""

import heapq
import json
import os
from typing import Dict, List, Optional, Sequence, Tuple

from ..features.gamedb.tables import CONFIGS

AB_DIR = os.environ.get("BOT_ASSET_DIR", "")
Pos = Tuple[int, int]


class Grid:
    def __init__(self, w: int, h: int, walk: List[List[bool]], warps: Dict[Pos, Tuple[int, int, int]]) -> None:
        self.w, self.h, self.walk, self.warps = w, h, walk, warps

    def ok(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h and self.walk[x][y]


def _cfg(name: str, configs: str) -> list:
    with open(os.path.join(configs, f"{name}.json"), encoding="utf-8") as fp:
        return json.load(fp)


def parse_mapdata(data: bytes) -> Tuple[int, int, List[List[int]], List[List[int]]]:
    """mapdata TextAsset → (寬, 高, 地面 tile, 建築)。前 40 bytes 是檔頭；之後大端 u16 寬高與兩層 u16 陣列（列優先）。"""
    b = data[40:]
    w, h = int.from_bytes(b[0:2], "big"), int.from_bytes(b[2:4], "big")
    p = 4

    def layer(off: int) -> List[List[int]]:
        out = [[0] * h for _ in range(w)]
        for j in range(h):
            for i in range(w):
                k = off + (j * w + i) * 2
                out[i][j] = int.from_bytes(b[k:k + 2], "big")
        return out
    tile = layer(p)
    bld = layer(p + w * h * 2)
    return w, h, tile, bld


def build_grid(mapid: int, w: int, h: int, tile, bld, configs: str = CONFIGS) -> Grid:
    off = {r["ID"]: r for r in _cfg("MapOffset", configs)}
    warps = {(x["fX"], x["fY"]): (x["tMap"], x["tX"], x["tY"]) for x in _cfg("MapWarp", configs)
             if x["fMap"] == mapid and not x.get("isNotUse")}
    npc = {(n["x"], n["y"]) for n in _cfg("Npc", configs) if n.get("map") == mapid and n.get("visable")}
    mn = {r["ID"]: r for r in _cfg("MapName", configs)}
    ban = {(q["x"], q["y"]) for q in (mn.get(mapid, {}).get("banPos") or [])}

    def flag(base: bool, x: int, y: int) -> bool:
        f = base or (x, y) in warps
        return f and (x, y) not in npc and (x, y) not in ban

    walk = [[False] * h for _ in range(w)]
    for i in range(w):
        for j in range(h):
            o = off.get(tile[i][j])
            walk[i][j] = flag(o is not None and o["walkable"] != 0, i, j)
    for i in range(w):
        for j in range(h):
            n = bld[i][j]
            if n <= 99 or n in (14999, 15999):
                continue
            o = off.get(n)
            if o is None:
                continue
            for a in range(o["xland"]):
                for c in range(o["yland"]):
                    x, y = i + a, j - c
                    if 0 <= x < w and 0 <= y < h:
                        walk[x][y] = flag(o["walkable"] != 0, x, y)
    return Grid(w, h, walk, warps)


GRID_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "mapgrids")


def grid_to_dict(g: Grid) -> dict:
    """可走格子壓成 zlib＋base64（按 x 再 y 的順序一格一位元），傳送格照存。"""
    import base64
    import zlib
    bits = bytearray((g.w * g.h + 7) // 8)
    k = 0
    for x in range(g.w):
        for y in range(g.h):
            if g.walk[x][y]:
                bits[k >> 3] |= 1 << (k & 7)
            k += 1
    return {"w": g.w, "h": g.h, "walk": base64.b64encode(zlib.compress(bytes(bits), 9)).decode("ascii"),
            "warps": [[x, y, m, tx, ty] for (x, y), (m, tx, ty) in sorted(g.warps.items())]}


def grid_from_dict(d: dict) -> Grid:
    import base64
    import zlib
    bits = zlib.decompress(base64.b64decode(d["walk"]))
    w, h = d["w"], d["h"]
    walk = [[False] * h for _ in range(w)]
    k = 0
    for x in range(w):
        for y in range(h):
            walk[x][y] = bool(bits[k >> 3] >> (k & 7) & 1)
            k += 1
    return Grid(w, h, walk, {(x, y): (m, tx, ty) for x, y, m, tx, ty in d["warps"]})


def load_grid(mapid: int, ab_dir: str = AB_DIR, configs: str = CONFIGS, grid_dir: str = GRID_DIR) -> Grid:
    """有遊戲 AssetBundles 就從地圖檔算；沒有（例如 Mac）就讀 data/mapgrids/<地圖>.json（tools/export_mapgrids.py 匯出）。"""
    did = {r["ID"]: r["changeId"] for r in _cfg("MapChange", configs)}.get(mapid, mapid)
    if not os.path.exists(os.path.join(ab_dir, f"mapdata_{did}.assetbundle")):
        cached = os.path.join(grid_dir, f"{mapid}.json")
        if not os.path.exists(cached):
            raise FileNotFoundError(f"地圖 {mapid} 沒有地圖檔，也沒有 data/mapgrids/{mapid}.json")
        with open(cached, encoding="utf-8") as fp:
            return grid_from_dict(json.load(fp))
    return load_grid_from_bundle(mapid, ab_dir, configs)


def load_grid_from_bundle(mapid: int, ab_dir: str = AB_DIR, configs: str = CONFIGS) -> Grid:
    import UnityPy
    did = {r["ID"]: r["changeId"] for r in _cfg("MapChange", configs)}.get(mapid, mapid)
    with open(os.path.join(ab_dir, f"mapdata_{did}.assetbundle"), "rb") as fp:
        env = UnityPy.load(fp.read()[331:])
    data = None
    for o in env.objects:
        if o.type.name == "TextAsset":
            t = o.read()
            data = t.m_Script if isinstance(t.m_Script, bytes) else t.m_Script.encode("utf-8", "surrogateescape")
    if data is None:
        raise FileNotFoundError(f"mapdata_{did} 沒有 TextAsset")
    w, h, tile, bld = parse_mapdata(data)
    return build_grid(mapid, w, h, tile, bld, configs)


def astar(g: Grid, s: Pos, goal: Pos) -> Optional[List[Pos]]:
    """8 方向 A*；斜走時兩個側邊至少一格可走；不穿過起點以外的傳送點。"""
    openh = [(0.0, s)]
    came: Dict[Pos, Optional[Pos]] = {s: None}
    cost = {s: 0.0}
    while openh:
        _, cur = heapq.heappop(openh)
        if cur == goal:
            break
        if cur != s and cur in g.warps:
            continue
        x, y = cur
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if not g.ok(nx, ny) or (dx and dy and not (g.ok(x + dx, y) or g.ok(x, y + dy))):
                    continue
                nc = cost[cur] + (1.414 if dx and dy else 1.0)
                if (nx, ny) not in cost or nc < cost[(nx, ny)]:
                    cost[(nx, ny)] = nc
                    came[(nx, ny)] = cur
                    heapq.heappush(openh, (nc + max(abs(goal[0] - nx), abs(goal[1] - ny)), (nx, ny)))
    if goal not in came:
        return None
    path, c = [], goal
    while c is not None:
        path.append(c)
        c = came[c]
    return path[::-1]


def near(g: Grid, s: Pos, target: Pos, radius: int = 2) -> Optional[List[Pos]]:
    """走到 target 周圍 radius 格內最短的可走格（NPC 本身站的格子不可走）。"""
    best = None
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            q = (target[0] + dx, target[1] + dy)
            if not g.ok(*q):
                continue
            p = astar(g, s, q)
            if p and (best is None or len(p) < len(best)):
                best = p
    return best


def direction(a: Pos, b: Pos) -> int:
    """兩格之間的方向碼（0～7）。**推測**：照客戶端 Role_RoleWalk.dir 的編碼，實機待確認。"""
    dx, dy = (b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1])
    return {(0, -1): 0, (1, -1): 1, (1, 0): 2, (1, 1): 3, (0, 1): 4, (-1, 1): 5, (-1, 0): 6, (-1, -1): 7}.get((dx, dy), 0)


def walk_payload(mapid: int, path: Sequence[Pos]) -> dict:
    """Role_RoleWalk 的內容：整條路徑（不含起點）。"""
    steps = list(path[1:])
    return {"walkPath": [{"x": x, "y": y, "z": 0} for x, y in steps], "map": mapid,
            "dir": direction(path[-2], path[-1]) if len(path) >= 2 else 0}
