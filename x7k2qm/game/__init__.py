"""game - 操作遊戲畫面（Windows 點擊）並用即時封包確認每個動作的結果。

分層：
  window.py  Win32 視窗點擊（客戶區座標）
  ui.py      各畫面共用的按鈕座標（1366x768 客戶區，實測）
  driver.py  GameDriver：點擊 → 等回應封包（act）、自動登出重登取得金鑰（relogin）

各分析主題（x7k2qm/features/*）只需要自己的畫面座標 + 流程，共用這一層。
"""
