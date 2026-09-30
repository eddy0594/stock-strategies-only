"""每日股票池：證交所＋櫃買中心盤後資料，依「成交值」取前 N 名。

取代原本讀 Google Sheet Watchlist 的做法：
  - 證交所 OpenAPI（STOCK_DAY_ALL）與櫃買中心 OpenAPI 各打 1 次，拿全市場當日成交資料
  - 只保留普通股（4 碼、首碼非 0、排除 91xx 存託憑證），ETF／ETN／權證／特別股自動排除
  - 兩市場合併依成交值排序，取前 N 名
  - 不消耗 FinMind 額度

資料日期檢查：盤後資料要收盤後一段時間才更新。若抓到的不是今天（台北時間），
會每隔幾分鐘重抓一次，等到超過上限仍不是今天，就回傳 is_today=False 交給呼叫端決定
（通常代表休市日）。
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TW_TZ = ZoneInfo("Asia/Taipei")

# 兩個交易所的欄位名稱不同，這裡列候選名稱，依序比對
_CODE_KEYS = ["Code", "SecuritiesCompanyCode", "code", "證券代號", "代號"]
_NAME_KEYS = ["Name", "CompanyName", "name", "證券名稱", "名稱"]
_VALUE_KEYS = ["TradeValue", "TransactionAmount", "TradingAmount", "成交金額", "成交金額(元)"]
_DATE_KEYS = ["Date", "date", "資料日期"]

_COMMON_CODE = re.compile(r"^[1-9]\d{3}$")
_EXCLUDED_INDUSTRIES = {"ETF", "ETN", "存託憑證", "受益證券", "Index", "大盤"}

_HEADERS = {"User-Agent": "Mozilla/5.0 (stock-strategies daily universe)"}


def today_tw() -> date:
    return datetime.now(TW_TZ).date()


def _pick(row: dict, keys: list[str]):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def parse_tw_date(v) -> date | None:
    """支援民國年 1150930、西元 20260930、2026-09-30、115/09/30。"""
    if v is None:
        return None
    s = str(v).strip()
    parts = re.split(r"[/\-.]", s)
    try:
        if len(parts) == 3:
            y, m, d = (int(p) for p in parts)
        else:
            digits = re.sub(r"\D", "", s)
            if len(digits) == 7:
                y, m, d = int(digits[:3]), int(digits[3:5]), int(digits[5:])
            elif len(digits) == 8:
                y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:])
            else:
                return None
        if y < 1911:
            y += 1911
        return date(y, m, d)
    except ValueError:
        return None


def is_common_stock(code: str) -> bool:
    return bool(_COMMON_CODE.match(code)) and not code.startswith("91")


def normalize_rows(rows: list[dict], market: str) -> list[dict]:
    """把交易所原始 JSON 轉成 {stock_id, name, value, date, market}。"""
    out = []
    for row in rows or []:
        code = _pick(row, _CODE_KEYS)
        value = _to_float(_pick(row, _VALUE_KEYS))
        if code is None or value is None:
            continue
        out.append({
            "stock_id": str(code).strip(),
            "name": str(_pick(row, _NAME_KEYS) or "").strip(),
            "value": value,
            "date": parse_tw_date(_pick(row, _DATE_KEYS)),
            "market": market,
        })
    if rows and not out:
        raise ValueError(
            f"{market} 回傳欄位無法辨識（可能交易所改了格式），實際欄位: {list(rows[0].keys())}"
        )
    return out


def _fetch_json(url: str, timeout: int = 30, retries: int = 2) -> list[dict]:
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise ValueError(f"非預期的回傳格式: {type(data).__name__}")
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"讀取 {url} 失敗: {last}")


def fetch_snapshot(include_tpex: bool = True) -> tuple[list[dict], date | None, list[str]]:
    """抓全市場當日成交資料。回 (rows, 證交所資料日, notes)。

    上櫃資料若失敗或日期與上市不同，會捨棄上櫃並在 notes 說明，避免混用不同天的資料。
    """
    notes: list[str] = []
    twse = normalize_rows(_fetch_json(TWSE_URL), "上市")
    twse_dates = [r["date"] for r in twse if r["date"]]
    data_date = max(twse_dates) if twse_dates else None
    rows = list(twse)

    if include_tpex:
        try:
            tpex = normalize_rows(_fetch_json(TPEX_URL), "上櫃")
            tpex_dates = [r["date"] for r in tpex if r["date"]]
            tpex_date = max(tpex_dates) if tpex_dates else None
            if data_date and tpex_date and tpex_date != data_date:
                notes.append(f"上櫃資料日期 {tpex_date} 與上市 {data_date} 不同，本次只用上市")
            else:
                rows.extend(tpex)
        except Exception as e:  # noqa: BLE001
            notes.append(f"上櫃資料讀取失敗，本次只用上市（{str(e)[:80]}）")
    return rows, data_date, notes


def top_by_value(rows: list[dict], n: int, industry_map: dict | None = None) -> list[dict]:
    """過濾普通股 → 依成交值排序 → 取前 n。回傳格式與原 Watchlist 相容。"""
    industry_map = industry_map or {}
    seen = set()
    pool = []
    for r in rows:
        sid = r["stock_id"]
        if sid in seen or not is_common_stock(sid):
            continue
        industry = industry_map.get(sid)
        if industry in _EXCLUDED_INDUSTRIES:
            continue
        seen.add(sid)
        pool.append({**r, "industry": industry})
    pool.sort(key=lambda r: r["value"], reverse=True)
    return [
        {
            "stock_id": r["stock_id"],
            "name": r["name"],
            "category": r["industry"] or r["market"],
            "enabled": True,
            "trade_value": r["value"],
            "market": r["market"],
        }
        for r in pool[:n]
    ]


def _industry_map() -> dict:
    """用 FinMind TaiwanStockInfo 取產業別（快取 7 天、1 次請求），失敗就回空。"""
    try:
        from .datasources import get_stock_info

        info = get_stock_info()
        if info.empty or "industry_category" not in info.columns:
            return {}
        return dict(zip(info["stock_id"].astype(str), info["industry_category"]))
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 產業別讀取失敗，類股分類改用上市/上櫃: {e}")
        return {}


def get_daily_universe(
    n: int = 100,
    include_tpex: bool = True,
    wait_minutes: int = 60,
    poll_minutes: int = 10,
) -> dict:
    """回 {items, data_date, is_today, notes}。

    資料日期不是今天時，每 poll_minutes 分鐘重抓一次，最多等 wait_minutes 分鐘。
    """
    deadline = time.monotonic() + wait_minutes * 60
    while True:
        rows, data_date, notes = fetch_snapshot(include_tpex)
        is_today = data_date == today_tw()
        if is_today or time.monotonic() + poll_minutes * 60 > deadline:
            break
        print(f"  盤後資料尚未更新（目前 {data_date}），{poll_minutes} 分鐘後重試...")
        time.sleep(poll_minutes * 60)

    items = top_by_value(rows, n, _industry_map())
    return {"items": items, "data_date": data_date, "is_today": is_today, "notes": notes}
