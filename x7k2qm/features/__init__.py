"""features - 各分析主題，一個主題一個子套件。

新增主題時照 shop/ 的分法：
  book.py     從解碼事件彙整資料（純邏輯，不碰畫面/網路，最好測）
  crawler.py  需要操作畫面時的流程（用 x7k2qm.game.GameDriver）
  fixture.py  從真實擷取匯出去識別化的回歸樣本
文件放 doc/features/<主題>.md、測試放 tests/features/<主題>/、CLI 掛在 x7k2qm/__main__.py。
"""
