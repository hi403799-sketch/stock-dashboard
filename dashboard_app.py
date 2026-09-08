# -*- coding: utf-8 -*-
"""
관심 종목을 검색해서 추가하고, 종목마다 다른 조건(이동평균 근접, 직전 고점 돌파,
목표가 도달)을 설정한 뒤, 현재가가 그 조건에 얼마나 가까운지 실시간에 가깝게
보여주는 웹 대시보드입니다. 국내 주식, 미국 주식, 코인을 모두 등록할 수 있습니다.

[사전 준비]
pip install streamlit pykrx pandas requests yfinance

[실행 방법]
1. 명령 프롬프트에서 이 파일이 있는 폴더로 이동 (예: cd Downloads)
2. 아래 명령어 실행:
   streamlit run dashboard_app.py
3. 자동으로 브라우저가 열리면서 대시보드가 뜹니다. (안 열리면 명령 프롬프트에
   나오는 http://localhost:8501 주소를 직접 브라우저에 입력하세요)
4. 종료하려면 명령 프롬프트 창에서 Ctrl + C

[알림 받기]
폰에 ntfy 앱을 설치하고 토픽을 하나 구독한 뒤, 대시보드 왼쪽 사이드바에
그 토픽 이름을 입력하면 조건 달성 시 폰으로 알림이 옵니다.

[참고]
- 국내 주식 현재가: 네이버 금융의 실시간 시세를 가져옵니다 (장중 수십 초 이내 지연).
- 미국 주식 현재가: yfinance(야후 파이낸스)를 통해 가져옵니다 (보통 몇 분 내외 지연).
- 이동평균/직전 고점: 전일까지의 일봉(종가/고가) 기준으로 계산됩니다.
"""

import base64
import json
import os
from datetime import datetime, timedelta

import altair as alt
import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from pykrx import stock

# ==================================================================
# 기본 설정
# ==================================================================
st.set_page_config(page_title="관심종목 조건 모니터", layout="wide")

WATCHLIST_FILE = "watchlist.json"
CONFIG_FILE = "config.json"

AUTO_REFRESH_SECONDS = 60  # 몇 초마다 자동으로 새로고침할지 (원하면 숫자만 수정)

CONDITION_NAMES = {
    "ma_touch": "이동평균선 근접",
    "prior_high": "직전 고점 돌파",
    "target_price": "목표가 도달",
    "trend_break": "추세선 돌파",
}

CURRENCY_SYMBOL = {"KR": "원", "US": "$", "COIN": "$"}


def format_price(value, market):
    """시장에 맞는 통화 표시로 가격을 포맷한다."""
    if value is None:
        return "-"
    if market == "KR":
        return f"{value:,.0f}원"
    # 미국주식/코인: 가격이 작은 알트코인 등을 고려해 자릿수를 유동적으로
    decimals = 2 if value >= 1 else 6
    return f"${value:,.{decimals}f}"


# ==================================================================
# 저장 / 불러오기
# 로컬 실행: 그냥 파일로 저장 (재실행해도 유지됨)
# 클라우드 실행: 로컬 파일은 언제든 초기화될 수 있으므로, GitHub Secrets에
# github_token / github_repo 가 설정되어 있으면 GitHub 저장소 안의 파일에
# 직접 저장해서 앱이 잠들었다 깨어나도 데이터가 유지되게 한다.
# ==================================================================
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_secret(key, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


GITHUB_TOKEN = get_secret("github_token")
GITHUB_REPO = get_secret("github_repo")  # 예: "hi403799-sketch/stock-dashboard"


def github_configured():
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def github_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}


def github_get_file(path):
    """GitHub 저장소에서 파일을 읽어온다. (내용, sha) 튜플, 없으면 (None, None)"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    try:
        resp = requests.get(url, headers=github_headers(), timeout=10)
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        content = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
        return content, data["sha"]
    except Exception:
        return None, None


def github_put_file(path, content_dict, message):
    """GitHub 저장소의 파일을 새 내용으로 커밋한다."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    _, sha = github_get_file(path)
    body = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(content_dict, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("utf-8"),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    try:
        requests.put(url, headers=github_headers(), json=body, timeout=10)
    except Exception:
        pass


def load_persistent(path, default):
    if github_configured():
        content, _ = github_get_file(path)
        return content if content is not None else default
    return load_json(path, default)


def save_persistent(path, data):
    if github_configured():
        github_put_file(path, data, message=f"update {path}")
    else:
        save_json(path, data)


if "watchlist" not in st.session_state:
    st.session_state.watchlist = load_persistent(WATCHLIST_FILE, [])
if "config" not in st.session_state:
    st.session_state.config = load_persistent(
        CONFIG_FILE, {"ntfy_topic": "", "coingecko_api_key": ""}
    )
if "notified" not in st.session_state:
    st.session_state.notified = {}  # 중복 알림 방지용: {종목+조건 키: 이미 달성 여부}


# ==================================================================
# 종목 검색용 전체 종목명 목록 준비 (최초 1회만 시간 걸림, 이후 파일 재사용)
# ==================================================================


# ==================================================================
# 실시간 현재가 조회
# ==================================================================
def get_current_prices_kr(tickers):
    """네이버 금융 시세 API로 국내 주식 현재가를 한 번에 조회"""
    if not tickers:
        return {}
    codes = ",".join(tickers)
    url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{codes}"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        datas = resp.json().get("datas", [])
    except Exception as e:
        st.warning(f"국내 주식 시세 조회에 실패했습니다: {e}")
        return {}

    result = {}
    for i, item in enumerate(datas):
        code = item.get("itemCode") or item.get("cd") or item.get("code")
        if not code and i < len(tickers):
            code = tickers[i]  # 코드 필드가 없으면 요청 순서로 매칭
        try:
            price = int(str(item.get("closePrice", "")).replace(",", ""))
        except (ValueError, TypeError):
            price = None
        result[code] = {
            "price": price,
            "market_status": item.get("marketStatus", "-"),
        }
    return result


def get_current_prices_us(tickers):
    """yfinance로 미국 주식 현재가를 조회 (보통 몇 분 내외 지연)"""
    result = {}
    if not tickers:
        return result
    for t in tickers:
        try:
            fast_info = yf.Ticker(t).fast_info
            price = fast_info.get("lastPrice") or fast_info.get("last_price")
            result[t] = {"price": round(price, 2) if price else None, "market_status": "-"}
        except Exception:
            result[t] = {"price": None, "market_status": "-"}
    return result


def get_secret_coingecko_key():
    """Streamlit Cloud의 Secrets에 저장된 키가 있으면 반환 (재배포/재시작해도 안 사라짐)"""
    return get_secret("coingecko_api_key")


def get_coingecko_headers():
    """설정된 코인게코 Demo API 키가 있으면 헤더에 포함시킨다.
    우선순위: 사이드바에 직접 입력한 값 > Streamlit Secrets에 저장된 값
    (없으면 훨씬 낮은 무료 한도로 동작)"""
    headers = {"User-Agent": "Mozilla/5.0"}
    api_key = st.session_state.config.get("coingecko_api_key", "").strip() or get_secret_coingecko_key()
    if api_key:
        headers["x-cg-demo-api-key"] = api_key
    return headers


@st.cache_data(ttl=86400, show_spinner=False)
def search_coingecko_id(query):
    """코인 심볼/이름으로 코인게코 coin id를 찾는다. (예: 'BTC' -> 'bitcoin')"""
    if not query:
        return None
    url = "https://api.coingecko.com/api/v3/search"
    try:
        resp = requests.get(url, params={"query": query}, headers=get_coingecko_headers(), timeout=5)
        coins = resp.json().get("coins", [])
    except Exception:
        return None

    if not coins:
        return None

    query_lower = query.strip().lower()
    # 심볼이 정확히 일치하는 것을 최우선으로 선택 (예: 'HYPE' -> Hyperliquid)
    for c in coins:
        if c.get("symbol", "").lower() == query_lower:
            return {"id": c["id"], "name": c.get("name", c["id"]), "symbol": c.get("symbol", "")}
    # 정확히 일치하는 게 없으면 검색 결과 1순위 사용
    c = coins[0]
    return {"id": c["id"], "name": c.get("name", c["id"]), "symbol": c.get("symbol", "")}


def get_current_prices_coingecko(coin_ids):
    """코인게코 공개 API로 USD 기준 현재가를 한 번에 조회"""
    result = {}
    if not coin_ids:
        return result
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        resp = requests.get(
            url,
            params={"ids": ",".join(coin_ids), "vs_currencies": "usd"},
            headers=get_coingecko_headers(),
            timeout=5,
        )
        data = resp.json()
    except Exception as e:
        st.warning(f"코인 시세 조회에 실패했습니다: {e}")
        return result

    if not isinstance(data, dict) or "coins" not in data and any(
        k in data for k in ("status", "error")
    ):
        # 요청 실패(레이트리밋 등) 응답 형태인 경우
        st.warning("코인게코 응답에 오류가 있어요. API 키를 등록했는지, 요청이 너무 잦지 않은지 확인해보세요.")
        return result

    for coin_id, values in data.items():
        if not isinstance(values, dict):
            continue
        price = values.get("usd")
        result[coin_id] = {"price": price, "market_status": "-"}
    return result


def get_current_prices(watchlist):
    """종목의 market 값에 따라 국내(네이버) / 미국(yfinance) / 코인(코인게코)으로 나눠서 조회 후 합침"""
    kr_tickers = [w["ticker"] for w in watchlist if w.get("market", "KR") == "KR"]
    us_tickers = [w["ticker"] for w in watchlist if w.get("market", "KR") == "US"]
    coin_ids = [w["coin_id"] for w in watchlist if w.get("market", "KR") == "COIN" and w.get("coin_id")]
    prices = {}
    prices.update(get_current_prices_kr(kr_tickers))
    prices.update(get_current_prices_us(us_tickers))
    prices.update(get_current_prices_coingecko(coin_ids))
    return prices


# ==================================================================
# 일봉 데이터 조회 (이동평균/고점 계산용, 1시간 캐시 - 장중에 자주 안 바뀜)
# close/high 컬럼명으로 통일해서 국내/미국/코인 데이터를 동일하게 다룬다.
# ==================================================================
@st.cache_data(ttl=3600, show_spinner=False)
def get_history_kr(ticker, lookback_days):
    start = (datetime.now() - timedelta(days=lookback_days * 2 + 30)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    try:
        df = stock.get_market_ohlcv(start, end, ticker)
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return df

    # 이평/고점은 항상 '확정된 전일까지의 종가' 기준으로만 계산하기 위해
    # 혹시 오늘 날짜 데이터가 섞여 있으면 확실히 제외한다.
    today_str = datetime.now().strftime("%Y%m%d")
    df = df[df.index.strftime("%Y%m%d") != today_str]
    return df.rename(columns={"종가": "close", "고가": "high"})


@st.cache_data(ttl=3600, show_spinner=False)
def get_history_us(ticker, lookback_days):
    start = (datetime.now() - timedelta(days=lookback_days * 2 + 30)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    try:
        df = yf.Ticker(ticker).history(start=start, end=end)
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return df

    today_str = datetime.now().strftime("%Y-%m-%d")
    df = df[df.index.strftime("%Y-%m-%d") != today_str]
    return df.rename(columns={"Close": "close", "High": "high"})


@st.cache_data(ttl=3600, show_spinner=False)
def get_history_coingecko(coin_id, lookback_days):
    """코인게코 일별 가격 데이터 조회.
    (코인게코 무료 API는 시가/고가/저가를 따로 안 주기 때문에, '고점'은 이 일별
    가격들의 최고값으로 근사한다 - 장중 고점보다는 약간 낮게 잡힐 수 있음)"""
    days = min(lookback_days * 2 + 30, 365)
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    try:
        resp = requests.get(
            url,
            params={"vs_currency": "usd", "days": days},
            headers=get_coingecko_headers(),
            timeout=10,
        )
        prices = resp.json().get("prices", [])
    except Exception:
        return pd.DataFrame()

    if not prices:
        return pd.DataFrame()

    df = pd.DataFrame(prices, columns=["timestamp", "close"])
    df["date"] = pd.to_datetime(df["timestamp"], unit="ms").dt.strftime("%Y-%m-%d")
    df = df.groupby("date").last()  # 하루에 여러 값이 있으면 그날의 마지막 값만 사용
    df.index = pd.to_datetime(df.index)
    df["high"] = df["close"]  # 무료 API 한계로 종가를 고가의 근사치로 사용

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    df = df[df.index.strftime("%Y-%m-%d") != today_str]
    return df[["close", "high"]]



def get_data_id(item):
    """실제 데이터 조회에 쓸 식별자. 코인은 coin_id, 그 외에는 ticker."""
    if item.get("market", "KR") == "COIN":
        return item.get("coin_id")
    return item["ticker"]


def get_history(data_id, market, lookback_days):
    if market == "KR":
        return get_history_kr(data_id, lookback_days)
    if market == "COIN":
        return get_history_coingecko(data_id, lookback_days)
    return get_history_us(data_id, lookback_days)


def get_reference_value(item):
    """조건별 '기준값' 계산 (이평값, 직전 고점, 목표가)"""
    condition = item["condition"]
    market = item.get("market", "KR")
    data_id = get_data_id(item)

    if condition == "target_price":
        return item["target_price"]

    if condition == "trend_break":
        # 두 점(날짜+가격)을 지나는 직선을 오늘 날짜까지 연장한 값을 계산한다.
        try:
            d1 = datetime.strptime(item["point1_date"], "%Y-%m-%d").date()
            d2 = datetime.strptime(item["point2_date"], "%Y-%m-%d").date()
            p1 = item["point1_price"]
            p2 = item["point2_price"]
        except (KeyError, ValueError):
            return None

        days_total = (d2 - d1).days
        if days_total == 0:
            return None

        slope = (p2 - p1) / days_total
        days_to_today = (datetime.now().date() - d1).days
        return p1 + slope * days_to_today

    if not data_id:
        return None

    if condition == "ma_touch":
        period = item["ma_period"]
        ma_type = item.get("ma_type", "SMA")

        if ma_type == "EMA":
            # EMA는 과거 데이터를 재귀적으로 반영하기 때문에, 데이터가 짧으면
            # 증권사 값(보통 상장일부터 계산)과 오차가 생긴다. 충분히 긴 기간을 확보해서
            # 초기값의 영향이 거의 사라지도록 한다.
            fetch_days = max(period * 8, 300)
        else:
            fetch_days = period

        df = get_history(data_id, market, fetch_days)
        if df.empty or len(df) < period:
            return None

        if ma_type == "EMA":
            return df["close"].ewm(span=period, adjust=False).mean().iloc[-1]
        return df["close"].rolling(period).mean().iloc[-1]

    if condition == "prior_high":
        period = item["lookback_days"]
        df = get_history(data_id, market, period)
        if df.empty:
            return None
        recent = df.iloc[-period:] if len(df) >= period else df
        return recent["high"].max()

    return None


def get_external_chart_url(item):
    """네이버금융/야후파이낸스/코인게코 등 외부 차트 페이지 주소를 만든다."""
    market = item.get("market", "KR")
    if market == "KR":
        return f"https://finance.naver.com/item/main.naver?code={item['ticker']}"
    if market == "US":
        return f"https://finance.yahoo.com/quote/{item['ticker']}"
    if market == "COIN":
        coin_id = item.get("coin_id", "")
        return f"https://www.coingecko.com/en/coins/{coin_id}" if coin_id else ""
    return ""


def get_condition_label(item):
    """표에 표시할 조건 설명 문구를 만든다."""
    condition = item["condition"]
    market = item.get("market", "KR")
    if condition == "ma_touch":
        return f"{item['ma_period']}일 {item.get('ma_type', 'SMA')} 근접"
    if condition == "prior_high":
        return f"최근 {item['lookback_days']}일 고점 돌파"
    if condition == "target_price":
        return f"목표가 {format_price(item['target_price'], market)}"
    if condition == "trend_break":
        direction = "상향 돌파" if item.get("trend_direction", "above") == "above" else "하향 이탈"
        return f"추세선 {direction}"
    return CONDITION_NAMES.get(condition, condition)


# ==================================================================
# 알림 전송 (ntfy.sh)
# ==================================================================
def send_notification(item, current, ref, diff_pct):
    topic = (get_secret("ntfy_topic") or st.session_state.config.get("ntfy_topic", "")).strip()
    if not topic:
        return
    market = item.get("market", "KR")
    title = f"{item['name']} - {get_condition_label(item)}"
    message = (
        f"현재가 {format_price(current, market)} / "
        f"기준값 {format_price(ref, market)} (차이 {diff_pct:+.2f}%)"
    )
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Priority": "high"},
            timeout=10,
        )
    except Exception:
        pass  # 알림 실패해도 대시보드 동작에는 지장 없게 조용히 넘어감


# ==================================================================
# 사이드바: 알림 설정
# ==================================================================
st.sidebar.header("🔔 알림 설정")
if get_secret("ntfy_topic"):
    st.sidebar.success("✅ Secrets에 저장된 토픽을 사용 중이에요. (재배포해도 유지됨)")
else:
    st.sidebar.caption(
        "GitHub 저장소를 공개(Public)로 쓰고 있다면, 토픽 이름이 코드에 저장되지 않도록 "
        "Streamlit Secrets에 ntfy_topic 으로 등록하는 걸 추천해요."
    )
    topic_input = st.sidebar.text_input(
        "ntfy 토픽 이름", value=st.session_state.config.get("ntfy_topic", "")
    )
    if topic_input != st.session_state.config.get("ntfy_topic"):
        st.session_state.config["ntfy_topic"] = topic_input
        save_persistent(CONFIG_FILE, st.session_state.config)

st.sidebar.header("🪙 코인 시세 설정")
st.sidebar.caption(
    "코인게코 무료 API는 키 없이는 자주 실패해요. "
    "[Demo API 키](https://www.coingecko.com/en/developers/dashboard)를 "
    "무료로 발급받아 입력하면 훨씬 안정적으로 조회돼요."
)
if get_secret_coingecko_key():
    st.sidebar.success("✅ Secrets에 저장된 API 키를 사용 중이에요. (재배포해도 유지됨)")
else:
    coingecko_key_input = st.sidebar.text_input(
        "코인게코 API 키 (CG-로 시작)",
        value=st.session_state.config.get("coingecko_api_key", ""),
        type="password",
    )
    if coingecko_key_input != st.session_state.config.get("coingecko_api_key"):
        st.session_state.config["coingecko_api_key"] = coingecko_key_input
        save_persistent(CONFIG_FILE, st.session_state.config)

# ==================================================================
# 사이드바: 종목 추가
# ==================================================================
st.sidebar.header("➕ 종목 추가")

market_choice = st.sidebar.radio("시장 선택", ["국내", "미국", "코인"], horizontal=True)
market_code = {"국내": "KR", "미국": "US", "코인": "COIN"}[market_choice]

if market_code == "KR":
    st.sidebar.caption(
        "네이버 금융(finance.naver.com)에서 종목 검색 후, "
        "주소창에 보이는 6자리 숫자를 코드로 입력하세요."
    )
    ticker_input = st.sidebar.text_input("종목 코드 (6자리 숫자)", key="ticker_box_kr")
elif market_code == "US":
    st.sidebar.caption("예: 애플=AAPL, 테슬라=TSLA, 엔비디아=NVDA")
    ticker_input = st.sidebar.text_input("티커(symbol)", key="ticker_box_us")
else:
    st.sidebar.caption("코인 이름이나 심볼을 입력하면 코인게코에서 찾아줘요. 예: BTC, ETH, HYPE")
    ticker_input = st.sidebar.text_input("코인 심볼/이름", key="ticker_box_coin")

name_input = st.sidebar.text_input("표시할 종목명 (자유롭게 입력)", key="name_box")

coin_match = None

if ticker_input:
    ticker_clean = ticker_input.strip()
    if market_code == "KR":
        valid_code = ticker_clean.isdigit() and len(ticker_clean) == 6
        invalid_msg = "코드는 숫자 6자리여야 해요. (예: 005930)"
    elif market_code == "US":
        ticker_clean = ticker_clean.upper()
        valid_code = ticker_clean.isalpha() or ("." in ticker_clean or "-" in ticker_clean)
        invalid_msg = "영문 티커를 입력해주세요. (예: AAPL, TSLA)"
    else:  # COIN
        ticker_clean = ticker_clean.upper()
        coin_match = search_coingecko_id(ticker_clean)
        valid_code = coin_match is not None
        invalid_msg = "코인을 찾을 수 없어요. 심볼이나 이름을 다시 확인해주세요."

    if not valid_code:
        st.sidebar.warning(invalid_msg)
    else:
        if coin_match:
            st.sidebar.caption(f"✅ 인식된 코인: {coin_match['name']} ({coin_match['symbol'].upper()})")

        display_name = name_input.strip() if name_input.strip() else ticker_clean

        condition_type = st.sidebar.selectbox(
            "조건 종류",
            list(CONDITION_NAMES.keys()),
            format_func=lambda x: CONDITION_NAMES[x],
        )

        new_item = {
            "ticker": ticker_clean,
            "name": display_name,
            "market": market_code,
            "condition": condition_type,
        }
        if market_code == "COIN" and coin_match:
            new_item["coin_id"] = coin_match["id"]

        if condition_type == "ma_touch":
            new_item["ma_period"] = st.sidebar.number_input("이동평균 기간(일)", 5, 300, 50)
            new_item["ma_type"] = st.sidebar.selectbox("이동평균 종류", ["SMA", "EMA"])
            new_item["threshold_pct"] = st.sidebar.number_input(
                "근접 기준(±%)", 0.1, 10.0, 1.0
            )
        elif condition_type == "prior_high":
            new_item["lookback_days"] = st.sidebar.number_input(
                "고점 조회 기간(일)", 20, 500, 252
            )
            new_item["threshold_pct"] = st.sidebar.number_input(
                "근접 기준(±%)", 0.1, 10.0, 1.0
            )
        elif condition_type == "target_price":
            price_label = {"KR": "목표가(원)", "US": "목표가($)", "COIN": "목표가($)"}[
                market_code
            ]
            new_item["target_price"] = st.sidebar.number_input(
                price_label, min_value=0.0, step=100.0 if market_code == "KR" else 1.0
            )
            new_item["threshold_pct"] = st.sidebar.number_input(
                "근접 기준(±%)", 0.1, 10.0, 1.0
            )
        elif condition_type == "trend_break":
            st.sidebar.caption(
                "차트에서 추세선을 그을 두 점의 날짜와 가격을 입력하세요. "
                "(예: 상승 지지선이면 저점 2개, 하락 저항선이면 고점 2개)"
            )
            price_label = {"KR": "가격(원)", "US": "가격($)", "COIN": "가격($)"}[market_code]

            col_a, col_b = st.sidebar.columns(2)
            point1_date = col_a.date_input("점1 날짜", key="trend_p1_date")
            point1_price = col_b.number_input(
                price_label + " (점1)", min_value=0.0, step=1.0, key="trend_p1_price"
            )
            point2_date = col_a.date_input("점2 날짜", key="trend_p2_date")
            point2_price = col_b.number_input(
                price_label + " (점2)", min_value=0.0, step=1.0, key="trend_p2_price"
            )

            new_item["point1_date"] = point1_date.strftime("%Y-%m-%d")
            new_item["point1_price"] = point1_price
            new_item["point2_date"] = point2_date.strftime("%Y-%m-%d")
            new_item["point2_price"] = point2_price

            new_item["trend_direction"] = (
                "above"
                if st.sidebar.radio(
                    "돌파 방향", ["상향 돌파 (저항선 위로)", "하향 이탈 (지지선 아래로)"]
                )
                == "상향 돌파 (저항선 위로)"
                else "below"
            )
            new_item["threshold_pct"] = st.sidebar.number_input(
                "근접 기준(±%, 참고용)", 0.1, 10.0, 1.0
            )

        if st.sidebar.button("이 조건으로 추가"):
            st.session_state.watchlist.append(new_item)
            save_persistent(WATCHLIST_FILE, st.session_state.watchlist)
            st.sidebar.success(f"{display_name} 추가 완료!")
            st.rerun()

# ==================================================================
# 사이드바: 등록된 종목 관리(삭제)
# ==================================================================
if st.session_state.watchlist:
    st.sidebar.header("📋 등록된 종목")
    for i, item in enumerate(st.session_state.watchlist):
        col1, col2 = st.sidebar.columns([3, 1])
        flag = {"KR": "🇰🇷", "US": "🇺🇸", "COIN": "🪙"}.get(item.get("market", "KR"), "🇰🇷")
        col1.write(f"{flag} {item['name']} · {get_condition_label(item)}")
        if col2.button("삭제", key=f"del_{i}"):
            st.session_state.watchlist.pop(i)
            save_persistent(WATCHLIST_FILE, st.session_state.watchlist)
            st.rerun()

if st.sidebar.button("🔄 지금 새로고침"):
    st.rerun()

# ==================================================================
# 메인 화면: 조건 모니터링 표
# 표 부분만 주기적으로 새로고침되도록 fragment로 분리해서,
# 사이드바에서 종목을 입력/추가하는 동안 화면이 멈추지 않게 한다.
# ==================================================================
st.title("📈 관심종목 조건 모니터")


def render_item_chart(item):
    """선택한 종목의 가격 차트와 조건 기준선을 함께 보여준다."""
    market = item.get("market", "KR")
    data_id = get_data_id(item)
    if not data_id:
        st.info("차트를 표시할 데이터가 없어요.")
        return

    df = get_history(data_id, market, 300)
    if df.empty:
        st.info("차트 데이터를 가져오지 못했어요.")
        return

    sub = df[["close"]].reset_index()
    sub.columns = ["date", "close"]

    line = alt.Chart(sub).mark_line(color="#4C78A8").encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("close:Q", title="가격", scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("date:T", title="날짜"), alt.Tooltip("close:Q", title="종가", format=",.2f")],
    )
    layers = [line]

    if item["condition"] == "trend_break":
        # 추세선은 두 점을 지나는 대각선 그대로 그려준다.
        try:
            d1 = datetime.strptime(item["point1_date"], "%Y-%m-%d")
            p1 = item["point1_price"]
            p2 = item["point2_price"]
            d2 = datetime.strptime(item["point2_date"], "%Y-%m-%d")
            days_total = (d2 - d1).days
            if days_total != 0:
                slope = (p2 - p1) / days_total
                trend_df = sub[["date"]].copy()
                trend_df["trend_value"] = trend_df["date"].apply(
                    lambda d: p1 + slope * (d.to_pydatetime().date() - d1.date()).days
                )
                trend_line = alt.Chart(trend_df).mark_line(
                    color="orange", strokeDash=[4, 4]
                ).encode(x="date:T", y="trend_value:Q")
                layers.append(trend_line)
        except (KeyError, ValueError):
            pass
    else:
        ref_val = get_reference_value(item)
        if ref_val is not None:
            ref_df = pd.DataFrame({"y": [ref_val]})
            rule = alt.Chart(ref_df).mark_rule(color="red", strokeDash=[4, 4]).encode(y="y:Q")
            layers.append(rule)

    st.altair_chart(alt.layer(*layers).properties(height=320), use_container_width=True)
    st.caption(f"점선: {get_condition_label(item)} 기준선  ·  외부에서 보기: [{item['name']}]({get_external_chart_url(item)})")


@st.fragment(run_every=AUTO_REFRESH_SECONDS)
def render_watchlist_table():
    watchlist = st.session_state.watchlist
    if not watchlist:
        st.info("왼쪽 사이드바에서 종목을 검색해 추가해보세요.")
        return

    prices = get_current_prices(watchlist)

    rows = []
    for item in watchlist:
        ticker = item["ticker"]
        market = item.get("market", "KR")
        data_id = get_data_id(item)
        price_info = prices.get(data_id, {}) if data_id else {}
        current = price_info.get("price")
        market_status = price_info.get("market_status", "-")
        ref = get_reference_value(item)

        if current is None or ref is None or ref == 0:
            diff_pct = None
        else:
            diff_pct = (current - ref) / ref * 100

        threshold = item.get("threshold_pct", 1.0)
        if diff_pct is None:
            achieved = False
        elif item["condition"] == "prior_high":
            achieved = diff_pct >= 0
        elif item["condition"] == "trend_break":
            if item.get("trend_direction", "above") == "above":
                achieved = diff_pct >= 0  # 추세선 위로 상향 돌파
            else:
                achieved = diff_pct <= 0  # 추세선 아래로 하향 이탈
        else:
            achieved = abs(diff_pct) <= threshold

        # 중복 알림 방지: "미달성 -> 달성"으로 바뀌는 순간에만 알림 발송
        key = f"{market}_{ticker}_{item['condition']}"
        was_achieved = st.session_state.notified.get(key, False)
        if achieved and not was_achieved and diff_pct is not None:
            send_notification(item, current, ref, diff_pct)
        st.session_state.notified[key] = achieved

        rows.append(
            {
                "시장": {"KR": "🇰🇷 국내", "US": "🇺🇸 미국", "COIN": "🪙 코인"}.get(
                    market, "🇰🇷 국내"
                ),
                "종목명": item["name"],
                "현재가": format_price(current, market) if current is not None else "조회 실패",
                "시장상태": "장중" if market_status == "OPEN" else "-",
                "조건": get_condition_label(item),
                "기준값": format_price(ref, market),
                "차이": f"{diff_pct:+.2f}%" if diff_pct is not None else "-",
                "상태": "🔔 조건 도달" if achieved else "관찰중",
                "외부차트": get_external_chart_url(item),
            }
        )

    df_display = pd.DataFrame(rows)
    st.caption("표에서 종목을 클릭하면 아래에 가격 차트가 표시돼요.")
    event = st.dataframe(
        df_display,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "외부차트": st.column_config.LinkColumn("외부차트", display_text="🔗 열기"),
        },
    )
    st.caption(f"마지막 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        selected_item = watchlist[selected_rows[0]]
        st.subheader(f"📊 {selected_item['name']} 차트")
        render_item_chart(selected_item)


render_watchlist_table()
