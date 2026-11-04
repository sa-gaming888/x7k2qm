"""gamedb - 遊戲資料庫：從設定表（data/configs/）整理出寵物、道具/裝備、技能/精靈、戰鬥公式，
給之後的應用（攻擊推估、圖鑑、網頁）用。

  tables.py    設定表載入、語言表、共用工具
  pets.py      寵物（Monster、PetData、成長、屬性、轉生、騎乘…）
  items.py     道具與裝備（武器、防具、飾品、料理、套裝、合成…）
  skills.py    技能（寵物技能、人物技能、精靈）與狀態
  formulas.py  戰鬥/成長公式（從 HotUpdate.dll IL 逆向；推估用）
  build.py     統一輸出 data/gamedb/<名稱>.json（gitignore）

文件：doc/features/gamedb.md。CLI：python -m x7k2qm gamedb build。
"""
