"""Public discovery sources. Posts are leads, never authoritative SKU prices."""
import html
import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup


DOMAINS = {
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com"),
    "weibo": ("weibo.com", "weibo.cn"),
    "bilibili": ("bilibili.com", "b23.tv"),
    "manmanbuy": ("manmanbuy.com",),
    "gwdang": ("gwdang.com",),
    "sku": ("jd.com", "3.cn", "tmall.com", "taobao.com", "pinduoduo.com", "yangkeduo.com", "douyin.com"),
}
NAMES = {"xiaohongshu": "小红书", "weibo": "微博", "bilibili": "B站",
         "manmanbuy": "慢慢买", "gwdang": "购物党", "rss": "RSS", "sku": "商品SKU"}


def plain(value):
    return re.sub(r"\s+", " ", BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)).strip()


def published(value):
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            stamp = float(value)
            return datetime.fromtimestamp(stamp / 1000 if stamp > 10**11 else stamp, timezone.utc).isoformat()
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            try:
                result = parsedate_to_datetime(str(value))
            except ValueError:
                result = datetime.strptime(str(value), "%a %b %d %H:%M:%S %z %Y")
        # An unspecified timezone is not proof of a recent post.
        return result.isoformat() if result.tzinfo else ""
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def domain_matches(url, domains):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and any(host == domain or host.endswith("." + domain) for domain in domains))
    except ValueError:
        return False


def public_feed_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        if not host or host == "localhost" or host.endswith((".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


@dataclass
class Lead:
    source: str
    title: str
    text: str
    url: str
    published_at: str = ""
    product_urls: list = field(default_factory=list)
    page: str = ""


def product_links(soup, text="", base=""):
    urls = [urljoin(base, a.get("href", "")) for a in soup.select("a[href]")]
    urls.extend(re.findall(r"https://[^\s<>\"']+", html.unescape(text)))
    return list(dict.fromkeys(url for url in urls if domain_matches(url, DOMAINS["sku"])))[:4]


def json_state(page):
    soup = BeautifulSoup(page, "html.parser")
    for script in soup.select("script"):
        raw = script.string or script.get_text()
        if "window.__INITIAL_STATE__=" not in raw:
            continue
        raw = raw.split("window.__INITIAL_STATE__=", 1)[1].strip()
        # Replace JS undefined tokens, preserving all quoted strings. Never execute JS.
        raw = re.sub(r'("(?:\\.|[^"\\])*")|(\bundefined\b)', lambda m: m.group(1) or "null", raw)
        try:
            return json.JSONDecoder().raw_decode(raw)[0]
        except ValueError:
            continue
    return {}


def parse_xiaohongshu(page, url):
    data = json_state(page)
    records = list((data.get("note", {}).get("noteDetailMap") or {}).values())
    records.extend((data.get("search", {}).get("feeds") or []))
    leads = []
    for record in records:
        note = record.get("note") or record.get("noteCard") or record.get("note_card") or record
        title, text = plain(note.get("title") or note.get("display_title")), plain(note.get("desc"))
        identifier = note.get("noteId") or note.get("note_id") or record.get("id")
        link = url if "/explore/" in url or "/discovery/item/" in url else (
            "https://www.xiaohongshu.com/explore/" + str(identifier) if identifier else ""
        )
        if title and link:
            leads.append(Lead("小红书", title, text, link, published(note.get("time")),
                              product_links(BeautifulSoup(text, "html.parser"), text)))
    return leads


def parse_bilibili(data):
    if data.get("code") != 0:
        raise ValueError("B站接口暂时受限")
    body = data.get("data") or {}
    rows = body.get("result") if isinstance(body.get("result"), list) else [body]
    return [Lead("B站", plain(row.get("title")), plain(row.get("description") or row.get("desc")),
                 "https://www.bilibili.com/video/" + str(row["bvid"]),
                 published(row.get("pubdate")),
                 product_links(BeautifulSoup(str(row.get("description") or row.get("desc") or ""), "html.parser"),
                               str(row.get("description") or row.get("desc") or "")))
            for row in rows if row.get("bvid") and row.get("title")]


def parse_weibo(page, url):
    if page.lstrip().startswith("{"):
        item = json.loads(page)
        item = item.get("data") or item
        if item.get("text") and (item.get("id") or item.get("idstr")):
            raw = item.get("longText", {}).get("longTextContent") or item["text"]
            text = plain(raw)
            identifier = item.get("idstr") or item.get("id")
            return [Lead("微博", text[:160], text, "https://m.weibo.cn/detail/" + str(identifier),
                         published(item.get("created_at")), product_links(BeautifulSoup(raw, "html.parser"), raw))]
        return []
    soup = BeautifulSoup(page, "html.parser")
    leads = []
    for card in soup.select(".card-wrap[mid]"):
        text_node = card.select_one("p.txt")
        date_node = card.select_one(".from a[href]")
        if not text_node or not date_node:
            continue
        text = text_node.get_text(" ", strip=True)
        link = urljoin(url, date_node.get("href", ""))
        stamp = published(date_node.get("title") or date_node.get("date"))
        leads.append(Lead("微博", text[:160], text, link, stamp, product_links(card, text, url)))
    if leads:
        return leads
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            item = json.loads(script.string or script.get_text())
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("@type") in ("SocialMediaPosting", "Article"):
            text = plain(item.get("articleBody") or item.get("description"))
            return [Lead("微博", plain(item.get("headline")) or text[:160], text, url,
                         published(item.get("datePublished")), product_links(soup, text, url))]
    return []


def parse_comparison(page, url, provider):
    soup = BeautifulSoup(page, "html.parser")
    leads = []
    if provider == "manmanbuy":
        for info in soup.select('[class*="DiscountItemPC_itemInfo"]'):
            title_node = info.select_one('[class*="itemTitle"] a[href]')
            if not title_node:
                continue
            card = info.parent
            title = title_node.get_text(" ", strip=True)
            leads.append(Lead("慢慢买", title, card.get_text(" ", strip=True),
                              urljoin(url, title_node["href"]), "", product_links(card, base=url)))
        if leads:
            return leads
    # Only article/product containers, never the whole recommendation-filled page.
    body = soup.select_one("#content, .cu_content, .cu-content, .product-detail, article")
    title_node = soup.select_one("h1")
    if body and title_node:
        stamp_node = soup.select_one('time[datetime], meta[property="article:published_time"]')
        stamp = (stamp_node.get("datetime") or stamp_node.get("content")) if stamp_node else ""
        leads.append(Lead(NAMES[provider], title_node.get_text(" ", strip=True), body.get_text(" ", strip=True),
                          url, published(stamp), product_links(body, base=url)))
    return leads


def parse_feed(page, url, name="RSS"):
    if re.search(r"<!DOCTYPE|<!ENTITY", page, re.I):
        raise ValueError("不支持带实体声明的订阅数据")
    try:
        root = ElementTree.fromstring(page)
    except ElementTree.ParseError as exc:
        raise ValueError("地址未返回有效RSS/Atom数据") from exc
    if root.tag.split("}")[-1] not in ("rss", "feed", "RDF"):
        raise ValueError("地址返回的是网页，非订阅数据")
    leads = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue
        fields = {node.tag.split("}")[-1]: node for node in item}
        def text(key):
            node = fields.get(key)
            return "" if node is None else "".join(node.itertext())
        link_nodes = [node for node in item if node.tag.split("}")[-1] == "link"]
        link_node = next((node for node in link_nodes if node.get("rel", "alternate") == "alternate"), None)
        link = (link_node.get("href") or "".join(link_node.itertext())) if link_node is not None else ""
        title = plain(text("title"))
        content = text("encoded") or text("content") or text("description") or text("summary")
        link = urljoin(url, link.strip())
        if title and public_feed_url(link):
            leads.append(Lead(name, title, plain(content), link,
                              published(text("pubDate") or text("published") or text("updated")),
                              product_links(BeautifulSoup(content, "html.parser"), content, link)))
    return leads


def single_product(page):
    records = []
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            kind = value.get("@type")
            if kind == "Product" or isinstance(kind, list) and "Product" in kind:
                records.append(value)
            else:
                for item in value.values():
                    if isinstance(item, (dict, list)):
                        visit(item)
    for node in BeautifulSoup(page, "html.parser").select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(node.string or node.get_text()))
        except ValueError:
            continue
    return records[0] if len(records) == 1 else None


class Collector:
    def __init__(self, session, cfg, clock=time.monotonic):
        self.session, self.cfg, self.clock = session, cfg, clock
        self.deadline = clock() + max(0, min(float(cfg.get("budget_seconds", 40)), 60))
        self.maximum = max(0, min(int(cfg.get("max_requests", 12)), 20))
        self.calls = 0
        self.cache = {}
        self.status = []

    def fetch(self, url, provider):
        if url in self.cache:
            return self.cache[url]
        original_url = url
        origin_host = urlparse(url).hostname or ""
        for _ in range(4):
            allowed = public_feed_url(url) if provider == "rss" else domain_matches(url, DOMAINS[provider])
            if not allowed:
                raise ValueError("链接不属于该来源的公开入口")
            if provider == "rss" and (urlparse(url).hostname or "") != origin_host:
                raise ValueError("RSS跳转到其他网站，需配置最终订阅地址")
            remaining = self.deadline - self.clock()
            if self.calls >= self.maximum or remaining <= 0:
                raise TimeoutError("新增来源请求预算用尽")
            self.calls += 1
            response = self.session.get(url, headers={"User-Agent": self.cfg.get("user_agent", "Mozilla/5.0")},
                                        timeout=(min(2, remaining), min(5, remaining)),
                                        stream=True, allow_redirects=False)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(16384):
                    size += len(chunk)
                    if size > 1_500_000 or self.clock() >= self.deadline:
                        raise TimeoutError("新增来源页面过大或请求预算用尽")
                    chunks.append(chunk)
                encoding = response.encoding
                if not encoding or encoding.lower() == "iso-8859-1":
                    encoding = "utf-8"
                page = b"".join(chunks).decode(encoding, errors="replace")
                if re.search(r"/slider/verify|passport\.weibo|alival\.manmanbuy", url, re.I) or (
                    "Sina Visitor System" in page or "验证后继续访问" in page
                ):
                    raise ValueError("来源需要登录或验证码")
                self.cache[url] = (page, url)
                self.cache[original_url] = (page, url)
                return page, url
            finally:
                response.close()
        raise ValueError("来源跳转过多")

    def scan(self, provider, url, feed_name=""):
        try:
            page, final = self.fetch(url, provider)
            if provider == "bilibili":
                leads = parse_bilibili(json.loads(page))
            elif provider == "xiaohongshu":
                leads = parse_xiaohongshu(page, final)
            elif provider == "weibo":
                leads = parse_weibo(page, final)
            elif provider == "rss":
                leads = parse_feed(page, final, feed_name or "RSS")
            else:
                leads = parse_comparison(page, final, provider)
            read_count = len(leads)
            leads = [lead for lead in leads if re.search(r"5060[\s_-]*ti", lead.title + " " + lead.text, re.I)]
            leads = leads[:max(1, min(int(self.cfg.get("max_items_per_source", 20)), 50))]
            reason = "可读取公开内容" if leads else "已读取，暂无相关5060 Ti条目" if read_count else "公开页面未提供帖子数据，需具体链接或登录" if provider in ("xiaohongshu", "weibo") else "没有可读取条目"
            self.status.append({"source": feed_name or NAMES[provider], "url": url,
                                "status": "ok" if read_count else "empty", "reason": reason,
                                "items": len(leads), "read_items": read_count})
            return leads
        except Exception as exc:
            reason = str(exc)[:140] if isinstance(exc, (ValueError, TimeoutError)) else type(exc).__name__
            self.status.append({"source": feed_name or NAMES[provider], "url": url,
                                "status": "unavailable", "reason": reason, "items": 0})
            return []

    def collect(self):
        leads = []
        # Prioritize working discovery endpoints over optional login-dependent pages.
        for provider in ("manmanbuy", "bilibili", "xiaohongshu", "weibo", "gwdang"):
            settings = self.cfg.get(provider, {})
            if not settings.get("enabled", True):
                continue
            urls = list(settings.get("urls", []))[:4]
            for keyword in settings.get("search_keywords", [])[:2]:
                query = quote(keyword)
                if provider == "manmanbuy":
                    urls.append("https://s.manmanbuy.com/pc/search/result?keyword=" + query + "&c=discount")
                elif provider == "bilibili":
                    urls.append("https://api.bilibili.com/x/web-interface/search/type?search_type=video&order=pubdate&keyword=" + query)
                elif provider == "xiaohongshu":
                    urls.append("https://www.xiaohongshu.com/search_result?keyword=" + query)
                elif provider == "weibo":
                    urls.append("https://s.weibo.com/weibo?q=" + query)
                elif provider == "gwdang":
                    urls.append("https://www.gwdang.com/search?keyword=" + query)
            if not urls:
                self.status.append({"source": NAMES[provider], "status": "needs_input", "items": 0,
                                    "reason": settings.get("setup_note", "已接入，需添加可公开读取的帖子或商品链接")})
            for url in dict.fromkeys(urls):
                if provider == "bilibili" and re.search(r"/video/BV[\w]+", url):
                    identifier = re.search(r"/video/(BV[\w]+)", url).group(1)
                    url = "https://api.bilibili.com/x/web-interface/view?bvid=" + identifier
                elif provider == "weibo" and re.search(r"/(?:status|detail)/([\w]+)", url):
                    identifier = re.search(r"/(?:status|detail)/([\w]+)", url).group(1)
                    url = "https://m.weibo.cn/statuses/show?id=" + identifier
                elif provider == "weibo" and re.fullmatch(r"/[0-9]+/[\w]+", urlparse(url).path):
                    url = "https://m.weibo.cn/statuses/show?id=" + urlparse(url).path.split("/")[-1]
                leads.extend(self.scan(provider, url))
        for feed in self.cfg.get("rss", [])[:4]:
            if feed.get("enabled", True):
                leads.extend(self.scan("rss", feed.get("url", ""), feed.get("name", "RSS")))
        return list({(lead.source, lead.url): lead for lead in leads}.values())
