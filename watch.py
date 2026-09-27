#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / ".watch_state.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

CN_TZ = timezone(timedelta(hours=8))
PRICE_NUMBER = r"([1-9]\d{2,4}(?:\.\d{1,2})?)"

# Order matters: an explicit checkout/final price must beat a coupon or member price.
FINAL_PRICE_RULES = [
    (
        "最终到手/实付价",
        r"(?:最终(?:到手)?价|最终到手|实际支付|实付(?:价)?|到手价?)",
    ),
    ("国补后价", r"(?:国补(?:后|价)|政府补贴后|补贴后)"),
    ("百亿补贴价", r"(?:百亿补贴(?:后|价)?)"),
    ("券后价", r"(?:(?:领|用)券后|券后(?:价)?)"),
    ("满减后价", r"(?:满减后(?:价)?)"),
    ("下单价", r"(?:下单价)"),
    ("拼单价", r"(?:拼单价)"),
    ("PLUS价", r"(?:PLUS(?:会员)?价)"),
    ("会员价", r"(?:会员价)"),
]


@dataclass
class Candidate:
    source: str
    platform: str
    title: str
    price: float
    url: str
    merchant: str = ""
    published_at: str = ""
    reliability: str = "线索"
    page_price: Optional[float] = None
    price_type: str = "页面/API价"
    discount_info: str = ""


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Cache-Control": "no-cache",
        }
    )
    return s


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"alerts": {}, "health": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"alerts": {}, "health": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _price_value(raw: str) -> Optional[float]:
    try:
        value = float(raw.replace(",", ""))
    except (AttributeError, ValueError):
        return None
    if 1000 <= value <= 10000:
        return value
    return None


def _blocked_price_spans(text: str) -> list[tuple[int, int]]:
    """Return numeric spans that are model numbers or discount components."""
    spans: list[tuple[int, int]] = []
    patterns = [
        # The GPU model number is never a price.
        r"(?:RTX\s*)?(5060)\s*Ti",
        # Neither the threshold nor reduction in 满3000减300 is the sale price.
        r"满\s*([1-9]\d{2,4}(?:\.\d{1,2})?)\s*(?:元)?\s*(?:减|返|赠)\s*([1-9]\d{1,4}(?:\.\d{1,2})?)",
        # A coupon face value is a condition, not the product price.
        r"(?:领|叠加?|使用)?\s*([1-9]\d{1,4}(?:\.\d{1,2})?)\s*(?:元)?\s*(?:优惠)?券",
        r"(?:优惠券|券)(?!后)\s*[:：]?\s*([1-9]\d{1,4}(?:\.\d{1,2})?)\s*(?:元)?",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            spans.extend(
                match.span(i)
                for i in range(1, len(match.groups()) + 1)
                if match.group(i) is not None
            )
    return spans


def _is_blocked(span: tuple[int, int], blocked: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and span[1] > start for start, end in blocked)


def _first_usable_price(
    text: str,
    pattern: str,
    blocked: list[tuple[int, int]],
) -> Optional[float]:
    for match in re.finditer(pattern, text, re.I):
        if _is_blocked(match.span(1), blocked):
            continue
        value = _price_value(match.group(1))
        if value is not None:
            return value
    return None


def extract_final_price(text: str) -> tuple[Optional[float], str]:
    """Extract a semantically labelled payable price, in explicit priority order."""
    t = normalize(text).replace(",", "")
    if not t:
        return None, ""

    for price_type, label in FINAL_PRICE_RULES:
        patterns = [
            rf"{label}[^\d¥￥]{{0,12}}[¥￥]?\s*{PRICE_NUMBER}\s*(?:元)?",
            rf"[¥￥]?\s*{PRICE_NUMBER}\s*(?:元)?[^\d]{{0,8}}{label}",
        ]
        for pattern in patterns:
            match = re.search(pattern, t, re.I)
            if match:
                value = _price_value(match.group(1))
                if value is not None:
                    return value, price_type
    return None, ""


def parse_price(text: str, *, allow_bare: bool = True) -> Optional[float]:
    """Parse one safe price while excluding GPU models and discount thresholds."""
    if not text:
        return None
    t = normalize(text).replace(",", "")

    final_price, _ = extract_final_price(t)
    if final_price is not None:
        return final_price

    blocked = _blocked_price_spans(t)
    patterns = [
        rf"[¥￥]\s*{PRICE_NUMBER}",
        rf"(?:页面价|活动价|原价|售价|标价|现价|京东价|商品价)[^\d]{{0,10}}[¥￥]?\s*{PRICE_NUMBER}",
        rf"(?<!\d){PRICE_NUMBER}\s*元(?!券)",
    ]
    if allow_bare:
        patterns.append(rf"(?<!\d){PRICE_NUMBER}(?!\d)")

    for pattern in patterns:
        value = _first_usable_price(t, pattern, blocked)
        if value is not None:
            return value
    return None


def extract_page_price(text: str) -> Optional[float]:
    """Extract a displayed/base price without re-labelling a final price as page price."""
    if not text:
        return None
    t = normalize(text).replace(",", "")
    blocked = _blocked_price_spans(t)

    for _, label in FINAL_PRICE_RULES:
        contextual_patterns = [
            rf"{label}[^\d¥￥]{{0,12}}[¥￥]?\s*{PRICE_NUMBER}\s*(?:元)?",
            rf"[¥￥]?\s*{PRICE_NUMBER}\s*(?:元)?[^\d]{{0,8}}{label}",
        ]
        for pattern in contextual_patterns:
            blocked.extend(match.span(1) for match in re.finditer(pattern, t, re.I))

    patterns = [
        rf"(?:页面价|活动价|原价|售价|标价|现价|京东价|商品价)[^\d]{{0,10}}[¥￥]?\s*{PRICE_NUMBER}",
        rf"[¥￥]\s*{PRICE_NUMBER}",
        rf"(?<!\d){PRICE_NUMBER}\s*元(?!券)",
    ]
    for pattern in patterns:
        value = _first_usable_price(t, pattern, blocked)
        if value is not None:
            return value
    return None


def extract_discount_info(text: str) -> str:
    """Extract concise, de-duplicated discount conditions for notifications."""
    t = normalize(text).replace(",", "")
    if not t:
        return ""

    info: list[str] = []

    def add(value: str) -> None:
        value = re.sub(r"\s+", "", value)
        if value and value not in info:
            info.append(value)

    for match in re.finditer(
        r"满\s*\d+(?:\.\d+)?\s*(?:元)?\s*减\s*\d+(?:\.\d+)?\s*(?:元)?",
        t,
        re.I,
    ):
        add(match.group(0))

    coupon_patterns = [
        r"(?:领|叠加?|使用)?\s*\d+(?:\.\d+)?\s*(?:元)?\s*(?:优惠)?券",
        r"(?:优惠券|券)(?!后)\s*[:：]?\s*\d+(?:\.\d+)?\s*(?:元)?",
    ]
    for pattern in coupon_patterns:
        for match in re.finditer(pattern, t, re.I):
            add(match.group(0))

    if "百亿补贴" in t:
        add("百亿补贴")
    elif "国补" in t:
        match = re.search(r"国补\s*\d+(?:\.\d+)?\s*%", t)
        add(match.group(0) if match else "国补")
    elif "补贴" in t:
        add("补贴")

    keyword_conditions = [
        (r"券后|领券后|用券后", "券后"),
        (r"满减后", "满减后"),
        (r"PLUS(?:会员)?价", "PLUS会员价"),
        (r"会员价", "会员价"),
        (r"拼单价", "拼单价"),
        (r"下单价", "下单价"),
    ]
    for pattern, label in keyword_conditions:
        if re.search(pattern, t, re.I):
            add(label)

    return " + ".join(info)


def extract_offer_details(
    text: str,
    api_price: object = None,
    api_label: str = "API价",
) -> tuple[Optional[float], Optional[float], str, str]:
    """Return final price, page/API price, price type and discount conditions."""
    final_price, price_type = extract_final_price(text)
    page_price = parse_price(str(api_price or ""), allow_bare=True)

    if page_price is None:
        page_price = extract_page_price(text)

    if final_price is None:
        final_price = page_price
        price_type = api_label if final_price is not None else ""

    return final_price, page_price, price_type, extract_discount_info(text)


def is_target_gpu(title: str, cfg: dict) -> bool:
    t = title.lower().replace("-", " ").replace("_", " ")
    if not re.search(r"5060\s*ti", t, re.I):
        return False
    if not re.search(r"\b8\s*g(?:b)?\b|8g(?:b)?", t, re.I):
        return False

    for bad in cfg.get("exclude_keywords", []):
        if bad.lower() in t:
            return False

    if re.search(r"\b16\s*g(?:b)?\b|16g(?:b)?", t, re.I):
        return False
    return True


def platform_from_text(text: str) -> str:
    t = text.lower()
    mapping = [
        ("拼多多", ["拼多多", "pdd", "yangkeduo"]),
        ("京东", ["京东", "jd.com", "京东商城"]),
        ("天猫", ["天猫", "tmall"]),
        ("淘宝", ["淘宝", "taobao"]),
        ("抖音", ["抖音", "douyin"]),
    ]
    for name, keys in mapping:
        if any(k in t for k in keys):
            return name
    return "未知平台"


def parse_smzdm_time(text: str) -> Optional[datetime]:
    text = normalize(text)
    now = now_cn()

    m = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text)
    if m and not re.search(r"\d{1,2}-\d{1,2}", text):
        dt = now.replace(
            hour=int(m.group(1)),
            minute=int(m.group(2)),
            second=0,
            microsecond=0,
        )
        if dt > now + timedelta(minutes=5):
            dt -= timedelta(days=1)
        return dt

    m = re.search(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", text)
    if m:
        return datetime(
            now.year,
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(4)),
            tzinfo=CN_TZ,
        )
    return None


def fetch_smzdm_api(session: requests.Session, cfg: dict) -> list[Candidate]:
    """Use SMZDM's JSON search endpoint first; it is less brittle than HTML."""
    out: list[Candidate] = []
    seen: set[str] = set()
    freshness = int(cfg.get("smzdm_fresh_minutes", 180))
    headers = {
        "User-Agent": UA,
        "Referer": "https://search.smzdm.com/",
        "Accept": "application/json,text/plain,*/*",
    }

    for keyword in cfg.get("search_keywords", []):
        r = session.get(
            "https://api.smzdm.com/v1/list",
            params={
                "keyword": keyword,
                "category_id": "",
                "brand_id": "",
                "mall_id": "",
                "order": "time",
                "limit": 30,
                "offset": 0,
            },
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        rows = ((data or {}).get("data") or {}).get("rows") or []
        print(f"[smzdm-api] keyword={keyword!r}, rows={len(rows)}")

        for row in rows:
            channel_id = str(row.get("article_channel_id") or "")
            if channel_id and channel_id != "2":
                continue

            title = normalize(str(row.get("article_title") or ""))
            if not title or not is_target_gpu(title, cfg):
                continue

            href = str(row.get("article_url") or "").strip()
            if not href or href in seen:
                continue

            raw_price = row.get("article_price")
            deal_text = normalize(
                " ".join(
                    str(row.get(key) or "")
                    for key in (
                        "article_title",
                        "article_subtitle",
                        "article_content",
                        "article_tips",
                        "article_mall",
                    )
                )
            )
            price, page_price, price_type, discount_info = extract_offer_details(
                deal_text,
                raw_price,
                "API价",
            )
            if price is None:
                continue

            published = None
            raw_ts = row.get("publish_date_lt")
            try:
                if raw_ts not in (None, ""):
                    published = datetime.fromtimestamp(int(float(raw_ts)), CN_TZ)
            except Exception:
                published = None

            if published is None:
                time_sort = normalize(str(row.get("time_sort") or ""))
                if time_sort:
                    try:
                        published = datetime.strptime(
                            time_sort, "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=CN_TZ)
                    except Exception:
                        published = None

            if published and (now_cn() - published) > timedelta(minutes=freshness):
                continue

            mall = normalize(str(row.get("article_mall") or ""))
            platform = platform_from_text(mall + " " + title + " " + href)
            out.append(
                Candidate(
                    source="什么值得买API",
                    platform=platform,
                    title=title,
                    price=price,
                    url=href,
                    merchant=mall,
                    published_at=published.isoformat() if published else "",
                    reliability="JSON搜索线索",
                    page_price=page_price,
                    price_type=price_type,
                    discount_info=discount_info,
                )
            )
            seen.add(href)

    return out


def fetch_smzdm(session: requests.Session, cfg: dict) -> list[Candidate]:
    """API first; fall back to public search HTML if it fails or yields nothing."""
    try:
        out = fetch_smzdm_api(session, cfg)
        if out:
            return out
    except Exception as e:
        print(f"[smzdm-api] failed: {e}", file=sys.stderr)

    return fetch_smzdm_html(session, cfg)


def fetch_smzdm_html(session: requests.Session, cfg: dict) -> list[Candidate]:
    cookie = os.getenv("SMZDM_COOKIE", "").strip()
    headers = {
        "Referer": "https://search.smzdm.com/",
        "User-Agent": UA,
    }
    if cookie:
        headers["Cookie"] = cookie

    out: list[Candidate] = []
    seen: set[str] = set()
    freshness = int(cfg.get("smzdm_fresh_minutes", 180))

    for keyword in cfg.get("search_keywords", []):
        url = (
            "https://search.smzdm.com/?c=home&s="
            + quote(keyword)
            + "&order=time&v=a&mx_v=a"
        )
        r = session.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select(".feed-row-wide")

        for row in rows[:30]:
            title_links = row.select(".feed-block-title a")
            title = normalize(" ".join(a.get_text(" ", strip=True) for a in title_links))
            if not title or not is_target_gpu(title, cfg):
                continue

            href = ""
            for a in title_links:
                h = a.get("href")
                if h:
                    href = urljoin("https://www.smzdm.com/", h)
                    break
            if not href or href in seen:
                continue

            full_text = normalize(row.get_text(" ", strip=True))
            price, page_price, price_type, discount_info = extract_offer_details(
                full_text,
                None,
                "页面价",
            )
            if price is None:
                continue

            extras = normalize(
                " ".join(x.get_text(" ", strip=True) for x in row.select(".feed-block-extras"))
            )
            published = parse_smzdm_time(extras)
            if published and (now_cn() - published) > timedelta(minutes=freshness):
                continue

            platform = platform_from_text(full_text)
            out.append(
                Candidate(
                    source="什么值得买HTML",
                    platform=platform,
                    title=title,
                    price=price,
                    url=href,
                    merchant="",
                    published_at=published.isoformat() if published else "",
                    reliability="新优惠线索",
                    page_price=page_price,
                    price_type=price_type,
                    discount_info=discount_info,
                )
            )
            seen.add(href)

    return out


def fetch_jd(session: requests.Session, cfg: dict) -> list[Candidate]:
    cookie = os.getenv("JD_COOKIE", "").strip()
    headers = {
        "Referer": "https://www.jd.com/",
        "User-Agent": UA,
    }
    if cookie:
        headers["Cookie"] = cookie

    out: list[Candidate] = []
    seen: set[str] = set()

    for keyword in cfg.get("search_keywords", []):
        url = (
            "https://search.jd.com/Search?keyword="
            + quote(keyword)
            + "&enc=utf-8&wq="
            + quote(keyword)
        )
        r = session.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for item in soup.select("li.gl-item")[:60]:
            sku = item.get("data-sku", "")
            name_node = item.select_one(".p-name em") or item.select_one(".p-name")
            price_node = item.select_one(".p-price i")
            shop_node = item.select_one(".p-shop a") or item.select_one(".p-shop")
            link_node = item.select_one(".p-name a")
            if not (name_node and price_node and link_node):
                continue

            title = normalize(name_node.get_text(" ", strip=True))
            if not is_target_gpu(title, cfg):
                continue

            listed_price = parse_price(
                price_node.get_text(" ", strip=True),
                allow_bare=True,
            )
            full_text = normalize(item.get_text(" ", strip=True))
            price, page_price, price_type, discount_info = extract_offer_details(
                full_text,
                listed_price,
                "页面价",
            )
            if price is None:
                continue

            href = link_node.get("href", "")
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = urljoin("https://item.jd.com/", href)
            if not href and sku:
                href = f"https://item.jd.com/{sku}.html"
            if not href or href in seen:
                continue

            merchant = normalize(shop_node.get_text(" ", strip=True)) if shop_node else ""
            out.append(
                Candidate(
                    source="京东搜索",
                    platform="京东",
                    title=title,
                    price=price,
                    url=href,
                    merchant=merchant,
                    published_at=now_cn().isoformat(),
                    reliability="商品搜索页实时价",
                    page_price=page_price,
                    price_type=price_type,
                    discount_info=discount_info,
                )
            )
            seen.add(href)

    return out


def fetch_direct_urls(session: requests.Session, cfg: dict) -> list[Candidate]:
    """Watch optional direct URLs with generic or configured price extraction."""
    out: list[Candidate] = []
    for item in cfg.get("direct_urls", []):
        url = item.get("url", "").strip()
        if not url:
            continue
        try:
            r = session.get(url, timeout=20, allow_redirects=True)
            r.raise_for_status()
            text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
            if any(x in text for x in ["商品已售罄", "已下架", "商品不存在"]):
                continue

            title = item.get("name", "") or text[:300]
            title_regex = item.get("title_regex")
            if title_regex and not re.search(title_regex, text, re.I):
                continue

            price, page_price, price_type, discount_info = extract_offer_details(
                text,
                None,
                "页面价",
            )
            if item.get("price_regex"):
                match = re.search(item["price_regex"], text, re.I | re.S)
                if match:
                    configured_price = parse_price(match.group(1), allow_bare=True)
                    if configured_price is not None:
                        price = configured_price
                        price_type = item.get("price_type", "配置价格")
            if price is None:
                continue

            out.append(
                Candidate(
                    source="直链监控",
                    platform=item.get("platform", platform_from_text(url + " " + text)),
                    title=normalize(item.get("name", title)),
                    price=price,
                    url=r.url,
                    merchant=item.get("merchant", ""),
                    published_at=now_cn().isoformat(),
                    reliability="直链页面价",
                    page_price=page_price,
                    price_type=price_type,
                    discount_info=discount_info,
                )
            )
        except Exception as e:
            print(f"[direct] {url} failed: {e}", file=sys.stderr)
    return out


def threshold_for(candidate: Candidate, cfg: dict) -> float:
    t = candidate.title.lower()
    for brand in cfg.get("good_brands", []):
        if brand.lower() in t:
            return float(cfg["thresholds"]["good_brand"])
    return float(cfg["thresholds"]["normal"])


def should_alert(candidate: Candidate, cfg: dict) -> bool:
    return candidate.price <= threshold_for(candidate, cfg)


def alert_key(c: Candidate) -> str:
    base = c.url.split("#", 1)[0]
    return f"{c.platform}|{base}"


def dedupe_ok(c: Candidate, state: dict, cfg: dict) -> bool:
    key = alert_key(c)
    prev = state.get("alerts", {}).get(key)
    if not prev:
        return True

    cooldown_h = float(cfg.get("same_price_cooldown_hours", 12))
    price_drop = float(cfg.get("realert_price_drop", 30))
    prev_price = float(prev.get("price", 1e9))
    prev_time = prev.get("time", "")

    if c.price <= prev_price - price_drop:
        return True

    try:
        dt = datetime.fromisoformat(prev_time)
        if now_cn() - dt >= timedelta(hours=cooldown_h):
            return True
    except Exception:
        return True
    return False


def format_message(c: Candidate, cfg: dict) -> str:
    threshold = threshold_for(c, cfg)
    age = ""
    if c.published_at:
        try:
            pub = datetime.fromisoformat(c.published_at)
            mins = int((now_cn() - pub).total_seconds() / 60)
            if mins >= 0:
                age = f"\n发布时间：约 {mins} 分钟前"
        except Exception:
            pass

    merchant = f"\n店铺：{c.merchant}" if c.merchant else ""
    page_price = (
        f"¥{c.page_price:.0f}" if c.page_price is not None else "未提供"
    )
    discount_info = c.discount_info or "未识别（以结算页为准）"
    return (
        "🔥【RTX 5060 Ti 8G 好价】\n"
        f"型号：{c.title}\n"
        f"平台：{c.platform}{merchant}\n"
        f"页面/API价：{page_price}\n"
        f"最终到手价：¥{c.price:.0f}\n"
        f"价格类型：{c.price_type or '页面/API价'}\n"
        f"优惠条件：{discount_info}\n"
        f"提醒线：≤¥{threshold:.0f}\n"
        f"来源：{c.source}（{c.reliability}）"
        f"{age}\n"
        f"链接：{c.url}\n\n"
        "⚠️ 电商券、地区补贴和库存可能随账号变化；下单前请以结算页实付为准。"
    )


def send_feishu(text: str) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        raise RuntimeError("缺少 FEISHU_WEBHOOK Secret")
    resp = requests.post(
        webhook,
        json={"msg_type": "text", "content": {"text": text}},
        timeout=15,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
        code = data.get("code", data.get("StatusCode", 0))
        if code not in (0, "0", None):
            raise RuntimeError(f"飞书返回异常：{data}")
    except ValueError:
        pass


def send_test() -> None:
    send_feishu(
        "✅【5060 Ti 8G 价格监控测试】\n"
        "GitHub Actions → 飞书通知已打通。\n"
        f"测试时间：{now_cn().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）"
    )


def main() -> int:
    cfg = load_config()
    state = load_state()

    if os.getenv("TEST_NOTIFY", "").lower() in {"1", "true", "yes"}:
        send_test()
        print("Test notification sent.")
        return 0

    session = make_session()
    candidates: list[Candidate] = []
    errors: list[str] = []

    sources = [
        ("smzdm", fetch_smzdm),
        ("jd", fetch_jd),
        ("direct", fetch_direct_urls),
    ]
    for name, func in sources:
        if not cfg.get("sources", {}).get(name, True):
            continue
        try:
            got = func(session, cfg)
            print(f"[{name}] {len(got)} candidates")
            candidates.extend(got)
        except Exception as e:
            msg = f"[{name}] failed: {e}"
            errors.append(msg)
            print(msg, file=sys.stderr)

    uniq: dict[tuple[str, int], Candidate] = {}
    for c in candidates:
        if c.source != "直链监控" and not is_target_gpu(c.title, cfg):
            continue
        uniq[(c.url, int(c.price * 100))] = c
    candidates = list(uniq.values())
    candidates.sort(key=lambda x: x.price)

    hit_count = 0
    state_changed = False

    for c in candidates:
        limit = threshold_for(c, cfg)
        print(f"CHECK {c.price:.0f} <= {limit:.0f}? {c.platform} | {c.title[:80]}")
        if not should_alert(c, cfg):
            continue
        if not dedupe_ok(c, state, cfg):
            print("  -> skipped by dedupe")
            continue

        send_feishu(format_message(c, cfg))
        key = alert_key(c)
        state.setdefault("alerts", {})[key] = {
            "price": c.price,
            "time": now_cn().isoformat(),
            "title": c.title,
            "url": c.url,
        }
        hit_count += 1
        state_changed = True
        print("  -> ALERT SENT")

        if hit_count >= int(cfg.get("max_alerts_per_run", 3)):
            break

    keep_days = int(cfg.get("state_retention_days", 30))
    cutoff = now_cn() - timedelta(days=keep_days)
    for key, item in list(state.get("alerts", {}).items()):
        try:
            if datetime.fromisoformat(item.get("time", "")) < cutoff:
                del state["alerts"][key]
                state_changed = True
        except Exception:
            pass

    if state_changed:
        save_state(state)

    enabled_sources = [s for s in sources if cfg.get("sources", {}).get(s[0], True)]
    if errors and len(errors) == len(enabled_sources):
        print("WARNING: all enabled sources failed", file=sys.stderr)
        return 2

    print(f"Done. candidates={len(candidates)}, alerts={hit_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

