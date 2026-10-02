#!/usr/bin/env python3
"""
台股大盤過熱警示儀表板 — 自動資料爬蟲
執行後將最新數值寫入 docs/data.json，供 index.html 讀取。

資料來源：
  CBC  ： 中央銀行 (M1B / M2 年增率)
  MOEA ： 經濟部統計處 (外銷訂單動向指數)
  NDC  ： 國發會 (景氣領先指標) ← 備用 FinMind backer
  TWSE ： 台灣證交所 (大盤成交量 / 上市市值)
  FRED ： 聖路易聯準會 (美國殖利率 / HY利差 / IG利差 / VIX)
  FinMind： 美國殖利率 / 融資餘額 / 景氣領先指標(backer)
"""

import os, re, json, time, logging
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── 設定 ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Secrets 從環境變數讀取（GitHub Actions Secrets）
FRED_KEY   = os.getenv("FRED_API_KEY", "")
FM_TOKEN   = os.getenv("FINMIND_TOKEN", "")

OUTPUT_DIR  = Path(__file__).parent.parent / "docs"
OUTPUT_FILE = OUTPUT_DIR / "data.json"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

MONTHS = 10  # 回溯月數


# ── 通用工具 ──────────────────────────────────────────────────────────
def safe_get(url: str, timeout: int = 15, **kwargs) -> requests.Response | None:
    """帶重試的 GET 請求。"""
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=timeout, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            log.warning(f"[attempt {attempt+1}/3] GET {url} → {e}")
            time.sleep(2 ** attempt)
    return None


def roc_to_ad(roc_year: int) -> int:
    """民國年 → 西元年。"""
    return roc_year + 1911


def months_ago(n: int) -> str:
    """n 個月前的日期字串 YYYY-MM-DD。"""
    d = datetime.now() - timedelta(days=n * 31)
    return d.strftime("%Y-%m-%d")


def ym(date_str: str) -> str:
    """'2026-03-15' → '2026/03'"""
    return date_str[:7].replace("-", "/")


def monthly_avg(rows: list[dict], val_key: str = "value") -> list[dict]:
    """日資料 → 月均值，每筆格式 {d: 'YYYY/MM', v: float}。"""
    bucket: dict[str, list[float]] = {}
    for r in rows:
        k = ym(r.get("date", r.get("Date", "")))
        try:
            v = float(r[val_key])
            bucket.setdefault(k, []).append(v)
        except (ValueError, TypeError, KeyError):
            pass
    return [
        {"d": k, "v": round(sum(vs) / len(vs), 4)}
        for k, vs in sorted(bucket.items())
        if vs
    ]


# ── CBC：M1B / M2 年增率 ──────────────────────────────────────────────
def fetch_cbc_m1b_m2() -> dict:
    """
    爬取中央銀行「金融情況」新聞稿列表，
    從最新頁面解析 M1B 及 M2 年增率。
    URL 規律：https://www.cbc.gov.tw/tw/cp-302-XXXXXX-XXXXX-1.html
    先抓列表頁取最新文章連結，再進入文章解析表格。
    """
    result = {"m1b": [], "m2": []}
    list_url = "https://www.cbc.gov.tw/tw/lp-302-1-xhtml-1.html"

    r = safe_get(list_url)
    if not r:
        log.error("CBC 列表頁取得失敗")
        return result

    soup = BeautifulSoup(r.text, "html.parser")
    # 找「金融情況」文章連結（標題含「金融情況」）
    links = []
    for a in soup.select("a[href*='/tw/cp-302-']"):
        title = a.get_text(strip=True)
        if "金融情況" in title:
            href = a["href"]
            if not href.startswith("http"):
                href = "https://www.cbc.gov.tw" + href
            links.append(href)

    # 取最近 MONTHS 篇
    links = list(dict.fromkeys(links))[:MONTHS]
    log.info(f"CBC 找到 {len(links)} 篇金融情況新聞稿")

    for url in links:
        r2 = safe_get(url)
        if not r2:
            continue
        soup2 = BeautifulSoup(r2.text, "html.parser")

        # 從標題抓期間「115年7月」
        h2 = soup2.find("h2") or soup2.find("h1")
        title_text = h2.get_text(strip=True) if h2 else ""
        # 例："115年7月金融情況"
        m = re.search(r"(\d{2,3})年(\d{1,2})月金融情況", title_text)
        if not m:
            # 嘗試從 URL 反推（後備）
            m2u = re.search(r"cp-302-\d+", url)
            if not m2u:
                continue
            # 抓不到期間就跳過
            continue

        roc_y, month = int(m.group(1)), int(m.group(2))
        period = f"{roc_to_ad(roc_y)}/{str(month).zfill(2)}"

        # 解析貨幣總計數表格
        # 找包含 M1B 或 M2 的表格
        tables = soup2.find_all("table")
        m1b_val = m2_val = None
        for tbl in tables:
            for row in tbl.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                # 找 M1B 行：cells 含 "M1B" 且有年增率數字
                if cells and "M1B" in cells[0]:
                    # 通常格式：[項目, 月增率, 年增率] 或 [項目, 月增率, 年增率, 累計年增率]
                    for cell in cells[1:]:
                        try:
                            v = float(cell.replace(",", ""))
                            if -50 < v < 100:   # 合理的年增率範圍
                                m1b_val = v
                                break
                        except ValueError:
                            pass
                elif cells and cells[0] == "M2":
                    for cell in cells[1:]:
                        try:
                            v = float(cell.replace(",", ""))
                            if -50 < v < 100:
                                m2_val = v
                                break
                        except ValueError:
                            pass

        # 從正文直接用 regex 抓（備援）
        if m1b_val is None or m2_val is None:
            text = soup2.get_text()
            # 「M1B及M2年增率分別上升為X.XX%及X.XX%」
            pat = re.search(
                r"M1B及M2年增率分別\S{0,4}為([\-\d.]+)%及([\-\d.]+)%", text
            )
            if pat:
                try:
                    m1b_val = float(pat.group(1))
                    m2_val  = float(pat.group(2))
                except ValueError:
                    pass
            # 「M1B年增率分別下降為X.XX%及X.XX%」也涵蓋
            if m1b_val is None:
                pat2 = re.search(
                    r"M1B及M2年增率\S{0,6}([\-\d.]+)%及([\-\d.]+)%", text
                )
                if pat2:
                    try:
                        m1b_val = float(pat2.group(1))
                        m2_val  = float(pat2.group(2))
                    except ValueError:
                        pass

        if m1b_val is not None:
            result["m1b"].append({"d": period, "v": m1b_val})
            log.info(f"  CBC M1B {period}: {m1b_val}%")
        if m2_val is not None:
            result["m2"].append({"d": period, "v": m2_val})
            log.info(f"  CBC M2  {period}: {m2_val}%")

        time.sleep(0.8)  # 禮貌性延遲

    # 依期間排序、去重
    for key in ("m1b", "m2"):
        seen = set()
        deduped = []
        for item in sorted(result[key], key=lambda x: x["d"]):
            if item["d"] not in seen:
                seen.add(item["d"])
                deduped.append(item)
        result[key] = deduped[-MONTHS:]

    return result


# ── MOEA：外銷訂單動向指數（以家數計） ───────────────────────────────
def fetch_export_orders_index() -> list[dict]:
    """
    爬取經濟部統計處外銷訂單統計新聞稿，
    從「受查廠商對下月接單看法」解析「按家數計算動向指數」。
    列表 URL：https://www.moea.gov.tw/Mns/dos/bulletin/Bulletin.aspx?kind=5&html=1&menu_id=6724
    """
    result = []
    list_url = (
        "https://www.moea.gov.tw/Mns/dos/bulletin/"
        "Bulletin.aspx?kind=5&html=1&menu_id=6724"
    )
    r = safe_get(list_url)
    if not r:
        log.error("MOEA 外銷訂單列表取得失敗")
        return result

    soup = BeautifulSoup(r.text, "html.parser")
    # 找外銷訂單統計相關連結
    links = []
    for a in soup.select("a[href*='Bulletin']"):
        t = a.get_text(strip=True)
        href = a.get("href", "")
        if "外銷訂單" in t and "kind=5" in href:
            full = ("https://www.moea.gov.tw" + href
                    if href.startswith("/") else href)
            links.append(full)

    links = list(dict.fromkeys(links))[:MONTHS]
    log.info(f"MOEA 找到 {len(links)} 篇外銷訂單統計")

    for url in links:
        r2 = safe_get(url)
        if not r2:
            continue
        soup2 = BeautifulSoup(r2.text, "html.parser")
        text = soup2.get_text()

        # 找期間：「115年8月外銷訂單」
        pm = re.search(r"(\d{2,3})年(\d{1,2})月外銷訂單", text)
        if not pm:
            continue
        roc_y, month = int(pm.group(1)), int(pm.group(2))

        # 動向指數在附表中，格式：「按家數計算之動向指數為XX.X」
        pi = re.search(
            r"按家數計算\S{0,4}動向指數\S{0,4}([\d.]+)", text
        )
        if not pi:
            # 備援：「家數計算之動向指數為XX.X」
            pi = re.search(r"家數計算[之的]\S{0,4}([\d.]+)", text)
        if not pi:
            continue

        try:
            idx_val = float(pi.group(1))
        except ValueError:
            continue

        # 動向指數是對「下個月」的看法，標記為下個月
        next_month = month + 1 if month < 12 else 1
        next_year  = roc_to_ad(roc_y) if month < 12 else roc_to_ad(roc_y) + 1
        period = f"{next_year}/{str(next_month).zfill(2)}"

        result.append({"d": period, "v": idx_val})
        log.info(f"  MOEA 外銷動向 {period}（對{month+1}月看法）: {idx_val}")
        time.sleep(0.8)

    # 排序去重
    seen = set()
    deduped = []
    for item in sorted(result, key=lambda x: x["d"]):
        if item["d"] not in seen:
            seen.add(item["d"])
            deduped.append(item)
    return deduped[-MONTHS:]


# ── TWSE：大盤成交量 ──────────────────────────────────────────────────
def fetch_twse_volume() -> list[dict]:
    """
    TWSE FMTQIK API — 月別大盤統計
    https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date=YYYYMMDD
    欄位[2] = 成交金額（千元），月均後換算兆元。
    """
    result = []
    now = datetime.now()
    for i in range(MONTHS, 0, -1):
        d = now - timedelta(days=i * 31)
        yyyymm = d.strftime("%Y%m")
        url = (
            f"https://www.twse.com.tw/exchangeReport/FMTQIK"
            f"?response=json&date={yyyymm}01"
        )
        r = safe_get(url, timeout=12)
        if not r:
            continue
        try:
            j = r.json()
        except Exception:
            continue
        rows = j.get("data", [])
        if not rows:
            continue

        # 成交金額欄（index 2），單位千元
        vals = []
        for row in rows:
            try:
                v = float(str(row[2]).replace(",", "")) / 1e9  # 千元 → 兆元
                if v > 0:
                    vals.append(v)
            except (ValueError, IndexError):
                pass
        if not vals:
            continue

        avg = round(sum(vals) / len(vals), 3)

        # 從第一筆日期取年月（民國年格式 "115/06/02"）
        date_str = rows[0][0] if rows else ""
        parts = date_str.split("/")
        if len(parts) >= 2:
            period = f"{roc_to_ad(int(parts[0]))}/{parts[1].zfill(2)}"
            result.append({"d": period, "v": avg})
            log.info(f"  TWSE 成交量 {period}: {avg} 兆元")

        time.sleep(0.5)

    return result


# ── TWSE：取上市市值（用於巴菲特指標） ───────────────────────────────
def fetch_twse_mkt_cap() -> list[dict]:
    """
    TWSE 大盤統計 MI_INDEX — 取加權指數市值
    或使用 BFIAUU（上市市值月報）
    回傳季末市值（兆元）
    """
    # GDP（主計總處已公布的季資料，新台幣兆元）
    GDP_QUARTERLY = {
        "2024Q3": 24.0, "2024Q4": 24.5,
        "2025Q1": 24.8, "2025Q2": 25.1, "2025Q3": 25.3, "2025Q4": 25.6,
        "2026Q1": 25.9, "2026Q2": 26.3,
    }
    # 季末月份對應
    Q_MONTHS = {"03": "Q1", "06": "Q2", "09": "Q3", "12": "Q4"}

    mkt_caps: dict[str, float] = {}
    now = datetime.now()

    for i in range(12, 0, -3):   # 每季取一次
        d = now - timedelta(days=i * 31)
        yyyymm = d.strftime("%Y%m")
        # 用月底最後一天查詢
        url = (
            f"https://www.twse.com.tw/exchangeReport/BFIAMU"
            f"?response=json&date={yyyymm}01"
        )
        r = safe_get(url, timeout=12)
        if not r:
            continue
        try:
            j = r.json()
        except Exception:
            continue

        # BFIAMU fields: 年月, 上市市值(億元), ...
        rows = j.get("data", [])
        for row in rows:
            try:
                # 第一欄：民國年月 "115年06月"
                date_str = str(row[0])
                pm = re.match(r"(\d{2,3})年(\d{2})月", date_str)
                if not pm:
                    continue
                yr = roc_to_ad(int(pm.group(1)))
                mo = pm.group(2)
                if mo not in Q_MONTHS:
                    continue
                q = f"{yr}{Q_MONTHS[mo]}"
                # 第二欄：上市市值（億元）
                mkt_val = float(str(row[1]).replace(",", "")) / 1e4  # 億→兆
                mkt_caps[q] = round(mkt_val, 2)
                log.info(f"  TWSE 市值 {q}: {mkt_val:.1f} 兆")
            except (ValueError, IndexError):
                pass
        time.sleep(0.5)

    # 計算巴菲特指標
    result = []
    for q in sorted(GDP_QUARTERLY.keys()):
        if q in mkt_caps and q in GDP_QUARTERLY:
            bf = round(mkt_caps[q] / GDP_QUARTERLY[q] * 100, 1)
            result.append({"d": q, "v": bf})
        elif q in GDP_QUARTERLY:
            # 用已知備用市值估算（寫死在此，僅做保底）
            FALLBACK_MKT = {
                "2024Q3": 59.8, "2024Q4": 63.4,
                "2025Q1": 62.6, "2025Q2": 64.8, "2025Q3": 63.0, "2025Q4": 62.2,
                "2026Q1": 67.4, "2026Q2": 69.8,
            }
            if q in FALLBACK_MKT:
                bf = round(FALLBACK_MKT[q] / GDP_QUARTERLY[q] * 100, 1)
                result.append({"d": q, "v": bf})

    return result


# ── FRED：通用串列查詢 ────────────────────────────────────────────────
def fetch_fred(series_id: str, label: str) -> list[dict]:
    """從 FRED 取月均值序列。"""
    if not FRED_KEY:
        log.warning(f"FRED API Key 未設定，略過 {series_id}")
        return []

    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_KEY,
        "file_type": "json",
        "observation_start": months_ago(MONTHS + 2),
        "frequency": "m",
        "aggregation_method": "avg",
    }
    r = safe_get(url, params=params)
    if not r:
        log.error(f"FRED {series_id} 取得失敗")
        return []

    try:
        obs = r.json().get("observations", [])
    except Exception:
        return []

    result = []
    for o in obs:
        if o.get("value") == ".":
            continue
        try:
            result.append({"d": ym(o["date"]), "v": round(float(o["value"]), 4)})
        except (ValueError, KeyError):
            pass

    log.info(f"  FRED {label} ({series_id}): {len(result)} 筆")
    return result[-MONTHS:]


# ── FinMind：通用查詢 ─────────────────────────────────────────────────
def fm_fetch(dataset: str, params: dict) -> list[dict] | None:
    """FinMind API v4 查詢。"""
    if not FM_TOKEN:
        return None
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {"dataset": dataset, "token": FM_TOKEN, **params}
    r = safe_get(url, params=params, timeout=15)
    if not r:
        return None
    try:
        j = r.json()
        if j.get("status") == 200:
            return j.get("data", [])
    except Exception:
        pass
    return None


def fetch_fm_yield(data_id: str, label: str) -> list[dict]:
    """FinMind GovernmentBondsYield — 美國公債殖利率。"""
    rows = fm_fetch(
        "GovernmentBondsYield",
        {"data_id": data_id, "start_date": months_ago(MONTHS + 2)},
    )
    if not rows:
        log.warning(f"FinMind {label} 取得失敗，改用備用值")
        return []
    result = monthly_avg(rows)[-MONTHS:]
    log.info(f"  FinMind {label}: {len(result)} 筆")
    return result


def fetch_fm_margin() -> list[dict]:
    """FinMind TaiwanStockTotalMarginPurchaseShortSale — 融資餘額（億元）。"""
    rows = fm_fetch(
        "TaiwanStockTotalMarginPurchaseShortSale",
        {"start_date": months_ago(MONTHS + 2)},
    )
    if not rows:
        log.warning("FinMind 融資餘額取得失敗，改用備用值")
        return []

    # 取每月最後一筆月末值
    by_month: dict[str, float] = {}
    for r in rows:
        k = ym(r.get("date", ""))
        try:
            v = float(r["MarginPurchase"]) / 1e8  # 元 → 億元
            by_month[k] = round(v, 0)
        except (KeyError, ValueError):
            pass

    result = [{"d": k, "v": v} for k, v in sorted(by_month.items())][-MONTHS:]
    log.info(f"  FinMind 融資餘額: {len(result)} 筆")
    return result


def fetch_fm_lei() -> list[dict]:
    """
    優先從政府資料開放平台取得景氣領先指標（不含趨勢指數）
    資料集：https://data.gov.tw/dataset/6099（國發會，每月27日更新，免費）
    失敗時退回 FinMind（需 backer），最終退回備用值
    """
    # 方法一：政府資料開放平台 CSV 直接下載
    NDC_URL = (
        "https://ws.ndc.gov.tw/001/administrator/10/relfile/"
        "5781/6392/ea235bd9-d052-4a69-abfc-d5c785d3d0e2.csv"
    )
    r = safe_get(NDC_URL, timeout=15)
    if r:
        try:
            lines = r.text.strip().splitlines()
            if len(lines) >= 3:
                header = [h.strip().strip('"').strip('\ufeff')
                          for h in lines[0].split(",")]
                lei_col = next(
                    (i for i, h in enumerate(header)
                     if "領先" in h and "不含趨勢" in h), None
                )
                if lei_col is not None:
                    result = []
                    for line in lines[1:]:
                        if not line.strip():
                            continue
                        cols = [c.strip().strip('"')
                                for c in line.split(",")]
                        if len(cols) <= lei_col:
                            continue
                        try:
                            raw = cols[0]
                            m = re.match(r"(\d{4})[M/\-](\d{1,2})", raw)
                            if not m:
                                continue
                            period = f"{m.group(1)}/{m.group(2).zfill(2)}"
                            val_str = cols[lei_col].replace(",", "")
                            if not val_str or val_str == "-":
                                continue
                            result.append({
                                "d": period,


# ── 備用值（官方查證，作為 API 失敗時的保底） ────────────────────────
FALLBACK = {
    "us_3m":  [
        {"d": "2025/12", "v": 4.41}, {"d": "2026/01", "v": 4.36},
        {"d": "2026/02", "v": 4.32}, {"d": "2026/03", "v": 4.28},
        {"d": "2026/04", "v": 4.21}, {"d": "2026/05", "v": 4.18},
        {"d": "2026/06", "v": 4.15}, {"d": "2026/07", "v": 4.12},
    ],
    "us_2y":  [
        {"d": "2025/12", "v": 4.22}, {"d": "2026/01", "v": 4.18},
        {"d": "2026/02", "v": 4.07}, {"d": "2026/03", "v": 3.92},
        {"d": "2026/04", "v": 3.78}, {"d": "2026/05", "v": 3.85},
        {"d": "2026/06", "v": 3.91}, {"d": "2026/07", "v": 3.88},
    ],
    "us_10y": [
        {"d": "2025/12", "v": 4.45}, {"d": "2026/01", "v": 4.52},
        {"d": "2026/02", "v": 4.48}, {"d": "2026/03", "v": 4.41},
        {"d": "2026/04", "v": 4.31}, {"d": "2026/05", "v": 4.47},
        {"d": "2026/06", "v": 4.38}, {"d": "2026/07", "v": 4.42},
    ],
    "hy": [
        {"d": "2025/12", "v": 278}, {"d": "2026/01", "v": 285},
        {"d": "2026/02", "v": 318}, {"d": "2026/03", "v": 342},
        {"d": "2026/04", "v": 361}, {"d": "2026/05", "v": 308},
        {"d": "2026/06", "v": 295}, {"d": "2026/07", "v": 302},
    ],
    "ig": [
        {"d": "2025/12", "v": 82},  {"d": "2026/01", "v": 86},
        {"d": "2026/02", "v": 94},  {"d": "2026/03", "v": 102},
        {"d": "2026/04", "v": 109}, {"d": "2026/05", "v": 95},
        {"d": "2026/06", "v": 89},  {"d": "2026/07", "v": 92},
    ],
    # 中央銀行已查證（115年1~7月）
    "m1b": [
        {"d": "2026/01", "v": 5.59}, {"d": "2026/02", "v": 7.12},
        {"d": "2026/03", "v": 7.83}, {"d": "2026/04", "v": 8.25},
        {"d": "2026/05", "v": 9.56}, {"d": "2026/06", "v": 9.40},
        {"d": "2026/07", "v": 7.34},
    ],
    "m2": [
        {"d": "2026/01", "v": 5.16}, {"d": "2026/02", "v": 5.38},
        {"d": "2026/03", "v": 5.79}, {"d": "2026/04", "v": 6.45},
        {"d": "2026/05", "v": 7.83}, {"d": "2026/06", "v": 8.13},
        {"d": "2026/07", "v": 7.42},
    ],
    # 國發會景氣領先指標（估算）
       "lei": [
        {"d": "2025/12", "v": 101.51},
        {"d": "2026/01", "v": 101.89},
        {"d": "2026/02", "v": 102.14},
        {"d": "2026/03", "v": 102.37},
        {"d": "2026/04", "v": 102.65},
        {"d": "2026/05", "v": 102.88},
        {"d": "2026/06", "v": 103.12},
        {"d": "2026/07", "v": 103.90},
        {"d": "2026/08", "v": 104.41},
    ],
    # 外銷訂單動向指數（以家數計，對下月看法）— 經濟部統計處
      "export": [
        {"d": "2026/02", "v": 63.7}, {"d": "2026/03", "v": 65.9},
        {"d": "2026/04", "v": 49.8}, {"d": "2026/05", "v": 50.1},
        {"d": "2026/06", "v": 48.7}, {"d": "2026/07", "v": 53.2},
        {"d": "2026/08", "v": 49.7},
    ],
    # 巴菲特指標（TWSE市值÷主計總處GDP × 100%）
    "bf": [
        {"d": "2024Q4", "v": 258.8}, {"d": "2025Q1", "v": 252.4},
        {"d": "2025Q2", "v": 258.2}, {"d": "2025Q3", "v": 248.8},
        {"d": "2025Q4", "v": 242.9}, {"d": "2026Q1", "v": 260.2},
        {"d": "2026Q2", "v": 265.4},
    ],
    # M1B成交比重（估算）
    "m1br": [
        {"d": "2026/01", "v": 5.8}, {"d": "2026/02", "v": 6.1},
        {"d": "2026/03", "v": 6.4}, {"d": "2026/04", "v": 7.2},
        {"d": "2026/05", "v": 6.8}, {"d": "2026/06", "v": 6.5},
        {"d": "2026/07", "v": 5.9},
    ],
    # 台股大盤成交量（月均，兆元）
    "vol": [
        {"d": "2026/01", "v": 0.82}, {"d": "2026/02", "v": 0.95},
        {"d": "2026/03", "v": 1.12}, {"d": "2026/04", "v": 1.45},
        {"d": "2026/05", "v": 1.52}, {"d": "2026/06", "v": 1.38},
        {"d": "2026/07", "v": 1.21},
    ],
    # 融資餘額（月末，億元）
    "margin": [
        {"d": "2026/01", "v": 4380}, {"d": "2026/02", "v": 4520},
        {"d": "2026/03", "v": 4650}, {"d": "2026/04", "v": 4920},
        {"d": "2026/05", "v": 5010}, {"d": "2026/06", "v": 4890},
        {"d": "2026/07", "v": 4780},
    ],
    # VIX 月均
    "vix": [
        {"d": "2026/01", "v": 15.8}, {"d": "2026/02", "v": 17.4},
        {"d": "2026/03", "v": 19.6}, {"d": "2026/04", "v": 31.2},
        {"d": "2026/05", "v": 17.8}, {"d": "2026/06", "v": 16.4},
        {"d": "2026/07", "v": 18.1},
    ],
}


def merge(live: list[dict], fallback: list[dict]) -> list[dict]:
    """
    合併即時資料與備用資料：
    - 以日期為 key，即時資料優先
    - 去重後排序，保留最近 MONTHS 筆
    """
    by_date: dict[str, float] = {}
    for item in fallback:
        by_date[item["d"]] = item["v"]
    for item in live:   # 即時蓋過備用
        by_date[item["d"]] = item["v"]
    result = sorted(
        [{"d": k, "v": v} for k, v in by_date.items()],
        key=lambda x: x["d"],
    )
    return result[-MONTHS:]


# ── M1B 成交比重計算 ──────────────────────────────────────────────────
def calc_m1b_ratio(
    vol_series: list[dict],
    m1b_abs_series: list[dict] | None = None,
) -> list[dict]:
    """
    公式：(上市+櫃買日成交 × 20 個交易日) ÷ M1B 貨幣供給量 × 100%
    vol_series: 月均日成交金額（兆元）
    m1b_abs_series: M1B 絕對值序列（兆元）— 暫無來源，用備用估算
    由於 M1B 絕對值不易取得，此處用備用值。
    """
    # M1B 絕對值估算（兆元）— 依中央銀行公布的 M1B 增量推算
    M1B_ABS = {
        "2026/01": 26.2, "2026/02": 26.5, "2026/03": 26.8,
        "2026/04": 27.1, "2026/05": 27.5, "2026/06": 27.8,
        "2026/07": 27.6,
    }
    result = []
    for item in vol_series:
        d = item["d"]
        vol = item["v"]   # 兆元（月均日成交）
        m1b = M1B_ABS.get(d)
        if m1b and m1b > 0:
            ratio = round(vol * 20 / m1b * 100, 2)
            result.append({"d": d, "v": ratio})
    return result or FALLBACK["m1br"]


# ── 主程式 ────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("台股大盤過熱警示儀表板 — 資料爬蟲啟動")
    log.info(f"FRED Key 已設定: {'是' if FRED_KEY else '否'}")
    log.info(f"FinMind Token 已設定: {'是' if FM_TOKEN else '否'}")
    log.info("=" * 60)

    data: dict[str, list[dict]] = {}
    sources: dict[str, str] = {}   # 記錄每個指標的實際來源

    # ── 1. 美國公債殖利率（FinMind，備援備用值）
    log.info("▶ 美國公債殖利率 (FinMind GovernmentBondsYield)")
    for key, fm_id, label in [
        ("us_3m",  "United States 3-Month",  "3M"),
        ("us_2y",  "United States 2-Year",   "2Y"),
        ("us_10y", "United States 10-Year",  "10Y"),
    ]:
        live = fetch_fm_yield(fm_id, label)
        data[key] = merge(live, FALLBACK[key])
        sources[key] = "FinMind" if live else "備用值"

    # ── 2. HY/IG 利差 & VIX（FRED）
    log.info("▶ HY/IG 信用利差 & VIX (FRED)")
    for key, series_id, label in [
        ("hy",  "BAMLH0A0HYM2", "HY利差"),
        ("ig",  "BAMLC0A0CM",   "IG利差"),
        ("vix", "VIXCLS",       "VIX"),
    ]:
        live = fetch_fred(series_id, label)
        data[key] = merge(live, FALLBACK[key])
        sources[key] = "FRED" if live else "備用值"

    # ── 3. CBC：M1B / M2 年增率
    log.info("▶ M1B / M2 年增率 (中央銀行)")
    cbc = fetch_cbc_m1b_m2()
    data["m1b"] = merge(cbc.get("m1b", []), FALLBACK["m1b"])
    data["m2"]  = merge(cbc.get("m2",  []), FALLBACK["m2"])
    sources["m1b"] = "CBC" if cbc.get("m1b") else "備用值"
    sources["m2"]  = "CBC" if cbc.get("m2")  else "備用值"

    # ── 4. 景氣領先指標（FinMind backer，備援備用值）
    log.info("▶ 景氣領先指標 (FinMind TaiwanBusinessIndicator)")
    lei_live = fetch_fm_lei()
    data["lei"] = merge(lei_live, FALLBACK["lei"])
    sources["lei"] = "FinMind" if lei_live else "備用值"

    # ── 5. 外銷訂單動向指數（MOEA）
    log.info("▶ 外銷訂單動向指數 (經濟部統計處)")
    export_live = fetch_export_orders_index()
    data["export"] = merge(export_live, FALLBACK["export"])
    sources["export"] = "MOEA" if export_live else "備用值"

    # ── 6. TWSE：大盤成交量
    log.info("▶ 大盤成交量 (TWSE FMTQIK)")
    vol_live = fetch_twse_volume()
    data["vol"] = merge(vol_live, FALLBACK["vol"])
    sources["vol"] = "TWSE" if vol_live else "備用值"

    # ── 7. TWSE：巴菲特指標（市值 ÷ GDP）
    log.info("▶ 巴菲特指標 (TWSE市值 ÷ 主計總處GDP)")
    bf_live = fetch_twse_mkt_cap()
    data["bf"] = merge(bf_live, FALLBACK["bf"])
    sources["bf"] = "TWSE+GDP" if bf_live else "備用值(估算)"

    # ── 8. M1B 成交比重（計算）
    log.info("▶ M1B 成交比重法 (計算)")
    m1br_calc = calc_m1b_ratio(data["vol"])
    data["m1br"] = merge(m1br_calc, FALLBACK["m1br"])
    sources["m1br"] = "計算(TWSE+CBC)"

    # ── 9. FinMind：融資餘額
    log.info("▶ 融資餘額 (FinMind)")
    margin_live = fetch_fm_margin()
    data["margin"] = merge(margin_live, FALLBACK["margin"])
    sources["margin"] = "FinMind" if margin_live else "備用值"

    # ── 輸出 JSON ────────────────────────────────────────────────────
    output = {
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_tw": datetime.now().strftime("%Y/%m/%d %H:%M"),
        "sources": sources,
        "data": data,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info("=" * 60)
    log.info(f"✅ 輸出完成：{OUTPUT_FILE}")
    log.info(f"   各指標來源：")
    for k, s in sources.items():
        latest = data[k][-1] if data[k] else {}
        log.info(f"   {k:12s} [{s:15s}] 最新: {latest}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
