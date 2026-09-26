# 台股大盤過熱警示儀表板 — 自動更新版

每月 **26 日自動執行**爬蟲，將最新總經數據寫入 `docs/data.json`，  
並透過 **GitHub Pages** 提供公開網址，開啟即顯示最新資料。

---

## 📦 專案結構

```
tw-macro-monitor/
├── .github/
│   └── workflows/
│       └── update_data.yml   ← GitHub Actions 排程設定
├── scripts/
│   ├── fetch_data.py         ← 主爬蟲腳本
│   └── requirements.txt      ← Python 套件清單
├── docs/
│   ├── index.html            ← 儀表板網頁（讀取 data.json）
│   └── data.json             ← 爬蟲輸出（自動更新）
└── README.md
```

---

## 🚀 快速設定（10 分鐘完成）

### Step 1：Fork 或上傳到 GitHub

1. 在 GitHub 建立新的 **public** repository（名稱例如 `tw-macro-monitor`）
2. 將本專案所有檔案上傳到 repository

```bash
git init
git add .
git commit -m "初始設定"
git remote add origin https://github.com/你的帳號/tw-macro-monitor.git
git push -u origin main
```

### Step 2：申請免費 API Key（2 個，共約 5 分鐘）

#### FRED API Key（完全免費，必要）
用於 HY/IG 信用利差、VIX
1. 前往 https://fredaccount.stlouisfed.org/apikeys
2. 免費註冊帳號 → 點「Request API Key」
3. 填寫用途說明（例：personal research）→ 取得 32 碼 Key

#### FinMind Token（免費帳號即可，建議設定）
用於美國殖利率、融資餘額；景氣領先指標需 backer 帳號
1. 前往 https://finmindtrade.com/analysis/#/data/api
2. 免費註冊 → 登入 → 複製 Token

### Step 3：設定 GitHub Secrets

在 GitHub repository 頁面 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret 名稱 | 值 | 必要性 |
|---|---|---|
| `FRED_API_KEY` | FRED 32 碼 Key | 必要 |
| `FINMIND_TOKEN` | FinMind Token | 建議設定 |

### Step 4：啟用 GitHub Pages

1. GitHub repository → **Settings** → **Pages**
2. **Source**：Deploy from a branch
3. **Branch**：`main` → **Folder**：`/docs`
4. 點 **Save**

約 1-2 分鐘後，網址會顯示為：
```
https://你的帳號.github.io/tw-macro-monitor/
```

### Step 5：首次執行（產生初始 data.json）

**方法 A：手動觸發 GitHub Actions（推薦）**
1. GitHub repository → **Actions** → **每月自動更新總經資料**
2. 點右上角 **Run workflow** → **Run workflow**
3. 等待約 3-5 分鐘完成

**方法 B：在本機執行**
```bash
cd tw-macro-monitor
pip install -r scripts/requirements.txt

# 設定環境變數
export FRED_API_KEY="你的FRED_Key"
export FINMIND_TOKEN="你的FinMind_Token"

python scripts/fetch_data.py
# 執行後 docs/data.json 會自動產生

git add docs/data.json
git commit -m "初始資料"
git push
```

---

## ⏰ 自動執行時程

| 觸發時機 | 說明 |
|---|---|
| **每月 26 日 11:00（台灣時間）** | 主要排程（CBC M1B/M2 約 22-26 日公布） |
| **Push 到 main** | 修改爬蟲腳本或 workflow 時自動觸發 |
| **手動觸發** | Actions 頁面點「Run workflow」 |

---

## 📡 資料來源對照表

| 指標 | 來源 | API | 費用 | 公布時程 |
|---|---|---|---|---|
| M1B / M2 年增率 | 中央銀行 | 爬蟲 cbc.gov.tw | 免費 | 每月 22-26 日 |
| 外銷訂單動向指數 | 經濟部統計處 | 爬蟲 moea.gov.tw | 免費 | 每月 20-23 日 |
| 美國殖利率 | FinMind | GovernmentBondsYield | 免費 Token | 每日 |
| 融資餘額 | FinMind | TaiwanStockTotalMarginPurchaseShortSale | 免費 Token | 每日 |
| 景氣領先指標 | FinMind | TaiwanBusinessIndicator | **需 backer 帳號** | 每月 |
| HY/IG 信用利差 | FRED 聯準會 | BAMLH0A0HYM2 / BAMLC0A0CM | 免費 Key | 每日 |
| VIX | FRED 聯準會 | VIXCLS | 免費 Key | 每日 |
| 大盤成交量 | TWSE 證交所 | FMTQIK | 免費 | 每日 |
| 巴菲特指標 | TWSE + 主計總處 GDP | 爬蟲 + 計算 | 免費 | 季資料 |

---

## 🔧 備用值機制

當 API 爬取失敗時，程式自動退回 `scripts/fetch_data.py` 中的 `FALLBACK` 字典（近期已查證官方公開數值），確保儀表板不會出現空白。

若備用值也過期了，請更新 `FALLBACK` 字典中的數值。

---

## 📋 常見問題

**Q: GitHub Actions 失敗怎麼辦？**  
A: 到 Actions → 點失敗的 workflow → 查看 log 找原因。最常見原因是 Secret 名稱打錯，或是 API Key 過期。

**Q: 景氣領先指標一直顯示備用值？**  
A: FinMind `TaiwanBusinessIndicator` 需要付費的 backer 帳號。免費帳號只能用備用值。

**Q: 可以更改自動更新頻率嗎？**  
A: 修改 `.github/workflows/update_data.yml` 中的 `cron` 表達式即可。例如每月 20 日改為 `0 3 20 * *`。

**Q: 如何在本機測試爬蟲？**  
```bash
FRED_API_KEY="xxx" FINMIND_TOKEN="yyy" python scripts/fetch_data.py
```
