# PGG Field App — 安裝與部署說明

## 我做了什麼

依據 `Field_Script_v4.docx`、`app_設計架構圖.docx`、`app_UI範例.pdf`，我做了一個離線、可安裝的網頁應用程式 (Progressive Web App, PWA)。

**檔案清單**：
- `index.html` — 主程式（包含所有邏輯與 UI，無須網路）
- `manifest.json` — 讓手機把它當成原生 App
- `sw.js` — Service Worker，第一次載入後就完全離線
- `icon-192.png`, `icon-512.png` — App 圖示

## 為什麼是 PWA 而不是 .apk / .ipa

要產生**真正能上架或側載的 native App**，必須在我這個 Linux 沙盒外完成下列步驟其中一條，這些都需要你的本機環境：

| 平台 | 需要的工具 | 我能做嗎 |
| --- | --- | --- |
| Android `.apk` | Android Studio + JDK + Gradle + 簽章金鑰 | ❌ 沙盒沒有 Android SDK；簽章需要你本人的金鑰 |
| iOS `.ipa` | macOS + Xcode + Apple Developer 帳號 ($99/年) | ❌ 完全無法在 Linux 上做 |

**PWA 的優點**：
1. **完全離線運作** — 載入一次後，飛航模式下也能跑（Service Worker 把所有檔案快取）
2. **資料存在手機本地** — 用 `localStorage`，斷網、關機、隔天再開都還在
3. **Android 與 iOS 都能「加到主畫面」** — 從主畫面打開，沒有網址列、沒有瀏覽器 UI，看起來和原生 App 幾乎一樣
4. **更新只要換檔案** — 不用過 App Store 審查

對 South Africa 田野實驗來說，PWA 反而是**最務實的方案**：7 支手機要可靠地離線、不能依賴 Play Store 審核延遲、資料能匯出 CSV。

---

## 部署方式（三選一）

### 方式 A：最快 — 用 GitHub Pages（推薦）

1. 把 `app/` 資料夾整包丟到 GitHub repo（例如 `pgg-field-app`）
2. Settings → Pages → Source 選 `main / root` → Save
3. 拿到網址 `https://你的帳號.github.io/pgg-field-app/`
4. **每支手機**用 Chrome (Android) 或 Safari (iOS) 打開該網址
5. 安裝到主畫面（見下方）

**重要**：PWA 必須透過 HTTPS 才能離線運作（GitHub Pages 自動有 HTTPS）。用本機檔案 (`file://`) 開啟 Service Worker 不會啟動。

### 方式 B：自架 — 用 Cloudflare Pages / Netlify / Vercel

把 `app/` 拖到 cloudflarepages.com 或 netlify.com 的部署視窗，免費，1 分鐘上線，也是 HTTPS。

### 方式 C：完全離線、不靠網路 — 區域網路 (LAN) 自架

田野現場可能根本沒網路。做法：
1. 帶一台筆電到田野，跑 `python3 -m http.server 8000`
2. 開手機 hotspot 或攜帶式路由器，讓筆電與 7 支手機連到同一個 Wi-Fi
3. 每支手機在 Chrome/Safari 連 `http://筆電IP:8000`
4. **第一次載入後**，手機上的 Service Worker 就會把所有資源快取，之後完全不需要筆電/網路

⚠ Service Worker 在 `http://` 本機 IP 下**只在某些瀏覽器**能跑（Chrome OK, Safari 嚴格要求 HTTPS）。所以 iOS 的話建議走方式 A 或 B。

---

## 在手機上安裝

### Android（Chrome）
1. 開啟網址
2. Chrome 右上角 ⋮ → **加到主畫面 (Add to Home screen)** → **安裝**
3. 從主畫面打開就是全螢幕 App

### iOS（Safari）
1. 開啟網址（**必須用 Safari**，Chrome on iOS 不支援 PWA 安裝）
2. 分享按鈕 ⤴️ → **加入主畫面 (Add to Home Screen)** → 加入
3. 從主畫面打開就是全螢幕 App

---

## 真的要 APK / IPA 嗎？

如果一定要拿到 `.apk` 檔案（例如 IT 要求、或想用 MDM 部署），有兩個方法都需要你本機操作：

### APK：用 PWABuilder 包 APK（最簡單）
1. 部署 PWA 到任何 HTTPS 網址（方式 A / B）
2. 去 https://www.pwabuilder.com
3. 貼網址，點 Build → Android → 下載 `.apk`
4. 你會拿到一個 Trusted Web Activity wrapper，本質上還是這個 PWA

### IPA：用 Capacitor
1. `npm init @capacitor/app` → 把 `index.html` 等放進 `www/` 資料夾
2. `npx cap add ios` → `npx cap open ios` → 在 Xcode build & sign
3. 需要 Mac + Xcode + 開發者帳號

---

## 功能 vs. 田野腳本對照

| Field Script v4 段落 | App 對應頁面 |
| --- | --- |
| Z3.1 設定（Village_no, Group colour, ID）| 第一次開啟的 Setup 頁 |
| A.2 / B.2 / C.2 / D.2 token contribution（4 位輪流貢獻）| Home → This Round → Session/Block → Call Member 1..4 → Pin → Contribute → Confirm |
| A.3 / B.3 group total announcement | Group Total 頁（顯示 (A1+A2+A3+A4) × 2）|
| 公式 A/B/C/D/E/F | This Round Result 頁（每位受試者個別查看）|
| 個人歷史紀錄 (Self-Record Table 鏡像) | 每位受試者的 History Record 頁 |
| Facilitator 端 4 位受試者的紀錄總覽 | Home → History Record → 任一受試者 → 任一 Session/Block |
| 資料匯出 | Home → Export to CSV |

## 設計細節

- **密碼**：第一次 Setup 設定的 4 位數密碼，全程所有受試者輸入時都是同一組（離線、無雜湊，純比對；研究情境足夠）
- **顏色**：Setup 時選 Red 即整支手機是紅色系；4 位受試者的紅各自有微差（C8514D / B0413E / D9665F / A8312E），讓受試者用顏色辨識而不是用 ID
- **離線資料**：所有歷史寫入 `localStorage`，斷電/關機/隔天再開都還在
- **進入 This Round 後**：依照設計圖，無法回首頁，必須跑完整個 10 round 流程的單一 round（即四人都貢獻 + 看完結果）才回得了首頁
- **匯出 CSV**：欄位 `village_number, village_name, group_colour, date, facilitator, participant_id, session, block, round, A, B, C, D, E, F`，可直接丟到 Stata/R 分析

## 下一步建議

1. **先在你自己手機上跑通整個流程**（兩個 Session × 兩個 Block 各 1-2 個 Round），確認 UI 沒問題
2. 跟 Syden 一起在實際手機上做一輪 pilot（建議至少 4 個 round）
3. 如果有要改的地方告訴我，我可以針對 `index.html` 做小幅修改
4. 田野前一晚：每支手機都先連網把 PWA 載完整、確認 Service Worker 已 install（DevTools → Application → Service Workers 看到 "activated"）
