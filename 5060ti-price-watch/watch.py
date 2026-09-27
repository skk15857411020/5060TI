#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
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


def parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    t = (
        text.replace(",", "")
        .replace("￥", "¥")
        .replace("元", "")
        .replace("到手", "")
        .replace("券后", "")
        .replace("补贴后", "")
        .replace("拼单价", "")
        .replace("最低", "")
    )
    # Prefer a price adjacent to ¥/￥ first.
    m = re.search(r"[¥]\s*([1-9]\d{2,4}(?:\.\d{1,2})?)", t)
    if not m:
        m = re.search(r"(?<!\d)([1-9]\d{2,4}(?:\.\d{1,2})?)(?!\d)", t)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    if 1000 <= value <= 10000:
        return value
    return None


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def is_target_gpu(title: str, cfg: dict) -> bool:
    t = title.lower().replace("-", " ").replace("_", " ")
    # Require 5060 + Ti + 8G/8GB in the same title.
    if not re.search(r"5060\s*ti", t, re.I):
        return False
    if not re.search(r"\b8\s*g(?:b)?\b|8g(?:b)?", t, re.I):
        return False

    for bad in cfg.get("exclude_keywords", []):
        if bad.lower() in t:
            return False

    # Reject 16G variants when both 8G and 16G are mixed into a generic title.
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

    # HH:MM, assume today.
    m = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text)
    if m and not re.search(r"\d{1,2}-\d{1,2}", text):
        dt = now.replace(
            hour=int(m.group(1)),
            minute=int(m.group(2)),
            second=0,
            microsecond=0,
        )
        # If parsing just after midnight and page contains late-night item.
        if dt > now + timedelta(minutes=5):
            dt -= timedelta(days=1)
        return dt

    # MM-DD HH:MM
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


def fetch_smzdm(session: requests.Session, cfg: dict) -> list[Candidate]:
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

            price = parse_price(title)
            if price is None:
                # Search card description often contains the final price.
                desc = normalize(row.get_text(" ", strip=True))
                price = parse_price(desc)
            if price is None:
                continue

            extras = normalize(
                " ".join(x.get_text(" ", strip=True) for x in row.select(".feed-block-extras"))
            )
            published = parse_smzdm_time(extras)
            if published and (now_cn() - published) > timedelta(minutes=freshness):
                continue

            full_text = normalize(row.get_text(" ", strip=True))
            platform = platform_from_text(full_text)
            out.append(
                Candidate(
                    source="什么值得买",
                    platform=platform,
                    title=title,
                    price=price,
                    url=href,
                    merchant="",
                    published_at=published.isoformat() if published else "",
                    reliability="新优惠线索",
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

            price = parse_price(price_node.get_text(" ", strip=True))
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
                )
            )
            seen.add(href)

    return out


def fetch_direct_urls(session: requests.Session, cfg: dict) -> list[Candidate]:
    """
    Optional direct URL watcher.

    Each config item may define:
      {
        "url": "...",
        "platform": "京东",
        "title_regex": "5060.*ti.*8g",
        "price_regex": "券后...([0-9.]+)"
      }

    This is intentionally generic because PDD/Taobao/Tmall frequently change HTML.
    """
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

            price = None
            if item.get("price_regex"):
                m = re.search(item["price_regex"], text, re.I | re.S)
                if m:
                    price = parse_price(m.group(1))
            if price is None:
                price = parse_price(text)
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
    # URL is the most stable key. Trim tracking fragments.
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
    return (
        "🔥【RTX 5060 Ti 8G 好价】\n"
        f"型号：{c.title}\n"
        f"平台：{c.platform}{merchant}\n"
        f"当前价：¥{c.price:.0f}\n"
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
        # Feishu webhook success usually code=0 / StatusCode=0.
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

    # Keep exact target products and deduplicate same URL/price.
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

    # Prevent unbounded state growth.
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

    if errors and len(errors) == len([s for s in sources if cfg.get("sources", {}).get(s[0], True)]):
        print("WARNING: all enabled sources failed", file=sys.stderr)
        return 2

    print(f"Done. candidates={len(candidates)}, alerts={hit_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
