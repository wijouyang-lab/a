# -*- coding: utf-8 -*-
"""
雪球优先数据层（A股）
- 行情：雪球 detail/batch quote
- K线：雪球 chart/kline
- 活跃榜：雪球 screener，按成交额排序
- Cookie：优先使用 XUEQIU_COOKIE；否则自动访问雪球首页/股票页预热匿名 Cookie
- 设计目标：雪球作为第一数据源；调用方仅允许使用 Eastmoney/Sina/Yahoo 行情备用，不再回退到 Tushare
"""
import os
import time
import json
import random
import urllib.parse
import urllib.request
import urllib.error
import http.cookiejar
import datetime
import pandas as pd

BASE = "https://stock.xueqiu.com"
HOME = "https://xueqiu.com/"
DEFAULT_UA = os.environ.get(
    "XUEQIU_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

_SESSION = None
_BOOTSTRAPPED = False
_LAST_REQUEST_TS = 0.0


def normalize_symbol(ticker):
    s = str(ticker or "").strip().upper()
    if not s:
        return ""
    if s.startswith(("SH", "SZ", "BJ")) and len(s) >= 8:
        return s
    if "." in s:
        code, market = s.split(".", 1)
        market = market.upper()
        if market == "SH":
            return "SH" + code
        if market == "SZ":
            return "SZ" + code
        if market == "BJ":
            return "BJ" + code
        return s
    if s.isdigit() and len(s) == 6:
        return ("SH" if s.startswith(("5", "6", "68")) else "SZ") + s
    return s


def _build_session():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", DEFAULT_UA),
        ("Accept", "application/json,text/plain,*/*"),
        ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.6"),
        ("Connection", "keep-alive"),
    ]
    return opener, jar


def _apply_cookie_string(jar, cookie_string):
    if not cookie_string:
        return
    for item in str(cookie_string).split(";"):
        if "=" not in item:
            continue
        name, value = item.strip().split("=", 1)
        if not name:
            continue
        try:
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value=value, port=None, port_specified=False,
                domain=".xueqiu.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=True, expires=None, discard=True,
                comment=None, comment_url=None, rest={"HttpOnly": None}, rfc2109=False
            ))
        except Exception:
            pass


def _ensure_session(symbol=None):
    global _SESSION, _BOOTSTRAPPED
    if _SESSION is None:
        _SESSION = _build_session()
        cookie = os.environ.get("XUEQIU_COOKIE", "").strip()
        _apply_cookie_string(_SESSION[1], cookie)

    if not _BOOTSTRAPPED:
        targets = [HOME]
        if symbol:
            targets.append(f"https://xueqiu.com/S/{normalize_symbol(symbol)}")
        for url in targets:
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": DEFAULT_UA,
                    "Accept": "text/html,application/xhtml+xml,application/json",
                })
                with _SESSION[0].open(req, timeout=8) as resp:
                    resp.read(1024)
                _BOOTSTRAPPED = True
                break
            except Exception:
                continue
        # 即使首页预热失败，也允许带显式 Cookie 继续请求。
        if os.environ.get("XUEQIU_COOKIE"):
            _BOOTSTRAPPED = True
    return _SESSION[0]


def _request_json(path, params=None, timeout=12, retries=3, symbol=None):
    global _LAST_REQUEST_TS, _BOOTSTRAPPED
    opener = _ensure_session(symbol=symbol)
    params = params or {}
    url = path if path.startswith("http") else BASE + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

    last_err = None
    for attempt in range(1, retries + 1):
        # 限频保护；不同 ticker 也保留小间隔，避免 GitHub Actions 短时间爆量。
        gap = 0.28 + random.uniform(0.05, 0.16)
        elapsed = time.time() - _LAST_REQUEST_TS
        if elapsed < gap:
            time.sleep(gap - elapsed)

        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": DEFAULT_UA,
                "Accept": "application/json,text/plain,*/*",
                "Referer": f"https://xueqiu.com/S/{normalize_symbol(symbol)}" if symbol else HOME,
                "Origin": "https://xueqiu.com",
            })
            _LAST_REQUEST_TS = time.time()
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            obj = json.loads(raw)

            # 常见雪球返回：data + error_code / error_description
            if isinstance(obj, dict):
                code = obj.get("error_code")
                if code not in (None, 0, "0"):
                    raise RuntimeError(f"雪球 error_code={code}: {obj.get('error_description', '')}")
            return obj

        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (401, 403, 429):
                _BOOTSTRAPPED = False
                if attempt < retries:
                    time.sleep(min(1.2 * attempt, 4.0) + random.uniform(0.1, 0.4))
                    opener = _ensure_session(symbol=symbol)
                    continue
            if attempt < retries:
                time.sleep(min(0.8 * (2 ** (attempt - 1)), 4) + random.uniform(0.1, 0.3))
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(min(0.8 * (2 ** (attempt - 1)), 4) + random.uniform(0.1, 0.3))

    return None


def get_quote(symbol, detail=True):
    sym = normalize_symbol(symbol)
    if not sym:
        return {}
    path = "/v5/stock/quote.json" if detail else "/v5/stock/realtime/quotec.json"
    params = {"symbol": sym}
    if detail:
        params["extend"] = "detail"
    obj = _request_json(path, params=params, symbol=sym)
    try:
        return ((obj or {}).get("data") or {}).get("quote") or {}
    except Exception:
        return {}


def get_quotes(symbols):
    syms = [normalize_symbol(x) for x in symbols if normalize_symbol(x)]
    if not syms:
        return {}
    out = {}
    # 批量接口不稳定时拆成较小批次。
    for i in range(0, len(syms), 50):
        chunk = syms[i:i+80]
        obj = _request_json(
            "/v5/stock/batch/quote.json",
            params={"symbol": ",".join(chunk)},
            symbol=chunk[0] if chunk else None,
        )
        items = ((obj or {}).get("data") or {}).get("items") or []
        for item in items:
            q = item.get("quote") if isinstance(item, dict) else None
            if not q and isinstance(item, dict):
                q = item
            if isinstance(q, dict):
                out[str(q.get("symbol") or item.get("symbol") or "").upper()] = q
    return out


def get_kline(symbol, count=260, period="day"):
    sym = normalize_symbol(symbol)
    if not sym:
        return pd.DataFrame()
    begin = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    obj = _request_json(
        "/v5/stock/chart/kline.json",
        params={
            "symbol": sym,
            "begin": begin,
            "period": period,
            "type": "before",
            "count": -abs(int(count)),
            "indicator": "kline,pe,pb,ps,pcf,market_capital",
        },
        symbol=sym,
    )
    try:
        items = ((obj or {}).get("data") or {}).get("item") or []
        if not items:
            return pd.DataFrame()
        rows = []
        for x in items:
            if not isinstance(x, (list, tuple)) or len(x) < 9:
                continue
            ts = x[0]
            # 雪球 K 线返回时间戳为毫秒
            dt = pd.to_datetime(float(ts), unit="ms", utc=True).dt.tz_localize(None) if False else pd.to_datetime(float(ts), unit="ms", utc=True).tz_localize(None)
            rows.append({
                "ts_code": symbol if "." in str(symbol) else (
                    str(sym)[2:] + "." + str(sym)[:2] if str(sym)[:2] in ("SH", "SZ", "BJ") else str(sym)
                ),
                "trade_date": dt.strftime("%Y%m%d"),
                "datetime": dt,
                "open": float(x[1]) if x[1] is not None else None,
                "close": float(x[2]) if x[2] is not None else None,
                "high": float(x[3]) if x[3] is not None else None,
                "low": float(x[4]) if x[4] is not None else None,
                "pct_chg": float(x[5]) if x[5] is not None else None,
                "change": float(x[6]) if x[6] is not None else None,
                "vol": float(x[7]) if x[7] is not None else 0.0,
                "amount": float(x[8]) if x[8] is not None else 0.0,
            })
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        return df.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def get_daily_ohlc_on_date(symbol, target_date):
    df = get_kline(symbol, count=30, period="day")
    if df.empty:
        return {}
    target = pd.to_datetime(target_date, errors="coerce")
    if pd.isna(target):
        return {}
    row = df[pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce") == target.normalize()]
    if row.empty:
        return {}
    r = row.iloc[-1]
    return {
        "open": r.get("open"),
        "high": r.get("high"),
        "low": r.get("low"),
        "close": r.get("close"),
        "amount": r.get("amount"),
        "vol": r.get("vol"),
        "pct_chg": r.get("pct_chg"),
    }


def get_top_by_amount(size=300):
    obj = _request_json(
        "/v5/stock/screener/quote/list.json",
        params={
            "market": "CN",
            "type": "sh_sz",
            "order": "desc",
            "order_by": "amount",
            "page": 1,
            "size": int(size),
        },
    )
    try:
        data = (obj or {}).get("data") or {}
        items = data.get("list") or data.get("items") or []
        return items
    except Exception:
        return []

def search_statuses(query, count=10):
    """雪球动态搜索。失败返回空列表，不影响其它数据源。"""
    q = str(query or "").strip()
    if not q:
        return []
    obj = _request_json(
        "https://xueqiu.com/statuses/search.json",
        params={"source": "all", "q": q, "count": int(count)},
    )
    try:
        data = (obj or {}).get("list") or (obj or {}).get("data") or []
        if isinstance(data, dict):
            data = data.get("list") or data.get("statuses") or []
        return data if isinstance(data, list) else []
    except Exception:
        return []
