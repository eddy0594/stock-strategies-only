from datetime import date

import pytest

from stock_strategies import universe as u


TWSE_ROWS = [
    {"Date": "1150930", "Code": "2330", "Name": "台積電", "TradeValue": "50,000,000,000"},
    {"Date": "1150930", "Code": "0050", "Name": "元大台灣50", "TradeValue": "90,000,000,000"},
    {"Date": "1150930", "Code": "00878", "Name": "國泰永續高股息", "TradeValue": "30,000,000,000"},
    {"Date": "1150930", "Code": "9105", "Name": "泰金寶-DR", "TradeValue": "20,000,000,000"},
    {"Date": "1150930", "Code": "2317", "Name": "鴻海", "TradeValue": "20,000,000,000"},
    {"Date": "1150930", "Code": "2881A", "Name": "富邦特", "TradeValue": "10,000,000,000"},
    {"Date": "1150930", "Code": "2454", "Name": "聯發科", "TradeValue": ""},
]
TPEX_ROWS = [
    {"Date": "1150930", "SecuritiesCompanyCode": "5274", "CompanyName": "信驊",
     "TransactionAmount": "25000000000"},
    {"Date": "1150930", "SecuritiesCompanyCode": "006201", "CompanyName": "元大富櫃50",
     "TransactionAmount": "99000000000"},
]


def test_parse_tw_date():
    assert u.parse_tw_date("1150930") == date(2026, 9, 30)
    assert u.parse_tw_date("20260930") == date(2026, 9, 30)
    assert u.parse_tw_date("115/09/30") == date(2026, 9, 30)
    assert u.parse_tw_date("2026-09-30") == date(2026, 9, 30)
    assert u.parse_tw_date("garbage") is None


def test_is_common_stock():
    assert u.is_common_stock("2330")
    assert u.is_common_stock("5274")
    assert not u.is_common_stock("0050")
    assert not u.is_common_stock("00878")
    assert not u.is_common_stock("9105")      # 存託憑證
    assert not u.is_common_stock("2881A")     # 特別股
    assert not u.is_common_stock("030001")    # 權證


def test_normalize_rows_handles_both_exchanges():
    tw = u.normalize_rows(TWSE_ROWS, "上市")
    tp = u.normalize_rows(TPEX_ROWS, "上櫃")
    assert {r["stock_id"] for r in tw} == {"2330", "0050", "00878", "9105", "2317", "2881A"}
    assert tp[0] == {"stock_id": "5274", "name": "信驊", "value": 25e9,
                     "date": date(2026, 9, 30), "market": "上櫃"}


def test_normalize_rows_raises_on_unknown_format():
    with pytest.raises(ValueError, match="欄位無法辨識"):
        u.normalize_rows([{"foo": 1, "bar": 2}], "上市")


def test_top_by_value_filters_and_ranks():
    rows = u.normalize_rows(TWSE_ROWS, "上市") + u.normalize_rows(TPEX_ROWS, "上櫃")
    top = u.top_by_value(rows, 2, {"2330": "半導體業"})
    assert [t["stock_id"] for t in top] == ["2330", "5274"]
    assert top[0]["category"] == "半導體業"
    assert top[1]["category"] == "上櫃"          # 無產業資料時用市場別
    assert all(t["enabled"] for t in top)


def test_top_by_value_excludes_etf_by_industry():
    rows = [{"stock_id": "1234", "name": "X", "value": 1e9, "date": None, "market": "上市"}]
    assert u.top_by_value(rows, 10, {"1234": "ETF"}) == []


def _patch_fetch(monkeypatch, twse, tpex):
    def fake(url, **_):
        if url == u.TWSE_URL:
            return twse
        if isinstance(tpex, Exception):
            raise tpex
        return tpex
    monkeypatch.setattr(u, "_fetch_json", fake)
    monkeypatch.setattr(u, "_industry_map", lambda: {})


def test_snapshot_drops_tpex_when_dates_differ(monkeypatch):
    stale_tpex = [{**r, "Date": "1150929"} for r in TPEX_ROWS]
    _patch_fetch(monkeypatch, TWSE_ROWS, stale_tpex)
    rows, d, notes = u.fetch_snapshot(include_tpex=True)
    assert d == date(2026, 9, 30)
    assert all(r["market"] == "上市" for r in rows)
    assert "只用上市" in notes[0]


def test_snapshot_survives_tpex_failure(monkeypatch):
    _patch_fetch(monkeypatch, TWSE_ROWS, RuntimeError("boom"))
    rows, _, notes = u.fetch_snapshot(include_tpex=True)
    assert rows and "讀取失敗" in notes[0]


def test_get_daily_universe_today(monkeypatch):
    _patch_fetch(monkeypatch, TWSE_ROWS, TPEX_ROWS)
    monkeypatch.setattr(u, "today_tw", lambda: date(2026, 9, 30))
    res = u.get_daily_universe(n=100, wait_minutes=0)
    assert res["is_today"] is True
    assert [i["stock_id"] for i in res["items"]] == ["2330", "5274", "2317"]


def test_get_daily_universe_stale_does_not_wait_when_zero(monkeypatch):
    _patch_fetch(monkeypatch, TWSE_ROWS, TPEX_ROWS)
    monkeypatch.setattr(u, "today_tw", lambda: date(2026, 10, 1))
    monkeypatch.setattr(u.time, "sleep", lambda s: pytest.fail("should not sleep"))
    res = u.get_daily_universe(n=100, wait_minutes=0)
    assert res["is_today"] is False
    assert res["data_date"] == date(2026, 9, 30)
