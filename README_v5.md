# PGG Field App Test — v5 (修正版)

## 修正內容

上一版我誤判 `block` 欄位該存 'A'/'B' 字面值 — 抓錯了 bug。

正確設計（你跟我說明過的）：

| 欄位 | 編碼 | 意義 |
|------|------|------|
| `block` | 0 / 1 | 0 = R1-10 (no comm), 1 = R11-20 (with comm) |
| `treatment_comm` | 0 / 1 | 跟 `block` 完全一樣（同一變數兩個別名） |
| `treatment_chief` | 0 / 1 | 0 = Session 1, 1 = Session 2 |

`block` 跟 `treatment_comm` **數值一定相等是 by design**，方便 Stata 不同 model spec 直接用對應欄位名。

修正後測試結果：**51/51 全 PASS、0 bug** ✅

## 完整測試結果

| Layer | PASS | 內容 |
|-------|------|------|
| A | 24/24 | 計算正確（F=ceil(B+E)）、CSV 24 欄、treatment dummies、lag 變數、group stats、Z5 grand total |
| B | 16/16 | Resume 機制 — v3 critical bug 修好確認 (B1-B5 五個壓力點) |
| C | 5/5 | popstate / browser-back 攔截 |
| D | 6/6 | LLM 端對端跨 block 流程、decision_seconds 合理 |

## 用法

```powershell
pip install playwright anthropic
python -m playwright install chromium
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# 跑完整測試（推薦每次改 app 之後跑一次當 regression）
python pgg_bot_v5.py

# 只跑特定 layer (快速 debug)
python pgg_bot_v5.py --layers A
python pgg_bot_v5.py --layers B C

# Layer D 預設只跑 3 round/block。要跨 block 切換需要 ≥10
python pgg_bot_v5.py --rounds-per-block 10

# 不用 LLM (免費)
python pgg_bot_v5.py --no-llm

# 看 bot 在做什麼
python pgg_bot_v5.py --headed
```

輸出到 `C:\Users\yating\Desktop`：
- `pgg_v5_<時間>_results.csv` — 每個 check 一行
- `pgg_v5_<時間>_bugs.txt` — bug 列表
- `pgg_v5_<時間>_summary.txt` — 整體 summary
- `pgg_v5_<時間>_layer_*_csv.csv` — 各 layer 從 app 匯出的 CSV

## 為什麼 51/51 PASS 對你很重要

之前 v3 抓到的 critical bug（reload 清空資料、results phase 跳過顯示）— 我特別寫了 5 個壓力點重壓測試 (B1-B5)：

- **B1**：contribution phase 中 reload → 從中斷的成員接續，先前已輸入的不變
- **B2**：兩次 reload 連發 → m1, m2 都保留
- **B3**：results phase 中 reload (m1 已看完) → 從 m2 接續，不會重新讓 m1 再看一次
- **B4**：看完 m3 reload → 接到 m4 而非從 m1 重來
- **B5**：全看完 reload → 接到 callFinalTotal

**全部 PASS** 證實你的 `advanceToNextRound()` state machine 寫對了。

加上 popstate handler 真的擋住 Android 硬體返回鍵 (Layer C)、Z5 計算正確、F ceiling、decision_seconds 都對 — 這個 app 在你帶到 Gqeberha 之前的 QA 基本完成了。

## 後續用法

每次你修 app 或加新功能，跑一次 `python pgg_bot_v5.py` 就知道有沒有 regression。

如果有新欄位、新畫面，可以照 Layer A 的格式加 check：

```python
report.check("A", "我新加的欄位驗證",
             condition_to_check,
             "additional context")
```
