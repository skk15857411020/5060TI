#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import html
import json
import math
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
HISTORY_PATH = BASE_DIR / "data" / "price_history.csv"
DOCS_DIR = BASE_DIR / "docs"
HOURLY_PATH = DOCS_DIR / "hourly_min.csv"
CHART_PATH = DOCS_DIR / "price_chart.svg"
INDEX_PATH = DOCS_DIR / "index.html"

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
    price_verified: bool = True
    price_check: str = ""


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


def gpu_variant(text: str) -> str:
    """Return 8G or 16G for an unambiguous RTX 5060 Ti listing."""
    t = normalize(text).lower().replace("-", " ").replace("_", " ")
    has_8g = bool(
        re.search(
            r"(?<!\d)8\s*g(?:b)?(?!\d)|\bo8g\b|显存\s*8\s*g(?:b)?",
            t,
            re.I,
        )
    )
    has_16g = bool(
        re.search(
            r"(?<!\d)16\s*g(?:b)?(?!\d)|\bo16g\b|显存\s*16\s*g(?:b)?",
            t,
            re.I,
        )
    )
    # Mixed 8G/16G SKU pages are unsafe for price attribution.
    if has_8g == has_16g:
        return ""
    return "16G" if has_16g else "8G"


def _has_non_ti_5060(text: str) -> bool:
    return bool(re.search(r"(?<!\d)5060(?![\s_-]*ti|\d)", text, re.I))


def _has_other_gpu_model(text: str) -> bool:
    return any(
        model != "5060"
        for model in re.findall(r"(?<!\d)(?:rtx\s*)?([345]0\d{2})(?!\d)", text, re.I)
    )


def is_target_gpu(text: str, cfg: dict) -> bool:
    """Match an unambiguous RTX 5060 Ti 8GB or 16GB listing."""
    t = normalize(text).lower().replace("-", " ").replace("_", " ")
    if not re.search(r"(?:rtx\s*)?5060\s*ti|5060ti", t, re.I):
        return False

    if _has_non_ti_5060(t):
        return False

    if _has_other_gpu_model(t):
        return False

    if re.search(r"整机|台式机|笔记本|显卡坞|主机", t):
        return False

    # Reject generic mixed-model SKU pages such as "RTX5060/5060Ti16G";
    # the displayed low price may belong to the non-Ti option.
    if re.search(
        r"5060\s*/\s*(?:rtx\s*)?5060\s*ti|"
        r"5060\s*(?:和|及|与|\+)\s*(?:rtx\s*)?5060\s*ti",
        t,
        re.I,
    ):
        return False

    if not gpu_variant(t):
        return False

    for bad in cfg.get("exclude_keywords", []):
        if bad.lower() in t:
            return False
    return True


def _smzdm_spec_texts(row: dict) -> list[str]:
    """Keep each SKU/option's model and price together, without joining siblings."""
    texts: list[str] = []

    def visit(value: object, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, str):
            value = value.strip()
            if value.startswith(("{", "[")):
                try:
                    visit(json.loads(value), depth + 1)
                    return
                except (ValueError, TypeError):
                    pass
            if value:
                texts.append(value)
        elif isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, dict):
            scalar = [
                f"价格{v}元" if re.search(r"price|价", str(k), re.I) else str(v)
                for k, v in value.items()
                if isinstance(v, (str, int, float))
            ]
            if (
                scalar
                and not any(isinstance(v, (dict, list)) for v in value.values())
                and any(re.search(r"price|价", str(k), re.I) for k in value)
            ):
                texts.append(" ".join(scalar))
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    visit(nested, depth + 1)

    for key, value in row.items():
        if re.search(r"sku|spec|option|variant|规格|选项|款式", str(key), re.I):
            visit(value)
    return texts


def verify_smzdm_offer(
    row: dict, cfg: dict
) -> tuple[Optional[float], Optional[float], str, str, bool, str]:
    """Accept only a price explicitly paired with the target model and memory size."""
    title = normalize(str(row.get("article_title") or ""))
    variant = gpu_variant(title)
    fields = [("title", title)]
    fields.extend(
        (key, str(row.get(key) or "").replace(title, ""))
        for key in ("article_subtitle", "article_content", "article_tips")
    )
    fields.extend(("spec", item) for item in _smzdm_spec_texts(row))
    raw_price = row.get("article_price")
    if isinstance(raw_price, str):
        fields.append(("article_price", raw_price))

    bound: list[tuple[float, Optional[float], str, str]] = []
    other_variant_prices: set[float] = set()
    for source, field in fields:
        plain = BeautifulSoup(field, "html.parser").get_text(" ", strip=True)
        for clause in re.split(r"[，,。；;|\n\r/]+", plain):
            clause = normalize(clause)
            if not clause:
                continue
            offer, base, kind, discount = extract_offer_details(clause)
            if offer is None:
                continue
            if _has_non_ti_5060(clause) or (
                re.search(r"5060\s*ti", clause, re.I) and gpu_variant(clause) != variant
            ):
                other_variant_prices.add(offer)
                continue
            if not is_target_gpu(clause, cfg) or gpu_variant(clause) != variant:
                continue
            # A floor price on a multi-option listing does not identify the SKU.
            if re.search(r"(?:低至|最低|起步|[¥￥]?\s*\d{3,5}(?:\.\d+)?\s*元?起)", clause):
                continue
            # A headline can advertise a low price for a different default
            # option. Require pricing evidence from the deal details or SKU.
            if source != "title":
                bound.append((offer, base, kind, discount))

    safe_bound = [item for item in bound if item[0] not in other_variant_prices]
    if safe_bound:
        # Prefer an explicitly labelled payable price over an ordinary list price.
        safe_bound.sort(
            key=lambda item: (item[2] not in {r[0] for r in FINAL_PRICE_RULES}, item[0])
        )
        price, _, kind, discount = safe_bound[0]
        api_base = parse_price(str(raw_price or ""), allow_bare=True)
        if api_base is not None and api_base < price:
            api_base = None
        return price, api_base, kind or "规格标价", discount, True, "规格与价格同段"
    if bound:
        reason = "同价也对应其他规格"
    else:
        reason = "未找到与5060 Ti具体显存规格同段的明确价格"

    # Keep the lead for diagnostics, but do not use it for alerts or new chart points.
    lead_text = normalize(" ".join(value for _, value in fields))
    price, page_price, kind, discount = extract_offer_details(lead_text, raw_price)
    return price, page_price, kind, discount, False, reason


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


def build_search_queries(cfg: dict) -> list[str]:
    """Expand base GPU queries with marketplace names to surface more non-JD deals."""
    bases = [normalize(str(x)) for x in cfg.get("search_keywords", []) if normalize(str(x))]
    markets = [normalize(str(x)) for x in cfg.get("marketplaces", []) if normalize(str(x))]
    queries: list[str] = []
    seen: set[str] = set()

    for q in bases:
        if q not in seen:
            queries.append(q)
            seen.add(q)

    # Use one representative query for each VRAM variant on each marketplace.
    seed_bases: list[str] = []
    for wanted in ("8G", "16G"):
        seed = next((base for base in bases if gpu_variant(base) == wanted), "")
        if seed:
            seed_bases.append(seed)
    if not seed_bases:
        seed_bases = ["RTX 5060 Ti 8G", "RTX 5060 Ti 16G"]
    for market in markets:
        for base in seed_bases:
            q = f"{base} {market}"
            if q not in seen:
                queries.append(q)
                seen.add(q)

    return queries


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

    page_size = max(10, min(int(cfg.get("smzdm_page_size", 30)), 100))
    pages = max(1, min(int(cfg.get("smzdm_pages", 3)), 5))

    for keyword in build_search_queries(cfg):
        for page in range(pages):
            offset = page * page_size
            r = session.get(
                "https://api.smzdm.com/v1/list",
                params={
                    "keyword": keyword,
                    "category_id": "",
                    "brand_id": "",
                    "mall_id": "",
                    "order": "time",
                    "limit": page_size,
                    "offset": offset,
                },
                headers=headers,
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            rows = ((data or {}).get("data") or {}).get("rows") or []
            print(
                f"[smzdm-api] keyword={keyword!r}, page={page + 1}/{pages}, "
                f"offset={offset}, rows={len(rows)}"
            )
            if not rows:
                break

            stats = {
                "channel": 0,
                "target": 0,
                "price": 0,
                "low_confidence": 0,
                "fresh": 0,
                "accepted": 0,
            }
            allowed_channels = {
                str(x) for x in cfg.get("smzdm_channel_ids", []) if str(x)
            }

            for row in rows:
                channel_id = str(row.get("article_channel_id") or "")
                if allowed_channels and channel_id and channel_id not in allowed_channels:
                    stats["channel"] += 1
                    continue

                title = normalize(str(row.get("article_title") or ""))
                if not title or not is_target_gpu(title, cfg):
                    stats["target"] += 1
                    continue

                href = str(row.get("article_url") or "").strip()
                if not href or href in seen:
                    continue

                raw_price = row.get("article_price")
                (
                    price, page_price, price_type, discount_info,
                    verified, price_check,
                ) = verify_smzdm_offer(row, cfg)
                if price is None:
                    stats["price"] += 1
                    continue
                if not verified:
                    stats["low_confidence"] += 1
                    print(
                        f"[smzdm-price-check] low_confidence: {price_check}; "
                        f"article_price={str(raw_price)[:80]!r}; title={title[:100]!r}"
                    )

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
                    stats["fresh"] += 1
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
                        reliability="JSON搜索线索" if verified else "低可信价格线索",
                        page_price=page_price,
                        price_type=price_type,
                        discount_info=discount_info,
                        price_verified=verified,
                        price_check=price_check,
                    )
                )
                seen.add(href)
                stats["accepted"] += 1

            print(
                "[smzdm-filter] "
                f"keyword={keyword!r}, page={page + 1}, "
                f"accepted={stats['accepted']}, "
                f"target_filtered={stats['target']}, "
                f"price_filtered={stats['price']}, "
                f"low_confidence={stats['low_confidence']}, "
                f"stale_filtered={stats['fresh']}, "
                f"channel_filtered={stats['channel']}"
            )

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
            offer_row = {
                "article_title": title,
                "article_content": full_text.replace(title, "", 1),
            }
            (
                price, page_price, price_type, discount_info,
                verified, price_check,
            ) = verify_smzdm_offer(offer_row, cfg)
            if price is None:
                continue
            if not verified:
                print(
                    f"[smzdm-price-check] low_confidence: {price_check}; "
                    f"title={title[:100]!r}"
                )

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
                    reliability="新优惠线索" if verified else "低可信价格线索",
                    page_price=page_price,
                    price_type=price_type,
                    discount_info=discount_info,
                    price_verified=verified,
                    price_check=price_check,
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



def _read_history_rows() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        with HISTORY_PATH.open("r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        print(f"[history] read failed: {e}", file=sys.stderr)
        return []


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def record_price_history(candidates: list[Candidate], keep_days: int = 30) -> list[dict]:
    """Persist the lowest observed price per variant and platform every sampling run."""
    rows = _read_history_rows()
    cutoff = now_cn() - timedelta(days=max(1, keep_days))
    kept: list[dict] = []

    for row in rows:
        try:
            dt = datetime.fromisoformat(row.get("sampled_at", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CN_TZ)
            if dt >= cutoff:
                # All history written before 16G monitoring was enabled is 8G.
                row["variant"] = row.get("variant") or gpu_variant(row.get("title", "")) or "8G"
                kept.append(row)
        except Exception:
            continue

    best: dict[tuple[str, str], Candidate] = {}
    for cand in candidates:
        variant = gpu_variant(cand.title)
        if not variant:
            continue
        platform = cand.platform or "未知平台"
        key = (variant, platform)
        prev = best.get(key)
        if prev is None or cand.price < prev.price:
            best[key] = cand

    sampled_at = now_cn().replace(second=0, microsecond=0).isoformat()
    if best:
        keys = set(best)
        kept = [
            row
            for row in kept
            if not (
                row.get("sampled_at") == sampled_at
                and ((row.get("variant") or "8G"), row.get("platform") or "未知平台") in keys
            )
        ]

    for (variant, platform), cand in sorted(best.items()):
        kept.append(
            {
                "sampled_at": sampled_at,
                "variant": variant,
                "platform": platform,
                "price": f"{cand.price:.2f}",
                "page_price": (
                    f"{cand.page_price:.2f}" if cand.page_price is not None else ""
                ),
                "price_type": cand.price_type,
                "title": cand.title,
                "source": cand.source,
                "merchant": cand.merchant,
                "url": cand.url,
            }
        )

    kept.sort(
        key=lambda r: (
            r.get("sampled_at", ""),
            r.get("variant", "8G"),
            r.get("platform", ""),
        )
    )
    _write_csv(
        HISTORY_PATH,
        [
            "sampled_at",
            "variant",
            "platform",
            "price",
            "page_price",
            "price_type",
            "title",
            "source",
            "merchant",
            "url",
        ],
        kept,
    )
    print(f"[history] samples={len(kept)}, series_this_run={len(best)}")
    return kept


def build_hourly_min(history_rows: list[dict]) -> list[dict]:
    """Aggregate samples into one minimum price per variant/platform/hour."""
    grouped: dict[tuple[str, str, str], dict] = {}

    for row in history_rows:
        try:
            dt = datetime.fromisoformat(row.get("sampled_at", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CN_TZ)
            hour = dt.replace(minute=0, second=0, microsecond=0).isoformat()
            variant = row.get("variant") or gpu_variant(row.get("title", "")) or "8G"
            platform = row.get("platform") or "未知平台"
            price = float(row.get("price", ""))
        except Exception:
            continue

        key = (hour, variant, platform)
        prev = grouped.get(key)
        if prev is None or price < float(prev["price"]):
            grouped[key] = {
                "hour": hour,
                "variant": variant,
                "platform": platform,
                "price": f"{price:.2f}",
                "title": row.get("title", ""),
                "source": row.get("source", ""),
                "url": row.get("url", ""),
            }

    rows = [grouped[key] for key in sorted(grouped)]
    _write_csv(
        HOURLY_PATH,
        ["hour", "variant", "platform", "price", "title", "source", "url"],
        rows,
    )
    return rows


def _platform_display_name(name: str) -> str:
    return {
        "京东": "JD",
        "拼多多": "PDD",
        "淘宝": "Taobao",
        "天猫": "Tmall",
        "抖音": "Douyin",
        "未知平台": "Other",
    }.get(name, name)


def write_price_chart_svg(hourly_rows: list[dict]) -> None:
    """Generate a dependency-free SVG trend chart from hourly minimum prices."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    width, height = 1200, 620
    left, right, top, bottom = 92, 38, 76, 78
    plot_w = width - left - right
    plot_h = height - top - bottom

    parsed: list[tuple[datetime, str, float]] = []
    for row in hourly_rows:
        try:
            dt = datetime.fromisoformat(row["hour"])
            variant = row.get("variant") or "8G"
            series = f"{variant} {_platform_display_name(row['platform'])}"
            parsed.append((dt, series, float(row["price"])))
        except Exception:
            continue

    if not parsed:
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="white"/>
<text x="{width/2}" y="{height/2}" text-anchor="middle" font-family="Arial, sans-serif" font-size="28" fill="#555">RTX 5060 Ti 8G / 16G price trend — waiting for samples</text>
</svg>"""
        CHART_PATH.write_text(svg, encoding="utf-8")
        return

    times = [x[0].timestamp() for x in parsed]
    prices = [x[2] for x in parsed]
    min_t, max_t = min(times), max(times)
    min_p, max_p = min(prices), max(prices)

    y_low = max(0.0, math.floor((min_p - 100) / 100) * 100)
    y_high = math.ceil((max_p + 100) / 100) * 100
    if y_high <= y_low:
        y_high = y_low + 200

    def sx(ts: float) -> float:
        if max_t == min_t:
            return left + plot_w / 2
        return left + (ts - min_t) / (max_t - min_t) * plot_w

    def sy(price: float) -> float:
        return top + (y_high - price) / (y_high - y_low) * plot_h

    colors = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="60" y="38" font-family="Arial, sans-serif" font-size="26" font-weight="700" fill="#111827">RTX 5060 Ti 8G / 16G — 30 Day Price Trend</text>',
        '<text x="60" y="61" font-family="Arial, sans-serif" font-size="14" fill="#6b7280">5-minute checks, charted as hourly minimum price</text>',
    ]

    for i in range(6):
        value = y_low + (y_high - y_low) * i / 5
        y = sy(value)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e5e7eb" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left-12}" y="{y+5:.1f}" text-anchor="end" font-family="Arial, sans-serif" font-size="13" fill="#6b7280">¥{value:.0f}</text>'
        )

    parts.append(
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#9ca3af" stroke-width="1"/>'
    )
    parts.append(
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#9ca3af" stroke-width="1"/>'
    )

    tick_count = min(6, max(2, len(set(times))))
    for i in range(tick_count):
        ts = min_t if tick_count == 1 else min_t + (max_t - min_t) * i / (tick_count - 1)
        x = sx(ts)
        label = datetime.fromtimestamp(ts, CN_TZ).strftime("%m-%d")
        parts.append(
            f'<text x="{x:.1f}" y="{height-bottom+28}" text-anchor="middle" font-family="Arial, sans-serif" font-size="13" fill="#6b7280">{label}</text>'
        )

    platforms = sorted({x[1] for x in parsed})
    legend_x = 620
    for idx, platform in enumerate(platforms):
        color = colors[idx % len(colors)]
        rows = sorted(
            [(dt, price) for dt, p, price in parsed if p == platform],
            key=lambda x: x[0],
        )
        pts = " ".join(
            f"{sx(dt.timestamp()):.1f},{sy(price):.1f}" for dt, price in rows
        )
        if len(rows) >= 2:
            parts.append(
                f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>'
            )
        elif rows:
            x = sx(rows[0][0].timestamp())
            y = sy(rows[0][1])
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}"/>')

        lx = legend_x + (idx % 4) * 130
        ly = 35 + (idx // 4) * 24
        parts.append(
            f'<line x1="{lx}" y1="{ly}" x2="{lx+24}" y2="{ly}" stroke="{color}" stroke-width="3"/>'
        )
        parts.append(
            f'<text x="{lx+31}" y="{ly+5}" font-family="Arial, sans-serif" font-size="13" fill="#374151">{html.escape(_platform_display_name(platform))}</text>'
        )

    parts.append(
        f'<text x="{width/2}" y="{height-18}" text-anchor="middle" font-family="Arial, sans-serif" font-size="12" fill="#9ca3af">Hourly minimum from 5-minute samples · Beijing time</text>'
    )
    parts.append("</svg>")
    CHART_PATH.write_text("\n".join(parts), encoding="utf-8")


def write_dashboard(hourly_rows: list[dict]) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    if hourly_rows:
        latest_hour = max(row["hour"] for row in hourly_rows)
        latest = sorted(
            [row for row in hourly_rows if row["hour"] == latest_hour],
            key=lambda r: float(r["price"]),
        )
        overall = min(hourly_rows, key=lambda r: float(r["price"]))
        latest_label = datetime.fromisoformat(latest_hour).strftime("%Y-%m-%d %H:00")
        rows_html = "\n".join(
            "<tr>"
            f"<td>{html.escape(row.get('variant') or '8G')}</td>"
            f"<td>{html.escape(row['platform'])}</td>"
            f"<td>¥{float(row['price']):.0f}</td>"
            f"<td>{html.escape(row.get('title',''))}</td>"
            f"<td><a href=\"{html.escape(row.get('url',''), quote=True)}\">查看</a></td>"
            "</tr>"
            for row in latest
        )
        min_text = (
            f"{html.escape(overall.get('variant') or '8G')} · "
            f"¥{float(overall['price']):.0f} · "
            f"{html.escape(overall['platform'])} · "
            f"{datetime.fromisoformat(overall['hour']).strftime('%Y-%m-%d %H:00')}"
        )
    else:
        latest_label = "等待首批数据"
        rows_html = '<tr><td colspan="5">暂无价格数据</td></tr>'
        min_text = "暂无"

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RTX 5060 Ti 8G / 16G 价格走势</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;background:#f5f7fb;color:#111827}}
main{{max-width:1180px;margin:32px auto;padding:0 18px}}
.card{{background:white;border-radius:16px;padding:22px;margin-bottom:18px;box-shadow:0 6px 24px rgba(0,0,0,.06)}}
h1{{margin:0 0 8px}} .muted{{color:#6b7280}}
img{{width:100%;height:auto;display:block}}
table{{width:100%;border-collapse:collapse}} th,td{{padding:10px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}
a{{color:#2563eb;text-decoration:none}}
.metric{{font-size:28px;font-weight:700}}
</style>
</head>
<body>
<main>
<div class="card">
<h1>RTX 5060 Ti 8G / 16G 价格走势</h1>
<div class="muted">每 5 分钟采集一次；走势图使用每个平台每小时最低价。原始 5 分钟数据保存在 GitHub Actions artifact 中。</div>
</div>
<div class="card"><img src="price_chart.svg" alt="30天价格走势图"></div>
<div class="card">
<div class="muted">近30天最低（8G / 16G 合并）</div>
<div class="metric">{min_text}</div>
</div>
<div class="card">
<h2>最近一小时最低价</h2>
<div class="muted">{latest_label}（北京时间）</div>
<table>
<thead><tr><th>规格</th><th>平台</th><th>最低价</th><th>商品</th><th>链接</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
<p><a href="hourly_min.csv">下载小时聚合 CSV</a></p>
</div>
</main>
</body>
</html>"""
    INDEX_PATH.write_text(page, encoding="utf-8")


def threshold_for(candidate: Candidate, cfg: dict) -> float:
    variant = gpu_variant(candidate.title)
    table = cfg.get("thresholds", {}).get(variant, {})
    if not table:
        return 0.0

    t = candidate.title.lower()
    for brand in cfg.get("good_brands", []):
        if brand.lower() in t:
            return float(table["good_brand"])
    return float(table["normal"])


def should_alert(candidate: Candidate, cfg: dict) -> bool:
    if not candidate.price_verified:
        return False
    if candidate.price > threshold_for(candidate, cfg):
        return False

    # Keep a longer window for chart/history, but do not alert on stale deal posts.
    if candidate.published_at and candidate.source.startswith("什么值得买"):
        try:
            published = datetime.fromisoformat(candidate.published_at)
            max_age = int(cfg.get("alert_fresh_minutes", 240))
            if now_cn() - published > timedelta(minutes=max_age):
                return False
        except Exception:
            pass
    return True


def alert_key(c: Candidate) -> str:
    base = c.url.split("#", 1)[0]
    return f"{gpu_variant(c.title)}|{c.platform}|{base}"


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
    variant = gpu_variant(c.title) or "未知显存"
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
        f"🔥【RTX 5060 Ti {variant} 好价】\n"
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
        "✅【5060 Ti 8G / 16G 价格监控测试】\n"
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
        key = (c.url, int(c.price * 100))
        if key not in uniq or c.price_verified:
            uniq[key] = c
    candidates = list(uniq.values())
    candidates.sort(key=lambda x: x.price)

    history_rows = record_price_history(
        [c for c in candidates if c.price_verified],
        int(cfg.get("history_retention_days", 30)),
    )
    hourly_rows = build_hourly_min(history_rows)
    write_price_chart_svg(hourly_rows)
    write_dashboard(hourly_rows)

    hit_count = 0
    state_changed = False

    for c in candidates:
        limit = threshold_for(c, cfg)
        print(f"CHECK {c.price:.0f} <= {limit:.0f}? {c.platform} | {c.title[:80]}")
        if not c.price_verified:
            print(f"  -> skipped low-confidence price: {c.price_check}")
            continue
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


